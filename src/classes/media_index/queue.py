"""
 @file
 @brief Which queued file to index next.

 Pure helpers so the ordering rule is tested without the editor. The file model keeps its
 own bounded worker slots and calls ``next_index`` each time one frees up.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Sequence, Set


def timeline_file_ids(clips: Iterable[Any]) -> Set[str]:
    """Ids of the files that at least one timeline clip uses."""
    used: Set[str] = set()
    for clip in clips or []:
        if isinstance(clip, dict) and clip.get("file_id"):
            used.add(str(clip["file_id"]))
    return used


def next_index(queue: Sequence[str], used_on_timeline: Set[str]) -> int:
    """Position in *queue* to start next: a file on the timeline first, else the oldest.

    Within each group the order is first in, first out, so nothing is starved by a later
    import. An empty queue returns 0 (the caller checks for emptiness before popping).
    """
    for position, file_id in enumerate(queue):
        if str(file_id) in used_on_timeline:
            return position
    return 0


def unsearchable_ids(files: Iterable[Any], needs_vectors: Callable[[Dict[str, Any]], bool]) -> List[str]:
    """Ids of the media files *needs_vectors* says have no search vectors from the chosen model yet."""
    return [str(item["id"]) for item in files or []
            if isinstance(item, dict) and item.get("id") and str(item.get("media_type") or "video") in ("video", "audio", "image")
            and needs_vectors(item)]
