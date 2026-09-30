from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from contextlib import asynccontextmanager

from r2.ha_transport import HAConnection, HAError

SEED_DEVICES = {"devin light", "leahs light", "fireplace lamp", "fireplace 2", "living room lamp", "living room light", "laundry"}


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", unicodedata.normalize("NFKD", value).lower())).strip()


def build_catalog(entities: list, devices: list, areas: list, states: list, allowed: list[str] | None) -> list[dict]:
    device_map = {d["id"]: d for d in devices}
    area_map = {a["area_id"]: a["name"] for a in areas}
    state_map = {s["entity_id"]: s for s in states}
    result = []
    for entity in entities:
        entity_id = entity["entity_id"]
        if not entity_id.startswith("light.") or entity.get("disabled_by") or entity.get("entity_category"):
            continue
        device = device_map.get(entity.get("device_id"), {})
        device_name = device.get("name_by_user") or device.get("name") or ""
        # Bootstrap the seven known devices once, then persist actual entity IDs.
        if allowed is None:
            if entity.get("platform") != "tplink" or normalize(device_name) not in SEED_DEVICES:
                continue
        elif entity_id not in allowed:
            continue
        state = state_map.get(entity_id, {})
        area_id = entity.get("area_id") or device.get("area_id")
        attrs = state.get("attributes", {})
        result.append({
            "entity_id": entity_id,
            "name": entity.get("name") or attrs.get("friendly_name") or device_name or entity_id,
            "device_name": device_name,
            "area_id": area_id,
            "room": area_map.get(area_id, "Unassigned"),
            "state": state.get("state", "unavailable"),
            "brightness_pct": round(attrs["brightness"] / 255 * 100) if attrs.get("brightness") is not None else None,
        })
    return sorted(result, key=lambda item: (item["room"], item["name"]))


def resolve_target(catalog: list[dict], target: str) -> list[dict]:
    if not isinstance(target, str) or not target.strip():
        raise HAError("Specify a room or light name.")
    name = normalize(target)
    if name in {"all", "all lights", "house lights", "every light"}:
        return catalog
    matches = []
    for item in catalog:
        aliases = {normalize(item["entity_id"]), normalize(item["name"]), normalize(item["device_name"])}
        if name in aliases:
            matches.append(item)
    if len(matches) == 1:
        return matches
    if len(matches) > 1:
        raise HAError("That light name is ambiguous. Ask for the room or exact light name.")
    rooms = {item["room"] for item in catalog if name in {normalize(item["room"]), normalize(item["room"] + " lights"), normalize(item["area_id"] or "")}}
    if len(rooms) == 1:
        return [item for item in catalog if item["room"] in rooms]
    raise HAError("Unknown or ambiguous target. Available rooms: " + ", ".join(sorted({item["room"] for item in catalog})))


def build_lock_catalog(entities, devices, areas, states, allowed, names=None):
    device_map = {d["id"]: d for d in devices}
    area_map = {a["area_id"]: a["name"] for a in areas}
    state_map = {s["entity_id"]: s for s in states}
    result = []
    for entity in entities:
        entity_id = entity["entity_id"]
        if not entity_id.startswith("lock.") or entity.get("disabled_by") or entity.get("entity_category"):
            continue
        if allowed is not None and entity_id not in allowed:
            continue
        device = device_map.get(entity.get("device_id"), {})
        if device.get("disabled_by"):
            continue
        state = state_map.get(entity_id, {})
        attrs = state.get("attributes", {})
        device_name = device.get("name_by_user") or device.get("name") or ""
        area_id = entity.get("area_id") or device.get("area_id")
        result.append({
            "entity_id": entity_id,
            "name": (names or {}).get(entity_id) or entity.get("name") or attrs.get("friendly_name") or device_name or entity_id,
            "device_name": device_name,
            "room": area_map.get(area_id, "Unassigned"),
            "area_id": area_id,
            "state": state.get("state", "unavailable"),
            "code_required": bool(attrs.get("code_format")),
            "serial_suffix": (device.get("serial_number") or "")[-6:],
        })
    return sorted(result, key=lambda item: (item["name"], item["entity_id"]))


def resolve_lock(catalog: list[dict], target: str) -> dict:
    if not isinstance(target, str) or not target.strip():
        raise HAError("Specify a door or lock name.")
    if not catalog:
        raise HAError("No door locks are enabled for R2. Choose locks in Settings.")
    # An exact entity ID always identifies one lock, even when friendly names collide.
    exact = [item for item in catalog if target.strip() == item["entity_id"]]
    if len(exact) == 1:
        return exact[0]
    name = normalize(target)
    matches = []
    for item in catalog:
        aliases = {normalize(item["name"]), normalize(item["device_name"])}
        aliases |= {alias + " lock" for alias in list(aliases) if alias}
        if item["room"] != "Unassigned":
            aliases |= {normalize(item["room"]), normalize(item["room"] + " door"), normalize(item["room"] + " lock")}
        if name in aliases:
            matches.append(item)
    if name in {"door", "the door", "lock", "the lock"}:
        matches = catalog
    if len(matches) == 1:
        return matches[0]
    choices = ", ".join(f'{item["name"]} ({item["entity_id"]})' for item in catalog)
    raise HAError("Unknown or ambiguous door. Ask which specific lock; do not guess. Available locks: " + choices)


class HomeAssistant:
    def __init__(self, url: str, token: str, allowed: list[str] | None, allowed_locks=None, lock_names=None):
        self.url, self.token, self.allowed = url, token, allowed
        self.allowed_locks = list(allowed_locks or [])
        self.lock_names = dict(lock_names or {})
        self.lock = asyncio.Lock()
        self.keep_alive = False
        self.connection_lock = asyncio.Lock()
        self.connection = None
        self.closed = False

    @asynccontextmanager
    async def connect(self):
        if self.closed:
            raise HAError("Home Assistant conversation ended.")
        if not self.keep_alive:
            async with HAConnection(self.url, self.token) as connection:
                yield connection
            return
        async with self.connection_lock:
            if self.closed:
                raise HAError("Home Assistant conversation ended.")
            if self.connection is None or not self.connection.connected:
                if self.connection:
                    await self.connection.aclose()
                connection = HAConnection(self.url, self.token, cache_metadata=True)
                await connection.__aenter__()
                self.connection = connection
            connection = self.connection
        # Reconnect only before a new operation. Never retry a yielded command.
        yield connection

    async def aclose(self):
        self.closed = True
        async with self.connection_lock:
            if self.connection:
                await self.connection.aclose()
                self.connection = None

    async def registry(self):
        async with self.connect() as ha:
            return await ha.registry()

    async def catalog(self) -> list[dict]:
        return build_catalog(*await self.registry(), self.allowed)

    async def lock_catalog(self, discover=False) -> list[dict]:
        return build_lock_catalog(*await self.registry(), None if discover else self.allowed_locks, self.lock_names)

    async def voice_catalog(self) -> list[dict]:
        registry = await self.registry()
        return build_catalog(*registry, self.allowed) + build_lock_catalog(*registry, self.allowed_locks, self.lock_names)

    async def get_locks(self, target="all"):
        catalog = await self.lock_catalog()
        if not isinstance(target, str):
            raise HAError("Specify a door name or all.")
        locks = catalog if normalize(target) in {"all", "all locks", "all doors"} else [resolve_lock(catalog, target)]
        return {"status": "ok", "locks": locks}

    async def set_lock(self, target: str, action: str):
        if action not in ("lock", "unlock"):
            raise HAError("Lock action must be lock or unlock.")
        expected = "locked" if action == "lock" else "unlocked"
        async with self.lock:
            item = resolve_lock(await self.lock_catalog(), target)
            if item["state"] == expected:
                return {"status": "confirmed", "confirmed": [item["name"]], "entity_id": item["entity_id"], "state": expected, "already_in_state": True}
            if item["state"] not in {"locked", "unlocked", "open"}:
                raise HAError(f'{item["name"]} reports {item["state"]}. No command was sent. Check the lock before trying again.')
            if item["code_required"]:
                raise HAError("This lock requires a code. Configure its code in Home Assistant before using R2; do not speak the code aloud.")
            async with self.connect() as ha, ha.watch_states([item["entity_id"]]) as changes:
                await ha.request("call_service", domain="lock", service=action, service_data={}, target={"entity_id": [item["entity_id"]]})
                deadline = time.monotonic() + 10
                while True:
                    changes.clear()
                    states = {s["entity_id"]: s for s in await ha.request("get_states")}
                    actual = states.get(item["entity_id"], {}).get("state", "unavailable")
                    if actual == expected:
                        return {"status": "confirmed", "confirmed": [item["name"]], "entity_id": item["entity_id"], "state": actual, "already_in_state": False}
                    remaining = deadline - time.monotonic()
                    if actual in {"jammed", "unavailable", "unknown"} or remaining <= 0:
                        return {"status": "unconfirmed", "entity_id": item["entity_id"], "state": actual, "message": f'{item["name"]}: {expected} was not confirmed; Home Assistant reports {actual}. Check the door before retrying. Do not retry automatically.'}
                    await changes.wait(remaining)

    async def get_lights(self, target="all"):
        return {"status": "ok", "lights": resolve_target(await self.catalog(), target)}

    async def set_lights(self, target: str, power: str | None = None, brightness_pct: float | None = None):
        if power not in {None, "on", "off"}:
            raise HAError("Power must be on or off.")
        if brightness_pct is not None and (type(brightness_pct) not in {int, float} or not 0 <= brightness_pct <= 100):
            raise HAError("Brightness must be between 0 and 100.")
        if power is None and brightness_pct is None:
            raise HAError("Specify power or brightness.")
        if power == "off" and brightness_pct not in {None, 0}:
            raise HAError("Cannot turn a light off and set a nonzero brightness simultaneously.")
        expected = "off" if power == "off" or brightness_pct == 0 else "on"
        async with self.lock:
            targets = resolve_target(await self.catalog(), target)
            eligible = [x for x in targets if x["state"] not in {"unavailable", "unknown"}]
            confirmed, failed = [], [{"name": x["name"], "reason": "unavailable"} for x in targets if x not in eligible]
            if eligible:
                async with self.connect() as ha, ha.watch_states([x["entity_id"] for x in eligible]) as changes:
                    service_data = {}
                    if expected == "on" and brightness_pct is not None:
                        service_data["brightness_pct"] = brightness_pct
                    await ha.request("call_service", domain="light", service="turn_" + expected, service_data=service_data, target={"entity_id": [x["entity_id"] for x in eligible]})
                    deadline = time.monotonic() + 8
                    pending = {x["entity_id"]: x for x in eligible}
                    while pending:
                        changes.clear()
                        states = {s["entity_id"]: s for s in await ha.request("get_states")}
                        for entity_id, item in list(pending.items()):
                            state = states.get(entity_id, {})
                            # Off lights commonly report brightness=null. Power-only
                            # actions need state verification, not a brightness value.
                            brightness_matches = True
                            if expected == "on" and brightness_pct is not None:
                                brightness = state.get("attributes", {}).get("brightness")
                                brightness_matches = type(brightness) in {int, float} and abs(brightness / 255 * 100 - brightness_pct) <= 2
                            if state.get("state") == expected and brightness_matches:
                                confirmed.append(item["name"])
                                pending.pop(entity_id)
                        if not pending:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        await changes.wait(remaining)
                    failed.extend({"name": x["name"], "reason": "state not confirmed"} for x in pending.values())
            return {"status": "confirmed" if confirmed and not failed else "partial" if confirmed else "failed", "confirmed": confirmed, "failed": failed, "power": expected, "brightness_pct": brightness_pct}
