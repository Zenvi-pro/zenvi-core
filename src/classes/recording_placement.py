"""Where a voiceover recorded over a clip goes (Clip menu > Audio > Record).

Qt-free, so the timeline's menu handler and the assistant's prepare_recording_tool
share one rule: the nearest lower, unlocked track with no clip overlapping the
source clip's time range; the clip's own track when there is none.
"""

from __future__ import annotations

from typing import Iterable


def _span(data: dict) -> tuple:
    left = float(data.get("position", 0.0) or 0.0)
    return left, left + max(0.0, float(data.get("end", 0.0) or 0.0) - float(data.get("start", 0.0) or 0.0))


def recording_track_for_clip(clip_data: dict, tracks: Iterable[dict], clips: Iterable[dict]) -> int:
    """Layer number for a recording over *clip_data* (tracks and clips are project dicts)."""
    try:
        clip_data = clip_data if isinstance(clip_data, dict) else {}
        source_track = int(clip_data.get("layer", 1) or 1)
        start, end = _span(clip_data)
    except (TypeError, ValueError):
        return 1
    end = max(end, start + 0.001)

    numbers = []
    for track in tracks or []:
        try:
            number = int(track.get("number", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if number < source_track and not track.get("lock", False):
            numbers.append(number)
    if not numbers:
        return source_track

    occupied: dict = {}
    for data in clips or []:
        if not isinstance(data, dict):
            continue
        try:
            occupied.setdefault(int(data.get("layer", 0) or 0), []).append(_span(data))
        except (TypeError, ValueError):
            continue

    for number in sorted(numbers, reverse=True):
        if not any(left < end and right > start for left, right in occupied.get(number, [])):
            return number
    return source_track
