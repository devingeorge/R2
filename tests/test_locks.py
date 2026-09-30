import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from r2.home_assistant import HAError, HomeAssistant, build_lock_catalog, resolve_lock
from r2.live import ToolExecutor, session_config


@pytest.fixture
def locks():
    return [
        {"entity_id": "lock.front", "name": "Front door", "device_name": "Aqara U100", "room": "Unassigned", "area_id": None, "state": "locked", "code_required": False},
        {"entity_id": "lock.back", "name": "Back door", "device_name": "Aqara U100", "room": "Unassigned", "area_id": None, "state": "locked", "code_required": False},
    ]


def test_discovery_is_separate_from_permission_and_preserves_named_mapping():
    entities = [
        {"entity_id": "lock.front", "device_id": "d1"},
        {"entity_id": "lock.back", "device_id": "d2"},
        {"entity_id": "light.front", "device_id": "d1"},
        {"entity_id": "lock.disabled", "disabled_by": "user"},
        {"entity_id": "lock.diagnostic", "entity_category": "diagnostic"},
    ]
    devices = [{"id": "d1", "name": "Aqara U100", "serial_number": "abcd132f2", "area_id": "entry"}]
    args = (entities, devices, [{"area_id": "entry", "name": "Entryway"}], [])
    assert build_lock_catalog(*args, []) == []
    assert len(build_lock_catalog(*args, None)) == 2
    front = build_lock_catalog(*args, ["lock.front"], {"lock.front": "Front door"})[0]
    assert front["name"] == "Front door" and front["room"] == "Entryway"
    assert front["serial_suffix"] == "d132f2" and front["state"] == "unavailable"
    assert HomeAssistant("http://localhost", "token", []).allowed_locks == []


def test_exact_door_names_and_ambiguous_names(locks):
    assert resolve_lock(locks, "front door")["entity_id"] == "lock.front"
    assert resolve_lock(locks, "back door lock")["entity_id"] == "lock.back"
    assert resolve_lock(locks, "lock.back")["entity_id"] == "lock.back"
    assert resolve_lock(locks[:1], "the door")["entity_id"] == "lock.front"
    for target in ("the door", "Aqara U100", "all", "all doors", "garage", "lock.other", None, []):
        with pytest.raises(HAError):
            resolve_lock(locks, target)
    with pytest.raises(HAError, match="No door locks"):
        resolve_lock([], "front door")


class FakeConnection:
    def __init__(self, states):
        self.states = iter(states)
        self.calls = []

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    @asynccontextmanager
    async def watch_states(self, entity_ids):
        async def ready(timeout): pass
        yield SimpleNamespace(clear=lambda: None, wait=ready)

    async def request(self, kind, **kwargs):
        self.calls.append((kind, kwargs))
        if kind == "get_states":
            state = next(self.states)
            if isinstance(state, Exception):
                raise state
            return [{"entity_id": "lock.front", "state": state}, {"entity_id": "lock.back", "state": "locked"}]
        return {}


def make_ha(locks, states):
    ha = HomeAssistant("http://localhost", "token", [], ["lock.front", "lock.back"])
    async def catalog(): return locks
    ha.lock_catalog = catalog
    fake = FakeConnection(states)
    ha.connect = lambda: fake
    return ha, fake


@pytest.mark.asyncio
@pytest.mark.parametrize("action,initial,transitional,expected", [("unlock", "locked", "unlocking", "unlocked"), ("lock", "unlocked", "locking", "locked")])
async def test_single_service_correct_door_and_verified_state(locks, action, initial, transitional, expected, monkeypatch):
    locks[0]["state"] = initial
    ha, fake = make_ha(locks, [transitional, expected])
    async def no_wait(_): pass
    monkeypatch.setattr("r2.home_assistant.asyncio.sleep", no_wait)
    result = await ha.set_lock("front door", action)
    assert result["status"] == "confirmed" and result["state"] == expected
    assert result["already_in_state"] is False
    services = [call for call in fake.calls if call[0] == "call_service"]
    assert services == [("call_service", {"domain": "lock", "service": action, "service_data": {}, "target": {"entity_id": ["lock.front"]}})]
    assert len(fake.calls) == 3


@pytest.mark.asyncio
async def test_already_in_state_sends_no_command(locks):
    ha, fake = make_ha(locks, [])
    result = await ha.set_lock("front door", "lock")
    assert result["status"] == "confirmed" and result["already_in_state"]
    assert not fake.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["jammed", "unavailable", "unknown", "locking", "unlocking"])
async def test_bad_or_moving_state_sends_no_command(locks, state):
    locks[0]["state"] = state
    ha, fake = make_ha(locks, [])
    with pytest.raises(HAError, match="No command was sent"):
        await ha.set_lock("front door", "unlock")
    assert not fake.calls


@pytest.mark.asyncio
async def test_code_dependent_locks_and_invalid_actions_sends_no_command(locks):
    locks[0]["code_required"] = True
    ha, fake = make_ha(locks, [])
    with pytest.raises(HAError, match="requires a code"):
        await ha.set_lock("front door", "unlock")
    for action in ("open", "toggle", True, {}, None):
        with pytest.raises(HAError):
            await ha.set_lock("front door", action)
    assert not fake.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("actual", ["locked", "jammed", "unknown", "unavailable"])
async def test_unconfirmed_never_claims_success_or_replays(locks, actual, monkeypatch):
    ha, fake = make_ha(locks, [actual])
    clock = iter([0, 11])
    monkeypatch.setattr("r2.home_assistant.time", SimpleNamespace(monotonic=lambda: next(clock)))
    result = await ha.set_lock("front door", "unlock")
    assert result["status"] == "unconfirmed" and "confirmed" not in result
    assert "Do not retry automatically" in result["message"]
    assert len([call for call in fake.calls if call[0] == "call_service"]) == 1


@pytest.mark.asyncio
async def test_tool_disconnect_is_not_replayed_and_duplicate_calls_cached(locks):
    ha, fake = make_ha(locks, [ConnectionError("disconnected")])
    executor = ToolExecutor(ha)
    item = {"call_id": "unlock1", "name": "set_lock", "arguments": json.dumps({"target": "Front door", "action": "unlock"})}
    results = await asyncio.gather(executor.execute(item), executor.execute(item))
    assert results[0] == results[1]
    assert results[0]["status"] == "error" and "unconfirmed" in results[0]["message"]
    assert len([call for call in fake.calls if call[0] == "call_service"]) == 1
    executor.closed = True
    assert (await executor.execute(item | {"call_id": "unlock2"}))["status"] == "error"
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_unpermitted_ambiguous_or_extra_arguments_do_not_call_service(locks):
    ha, fake = make_ha(locks, [])
    executor = ToolExecutor(ha)
    for index, args in enumerate([
        {"target": "the door", "action": "unlock"},
        {"target": "lock.other", "action": "unlock"},
        {"target": "front door", "action": "unlock", "code": "1234"},
        {"target": "front door", "action": "open"},
    ]):
        result = await executor.execute({"call_id": str(index), "name": "set_lock", "arguments": json.dumps(args)})
        assert result["status"] == "error"
    assert not fake.calls


@pytest.mark.asyncio
async def test_status_is_read_only_and_voice_catalog_contains_both_types(locks):
    ha, fake = make_ha(locks, [])
    executor = ToolExecutor(ha)
    result = await executor.execute({"call_id": "status", "name": "get_locks", "arguments": '{"target":"all"}'})
    assert len(result["locks"]) == 2 and not fake.calls
    config = session_config(locks)
    assert "set_lock" in config["instructions"]
    assert "Front door" in config["delegation"]["responses"]["instructions"]
    assert "startup snapshot" in config["delegation"]["responses"]["instructions"]


@pytest.mark.asyncio
async def test_catalog_permissions_enforced_against_registry():
    ha = HomeAssistant("http://localhost", "token", ["light.good"], ["lock.front"], {"lock.front": "Front door"})
    async def registry():
        return ([{"entity_id": "light.good"}, {"entity_id": "lock.front"}, {"entity_id": "lock.other"}], [], [], [])
    ha.registry = registry
    assert {x["entity_id"] for x in await ha.voice_catalog()} == {"light.good", "lock.front"}
    assert len(await ha.lock_catalog(discover=True)) == 2
    with pytest.raises(HAError):
        await ha.set_lock("lock.other", "unlock")
