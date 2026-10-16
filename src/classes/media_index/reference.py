"""
 @file
 @brief Recreate a reference from available footage, and find things inside footage.

 ``match_reference`` walks the reference shot by shot. For each one it searches the other files by
 picture (the reference's own saved frames, no cloud call), keeps candidates long enough to fill the
 needed duration, and places the cut window around the best moment. A shot nothing matches is
 reported with a stock-search phrase from its description, so the gap can be filled from stock.
 ``locate`` finds an object or on-screen text with its rough position, the input a mask or a
 generative replace needs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from classes.media_index import search as msearch
from classes.media_index.library import FileIndex

MIN_REFERENCE_SHOT = 0.25          # a flash shorter than this is not worth matching on its own


def _shot_query_vector(ref: FileIndex, shot: Dict[str, Any]) -> Optional[np.ndarray]:
    """What the reference shot looks like: its own frames, else the vector of its description."""
    v = msearch.reference_vector_from(ref, shot["start"], shot["end"])
    if v is not None:
        return v
    if ref.text_matrix is not None:
        for i, row in enumerate(ref.text_rows):
            if row.get("kind") == "shot" and int(row.get("shot", -1)) == int(shot["id"]):
                return ref.text_matrix[i]
    return None


def _window(hit: Dict[str, Any], shot: Dict[str, Any], need: float) -> Dict[str, Any]:
    """A cut of `need` seconds inside the hit's shot, centred on where the match peaks."""
    length = shot["end"] - shot["start"]
    if length + 1e-6 < need:
        return {"in": round(shot["start"], 3), "out": round(shot["end"], 3), "fits": False, "short_by": round(need - length, 3)}
    start = min(max(hit["peak"] - need / 2.0, shot["start"]), shot["end"] - need)
    return {"in": round(start, 3), "out": round(start + need, 3), "fits": True, "short_by": 0.0}


def match_reference(ref: FileIndex, files: Sequence[FileIndex], start: Optional[float] = None, end: Optional[float] = None,
                    per_shot: int = 3, filters: Optional[Dict[str, Any]] = None, include_reference: bool = False
                    ) -> Dict[str, Any]:
    pool = [f for f in files if include_reference or f.sha != ref.sha]
    lo = 0.0 if start is None else float(start)
    hi = float(ref.duration or (ref.shots[-1]["end"] if ref.shots else 0.0)) if end is None else float(end)
    by_sha = {f.sha: f for f in pool}
    rows: List[Dict[str, Any]] = []
    for shot in ref.shots_between(lo, hi):
        a, b = max(shot["start"], lo), min(shot["end"], hi)
        need = b - a
        if need < MIN_REFERENCE_SHOT or shot.get("black"):
            continue
        watch = shot.get("watch") or {}
        row: Dict[str, Any] = {"reference_shot": shot["id"], "start": round(a, 3), "end": round(b, 3), "duration": round(need, 3),
                               "description": watch.get("description") or "", "camera": (shot.get("motion") or {}).get("class"),
                               "candidates": []}
        qv = _shot_query_vector(ref, shot)
        if qv is not None and pool:
            res = msearch.search(pool, query_vector=qv, filters=filters or {}, limit=max(per_shot * 3, 6))
            for hit in res["hits"]:
                cand = by_sha[hit["sha"]]
                src = next((s for s in cand.shots if s["id"] == hit["shot_id"]), None)
                if src is None:
                    continue
                w = _window(hit, src, need)
                row["candidates"].append({"file_id": hit["file_id"], "name": hit["name"], "shot_id": hit["shot_id"],
                                          "score": hit["score"], "scores": hit["scores"], "why": hit["why"], **w})
            # whole-fit candidates first, then by rank (stable)
            row["candidates"].sort(key=lambda c: (not c["fits"], -c["score"]))
            row["candidates"] = row["candidates"][:per_shot]
        fitting = [c for c in row["candidates"] if c["fits"]]
        row["status"] = "matched" if fitting else ("short_only" if row["candidates"] else "no_match")
        if row["status"] != "matched":
            row["stock_query"] = " ".join((row["description"] or "footage").split()[:14])
        rows.append(row)
    matched = sum(1 for r in rows if r["status"] == "matched")
    return {"shots": rows, "matched": matched, "total": len(rows)}


def locate(files: Sequence[FileIndex], what: str, *, kind: str = "auto", start: Optional[float] = None,
           end: Optional[float] = None, limit: int = 30) -> List[Dict[str, Any]]:
    """Objects (label) and on-screen text matching `what`, with absolute time and the rough box [x, y, w, h] (0-1)."""
    want = " ".join(str(what or "").lower().split())
    if not want:
        return []
    words = want.split()

    def hit(text: str) -> bool:
        t = " ".join(str(text or "").lower().split())
        return bool(t) and (want in t or t in want or all(w in t for w in words))

    out: List[Dict[str, Any]] = []
    for fi in files:
        for shot in fi.shots:
            if start is not None and shot["end"] <= start or end is not None and shot["start"] >= end:
                continue
            watch = shot.get("watch") or {}
            found = []
            if kind in ("auto", "object"):
                found += [("object", o["label"], o) for o in watch.get("objects") or [] if hit(o.get("label"))]
            if kind in ("auto", "text"):
                found += [("text", o["text"], o) for o in watch.get("on_screen_text") or [] if hit(o.get("text"))]
            for k, label, o in found:
                t = o.get("t")
                t_abs = shot["start"] + float(t) if t is not None else (shot["start"] + shot["end"]) / 2.0
                if start is not None and t_abs < start or end is not None and t_abs > end:
                    continue
                out.append({"file_id": fi.file_id, "name": fi.name, "kind": k, "label": label, "shot_id": shot["id"],
                            "shot_start": round(shot["start"], 3), "shot_end": round(shot["end"], 3), "t": round(t_abs, 3),
                            "box": o.get("box"), "box_precision": "rough", "confidence": watch.get("confidence")})
    out.sort(key=lambda r: (r["file_id"], r["t"]))
    return out[:limit]


# ============================ a whole recreation: who plays each shot ============================
# `match_reference` ranks candidates one reference shot at a time, so when the footage is small one clip is the best match for most
# shots (measured on a real project: one 3 s clip was the top match for 41 of 85 shots, and picture similarity only spread from 0.54
# to 0.65 across all candidates). `plan_recreation` chooses for every shot at once: each candidate window is scored against the shot
# relative to its alternatives, then windows are handed out best first under limits on how often the same moment and the same file may
# be used and with no file playing twice in a row.
WEIGHT_PICTURE, WEIGHT_LOOK, WEIGHT_MOTION = 0.60, 0.25, 0.15
MIN_PLAN_SCORE = 0.45          # under this a choice is weak: look for stock or another source
BUCKET_SECONDS = 0.25          # reuse is counted per quarter second of source footage
MAX_WINDOWS_PER_SHOT = 12
FIT_MIN = 0.50                 # a pick looks like its reference shot only when its picture similarity is at least this far from the typical pair toward the best pair
REPEAT_MIN_GAP = 3             # a repeat comes back at least this many shots later
REPEAT_COSINE = 0.93           # two reference shots this alike are the same image coming back: only then may a source moment be used again
PICTURE_SPREAD_FLOOR = 0.03    # picture similarity spreads about this much across real footage; less than that is noise and must not be blown up


def _look_similarity(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> float:
    from classes import color_agent as ca
    d = ca.look_profile_distance(a or {}, b or {})
    return 0.5 if d is None else 1.0 / (1.0 + 2.0 * float(d))


def _motion_match(ref_class: Optional[str], src_class: Optional[str]) -> float:
    if not ref_class or not src_class or "unknown" in (ref_class, src_class):
        return 0.5
    if ref_class == src_class:
        return 1.0
    return 0.0 if (ref_class == "static") != (src_class == "static") else 0.4


def _windows(src: Dict[str, Any], need: float) -> List[tuple]:
    """Where inside one source shot a cut of *need* seconds could start: spread across it, never crossing its own cuts."""
    first, last = float(src["start"]), float(src["end"]) - need
    if last < first - 1e-6:
        return []
    if last - first < 1e-6:
        return [(first, first + need)]
    n = min(MAX_WINDOWS_PER_SHOT, max(2, int((last - first) / max(0.4, need * 0.5)) + 1))
    return [(float(s), float(s) + need) for s in np.linspace(first, last, n)]


def scored_windows(ref: FileIndex, shot: Dict[str, Any], need: float, pool_files: Sequence[FileIndex]) -> List[tuple]:
    """Every window of *need* seconds that could play *shot*, scored: [(file index, in, out, source shot id, score, parts)].

    Picture similarity barely varies across a small bin of footage, so it counts relative to the shot's other candidates; the
    measured look and the camera move make up the rest. Used by the planner and by the checker (to say how good a placed clip was).
    """
    qv = _shot_query_vector(ref, shot)
    raw: List[tuple] = []
    for fi_index, cand in enumerate(pool_files):
        sims = times = None
        if qv is not None and cand.image_matrix is not None and len(cand.image_rows):
            sims = cand.image_matrix @ qv
            times = np.array([float(r["t"]) for r in cand.image_rows])
        for src in cand.shots:
            for w_in, w_out in _windows(src, need):
                pic = None
                if sims is not None and times is not None:
                    inside = (times >= w_in) & (times < w_out)
                    if not inside.any():
                        inside = (times >= float(src["start"])) & (times < float(src["end"]))
                    pic = float(sims[inside].mean()) if inside.any() else None
                raw.append((fi_index, w_in, w_out, src["id"], pic, _look_similarity(shot.get("look"), src.get("look")),
                            _motion_match((shot.get("motion") or {}).get("class"), (src.get("motion") or {}).get("class"))))
    pics = np.array([r[4] for r in raw if r[4] is not None], dtype=np.float64)
    mean, std = (float(pics.mean()), max(float(pics.std()), PICTURE_SPREAD_FLOOR)) if len(pics) else (0.0, 1.0)
    out = []
    for fi_index, w_in, w_out, src_id, pic, look, motion in raw:
        pic_n = 0.5 if pic is None else 1.0 / (1.0 + float(np.exp(-1.2 * (pic - mean) / std)))
        out.append((fi_index, w_in, w_out, src_id, WEIGHT_PICTURE * pic_n + WEIGHT_LOOK * look + WEIGHT_MOTION * motion,
                    {"picture": round(pic_n, 3), "look": round(look, 3), "motion": round(motion, 3), "raw_picture": None if pic is None else round(pic, 4)}))
    return out


def rank_of_window(scored: Sequence[tuple], file_index: int, w_in: float, w_out: float) -> Optional[float]:
    """Where a placed window stands among all the candidates for its shot: 1.0 = the best of them, 0.0 = the worst (None when unknown)."""
    if not scored:
        return None
    mine = [x for x in scored if x[0] == file_index and x[1] < w_out - 1e-6 and x[2] > w_in + 1e-6]
    if not mine:
        return None
    best_of_mine = max(x[4] for x in mine)
    return round(sum(1 for x in scored if x[4] <= best_of_mine) / len(scored), 3)


_ANIMATION_WORDS = {"anime", "animated", "animation", "cartoon", "illustrated", "illustration", "manga", "cel-shaded", "2d", "stylized", "stylised", "drawn", "digital"}
_CHARACTER_WORDS = {"character", "characters"}


def stock_query_for(description: str, look: Optional[Dict[str, Any]] = None, limit: int = 8) -> str:
    """Words to search real stock footage with for a shot described in the reference: the scene without the animation vocabulary ("anime
    character" becomes "person"), plus "dark" or "bright" from the shot's measured look, since lighting is what a grade cannot supply."""
    words = []
    for w in (description or "footage").replace(",", " ").replace(".", " ").split():
        low = w.lower().strip("'\"()")
        if low in _ANIMATION_WORDS:
            continue
        words.append("person" if low in _CHARACTER_WORDS else w)
        if len(words) >= limit:
            break
    luma = (look or {}).get("avg_luma")
    tone = "" if luma is None else ("dark moody" if float(luma) < 0.3 else ("bright" if float(luma) > 0.65 else ""))
    return " ".join(([tone] if tone else []) + words)


def picture_fit(raw_values: Sequence[float], value: Optional[float]) -> Optional[float]:
    """How far *value* (a raw picture similarity) is from the typical shot/window pair toward the best ones: 0 = typical, 1 = as alike as the
    best 5% of all pairs. Judged against every pair, not the shot's own options, so the best of a poor lot does not score well. None when unknown."""
    if value is None or not len(raw_values):
        return None
    arr = np.asarray(raw_values, dtype=np.float64)
    typical, best = float(np.median(arr)), float(np.percentile(arr, 95))
    return round(float(np.clip((value - typical) / max(best - typical, PICTURE_SPREAD_FLOOR / 2), -1.0, 1.5)), 3)


def repeated_shots(ref: FileIndex, shots: Sequence[Dict[str, Any]]) -> Dict[int, int]:
    """{index of a reference shot: index of an earlier one it repeats}: the same image coming back (a chorus, a reused cut) is the only
    case where a recreation should show the same footage again."""
    vecs = [_shot_query_vector(ref, s) for s in shots]
    out: Dict[int, int] = {}
    for i, v in enumerate(vecs):
        if v is None:
            continue
        best, best_cos = None, REPEAT_COSINE
        for j in range(i - REPEAT_MIN_GAP + 1):                       # a similar shot right next to it is just a similar angle, not the image coming back
            if vecs[j] is not None and j not in out:
                cos = float(np.dot(v, vecs[j]))
                if cos >= best_cos:
                    best, best_cos = j, cos
        if best is not None:
            out[i] = best
    return out


def plan_recreation(ref: FileIndex, files: Sequence[FileIndex], start: Optional[float] = None, end: Optional[float] = None,
                    max_reuse: int = 1, min_score: float = MIN_PLAN_SCORE, alternatives: int = 2) -> Dict[str, Any]:
    """Choose a source window for every shot of *ref*, using each other file's own saved facts (no cloud call)."""
    pool_files = [f for f in files if f.sha != ref.sha]
    lo = 0.0 if start is None else float(start)
    hi = float(ref.duration or (ref.shots[-1]["end"] if ref.shots else 0.0)) if end is None else float(end)
    slots = []
    for shot in ref.shots_between(lo, hi):
        a, b = max(float(shot["start"]), lo), min(float(shot["end"]), hi)
        if b - a >= MIN_REFERENCE_SHOT and not shot.get("black"):
            slots.append((shot, a, b))

    # every candidate: (score, slot index, file index, window in, window out, source shot id, parts)
    candidates: List[tuple] = []
    for si, (shot, a, b) in enumerate(slots):
        for fi_index, w_in, w_out, src_id, score, parts in scored_windows(ref, shot, b - a, pool_files):
            candidates.append((score, si, fi_index, round(w_in, 3), round(w_out, 3), src_id, parts))
    candidates.sort(key=lambda c: -c[0])

    n_files = max(1, len({c[2] for c in candidates}))
    soft_cap = int(np.ceil(1.25 * len(slots) / n_files)) if slots else 0
    chosen: Dict[int, tuple] = {}
    file_uses: Dict[int, int] = {}
    buckets: Dict[tuple, int] = {}

    def spans(c: tuple) -> range:
        return range(int(c[3] / BUCKET_SECONDS), int(max(c[3], c[4] - 1e-6) / BUCKET_SECONDS) + 1)

    def reuse_of(c: tuple) -> int:
        return max((buckets.get((c[2], k), 0) for k in spans(c)), default=0)

    def next_to_same_file(c: tuple) -> bool:
        return any(chosen.get(n) is not None and chosen[n][2] == c[2] for n in (c[1] - 1, c[1] + 1))

    rep = repeated_shots(ref, [t[0] for t in slots])
    for reuse_limit, use_cap, no_repeat in ((1, True, True), (1, False, True), (1, False, False)) + (((max_reuse, False, True),) if max_reuse > 1 else ()):
        for c in candidates:
            if c[1] in chosen or c[0] <= 0 or c[1] in rep:
                continue
            if reuse_of(c) >= reuse_limit or (use_cap and file_uses.get(c[2], 0) >= soft_cap) or (no_repeat and next_to_same_file(c)):
                continue
            chosen[c[1]] = c
            file_uses[c[2]] = file_uses.get(c[2], 0) + 1
            for k in spans(c):
                buckets[(c[2], k)] = buckets.get((c[2], k), 0) + 1

    by_slot: Dict[int, List[tuple]] = {}
    for c in candidates:
        by_slot.setdefault(c[1], []).append(c)

    # a reference shot that is the same image coming back reuses the footage chosen for its first showing (the only reuse allowed)
    repeats: Dict[int, int] = {}
    for si, sj in sorted(rep.items()):
        base = chosen.get(sj)
        if base is None:
            continue
        same = [c for c in by_slot.get(si, []) if c[2] == base[2] and abs(c[3] - base[3]) <= 1.0 and not next_to_same_file(c)]
        if same:
            chosen[si] = same[0]
            file_uses[base[2]] = file_uses.get(base[2], 0) + 1
            repeats[si] = sj
    for si in rep:                                             # a repeat whose first showing found nothing is placed like any other shot
        if si in chosen:
            continue
        for c in by_slot.get(si, []):
            if c[0] > 0 and reuse_of(c) < 1 and not next_to_same_file(c):
                chosen[si] = c
                file_uses[c[2]] = file_uses.get(c[2], 0) + 1
                for k in spans(c):
                    buckets[(c[2], k)] = buckets.get((c[2], k), 0) + 1
                break
    every_raw = [c[6]["raw_picture"] for c in candidates if c[6].get("raw_picture") is not None]
    rows: List[Dict[str, Any]] = []
    for si, (shot, a, b) in enumerate(slots):
        watch = shot.get("watch") or {}
        desc = watch.get("description") or ""
        row: Dict[str, Any] = {"reference_shot": shot["id"], "start": round(a, 3), "end": round(b, 3), "duration": round(b - a, 3), "description": desc,
                               "camera": (shot.get("motion") or {}).get("class"), "opens_with": (shot.get("opens_with") or {}).get("kind"),
                               "look": shot.get("look"), "choice": None, "alternatives": []}
        pick = chosen.get(si)
        if pick is not None:
            cand = pool_files[pick[2]]
            row["choice"] = {"file_id": cand.file_id, "name": cand.name, "in": pick[3], "out": pick[4], "score": round(pick[0], 3),
                             "source_shot": pick[5], "parts": pick[6]}
            fit = picture_fit(every_raw, pick[6].get("raw_picture"))
            row["choice"]["picture_fit"] = fit
            row["status"] = "matched" if pick[0] >= min_score and (fit is None or fit >= FIT_MIN or si in repeats) else "weak"
            if si in repeats:
                row["repeat_of"] = slots[repeats[si]][0]["id"]
            taken = {(pick[2], pick[3])}
        else:
            row["status"] = "short_only" if not by_slot.get(si) and any(
                f.shots and max(s["end"] - s["start"] for s in f.shots) > 0 for f in pool_files) else "no_match"
            taken = set()
        seen_files = {pick[2]} if pick is not None else set()
        for c in by_slot.get(si, []):
            if len(row["alternatives"]) >= alternatives:
                break
            if (c[2], c[3]) in taken or c[2] in seen_files:
                continue
            cand = pool_files[c[2]]
            row["alternatives"].append({"file_id": cand.file_id, "name": cand.name, "in": c[3], "out": c[4], "score": round(c[0], 3), "source_shot": c[5]})
            seen_files.add(c[2])
        if row["status"] != "matched":
            row["stock_query"] = stock_query_for(desc, shot.get("look"))
        rows.append(row)

    used = [(pool_files[c[2]].file_id, c[3], c[4]) for c in chosen.values()]
    counts = {pool_files[k].file_id: v for k, v in sorted(file_uses.items(), key=lambda kv: -kv[1])}
    adjacent = sum(1 for si in chosen if si + 1 in chosen and chosen[si][2] == chosen[si + 1][2])
    return {"shots": rows, "total": len(rows), "matched": sum(1 for r in rows if r["status"] == "matched"),
            "weak": sum(1 for r in rows if r["status"] == "weak"), "unfilled": sum(1 for r in rows if r["choice"] is None),
            "reuse": {"files_used": len(counts), "uses_per_file": counts, "most_uses_of_one_moment": max(buckets.values(), default=0),
                      "same_file_back_to_back": adjacent, "slot_seconds": round(sum(b - a for _s, a, b in slots), 2),
                      "source_seconds": round(sum(u[2] - u[1] for u in used), 2)}}
