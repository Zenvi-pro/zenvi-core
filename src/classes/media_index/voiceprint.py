"""
 @file
 @brief Who is speaking: a voice as 256 numbers, with plain numpy around one ONNX speaker model.

 The model is WeSpeaker's ResNet34 (CC-BY-4.0, trained on VoxCeleb2). It takes 80-band log-mel filterbank features computed the
 way Kaldi does (the features it was trained on, reproduced here without torchaudio) and returns one vector per clip of speech.
 A voiceprint is biometric data, handled like a face: local only, never logged, never uploaded.
"""

from __future__ import annotations

import wave
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

SAMPLE_RATE = 16000
FRAME = 400                  # 25 ms
SHIFT = 160                  # 10 ms
FFT = 512
MELS = 80
LOW_HZ = 20.0
PREEMPH = 0.97
MIN_SECONDS = 1.0            # speech shorter than this is too little to tell a voice from
SEGMENT_MAX_SECONDS = 6.0
GAP_SECONDS = 0.8            # a pause longer than this ends a stretch of speech
EMBED_DIMS = 256
# cosine lines, set from tests/eval/results/voice_calibration.json
IN_FILE = 0.50               # grouping the voices heard in one file
SAME_VOICE = 0.55            # across files: at or above, the same speaker (false merges are the costly mistake, so this line is stricter)
UNSURE_VOICE = 0.45          # from here to SAME_VOICE: a possible match, said so and never merged


# ============================ features ============================
def _mel(hz):
    return 1127.0 * np.log(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


def mel_banks() -> np.ndarray:
    """(80, 257) triangular filters over the FFT bins, as Kaldi builds them (mel scale, 20 Hz to Nyquist)."""
    bins = FFT // 2
    width = SAMPLE_RATE / FFT
    low, high = _mel(LOW_HZ), _mel(SAMPLE_RATE / 2.0)
    delta = (high - low) / (MELS + 1)
    b = np.arange(MELS)[:, None]
    left, centre, right = low + b * delta, low + (b + 1) * delta, low + (b + 2) * delta
    mel = _mel(width * np.arange(bins))[None, :]
    up, down = (mel - left) / (centre - left), (right - mel) / (right - centre)
    banks = np.maximum(0.0, np.minimum(up, down))
    return np.pad(banks, ((0, 0), (0, 1)))


_BANKS: Optional[np.ndarray] = None


def fbank(samples: np.ndarray) -> np.ndarray:
    """Log-mel features, (frames, 80), for mono float audio in -1..1 at 16 kHz; each band has its mean removed over the clip."""
    global _BANKS
    if _BANKS is None:
        _BANKS = mel_banks()
    x = np.asarray(samples, dtype=np.float64) * 32768.0
    if len(x) < FRAME:
        return np.zeros((0, MELS), dtype=np.float32)
    n = 1 + (len(x) - FRAME) // SHIFT
    idx = np.arange(FRAME)[None, :] + SHIFT * np.arange(n)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)
    shifted = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = frames - PREEMPH * shifted
    frames = frames * np.hamming(FRAME)[None, :]
    power = np.abs(np.fft.rfft(frames, n=FFT, axis=1)) ** 2
    feats = np.log(np.maximum(power @ _BANKS.T, np.finfo(np.float32).eps))
    return (feats - feats.mean(axis=0, keepdims=True)).astype(np.float32)


# ============================ embedding ============================
def embed(session: Any, samples: np.ndarray) -> Optional[np.ndarray]:
    """The voiceprint of a clip of speech, scaled to length 1, or None when it is too short to use."""
    if len(samples) < MIN_SECONDS * SAMPLE_RATE:
        return None
    feats = fbank(samples)
    if len(feats) < 50:
        return None
    vec = np.asarray(session.run(None, {session.get_inputs()[0].name: feats[None]})[0], dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 else None


def read_wav16k(path: str) -> np.ndarray:
    """A 16 kHz mono 16-bit wav file as floats in -1..1. Raises RuntimeError for anything else."""
    try:
        with wave.open(path, "rb") as w:
            if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise RuntimeError("expected 16 kHz mono 16-bit audio")
            raw = w.readframes(w.getnframes())
    except (wave.Error, OSError, EOFError) as exc:
        raise RuntimeError(f"could not read the audio: {exc}")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


# ============================ who spoke when ============================
def segments_from_words(words: Sequence[Any], gap: float = GAP_SECONDS, longest: float = SEGMENT_MAX_SECONDS) -> List[Dict[str, Any]]:
    """Stretches of continuous speech from word times: words close together, cut at ``longest`` seconds. Each lists its word indices."""
    segs: List[Dict[str, Any]] = []
    for i, w in enumerate(words):
        a, b = float(w.startSec), float(w.endSec)
        if segs and a - segs[-1]["end"] <= gap and b - segs[-1]["start"] <= longest:
            segs[-1]["end"] = max(segs[-1]["end"], b)
            segs[-1]["words"].append(i)
        else:
            segs.append({"start": a, "end": b, "words": [i]})
    return segs


def cluster(vectors: Sequence[np.ndarray], threshold: float = IN_FILE, max_clusters: int = 8) -> List[int]:
    """Group voiceprints by who they sound like (average linkage on cosine): a label per vector, 0 for the first speaker heard, 1 for the next.

    Groups merge while the closest pair is at least ``threshold`` alike, and are never more than ``max_clusters``.
    """
    n = len(vectors)
    if n == 0:
        return []
    groups: List[List[int]] = [[i] for i in range(n)]
    sim = np.stack(vectors) @ np.stack(vectors).T

    def link(a: List[int], b: List[int]) -> float:
        return float(sim[np.ix_(a, b)].mean())

    while len(groups) > 1:
        best, pair = -2.0, (0, 1)
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                s = link(groups[i], groups[j])
                if s > best:
                    best, pair = s, (i, j)
        if best < threshold and len(groups) <= max_clusters:
            break
        i, j = pair
        groups[i] = groups[i] + groups[j]
        del groups[j]
    order = sorted(range(len(groups)), key=lambda g: min(groups[g]))
    labels = [0] * n
    for rank, g in enumerate(order):
        for k in groups[g]:
            labels[k] = rank
    return labels


def speakers_of(session: Any, samples: np.ndarray, words: Sequence[Any], *, threshold: float = IN_FILE, max_speakers: int = 8,
                should_cancel: Any = None) -> Dict[str, Any]:
    """Who says each word: ``{"labels": [speaker index per word], "speakers": [{"index", "seconds", "vector"}], "unused": words too short to place}``.

    Stretches of speech of a second or more are embedded and grouped; a word in a shorter stretch takes the speaker of the nearest
    stretch in time.
    """
    segs = segments_from_words(words)
    vecs, used = [], []
    for s in segs:
        if should_cancel and should_cancel():
            raise InterruptedError("voice scan cancelled")
        a, b = int(max(0.0, s["start"]) * SAMPLE_RATE), int(s["end"] * SAMPLE_RATE)
        v = embed(session, samples[a:b])
        if v is not None:
            vecs.append(v)
            used.append(s)
    labels_by_seg = cluster(vecs, threshold, max_speakers)
    if not used:
        return {"labels": [0] * len(words), "speakers": [], "unused": len(words)}
    labels = [0] * len(words)
    placed = 0
    for s in segs:
        if s in used:
            lab = labels_by_seg[used.index(s)]
        else:
            centre = (s["start"] + s["end"]) / 2.0
            near = min(range(len(used)), key=lambda k: abs((used[k]["start"] + used[k]["end"]) / 2.0 - centre))
            lab = labels_by_seg[near]
        for i in s["words"]:
            labels[i] = lab
        placed += len(s["words"]) if s in used else 0
    speakers = []
    for k in range(max(labels_by_seg) + 1):
        mine = [v for v, lab in zip(vecs, labels_by_seg) if lab == k]
        secs = sum(u["end"] - u["start"] for u, lab in zip(used, labels_by_seg) if lab == k)
        mean = np.mean(mine, axis=0)
        speakers.append({"index": k, "seconds": round(float(secs), 2), "vector": mean / (np.linalg.norm(mean) or 1.0)})
    return {"labels": labels, "speakers": speakers, "unused": len(words) - placed}


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))


def check_dims(vec: Sequence[float]) -> Tuple[bool, str]:
    return (len(vec) == EMBED_DIMS, f"expected {EMBED_DIMS} numbers")
