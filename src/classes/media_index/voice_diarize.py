"""
 @file
 @brief Speaker labels from voiceprints, in place of the energy-and-timing baseline, when people identity is on and ready.

 The speech package's diarizer assigns S1, S2, ... to words. Its default groups speech by loudness and time, which cannot tell two
 voices apart. With people identity on and the voice model installed, this engine groups the speech by how each stretch sounds.
 Otherwise the factory hands back the baseline, so with the preference off nothing changes.
"""

from __future__ import annotations

import logging
from typing import Sequence

from classes.speech.cache import Word
from classes.speech.diarize import WindowClusterDiarize, set_diarize_factory
from classes.speech.runtime import CancelToken

log = logging.getLogger("speech.diarize")
MAX_SPEAKERS = 8


class EmbeddingDiarize:
    """Group speech into speakers by voiceprint (``voiceprint.speakers_of``)."""

    def __init__(self, session=None):
        self._session = session

    def label_words(self, wav_path: str, words: Sequence[Word], *, token: CancelToken, max_speakers: int = 2):
        from classes.media_index import people_models as pm, voiceprint as vp
        if not words:
            return list(words), []
        sess = self._session or pm.session("voice")
        samples = vp.read_wav16k(wav_path)
        got = vp.speakers_of(sess, samples, words, max_speakers=max(1, min(int(max_speakers or MAX_SPEAKERS), MAX_SPEAKERS)), should_cancel=lambda: token.is_cancelled)
        warnings = []
        if got["unused"] and got["unused"] > len(words) * 0.5:
            warnings.append("Most of the speech was too short or too mixed to tell voices apart: labels are best-effort.")
        out = [Word(w.text, w.startSec, w.endSec, w.confidence, f"S{lab + 1}") for w, lab in zip(words, got["labels"])]
        return out, warnings


def ready() -> bool:
    try:
        from classes.media_index import people_models as pm
        from classes.media_index.flags import people_enabled
        return bool(people_enabled() and pm.runtime_available() and pm.model_path("voice"))
    except Exception:
        return False


def factory():
    """The engine to use now: voiceprints when ready, the baseline otherwise (so it is safe to leave installed)."""
    if ready():
        return EmbeddingDiarize()
    return WindowClusterDiarize()


def install() -> None:
    set_diarize_factory(factory)


__all__ = ["EmbeddingDiarize", "factory", "install", "ready"]
