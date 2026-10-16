"""
 @file
 @brief A video as its shots: which shots a page of the storyboard holds, and the one instant that stands for each.

 Pure functions (no Qt, no ffmpeg) so the paging and the picture times are tested without a file.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from classes.media_index import contact


def page(shots: List[Dict[str, Any]], start: Optional[float], per_page: int = contact.MAX_TILES) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """(the shots of this page, the start to ask for the next page or None) for shots at or after *start*."""
    later = [s for s in shots if start is None or float(s["end"]) > float(start)]
    here, rest = later[:per_page], later[per_page:]
    return here, (float(rest[0]["start"]) if rest else None)


def keyframe_time(shot: Dict[str, Any], frame: float = 0.0) -> float:
    """The instant that stands for a shot: its middle, but never past its last frame."""
    start, end = float(shot["start"]), float(shot["end"])
    last = max(start, end - (frame if frame > 0 else 0.04))
    return round(min(last, (start + end) / 2.0), 3)
