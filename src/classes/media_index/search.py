"""
 @file
 @brief Search a project's footage across every layer, then snap results to real boundaries.

 A text query is embedded once (by the caller) and compared with each file's saved vectors:
 shot descriptions, transcript sentences and pictures, a plain dot product in numpy. The ranked
 lists are fused (reciprocal rank, weighted like the backend's old retrieval: moments and
 speech count a little more than a scene summary) at shot level, filtered by measured facts
 (camera move, shot type, mood, colour, duration, speech) and returned with where in the shot
 the match peaks. Speech matches snap to the sentence's own word edges, everything else to the
 shot's cuts.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from classes import color_agent as ca
from classes.media_index.library import FileIndex

RRF_K = 60
PER_LAYER = 60                    # candidates taken from each ranked list before fusing
WEIGHT = {"shot": 1.35, "speech": 1.30, "image": 1.0, "look": 0.8}
MIN_COSINE = {"shot": 0.30, "speech": 0.30, "image": 0.20}   # a hit below this is noise, not a match

def _num(d: Dict[str, Any], key: str, default: float) -> float:
    value = d.get(key)
    return default if value is None else float(value)


def _saturation(p: Dict[str, Any], e: Dict[str, Any]) -> float:
    return float((e.get("saturation") or {}).get("mean") or p.get("sat_proxy") or 0.0)


LOOK_TESTS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], bool]] = {
    "warm": lambda p, e: _num(p, "warm_cool", 0.0) >= 0.06,
    "cool": lambda p, e: _num(p, "warm_cool", 0.0) <= -0.06,
    "dark": lambda p, e: _num(p, "avg_luma", 0.5) <= 0.30,
    "bright": lambda p, e: _num(p, "avg_luma", 0.5) >= 0.60,
    "saturated": lambda p, e: _saturation(p, e) >= 0.45,
    "muted": lambda p, e: _saturation(p, e) <= 0.20,
    "contrasty": lambda p, e: _num(p, "contrast_span", 0.0) >= 0.55,
    "flat": lambda p, e: _num(p, "contrast_span", 1.0) <= 0.25,
}
LOOK_NAMES = tuple(LOOK_TESTS)


def rrf(lists: Sequence[Tuple[str, Sequence[Any]]], k: int = RRF_K) -> Dict[Any, float]:
    """Weighted reciprocal-rank fusion of ranked key lists: {key: score}."""
    scores: Dict[Any, float] = {}
    for layer, keys in lists:
        weight = WEIGHT.get(layer, 1.0)
        for rank, key in enumerate(keys):
            scores[key] = scores.get(key, 0.0) + weight / (k + rank + 1)
    return scores


def passes_filters(shot: Dict[str, Any], f: Dict[str, Any], fi: FileIndex) -> bool:
    """Measured/described facts a shot must have. Empty or unset filters never exclude."""
    if f.get("exclude_black", True) and shot.get("black"):
        return False
    dur = float(shot["end"]) - float(shot["start"])
    if f.get("min_duration") and dur < float(f["min_duration"]):
        return False
    if f.get("max_duration") and dur > float(f["max_duration"]):
        return False
    if f.get("orientation") and fi.orientation and fi.orientation != f["orientation"]:
        return False
    motion = shot.get("motion") or {}
    if f.get("camera") and motion.get("class") != f["camera"]:
        return False
    watch = shot.get("watch") or {}
    if f.get("shot_type") and (watch.get("shot_type") or "") != str(f["shot_type"]).lower():
        return False
    if f.get("mood") and str(f["mood"]).lower() not in (watch.get("mood") or "").lower():
        return False
    if f.get("object_label"):
        want = str(f["object_label"]).lower()
        if not any(want in (o.get("label") or "").lower() for o in watch.get("objects") or []):
            return False
    if f.get("text_on_screen"):
        want = str(f["text_on_screen"]).lower()
        if not any(want in (o.get("text") or "").lower() for o in watch.get("on_screen_text") or []):
            return False
    if f.get("speech") is True and float((shot.get("speech") or {}).get("speech_ratio") or 0.0) < 0.2:
        return False
    if f.get("speech") is False and float((shot.get("speech") or {}).get("speech_ratio") or 0.0) >= 0.2:
        return False
    if f.get("look"):
        profile = shot.get("look") or {}
        test = LOOK_TESTS.get(str(f["look"]))
        if not profile.get("present") or test is None or not test(profile, shot.get("look_extras") or {}):
            return False
    return True


def _top(sims: np.ndarray, minimum: float, n: int = PER_LAYER) -> List[int]:
    order = np.argsort(-sims)[:n]
    return [int(i) for i in order if sims[i] >= minimum]


def _speech_text(fi: FileIndex, start: float, end: float, limit: int = 160) -> str:
    said = " ".join(s["text"] for s in fi.sentences if s["end"] > start and s["start"] < end)
    return said[:limit]


def search(files: Sequence[FileIndex], *, query_vector: Optional[np.ndarray] = None,
           reference_vector: Optional[np.ndarray] = None, reference_look: Optional[Dict[str, Any]] = None,
           look_for: str = "", filters: Optional[Dict[str, Any]] = None, limit: int = 20, offset: int = 0
           ) -> Dict[str, Any]:
    """Rank shots of *files*. With no query, vector or look reference, returns the shots that pass the filters.

    ``look_for`` is "spoken" (transcript only), "on_screen" (what is seen) or "" (both).
    """
    f = dict(filters or {})
    q = query_vector if query_vector is not None else reference_vector
    peak: Dict[Tuple[str, int], Dict[str, Any]] = {}
    layer_scores: Dict[Tuple[str, int], Dict[str, float]] = {}
    lists: List[Tuple[str, List[Any]]] = []

    def note(key: Tuple[str, int], layer: str, score: float, t: Optional[float] = None, span: Optional[Tuple[float, float]] = None,
             why: str = "") -> None:
        cur = layer_scores.setdefault(key, {})
        if score > cur.get(layer, -1.0):
            cur[layer] = round(float(score), 4)
            info = peak.setdefault(key, {})
            if layer == "speech" or "t" not in info or layer == "shot":
                if t is not None:
                    info["t"] = t
                if span:
                    info["span"] = span
                if why:
                    info["why"] = why

    by_sha = {fi.sha: fi for fi in files}
    if q is not None:
        shot_rank: List[Tuple[Tuple[str, int], float]] = []
        speech_rank: List[Tuple[Tuple[str, int], float]] = []
        image_rank: List[Tuple[Tuple[str, int], float]] = []
        for fi in files:
            if fi.text_matrix is not None:
                sims = fi.text_matrix @ q
                for i in _top(sims, MIN_COSINE["shot"]):
                    row = fi.text_rows[i]
                    if row["kind"] == "shot":
                        if look_for == "spoken":
                            continue
                        key = (fi.sha, int(row["shot"]))
                        shot_rank.append((key, float(sims[i])))
                        note(key, "shot", sims[i], t=(row["start"] + row["end"]) / 2.0, why=(row["text"] or "")[:160])
                    else:
                        if look_for == "on_screen":
                            continue
                        shot = fi.shot_at((row["start"] + row["end"]) / 2.0)
                        if not shot:
                            continue
                        key = (fi.sha, int(shot["id"]))
                        speech_rank.append((key, float(sims[i])))
                        note(key, "speech", sims[i], t=row["start"], span=(row["start"], row["end"]), why=(row["text"] or "")[:160])
            if fi.image_matrix is not None and look_for != "spoken":
                sims = fi.image_matrix @ q
                for i in _top(sims, MIN_COSINE["image"]):
                    row = fi.image_rows[i]
                    key = (fi.sha, int(row["shot"]))
                    image_rank.append((key, float(sims[i])))
                    note(key, "image", sims[i], t=row["t"])
        for layer, rows in (("shot", shot_rank), ("speech", speech_rank), ("image", image_rank)):
            seen: List[Tuple[str, int]] = []
            for key, _ in sorted(rows, key=lambda r: -r[1]):
                if key not in seen:
                    seen.append(key)
            if seen:
                lists.append((layer, seen))
    if reference_look and reference_look.get("present"):
        scored: List[Tuple[Tuple[str, int], float]] = []
        for fi in files:
            for shot in fi.shots:
                p = shot.get("look")
                if p and p.get("present"):
                    d = ca.look_profile_distance(p, reference_look)
                    if d is not None:
                        scored.append(((fi.sha, int(shot["id"])), d))
        order = [k for k, _ in sorted(scored, key=lambda r: r[1])][:PER_LAYER * 2]
        if order:
            lists.append(("look", order))
            for key, d in scored:
                layer_scores.setdefault(key, {})["look_distance"] = round(float(d), 4)

    queried = q is not None or bool(reference_look and reference_look.get("present"))
    ranking_active = bool(lists)
    if ranking_active:
        fused = rrf(lists)
        keys = sorted(fused, key=lambda k: -fused[k])
    elif queried:
        fused = {}
        keys = []          # something was asked for and nothing matched: say so, don't list everything
    else:
        fused = {}
        keys = [(fi.sha, int(s["id"])) for fi in files for s in fi.shots]

    hits: List[Dict[str, Any]] = []
    for key in keys:
        fi = by_sha[key[0]]
        shot = next((s for s in fi.shots if s["id"] == key[1]), None)
        if shot is None or not passes_filters(shot, f, fi):
            continue
        info = peak.get(key, {})
        span = info.get("span")
        start, end = (max(shot["start"], span[0]), min(shot["end"], span[1])) if span and "speech" in layer_scores.get(key, {}) and \
            layer_scores[key].get("speech", 0) >= layer_scores[key].get("shot", 0) else (shot["start"], shot["end"])
        if end <= start:
            start, end = shot["start"], shot["end"]
        watch = shot.get("watch") or {}
        hits.append({
            "file_id": fi.file_id, "name": fi.name, "sha": fi.sha, "shot_id": shot["id"],
            "start": round(start, 3), "end": round(end, 3),
            "snapped_to": "speech" if (start, end) != (shot["start"], shot["end"]) else "shot",
            "peak": round(float(info.get("t", (shot["start"] + shot["end"]) / 2.0)), 3),
            "score": round(float(fused.get(key, 0.0)), 5), "scores": layer_scores.get(key, {}),
            "why": info.get("why") or (watch.get("description") or _speech_text(fi, shot["start"], shot["end"]))[:160],
            "camera": (shot.get("motion") or {}).get("class"), "shot_type": watch.get("shot_type") or None,
            "mood": watch.get("mood") or None,
            "speech_ratio": (shot.get("speech") or {}).get("speech_ratio"),
        })
    total = len(hits)
    page = hits[offset: offset + max(1, limit)]
    return {"hits": page, "total": total, "next": offset + len(page) if offset + len(page) < total else None,
            "ranked": ranking_active}


def reference_vector_from(fi: FileIndex, start: float, end: float) -> Optional[np.ndarray]:
    """The mean picture vector of a file's range (its own keyframes: no cloud call needed)."""
    if fi.image_matrix is None:
        return None
    rows = [i for i, r in enumerate(fi.image_rows) if start <= r["t"] < end]
    if not rows:
        return None
    v = fi.image_matrix[rows].mean(axis=0)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def reference_look_from(fi: FileIndex, start: Optional[float] = None, end: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A file's look profile, or the median look of the shots in a range."""
    if start is None or end is None:
        prof = (fi.look_file or {}).get("profile")
        return prof if prof and prof.get("present") else None
    profiles = [s["look"] for s in fi.shots_between(start, end) if s.get("look") and s["look"].get("present")]
    if not profiles:
        return None
    if len(profiles) == 1:
        return profiles[0]
    merged = dict(profiles[0])
    for key in ("avg_luma", "warm_cool", "green_magenta", "sat_proxy", "contrast_span", "clipped_shadows", "clipped_highlights"):
        vals = [p[key] for p in profiles if p.get(key) is not None]
        if vals:
            merged[key] = round(float(np.median(vals)), 4)
    merged["channel_means"] = {c: round(float(np.median([p["channel_means"][c] for p in profiles if p.get("channel_means", {}).get(c) is not None] or [0.0])), 4)
                               for c in ("red", "green", "blue", "luma")}
    return merged
