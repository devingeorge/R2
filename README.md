# R2 · local voice assistant

A Windows browser companion for Home Assistant lights, door locks, and short web searches. “Hey R2” runs locally; only an activated conversation streams audio to OpenAI.

## Start

Double-click **Start R2.cmd**, then open **http://127.0.0.1:8765** in Chrome or Edge. Keep the terminal and browser tab open, and keep the computer awake. The server binds only to the loopback interface.

For a fresh installation with Python 3.12:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -u scripts\train_wake.py
.\.venv\Scripts\python.exe -m uvicorn r2.server:app --host 127.0.0.1 --port 8765 --no-access-log
```

## Connections

In Settings, save an OpenAI project API key and a Home Assistant long-lived access token. They are stored using the Windows Credential Manager backend, under `R2 Voice Assistant`. No key is stored in source files, browser storage, or application settings. A valid OpenAI key still requires API credits and access to both configured models.

Home Assistant defaults to `http://homeassistant.local:8123`. The first connection discovers only these known TP-Link devices and saves their actual light entity IDs to the local allowlist:

| Room | Lights |
| --- | --- |
| Bedroom | Devin Light; Leah’s Light |
| Fireplace Room | Fireplace Lamp; fireplace 2 |
| Living Room | Living room lamp; Living room light |
| Laundry Room | laundry |

Room assignments and friendly names are read from Home Assistant on subsequent requests. A Home Assistant token inherits its account’s access; the application enforces separate allowlists for the seven lights and selected door locks. Do not expose this service to the internet or change its bind address. A separate non-administrator Home Assistant account can further limit the token’s privileges.

The backend provides `get_lights`, `set_lights`, `get_locks`, and `set_lock` functions and OpenAI’s built-in `web_search`. It does not offer general Home Assistant service execution. Changes use `call_service`, never entity-state writes. It verifies fresh states before confirming success, uses state-change events to avoid polling delays when supported, and returns partial results for unavailable bulbs. Repeated function call IDs are executed once per conversation; cloud transport failures are not automatically replayed.

During a conversation, R2 reuses one authenticated Home Assistant connection and caches device metadata while continuing to read current states. Registry changes invalidate that metadata. End the current conversation before changing Home Assistant connection settings or door permissions. The local connection closes when the conversation ends; DNS preparation while listening does not open a paid OpenAI session. See [Home Assistant tool latency](docs/home-assistant-latency.md) for measurements and the read-only benchmark.

### Door locks

In Settings → Door voice control, select each permitted lock and give it a distinct name such as **Front door** or **Back door**. Serial-number endings distinguish devices with identical Home Assistant names. Names are saved in R2 without renaming Home Assistant devices. New locks remain disabled until selected; changing the Home Assistant address clears lock permissions. End the current conversation before changing door settings.

Say “Unlock the front door,” “Lock the back door,” or “Are the doors locked?” R2 asks which door when the request is ambiguous. Each action targets one permitted entity and uses Home Assistant's [lock or unlock service](https://www.home-assistant.io/integrations/lock/). R2 checks the reported state before announcing success, avoids resending a command when already in the requested state, and reports unconfirmed results without automatically retrying. Lock status does not establish whether a door is physically open or closed. Code-dependent locks are not supported by R2's voice tools; no spoken code is accepted.

For a voice routing check without moving any physical lock, run `.\.venv\Scripts\python.exe -u scripts\smoke_locks.py --run`. This uses brief paid GPT-Live API time and synthetic speech against simulated door states. It verifies that “Unlock the front door” selects the configured front-door entity, and saves the result to `data/lock_voice_validation.json`. Unit tests cover real service payloads, state verification, ambiguous targets, permissions, and failure handling using fake Home Assistant connections.

## Using R2

- **Start Listening:** allow the microphone, then say “Hey R2.” Detection stays on this computer until activation.
- **Talk to R2:** open a conversation without the wake phrase.
- **Mute microphone:** stop sending microphone content. An active cloud session remains connected until ended or timed out.
- **End conversation:** close the cloud session and return to local listening. Already-dispatched device operations may finish; no new operations start.
- **Stop Listening:** release the microphone and end the conversation.

Examples: “Turn on the bedroom lights,” “Dim them to thirty percent,” “Is the laundry light on?” and “Search the web for the next SpaceX launch.” Search citations appear as clickable links beneath the conversation.

Conversation context lasts for the current session. Sessions close after 30 seconds without conversation activity once playback and backend work finish; a five-minute hard limit also applies. API voice billing is based on connection duration, with backend and web-search usage additional.

R2's voice and delegated tool prompts live in `r2/prompts.py`. They favor warm, concise replies, direct execution without a repeated preamble, contextual follow-ups, and brief verified outcomes. R2 yields to spoken interruptions and carries corrections forward; interrupting speech does not itself cancel a dispatched light command. Speaking preferences apply within the current conversation. Source links stay on screen rather than being read aloud. The prompt structure follows the [GPT-Live prompting guide](https://developers.openai.com/api/docs/guides/live-prompting).

The revised prompts were checked in a real GPT-Live conversation with simulated light tools: a greeting used no tools, a bedroom brightness command and “dim them” follow-up targeted both bulbs correctly, and a partial result produced an accurate unavailable-bulb warning. That test changed no physical devices; its transcript is saved in `data/prompt_validation.json`. Short acknowledgments can still occur despite the preference for minimal preambles. Real microphone pacing and interruption behavior remain listening tests.

## Wake-word model and calibration

The included training script downloads the official openWakeWord feature models and the multispeaker Piper LibriTTS-R voice. It synthesizes “Hey R2” and ordinary/confusable phrases, augments them, holds out entire speakers, and trains an ONNX classification head on openWakeWord embeddings. Training and inference use the CPU. Downloads and audio caches remain in `data/training`; model artifacts remain in `models`. These generated files are excluded from Git.

`models/training_report.json` describes held-out clip classification. `scripts/evaluate_wake.py` evaluates streaming detection over held-out synthetic voices. These tests **do not establish reliability for your microphone or room**, and do not measure real-world false activations per hour.

Use **Settings → Test wake word locally**. This test never starts a cloud session. Try at least 20 wake phrases at typical distances, plus ordinary speech and background TV. Aim for at least 18 successful detections and no accidental activations during a 10-minute initial background test. If needed, adjust the threshold, save it, and retest.

Under **Tune with your microphone**, record at least 10 positive clips and several background clips, then click **Train with local recordings**. These explicit calibration recordings are stored locally under `data/calibration` and incorporated into training. Normal listening audio is held only in bounded memory buffers; it is not recorded to disk. Mark the microphone test complete only after it succeeds. Synthetic training success alone never sets that flag.

The model pipeline draws on [openWakeWord](https://github.com/dscripka/openWakeWord) and [Piper voices](https://huggingface.co/rhasspy/piper-voices). Consult their licenses and the downloaded voice model card before redistribution. It is intended here for this personal local installation.

## Validation and troubleshooting

Wake-to-session startup timings, measured before/after results, and the synthetic
benchmark are documented in [Startup latency](docs/startup-latency.md). The latest
browser trace is available as `window.r2StartupTiming`; server output includes a
bounded `R2 startup` timing summary when each conversation ends.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check static/app.js
node --check static/capture.js
node --test tests/test_startup_ui.cjs
.\.venv\Scripts\python.exe scripts\evaluate_wake.py
```

With API credit available and no browser connection active, `scripts/smoke_live.py` uses synthetic speech through the actual local WebSocket and GPT-Live connection to test web search, captions, returned audio, citations and session closure. `scripts/smoke_direct.py` exercises the same session implementation without taking the browser connection. Both incur brief API usage and save reports under `data`. Neither operates lights.

For an opt-in physical light test, run `.\.venv\Scripts\python.exe -u scripts\smoke_lights.py --run`. This incurs brief API usage and changes the two bedroom bulbs to 30%, then 20%, through spoken GPT-Live commands. It snapshots their power and brightness, restores them in a `finally` block, verifies restoration, and checks that the other five lights remain unchanged. Results are saved to `data/light_validation.json`.

Verified during setup: 17 automated tests; JavaScript syntax and installed dependencies; a real GPT-Live search with input/output transcripts, returned audio, a source link, and clean session closure. All seven Home Assistant bulbs are connected. A spoken bedroom command reached 30% on both bulbs, and the follow-up “Now dim them to twenty percent” reached 20%. Both were restored to their original on/25% settings, and the other five lights were unchanged. The retrained detector recognized 40/40 held-out synthetic wake phrases with 0/100 triggers on held-out negative phrases. Microphone reliability remains a separate real-room calibration test.

If the API reports an empty credit balance, add credits in the OpenAI Platform billing settings. A saved key by itself does not mean live audio is ready. If microphone access is declined, allow it from Chrome’s site permission control. Only one listening tab can own the audio connection at a time.

Background server output is in `data/server.log` and `data/server-error.log` when launched by the setup agent; training started from the interface logs to `data/training.log`. Credentials and raw upstream error bodies are not logged. To stop a manually launched server, press Ctrl+C in its terminal.

Implementation references: [GPT-Live delegation](https://developers.openai.com/api/docs/guides/live-delegation), [GPT-Live WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=live), [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/).
