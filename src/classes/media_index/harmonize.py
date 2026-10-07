"""
 @file
 @brief Make clips look like they belong together: pick the reference look and the clips that stray from it.

 Pure planning over the measured look of each clip (the Phase 6 LookProfile of the clip as it plays, grade
 included). The reference is the *medoid*: the clip whose look is closest to all the others, so the group is
 pulled toward its own centre and nothing is dragged toward an outlier. The grading itself (solving the
 ColorGrade values, applying them, re-measuring and recovering from a bad result) is Phase 6's, reused as it is.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

DEFAULT_TOLERANCE = 0.15      # look_profile_distance beyond this is a visibly different look (the same scene twice is about 0.02)
DEFAULT_MAX_CLIPS = 12

Distance = Callable[[Dict[str, Any], Dict[str, Any]], Optional[float]]


def pick_reference(profiles: Dict[str, Dict[str, Any]], order: Sequence[str], distance: Distance, how: str = "median") -> str:
    """The clip id to match the others to: *how* is a clip id, or "median" for the medoid."""
    present = [cid for cid in order if cid in profiles]
    if not present:
        raise ValueError("no clip has a measured look")
    if how and how != "median":
        if how not in profiles:
            raise ValueError(f"clip {how!r} has no measured look")
        return how
    if len(present) == 1:
        return present[0]

    def total(cid: str) -> float:
        return sum(d for d in (distance(profiles[cid], profiles[o]) for o in present if o != cid) if d is not None)

    return min(present, key=lambda cid: (total(cid), present.index(cid)))


def plan_harmonize(profiles: Dict[str, Dict[str, Any]], order: Sequence[str], distance: Distance, *, reference: str = "median",
                   tolerance: float = DEFAULT_TOLERANCE, max_clips: int = DEFAULT_MAX_CLIPS) -> Dict[str, Any]:
    """Which clips to match to which reference. ``order`` is the timeline order of the clip ids."""
    ref = pick_reference(profiles, order, distance, reference)
    distances: Dict[str, float] = {}
    unmeasured: List[str] = []
    for cid in order:
        if cid == ref:
            continue
        if cid not in profiles:
            unmeasured.append(cid)
            continue
        d = distance(profiles[cid], profiles[ref])
        if d is None:
            unmeasured.append(cid)
        else:
            distances[cid] = round(float(d), 4)
    strays = sorted((cid for cid, d in distances.items() if d > tolerance), key=lambda cid: -distances[cid])
    return {"reference": ref, "distances": distances, "to_match": strays[:max_clips], "left_out": strays[max_clips:],
            "within_tolerance": [cid for cid in order if cid in distances and distances[cid] <= tolerance], "unmeasured": unmeasured,
            "tolerance": tolerance}
