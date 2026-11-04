"""
 @file
 @brief When layers saved under a fingerprint may be reused for a new import of the same footage.

 Pure functions (no Qt, no network) so the rules are tested without a running editor.
 The worker calls ``same_footage`` before it reuses the layers saved for the content.
"""

from __future__ import annotations

from typing import Any

from classes.media_index.store import Shelf

# A fingerprint is the size plus the first and last megabyte, so two different files can
# in theory share one. A saved analysis is only reused when the file also has the duration
# it was made from, which turns that theory into something that cannot happen in practice.
_DURATION_ABS_TOLERANCE = 0.5
_DURATION_REL_TOLERANCE = 0.01


def durations_match(saved: Any, current: Any, media_type: str) -> bool:
    if media_type == "image":
        return True
    try:
        saved, current = float(saved or 0.0), float(current or 0.0)
    except (TypeError, ValueError):
        return False
    if saved <= 0.0 or current <= 0.0:
        return False
    return abs(saved - current) <= max(_DURATION_ABS_TOLERANCE, _DURATION_REL_TOLERANCE * max(saved, current))


def same_footage(shelf: Shelf, fingerprint: Any, *, media_type: str, duration: float) -> bool:
    """False when the layers saved under this fingerprint were made from different footage (another type or length).

    Nothing saved yet counts as the same, so a first analysis is never held back.
    """
    manifest = shelf.read_json(fingerprint, "manifest.json") or {}
    source = manifest.get("source")
    if not isinstance(source, dict) or not source:
        return True
    if str(source.get("media_type") or "video") != str(media_type or "video"):
        return False
    return durations_match(source.get("duration"), duration, str(media_type or "video"))
