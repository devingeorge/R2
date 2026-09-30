"""Opt-in real bedroom light test; always restores original power/brightness."""
import argparse
import asyncio
import io
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
from r2.home_assistant import HomeAssistant, HAError, resolve_target
from r2.live import LiveSession


async def main():
    settings, vault = read_settings(), Credentials()
    ha = HomeAssistant(settings["ha_url"], vault.get("ha_token"), settings["allowed_entities"])
    catalog = await ha.catalog()
    bedroom = {x["entity_id"] for x in resolve_target(catalog, "Bedroom")}
    async with ha.connect() as connection:
        initial = {s["entity_id"]: s for s in await connection.request("get_states") if s["entity_id"] in settings["allowed_entities"]}
    if len(bedroom) != 2 or any(initial[x]["state"] not in {"on", "off"} for x in bedroom):
        raise RuntimeError("Both expected bedroom lights must be available.")

    class BedroomOnly:
        async def get_lights(self, **kwargs): return await ha.get_lights(**kwargs)
        async def set_lights(self, target, **kwargs):
            if {x["entity_id"] for x in resolve_target(catalog, target)} != bedroom:
                raise HAError("This validation run permits only the two bedroom bulbs.")
            return await ha.set_lights(target, **kwargs)

    report = {"tool_results": [], "transcripts": [], "errors": [], "restored": False, "other_lights_unchanged": False}
    results, frames = asyncio.Queue(), asyncio.Queue()
    async def emit(event):
        if event["type"] == "tool":
            report["tool_results"].append(event)
            if event["name"] == "set_lights": results.put_nowait(event["result"])
        elif event["type"] == "transcript":
            report["transcripts"].append({"role": event["role"], "text": event["delta"]})
        elif event["type"] == "error": report["errors"].append(event["message"])
    voice = PiperVoice.load(DATA / "training" / "libritts.onnx")
    def enqueue(text):
        data = io.BytesIO()
        with wave.open(data, "wb") as output: voice.synthesize_wav(text, output)
        data.seek(0)
        with wave.open(data, "rb") as source:
            pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32)
            pcm = resample_poly(pcm, 16000, source.getframerate())
        pcm = np.concatenate([np.zeros(3200), pcm, np.zeros(3200)]).astype("<i2")
        for offset in range(0, len(pcm), 1280):
            frame = pcm[offset:offset+1280]
            frames.put_nowait(np.pad(frame, (0, 1280-len(frame))).tobytes())
    session = LiveSession(vault.get("openai_api_key"), BedroomOnly(), catalog, emit, {"idle_seconds": 30, "max_seconds": 100})
    run = asyncio.create_task(session.run())
    async def feed():
        while not session.closing:
            session.feed(frames.get_nowait() if not frames.empty() else bytes(2560))
            await asyncio.sleep(.08)
    feeder = None
    try:
        await asyncio.wait_for(session.ready.wait(), 20)
        feeder = asyncio.create_task(feed())
        for text, expected in [("Set the bedroom lights to thirty percent brightness.", 30), ("Now dim them to twenty percent.", 20)]:
            enqueue(text)
            result = await asyncio.wait_for(results.get(), 35)
            if result.get("status") != "confirmed" or result.get("brightness_pct") != expected or len(result.get("confirmed", [])) != 2:
                raise RuntimeError("Unexpected tool result: " + json.dumps(result))
            print(f"Verified spoken command: both bedroom bulbs reached {expected}%", flush=True)
            await asyncio.sleep(3)
    except Exception as exc:
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        session.stop("Light validation complete")
        if feeder:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        try:
            await asyncio.wait_for(run, 25)
        finally:
            async with ha.connect() as connection:
                for entity_id in bedroom:
                    original = initial[entity_id]
                    values = {}
                    if original["state"] == "on" and original["attributes"].get("brightness") is not None:
                        values["brightness"] = original["attributes"]["brightness"]
                    await connection.request("call_service", domain="light", service="turn_" + original["state"], service_data=values, target={"entity_id": entity_id})
                deadline = time.monotonic() + 10
                def same(a, b):
                    return a["state"] == b["state"] and (a["state"] == "off" or a.get("attributes", {}).get("brightness") == b.get("attributes", {}).get("brightness"))
                while True:
                    current = {s["entity_id"]: s for s in await connection.request("get_states") if s["entity_id"] in initial}
                    report["restored"] = all(same(initial[x], current[x]) for x in bedroom)
                    report["other_lights_unchanged"] = all(same(initial[x], current[x]) for x in initial if x not in bedroom)
                    if report["restored"] or time.monotonic() > deadline: break
                    await asyncio.sleep(.4)
            (DATA / "light_validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k != "transcripts"}, indent=2))
    if report["errors"] or not report["restored"]: raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Run the real light/paid voice test")
    if parser.parse_args().run: asyncio.run(main())
    else: parser.print_help()
