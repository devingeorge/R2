"""Train a CPU-friendly openWakeWord ONNX head for Hey R2.

Uses the upstream openWakeWord embedding backbone and a multispeaker Piper
voice. Personal recordings are optional and never leave this computer. This
produces a candidate model; microphone calibration is a separate acceptance step.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import urllib.request
import wave

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from scipy.signal import resample_poly

from r2.config import DATA, MODELS

VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx"
PHRASES = [
    "Hey, are you there?", "Hey Arthur.", "Hey Artoo?", "R two D two.",
    "Hey Siri.", "Hey Google.", "Alexa, turn on the bedroom lights.",
    "Are you ready?", "How are you doing?", "There are two lights in the bedroom.",
    "Turn the living room lamp on.", "Please dim the bedroom lights.",
    "What time is it?", "It is time for dinner.", "Can you hear me?",
    "The weather is beautiful today.", "I will see you tomorrow.",
    "Could you turn that down?", "Where did you put the keys?",
    "The laundry is finished.", "We should go for a walk.",
    "Do you want a cup of coffee?", "Search the web for the next launch.",
    "Hey, aren't you coming?", "Please switch off all of the lights.",
    "Here are two things to remember.", "Stay right here.", "What are you doing?",
    "Let us watch something on television.", "I think we have everything we need.",
    "The score is two to one.", "Thank you very much.", "Good morning everyone.",
    "Do not turn off the kitchen light.", "Are two people coming over?",
    "Hey, our food is ready.", "R2, turn on the lights.", "Hey, you too!",
]
# 'Hey Artoo' is phonetically the desired wake phrase, not a useful negative.
PHRASES.remove("Hey Artoo?")


def download(url, path):
    if path.exists():
        return
    print("Downloading", path.name, flush=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with urllib.request.urlopen(url, timeout=90) as response, temporary.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)
    temporary.replace(path)


def pcm_wav(path):
    with wave.open(str(path), "rb") as reader:
        data = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32)
        if reader.getnchannels() > 1:
            data = data.reshape(-1, reader.getnchannels()).mean(axis=1)
        rate = reader.getframerate()
    return resample_poly(data, 16000, rate).astype(np.float32)


def fixed_clip(audio, rng, augment=True):
    if augment:
        audio = resample_poly(audio, 100, int(rng.integers(88, 113)))
        audio = audio * rng.uniform(0.3, 1.1)
        if rng.random() < .6:
            delay = int(rng.integers(400, 2800))
            echo = np.pad(audio, (delay, 0))[:len(audio)]
            audio = audio + echo * rng.uniform(0.04, .25)
    size = 48000
    tail = int(rng.integers(2000, 6000))
    clipped = audio[-(size-tail):]
    padded = np.pad(clipped, (size-len(clipped)-tail, tail))
    if augment:
        padded += rng.normal(0, rng.uniform(5, 180), size)
    return np.clip(padded, -32767, 32767).astype(np.int16)


def export_model(classifier, scaler, path):
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    tensors = [numpy_helper.from_array(scaler.mean_.astype(np.float32), "mean"), numpy_helper.from_array(scaler.scale_.astype(np.float32), "scale")]
    nodes = [helper.make_node("Flatten", ["input"], ["flat"], axis=1), helper.make_node("Sub", ["flat", "mean"], ["centered"]), helper.make_node("Div", ["centered", "scale"], ["scaled"])]
    previous = "scaled"
    for i, (weight, bias) in enumerate(zip(classifier.coefs_, classifier.intercepts_)):
        tensors += [numpy_helper.from_array(weight.astype(np.float32), f"w{i}"), numpy_helper.from_array(bias.astype(np.float32), f"b{i}")]
        nodes += [helper.make_node("MatMul", [previous, f"w{i}"], [f"mm{i}"]), helper.make_node("Add", [f"mm{i}", f"b{i}"], [f"sum{i}"])]
        final = i == len(classifier.coefs_) - 1
        previous = "output" if final else f"relu{i}"
        nodes.append(helper.make_node("Sigmoid" if final else "Relu", [f"sum{i}"], [previous]))
    graph = helper.make_graph(nodes, "hey_r2", [helper.make_tensor_value_info("input", TensorProto.FLOAT, [None, 16, 96])], [helper.make_tensor_value_info("output", TensorProto.FLOAT, [None, 1])], initializer=tensors)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], producer_name="R2 local training", ir_version=9)
    onnx.checker.check_model(model)
    temp = path.with_suffix(".tmp.onnx")
    onnx.save(model, temp)
    temp.replace(path)


def main():
    MODELS.mkdir(exist_ok=True)
    cache = DATA / "training"
    cache.mkdir(parents=True, exist_ok=True)
    for filename in ["melspectrogram.onnx", "embedding_model.onnx"]:
        download("https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/" + filename, MODELS / filename)
    voice_path = cache / "libritts.onnx"
    download(VOICE_URL, voice_path)
    download(VOICE_URL + ".json", voice_path.with_suffix(".onnx.json"))
    download("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/MODEL_CARD", cache / "VOICE_MODEL_CARD.txt")
    from piper import PiperVoice, SynthesisConfig
    from openwakeword.utils import AudioFeatures
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import recall_score, precision_score
    from threadpoolctl import threadpool_limits

    rng = np.random.default_rng(42)
    voice = PiperVoice.load(voice_path)
    samples, labels, heldout = [], [], []
    speakers = rng.choice(voice.config.num_speakers, size=min(96, voice.config.num_speakers), replace=False)
    for index, speaker in enumerate(speakers):
        # Hold entire speakers out, not augmented copies of training recordings.
        validation = index % 5 == 0
        prompts = [(1, "Hey R two."), (1, "Hey, R2.")]
        prompts += [(0, PHRASES[(index * 5 + j) % len(PHRASES)]) for j in range(5)]
        for variant, (label, text) in enumerate(prompts):
            path = cache / f"speaker_{speaker}_{variant}.wav"
            if not path.exists():
                with wave.open(str(path), "wb") as wav:
                    voice.synthesize_wav(text, wav, SynthesisConfig(speaker_id=int(speaker), length_scale=float(rng.uniform(.82, 1.15))))
            audio = pcm_wav(path)
            for _ in range(2 if label else 1):
                samples.append(fixed_clip(audio, rng)); labels.append(label); heldout.append(validation)
            if not label and len(audio) > 40000:
                # Long ordinary phrases must also teach the beginning, not just
                # their final three seconds (where the initial 'Hey' is gone).
                samples.append(fixed_clip(audio[:40000], rng)); labels.append(0); heldout.append(validation)
        if index % 8 == 0:
            print(f"Generated speech for {index+1}/{len(speakers)} speakers", flush=True)
    # Include silence, noise and real microphone negatives in the training set.
    for _ in range(120):
        samples.append(np.clip(rng.normal(0, rng.uniform(0, 1500), 48000), -32767, 32767).astype(np.int16))
        labels.append(0); heldout.append(False)
    recordings = 0
    for label_name, label in [("positive", 1), ("negative", 0)]:
        paths = sorted((DATA / "calibration" / label_name).glob("*.wav"))
        for i, path in enumerate(paths):
            audio = pcm_wav(path)
            if label:
                indices = np.where(np.abs(audio) > max(250, np.max(np.abs(audio)) * .06))[0]
                if len(indices):
                    audio = audio[max(0, indices[0]-2000):min(len(audio), indices[-1]+2000)]
            chunks = [audio] if label else [audio[start:start+48000] for start in range(0, max(1, len(audio)-32000), 24000)]
            for chunk in chunks:
                for _ in range(8 if label else 2):
                    samples.append(fixed_clip(chunk, rng)); labels.append(label); heldout.append(i % 5 == 0)
            recordings += 1
    print(f"Extracting openWakeWord features from {len(samples)} clips…", flush=True)
    features = AudioFeatures(melspec_model_path=str(MODELS / "melspectrogram.onnx"), embedding_model_path=str(MODELS / "embedding_model.onnx"), inference_framework="onnx", ncpu=2)
    vectors, expanded_labels, expanded_heldout = [], [], []
    for offset in range(0, len(samples), 32):
        embeddings = features.embed_clips(np.stack(samples[offset:offset+32]), batch_size=32, ncpu=2)
        for j, embedding in enumerate(embeddings):
            label = labels[offset+j]
            starts = [len(embedding)-16, len(embedding)-17, len(embedding)-18] if label else range(0, len(embedding)-15, 2)
            for start in starts:
                if start < 0: continue
                vectors.append(embedding[start:start+16].reshape(-1))
                expanded_labels.append(label)
                expanded_heldout.append(heldout[offset+j])
        if offset % 160 == 0:
            print(f"Features: {min(offset+32,len(samples))}/{len(samples)}", flush=True)
    x, y, validation = np.stack(vectors), np.array(expanded_labels), np.array(expanded_heldout, dtype=bool)
    scaler = StandardScaler().fit(x[~validation])
    model = MLPClassifier(hidden_layer_sizes=(64,), max_iter=180, batch_size=64, random_state=42, early_stopping=True, n_iter_no_change=15, alpha=.03, learning_rate_init=.0008)
    print("Training classifier…", flush=True)
    with threadpool_limits(limits=2):
        model.fit(scaler.transform(x[~validation]), y[~validation])
    predicted = model.predict_proba(scaler.transform(x[validation]))[:, 1]
    threshold = .65
    report = {"phrase": "Hey R2", "synthetic_speakers": len(speakers), "microphone_recordings": recordings, "training_windows": int((~validation).sum()), "heldout_windows": int(validation.sum()), "heldout_recall": float(recall_score(y[validation], predicted >= threshold, zero_division=0)), "heldout_precision": float(precision_score(y[validation], predicted >= threshold, zero_division=0)), "threshold": threshold, "microphone_validated": False, "note": "Synthetic held-out speaker results are not a substitute for real microphone testing. False activations/hour has not been measured."}
    export_model(model, scaler, MODELS / "hey_r2.onnx")
    (MODELS / "training_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
    # Check the same inference path the application uses.
    from r2.wake import WakeDetector
    detector = WakeDetector(threshold)
    for start in range(0, 48000-1280, 1280):
        detector.process(samples[0][start:start+1280].tobytes())
    print("ONNX streaming smoke test passed. Microphone calibration is still required.", flush=True)


if __name__ == "__main__":
    main()
