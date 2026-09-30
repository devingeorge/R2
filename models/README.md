This directory contains the generated local `hey_r2.onnx` wake-word classifier,
the official openWakeWord feature models, and evaluation reports. These binary
and generated report artifacts are ignored by Git. Run `scripts/train_wake.py`
to reproduce a candidate model, then `scripts/evaluate_wake.py` and the Settings
microphone test. Do not mark a candidate microphone-validated based only on
synthetic evaluation scores.
