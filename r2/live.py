from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import defaultdict
from urllib.parse import urlparse

from openai import AsyncOpenAI

from r2.home_assistant import HAError
from r2.prompts import BACKEND_INSTRUCTIONS, VOICE_INSTRUCTIONS
from r2.timing import StartupTrace

TOOLS = [
    {"type": "function", "name": "get_locks", "description": "Read current door lock states. Target a specific permitted door name or all. This never changes a lock.", "strict": True, "parameters": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"], "additionalProperties": False}},
    {"type": "function", "name": "set_lock", "description": "Lock or unlock one explicitly requested, permitted door. Use its exact name or entity ID; never guess an ambiguous door. Only act on the user's explicit lock/unlock request. Wait for a confirmed result before claiming success. Never retry an unconfirmed action automatically.", "strict": True, "parameters": {"type": "object", "properties": {"target": {"type": "string"}, "action": {"type": "string", "enum": ["lock", "unlock"]}}, "required": ["target", "action"], "additionalProperties": False}},
    {"type": "web_search"},
    {"type": "function", "name": "get_lights", "description": "Read the current state of permitted lights. Target an exact room, light name, or all.", "strict": True, "parameters": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"], "additionalProperties": False}},
    {"type": "function", "name": "set_lights", "description": "Set permitted lights in a room or by name. Always use explicit on/off, never toggle. Brightness is 0–100; null leaves it unchanged. Set power to null for brightness-only changes. Results must be verified before announcing success.", "strict": True, "parameters": {"type": "object", "properties": {"target": {"type": "string"}, "power": {"type": ["string", "null"], "enum": ["on", "off", None]}, "brightness_pct": {"type": ["number", "null"], "minimum": 0, "maximum": 100}}, "required": ["target", "power", "brightness_pct"], "additionalProperties": False}},
]


def session_config(catalog):
    return {
        "model": "gpt-live-1",
        "instructions": VOICE_INSTRUCTIONS,
        "audio": {"format": {"type": "audio/pcm", "rate": 16000}, "output": {"voice": "marin"}},
        "delegation": {"type": "responses", "responses": {
            "model": "gpt-5.6-luna", "tools": TOOLS, "tool_choice": "auto", "parallel_tool_calls": False,
            "reasoning": {"effort": "none"},
            "instructions": BACKEND_INSTRUCTIONS + "\nPermitted device catalog (JSON reference data):\n" + json.dumps(catalog),
        }},
    }


def citations(value):
    """Collect citations from completed content as well as annotation events."""
    found = {}
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "url_citation":
                citation = node.get("url_citation", node)
                url = citation.get("url", "")
                if urlparse(url).scheme in {"http", "https"}:
                    found[url] = {"url": url, "title": citation.get("title") or urlparse(url).hostname}
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value)
    return list(found.values())


class ToolExecutor:
    def __init__(self, ha):
        self.ha = ha
        self.results = {}
        self.lock = asyncio.Lock()
        self.closed = False

    async def execute(self, item):
        call_id = item["call_id"]
        async with self.lock:
            if call_id in self.results:
                return self.results[call_id]
            result = {"status": "error", "message": "Tool unavailable"}
            try:
                if self.closed:
                    raise HAError("Conversation ended; action was not started.")
                try:
                    args = json.loads(item["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError()
                    name = item["name"]
                    expected = {"get_lights": {"target"}, "set_lights": {"target", "power", "brightness_pct"}, "get_locks": {"target"}, "set_lock": {"target", "action"}}
                    required = {"target", "action"} if name == "set_lock" else {"target"}
                    if name not in expected or set(args) - expected[name] or not required <= set(args):
                        raise ValueError()
                except (ValueError, TypeError, KeyError):
                    raise HAError("Invalid tool arguments; clarify the request.") from None
                if self.ha is None:
                    raise HAError("Home Assistant is not connected. Add its token in Settings.")
                result = await getattr(self.ha, name)(**args)
            except HAError as exc:
                result = {"status": "error", "message": str(exc)}
            except Exception as exc:
                # Execution/verification can fail after an action was dispatched.
                # Never mislabel that as bad arguments and encourage a blind retry.
                logging.getLogger("uvicorn.error").warning("R2 tool execution failed: tool=%s error=%s", name, type(exc).__name__)
                result = {"status": "error", "message": "Home Assistant execution failed. The action outcome is unconfirmed; do not claim success or retry automatically."}
            self.results[call_id] = result
            return result


class LiveSession:
    def __init__(self, key, ha, catalog, emit, settings, *, trace=None):
        self.key, self.catalog, self.emit = key, catalog, emit
        self.executor = ToolExecutor(ha)
        self.settings = settings
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.stop_reason = "Conversation ended"
        self.last_activity = time.monotonic()
        self.started_at = self.last_activity
        self.playback_until = 0.0
        self.last_spoken = 0.0
        self.frames = asyncio.Queue(maxsize=125)  # Ten seconds, including connection pre-roll.
        self.responses = defaultdict(dict)
        self.pending_responses = set()
        self.continued = set()
        self.tasks = set()
        self.connection = None
        self.closing = False
        self.trace = trace or StartupTrace()

    def activity(self):
        self.last_activity = time.monotonic()

    def feed(self, frame):
        if self.closing:
            return
        try:
            self.frames.put_nowait(frame)
        except asyncio.QueueFull:
            self.stop("Audio connection fell behind. Please try again.")

    def stop(self, reason="Conversation ended"):
        self.stop_reason = reason
        self.closing = True
        self.executor.closed = True
        self.stop_requested.set()

    async def run(self, *, prepare=None):
        tasks = []
        try:
            if self.closing:
                return
            self.trace.mark("live_run_entered")
            await self.emit({"type": "state", "state": "connecting", "message": "Connecting to GPT-Live", "startup": self.trace.snapshot()})
            if self.closing:
                return
            stop_task = asyncio.create_task(self.stop_requested.wait())
            tasks.append(stop_task)
            preparation = asyncio.create_task(prepare()) if prepare else None
            if preparation:
                tasks.append(preparation)
            self.trace.mark("sdk_client_started")
            async with AsyncOpenAI(api_key=self.key) as client:
                self.trace.mark("sdk_client_completed")
                self.trace.mark("websocket_connect_started")
                async with client.live.connect(max_retries=0) as connection:
                    self.trace.mark("websocket_opened")
                    self.connection = connection
                    if preparation:
                        await asyncio.wait([preparation, stop_task], return_when=asyncio.FIRST_COMPLETED)
                    if self.closing:
                        return
                    if preparation:
                        await preparation
                    reader = asyncio.create_task(self.receive())
                    tasks.append(reader)
                    self.trace.mark("config_assembly_started")
                    config = session_config(self.catalog)
                    self.trace.mark("config_assembly_completed")
                    await connection.session.start(session=config)
                    self.trace.mark("session_start_sent")
                    ready_task = asyncio.create_task(self.ready.wait())
                    tasks.append(ready_task)
                    done, _ = await asyncio.wait([ready_task, stop_task, reader], timeout=15, return_when=asyncio.FIRST_COMPLETED)
                    if not self.ready.is_set():
                        if reader.done():
                            await reader
                        if not self.stop_requested.is_set():
                            self.stop("GPT-Live did not start. Check model access and billing in Settings.")
                    else:
                        tasks.extend([asyncio.create_task(self.send_audio()), asyncio.create_task(self.watchdog())])
                        await asyncio.wait([stop_task, reader, *tasks[-2:]], return_when=asyncio.FIRST_COMPLETED)
                        if reader.done():
                            await reader
                        for task in tasks[-2:]:
                            if task.done():
                                await task
                    self.closing = True
                    self.executor.closed = True
                    # Already dispatched light actions may finish, but no new actions start.
                    if self.tasks:
                        await asyncio.wait(self.tasks, timeout=12)
                    if not self.finished.is_set() and self.ready.is_set():
                        await connection.session.close()
                        try:
                            await asyncio.wait_for(self.finished.wait(), 8)
                        except asyncio.TimeoutError:
                            await self.emit({"type": "notice", "message": "Connection closed; final usage could not be confirmed."})
        except Exception as exc:
            name = type(exc).__name__
            # Never expose upstream bodies, headers, or credentials to logs/UI.
            await self.emit({"type": "error", "message": "GPT-Live connection failed (" + name + "). Check API key, billing, model access and network in Settings."})
        finally:
            self.closing = True
            self.executor.closed = True
            for task in tasks + list(self.tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, *self.tasks, return_exceptions=True)
            self.trace.mark("session_ended")
            self.trace.log()
            await self.emit({"type": "session_end", "message": self.stop_reason, "startup": self.trace.snapshot()})

    async def send_audio(self):
        # Pace the buffered start at its sample rate. Drop only queued silence to
        # catch up, never drop speech from an immediate wake-phrase command.
        while not self.closing:
            frame = await self.frames.get()
            if self.frames.qsize() > 3:
                import numpy as np
                if np.max(np.abs(np.frombuffer(frame, dtype="<i2").astype(np.int32))) < 400:
                    continue
            before = time.monotonic()
            await self.connection.session.input_audio.append(audio=base64.b64encode(frame).decode())
            self.trace.mark("first_user_audio_sent")
            await asyncio.sleep(max(0, len(frame) / 32000 - (time.monotonic() - before)))

    async def watchdog(self):
        while not self.closing:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if now - self.started_at >= self.settings["max_seconds"]:
                self.stop("Five-minute limit reached. Say Hey R2 to start again.")
            elif not self.pending_responses and not self.tasks and now > self.playback_until and now - self.last_activity >= self.settings["idle_seconds"]:
                self.stop("Back to local wake-word listening")

    async def receive(self):
        async for event in self.connection:
            data = event.model_dump() if hasattr(event, "model_dump") else event
            kind = data.get("type", "")
            if kind == "session.started":
                self.trace.mark("session_ready")
                self.ready.set()
                self.activity()
                await self.emit({"type": "state", "state": "listening", "message": "Go ahead, I’m listening", "startup": self.trace.snapshot()})
                self.trace.mark("ui_ready_sent")
            elif kind == "session.output_audio.delta":
                if not self.closing:
                    self.trace.mark("first_assistant_audio_received")
                    audio = data.get("delta", "")
                    import numpy as np
                    pcm = base64.b64decode(audio)
                    signal = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
                    now = time.monotonic()
                    if signal.size and np.max(np.abs(signal)) > 160:
                        self.last_spoken = now
                        self.activity()
                    # Live may stream silence. It must not keep the paid session
                    # alive indefinitely or appear as speaking in the browser.
                    if now - self.last_spoken < 0.3:
                        self.trace.mark("first_assistant_audio_forwarded")
                        self.playback_until = max(self.playback_until, now) + len(pcm) / 32000
                        await self.emit({"type": "audio", "audio": audio})
            elif kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
                if data.get("delta", "").strip():
                    self.activity()
                await self.emit({"type": "transcript", "role": "user" if "input_" in kind else "assistant", "delta": data.get("delta", ""), "start_ms": data.get("start_ms"), "end_ms": data.get("end_ms")})
            elif kind == "response.event":
                await self.backend_event(data)
            elif kind == "session.closed":
                self.finished.set()
                await self.emit({"type": "usage", "voice": data.get("usage")})
                break
            elif kind == "error":
                error = data.get("error", {})
                code = error.get("code", "")
                message = {
                    "credit_balance_exhausted": "Your OpenAI API credit balance is empty. Add credits at platform.openai.com/settings/organization/billing, then try again.",
                    "insufficient_quota": "Your OpenAI API quota is unavailable. Check API billing and project limits, then try again.",
                    "invalid_api_key": "OpenAI rejected the API key. Update it in Settings.",
                    "model_not_found": "This OpenAI project cannot access the configured model. Check access to gpt-live-1 and gpt-5.6-luna.",
                    "rate_limit_exceeded": "OpenAI is rate limiting this project. Wait briefly before starting another conversation.",
                }.get(code, "OpenAI rejected a session command (" + str(code or "unknown error") + "). Check model access and configuration.")
                await self.emit({"type": "error", "message": message})
                self.stop("Session error")
            elif kind == "session.delegation.created":
                self.activity()

    async def backend_event(self, envelope):
        event = envelope.get("event", {})
        kind = event.get("type")
        response_id = event.get("response_id") or event.get("response", {}).get("id") or envelope.get("response_id") or envelope.get("delegation_id")
        # All events of a delegation share this bucket even if event payloads omit response_id.
        bucket = envelope.get("delegation_id") or response_id
        if kind == "response.created":
            self.pending_responses.add(bucket)
            self.responses[bucket] = {}
        elif kind == "response.output_item.done":
            item = event.get("item", {})
            if item.get("type") == "function_call":
                self.responses[bucket][item["call_id"]] = item
            elif item.get("type") == "web_search_call":
                await self.emit({"type": "tool", "name": "web_search", "result": {"status": item.get("status", "completed")}})
        if "web_search_call" in str(kind):
            await self.emit({"type": "state", "state": "working", "message": "Searching the web"})
        sources = citations(event)
        if sources:
            await self.emit({"type": "sources", "sources": sources})
        if kind in {"response.completed", "response.failed", "response.cancelled", "response.incomplete"}:
            self.pending_responses.discard(bucket)
            self.activity()
            if kind != "response.completed":
                await self.emit({"type": "notice", "message": "The backend could not finish that request. Please try again."})
                return
            response_status = event.get("response", {}).get("status")
            if response_status in {"failed", "incomplete", "cancelled"}:
                await self.emit({"type": "notice", "message": "The request did not complete."})
                return
            items = list(self.responses.pop(bucket, {}).values())
            continuation_key = response_id or tuple(x["call_id"] for x in items)
            if items and continuation_key not in self.continued and not self.closing:
                self.continued.add(continuation_key)
                self.pending_responses.add(bucket)
                task = asyncio.create_task(self.run_tools(items, bucket))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            if event.get("response", {}).get("usage"):
                await self.emit({"type": "usage", "backend": event["response"]["usage"]})

    async def run_tools(self, items, bucket):
        try:
            for item in items:
                result = await self.executor.execute(item)
                await self.emit({"type": "tool", "name": item["name"], "result": result})
                await self.connection.response.item.create(item={"type": "function_call_output", "call_id": item["call_id"], "output": json.dumps(result)})
            if not self.closing:
                await self.connection.response.create()
            else:
                self.pending_responses.discard(bucket)
        except Exception:
            self.pending_responses.discard(bucket)
            self.stop("Tool result could not be delivered; check the device before retrying.")
