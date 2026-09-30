"""Opt-in GPT-Live voice check using simulated locks; never calls a device service."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from piper import PiperVoice
from scipy.signal import resample_poly

from r2.config import Credentials, DATA, read_settings
from r2.home_assistant import HAError, HomeAssistant, resolve_lock
from r2.live import LiveSession


class SimulatedLocks:
    """No network connection or physical write method is reachable from these tools."""
    def __init__(self, catalog):
        self.catalog = [item | {"state": "locked"} for item in catalog if item["entity_id"].startswith("lock.")]

    async def get_locks(self, target="all"):
        locks = self.catalog if target == "all" else [resolve_lock(self.catalog, target)]
        return {"status": "ok", "locks": locks}

    async def set_lock(self, target, action):
        if action not in ("lock", "unlock"):
            raise HAError("Specify lock or unlock.")
        lock = resolve_lock(self.catalog, target)
        return {"status": "dry_run", "entity_id": lock["entity_id"], "action": action, "message": f'Dry run: would {action} {lock["name"]}. No physical lock was changed.'}


async def main():
    settings = read_settings()
    vault = Credentials()
    ha = HomeAssistant(settings["ha_url"], vault.get("ha_token"), settings["allowed_entities"], settings["allowed_locks"], settings["lock_names"])
    simulator = SimulatedLocks(await ha.lock_catalog())
    expected_id = resolve_lock(simulator.catalog, "Front door")["entity_id"]
    prompt = "Unlock the front door."
    path = DATA / "lock_voice_prompt.wav"
    voice = PiperVoice.load(DATA / "training" / "libritts.onnx")
    with wave.open(str(path), "wb") as output:
        voice.synthesize_wav(prompt, output)
    with wave.open(str(path), "rb") as source:
        audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32)
        audio = resample_poly(audio, 16000, source.getframerate())
    audio = np.concatenate([np.zeros(6400), audio, np.zeros(6400)]).astype("<i2")
    report = {"prompt": prompt, "simulation": True, "transcripts": [], "tools": [], "errors": [], "closed": False}
    tool_at = None

    async def emit(event):
        nonlocal tool_at
        if event["type"] == "transcript":
            report["transcripts"].append({"role": event["role"], "text": event["delta"]})
        elif event["type"] == "tool":
            report["tools"].append(event)
            tool_at = time.monotonic()
        elif event["type"] == "error":
            report["errors"].append(event["message"])
        elif event["type"] == "session_end":
            report["closed"] = True

    session = LiveSession(vault.get("openai_api_key"), simulator, simulator.catalog, emit, {"idle_seconds": 30, "max_seconds": 50})
    task = asyncio.create_task(session.run())
    try:
        await asyncio.wait_for(session.ready.wait(), 20)
        for position in range(0, 16000 * 40, 1280):
            if session.closing or (tool_at and time.monotonic() - tool_at > 7):
                break
            frame = audio[position:position + 1280]
            if len(frame) < 1280:
                frame = np.pad(frame, (0, 1280 - len(frame)))
            session.feed(frame.tobytes())
            await asyncio.sleep(.08)
    except asyncio.TimeoutError:
        report["errors"].append("Session startup timed out")
    finally:
        session.stop("Simulated lock voice check complete")
        await asyncio.wait_for(task, 25)
    report["passed"] = not report["errors"] and any(
        item["name"] == "set_lock" and item["result"].get("entity_id") == expected_id
        and item["result"].get("action") == "unlock" and item["result"]["status"] == "dry_run"
        for item in report["tools"]
    )
    (DATA / "lock_voice_validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Allow brief paid GPT-Live API usage; lock actions remain simulated.")
    if not parser.parse_args().run:
        parser.error("Pass --run to allow API usage. No physical lock action is performed.")
    asyncio.run(main())
