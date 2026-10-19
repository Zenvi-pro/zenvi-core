"""
 @file
 @brief Search a project's footage in one vector space, then snap results to real boundaries.

 A query is embedded once per model (by the caller) and compared with every vector of each file: keyframes, spoken
 sentences, sound and shot descriptions. A shot ranks by its single best-matching vector, whatever its kind, so a
 clip that happens to have a transcript never outranks a clearer picture match. Exact words in transcripts and
 descriptions are matched too and merged by rank, so names and numbers are not missed. One floor per model drops
 weak matches, and a relative cut drops hits far below the best one. Results are filtered by measured facts
 (camera move, shot type, colour, duration, speech) and snapped: a sentence match to the sentence's span, anything
 else to the shot's cuts.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from classes import color_agent as ca
from classes.media_index import embedder as E
from classes.media_index.library import FileIndex

RRF_K = 60
PER_MATRIX = 200                 # best rows taken from each file's matrix before choosing each shot's best
# A best match under the floor is noise. EmbeddingGemma 2 (q4) on 85 labelled queries and 12 that match nothing: the
# right answer scored 0.615-0.85 (median 0.77), the no-match queries' best 0.63-0.71, so 0.64 keeps 84 of 85 answers
# and turns away 5 of 12 no-match queries. Gemini Embedding 2's is its old picture floor (its per-kind floors were
# 0.36 picture, 0.40 description, 0.52 sentence; not re-measured for one-space ranking).
FLOOR = {E.MODEL_LOCAL: 0.64, E.MODEL_CLOUD: 0.36}
# Picture against picture (a reference clip): EmbeddingGemma 2 scores different subjects 0.64-0.84 (median 0.72), the
# same subject in another clip 0.73-0.93 (median 0.83); Gemini's picture floor stays as it was.
REFERENCE_FLOOR = {E.MODEL_LOCAL: 0.75, E.MODEL_CLOUD: 0.36}
RELATIVE_CUT = 0.08              # drop hits this far below the best one of the same model (right answers sat within 0.04)
KINDS = {"spoken": ("speech", "audio"), "on_screen": ("image", "shot"), "": ("image", "speech", "audio", "shot")}
_WORD = re.compile(r"[\w']+", re.UNICODE)
_STOP = frozenset("a an and are at be by for from has have he her his in is it its of on or she that the their they this to "
                  "was were where who with what when".split())
QueryVector = Union[None, np.ndarray, Dict[str, np.ndarray]]


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
    if "person_shots" in f and int(shot["id"]) not in ((f["person_shots"] or {}).get(fi.file_id) or ()):
        return False                         # only the shots a chosen person is on screen in (computed by the caller from people data)
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
    q = shot.get("quality") or {}
    if f.get("usable_only") and ((q.get("inferred") or {}).get("usable") is False or set(q.get("flags") or []) & {"blurry", "black"}):
        return False
    if f.get("min_highlight") and float(q.get("highlight", 0.0)) < float(f["min_highlight"]):
        return False
    if f.get("look"):
        profile = shot.get("look") or {}
        test = LOOK_TESTS.get(str(f["look"]))
        if not profile.get("present") or test is None or not test(profile, shot.get("look_extras") or {}):
            return False
    return True



def rrf(lists: Sequence[Sequence[Any]], k: int = RRF_K) -> Dict[Any, float]:
    """Reciprocal-rank fusion of ranked key lists: {key: score}."""
    scores: Dict[Any, float] = {}
    for keys in lists:
        for rank, key in enumerate(keys):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _speech_text(fi: FileIndex, start: float, end: float, limit: int = 160) -> str:
    said = " ".join(s["text"] for s in fi.sentences if s["end"] > start and s["start"] < end)
    return said[:limit]


def _vector_for(q: QueryVector, model: str) -> Optional[np.ndarray]:
    return q.get(model) if isinstance(q, dict) else q


def _words(text: str) -> List[str]:
    return [w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP]


def _shot_id(fi: FileIndex, row: Dict[str, Any], t: float) -> Optional[int]:
    if row.get("shot") is not None:
        return int(row["shot"])
    shot = fi.shot_at(t)
    return int(shot["id"]) if shot else None


def _best_vectors(fi: FileIndex, q: np.ndarray, kinds: Sequence[str]) -> Dict[int, Dict[str, Any]]:
    """{shot id: the shot's best-matching vector}: its kind, score and the time it stands for."""
    best: Dict[int, Dict[str, Any]] = {}
    for rows, matrix in ((fi.text_rows, fi.text_matrix), (fi.image_rows, fi.image_matrix), (fi.audio_rows, fi.audio_matrix)):
        if matrix is None or not rows:
            continue
        allowed = np.array([r.get("kind") in kinds for r in rows])
        if not allowed.any():
            continue
        sims = np.where(allowed, matrix @ q, -np.inf)          # the kinds asked for, before taking the best rows
        for i in np.argsort(-sims)[:int(allowed.sum())][:PER_MATRIX]:
            row, score = rows[int(i)], float(sims[int(i)])
            kind = row.get("kind")
            span = None if kind == "image" else (float(row["start"]), float(row["end"]))
            t = float(row["t"]) if kind == "image" else (span[0] + span[1]) / 2.0
            sid = _shot_id(fi, row, t)
            if sid is None:
                continue
            b = best.setdefault(sid, {"score": -1.0, "scores": {}})
            b["scores"][kind] = max(b["scores"].get(kind, -1.0), round(score, 4))
            if score > b["score"]:
                b.update(score=score, kind=kind, t=span[0] if kind == "speech" else t, span=span if kind == "speech" else None,
                         why=str(row.get("text") or "")[:160])
    return best


def _word_hits(fi: FileIndex, words: List[str], kinds: Sequence[str]) -> Dict[int, Dict[str, Any]]:
    """{shot id: the first sentence or description that holds every query word}."""
    out: Dict[int, Dict[str, Any]] = {}
    for row in fi.text_rows:
        if row.get("kind") not in kinds or not set(words) <= set(_words(row.get("text") or "")):
            continue
        start, end = float(row["start"]), float(row["end"])
        sid = _shot_id(fi, row, (start + end) / 2.0)
        if sid is not None:
            out.setdefault(sid, {"t": start, "span": (start, end) if row["kind"] == "speech" else None,
                                 "why": str(row.get("text") or "")[:160]})
    return out


def search(files: Sequence[FileIndex], *, query_vector: QueryVector = None, query_text: str = "",
           reference_vector: QueryVector = None, reference_look: Optional[Dict[str, Any]] = None,
           look_for: str = "", filters: Optional[Dict[str, Any]] = None, limit: int = 20, offset: int = 0,
           sort: str = "relevance") -> Dict[str, Any]:
    """Rank shots of *files*. With no query, vector or look reference, returns the shots that pass the filters.

    ``query_vector`` is the query embedded by each file's model ({model: vector}); ``query_text`` is its words, for
    exact matches. ``reference_vector`` ranks by picture likeness to a reference. ``look_for`` is "spoken" (sentences
    and sound), "on_screen" (pictures and descriptions) or "" (all). ``sort`` is "relevance" (the ranking, or file
    order when nothing was asked), "highlight" (best moments first) or "chronological" (by when each was shot, then
    by position in its file).
    """
    f = dict(filters or {})
    kinds = KINDS.get(look_for or "", KINDS[""])
    # The same footage imported twice is one candidate (the first file wins); the others are reported, not ranked again.
    by_sha: Dict[str, FileIndex] = {}
    same_content: Dict[str, List[str]] = {}
    for fi in files:
        if fi.sha in by_sha:
            same_content.setdefault(fi.sha, []).append(fi.file_id)
        else:
            by_sha[fi.sha] = fi
    files = list(by_sha.values())

    found: Dict[Tuple[str, int], Dict[str, Any]] = {}
    lists: List[List[Tuple[str, int]]] = []
    reference = query_vector is None
    q = reference_vector if reference else query_vector
    if q is not None:
        scored: List[Tuple[Tuple[str, int], float, str]] = []
        for fi in files:
            v = _vector_for(q, fi.vector_model)
            if v is None:
                continue
            for sid, b in _best_vectors(fi, v, ("image",) if reference else kinds).items():
                if b["score"] >= (REFERENCE_FLOOR if reference else FLOOR).get(fi.vector_model, 0.0):
                    found[(fi.sha, sid)] = b
                    scored.append(((fi.sha, sid), b["score"], fi.vector_model))
        # Each model's scores are ranked on their own: a cosine from one model means nothing next to another's.
        for model in sorted({m for _, _, m in scored}):
            mine = sorted((r for r in scored if r[2] == model), key=lambda r: -r[1])
            lists.append([k for k, s, _ in mine if reference or s >= mine[0][1] - RELATIVE_CUT])
    words = _words(query_text)
    by_words: List[Tuple[str, int]] = []
    if words:
        for fi in files:
            for sid, w in _word_hits(fi, words, kinds).items():
                key = (fi.sha, sid)
                if key not in found:
                    found[key] = {"kind": "words", "score": None, "scores": {}, **w}
                elif w["span"] and found[key].get("kind") != "speech":
                    found[key].update(kind="words", t=w["t"], span=w["span"], why=w["why"])
                by_words.append(key)
    if reference_look and reference_look.get("present"):
        looks: List[Tuple[Tuple[str, int], float]] = []
        for fi in files:
            for shot in fi.shots:
                p = shot.get("look")
                if p and p.get("present"):
                    d = ca.look_profile_distance(p, reference_look)
                    if d is not None:
                        looks.append(((fi.sha, int(shot["id"])), d))
        lists.append([k for k, _ in sorted(looks, key=lambda r: r[1])][:PER_MATRIX])
        for key, d in looks:
            found.setdefault(key, {"kind": "look", "score": None, "scores": {}})["scores"]["look_distance"] = round(float(d), 4)

    queried = q is not None or bool(words) or bool(reference_look and reference_look.get("present"))
    lists = [x for x in lists if x]
    if lists or by_words:
        fused = rrf(lists)
        for key in by_words:              # every exact-word hit counts the same: where it was found says nothing
            fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + 1)
        keys = sorted(fused, key=lambda k: -fused[k])
    elif queried:
        fused, keys = {}, []          # something was asked for and nothing matched: say so, don't list everything
    else:
        fused, keys = {}, [(fi.sha, int(s["id"])) for fi in files for s in fi.shots]

    hits: List[Dict[str, Any]] = []
    for key in keys:
        fi = by_sha[key[0]]
        shot = next((s for s in fi.shots if s["id"] == key[1]), None)
        if shot is None or not passes_filters(shot, f, fi):
            continue
        info = found.get(key, {})
        span = info.get("span")
        start, end = (max(shot["start"], span[0]), min(shot["end"], span[1])) if span else (shot["start"], shot["end"])
        if end <= start:
            start, end = shot["start"], shot["end"]
        watch = shot.get("watch") or {}
        score = info.get("score")
        hits.append({
            "file_id": fi.file_id, "name": fi.name, "sha": fi.sha, "shot_id": shot["id"],
            "start": round(start, 3), "end": round(end, 3),
            "snapped_to": "speech" if (start, end) != (shot["start"], shot["end"]) else "shot",
            "peak": round(min(max(float(info.get("t", (shot["start"] + shot["end"]) / 2.0)), shot["start"]), shot["end"]), 3),
            "matched": info.get("kind"), "score": round(float(score), 4) if score is not None else None,
            "rank_score": round(float(fused.get(key, 0.0)), 5), "scores": info.get("scores") or {},
            "why": info.get("why") or (watch.get("description") or _speech_text(fi, shot["start"], shot["end"]))[:160],
            "camera": (shot.get("motion") or {}).get("class"), "shot_type": watch.get("shot_type") or None,
            "mood": watch.get("mood") or None,
            "speech_ratio": (shot.get("speech") or {}).get("speech_ratio"),
            "highlight": (shot.get("quality") or {}).get("highlight"), "captured_at": fi.captured_at or None,
            "same_content_files": same_content.get(fi.sha) or None,
        })
    if sort == "highlight":
        hits.sort(key=lambda h: (-(h["highlight"] or 0.0), -h["rank_score"]))
    elif sort == "chronological":
        order = {fi.file_id: i for i, fi in enumerate(files)}
        hits.sort(key=lambda h: (h["captured_at"] or "9999", order.get(h["file_id"], 0), h["start"]))
    total = len(hits)
    page = hits[offset: offset + max(1, limit)]
    return {"hits": page, "total": total, "next": offset + len(page) if offset + len(page) < total else None,
            "ranked": queried}


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
