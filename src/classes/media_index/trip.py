"""
 @file
 @brief Order a project's clips into days and places, from when and where they were shot.

 Pure logic over the capture facts already on the shelf (``captured_at`` and ``gps`` per file). A new
 *day* starts after a long gap between clips (a night's sleep), not at midnight, so late-evening footage
 stays with its day and time zones do not matter. A *place* is a cluster of nearby GPS fixes. Location
 never leaves this machine: nothing here is sent anywhere, and no place names are looked up.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

DAY_GAP_HOURS = 6.0
PLACE_RADIUS_KM = 1.5
EARTH_KM = 6371.0088


def haversine_km(a: Dict[str, float], b: Dict[str, float]) -> float:
    la1, lo1, la2, lo2 = (math.radians(float(v)) for v in (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def _when(text: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def trip_outline(files: Sequence[Dict[str, Any]], *, day_gap_hours: float = DAY_GAP_HOURS,
                 place_radius_km: float = PLACE_RADIUS_KM) -> Dict[str, Any]:
    """Days and places for ``files``: dicts with ``file_id``, ``captured_at`` (ISO), ``gps`` and ``duration``.

    Files without a capture time are listed under ``undated`` (their order is unknown); files with a time
    but no position simply have no place.
    """
    dated, undated = [], []
    for f in files:
        when = _when(f.get("captured_at"))
        (dated if when else undated).append((when, f))
    dated.sort(key=lambda it: (it[0], str(it[1].get("file_id"))))

    places: List[Dict[str, Any]] = []

    def place_of(gps: Optional[Dict[str, float]]) -> Optional[int]:
        if not gps:
            return None
        for p in places:
            if haversine_km(p, gps) <= place_radius_km:
                n = p["fixes"]
                p["lat"] = (p["lat"] * n + gps["lat"]) / (n + 1)       # the centroid drifts toward the data
                p["lon"] = (p["lon"] * n + gps["lon"]) / (n + 1)
                p["fixes"] = n + 1
                return p["id"]
        places.append({"id": len(places), "lat": float(gps["lat"]), "lon": float(gps["lon"]), "fixes": 1})
        return places[-1]["id"]

    days: List[Dict[str, Any]] = []
    last: Optional[datetime] = None
    for when, f in dated:
        assert when is not None
        if last is None or (when - last).total_seconds() > day_gap_hours * 3600.0:
            days.append({"day": len(days) + 1, "start": f["captured_at"], "end": f["captured_at"], "date": str(f["captured_at"])[:10],
                         "file_ids": [], "place_ids": [], "minutes": 0.0})
        d = days[-1]
        pid = place_of(f.get("gps"))
        d["file_ids"].append(f["file_id"])
        d["end"] = f["captured_at"]
        d["minutes"] = round(d["minutes"] + float(f.get("duration") or 0.0) / 60.0, 2)
        if pid is not None and pid not in d["place_ids"]:
            d["place_ids"].append(pid)
        f["_place"] = pid
        last = when

    out_places = []
    for p in places:
        members = [f["file_id"] for _, f in dated if f.get("_place") == p["id"]]
        out_places.append({"id": p["id"], "lat": round(p["lat"], 5), "lon": round(p["lon"], 5), "file_ids": members,
                           "first_seen": next(f["captured_at"] for _, f in dated if f.get("_place") == p["id"]),
                           "minutes": round(sum(float(f.get("duration") or 0.0) for _, f in dated if f.get("_place") == p["id"]) / 60.0, 2)})
    for _, f in dated:
        f.pop("_place", None)
    return {"days": days, "places": out_places, "undated": [f["file_id"] for _, f in undated],
            "files_with_position": sum(1 for _, f in dated if f.get("gps")) + sum(1 for _, f in undated if f.get("gps"))}
