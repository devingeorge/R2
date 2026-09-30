"""Exercise the app's real GPT-Live session without taking the browser microphone."""
import asyncio
import json
from pathlib import Path
import sys
import wave
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from piper import PiperVoice
from scipy.signal import resample_poly
from r2.config import Credentials, DATA
from r2.live import LiveSession


async def main():
    text = "Search the web for the official Home Assistant release notes for September 2026. Give me a short answer."
    path = DATA / "smoke_prompt.wav"
    voice = PiperVoice.load(DATA / "training" / "libritts.onnx")
    with wave.open(str(path),"wb") as output: voice.synthesize_wav(text,output)
    with wave.open(str(path),"rb") as source:
        audio = np.frombuffer(source.readframes(source.getnframes()),dtype="<i2").astype(np.float32)
        audio = resample_poly(audio,16000,source.getframerate())
    audio = np.concatenate([np.zeros(6400), audio, np.zeros(6400)]).astype("<i2")
    report = {"transcripts": [], "tools": [], "sources": [], "audio_bytes": 0, "errors": [], "closed": False}
    async def emit(event):
        kind = event["type"]
        if kind == "transcript": report["transcripts"].append({"role":event["role"],"text":event["delta"]})
        elif kind == "tool": report["tools"].append(event)
        elif kind == "sources": report["sources"].extend(event["sources"])
        elif kind == "audio": report["audio_bytes"] += len(event["audio"])*3//4
        elif kind == "error": report["errors"].append(event["message"])
        elif kind == "session_end": report["closed"] = True
    session = LiveSession(Credentials().get("openai_api_key"),None,[],emit,{"idle_seconds":30,"max_seconds":65})
    task = asyncio.create_task(session.run())
    try:
        await asyncio.wait_for(session.ready.wait(),20)
        for position in range(0,16000*45,1280):
            if session.closing: break
            frame = audio[position:position+1280]
            if len(frame)<1280: frame=np.pad(frame,(0,1280-len(frame)))
            session.feed(frame.tobytes())
            await asyncio.sleep(.08)
            if report["sources"] and position>16000*30: break
    except asyncio.TimeoutError: report["errors"].append("Start timed out")
    finally:
        session.stop("Smoke test complete")
        await asyncio.wait_for(task,25)
    (DATA / "live_validation.json").write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=="__main__":asyncio.run(main())

