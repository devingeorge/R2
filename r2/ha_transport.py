"""Home Assistant transport: bounded DNS reuse, one reader, no command replay."""
from __future__ import annotations

import asyncio
import json
import socket
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import websockets
from websockets.exceptions import InvalidHandshake


class HAError(Exception):
    pass


class AddressCache:
    def __init__(self, ttl=300, capacity=8):
        self.ttl, self.capacity = ttl, capacity
        self.entries = OrderedDict()

    async def resolve(self, host, port):
        key = (host, port)
        cached = self.entries.get(key)
        if cached and cached[0] > time.monotonic():
            self.entries.move_to_end(key)
            return cached[1], True
        self.entries.pop(key, None)
        rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = []
        for family, _, _, _, address in rows:
            ip = address[0]
            if family == socket.AF_INET6 and address[3] and '%' not in ip:
                ip += '%' + str(address[3])
            if (ip, family) not in addresses:
                addresses.append((ip, family))
        if not addresses:
            raise OSError("Home Assistant hostname has no addresses")
        self.entries[key] = (time.monotonic() + self.ttl, addresses)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
        return addresses, False

    def discard(self, host, port):
        self.entries.pop((host, port), None)


ADDRESSES = AddressCache()


async def prepare_address(url):
    parsed = urlparse(url)
    async with asyncio.timeout(8):
        await ADDRESSES.resolve(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))


async def open_socket(url):
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == 'wss' else 80)
    async with asyncio.timeout(8):
        addresses, cached = await ADDRESSES.resolve(parsed.hostname, port)
        while True:
            for address, family in addresses:
                try:
                    # Override only the TCP destination. Preserve URL/Host/TLS SNI
                    # and certificate verification for the configured hostname.
                    return await websockets.connect(url, host=address, port=port, family=family,
                                                    open_timeout=3, close_timeout=2,
                                                    max_size=8_000_000, proxy=None)
                except (OSError, TimeoutError, InvalidHandshake):
                    ADDRESSES.discard(parsed.hostname, port)
                    continue
            ADDRESSES.discard(parsed.hostname, port)
            if not cached:
                raise ConnectionError("Could not connect to Home Assistant")
            # A moved device may have invalidated the cached address. Only retry
            # connecting, before authentication or any application command.
            addresses, cached = await ADDRESSES.resolve(parsed.hostname, port)
            cached = False


class StateWatch:
    def __init__(self, connection, entity_ids, enabled):
        self.connection = connection
        self.entity_ids = set(entity_ids)
        self.enabled = enabled
        self.changed = asyncio.Event()

    def clear(self):
        self.changed.clear()

    async def wait(self, timeout):
        if not self.connection.connected:
            raise ConnectionError("Home Assistant disconnected during verification")
        if not self.enabled:
            await asyncio.sleep(min(.35, max(0, timeout)))
            return
        try:
            await asyncio.wait_for(self.changed.wait(), max(0, timeout))
        except asyncio.TimeoutError:
            pass  # Caller performs a final fresh read at the deadline.
        if not self.connection.connected:
            raise ConnectionError("Home Assistant disconnected during verification")


class HAConnection:
    def __init__(self, url, token, *, cache_metadata=False):
        self.url = url.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1) + '/api/websocket'
        self.token = token
        self.sequence = 0
        self.ws = None
        self.reader = None
        self.pending = {}
        self.subscriptions = {}
        self.subscription_lock = asyncio.Lock()
        self.watches = set()
        self.cache_metadata = cache_metadata
        self.metadata_lock = asyncio.Lock()
        self.metadata = None
        self.metadata_until = 0
        self.metadata_revision = 0

    @property
    def connected(self):
        return self.reader is not None and not self.reader.done()

    async def __aenter__(self):
        self.ws = await open_socket(self.url)
        try:
            await asyncio.wait_for(self.ws.recv(), 8)
            await self.ws.send(json.dumps({'type': 'auth', 'access_token': self.token}))
            if json.loads(await asyncio.wait_for(self.ws.recv(), 8)).get('type') != 'auth_ok':
                raise HAError("Home Assistant rejected the access token.")
            self.reader = asyncio.create_task(self._receive())
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, *args):
        await self.aclose()

    async def aclose(self):
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.ws:
            await self.ws.close()
        self.metadata = None

    async def _receive(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if message.get('type') == 'result':
                    future = self.pending.get(message.get('id'))
                    if future is not None and not future.done():
                        future.set_result(message)
                elif message.get('type') == 'event':
                    event = message.get('event', {})
                    kind = event.get('event_type')
                    if kind in {'entity_registry_updated', 'device_registry_updated', 'area_registry_updated'}:
                        self.metadata_revision += 1
                        self.metadata = None
                    elif kind == 'state_changed':
                        entity_id = event.get('data', {}).get('entity_id')
                        for watch in self.watches:
                            if entity_id in watch.entity_ids:
                                watch.changed.set()
        except Exception:
            # Do not log raw frames, tokens or server exception bodies.
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Home Assistant disconnected"))
            for watch in self.watches:
                watch.changed.set()
            self.metadata = None

    async def request(self, command, **data):
        if not self.connected:
            raise ConnectionError("Home Assistant is not connected")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.ws.send(json.dumps({'id': request_id, 'type': command, **data}))
            message = await asyncio.wait_for(future, 12)
            if not message.get('success'):
                raise HAError("Home Assistant could not complete " + command)
            return message.get('result')
        except (TimeoutError, asyncio.CancelledError):
            # A timed-out action may have happened. Retire this connection, never
            # re-send the request; a later independent request may reconnect.
            await self.aclose()
            raise
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Retrieve disconnect errors if send itself failed.

    async def subscribe(self, event_type):
        async with self.subscription_lock:
            if event_type not in self.subscriptions:
                try:
                    await self.request('subscribe_events', event_type=event_type)
                    self.subscriptions[event_type] = True
                except HAError:
                    self.subscriptions[event_type] = False
            return self.subscriptions[event_type]

    @asynccontextmanager
    async def watch_states(self, entity_ids):
        enabled = await self.subscribe('state_changed')
        watch = StateWatch(self, entity_ids, enabled)
        self.watches.add(watch)
        try:
            yield watch
        finally:
            self.watches.discard(watch)

    async def registry(self):
        async with self.metadata_lock:
            reusable = False
            if self.cache_metadata:
                subscriptions = [await self.subscribe(kind) for kind in
                                 ('entity_registry_updated', 'device_registry_updated', 'area_registry_updated')]
                reusable = all(subscriptions)
            if not reusable or self.metadata is None or time.monotonic() >= self.metadata_until:
                # One reader routes independent replies by id, so these can overlap.
                for attempt in range(2):
                    revision = self.metadata_revision
                    metadata = await asyncio.gather(*(self.request(command) for command in
                        ('config/entity_registry/list', 'config/device_registry/list', 'config/area_registry/list')))
                    if revision == self.metadata_revision:
                        break
                else:
                    raise HAError("Device configuration changed. Please try again.")
                if reusable:
                    self.metadata = metadata
                    self.metadata_until = time.monotonic() + 60
            else:
                metadata = self.metadata
            # Never reuse state snapshots for a new status check or action.
            revision = self.metadata_revision
            states = await self.request('get_states')
            if revision != self.metadata_revision:
                raise HAError("Device configuration changed. Please try again.")
            return (*metadata, states)
