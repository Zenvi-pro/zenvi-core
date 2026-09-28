"""Speaker diarization — portable clustering over speech windows.

Uses mean-energy + timing clusters as a CPU baseline. Optional WeSpeaker /
pyannote ONNX can replace the engine via ``set_diarize_factory``. Speakers are
labelled ``S1``, ``S2``, … and can be renamed in the Index panel registry.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Protocol, Sequence

from classes.speech.cache import Word
from classes.speech.runtime import CancelToken, inference_slot
from classes.speech.vad import _read_pcm16_mono, detect_speech_windows
from classes.speech.audio_extract import extract_mono_16k_wav

log = logging.getLogger("speech.diarize")


class DiarizeEngine(Protocol):
    def label_words(
        self,
        wav_path: str,
        words: Sequence[Word],
        *,
        token: CancelToken,
        max_speakers: int,
    ) -> tuple[list[Word], list[str]]:
        """Return (words_with_speakerId, warnings)."""
        ...


class WindowClusterDiarize:
    """Assign speakers by clustering speech regions (2-means on time+energy)."""

    def label_words(
        self,
        wav_path: str,
        words: Sequence[Word],
        *,
        token: CancelToken,
        max_speakers: int = 2,
    ) -> tuple[list[Word], list[str]]:
        warnings: list[str] = []
        if not words:
            return list(words), warnings
        samples, rate = _read_pcm16_mono(wav_path)
        # Build per-word energy
        feats: list[tuple[float, float]] = []  # (mid_time, rms)
        for w in words:
            token.raise_if_cancelled()
            i0 = max(0, int(w.startSec * rate))
            i1 = min(len(samples), max(i0 + 1, int(w.endSec * rate)))
            chunk = samples[i0:i1] or [0.0]
            rms = (sum(x * x for x in chunk) / len(chunk)) ** 0.5
            mid = 0.5 * (w.startSec + w.endSec)
            feats.append((mid, rms))

        k = max(1, min(int(max_speakers), 4, len(set(round(t) for t, _ in feats)) or 1))
        if k == 1 or len(words) < 4:
            out = [
                Word(w.text, w.startSec, w.endSec, w.confidence, "S1")
                for w in words
            ]
            return out, warnings

        # Simple 2-means on (normalized time, energy)
        t_max = max(t for t, _ in feats) or 1.0
        e_max = max(e for _, e in feats) or 1e-9
        pts = [(t / t_max, e / e_max) for t, e in feats]
        # Init centroids at quartiles
        cents = [pts[len(pts) // (k + 1) * (i + 1)] for i in range(k)]
        assigns = [0] * len(pts)
        for _ in range(8):
            token.raise_if_cancelled()
            for i, p in enumerate(pts):
                assigns[i] = min(
                    range(k),
                    key=lambda c: (p[0] - cents[c][0]) ** 2 + (p[1] - cents[c][1]) ** 2,
                )
            for c in range(k):
                members = [pts[i] for i, a in enumerate(assigns) if a == c]
                if members:
                    cents[c] = (
                        sum(m[0] for m in members) / len(members),
                        sum(m[1] for m in members) / len(members),
                    )
        # Overlap heuristic: rapid speaker flips
        flips = sum(1 for i in range(1, len(assigns)) if assigns[i] != assigns[i - 1])
        if flips > len(assigns) * 0.45:
            warnings.append(
                "Speaker overlap confidence is low — labels are best-effort."
            )
        out = []
        for w, a in zip(words, assigns):
            out.append(
                Word(w.text, w.startSec, w.endSec, w.confidence, f"S{a + 1}")
            )
        return out, warnings


_factory: Callable[[], DiarizeEngine] = WindowClusterDiarize


def set_diarize_factory(factory: Callable[[], DiarizeEngine]) -> None:
    global _factory
    _factory = factory


def reset_diarize_factory() -> None:
    global _factory
    _factory = WindowClusterDiarize


def diarize_file_words(
    media_path: str,
    words: Sequence[Word],
    *,
    max_speakers: int = 2,
    token: Optional[CancelToken] = None,
) -> tuple[list[Word], list[str]]:
    import os

    with inference_slot(token) as tok:
        wav, err = extract_mono_16k_wav(media_path)
        if err:
            raise RuntimeError(err)
        try:
            return _factory().label_words(
                wav, words, token=tok, max_speakers=max_speakers,
            )
        finally:
            try:
                if os.path.isfile(wav):
                    os.remove(wav)
            except OSError:
                pass


def speaker_turns(words: Sequence[dict]) -> list[dict]:
    """Run-length speaker turns from word rows that carry speakerId."""
    turns: list[dict] = []
    for w in words:
        sid = w.get("speakerId")
        if not sid:
            continue
        if turns and turns[-1]["speakerId"] == sid:
            turns[-1]["endFrame"] = w.get("endFrame", turns[-1]["endFrame"])
            turns[-1]["endSec"] = w.get("endSec", turns[-1]["endSec"])
            turns[-1]["text"] = (turns[-1]["text"] + " " + str(w.get("text") or "")).strip()
        else:
            turns.append({
                "speakerId": sid,
                "startFrame": w.get("startFrame"),
                "endFrame": w.get("endFrame"),
                "startSec": w.get("startSec"),
                "endSec": w.get("endSec"),
                "text": str(w.get("text") or ""),
            })
    return turns
