"""Resolve timeline clips from tags, metadata, and natural-language queries."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from classes.logger import log

MIN_CONFIDENCE = 0.15
AMBIGUITY_GAP = 0.08


@dataclass
class ClipCandidate:
    timeline_clip_id: str
    title: str
    layer: Any
    position: float
    file_id: str
    file_name: str
    score: float


@dataclass
class ResolveResult:
    ok: bool
    clip: Any = None
    window: Any = None
    error: str = ""
    candidates: List[ClipCandidate] = field(default_factory=list)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())


def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _normalize_text(text)) if len(t) > 1]


def _clip_title(clip_data: dict) -> str:
    return str(clip_data.get("title") or clip_data.get("label") or "").strip()


def _file_display_name(file_data: dict) -> str:
    return str(
        file_data.get("name")
        or os.path.basename(str(file_data.get("path") or ""))
        or ""
    ).strip()


def _metadata_corpus(ai: dict, *, max_scenes: int = 8) -> str:
    if not isinstance(ai, dict):
        return ""
    parts: List[str] = []
    tags = ai.get("tags") if isinstance(ai.get("tags"), dict) else {}
    for key in ("objects", "scenes", "activities", "mood"):
        vals = tags.get(key)
        if isinstance(vals, list):
            parts.extend(str(v) for v in vals if v)
    desc = ai.get("description")
    if desc:
        parts.append(str(desc))
    for sc in (ai.get("scene_descriptions") or [])[:max_scenes]:
        if isinstance(sc, dict) and sc.get("description"):
            parts.append(str(sc["description"]))
    return " ".join(parts)


def _score_clip_against_query(
    clip_data: dict,
    file_data: Optional[dict],
    ai: Optional[dict],
    query: str,
    *,
    prefer_track: str = "",
    prefer_position_near: float = 0.0,
) -> float:
    q_norm = _normalize_text(query)
    if not q_norm:
        return 0.0

    q_tokens = set(_tokenize(q_norm))
    corpus_parts = [
        _clip_title(clip_data),
        str(clip_data.get("file_id") or ""),
    ]
    if file_data:
        corpus_parts.append(_file_display_name(file_data))
    if ai:
        corpus_parts.append(_metadata_corpus(ai))

    corpus = _normalize_text(" ".join(corpus_parts))
    if not corpus:
        return 0.0

    score = 0.0
    if q_norm in corpus:
        score += 0.45
    overlap = len(q_tokens & set(_tokenize(corpus)))
    if q_tokens:
        score += 0.55 * (overlap / len(q_tokens))

    if prefer_track:
        layer = str(clip_data.get("layer", ""))
        pt = _normalize_text(prefer_track)
        if pt and (pt == layer or pt in layer):
            score += 0.1

    if prefer_position_near > 0:
        pos = float(clip_data.get("position", 0.0) or 0.0)
        end = pos + max(
            0.0,
            float(clip_data.get("end", 0.0) or 0.0)
            - float(clip_data.get("start", 0.0) or 0.0),
        )
        if pos <= prefer_position_near <= end:
            score += 0.12
        else:
            dist = min(abs(prefer_position_near - pos), abs(prefer_position_near - end))
            score += max(0.0, 0.08 - dist * 0.01)

    return min(score, 1.0)


def _enumerate_timeline_clips():
    from classes.query import Clip, File

    clips = Clip.filter() or []
    rows: List[Tuple[Any, dict, Optional[dict], Optional[dict]]] = []
    for clip_obj in clips:
        data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        fid = str(data.get("file_id") or "")
        file_data = None
        ai = None
        if fid:
            fobj = File.get(id=fid)
            if fobj and isinstance(fobj.data, dict):
                file_data = fobj.data
                ai_raw = file_data.get("ai_metadata")
                ai = ai_raw if isinstance(ai_raw, dict) else None
        rows.append((clip_obj, data, file_data, ai))
    return rows


def _candidate_from_row(
    clip_obj: Any,
    data: dict,
    file_data: Optional[dict],
    score: float,
) -> ClipCandidate:
    fid = str(data.get("file_id") or "")
    fname = _file_display_name(file_data) if file_data else ""
    return ClipCandidate(
        timeline_clip_id=str(clip_obj.id or data.get("id", "")),
        title=_clip_title(data) or fname or "Clip",
        layer=data.get("layer", ""),
        position=float(data.get("position", 0.0) or 0.0),
        file_id=fid,
        file_name=fname,
        score=score,
    )


def _format_candidates_error(candidates: List[ClipCandidate], prefix: str) -> str:
    lines = [prefix]
    for i, c in enumerate(candidates[:3], 1):
        lines.append(
            f"  {i}. timeline_clip_id={c.timeline_clip_id} title={c.title!r} "
            f"track={c.layer} file={c.file_name!r} score={c.score:.2f}"
        )
    lines.append("Pass timeline_clip_id from list_clips_tool or refine clip_query.")
    return "\n".join(lines)


def _playhead_position() -> float:
    try:
        from classes.app import get_app
        win = get_app().window
        tl = getattr(win, "timeline", None)
        if tl is not None:
            return float(getattr(tl, "playheadPosition", 0.0) or 0.0)
    except Exception:
        pass
    return 0.0


def resolve_timeline_clip(
    *,
    timeline_clip_id: str = "",
    clip_query: str = "",
    prefer_track: str = "",
    prefer_position_near: Optional[float] = None,
) -> ResolveResult:
    """Resolve a timeline clip by explicit id, tag/query scoring, or single-clip shortcut."""
    from classes.query import Clip

    try:
        from classes.app import get_app
        win = get_app().window
    except Exception:
        win = None

    if timeline_clip_id and str(timeline_clip_id).strip():
        clip_obj = Clip.get(id=str(timeline_clip_id).strip())
        if clip_obj:
            return ResolveResult(ok=True, clip=clip_obj, window=win)
        return ResolveResult(
            ok=False,
            window=win,
            error=f"Error: No timeline clip with id={timeline_clip_id!r}.",
        )

    rows = _enumerate_timeline_clips()
    if not rows:
        return ResolveResult(
            ok=False,
            window=win,
            error="Error: Timeline is empty — add clips before using this tool.",
        )

    pos_near = (
        float(prefer_position_near)
        if prefer_position_near is not None
        else _playhead_position()
    )

    q = (clip_query or "").strip()
    if q:
        scored: List[ClipCandidate] = []
        for clip_obj, data, file_data, ai in rows:
            s = _score_clip_against_query(
                data,
                file_data,
                ai,
                q,
                prefer_track=prefer_track,
                prefer_position_near=pos_near,
            )
            if s > 0:
                scored.append(_candidate_from_row(clip_obj, data, file_data, s))
        scored.sort(key=lambda c: (-c.score, c.position))
        if scored:
            best = scored[0]
            if best.score >= MIN_CONFIDENCE:
                if len(scored) > 1:
                    second = scored[1]
                    same_file = (
                        best.file_id
                        and best.file_id == second.file_id
                    )
                    score_gap = best.score - second.score
                    # Only trigger same_file ambiguity if no track hint was provided
                    if (same_file and not prefer_track) or score_gap < AMBIGUITY_GAP:
                        prefix = (
                            f"Same source file appears on multiple tracks for clip_query {q!r} — "
                            "pass timeline_clip_id or include track in clip_query:"
                            if same_file
                            else f"Ambiguous clip_query {q!r} — multiple matches:"
                        )
                        return ResolveResult(
                            ok=False,
                            window=win,
                            candidates=scored[:3],
                            error=_format_candidates_error(scored, prefix),
                        )
                clip_obj = Clip.get(id=best.timeline_clip_id)
                if clip_obj:
                    log.info(
                        "clip_resolver: query=%r -> clip_id=%s score=%.2f",
                        q,
                        best.timeline_clip_id,
                        best.score,
                    )
                    return ResolveResult(ok=True, clip=clip_obj, window=win, candidates=scored[:3])
            return ResolveResult(
                ok=False,
                window=win,
                candidates=scored[:3],
                error=_format_candidates_error(
                    scored if scored else [],
                    f"No confident match for clip_query {q!r}.",
                ) if scored else (
                    f"Error: No timeline clip matched clip_query {q!r}. "
                    "Use list_clips_tool or get_clips_with_full_metadata_tool."
                ),
            )

    playhead_hits = []
    for clip_obj, data, _file_data, _ai in rows:
        pos = float(data.get("position", 0.0) or 0.0)
        dur = max(
            0.0,
            float(data.get("end", 0.0) or 0.0) - float(data.get("start", 0.0) or 0.0),
        )
        end = pos + dur
        if pos <= pos_near <= end:
            playhead_hits.append(clip_obj)
    if len(playhead_hits) == 1:
        clip_obj = playhead_hits[0]
        log.info("clip_resolver: playhead clip id=%s", clip_obj.id)
        return ResolveResult(ok=True, clip=clip_obj, window=win)

    if len(rows) == 1:
        clip_obj = rows[0][0]
        log.info("clip_resolver: single clip shortcut id=%s", clip_obj.id)
        return ResolveResult(ok=True, clip=clip_obj, window=win)

    return ResolveResult(
        ok=False,
        window=win,
        error=(
            "Error: Multiple clips on timeline — pass clip_query (filename, subject, track) "
            "or timeline_clip_id from list_clips_tool."
        ),
    )


@dataclass
class PairResolveResult:
    ok: bool
    clip_a: Any = None
    clip_b: Any = None
    window: Any = None
    error: str = ""


def resolve_clip_pair(
    *,
    clip_a_id: str = "",
    clip_b_id: str = "",
    clip_a_query: str = "",
    clip_b_query: str = "",
) -> PairResolveResult:
    """Resolve two adjacent timeline clips for transitions."""
    if clip_a_id and clip_b_id:
        from classes.query import Clip
        try:
            from classes.app import get_app
            win = get_app().window
        except Exception:
            win = None
        a = Clip.get(id=str(clip_a_id))
        b = Clip.get(id=str(clip_b_id))
        if a and b:
            return PairResolveResult(ok=True, clip_a=a, clip_b=b, window=win)
        return PairResolveResult(ok=False, error="Error: Could not find both clips by id.")

    rows = _enumerate_timeline_clips()
    if len(rows) < 2:
        return PairResolveResult(ok=False, error="Error: Need at least two clips on the timeline.")

    def _pos_end(data: dict) -> float:
        pos = float(data.get("position", 0.0) or 0.0)
        dur = max(
            0.0,
            float(data.get("end", 0.0) or 0.0) - float(data.get("start", 0.0) or 0.0),
        )
        return pos + dur

    indexed = []
    for clip_obj, data, file_data, ai in rows:
        indexed.append({
            "clip": clip_obj,
            "data": data,
            "file_data": file_data,
            "ai": ai,
            "pos": float(data.get("position", 0.0) or 0.0),
            "end": _pos_end(data),
            "layer": data.get("layer"),
        })
    indexed.sort(key=lambda x: (x["pos"], str(x["layer"])))

    best_pair = None
    best_score = -1.0
    for i in range(len(indexed) - 1):
        a_row = indexed[i]
        b_row = indexed[i + 1]
        # Add layer constraint check
        if a_row["layer"] != b_row["layer"]:
            continue
        gap = b_row["pos"] - a_row["end"]
        # Reject negative gaps (overlapping clips)
        if gap < 0:
            continue
        if gap > 2.0:
            continue
        # When only one ID is provided, ensure at least one clip matches
        if clip_a_id and not clip_b_id:
            if a_row["clip"].id != clip_a_id and b_row["clip"].id != clip_a_id:
                continue
        elif clip_b_id and not clip_a_id:
            if a_row["clip"].id != clip_b_id and b_row["clip"].id != clip_b_id:
                continue
        sa = _score_clip_against_query(
            a_row["data"], a_row["file_data"], a_row["ai"], clip_a_query or "",
        ) if clip_a_query else 0.5
        sb = _score_clip_against_query(
            b_row["data"], b_row["file_data"], b_row["ai"], clip_b_query or "",
        ) if clip_b_query else 0.5
        if clip_a_query and sa < MIN_CONFIDENCE:
            continue
        if clip_b_query and sb < MIN_CONFIDENCE:
            continue
        pair_score = sa + sb - abs(gap) * 0.02
        if pair_score > best_score:
            best_score = pair_score
            best_pair = (a_row["clip"], b_row["clip"])

    if best_pair:
        try:
            from classes.app import get_app
            win = get_app().window
        except Exception:
            win = None
        return PairResolveResult(
            ok=True, clip_a=best_pair[0], clip_b=best_pair[1], window=win,
        )

    ra = resolve_timeline_clip(timeline_clip_id=clip_a_id, clip_query=clip_a_query)
    rb = resolve_timeline_clip(timeline_clip_id=clip_b_id, clip_query=clip_b_query)
    if ra.ok and rb.ok and ra.clip and rb.clip:
        return PairResolveResult(ok=True, clip_a=ra.clip, clip_b=rb.clip, window=ra.window)

    err_parts = []
    if not ra.ok:
        err_parts.append(ra.error)
    if not rb.ok:
        err_parts.append(rb.error)
    return PairResolveResult(
        ok=False,
        error="\n".join(err_parts) or "Error: Could not resolve transition clip pair.",
    )
