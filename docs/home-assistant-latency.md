# Home Assistant tool latency

Implemented September 24, 2026. This implementation is limited to the local
Home Assistant path and does not edit OpenAI configuration, prompts, tool
schemas, conversation limits, audio handling, or tool-call deduplication. A
separate concurrent edit set backend reasoning effort to `none`; it was left
intact and is not part of the local transport measurements below.

## Changes

- A voice conversation reuses one authenticated Home Assistant WebSocket from
  initial catalog loading through tool execution and verification. It closes
  after the conversation, on startup failure, and on browser-disconnect cleanup. HTTP
  settings/status helpers continue to use short-lived connections.
- DNS is prepared asynchronously at Start Listening, without credentials or
  an OpenAI connection. Resolved addresses are cached in memory for five
  minutes, with a maximum of eight host/port entries. Failed connection attempts
  invalidate the entry and may resolve again before authentication. IPv6 scope
  IDs are preserved. The original URL/Host and TLS hostname checks remain in
  effect; only the TCP destination uses the resolved address.
- One reader routes concurrent command results by request ID and consumes
  subscribed events. Entity, device, and area metadata are read concurrently,
  cached per connection for up to 60 seconds, and invalidated by registry
  changes. Reconnection starts with empty metadata. Metadata is not cached if
  registry event subscriptions are denied.
- Every status query and action still obtains a fresh state snapshot. Settings
  are read anew for each conversation; the current conversation must finish
  before changing Home Assistant credentials/address or door permissions. The
  settings guard is rechecked after asynchronous validation to avoid a race
  with activation.
- Verification subscribes before issuing a command, reads states immediately,
  and then wakes on relevant `state_changed` events. Events trigger a fresh
  read; they are not taken as unconditional proof of success. Coalesced events
  cannot create an expanding queue, and events for unrelated entities do not
  wake verification. Clearing the wake signal before each fresh read prevents
  a change arriving during that read from being missed.
- Existing 8-second light / 10-second lock verification deadlines remain.
  Missing events cause a final state read at the deadline. Denied state-event
  subscriptions fall back to the existing 350 ms polling interval.
- A failed or uncertain service call is never resent by the transport.
  Disconnects wake pending requests and verification. A later request can open
  a new connection; repeated tool-call IDs still return their cached result.

The event/reply routing follows the [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/).

## Runtime measurements

Before and after used the same local computer, configured Home Assistant, and
`get_lights('Living Room')`. Measurements use Python's monotonic
`time.perf_counter()` and contain no physical service calls.

| Status read | Before | After DNS preparation + connection reuse |
|---|---:|---:|
| First | 2,826.50 ms | 131.29 ms |
| Second | 2,812.10 ms | 10.33 ms |
| Third | 2,803.02 ms | 12.02 ms |

DNS preparation itself took **2,736 ms**. That work is moved before activation
when possible, not eliminated. An immediate cold activation, expired cache, or
address change can still pay the DNS cost. These numbers describe the local HA
read path, not end-to-end voice latency or actual device actuation.

A second read-only check using all seven permitted lights measured 112.33 ms,
8.29 ms, and 8.67 ms, after 2,734.77 ms of DNS preparation. All three metadata
subscriptions and the state-change subscription were accepted by the actual
Home Assistant server, and connection closure was verified.

Reports are saved locally in `data/ha-tools-before.json`,
`data/ha-tools-after.json`, and `data/ha-latency-latest.json`. The first two use
the same room selection; the latest report is overwritten by the harness.
Pre-change source is retained under `data/ha-latency-baseline/`. These local
diagnostics are ignored by Git and contain no credentials.

## Validation and reproduction

85 Python tests and two JavaScript UI tests pass. New coverage includes
connection/metadata reuse, fresh states, metadata expiry and registry changes,
out-of-order replies, events during a read, subscription-denied fallback,
uncertain-action deduplication, reconnection without action replay, cancellation
of concurrent requests, credential rejection, IPv6 DNS caching and expiry,
stale-address recovery, voice-session cleanup, and settings-update races.

A simulated action with a state change after 20 ms is verified within 300 ms,
without the old fixed polling sleep. Physical light/lock timing and the complete
model-delegation-to-spoken-confirmation path have not been measured in this
change. No real lights or locks were operated during validation.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/test_startup_ui.cjs
.\.venv\Scripts\python.exe -u scripts\measure_ha_latency.py
```

The latency harness only reads Home Assistant and tests event subscriptions.
It opens no OpenAI session and closes its local socket on completion. Restart
the R2 service to load the changed Python modules, then reconnect the browser.
