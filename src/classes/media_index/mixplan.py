"""
 @file
 @brief The arithmetic of balancing a mix: even out the voice, then set the overall level.

 Pure functions. The tool that uses them applies the results through the editor's own volume and ducking
 operations; nothing here touches the project. All levels are decibels as measured by the media index
 (the average level of a clip's audible source range) plus the gain currently set on the clip.
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, Optional, Sequence

SPEECH_TOLERANCE_DB = 1.5        # voices closer than this to the target are left alone
MIN_GAIN_DB = -12.0              # one pass never cuts or boosts a clip by more than this
MAX_GAIN_DB = 9.0
MASTER_TOLERANCE_LU = 1.0
MASTER_MAX_UP = 9.0
MASTER_MAX_DOWN = -15.0
TRUE_PEAK_LIMIT_DB = -1.0


def speech_adjustments(items: Sequence[Dict[str, Any]], *, target_db: Optional[float] = None,
                       tolerance_db: float = SPEECH_TOLERANCE_DB, min_db: float = MIN_GAIN_DB, max_db: float = MAX_GAIN_DB
                       ) -> Dict[str, Any]:
    """Gain changes that bring every speaking clip to one level.

    ``items``: ``{"id", "level_db", "gain_db"}`` where ``level_db`` is the source level and ``gain_db`` the gain now set
    (None when the volume is automated: such a clip is left alone, since its level changes over time). The target is
    the median of the clips' current levels unless given. Returns ``{"target_db", "adjust": [...], "left_alone": [...]}``.
    """
    usable, left = [], []
    for it in items:
        if it.get("level_db") is None:
            left.append({"id": it["id"], "why": "its level is not indexed"})
        elif it.get("gain_db") is None:
            left.append({"id": it["id"], "why": "its volume is automated"})
        else:
            usable.append({"id": it["id"], "now": float(it["level_db"]) + float(it["gain_db"]), "gain": float(it["gain_db"])})
    if not usable:
        return {"target_db": target_db, "adjust": [], "left_alone": left}
    goal = float(target_db) if target_db is not None else round(statistics.median(u["now"] for u in usable), 2)
    adjust = []
    for u in usable:
        delta = goal - u["now"]
        if abs(delta) <= tolerance_db:
            continue
        new_gain = min(max_db, max(min_db, u["gain"] + delta))
        applied = new_gain - u["gain"]
        if abs(applied) < 0.05:
            left.append({"id": u["id"], "why": f"already at the {'cut' if delta < 0 else 'boost'} limit"})
            continue
        adjust.append({"id": u["id"], "from_db": round(u["now"], 1), "to_db": round(u["now"] + applied, 1), "delta_db": round(applied, 2),
                       "limited": abs(applied - delta) > 0.05})
    return {"target_db": goal, "adjust": adjust, "left_alone": left}


def master_correction(measured_lufs: Optional[float], target_lufs: float, true_peak_db: Optional[float] = None, *,
                      tolerance_lu: float = MASTER_TOLERANCE_LU, max_up: float = MASTER_MAX_UP, max_down: float = MASTER_MAX_DOWN,
                      peak_limit: float = TRUE_PEAK_LIMIT_DB) -> Dict[str, Any]:
    """The overall gain (dB) that brings a rendered mix to the target loudness without pushing peaks over the limit."""
    if measured_lufs is None:
        return {"delta_db": 0.0, "reason": "the mix has no measurable loudness (silent?)", "peak_warning": None}
    delta = float(target_lufs) - float(measured_lufs)
    if abs(delta) <= tolerance_lu:
        return {"delta_db": 0.0, "reason": f"already within {tolerance_lu:g} LU of the target", "peak_warning": None}
    clamped = min(max_up, max(max_down, delta))
    warning = None
    if true_peak_db is not None and clamped > 0 and float(true_peak_db) + clamped > peak_limit:
        room = peak_limit - float(true_peak_db)
        if room < 0.5:
            return {"delta_db": 0.0, "reason": "the peaks leave no room to raise the level",
                    "peak_warning": f"peaks are at {float(true_peak_db):.1f} dB, so the mix cannot get louder without distortion: "
                                    "lower the loudest clips, or add a compressor (apply_audio_effect_tool)"}
        clamped = round(room, 2)
        warning = f"raised only {clamped:.1f} dB so peaks stay under {peak_limit:g} dB; the mix will stay a little under target"
    return {"delta_db": round(clamped, 2), "reason": ("raised" if clamped > 0 else "lowered") + f" to move {measured_lufs:.1f} LUFS toward {target_lufs:g}"
            + (" (limited)" if abs(clamped - delta) > 0.05 and warning is None else ""), "peak_warning": warning}


DUCK_MARGIN_DB = 10.0            # the music should sit this far under the quietest voice it plays with
DUCK_FLOOR_DB = -24.0            # the deepest duck ever applied
DUCK_CEILING_DB = -3.0           # a duck shallower than this is not worth keyframing


def duck_depth(speech_levels_db: Sequence[float], bed_level_db: Optional[float], *, margin_db: float = DUCK_MARGIN_DB,
               floor_db: float = DUCK_FLOOR_DB, ceiling_db: float = DUCK_CEILING_DB) -> Dict[str, Any]:
    """How far to duck a music bed so it sits ``margin_db`` under the quietest voice it overlaps.

    Levels are what will be heard: source level plus the gain set on the clip. Returns ``{"duck_db", "needed", "limited"}``
    where ``duck_db`` is a negative attenuation (None when the bed already sits far enough under, or its level is unknown:
    the caller then leaves ducking to the editor's own estimate), and ``limited`` says the floor stopped it reaching the margin.
    """
    levels = [float(x) for x in speech_levels_db if x is not None]
    if not levels or bed_level_db is None:
        return {"duck_db": None, "needed": None, "limited": False, "why": "levels unknown"}
    quietest = min(levels)
    needed = (quietest - margin_db) - float(bed_level_db)          # attenuation that gives exactly the margin (negative = quieter)
    if needed >= -0.5:
        return {"duck_db": None, "needed": round(needed, 1), "limited": False, "why": "already far enough under the voice"}
    depth = min(ceiling_db, max(floor_db, needed))
    return {"duck_db": round(depth, 1), "needed": round(needed, 1), "limited": needed < floor_db, "why": "ducked to the margin"}
