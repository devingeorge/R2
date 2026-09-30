import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from r2.home_assistant import HomeAssistant, HAError, build_catalog, resolve_target
from r2.live import ToolExecutor


@pytest.fixture
def catalog():
    return [
        {"entity_id": "light.devin", "name": "Devin Light", "device_name": "Devin Light", "room": "Bedroom", "area_id": "bedroom", "state": "on", "brightness_pct": 80},
        {"entity_id": "light.leah", "name": "Leah’s Light", "device_name": "Leah’s Light", "room": "Bedroom", "area_id": "bedroom", "state": "off", "brightness_pct": 50},
        {"entity_id": "light.laundry", "name": "laundry", "device_name": "laundry", "room": "Laundry Room", "area_id": "laundry_room", "state": "unavailable", "brightness_pct": None},
    ]


def test_room_and_individual_resolution(catalog):
    assert [x["entity_id"] for x in resolve_target(catalog, "bedroom lights")] == ["light.devin", "light.leah"]
    assert resolve_target(catalog, "Leah's Light")[0]["entity_id"] == "light.leah"
    with pytest.raises(HAError):
        resolve_target(catalog, "lamp")
    duplicate = catalog + [catalog[0] | {"entity_id": "light.other"}]
    with pytest.raises(HAError, match="ambiguous"):
        resolve_target(duplicate, "Devin Light")


def test_registry_uses_entity_area_then_device_area_and_exact_allowlist():
    entities = [
        {"entity_id": "light.good", "device_id": "d1", "platform": "tplink", "area_id": "living"},
        {"entity_id": "light.other", "device_id": "d2", "platform": "tplink"},
        {"entity_id": "switch.siren", "device_id": "d1", "platform": "tplink"},
        {"entity_id": "light.diag", "device_id": "d1", "platform": "tplink", "entity_category": "diagnostic"},
    ]
    devices = [{"id": "d1", "name": "Devin Light", "area_id": "bedroom"}, {"id": "d2", "name": "Other Lamp"}]
    areas = [{"area_id": "bedroom", "name": "Bedroom"}, {"area_id": "living", "name": "Living Room"}]
    result = build_catalog(entities, devices, areas, [], None)
    assert len(result) == 1 and result[0]["room"] == "Living Room"
    assert build_catalog(entities, devices, areas, [], []) == []


class FakeConnection:
    def __init__(self, states):
        self.states = states
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
            return self.states
        return {"context": {"id": "test"}}


@pytest.mark.asyncio
async def test_set_lights_targets_only_bedroom_and_verifies(catalog):
    ha = HomeAssistant("http://homeassistant.local:8123", "test", [x["entity_id"] for x in catalog])
    async def get_catalog(): return catalog
    ha.catalog = get_catalog
    fake = FakeConnection([{"entity_id": x["entity_id"], "state": "on", "attributes": {"brightness": 77}} for x in catalog[:2]])
    ha.connect = lambda: fake
    result = await ha.set_lights("Bedroom", brightness_pct=30)
    assert result["status"] == "confirmed"
    call = fake.calls[0]
    assert call[0] == "call_service" and call[1]["service"] == "turn_on"
    assert call[1]["target"]["entity_id"] == ["light.devin", "light.leah"]
    assert call[1]["service_data"] == {"brightness_pct": 30}


@pytest.mark.asyncio
async def test_unavailable_never_claims_success(catalog):
    ha = HomeAssistant("http://homeassistant.local:8123", "test", [])
    async def get_catalog(): return catalog
    ha.catalog = get_catalog
    result = await ha.set_lights("Laundry Room", power="on")
    assert result["status"] == "failed" and result["failed"]
    for bad in [-1, 101, True, "30"]:
        with pytest.raises(HAError):
            await ha.set_lights("Bedroom", brightness_pct=bad)


@pytest.mark.asyncio
async def test_duplicate_call_executes_once_and_closing_prevents_new_calls():
    class HA:
        count = 0
        async def set_lights(self, **args):
            self.count += 1
            await asyncio.sleep(.01)
            return {"status": "confirmed"}
    ha = HA()
    executor = ToolExecutor(ha)
    item = {"name": "set_lights", "call_id": "one", "arguments": json.dumps({"target": "Bedroom", "power": "on"})}
    results = await asyncio.gather(executor.execute(item), executor.execute(item))
    assert ha.count == 1 and results[0] == results[1]
    executor.closed = True
    assert (await executor.execute(item | {"call_id": "two"}))["status"] == "error"
    assert ha.count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("attributes", [{"brightness": None}, {}])
@pytest.mark.parametrize("power", ["off", "on"])
async def test_power_only_verification_does_not_require_brightness(catalog, attributes, power):
    ha = HomeAssistant("http://localhost", "test", [x["entity_id"] for x in catalog])
    async def get_catalog(): return catalog
    ha.catalog = get_catalog
    fake = FakeConnection([{"entity_id": x["entity_id"], "state": power, "attributes": attributes} for x in catalog[:2]])
    ha.connect = lambda: fake
    executor = ToolExecutor(ha)
    item = {"name": "set_lights", "call_id": "power-only", "arguments": json.dumps({"target": "Bedroom", "power": power, "brightness_pct": None})}
    result = await executor.execute(item)
    assert result["status"] == "confirmed"
    assert result["confirmed"] == [x["name"] for x in catalog[:2]]
    assert result["failed"] == []
    assert await executor.execute(item) == result
    assert len([call for call in fake.calls if call[0] == "call_service"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("attributes", [{"brightness": None}, {}, {"brightness": "unknown"}])
async def test_requested_brightness_requires_numeric_readback(catalog, attributes, monkeypatch):
    from types import SimpleNamespace
    ha = HomeAssistant("http://localhost", "test", [x["entity_id"] for x in catalog])
    async def get_catalog(): return catalog
    ha.catalog = get_catalog
    fake = FakeConnection([{"entity_id": x["entity_id"], "state": "on", "attributes": attributes} for x in catalog[:2]])
    ha.connect = lambda: fake
    clock = iter([0, 9])
    monkeypatch.setattr("r2.home_assistant.time", SimpleNamespace(monotonic=lambda: next(clock)))
    result = await ha.set_lights("Bedroom", brightness_pct=1)
    assert result["status"] == "failed"
    assert result["confirmed"] == [] and len(result["failed"]) == 2
    assert len([call for call in fake.calls if call[0] == "call_service"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TypeError, ValueError, KeyError])
async def test_execution_error_is_unconfirmed_not_invalid_arguments(error):
    class HA:
        calls = 0
        async def set_lights(self, **args):
            self.calls += 1
            raise error("simulated verification failure after action")
    ha = HA()
    executor = ToolExecutor(ha)
    item = {"name": "set_lights", "call_id": "failed", "arguments": '{"target":"Living Room","power":"off","brightness_pct":null}'}
    result = await executor.execute(item)
    assert result["status"] == "error"
    assert "unconfirmed" in result["message"] and "retry automatically" in result["message"]
    assert "Invalid tool arguments" not in result["message"]
    assert await executor.execute(item) == result
    assert ha.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['not json', '[]', '{"target":"Living Room","extra":true}', '{"power":"off"}'])
async def test_invalid_arguments_rejected_before_dispatch(arguments):
    class HA:
        async def set_lights(self, **args): raise AssertionError("Invalid call dispatched")
    result = await ToolExecutor(HA()).execute({"name": "set_lights", "call_id": "invalid", "arguments": arguments})
    assert result["message"] == "Invalid tool arguments; clarify the request."
