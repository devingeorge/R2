# Wake-to-session startup investigation

Measured locally with Python 3.12, OpenAI SDK 3.19.2, real GPT-Live WebSockets,
the configured Home Assistant catalog, and the real ONNX wake detector. Each
before/after batch used a fresh Python process followed by two repeat wakes.
Both batches used the same cached synthetic recording:
“Hey R2, tell me a short joke about penguins.” Audio continued during startup.

## Evidence and change

The original path was:

`wake detected → detector.reset → credentials/settings → HA catalog → SDK client → WebSocket open → session.start → session.started → send buffered audio`

Two local waits were avoidable:

- Resetting the detector took 63–78 ms between detection and entering the
  startup handler. Live conversations bypass the detector, and session-end
  cleanup already resets it. Successful activation now uses that existing
  cleanup. Calibration and missing-key activation still reset immediately.
- Home Assistant discovery took 140–203 ms before the cloud connection even
  began. It now runs concurrently with the WebSocket handshake. The fresh
  catalog is still required before sending the unchanged session configuration.
  No stale permissions, device names, or credentials are cached.

The new path is:

`wake detected → credentials/settings → [HA catalog || SDK/WebSocket] → session.start → session.started → send buffered audio`

The UI receives “connecting” before discovery completes. It still receives
“listening” directly from `session.started`, without a polling/debounce wait.
The model prompts, tools, configuration, voice, and audio pacing are unchanged.
No OpenAI connection starts before activation. Session duration accounting,
30-second idle handling, five-minute limit, and session-end cleanup remain in
place. Startup preparation is cancelled on failure/early stop and cannot send
`session.start` after a stop.

## Actual before/after measurements

All values below are **milliseconds after wake detection**, not durations of
the individual stages. Windows `time.monotonic()` had approximately 16 ms
resolution in this environment. These six observations are not percentile or
statistical latency guarantees; network and model variation are visible.

| Milestone | Fresh process before → after | Repeat 1 before → after | Repeat 2 before → after |
|---|---:|---:|---:|
| Startup handler entered | 78 → 0 | 78 → 0 | 63 → 0 |
| WebSocket connection initiated | 422 → 110 | 218 → 0 | 203 → 0 |
| WebSocket opened | 1,000 → 657 | 609 → 297 | 641 → 360 |
| `session.start` sent | 1,016 → 657 | 609 → 313 | 641 → 360 |
| `session.started` / session ready | **1,360 → 969** | **906 → 641** | **1,047 → 610** |
| First user audio sent | 1,360 → 969 | 906 → 641 | 1,047 → 610 |
| First assistant audio delta (may be silent) | 2,047 → 1,641 | 1,562 → 1,438 | 1,719 → 1,235 |
| First nonsilent assistant audio forwarded | 5,094 → 4,250 | 4,281 → 4,610 | 4,656 → 3,829 |
| Actual first sound from speakers | Not measured | Not measured | Not measured |

Readiness improved by 391 ms (29%) on the fresh-process run and by 265 / 437 ms
on the repeat runs (mean 351 ms, 36%). Those total differences also include
network variation. The directly removed local serialized work was 203–281 ms.

After the change, the catalog finished before the socket opened in every run.
Handshake durations were 547 / 297 / 360 ms; start-to-ready durations were
312 / 328 / 250 ms. Fresh-process credential loading took 47 ms and SDK setup
63 ms; repeat values were below the clock resolution. Prompt assembly and the
extra settings read were also below that resolution, so no prompt changes or
additional credential caching were justified.

Raw reports: `data/startup-before.json`, `data/startup-after.json`. They contain
transcripts, milestones, and errors, with no credentials. Original source and
the instrumented pre-optimization source are retained locally under
`data/startup-original/` and `data/startup-instrumented-baseline/` for inspection.
The entire `data` directory remains ignored by Git.

## Audio and lifecycle verification

All six real sessions transcribed “tell me a short joke about penguins” in full,
returned assistant audio, used no tools, reported no errors, and closed. The
wake phrase itself may appear partially as “R2” or “R two”, as before; the
six-frame (480 ms) pre-roll is unchanged. The benchmark bypasses browser capture
and playback, so this does not establish real-room microphone reliability.

53 Python tests pass, including new coverage for both orders of catalog/socket
completion; exact-once ordered opening frames; no transmission before
`session.started`; early-stop/failed-handshake cleanup; idle-no-cloud behavior;
detector resets; and bounded startup buffering. A 126th queued 80 ms frame still
ends the session instead of growing the queue or discarding speech. Existing
silence catch-up and transmission pacing are unchanged. Two Node tests verify
immediate UI readiness, the unchanged 25 ms playback cushion, output-time
estimation with leading silence, and avoiding a playback-completed claim after
an early stop. JavaScript syntax checks pass.

## Timing instrumentation and reproduction

- Server: `StartupTrace` uses `time.monotonic()` relative to the activation.
  It records wake detection, handler entry, capture availability, local
  credential loading, settings, catalog, SDK creation, WebSocket open,
  configuration assembly/send, `session.started`, first audio sent, first audio
  received, first nonsilent forwarding, and session end. A single bounded
  summary is logged as `R2 startup` on session exit and sent to the browser.
- Browser: `window.r2StartupTiming` retains only the latest session. Browser
  marks use `performance.now()` for microphone acquisition, capture graph/first
  frame, observed activation, readiness receipt/UI update/render opportunity,
  audio arrival/scheduling, and first buffer completion. Audible output is
  explicitly an estimate using `AudioContext.getOutputTimestamp()` or output
  latency estimates, including leading silence within the first buffer. It is
  not a microphone/loopback measurement of sound actually emitted.
- Browser and server clocks have different origins. Compare durations within
  each clock; never subtract their raw timestamps. A negative server capture
  mark means capture was available before activation. The benchmark uses a
  browser sink, so its reports contain no browser/display/hardware timings.

To benchmark the currently checked-out implementation (brief paid sessions):

```powershell
.\.venv\Scripts\python.exe -u scripts\measure_startup.py --run --label after
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/test_startup_ui.cjs
node --check static/app.js
node --check static/capture.js
```

`--label` only names the report; it does not switch implementations. The harness
prohibits tool execution, uses synthetic audio, and does not take the microphone
or browser's local WebSocket ownership. A fresh process is not an OS cold boot
or a full browser-permission test.

The other timers were not startup gates: two wake-score hits / 80 ms capture
frames precede detection; the two-second cooldown follows a conversation;
15-second UI polling refreshes idle device cards; the 500 ms watchdog runs
after readiness; and device-verification polling happens only during tools.
There is no activation-time credential refresh request, tool broker startup,
or automatic WebSocket retry (`max_retries=0`). The 8-second catalog timeout and
15-second readiness timeout are error bounds, not fixed startup sleeps.

## Remaining delay and next useful investigation

The WebSocket handshake plus server readiness now dominate. The next useful
step is a larger sample on the normal microphone/browser path, with DNS/TCP/TLS
handshake breakdown and browser output timing, before deciding whether any
non-session transport preparation is worthwhile. Do not keep a billable idle
session open. If Home Assistant discovery is slow on another network, a
carefully invalidated catalog preparation strategy could help, but these runs
do not justify that added state-management complexity.

The official [GPT-Live WebSocket guide](https://developers.openai.com/api/docs/guides/voice-websockets?api=live)
and installed SDK require `session.start` first and `session.started` before
audio/application commands. This implementation retains that gate.
