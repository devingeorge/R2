import asyncio
import json
import socket
import time
from collections import Counter

import pytest

from r2 import ha_transport as transport
from r2.home_assistant import HomeAssistant
from r2.live import ToolExecutor


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({'type': 'auth_required'}))
        self.calls = []
        self.closed = False
        self.entities = [{'entity_id': 'light.test', 'name': 'Test light', 'area_id': 'living'}]
        self.states = [{'entity_id': 'light.test', 'state': 'on', 'attributes': {'brightness': 100}}]
        self.on_service = None
        self.on_states = None
        self.deny_events = False
        self.auto_reply = True

    async def recv(self): return await self.incoming.get()
    def __aiter__(self): return self
    async def __anext__(self):
        item = await self.incoming.get()
        if item is None: raise StopAsyncIteration
        return item
    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)
    def reply(self, request_id, result=None, success=True):
        self.incoming.put_nowait(json.dumps({'type': 'result', 'id': request_id, 'success': success, 'result': result}))
    def event(self, kind, **data):
        self.incoming.put_nowait(json.dumps({'type': 'event', 'event': {'event_type': kind, 'data': data}}))
    async def send(self, raw):
        message = json.loads(raw)
        self.calls.append(message)
        kind = message['type']
        if kind == 'auth':
            self.incoming.put_nowait(json.dumps({'type': 'auth_ok'}))
            return
        if not self.auto_reply: return
        if kind == 'subscribe_events' and self.deny_events:
            self.reply(message['id'], success=False)
            return
        result = None
        if kind == 'config/entity_registry/list': result = self.entities
        elif kind == 'config/device_registry/list': result = []
        elif kind == 'config/area_registry/list': result = [{'area_id': 'living', 'name': 'Living Room'}]
        elif kind == 'get_states':
            result = self.states
            if self.on_states: await self.on_states(message)
        elif kind == 'call_service' and self.on_service:
            await self.on_service(message)
            if self.closed: return
        self.reply(message['id'], result)


def install_sockets(monkeypatch, *sockets):
    opened = []
    async def connect(url):
        ws = sockets[len(opened)]
        opened.append(ws)
        return ws
    monkeypatch.setattr(transport, 'open_socket', connect)
    return opened


def persistent_ha():
    ha = HomeAssistant('http://test.local', 'test-token', ['light.test'])
    ha.keep_alive = True
    return ha


@pytest.mark.asyncio
async def test_one_socket_metadata_reuse_and_fresh_states(monkeypatch):
    ws = FakeSocket()
    opened = install_sockets(monkeypatch, ws)
    ha = persistent_ha()
    try:
        assert (await ha.get_lights())['lights'][0]['state'] == 'on'
        ws.states[0]['state'] = 'off'
        assert (await ha.get_lights())['lights'][0]['state'] == 'off'
        counts = Counter(x['type'] for x in ws.calls)
        assert len(opened) == 1 and counts['auth'] == 1
        assert counts['config/entity_registry/list'] == 1 and counts['get_states'] == 2
    finally:
        await ha.aclose()
    assert ws.closed and ha.connection is None
    with pytest.raises(transport.HAError, match='ended'):
        await ha.get_lights()


@pytest.mark.asyncio
async def test_metadata_change_and_expiry_invalidate_permissions(monkeypatch):
    ws = FakeSocket()
    install_sockets(monkeypatch, ws)
    ha = persistent_ha()
    try:
        await ha.get_lights()
        ws.entities[0]['disabled_by'] = 'user'
        ws.event('entity_registry_updated', action='update', entity_id='light.test')
        await asyncio.sleep(0)
        assert (await ha.get_lights())['lights'] == []
        ws.entities[0].pop('disabled_by')
        ha.connection.metadata_until = 0
        assert len((await ha.get_lights())['lights']) == 1
        assert sum(x['type'] == 'config/entity_registry/list' for x in ws.calls) == 3
    finally:
        await ha.aclose()


@pytest.mark.asyncio
async def test_registry_change_during_read_rejects_snapshot(monkeypatch):
    ws = FakeSocket()
    install_sockets(monkeypatch, ws)
    ha = persistent_ha()
    async def invalidate(message):
        ws.event('entity_registry_updated', entity_id='light.test')
    ws.on_states = invalidate
    try:
        with pytest.raises(transport.HAError, match='configuration changed'):
            await ha.get_lights()
        assert not any(x['type'] == 'call_service' for x in ws.calls)
    finally:
        await ha.aclose()


@pytest.mark.asyncio
async def test_reply_routing_out_of_order_and_disconnect_pending(monkeypatch):
    ws = FakeSocket()
    install_sockets(monkeypatch, ws)
    async with transport.HAConnection('http://test.local', 'test') as connection:
        ws.auto_reply = False
        first = asyncio.create_task(connection.request('get_states'))
        second = asyncio.create_task(connection.request('config/area_registry/list'))
        await asyncio.sleep(0)
        ws.reply(2, ['areas'])
        ws.event('state_changed', entity_id='unrelated')
        ws.reply(1, ['states'])
        assert await first == ['states'] and await second == ['areas']
        pending = asyncio.create_task(connection.request('get_states'))
        await asyncio.sleep(0)
        await ws.close()
        with pytest.raises(ConnectionError): await pending
    assert not connection.pending


@pytest.mark.asyncio
async def test_event_verification_wakes_before_poll_interval(monkeypatch):
    ws = FakeSocket()
    install_sockets(monkeypatch, ws)
    ha = persistent_ha()
    async def change_later():
        await asyncio.sleep(.02)
        ws.states[0] = {'entity_id': 'light.test', 'state': 'off', 'attributes': {'brightness': None}}
        ws.event('state_changed', entity_id='light.test')
    update = None
    async def service(message):
        nonlocal update
        assert any(x.get('event_type') == 'state_changed' for x in ws.calls)
        update = asyncio.create_task(change_later())
    ws.on_service = service
    try:
        result = await asyncio.wait_for(ha.set_lights('Living Room', power='off'), .3)
        assert result['status'] == 'confirmed'
        counts = Counter(x['type'] for x in ws.calls)
        assert counts['call_service'] == 1 and counts['get_states'] == 3
        assert not ha.connection.watches
    finally:
        if update: await update
        await ha.aclose()


@pytest.mark.asyncio
async def test_event_during_read_is_not_lost_and_unrelated_events_ignored(monkeypatch):
    ws = FakeSocket()
    install_sockets(monkeypatch, ws)
    async with transport.HAConnection('http://test.local', 'test') as connection:
        async with connection.watch_states(['light.test']) as watch:
            watch.clear()
            ws.event('state_changed', entity_id='light.other')
            await asyncio.sleep(0)
            assert not watch.changed.is_set()
            async def during_read(message): ws.event('state_changed', entity_id='light.test')
            ws.on_states = during_read
            await connection.request('get_states')
            await asyncio.wait_for(watch.wait(5), .1)
        assert not connection.watches


@pytest.mark.asyncio
async def test_denied_subscriptions_fall_back_without_metadata_cache(monkeypatch):
    ws = FakeSocket()
    ws.deny_events = True
    install_sockets(monkeypatch, ws)
    ha = persistent_ha()
    try:
        await ha.get_lights(); await ha.get_lights()
        assert sum(x['type'] == 'config/entity_registry/list' for x in ws.calls) == 2
        async with ha.connection.watch_states(['light.test']) as watch:
            assert not watch.enabled
            sleeps = []
            async def sleep(seconds): sleeps.append(seconds)
            monkeypatch.setattr(transport.asyncio, 'sleep', sleep)
            await watch.wait(4)
            assert sleeps == [.35]
    finally:
        await ha.aclose()


@pytest.mark.asyncio
async def test_disconnect_never_replays_action_and_next_request_reconnects(monkeypatch):
    first, second = FakeSocket(), FakeSocket()
    opened = install_sockets(monkeypatch, first, second)
    ha = persistent_ha()
    async def lose_reply(message): await first.close()
    first.on_service = lose_reply
    executor = ToolExecutor(ha)
    item = {'name': 'set_lights', 'call_id': 'one', 'arguments': '{"target":"Living Room","power":"off"}'}
    try:
        result = await executor.execute(item)
        assert 'unconfirmed' in result['message']
        assert await executor.execute(item) == result
        assert len(opened) == 1
        assert sum(x['type'] == 'call_service' for x in first.calls) == 1
        await ha.get_lights()
        assert len(opened) == 2
        assert not any(x['type'] == 'call_service' for x in second.calls)
        assert any(x['type'] == 'config/entity_registry/list' for x in second.calls)
    finally:
        await ha.aclose()
    assert first.closed and second.closed


@pytest.mark.asyncio
async def test_cancel_pending_request_releases_reader_and_socket(monkeypatch):
    ws = FakeSocket()
    install_sockets(monkeypatch, ws)
    async with transport.HAConnection('http://test.local', 'test') as connection:
        ws.auto_reply = False
        request = asyncio.create_task(connection.request('get_states'))
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError): await request
        assert ws.closed and connection.reader.done() and not connection.pending


@pytest.mark.asyncio
async def test_cancel_parallel_registry_read_cleans_all_requests(monkeypatch):
    ws = FakeSocket()
    install_sockets(monkeypatch, ws)
    async with transport.HAConnection('http://test.local', 'test') as connection:
        ws.auto_reply = False
        request = asyncio.create_task(connection.registry())
        async def waiting():
            while len(connection.pending) != 3: await asyncio.sleep(0)
        await asyncio.wait_for(waiting(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError): await request
        assert ws.closed and connection.reader.done() and not connection.pending


@pytest.mark.asyncio
async def test_disconnect_wakes_event_verification(monkeypatch):
    ws = FakeSocket()
    install_sockets(monkeypatch, ws)
    async with transport.HAConnection('http://test.local', 'test') as connection:
        async with connection.watch_states(['light.test']) as watch:
            waiting = asyncio.create_task(watch.wait(10))
            await ws.close()
            with pytest.raises(ConnectionError): await asyncio.wait_for(waiting, .2)


@pytest.mark.asyncio
async def test_auth_failure_closes_without_retry(monkeypatch):
    ws = FakeSocket()
    async def rejected(raw):
        ws.incoming.put_nowait(json.dumps({'type': 'auth_invalid'}))
    ws.send = rejected
    opened = install_sockets(monkeypatch, ws)
    with pytest.raises(transport.HAError, match='access token'):
        async with transport.HAConnection('http://test.local', 'wrong'):
            pytest.fail('Invalid credentials accepted')
    assert len(opened) == 1 and ws.closed


@pytest.mark.asyncio
async def test_dns_cache_scope_expiry_and_size(monkeypatch):
    lookups = []
    async def resolve(host, port, **kwargs):
        lookups.append((host, port))
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('fe80::123', port, 0, 7))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    cache = transport.AddressCache(capacity=1)
    addresses, cached = await cache.resolve('ha.local', 8123)
    assert addresses == [('fe80::123%7', socket.AF_INET6)] and not cached
    assert (await cache.resolve('ha.local', 8123))[1]
    cache.entries[('ha.local', 8123)] = (0, addresses)
    assert not (await cache.resolve('ha.local', 8123))[1]
    await cache.resolve('new.local', 8123)
    assert len(lookups) == 3 and len(cache.entries) == 1


@pytest.mark.asyncio
async def test_stale_address_refresh_preserves_hostname_and_tls(monkeypatch):
    cache = transport.AddressCache()
    cache.entries[('ha.local', 443)] = (time.monotonic() + 60, [('192.168.1.10', socket.AF_INET)])
    monkeypatch.setattr(transport, 'ADDRESSES', cache)
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.20', port))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    attempts = []
    sentinel = object()
    async def connect(url, **kwargs):
        attempts.append((url, kwargs))
        if len(attempts) == 1: raise OSError('old address')
        return sentinel
    monkeypatch.setattr(transport.websockets, 'connect', connect)
    assert await transport.open_socket('wss://ha.local/api/websocket') is sentinel
    assert [x[1]['host'] for x in attempts] == ['192.168.1.10', '192.168.1.20']
    assert all(x[0] == 'wss://ha.local/api/websocket' and 'ssl' not in x[1] and 'server_hostname' not in x[1] for x in attempts)
