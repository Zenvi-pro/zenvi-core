"""Pure helpers for MG transparent/opaque placement + layout regions."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

AVOID_RE = re.compile(
    r"\b(close[- ]?up|closeup|face|faces|talking[- ]?head|portrait|hero action|"
    r"fight|impact|explosion|interview|dialogue)\b",
    re.I,
)
PREFER_RE = re.compile(
    r"\b(wide|establishing|b[- ]?roll|landscape|exterior|insert|cutaway|empty|"
    r"drone|aerial|crowd wide|skyline|cityscape)\b",
    re.I,
)

AVOID_QUERY = "close-up face talking head portrait interview dialogue"
PREFER_QUERY = "wide establishing b-roll landscape exterior cutaway"

_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)


def tokenize(text: str) -> set:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 2}


def lexical_similarity(a: str, b: str) -> float:
    """Cheap stand-in when vector embeddings are unavailable (Jaccard on tokens)."""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return float(inter) / float(union) if union else 0.0


def score_scene_blob(blob: str) -> Tuple[int, bool, bool]:
    """Regex avoid/prefer + lexical similarity to avoid/prefer query phrases."""
    text = blob or ""
    avoid = bool(AVOID_RE.search(text))
    prefer = bool(PREFER_RE.search(text))
    score = 0
    if prefer:
        score += 3
    if avoid:
        score -= 4
    # Embedding-lite: boost/penalize by query phrase overlap
    avoid_sim = lexical_similarity(text, AVOID_QUERY)
    prefer_sim = lexical_similarity(text, PREFER_QUERY)
    if prefer_sim >= 0.12:
        score += 2
        prefer = True
    if avoid_sim >= 0.12:
        score -= 3
        avoid = True
    return score, avoid, prefer


def layout_region_for(
    *,
    avoid: bool,
    prefer: bool,
    is_gap: bool = False,
    score: int = 0,
) -> str:
    """Pick HTML layout region for non-blocking / cut-in placement."""
    if is_gap:
        return "mid_plate"
    if avoid and not prefer:
        return "lower_third"
    if avoid and prefer:
        return "corner_br"
    if prefer and score >= 2:
        return "full_frame"
    if prefer:
        return "corner_tr"
    return "lower_third"


def primary_track_overlaps(
    clips: Sequence[Dict[str, Any]],
    *,
    layer: int,
    t0: float,
    t1: float,
) -> bool:
    """True if any clip on layer overlaps [t0, t1)."""
    for c in clips:
        if int(c.get("layer", -1)) != int(layer):
            continue
        pos = float(c.get("position", 0) or 0)
        start = float(c.get("start", 0) or 0)
        end = float(c.get("end", start) or start)
        dur = max(0.0, end - start)
        c1 = pos + dur
        if pos < t1 and c1 > t0:
            return True
    return False


def ripple_positions(
    clips: Sequence[Dict[str, Any]],
    *,
    layer: int,
    t: float,
    delta: float,
) -> List[Tuple[str, float]]:
    """Return (clip_id, new_position) for clips on layer with position >= t."""
    out: List[Tuple[str, float]] = []
    for c in clips:
        if int(c.get("layer", -1)) != int(layer):
            continue
        cid = str(c.get("id") or "")
        if not cid:
            continue
        pos = float(c.get("position", 0) or 0)
        if pos + 1e-6 >= float(t):
            out.append((cid, pos + float(delta)))
    out.sort(key=lambda x: -x[1])  # shift rightmost first to avoid transient overlaps
    return out


def file_looks_transparent(file_data: Optional[dict]) -> bool:
    """Detect HyperFrames transparent overlay imports from file metadata/tags."""
    data = file_data or {}
    ai = data.get("ai_metadata") if isinstance(data.get("ai_metadata"), dict) else {}
    if ai.get("transparent") is True:
        return True
    if str(ai.get("transparent") or "").lower() in ("true", "1", "yes"):
        return True
    tags = data.get("tags") or ai.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    for t in tags:
        if "transparent" in str(t).lower():
            return True
    path = str(data.get("path") or "").lower()
    if path.endswith(".webm"):
        return True
    return False


def apply_embedding_time_boosts(
    windows: List[Dict[str, Any]],
    boosts: Iterable[Dict[str, Any]],
    *,
    radius_s: float = 1.5,
) -> None:
    """Mutate windows in place with nearby embedding hit boosts.

    Each boost: {t, score_delta, avoid?, prefer?}.
    """
    boost_list = list(boosts or [])
    if not boost_list:
        return
    for w in windows:
        wt = float(w.get("t") or 0)
        for b in boost_list:
            bt = float(b.get("t") or 0)
            if abs(wt - bt) <= radius_s:
                w["score"] = int(w.get("score") or 0) + int(b.get("score_delta") or 0)
                if b.get("avoid"):
                    w["avoid"] = True
                if b.get("prefer"):
                    w["prefer"] = True
