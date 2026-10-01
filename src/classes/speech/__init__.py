"""Portable on-device speech / ML (Phase 5).

ASR, VAD, diarize, beats, captions helpers, and visual index. Inference never
runs on the GUI thread; tools that call into this package are BACKGROUND_SAFE.
"""

from classes.speech.cache import TranscriptCache, TranscriptRecord, Word
from classes.speech.word_ranges import (
    compact_fragments_after_remove,
    merge_ranges,
    ranges_for_word_indices,
)

__all__ = [
    "TranscriptCache",
    "TranscriptRecord",
    "Word",
    "compact_fragments_after_remove",
    "merge_ranges",
    "ranges_for_word_indices",
]
