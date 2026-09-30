"""Evaluate streaming detection on held-out synthetic voices; no cloud calls."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import json
import numpy as np
from scripts.train_wake import pcm_wav
from r2.config import DATA, MODELS
from r2.wake import WakeDetector


def main():
    voice_config = json.loads((DATA / "training" / "libritts.onnx.json").read_text(encoding="utf-8"))
    rng = np.random.default_rng(42)
    speakers = rng.choice(voice_config["num_speakers"], size=min(96, voice_config["num_speakers"]), replace=False)[::5]
    detector = WakeDetector(.65)
    result = {"positive": {"detected": 0, "total": 0}, "negative": {"detected": 0, "total": 0}, "errors": []}
    for speaker in speakers:
        for variant in range(7):
            detector.reset()
            pcm = pcm_wav(DATA / "training" / f"speaker_{speaker}_{variant}.wav")
            pcm = np.concatenate([np.zeros(16000), pcm, np.zeros(16000)]).astype("<i2")
            hit = False
            for start in range(0, len(pcm)-1280, 1280):
                found, score = detector.process(pcm[start:start+1280].tobytes())
                hit = hit or found
            bucket = result["positive" if variant < 2 else "negative"]
            bucket["total"] += 1
            bucket["detected"] += int(hit)
            if hit != (variant < 2):
                result["errors"].append({"speaker": int(speaker), "variant": variant, "detected": hit})
    (MODELS / "streaming_report.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__": main()
