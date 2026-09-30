import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from r2 import server
from r2.config import validate_ha_url


class Vault:
    def __init__(self): self.data = {}
    def get(self, name): return self.data.get(name)
    def set(self, name, value): self.data[name] = value


@pytest.fixture
def client(monkeypatch, tmp_path):
    vault = Vault()
    monkeypatch.setattr(server, "credentials", lambda: vault)
    monkeypatch.setattr("r2.config.SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr("r2.config.DATA", tmp_path)
    async def prepare(url): pass
    monkeypatch.setattr(server, "prepare_address", prepare)
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def test_status_contains_no_credentials(client):
    data = client.get("/api/status").json()
    assert data["openai_configured"] is False
    assert "openai_api_key" not in data and "ha_token" not in data
    assert len(data["csrf"]) > 20


def test_cross_origin_and_missing_csrf_rejected(client):
    token = client.get("/api/status").json()["csrf"]
    assert client.post("/api/settings", json={}).status_code == 403
    assert client.post("/api/settings", json={}, headers={"Origin": "https://evil.example", "X-R2-CSRF": token}).status_code == 403
    assert client.post("/api/settings", json={}, headers={"Origin": "http://127.0.0.1:8765", "X-R2-CSRF": token}).status_code == 200
    assert client.get("/", headers={"Host":"evil.example"}).status_code == 400


def test_static_page_and_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200 and "Start Listening" in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/static/app.js").status_code == 200


def test_ha_url_validation():
    assert validate_ha_url("http://homeassistant.local:8123/") == "http://homeassistant.local:8123"
    assert validate_ha_url("http://192.168.40.20:8123")
    for url in ["https://evil.example", "http://user:password@localhost", "http://localhost/?key=x", "file:///C:/test"]:
        with pytest.raises(ValueError): validate_ha_url(url)


def test_idle_wake_frames_never_create_live_session(client, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("Idle audio opened a cloud connection")
    monkeypatch.setattr(server, "LiveSession", forbidden)
    class QuietWake:
        def __init__(self, threshold): pass
        def process(self, frame): return False, 0.0
        def reset(self): pass
    monkeypatch.setattr(server, "WakeDetector", QuietWake)
    token = client.get("/api/status").json()["csrf"]
    with client.websocket_connect("/ws", subprotocols=["r2",token], headers={"Origin":"http://127.0.0.1:8765"}) as ws:
        ws.send_json({"type":"arm"})
        assert ws.receive_json()["state"] == "idle"
        ws.send_bytes(bytes(2560))
        assert ws.receive_json()["type"] == "wake_score"
        ws.send_json({"type":"disarm"})


def door_headers(client):
    return {"Origin": "http://127.0.0.1:8765", "X-R2-CSRF": client.get("/api/status").json()["csrf"]}


def fake_locks(monkeypatch):
    class HA:
        allowed_locks = ["lock.front"]
        async def lock_catalog(self, discover=False):
            return [{"entity_id": "lock.front", "name": "Front door"}] + ([{"entity_id": "lock.back", "name": "Back door"}] if discover else [])
    monkeypatch.setattr(server, "make_ha", lambda: HA())


def test_lock_discovery_and_validated_settings_persist(client, monkeypatch):
    from r2.config import read_settings
    fake_locks(monkeypatch)
    assert len(client.get("/api/locks").json()["locks"]) == 1
    discovered = client.get("/api/locks?discover=true").json()["locks"]
    assert len(discovered) == 2 and discovered[0]["enabled"] and not discovered[1]["enabled"]
    body = {"locks": [{"entity_id": "lock.front", "name": "Front door"}, {"entity_id": "lock.back", "name": "Back door"}]}
    headers = door_headers(client)
    assert client.post("/api/locks/settings", json=body).status_code == 403
    assert client.post("/api/locks/settings", json=body, headers=headers).status_code == 200
    assert read_settings()["allowed_locks"] == ["lock.front", "lock.back"]
    assert read_settings()["lock_names"] == {"lock.front": "Front door", "lock.back": "Back door"}
    assert client.get("/api/status").json()["allowed_lock_count"] == 2
    assert client.post("/api/locks/settings", json={"locks": []}, headers=headers).status_code == 200
    assert read_settings()["allowed_locks"] == []


@pytest.mark.parametrize("entries", [None, ["bad"], [{"entity_id": "light.front", "name": "Front door"}], [{"entity_id": "lock.other", "name": "Other"}], [{"entity_id": "lock.front", "name": "Front door"}, {"entity_id": "lock.back", "name": "front door"}], [{"entity_id": "lock.front", "name": "Front door"}, {"entity_id": "lock.front", "name": "Back door"}]])
def test_invalid_door_settings_do_not_change_permissions(client, monkeypatch, entries):
    from r2.config import read_settings
    fake_locks(monkeypatch)
    response = client.post("/api/locks/settings", json={"locks": entries}, headers=door_headers(client))
    assert response.status_code == 400
    assert read_settings()["allowed_locks"] == []


def test_door_settings_rejected_during_conversation(client, monkeypatch):
    from types import SimpleNamespace
    fake_locks(monkeypatch)
    monkeypatch.setattr(server, "active_channel", SimpleNamespace(live=object()))
    assert client.get("/api/status").json()["voice_active"] is True
    assert client.post("/api/locks/settings", json={"locks": []}, headers=door_headers(client)).status_code == 409


@pytest.mark.asyncio
async def test_wake_reset_is_deferred_and_preroll_not_duplicated(monkeypatch):
    from types import SimpleNamespace
    messages = []
    async def send_json(message): messages.append(message)
    channel = server.VoiceChannel(SimpleNamespace(send_json=send_json))
    class Detector:
        resets = 0
        def process(self, frame): return True, 1.0
        def reset(self): self.resets += 1
    channel.detector = Detector()
    channel.armed = True
    vault = Vault()
    vault.data["openai_api_key"] = "test"
    monkeypatch.setattr(server, "credentials", lambda: vault)
    monkeypatch.setattr(server, "make_ha", lambda: None)
    async def run(session, **kwargs): await session.stop_requested.wait()
    monkeypatch.setattr(server.LiveSession, "run", run)
    frames = [(500 + i).to_bytes(2, "little") * 1280 for i in range(7)]
    channel.pre_roll.extend(frames[:5])
    await channel.frame(frames[5])
    assert channel.detector.resets == 0
    await channel.frame(frames[6])
    assert [channel.live.frames.get_nowait() for _ in range(7)] == frames
    assert channel.live.frames.empty()
    assert channel.live.trace.marks["wake_word_detected"] == 0
    channel.live.stop()
    await asyncio.wait_for(channel.live_task, 1)
    assert channel.detector.resets == 1 and channel.live is None


@pytest.mark.asyncio
@pytest.mark.parametrize("calibrating", [False, True])
async def test_wake_still_resets_without_cloud_session(monkeypatch, calibrating):
    from types import SimpleNamespace
    async def send_json(message): pass
    channel = server.VoiceChannel(SimpleNamespace(send_json=send_json))
    class Detector:
        resets = 0
        def process(self, frame): return True, 1.0
        def reset(self): self.resets += 1
    channel.detector = Detector()
    channel.armed = True
    channel.calibrating = calibrating
    monkeypatch.setattr(server, "credentials", Vault)
    await channel.frame(bytes(2560))
    assert channel.detector.resets == 1 and channel.live is None


def test_ha_settings_rejected_during_conversation(client, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(server, 'active_channel', SimpleNamespace(live=object()))
    response = client.post('/api/settings', json={'ha_token': 'replacement'}, headers=door_headers(client))
    assert response.status_code == 409
    assert server.credentials().get('ha_token') is None


def test_ha_settings_validation_race_does_not_replace_credentials(client, monkeypatch):
    from types import SimpleNamespace
    class HA:
        def __init__(self, *args): pass
        async def catalog(self):
            monkeypatch.setattr(server, 'active_channel', SimpleNamespace(live=object()))
            return [{'entity_id': 'light.test'}]
    monkeypatch.setattr(server, 'HomeAssistant', HA)
    response = client.post('/api/settings', json={'ha_token': 'replacement'}, headers=door_headers(client))
    assert response.status_code == 409
    assert server.credentials().get('ha_token') is None


@pytest.mark.asyncio
async def test_voice_session_reuses_and_closes_ha_even_on_failure(monkeypatch):
    from types import SimpleNamespace
    class HA:
        keep_alive = False
        closed = False
        async def voice_catalog(self): return []
        async def aclose(self): self.closed = True
    ha = HA()
    vault = Vault(); vault.data['openai_api_key'] = 'test'
    monkeypatch.setattr(server, 'make_ha', lambda: ha)
    monkeypatch.setattr(server, 'credentials', lambda: vault)
    async def run(session, *, prepare):
        await prepare()
        raise RuntimeError('simulated failure')
    monkeypatch.setattr(server.LiveSession, 'run', run)
    async def send_json(message): pass
    channel = server.VoiceChannel(SimpleNamespace(send_json=send_json))
    await channel.activate()
    with pytest.raises(RuntimeError, match='simulated failure'):
        await channel.live_task
    assert ha.keep_alive and ha.closed and channel.live is None


@pytest.mark.asyncio
async def test_dns_preparation_is_local_and_cancelled_on_disarm(monkeypatch):
    from types import SimpleNamespace
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def prepare(url):
        entered.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    monkeypatch.setattr(server, 'prepare_address', prepare)
    monkeypatch.setattr(server, 'WakeDetector', lambda threshold: object())
    async def send_json(message): pass
    channel = server.VoiceChannel(SimpleNamespace(send_json=send_json))
    await channel.command({'type': 'arm'})
    await asyncio.wait_for(entered.wait(), 1)
    assert channel.live is None  # DNS preparation never opens a paid session.
    await channel.command({'type': 'disarm'})
    assert cancelled.is_set() and channel.address_task is None
