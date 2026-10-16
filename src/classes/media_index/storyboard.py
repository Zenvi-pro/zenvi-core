"""
 @file
 @brief A video as its shots: which shots a page of the storyboard holds, and the one instant that stands for each.

 Pure functions (no Qt, no ffmpeg) so the paging and the picture times are tested without a file.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from classes.media_index import contact
from classes.media_index.library import FileIndex


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


MAX_LIST_SHOTS = 300      # a whole feature-length cut fits one call as one-line rows; longer files continue with next_start


def _clock(sec: float) -> str:
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{sec % 60:05.2f}"


def list_line(fi: FileIndex, shot: Dict[str, Any], max_chars: int = 110) -> str:
    """One row for a shot, with its own exact time: ``S12 0:05.50-0:05.90 (0.4s) close, handheld: what happens ...``.

    A row per shot is what lets an agent keep every cut, however short, instead of merging a run of quick shots into one range.
    """
    watch = shot.get("watch") or {}
    kind = ", ".join(x for x in (watch.get("shot_type"), (shot.get("motion") or {}).get("class")) if x and x != "unknown")
    said = " ".join(f'"{s["text"]}"' for s in fi.sentences if s["end"] > shot["start"] and s["start"] < shot["end"])
    text = str(watch.get("description") or "").strip().split(". ")[0].rstrip(".")
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    line = f"S{shot['id']} {_clock(shot['start'])}-{_clock(shot['end'])} ({float(shot['end']) - float(shot['start']):.1f}s)"
    line += (" " + kind if kind else "") + (": " + text if text else "")
    if shot.get("black"):
        line += " [black]"
    return line + (f" says {said}" if said else "")
