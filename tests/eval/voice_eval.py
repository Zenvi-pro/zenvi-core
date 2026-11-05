"""Measure voice matching on LibriSpeech (development only: nothing from it is kept in the repository).

    ZENVI_VOICE_MODEL=<voxceleb_resnet34_LM.onnx> ZENVI_LIBRISPEECH=<folder holding the speaker folders, e.g. LibriSpeech/dev-clean-2> python tests/eval/voice_eval.py

Part 1: same-speaker and different-speaker pairs of utterances (agreement and the cost of each cosine threshold).
Part 2: mixed "conversations" of 2 to 4 speakers with known turns, grouped by ``voiceprint.speakers_of``: the share of words given the right speaker.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from classes.media_index import voiceprint as V  # noqa: E402


def load_flac(path: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def session(model: str):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    return ort.InferenceSession(model, opts, providers=["CPUExecutionProvider"])


def utterances(root: str, per_speaker: int = 12, min_seconds: float = 4.0):
    out = {}
    for spk in sorted(os.listdir(root)):
        files = sorted(str(p) for p in Path(root, spk).rglob("*.flac"))
        picked = []
        for f in files:
            if len(picked) >= per_speaker:
                break
            x = load_flac(f)
            if len(x) >= min_seconds * V.SAMPLE_RATE:
                picked.append(x)
        if len(picked) >= 4:
            out[spk] = picked
    return out


def auc(pos, neg) -> float:
    order = np.argsort(np.concatenate([neg, pos]))
    ranks = np.empty(len(order))
    ranks[order] = np.arange(1, len(order) + 1)
    return float((ranks[len(neg):].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def verification(sess, data) -> dict:
    vecs, labels = [], []
    for k, (spk, xs) in enumerate(data.items()):
        for x in xs:
            vecs.append(V.embed(sess, x[: 10 * V.SAMPLE_RATE]))
            labels.append(k)
    E, L = np.stack(vecs), np.array(labels)
    iu = np.triu_indices(len(E), 1)
    same = (L[:, None] == L[None, :])[iu]
    sims = (E @ E.T)[iu]
    pos, neg = sims[same], sims[~same]
    eer_thr = min(np.linspace(0.2, 0.8, 121), key=lambda t: abs((pos < t).mean() - (neg >= t).mean()))
    return {"speakers": len(data), "utterances": len(E), "pairs": {"same": int(len(pos)), "different": int(len(neg))}, "auc": round(auc(pos, neg), 4),
            "same_p5": round(float(np.percentile(pos, 5)), 3), "different_p99": round(float(np.percentile(neg, 99)), 3),
            "eer_threshold": round(float(eer_thr), 3), "eer": round(float((pos < eer_thr).mean()), 4),
            "by_threshold": {f"{t:.2f}": {"same_accepted": round(float((pos >= t).mean()), 3), "false_merge": round(float((neg >= t).mean()), 5)} for t in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)}}


def conversations(sess, data, trials: int = 60, seed: int = 0, threshold: float = V.IN_FILE, pause: float = 1.0) -> dict:
    rng = np.random.default_rng(seed)
    names = list(data)
    right = total = exact = 0
    for _ in range(trials):
        k = int(rng.integers(2, 5))
        spk = list(rng.choice(len(names), size=k, replace=False))
        turns = []
        for _t in range(int(rng.integers(6, 10))):
            s = int(rng.choice(spk))
            turns.append((s, data[names[s]][int(rng.integers(0, len(data[names[s]])))][: int(rng.integers(3, 8)) * V.SAMPLE_RATE]))
        gap = np.zeros(int(pause * V.SAMPLE_RATE), dtype=np.float32)
        audio = np.concatenate([np.concatenate([t[1], gap]) for t in turns])
        words, truth, at = [], [], 0.0
        for s, x in turns:
            dur = len(x) / V.SAMPLE_RATE
            for w in np.arange(at, at + dur - 0.3, 0.3):
                words.append(SimpleNamespace(startSec=float(w), endSec=float(w) + 0.25))
                truth.append(s)
            at += dur + pause                                # a pause when the speaker changes; pause=0 is rapid turn-taking
        got = V.speakers_of(sess, audio, words, threshold=threshold)["labels"]
        best = 0
        for perm in itertools.permutations(range(max(got) + 1), min(len(set(truth)), max(got) + 1)):
            mapping = dict(zip(sorted(set(truth)), perm))
            best = max(best, sum(1 for g, t in zip(got, truth) if mapping.get(t) == g))
        right += best
        total += len(words)
        exact += int(len(set(got)) == len(set(truth)))
    return {"conversations": trials, "words_with_the_right_speaker": round(right / total, 4), "right_number_of_speakers": round(exact / trials, 3)}


if __name__ == "__main__":
    sess = session(os.environ["ZENVI_VOICE_MODEL"])
    data = utterances(os.environ["ZENVI_LIBRISPEECH"])
    print(verification(sess, data))
    for pause in (1.0, 0.0):
        for t in (0.40, 0.45, 0.50, 0.55, 0.60):
            print(f"pause {pause:g} s, threshold {t}:", conversations(sess, data, threshold=t, pause=pause))
