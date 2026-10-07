"""
 @file
 @brief Which queued file to index next.

 Pure helpers so the ordering rule is tested without the editor. The file model keeps its
 own bounded worker slots and calls ``next_index`` each time one frees up.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Sequence, Set, Tuple


def timeline_file_ids(clips: Iterable[Any]) -> Set[str]:
    """Ids of the files that at least one timeline clip uses."""
    used: Set[str] = set()
    for clip in clips or []:
        if isinstance(clip, dict) and clip.get("file_id"):
            used.add(str(clip["file_id"]))
    return used


def next_index(queue: Sequence[Tuple[str, bool]], used_on_timeline: Set[str]) -> int:
    """Position in *queue* to start next: a file on the timeline first, else the oldest.

    Within each group the order is first in, first out, so nothing is starved by a later
    import. An empty queue returns 0 (the caller checks for emptiness before popping).
    """
    for position, (file_id, _summarize_only) in enumerate(queue):
        if str(file_id) in used_on_timeline:
            return position
    return 0


def signin_skipped_ids(files: Iterable[Any]) -> List[str]:
    """Ids of files whose cloud indexing was skipped only because nobody was signed in."""
    ids: List[str] = []
    for item in files or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        meta = item.get("ai_metadata")
        if isinstance(meta, dict) and meta.get("skip_code") == "signin":
            ids.append(str(item["id"]))
    return ids
