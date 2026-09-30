"""Brief paid Live benchmark with real wake detection, HA reads, and synthetic speech.

No microphone or device actions. Run in a fresh process for each before/after set.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np

from r2.config import DATA, read_settings
from r2.server import VoiceChannel
from r2.wake import WakeDetector

PHRASE = "Hey R2, tell me a short joke about penguins."


def speech():
    path = DATA / "startup-prompt.wav"
    if not path.exists():
        from piper import PiperVoice
        voice = PiperVoice.load(DATA / "training" / "libritts.onnx")
        with wave.open(str(path), "wb") as output:
            voice.synthesize_wav(PHRASE, output)
    from scipy.signal import resample_poly
    with wave.open(str(path), "rb") as source:
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        pcm = resample_poly(pcm.astype(np.float32), 16000, source.getframerate())
    return np.concatenate([np.zeros(16000), pcm, np.zeros(16000)]).astype("<i2")


async def main(args):
    pcm = speech()
    reports = []
    report = {}

    class BrowserSink:
        async def send_json(self, event):
            kind = event["type"]
            if kind == "transcript":
                report["transcripts"].append({"role": event["role"], "text": event["delta"]})
            elif kind == "audio":
                report.setdefault("first_audio_at", time.monotonic())
                report["audio_chunks"] += 1
            elif kind in {"error", "notice"}:
                report["messages"].append(event["message"])
            elif kind == "tool":
                report["tools"].append(event["name"])
            if "startup" in event:
                report["startup"] = event["startup"]

    channel = VoiceChannel(BrowserSink())
    channel.armed = True
    channel.detector = WakeDetector(read_settings()["wake_threshold"])
    for attempt in range(args.runs):
        report = {"run": attempt + 1, "condition": "fresh_process" if attempt == 0 else "repeat",
                  "transcripts": [], "messages": [], "tools": [], "audio_chunks": 0}
        reports.append(report)
        deadline = time.monotonic() + 22
        position = 0
        triggered = False
        try:
            while time.monotonic() < deadline:
                frame = pcm[position:position + 1280]
                if len(frame) < 1280:
                    frame = np.pad(frame, (0, 1280 - len(frame)))
                position += 1280
                await channel.frame(frame.tobytes())
                triggered |= channel.live is not None
                if channel.live:
                    # The joke should use no tools; prohibit physical actions even if
                    # the model unexpectedly delegates one during this benchmark.
                    channel.live.executor.closed = True
                if triggered and channel.live is None:
                    break
                if report.get("first_audio_at") and time.monotonic() - report["first_audio_at"] > 3:
                    break
                await asyncio.sleep(.08)
        finally:
            if channel.live:
                channel.live.stop("Startup benchmark complete")
            if channel.live_task:
                await asyncio.wait_for(channel.live_task, 25)
        report.pop("first_audio_at", None)
        if not triggered:
            report["messages"].append("Synthetic wake word did not activate")
        print(json.dumps(report), flush=True)
        if attempt + 1 < args.runs:
            await asyncio.sleep(max(0, channel.cooldown_until - time.monotonic()) + .1)
    result = {"label": args.label, "phrase": PHRASE, "runs": reports,
              "limitations": "Synthetic capture and browser sink; no hardware playback measurement. First run is a fresh Python process, not an OS cold boot."}
    (DATA / ("startup-" + args.label + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Required: uses brief paid API sessions")
    parser.add_argument("--label", choices=["before", "after"], required=True)
    parser.add_argument("--runs", type=int, choices=range(1, 6), default=3)
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to use the real API")
    asyncio.run(main(args))
