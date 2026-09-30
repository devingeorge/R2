"""Run a brief real API check using synthetic speech, without microphone capture."""
import asyncio
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
import numpy as np
import websockets
from piper import PiperVoice
from scipy.signal import resample_poly
from r2.config import DATA


async def main():
    prompt = "Search the web for the official Home Assistant release notes for September 2026. Give me a brief answer."
    voice = PiperVoice.load(DATA / "training" / "libritts.onnx")
    path = DATA / "smoke_prompt.wav"
    with wave.open(str(path), "wb") as output:
        voice.synthesize_wav(prompt, output)
    with wave.open(str(path), "rb") as source:
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        pcm = resample_poly(pcm.astype(np.float32), 16000, source.getframerate())
    pcm = np.concatenate([np.zeros(6400), pcm, np.zeros(6400)]).astype("<i2")
    token = httpx.get("http://127.0.0.1:8765/api/status").json()["csrf"]
    report = {"prompt": prompt, "transcripts": [], "sources": [], "tools": [], "errors": [], "audio_bytes": 0, "session_end": False}
    async with websockets.connect("ws://127.0.0.1:8765/ws", origin="http://127.0.0.1:8765", subprotocols=["r2", token], proxy=None) as ws:
        ready = asyncio.Event()
        done = asyncio.Event()
        async def receive():
            async for raw in ws:
                event = json.loads(raw)
                kind = event["type"]
                if kind == "state" and event.get("state") == "listening": ready.set()
                if kind == "transcript": report["transcripts"].append({"role": event["role"], "text": event["delta"]})
                if kind == "audio": report["audio_bytes"] += len(event["audio"]) * 3 // 4
                if kind == "sources": report["sources"].extend(event["sources"])
                if kind == "tool": report["tools"].append(event)
                if kind == "error": report["errors"].append(event["message"]); done.set()
                if kind == "session_end": report["session_end"] = True; done.set()
        reader = asyncio.create_task(receive())
        await ws.send(json.dumps({"type": "activate"}))
        try:
            await asyncio.wait_for(ready.wait(), 20)
            started = time.monotonic()
            position = 0
            while time.monotonic() - started < 38 and not done.is_set():
                frame = pcm[position:position+1280]
                if len(frame) < 1280: frame = np.pad(frame,(0,1280-len(frame)))
                position += 1280
                await ws.send(frame.tobytes())
                await asyncio.sleep(.08)
                if report["sources"] and time.monotonic()-started > 26:
                    break
        except asyncio.TimeoutError:
            report["errors"].append("Timed out waiting for live session")
        finally:
            await ws.send(json.dumps({"type":"stop"}))
            try: await asyncio.wait_for(done.wait(), 15)
            except asyncio.TimeoutError: report["errors"].append("Close timeout")
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    (DATA / "smoke_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__": asyncio.run(main())

