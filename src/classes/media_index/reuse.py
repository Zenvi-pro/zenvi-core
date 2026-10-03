"""
 @file
 @brief When a saved analysis may stand in for a new import of the same content.

 Pure functions (no Qt, no network) so the rules are tested without a running editor.
 The worker calls ``reusable_analysis`` before spending anything, and
 ``restored_metadata`` to label what it found for the new file.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

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


def reusable_analysis(shelf: Shelf, fingerprint: Any, *, media_type: str,
                      duration: float) -> Optional[Dict[str, Any]]:
    """The saved analysis for this content, or None when it must be made again."""
    from classes.ai_metadata_utils import is_ai_metadata_usable

    saved = shelf.load_v1_index(fingerprint)
    if not saved:
        return None
    raw_source = saved.get("source")
    source: Dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    kind = str(media_type or "video")
    if str(source.get("media_type") or "video") != kind:
        return None
    if not durations_match(source.get("duration"), duration, kind):
        return None
    analysis = saved["ai_metadata"]
    return analysis if is_ai_metadata_usable(analysis) else None


def restored_metadata(saved: Dict[str, Any], *, index_name: str, file_id: str,
                      media_type: str) -> Dict[str, Any]:
    """A copy of a saved analysis, labelled as indexed under *index_name* for *file_id*."""
    meta = copy.deepcopy(saved)
    for stale in ("error", "skip_reason", "skip_code"):
        meta.pop(stale, None)
    block = {
        "status": "ready",
        "index_id": index_name,
        "index_name": index_name,
        "video_id": file_id,
        "provider": "gemini",
        "media_type": media_type,
        "restored": True,
    }
    meta["index"] = block
    meta["twelvelabs"] = dict(block)
    meta["analyzed"] = True
    meta["media_type"] = media_type
    return meta
