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
