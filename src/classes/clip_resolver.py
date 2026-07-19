"""Resolve timeline clips from tags, metadata, and natural-language queries."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from classes.logger import log
from classes.timeline_clip_context import TimelineClipContext, enumerate_timeline_contexts
from classes.track_display import normalize_track_or_layer_arg

MIN_CONFIDENCE = 0.15
AMBIGUITY_GAP = 0.08
AUDIO_HEAVY_MIN_CONFIDENCE = 0.08


def _effective_min_confidence(contexts: List[TimelineClipContext]) -> float:
    try:
        from classes.tl_search_strategy import infer_tl_search_hint
        for ctx in contexts:
            ai = ctx.effective_metadata or {}
            if infer_tl_search_hint(ai, ctx.file_name or "") == "prefer_audio_and_visual":
                return AUDIO_HEAVY_MIN_CONFIDENCE
    except Exception:
        pass
    return MIN_CONFIDENCE


@dataclass
class ClipCandidate:
    timeline_clip_id: str
    title: str
    layer: Any
    position: float
    file_id: str
    file_name: str
    score: float
    parent_file_id: str = ""
    source_start: float = 0.0
    source_end: float = 0.0
    ui_track: Optional[int] = None
    tags_preview: str = ""


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


def _clip_timeline_duration(data: dict, file_data: Optional[dict] = None) -> float:
    """Timeline-visible duration for a clip (handles end=0 / full-file trims)."""
    from classes.ai_metadata_utils import get_source_window

    start, end = get_source_window(data, file_data)
    return max(0.01, end - start)


def _fmt_position_mmss(pos: float) -> str:
    m = int(pos // 60)
    s = int(pos % 60)
    return f"{m}:{s:02d}"


def _score_context_against_query(
    ctx: TimelineClipContext,
    query: str,
    *,
    prefer_position_near: float = 0.0,
) -> float:
    q_norm = _normalize_text(query)
    if not q_norm:
        return 0.0

    ai = ctx.effective_metadata
    q_tokens = set(_tokenize(q_norm))
    corpus_parts = [
        ctx.title,
        ctx.file_name,
        ctx.tags_preview,
        str(ctx.file_id or ""),
        os.path.basename(ctx.source_path or ""),
    ]
    if ctx.ui_track is not None:
        corpus_parts.append(f"track {ctx.ui_track}")
    corpus_parts.append(_fmt_position_mmss(ctx.timeline_position))
    if ai:
        corpus_parts.append(_metadata_corpus(ai))

    corpus = _normalize_text(" ".join(p for p in corpus_parts if p))
    if not corpus:
        return 0.0

    if ai and ai.get("analyzed"):
        scenes = ai.get("scene_descriptions") or []
        tags = ai.get("tags") if isinstance(ai.get("tags"), dict) else {}
        tag_vals = []
        for key in ("objects", "scenes", "activities"):
            tag_vals.extend(tags.get(key) or [])
        if not scenes and not tag_vals and not any(t in corpus for t in q_tokens):
            return 0.0

    score = 0.0
    if q_norm in corpus:
        score += 0.45
    overlap = len(q_tokens & set(_tokenize(corpus)))
    if q_tokens:
        score += 0.55 * (overlap / len(q_tokens))

    if prefer_position_near > 0:
        if ctx.timeline_position <= prefer_position_near <= ctx.timeline_end:
            score += 0.15
        else:
            dist = min(
                abs(prefer_position_near - ctx.timeline_position),
                abs(prefer_position_near - ctx.timeline_end),
            )
            score += max(0.0, 0.08 - dist * 0.01)

    return min(score, 1.0)


def _twelvelabs_project_candidates(
    query: str,
    contexts: List[TimelineClipContext],
) -> List[ClipCandidate]:
    """Rank timeline placements via project-wide TwelveLabs search when tags fail."""
    try:
        from classes.api_client import get_backend_client
        from classes.project_tl_index import collect_project_twelvelabs_index

        client = get_backend_client()
        if not client.is_indexing_configured():
            return []

        # Prefer the project's shared index_id (zenvi-{project_id}), never a global default.
        info = collect_project_twelvelabs_index()
        index_id = str(info.get("index_id") or "").strip()
        if not index_id:
            # Fallback: majority vote from timeline contexts already loaded
            from collections import Counter

            index_ids = []
            for ctx in contexts:
                ai = ctx.effective_metadata or {}
                tl = ai.get("twelvelabs") if isinstance(ai.get("twelvelabs"), dict) else {}
                if str(tl.get("status") or "").lower() != "ready":
                    continue
                iid = str(tl.get("index_id") or "").strip()
                if iid:
                    index_ids.append(iid)
            if not index_ids:
                return []
            index_id = Counter(index_ids).most_common(1)[0][0]

        resp = client.search(query, top_k=10, page_limit=30, index_id=index_id)
        if resp.get("error"):
            log.debug("twelvelabs project search error: %s", resp.get("error"))
            return []
        items = resp.get("results") or resp.get("items") or []
        if not items:
            return []
    except Exception as exc:
        log.debug("twelvelabs project search failed: %s", exc)
        return []

    vid_to_ctxs: dict = {}
    for ctx in contexts:
        ai = ctx.effective_metadata or {}
        tl = ai.get("twelvelabs") if isinstance(ai.get("twelvelabs"), dict) else {}
        if str(tl.get("status") or "").lower() != "ready":
            continue
        vid = str(tl.get("video_id") or "").strip()
        if vid:
            vid_to_ctxs.setdefault(vid, []).append(ctx)

    out: List[ClipCandidate] = []
    seen_ids: set = set()
    for rank, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        vid = str(
            item.get("video_id") or item.get("twelvelabs_video_id") or ""
        ).strip()
        for ctx in vid_to_ctxs.get(vid, []):
            cid = ctx.timeline_clip_id
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            score = 0.35 + max(0.0, (10 - rank) * 0.04)
            out.append(
                ClipCandidate(
                    timeline_clip_id=cid,
                    title=ctx.title,
                    layer=ctx.layer,
                    position=ctx.timeline_position,
                    file_id=ctx.file_id,
                    file_name=ctx.file_name,
                    score=score,
                    parent_file_id=ctx.parent_file_id or ctx.file_id,
                    source_start=ctx.source_start,
                    source_end=ctx.source_end,
                    ui_track=ctx.ui_track,
                    tags_preview=ctx.tags_preview,
                )
            )
    out.sort(key=lambda c: (-c.score, int(c.layer or 0), c.position))
    return out


def _score_clip_against_query(
    clip_data: dict,
    file_data: Optional[dict],
    ai: Optional[dict],
    query: str,
    *,
    prefer_track: str = "",
    prefer_position_near: float = 0.0,
) -> float:
    """Backward-compatible scoring wrapper for unit tests."""
    from classes.ai_metadata_utils import build_tags_preview, get_effective_ai_metadata, get_source_window

    fid = str(clip_data.get("file_id") or "")
    base_file = dict(file_data) if isinstance(file_data, dict) else {}
    if ai and not base_file.get("ai_metadata"):
        base_file["ai_metadata"] = ai
    effective = get_effective_ai_metadata(base_file or None, clip_data=clip_data, rebased=True)
    if not effective and ai:
        effective = ai
    start, end = get_source_window(clip_data, file_data)
    pos = float(clip_data.get("position", 0.0) or 0.0)
    fname = ""
    if file_data:
        fname = str(file_data.get("name") or os.path.basename(str(file_data.get("path") or "")))
    ctx = TimelineClipContext(
        timeline_clip_id=str(clip_data.get("id", "")),
        file_id=fid,
        parent_file_id=fid,
        source_path=str((file_data or {}).get("path") or ""),
        source_start=start,
        source_end=end,
        timeline_position=pos,
        timeline_duration=max(0.01, end - start),
        timeline_end=pos + max(0.01, end - start),
        layer=clip_data.get("layer"),
        ui_track=None,
        title=str(clip_data.get("title") or clip_data.get("label") or fname or "Clip"),
        file_name=fname,
        effective_metadata=effective or {},
        tags_preview=build_tags_preview(effective),
    )
    return _score_context_against_query(ctx, query, prefer_position_near=prefer_position_near)


def _candidate_from_context(ctx: TimelineClipContext) -> ClipCandidate:
    return ClipCandidate(
        timeline_clip_id=ctx.timeline_clip_id,
        title=ctx.title or ctx.file_name or "Clip",
        layer=ctx.layer,
        position=ctx.timeline_position,
        file_id=ctx.file_id,
        file_name=ctx.file_name,
        score=ctx.score,
        parent_file_id=ctx.parent_file_id,
        source_start=ctx.source_start,
        source_end=ctx.source_end,
        ui_track=ctx.ui_track,
        tags_preview=ctx.tags_preview,
    )


def _enumerate_timeline_clips():
    """Legacy tuple rows for tests patching this hook."""
    from classes.query import Clip, File

    rows = []
    for ctx in enumerate_timeline_contexts():
        clip_obj = Clip.get(id=ctx.timeline_clip_id)
        if not clip_obj:
            continue
        data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        file_data = None
        ai = ctx.effective_metadata or None
        if ctx.file_id:
            fobj = File.get(id=ctx.file_id)
            if fobj and isinstance(fobj.data, dict):
                file_data = fobj.data
        rows.append((clip_obj, data, file_data, ai))
    return rows


def _project_layers() -> list:
    try:
        from classes.app import get_app
        return get_app().project.get("layers") or []
    except Exception:
        return []


def _resolve_track_filter(track_arg: str) -> tuple[Optional[int], Optional[str]]:
    if not track_arg:
        return None, None
    return normalize_track_or_layer_arg(track_arg, _project_layers())


def search_timeline_placements(
    query: str,
    *,
    track: str = "",
    prefer_track: str = "",
    position_near: Optional[float] = None,
    occurrence: int = 0,
    parent_file_id: str = "",
    min_confidence: float = MIN_CONFIDENCE,
) -> List[TimelineClipContext]:
    """Rank timeline placements by trim-aware metadata match."""
    track_arg = (track or prefer_track or "").strip()
    layer_filter, track_err = _resolve_track_filter(track_arg)
    if track_err:
        return []

    contexts = enumerate_timeline_contexts(layers=_project_layers())
    pos_near = float(position_near) if position_near is not None else 0.0
    parent_filter = str(parent_file_id or "").strip()

    scored: List[TimelineClipContext] = []
    for ctx in contexts:
        if layer_filter is not None:
            try:
                if int(ctx.layer) != int(layer_filter):
                    continue
            except (TypeError, ValueError):
                continue
        if parent_filter and ctx.parent_file_id != parent_filter and ctx.file_id != parent_filter:
            continue
        if position_near is not None:
            if not (ctx.timeline_position <= pos_near <= ctx.timeline_end):
                continue
        s = _score_context_against_query(ctx, query, prefer_position_near=pos_near)
        if s >= min_confidence:
            ctx.score = s
            scored.append(ctx)

    scored.sort(key=lambda c: (-c.score, int(c.layer or 0), c.timeline_position))

    if occurrence > 0:
        idx = occurrence - 1
        if idx < len(scored):
            return [scored[idx]]
        return []

    return scored


def _format_candidates_error(candidates: List[ClipCandidate], prefix: str) -> str:
    lines = [prefix]
    for i, c in enumerate(candidates[:3], 1):
        track_part = f" ui_track={c.ui_track}" if c.ui_track is not None else ""
        lines.append(
            f"  {i}. timeline_clip_id={c.timeline_clip_id} title={c.title!r} "
            f"track={c.layer}{track_part} file={c.file_name!r} "
            f"source={c.source_start:.1f}-{c.source_end:.1f}s score={c.score:.2f}"
        )
    lines.append(
        "Pass timeline_clip_id from list_clips_tool, or disambiguate with track, "
        "occurrence (1-based), or position_near (timeline seconds)."
    )
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


def _check_ambiguity(
    scored: List[ClipCandidate],
    *,
    track_filter: Optional[int],
    occurrence: int,
    position_near: Optional[float],
) -> Optional[str]:
    if not scored or occurrence > 0 or position_near is not None:
        return None
    best = scored[0]
    same_file_matches = [c for c in scored if c.file_id and c.file_id == best.file_id]
    same_track_dupes = [c for c in same_file_matches if c.layer == best.layer]
    if len(same_track_dupes) > 1:
        return (
            f"Same source file appears {len(same_track_dupes)} times on the same track for "
            f"clip_query — pass occurrence (1-based), position_near (timeline seconds), "
            f"or timeline_clip_id:"
        )
    layers = {c.layer for c in same_file_matches}
    if len(same_file_matches) > 1 and len(layers) > 1 and track_filter is None:
        return (
            f"Same source file appears on multiple tracks for clip_query — "
            "pass track, timeline_clip_id, or position_near:"
        )
    if len(scored) > 1:
        second = scored[1]
        if best.score - second.score < AMBIGUITY_GAP:
            return f"Ambiguous clip_query — multiple close matches:"
    return None


def resolve_timeline_clip(
    *,
    timeline_clip_id: str = "",
    clip_query: str = "",
    prefer_track: str = "",
    track: str = "",
    prefer_position_near: Optional[float] = None,
    position_near: Optional[float] = None,
    occurrence: int = 0,
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

    contexts = enumerate_timeline_contexts(layers=_project_layers())
    if not contexts:
        return ResolveResult(
            ok=False,
            window=win,
            error="Error: Timeline is empty — add clips before using this tool.",
        )

    track_arg = (track or prefer_track or "").strip()
    layer_filter, track_err = _resolve_track_filter(track_arg)
    if track_err:
        return ResolveResult(ok=False, window=win, error=track_err)

    pos_near_val = position_near if position_near is not None else prefer_position_near
    if pos_near_val is None:
        pos_near_val = _playhead_position()
    else:
        pos_near_val = float(pos_near_val)

    try:
        occ = int(occurrence or 0)
    except (TypeError, ValueError):
        occ = 0

    q = (clip_query or "").strip()
    if q:
        placements = search_timeline_placements(
            q,
            track=track_arg,
            position_near=pos_near_val if (position_near is not None or prefer_position_near is not None) else None,
            occurrence=occ,
            min_confidence=0.0,
        )
        scored = [_candidate_from_context(c) for c in placements if c.score > 0 or occ > 0]
        if not scored and placements:
            scored = [_candidate_from_context(c) for c in placements]
        if not scored:
            for ctx in contexts:
                if layer_filter is not None:
                    try:
                        if int(ctx.layer) != int(layer_filter):
                            continue
                    except (TypeError, ValueError):
                        continue
                s = _score_context_against_query(ctx, q, prefer_position_near=pos_near_val)
                if s > 0:
                    ctx.score = s
                    scored.append(_candidate_from_context(ctx))
            scored.sort(key=lambda c: (-c.score, int(c.layer or 0), c.position))

        if scored:
            if occ > 0:
                if len(placements) == 1:
                    best = scored[0]
                elif len(scored) >= occ:
                    ranked = sorted(scored, key=lambda c: (int(c.layer or 0), c.position))
                    best = ranked[occ - 1]
                else:
                    return ResolveResult(
                        ok=False,
                        window=win,
                        candidates=scored[:3],
                        error=f"Error: occurrence={occ} but only {len(scored)} match(es) for {q!r}.",
                    )
            else:
                best = scored[0]
                min_conf = _effective_min_confidence(contexts)
                if best.score < min_conf:
                    tl_scored = _twelvelabs_project_candidates(q, contexts)
                    if tl_scored:
                        best = tl_scored[0]
                        scored = tl_scored
                    else:
                        return ResolveResult(
                            ok=False,
                            window=win,
                            candidates=scored[:3],
                            error=_format_candidates_error(
                                scored, f"No confident match for clip_query {q!r}."
                            ),
                        )
                amb = _check_ambiguity(
                    scored,
                    track_filter=layer_filter,
                    occurrence=occ,
                    position_near=position_near if position_near is not None else prefer_position_near,
                )
                if amb:
                    return ResolveResult(
                        ok=False,
                        window=win,
                        candidates=scored[:3],
                        error=_format_candidates_error(scored, amb),
                    )

            clip_obj = Clip.get(id=best.timeline_clip_id)
            if clip_obj:
                log.info(
                    "clip_resolver: query=%r -> clip_id=%s score=%.2f occ=%s track=%r",
                    q,
                    best.timeline_clip_id,
                    best.score,
                    occ,
                    track_arg,
                )
                return ResolveResult(ok=True, clip=clip_obj, window=win, candidates=scored[:3])

        tl_fallback = _twelvelabs_project_candidates(q, contexts)
        return ResolveResult(
            ok=False,
            window=win,
            candidates=scored[:3] if scored else tl_fallback[:3],
            error=(
                _format_candidates_error(scored, f"No confident match for clip_query {q!r}.")
                if scored
                else (
                    _format_candidates_error(tl_fallback, f"No timeline clip matched clip_query {q!r}.")
                    if tl_fallback
                    else (
                        f"Error: No timeline clip matched clip_query {q!r}. "
                        "Use list_clips_tool or get_timeline_placements_metadata_tool."
                    )
                )
            ),
        )

    playhead_hits = []
    for ctx in contexts:
        if layer_filter is not None:
            try:
                if int(ctx.layer) != int(layer_filter):
                    continue
            except (TypeError, ValueError):
                continue
        if ctx.timeline_position <= pos_near_val <= ctx.timeline_end:
            clip_obj = Clip.get(id=ctx.timeline_clip_id)
            if clip_obj:
                playhead_hits.append(clip_obj)
    if len(playhead_hits) == 1:
        clip_obj = playhead_hits[0]
        log.info("clip_resolver: playhead clip id=%s", clip_obj.id)
        return ResolveResult(ok=True, clip=clip_obj, window=win)

    if len(contexts) == 1:
        clip_obj = Clip.get(id=contexts[0].timeline_clip_id)
        if clip_obj:
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
    allow_gap_seconds: float = 2.0,
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

    contexts = enumerate_timeline_contexts(layers=_project_layers())
    if len(contexts) < 2:
        return PairResolveResult(ok=False, error="Error: Need at least two clips on the timeline.")

    indexed = sorted(contexts, key=lambda c: (c.timeline_position, int(c.layer or 0)))

    best_pair = None
    best_score = -1.0
    for i in range(len(indexed) - 1):
        a_ctx = indexed[i]
        b_ctx = indexed[i + 1]
        if a_ctx.layer != b_ctx.layer:
            continue
        gap = b_ctx.timeline_position - a_ctx.timeline_end
        if gap < 0:
            continue
        if gap > allow_gap_seconds:
            continue
        if clip_a_id and a_ctx.timeline_clip_id != clip_a_id and b_ctx.timeline_clip_id != clip_a_id:
            continue
        if clip_b_id and a_ctx.timeline_clip_id != clip_b_id and b_ctx.timeline_clip_id != clip_b_id:
            continue
        sa = _score_context_against_query(a_ctx, clip_a_query or "") if clip_a_query else 0.5
        sb = _score_context_against_query(b_ctx, clip_b_query or "") if clip_b_query else 0.5
        if clip_a_query and sa < MIN_CONFIDENCE:
            continue
        if clip_b_query and sb < MIN_CONFIDENCE:
            continue
        pair_score = sa + sb - abs(gap) * 0.02
        if (
            a_ctx.parent_file_id
            and a_ctx.parent_file_id == b_ctx.parent_file_id
        ):
            src_gap = abs(b_ctx.source_start - a_ctx.source_end)
            if src_gap < 0.5:
                pair_score += 0.25
        if pair_score > best_score:
            best_score = pair_score
            best_pair = (a_ctx.timeline_clip_id, b_ctx.timeline_clip_id)

    if best_pair:
        from classes.query import Clip
        try:
            from classes.app import get_app
            win = get_app().window
        except Exception:
            win = None
        a = Clip.get(id=best_pair[0])
        b = Clip.get(id=best_pair[1])
        if a and b:
            return PairResolveResult(ok=True, clip_a=a, clip_b=b, window=win)

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
