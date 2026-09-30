import asyncio
import base64
import time
from types import SimpleNamespace

import pytest

from r2.live import LiveSession, citations, session_config


def test_tools_and_source_links():
    tools = session_config([])["delegation"]["responses"]["tools"]
    assert any(x["type"] == "web_search" for x in tools)
    assert {x.get("name") for x in tools if x["type"] == "function"} == {"get_lights", "set_lights", "get_locks", "set_lock"}
    data = {"annotations": [{"type": "url_citation", "url": "https://example.com/article", "title": "Article"}, {"type": "url_citation", "url": "javascript:alert(1)"}]}
    assert citations(data) == [{"url": "https://example.com/article", "title": "Article"}]


def make_session():
    messages = []
    async def emit(value): messages.append(value)
    return LiveSession("test", None, [], emit, {"idle_seconds": .01, "max_seconds": 300}), messages


@pytest.mark.asyncio
async def test_idle_ends_session_but_pending_tool_and_playback_prevent_timeout():
    session, _ = make_session()
    session.last_activity -= 5
    session.pending_responses.add("r1")
    task = asyncio.create_task(session.watchdog())
    await asyncio.sleep(.55)
    assert not session.closing
    session.pending_responses.clear()
    session.playback_until = time.monotonic() + 3
    await asyncio.sleep(.55)
    assert not session.closing
    session.playback_until = 0
    await asyncio.wait_for(task, 1)
    assert session.closing


@pytest.mark.asyncio
async def test_hard_limit_ends_even_with_pending_backend():
    session, _ = make_session()
    session.started_at -= 301
    session.pending_responses.add("r1")
    await asyncio.wait_for(session.watchdog(), 1)
    assert session.closing and "Five-minute" in session.stop_reason


@pytest.mark.asyncio
async def test_function_output_then_explicit_continuation():
    session, messages = make_session()
    sent = []
    async def output(**kwargs): sent.append(("item",kwargs))
    async def continuation(**kwargs): sent.append(("continue",kwargs))
    session.connection = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=output),create=continuation))
    env = {"delegation_id": "d1"}
    await session.backend_event(env | {"event": {"type": "response.created", "response": {"id": "r1"}}})
    await session.backend_event(env | {"event": {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "call1", "name": "get_lights", "arguments": '{"target":"Bedroom"}'}}})
    completed = env | {"event": {"type": "response.completed", "response": {"id": "r1", "output": []}}}
    await session.backend_event(completed)
    await asyncio.gather(*session.tasks)
    assert [x[0] for x in sent] == ["item", "continue"]
    assert sent[0][1]["item"]["call_id"] == "call1"
    await session.backend_event(completed)
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_no_cloud_open_after_early_stop(monkeypatch):
    session, messages = make_session()
    session.stop()
    def forbidden(**kwargs): raise AssertionError("Cloud connection opened after stop")
    monkeypatch.setattr("r2.live.AsyncOpenAI", forbidden)
    await session.run()
    assert messages[-1]["type"] == "session_end"


@pytest.mark.asyncio
async def test_silent_output_does_not_keep_session_alive():
    session, messages = make_session()
    class Connection:
        async def __aiter__(self):
            yield {"type": "session.output_audio.delta", "delta": base64.b64encode(bytes(2560)).decode()}
            yield {"type": "session.closed", "usage": {"seconds": 1}}
    session.connection = Connection()
    await session.receive()
    assert not any(x["type"] == "audio" for x in messages)
    assert session.playback_until == 0


@pytest.mark.asyncio
async def test_billing_failure_has_actionable_message():
    session, messages = make_session()
    class Connection:
        async def __aiter__(self):
            yield {"type": "error", "error": {"code": "credit_balance_exhausted"}}
    session.connection = Connection()
    await session.receive()
    assert session.closing
    assert "credit balance is empty" in messages[0]["message"]


class StartupConnection:
    def __init__(self):
        self.opening = asyncio.Event()
        self.allow_open = asyncio.Event()
        self.started = asyncio.Event()
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.session = SimpleNamespace(start=self.start, close=self.close,
                                       input_audio=SimpleNamespace(append=self.append))

    async def __aenter__(self):
        self.opening.set()
        await self.allow_open.wait()
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def __aiter__(self):
        while True:
            yield await self.events.get()

    async def start(self, *, session):
        self.config = session
        self.started.set()

    async def append(self, *, audio):
        self.sent.append(base64.b64decode(audio))

    async def close(self):
        await self.events.put({"type": "session.closed"})


def startup_client(monkeypatch, connection):
    class Client:
        def __init__(self, **kwargs):
            self.live = SimpleNamespace(connect=self.connect)
        def connect(self, **kwargs):
            assert kwargs["max_retries"] == 0
            return connection
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr("r2.live.AsyncOpenAI", Client)


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_first", [False, True])
async def test_parallel_startup_keeps_config_and_opening_audio(monkeypatch, catalog_first):
    session, messages = make_session()
    session.settings["idle_seconds"] = 30
    connection = StartupConnection()
    startup_client(monkeypatch, connection)
    preparing, allow_catalog = asyncio.Event(), asyncio.Event()
    async def prepare():
        preparing.set()
        await allow_catalog.wait()
        session.catalog = [{"name": "Fresh door name", "state": "locked"}]
    # Unique non-silent frames represent the immediate opening utterance.
    frames = [(500 + i).to_bytes(2, "little", signed=True) * 1280 for i in range(8)]
    for frame in frames[:6]: session.feed(frame)
    task = asyncio.create_task(session.run(prepare=prepare))
    try:
        await asyncio.wait_for(asyncio.gather(preparing.wait(), connection.opening.wait()), 1)
        (allow_catalog if catalog_first else connection.allow_open).set()
        await asyncio.sleep(.01)
        assert not connection.started.is_set()
        (connection.allow_open if catalog_first else allow_catalog).set()
        await asyncio.wait_for(connection.started.wait(), 1)
        assert "Fresh door name" in connection.config["delegation"]["responses"]["instructions"]
        for frame in frames[6:]: session.feed(frame)
        await asyncio.sleep(.02)
        assert connection.sent == []  # Neither open nor session.start permits audio.
        await connection.events.put({"type": "session.started"})
        await asyncio.wait_for(session.ready.wait(), 1)
        async def drained():
            while len(connection.sent) < len(frames): await asyncio.sleep(.01)
        await asyncio.wait_for(drained(), 2)
        assert connection.sent == frames  # Exactly once, in order, including pre-roll.
        assert session.frames.empty()
        assert any(m.get("state") == "listening" for m in messages)
        assert session.trace.marks["first_user_audio_sent"] >= session.trace.marks["session_ready"]
    finally:
        session.stop()
        await asyncio.wait_for(task, 1)
    assert connection.closed


@pytest.mark.asyncio
async def test_stop_during_parallel_catalog_cleans_up_without_start(monkeypatch):
    session, _ = make_session()
    connection = StartupConnection()
    connection.allow_open.set()
    startup_client(monkeypatch, connection)
    preparing, cancelled = asyncio.Event(), asyncio.Event()
    async def prepare():
        preparing.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    task = asyncio.create_task(session.run(prepare=prepare))
    await asyncio.wait_for(preparing.wait(), 1)
    session.stop()
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set() and connection.closed
    assert not connection.started.is_set() and connection.sent == []


def test_startup_backlog_is_bounded_and_stops_instead_of_losing_speech():
    session, _ = make_session()
    frame = (500).to_bytes(2, "little") * 1280
    for _ in range(126): session.feed(frame)
    assert session.frames.qsize() == 125
    assert session.closing and "fell behind" in session.stop_reason


@pytest.mark.asyncio
async def test_failed_handshake_cancels_catalog_work(monkeypatch):
    session, messages = make_session()
    preparing, cancelled = asyncio.Event(), asyncio.Event()
    class FailedConnection(StartupConnection):
        async def __aenter__(self):
            await preparing.wait()
            raise ConnectionError("simulated handshake failure")
    startup_client(monkeypatch, FailedConnection())
    async def prepare():
        preparing.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    await asyncio.wait_for(session.run(prepare=prepare), 1)
    assert cancelled.is_set()
    assert any(m["type"] == "error" for m in messages)
    assert messages[-1]["type"] == "session_end"


@pytest.mark.asyncio
async def test_disconnect_on_connecting_notice_does_not_open_cloud(monkeypatch):
    session, _ = make_session()
    async def disconnected(message): session.stop("Browser disconnected")
    session.emit = disconnected
    def forbidden(**kwargs): raise AssertionError("Opened after browser disconnected")
    monkeypatch.setattr("r2.live.AsyncOpenAI", forbidden)
    await session.run()
    assert session.closing
