from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
import wave
from collections import deque
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from r2.config import Credentials, DATA, MODELS, ROOT, read_settings, save_settings, validate_ha_url
from r2.home_assistant import HomeAssistant, normalize
from r2.ha_transport import prepare_address
from r2.live import LiveSession
from r2.timing import StartupTrace
from r2.wake import WakeDetector

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
CSRF = secrets.token_urlsafe(32)
ORIGINS = {"http://127.0.0.1:8765", "http://localhost:8765"}
active_channel = None
training_task = None
training_status = {"running": False, "message": ""}


def credentials():
    return Credentials()


@app.middleware("http")
async def protect(request: Request, call_next):
    if request.method not in {"GET", "HEAD"}:
        if request.headers.get("origin") not in ORIGINS or not secrets.compare_digest(request.headers.get("x-r2-csrf", ""), CSRF):
            return JSONResponse({"detail": "Open R2 from its local browser page."}, status_code=403)
        if int(request.headers.get("content-length", "0")) > 2_000_000:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' ws://127.0.0.1:8765 ws://localhost:8765; img-src 'self' data:; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    return response


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
async def status():
    vault = credentials()
    settings = read_settings()
    ready = (MODELS / "hey_r2.onnx").exists()
    report = MODELS / "training_report.json"
    return {"csrf": CSRF, "openai_configured": bool(vault.get("openai_api_key")), "ha_configured": bool(vault.get("ha_token")), "ha_url": settings["ha_url"], "allowed_count": len(settings["allowed_entities"]), "allowed_lock_count": len(settings["allowed_locks"]), "voice_active": bool(active_channel and active_channel.live), "wake_ready": ready, "wake_validated": settings["wake_validated"], "wake_threshold": settings["wake_threshold"], "training": training_status, "training_report": json.loads(report.read_text()) if report.exists() else None}


def make_ha():
    settings = read_settings()
    token = credentials().get("ha_token")
    return HomeAssistant(settings["ha_url"], token, settings["allowed_entities"], settings["allowed_locks"], settings["lock_names"]) if token else None


def require_idle_ha_settings():
    if active_channel and active_channel.live:
        raise HTTPException(409, "End the current conversation before changing Home Assistant settings.")


@app.get("/api/lights")
async def lights():
    ha = make_ha()
    if not ha:
        return {"lights": []}
    try:
        return {"lights": await ha.catalog()}
    except Exception:
        raise HTTPException(502, "Home Assistant is not reachable. Check its address and access token.")


@app.post("/api/settings")
async def settings_update(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid settings")
    if body.get("ha_token") or body.get("ha_url"):
        require_idle_ha_settings()
    vault = credentials()
    updates = {}
    messages = []
    for field in {"openai_api_key", "ha_token"}:
        value = body.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > 8192):
            raise HTTPException(400, "Invalid credential")
    if body.get("openai_api_key"):
        key = body["openai_api_key"].strip()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get("https://api.openai.com/v1/models/gpt-live-1", headers={"Authorization": "Bearer " + key})
            if response.status_code == 401:
                raise HTTPException(400, "OpenAI rejected that API key.")
            if response.status_code != 200:
                raise HTTPException(400, "The key could not access gpt-live-1. Check project model access and billing.")
        except httpx.HTTPError:
            raise HTTPException(502, "Could not reach OpenAI to validate the key.")
        vault.set("openai_api_key", key)
        messages.append("OpenAI key verified and saved to Windows Credential Manager.")
    if body.get("ha_token") or body.get("ha_url"):
        try:
            url = validate_ha_url(body.get("ha_url") or read_settings()["ha_url"])
            token = (body.get("ha_token") or vault.get("ha_token") or "").strip()
            if not token:
                raise HTTPException(400, "Enter a Home Assistant access token.")
            previous = read_settings()
            allowed = previous["allowed_entities"] if previous["allowed_entities"] and previous["ha_url"] == url else None
            catalog = await HomeAssistant(url, token, allowed).catalog()
            if not catalog:
                raise HTTPException(400, "Connected, but none of the seven expected Tapo lights were found. Check their device names in Home Assistant.")
            # Validation awaits the network; a conversation may have begun meanwhile.
            require_idle_ha_settings()
            updates.update(ha_url=url, allowed_entities=[item["entity_id"] for item in catalog])
            if previous["ha_url"] != url:
                updates.update(allowed_locks=[], lock_names={})
            vault.set("ha_token", token)
            messages.append(f"Home Assistant connected: {len(catalog)} permitted lights.")
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception:
            raise HTTPException(400, "Home Assistant connection failed. Verify its address and access token.")
    if "wake_threshold" in body:
        value = body["wake_threshold"]
        if type(value) not in {float, int} or not 0.1 <= value <= 0.99:
            raise HTTPException(400, "Wake threshold must be between 0.1 and 0.99.")
        updates.update(wake_threshold=value, wake_validated=False)
    if body.get("wake_validated") is True:
        if not (MODELS / "hey_r2.onnx").exists():
            raise HTTPException(400, "Train the wake model first.")
        updates["wake_validated"] = True
    if updates:
        save_settings(updates)
    return {"message": " ".join(messages) or "Settings saved."}


@app.get("/api/locks")
async def locks(discover: bool = False):
    ha = make_ha()
    if not ha:
        return {"locks": []}
    try:
        catalog = await ha.lock_catalog(discover=discover)
        return {"locks": [item | {"enabled": item["entity_id"] in ha.allowed_locks} for item in catalog]}
    except Exception:
        raise HTTPException(502, "Home Assistant locks are not reachable. Check its address and access token.")


@app.post("/api/locks/settings")
async def lock_settings(request: Request):
    if active_channel and active_channel.live:
        raise HTTPException(409, "End the current conversation before changing door settings.")
    body = await request.json()
    entries = body.get("locks") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise HTTPException(400, "Choose the door locks to enable.")
    allowed, names = [], {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"entity_id", "name"}:
            raise HTTPException(400, "Each lock needs an entity ID and a door name.")
        entity_id, name = entry["entity_id"], entry["name"]
        if not isinstance(entity_id, str) or not entity_id.startswith("lock.") or entity_id in allowed:
            raise HTTPException(400, "Choose each door lock only once.")
        if not isinstance(name, str) or not normalize(name) or len(name) > 80:
            raise HTTPException(400, "Give each door a name of 1–80 characters.")
        if normalize(name) in {normalize(value) for value in names.values()}:
            raise HTTPException(400, "Give each door a different name, such as Front door and Back door.")
        allowed.append(entity_id)
        names[entity_id] = name.strip()
    ha = make_ha()
    if not ha:
        raise HTTPException(400, "Connect Home Assistant first.")
    try:
        available = {item["entity_id"] for item in await ha.lock_catalog(discover=True)}
    except Exception:
        raise HTTPException(502, "Could not read Home Assistant locks. No door settings were saved.")
    if set(allowed) - available:
        raise HTTPException(400, "A selected lock is not available in Home Assistant. Refresh the list.")
    if active_channel and active_channel.live:
        raise HTTPException(409, "End the current conversation before changing door settings.")
    save_settings({"allowed_locks": allowed, "lock_names": names})
    return {"message": f"Saved {len(allowed)} doors for voice control."}


@app.post("/api/calibration/{label}")
async def calibration_clip(label: str, request: Request):
    if label not in {"positive", "negative"}:
        raise HTTPException(400, "Invalid recording label")
    data = await request.body()
    if not 32000 <= len(data) <= 32000 * 15 or len(data) % 2:
        raise HTTPException(400, "Record between 1 and 15 seconds of 16 kHz PCM audio.")
    directory = DATA / "calibration" / label
    directory.mkdir(parents=True, exist_ok=True)
    with wave.open(str(directory / (uuid.uuid4().hex + ".wav")), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(data)
    return {"count": len(list(directory.glob("*.wav"))), "message": "Recording saved locally for wake-word training."}


@app.post("/api/train")
async def train():
    global training_task
    if training_status["running"]:
        return training_status
    training_status.update(running=True, message="Training Hey R2 locally…")
    async def worker():
        import sys
        DATA.mkdir(exist_ok=True)
        try:
            with (DATA / "training.log").open("w") as log:
                process = await asyncio.create_subprocess_exec(sys.executable, "-u", str(ROOT / "scripts" / "train_wake.py"), cwd=ROOT, stdout=log, stderr=log)
                code = await process.wait()
            if code:
                training_status.update(running=False, message="Training failed. See data/training.log.")
            else:
                save_settings({"wake_validated": False})
                training_status.update(running=False, message="Model trained. Test it with your microphone before marking it ready.")
        except Exception:
            training_status.update(running=False, message="Could not start the local training process.")
    training_task = asyncio.create_task(worker())
    return {"running": True}


class VoiceChannel:
    def __init__(self, ws):
        self.ws = ws
        self.send_lock = asyncio.Lock()
        self.live = None
        self.live_task = None
        self.detector = None
        self.armed = False
        self.muted = False
        self.calibrating = False
        self.pre_roll = deque(maxlen=6)  # 480 ms, kept only in memory.
        self.last_score_sent = 0
        self.last_model_check = 0
        self.cooldown_until = 0
        self.capture_ready_at = None
        self.address_task = None

    async def prepare_address(self):
        try:
            await prepare_address(read_settings()["ha_url"])
        except (OSError, TimeoutError):
            pass  # Activation will report any real connection failure.

    async def cancel_preparation(self):
        if self.address_task:
            self.address_task.cancel()
            await asyncio.gather(self.address_task, return_exceptions=True)
            self.address_task = None

    async def emit(self, message):
        async with self.send_lock:
            try:
                await self.ws.send_json(message)
            except (RuntimeError, WebSocketDisconnect):
                if self.live:
                    self.live.stop("Browser disconnected")

    async def activate(self, pre_roll=False, trace=None):
        if self.live_task and not self.live_task.done():
            return
        trace = trace or StartupTrace("button")
        trace.mark("startup_handler_entered")
        if self.capture_ready_at is not None:
            trace.mark("audio_capture_ready", self.capture_ready_at)
        trace.mark("credentials_started")
        key = credentials().get("openai_api_key")
        if not key:
            await self.emit({"type": "error", "message": "Add your OpenAI API key in Settings before starting a conversation."})
            self.cooldown_until = time.monotonic() + 5
            return
        ha = make_ha()
        if ha:
            ha.keep_alive = True
        trace.mark("credentials_completed")
        catalog = []
        trace.mark("settings_started")
        settings = read_settings()
        trace.mark("settings_completed")
        self.live = LiveSession(key, ha, catalog, self.emit, settings, trace=trace)
        # Queue speech immediately while HA discovery and the cloud handshake run.
        if pre_roll:
            for frame in self.pre_roll:
                self.live.feed(frame)
        self.pre_roll.clear()
        session = self.live
        async def prepare_catalog():
            if ha:
                trace.mark("catalog_started")
                try:
                    session.catalog = await asyncio.wait_for(ha.voice_catalog(), 8)
                except Exception:
                    await self.emit({"type": "notice", "message": "Home Assistant is unavailable. Web search still works."})
                finally:
                    trace.mark("catalog_completed")
        async def run():
            try:
                # Fetch fresh permissions/state while the independent cloud socket opens.
                # LiveSession still waits for the catalog before sending session.start.
                await session.run(prepare=prepare_catalog)
            finally:
                try:
                    if ha:
                        await ha.aclose()
                finally:
                    self.live = None
                    self.cooldown_until = time.monotonic() + 2
                    self.pre_roll.clear()
                    if self.detector:
                        self.detector.reset()
        self.live_task = asyncio.create_task(run())

    async def command(self, data):
        action = data.get("type")
        if action == "arm":
            self.armed = True
            self.calibrating = bool(data.get("calibrate"))
            await self.cancel_preparation()
            if not self.calibrating:
                # DNS only: no credentials, microphone audio, or paid session.
                self.address_task = asyncio.create_task(self.prepare_address())
            try:
                self.detector = await asyncio.to_thread(WakeDetector, read_settings()["wake_threshold"])
                await self.emit({"type": "state", "state": "idle", "message": "Listening locally for Hey R2"})
            except Exception:
                self.detector = None
                await self.emit({"type": "notice", "message": "Wake-word model is not ready. Use Talk or train it in Settings."})
        elif action == "activate" and not self.calibrating:
            self.muted = False
            await self.activate()
        elif action in {"stop", "disarm"}:
            if self.live:
                self.live.stop()
            if action == "disarm":
                self.armed = False
                await self.cancel_preparation()
            self.pre_roll.clear()
        elif action == "mute":
            self.muted = bool(data.get("muted"))
            self.pre_roll.clear()
        elif action == "playback" and self.live:
            seconds = min(15, max(0, float(data.get("seconds", 0))))
            self.live.playback_until = time.monotonic() + seconds

    async def frame(self, frame):
        if len(frame) != 2560:
            raise ValueError("Expected 80 ms PCM16 audio at 16 kHz")
        if self.capture_ready_at is None:
            self.capture_ready_at = time.monotonic()
        if self.muted:
            if self.live:
                self.live.feed(bytes(len(frame)))
            return
        if self.live:
            self.live.feed(frame)
            return
        if not self.armed:
            return
        self.pre_roll.append(frame)
        if self.detector and time.monotonic() - self.last_model_check > 5:
            self.last_model_check = time.monotonic()
            path = MODELS / "hey_r2.onnx"
            if path.exists() and path.stat().st_mtime_ns != getattr(self.detector, "modified_at", path.stat().st_mtime_ns):
                self.detector = await asyncio.to_thread(WakeDetector, read_settings()["wake_threshold"])
        if self.detector and time.monotonic() >= self.cooldown_until:
            detected, score = await asyncio.to_thread(self.detector.process, frame)
            trace = StartupTrace("wake") if detected and not self.calibrating else None
            if trace:
                trace.mark("wake_word_detected", trace.started)
            if time.monotonic() - self.last_score_sent > 0.25:
                await self.emit({"type": "wake_score", "score": score})
                self.last_score_sent = time.monotonic()
            if detected:
                self.cooldown_until = time.monotonic() + 2
                await self.emit({"type": "wake", "calibrating": self.calibrating})
                if not self.calibrating:
                    await self.activate(pre_roll=True, trace=trace)
                # Active conversations bypass the detector; it is reset on session end.
                # Reset now only for local tests or activation without a session (no key).
                if self.live is None:
                    self.detector.reset()


@app.websocket("/ws")
async def voice(ws: WebSocket):
    global active_channel
    protocols = ws.headers.get("sec-websocket-protocol", "").split(",")
    if ws.headers.get("origin") not in ORIGINS or not any(secrets.compare_digest(x.strip(), CSRF) for x in protocols):
        await ws.close(code=1008)
        return
    if active_channel is not None:
        await ws.close(code=1013, reason="R2 is already open in another tab")
        return
    await ws.accept(subprotocol="r2")
    channel = active_channel = VoiceChannel(ws)
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await channel.frame(message["bytes"])
            elif message.get("text"):
                if len(message["text"]) > 4096:
                    break
                await channel.command(json.loads(message["text"]))
    except (WebSocketDisconnect, ValueError, RuntimeError):
        pass
    finally:
        await channel.cancel_preparation()
        if channel.live:
            channel.live.stop("Browser disconnected")
        if channel.live_task:
            try:
                await asyncio.wait_for(asyncio.shield(channel.live_task), 22)
            except Exception:
                channel.live_task.cancel()
                await asyncio.gather(channel.live_task, return_exceptions=True)
        active_channel = None


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
