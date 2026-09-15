"""TwelveLabs search hit helpers — preserve API relevance order."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def clip_overlap(
    seg_start: float,
    seg_end: float,
    clip_start: float,
    clip_end: float,
) -> Tuple[float, float, float]:
    """Return (overlap_start, overlap_end, overlap_ratio) for segment vs clip window."""
    if seg_end < clip_start or seg_start > clip_end:
        return (0.0, 0.0, 0.0)
    overlap_start = max(seg_start, clip_start)
    overlap_end = min(seg_end, clip_end)
    overlap_dur = max(0.0, overlap_end - overlap_start)
    seg_dur = max(seg_end - seg_start, 1e-6)
    return (overlap_start, overlap_end, overlap_dur / seg_dur)


def _hit_rank(hit: Any) -> int:
    rank = hit.get("rank") if isinstance(hit, dict) else getattr(hit, "rank", None)
    if rank is not None:
        try:
            return int(rank)
        except (TypeError, ValueError):
            pass
    return 999_999


def _hit_times(hit: Any) -> Tuple[float, float]:
    if isinstance(hit, dict):
        return (
            float(hit.get("start", 0.0) or 0.0),
            float(hit.get("end", 0.0) or 0.0),
        )
    return (
        float(getattr(hit, "start", 0.0) or 0.0),
        float(getattr(hit, "end", 0.0) or 0.0),
    )


def enrich_hit_for_clip(
    hit: Any,
    *,
    clip_start: float,
    clip_end: float,
) -> Optional[Dict[str, Any]]:
    """Map a raw search hit to clip-overlap metadata, or None if no overlap."""
    seg_start, seg_end = _hit_times(hit)
    overlap_start, overlap_end, overlap_ratio = clip_overlap(
        seg_start, seg_end, clip_start, clip_end
    )
    if overlap_ratio <= 0.0:
        return None
    if isinstance(hit, dict):
        base = dict(hit)
    else:
        base = {
            "video_id": getattr(hit, "video_id", None),
            "start": seg_start,
            "end": seg_end,
            "rank": getattr(hit, "rank", None),
            "score": getattr(hit, "score", 0.0),
            "filename": getattr(hit, "filename", "") or "",
            "transcription": getattr(hit, "transcription", "") or "",
        }
    base.update({
        "overlap_start": overlap_start,
        "overlap_end": overlap_end,
        "overlap_ratio": overlap_ratio,
        "rank": _hit_rank(base),
    })
    return base


def compute_cut_timestamp(
    seg_start: float,
    seg_end: float,
    *,
    clip_start: float = 0.0,
    clip_end: Optional[float] = None,
    mode: str = "start",
) -> float:
    """Map a TwelveLabs segment to a source-file cut point (seconds)."""
    if mode == "end":
        cut = seg_end
        if clip_end is not None:
            cut = min(cut, clip_end)
        return cut
    if mode == "mid":
        cut = (seg_start + seg_end) / 2.0
        if clip_end is not None:
            cut = min(max(cut, clip_start), clip_end)
        return cut
    return max(seg_start, clip_start)


def select_twelvelabs_match(
    hits: List[Any],
    *,
    clip_start: float,
    clip_end: float,
    occurrence: int = 0,
    cut_mode: str = "start",
) -> Optional[Dict[str, Any]]:
    """Pick the best overlapping hit in TwelveLabs API order (rank 1 first).

    TwelveLabs already ranks results by relevance — do not re-sort.
    """
    overlapping: List[Dict[str, Any]] = []
    for hit in hits:
        enriched = enrich_hit_for_clip(hit, clip_start=clip_start, clip_end=clip_end)
        if enriched:
            overlapping.append(enriched)

    if not overlapping:
        return None

    # Validate occurrence index is within range
    if occurrence > 0:
        idx = occurrence - 1
        if idx >= len(overlapping):
            return None
    else:
        idx = 0
    chosen = dict(overlapping[idx])
    chosen["cut_source"] = compute_cut_timestamp(
        chosen["start"],
        chosen["end"],
        clip_start=clip_start,
        clip_end=clip_end,
        mode=cut_mode,
    )
    return chosen


def select_hits_for_display(
    hits: List[Any],
    *,
    clip_start: float,
    clip_end: float,
    occurrence: int = 0,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """Return overlapping hits in TwelveLabs API order."""
    overlapping: List[Dict[str, Any]] = []
    for hit in hits:
        enriched = enrich_hit_for_clip(hit, clip_start=clip_start, clip_end=clip_end)
        if enriched:
            enriched = dict(enriched)
            enriched["cut_source"] = compute_cut_timestamp(
                enriched["start"],
                enriched["end"],
                clip_start=clip_start,
                clip_end=clip_end,
                mode="mid",
            )
            overlapping.append(enriched)

    if not overlapping:
        return []

    if occurrence > 0:
        idx = occurrence - 1
        if idx >= len(overlapping):
            return []
        return [overlapping[idx]]

    return overlapping[:max(1, top_k)]


def snap_source_time_to_frame(source_time: float, fps_num: float, fps_den: float) -> float:
    """Snap a source-media timestamp to the nearest frame boundary."""
    if fps_num <= 0 or fps_den <= 0:
        return float(source_time)
    return float(
        round((float(source_time) * fps_num) / fps_den) * fps_den
    ) / fps_num


def snap_timeline_position(timeline_pos: float, fps_num: float, fps_den: float) -> float:
    """Snap a timeline position using the same formula as Slice_Triggered."""
    if fps_num <= 0 or fps_den <= 0:
        return float(timeline_pos)
    return float(
        round((float(timeline_pos) * fps_num) / fps_den) * fps_den
    ) / fps_num


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
