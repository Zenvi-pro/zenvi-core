"""Per-file indexing status derived from ai_metadata + live indexing progress.

Pure: no Qt, no project data. The media bin badges (list + tree) and the
Scene Descriptions dock both read the same derivation so they cannot disagree.
"""

from __future__ import annotations

from collections import namedtuple
from typing import Any, Dict, Optional

NONE = "none"
PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"

PHASE_LABELS = {
    "uploading": "Uploading for search…",
    "indexing": "Indexing for search…",
    "summarizing": "Generating description…",
    "done": "Indexing complete",
}

_RUNNING_BLOCK_STATUS = ("uploading", "indexing", "processing", "pending", "validating")
_FAILED_BLOCK_STATUS = ("failed", "error")

IndexingStatus = namedtuple("IndexingStatus", "state label tooltip")


def phase_label(phase: Optional[str], percent: Optional[int] = None) -> str:
    """Human phase text, with the upload percentage when we have one."""
    base = PHASE_LABELS.get(phase or "", "Processing…")
    if phase == "uploading" and percent is not None and percent >= 0:
        return f"{base} ({percent}%)"
    return base


def _index_block(ai_metadata: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("index", "twelvelabs"):
        block = ai_metadata.get(key)
        if isinstance(block, dict) and block:
            return block
    return {}


def derive_indexing_status(
    ai_metadata: Optional[dict],
    progress: Optional[dict] = None,
    is_active: bool = False,
) -> IndexingStatus:
    """Map ai_metadata + FilesModel progress onto one badge state."""
    meta = ai_metadata if isinstance(ai_metadata, dict) else {}
    block = _index_block(meta)
    block_status = str(block.get("status") or "").lower()

    phase = (progress or {}).get("phase")
    percent = (progress or {}).get("percent")

    if is_active or (phase and phase != "done") or block_status in _RUNNING_BLOCK_STATUS:
        label = phase_label(phase or block_status or "indexing", percent)
        return IndexingStatus(RUNNING, label, label)

    skip_reason = str(meta.get("skip_reason") or "").strip()
    if skip_reason or block_status == "skipped":
        tooltip = skip_reason or str(block.get("error") or "").strip() or "Indexing skipped"
        return IndexingStatus(PENDING, "Indexing skipped", tooltip)

    error = str(meta.get("error") or "").strip()
    if not error and block_status in _FAILED_BLOCK_STATUS:
        error = str(block.get("error") or "").strip() or "Indexing failed"
    if error:
        return IndexingStatus(FAILED, "Indexing failed", error)

    if meta.get("analyzed"):
        return IndexingStatus(SUCCESS, "Indexed", "Indexed for search")

    from classes.twelvelabs_match import twelvelabs_is_indexed

    if twelvelabs_is_indexed(block):
        return IndexingStatus(SUCCESS, "Indexed", "Indexed for search")

    return IndexingStatus(NONE, "", "")
