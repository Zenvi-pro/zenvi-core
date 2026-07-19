"""Resolve the project's shared TwelveLabs index and video_id → file map."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


def build_project_index_name(project_id: str) -> str:
    """Must match desktop indexing in files_model / reindex handlers."""
    pid = str(project_id or "").strip()
    if not pid:
        return "zenvi-videos"
    return f"zenvi-{pid}"


def collect_project_twelvelabs_index(
    files: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """
    Scan project files for ready TwelveLabs metadata.

    Returns:
      {
        "index_id": str,
        "index_name": str,
        "video_map": { video_id: {"file_id", "name", "index_id", "index_name"} },
        "indexed_count": int,
        "error": str (optional),
      }
    """
    try:
        from classes.query import File
        from classes.twelvelabs_match import twelvelabs_is_indexed
    except Exception as e:
        return {
            "index_id": "",
            "index_name": "",
            "video_map": {},
            "indexed_count": 0,
            "error": f"File access unavailable: {e}",
        }

    if files is None:
        try:
            files = File.filter() or []
        except Exception as e:
            return {
                "index_id": "",
                "index_name": "",
                "video_map": {},
                "indexed_count": 0,
                "error": f"Could not list project files: {e}",
            }

    video_map: Dict[str, Dict[str, str]] = {}
    index_ids: List[str] = []
    index_names: List[str] = []

    for f in files:
        d = getattr(f, "data", None) or {}
        if not isinstance(d, dict):
            continue
        if d.get("zenvi_subclip"):
            # Prefer parent indexed assets; subclips share parent TL ids
            continue
        ai = d.get("ai_metadata") if isinstance(d.get("ai_metadata"), dict) else {}
        tl = ai.get("twelvelabs") if isinstance(ai.get("twelvelabs"), dict) else {}
        if not twelvelabs_is_indexed(tl):
            continue
        vid = str(tl.get("video_id") or "").strip()
        iid = str(tl.get("index_id") or "").strip()
        iname = str(tl.get("index_name") or "").strip()
        if not vid or not iid:
            continue
        fid = str(getattr(f, "id", None) or d.get("id") or "").strip()
        name = d.get("name") or (d.get("path") or "").split("/")[-1] or fid
        video_map[vid] = {
            "file_id": fid,
            "name": str(name),
            "index_id": iid,
            "index_name": iname,
        }
        index_ids.append(iid)
        if iname:
            index_names.append(iname)

    if not video_map:
        # Try to derive expected name from project id even with zero indexed files
        project_id = ""
        try:
            from classes.app import get_app
            project_id = str(get_app().project.get("id") or "")
        except Exception:
            pass
        return {
            "index_id": "",
            "index_name": build_project_index_name(project_id),
            "video_map": {},
            "indexed_count": 0,
            "error": "No TwelveLabs-indexed videos in this project yet.",
        }

    # Majority vote for shared project index_id
    index_id = Counter(index_ids).most_common(1)[0][0]
    index_name = ""
    if index_names:
        index_name = Counter(index_names).most_common(1)[0][0]
    if not index_name:
        try:
            from classes.app import get_app
            index_name = build_project_index_name(str(get_app().project.get("id") or ""))
        except Exception:
            index_name = "zenvi-videos"

    return {
        "index_id": index_id,
        "index_name": index_name,
        "video_map": video_map,
        "indexed_count": len(video_map),
    }


def map_search_hit_to_file(
    hit: Dict[str, Any],
    video_map: Dict[str, Dict[str, str]],
) -> Tuple[str, str]:
    """Return (media_bin_file_id, display_name) for a TL search hit."""
    vid = str(
        (hit or {}).get("video_id")
        or (hit or {}).get("twelvelabs_video_id")
        or ""
    ).strip()
    meta = video_map.get(vid) or {}
    return (
        str(meta.get("file_id") or ""),
        str(meta.get("name") or hit.get("filename") or vid or "unknown"),
    )
