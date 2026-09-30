from __future__ import annotations

import numpy as np

from r2.config import MODELS


class WakeDetector:
    def __init__(self, threshold=0.65):
        path = MODELS / "hey_r2.onnx"
        if not path.exists():
            raise FileNotFoundError("The Hey R2 model has not been trained yet.")
        self.modified_at = path.stat().st_mtime_ns
        from openwakeword.model import Model
        self.model = Model(wakeword_models=[str(path)], inference_framework="onnx", melspec_model_path=str(MODELS / "melspectrogram.onnx"), embedding_model_path=str(MODELS / "embedding_model.onnx"))
        self.threshold = threshold
        self.hits = 0

    def process(self, pcm: bytes) -> tuple[bool, float]:
        scores = self.model.predict(np.frombuffer(pcm, dtype="<i2"))
        score = float(max(scores.values(), default=0))
        self.hits = self.hits + 1 if score >= self.threshold else 0
        return self.hits >= 2, score

    def reset(self):
        self.model.reset()
        self.hits = 0
