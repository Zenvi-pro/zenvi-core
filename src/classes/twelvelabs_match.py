"""Index-block readers for TwelveLabs-indexed footage."""

from __future__ import annotations

from typing import Any, Dict


def twelvelabs_is_indexed(meta: Any) -> bool:
    """True when index metadata shows a completed index for this file.

    Accepts either the provider-neutral ``index`` block or legacy ``twelvelabs``.
    """
    if not isinstance(meta, dict):
        return False
    # If caller passed full ai_metadata, prefer nested blocks.
    if "index" in meta or "twelvelabs" in meta or "provider" in meta:
        block = meta.get("index") if isinstance(meta.get("index"), dict) else None
        if not block:
            block = meta.get("twelvelabs") if isinstance(meta.get("twelvelabs"), dict) else meta
    else:
        block = meta
    if not isinstance(block, dict):
        return False
    status = str(block.get("status") or "").lower()
    return (
        status == "ready"
        and bool(str(block.get("index_id") or "").strip())
        and bool(str(block.get("video_id") or "").strip())
    )


def get_index_block(ai_metadata: Any) -> Dict[str, Any]:
    """Return the index block from ai_metadata (index preferred, twelvelabs fallback)."""
    if not isinstance(ai_metadata, dict):
        return {}
    if isinstance(ai_metadata.get("index"), dict) and ai_metadata.get("index"):
        return dict(ai_metadata["index"])
    if isinstance(ai_metadata.get("twelvelabs"), dict):
        return dict(ai_metadata["twelvelabs"])
    return {}
