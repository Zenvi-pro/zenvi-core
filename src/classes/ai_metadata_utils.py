"""
Utility functions for handling AI metadata on clips, especially for sub-clipping operations.

Scene timestamps are always resolved against **root source-file seconds** for filtering.
Persisted clip metadata stores:
  - scene.source_time — absolute seconds in the indexed root file
  - scene.time — clip-local seconds (0 at clip trim start) when rebased=True
  - source_window — {start, end} absolute source bounds for this placement
"""

from __future__ import annotations

import copy
from typing import Dict, Any, List, Optional, Tuple

_SCENE_EPS = 1e-3

_BASIC_CLIP_PROPS = ("id", "layer", "position", "start", "end", "duration")


def merge_basic_clip_props(
    old_data: Optional[dict],
    clip_data: dict,
) -> dict:
    """Patch drag/resize fields without dropping ai_metadata, effects, title, etc."""
    if not isinstance(clip_data, dict):
        return old_data if isinstance(old_data, dict) else {}
    if isinstance(old_data, dict) and old_data:
        merged = dict(old_data)
        for key in _BASIC_CLIP_PROPS:
            if key in clip_data:
                merged[key] = clip_data[key]
        return merged
    return dict(clip_data)


def clip_metadata_is_valid(
    clip_ai: Optional[dict],
    source_start: float,
    source_end: float,
) -> bool:
    """True when persisted clip ai_metadata matches the trim window."""
    if not isinstance(clip_ai, dict) or not clip_ai.get("analyzed"):
        return False
    sw = clip_ai.get("source_window")
    if not isinstance(sw, dict):
        return False
    ws = float(sw.get("start", -1) or -1)
    we = float(sw.get("end", -1) or -1)
    if abs(ws - source_start) > _SCENE_EPS or abs(we - source_end) > _SCENE_EPS:
        return False
    scenes = clip_ai.get("scene_descriptions") or []
    if not scenes:
        return True
    return all(
        isinstance(s, dict) and s.get("source_time") is not None for s in scenes
    )


def get_source_window(
    clip_data: Optional[dict],
    file_data: Optional[dict] = None,
) -> Tuple[float, float]:
    """Return (source_start, source_end) in root source-file seconds for a placement."""
    data = clip_data if isinstance(clip_data, dict) else {}
    start = float(data.get("start", 0.0) or 0.0)
    end = float(data.get("end", 0.0) or 0.0)
    if end <= start + _SCENE_EPS and isinstance(file_data, dict):
        file_dur = float(file_data.get("duration", 0) or 0)
        if file_dur > start + _SCENE_EPS:
            end = file_dur
        else:
            f_start = float(file_data.get("start", 0.0) or 0.0)
            f_end = float(file_data.get("end", 0.0) or 0.0)
            if f_end > f_start + _SCENE_EPS:
                start, end = f_start, f_end
            elif file_dur > start + _SCENE_EPS:
                end = file_dur
    if end <= start + _SCENE_EPS:
        end = start + 0.1
    return start, end


def scene_source_time(
    scene: dict,
    metadata: Optional[dict] = None,
) -> float:
    """Map a scene entry to absolute root source seconds."""
    if not isinstance(scene, dict):
        return 0.0
    if scene.get("source_time") is not None:
        return float(scene["source_time"])
    t = float(scene.get("time", 0) or 0)
    if isinstance(metadata, dict):
        sw = metadata.get("source_window")
        if isinstance(sw, dict):
            ws = float(sw.get("start", 0) or 0)
            we = float(sw.get("end", 0) or 0)
            span = max(0.0, we - ws)
            # Rebasing stores clip-local times; reconstruct absolute when plausible.
            if span > 0 and t <= span + 1.0:
                return ws + t
    return t


def _filter_scenes_in_window(
    scenes: List[dict],
    start_time: float,
    end_time: float,
    *,
    rebased: bool,
    metadata: Optional[dict] = None,
    exclusive_start: bool = False,
) -> List[dict]:
    filtered: List[dict] = []
    for scene in scenes or []:
        if not isinstance(scene, dict) or not scene.get("description"):
            continue
        abs_time = scene_source_time(scene, metadata)
        if exclusive_start:
            if abs_time <= start_time + _SCENE_EPS:
                continue
        elif abs_time < start_time - _SCENE_EPS:
            continue
        if abs_time > end_time + _SCENE_EPS:
            continue
        entry: Dict[str, Any] = {
            "description": str(scene.get("description", "")),
            "source_time": abs_time,
            "time": max(0.0, abs_time - start_time) if rebased else abs_time,
        }
        filtered.append(entry)
    filtered.sort(key=lambda s: float(s.get("source_time", 0) or 0))
    return filtered


def _filter_tags_for_window(
    tags: dict,
    window_scenes: List[dict],
) -> dict:
    if not isinstance(tags, dict):
        return {"objects": [], "scenes": [], "activities": [], "mood": [], "quality": {}}
    corpus = " ".join(
        str(s.get("description", "")).lower() for s in window_scenes
    )
    filtered: Dict[str, Any] = {}
    for key in ("objects", "scenes", "activities", "mood"):
        vals = tags.get(key) or []
        if not isinstance(vals, list):
            filtered[key] = []
            continue
        kept = []
        for val in vals:
            text = str(val).strip()
            if not text:
                continue
            norm = text.lower()
            if norm in corpus or any(tok in corpus for tok in norm.split() if len(tok) > 2):
                kept.append(text)
        filtered[key] = kept
    filtered["quality"] = tags.get("quality", {}) if isinstance(tags.get("quality"), dict) else {}
    return filtered


def materialize_clip_ai_metadata(
    root_ai: Optional[dict],
    source_start: float,
    source_end: float,
    *,
    rebased: bool = True,
    exclusive_start: bool = False,
) -> Dict[str, Any]:
    """
    Build clip-local metadata from root file ai_metadata and a source trim window.

    Always filters by absolute source_time — never trusts rebased scene.time alone.
    """
    if not isinstance(root_ai, dict) or not root_ai.get("analyzed"):
        return {}

    filtered_scenes = _filter_scenes_in_window(
        root_ai.get("scene_descriptions") or [],
        source_start,
        source_end,
        rebased=rebased,
        metadata=root_ai,
        exclusive_start=exclusive_start,
    )
    tags = _filter_tags_for_window(
        root_ai.get("tags") if isinstance(root_ai.get("tags"), dict) else {},
        filtered_scenes,
    )

    return {
        "analyzed": True,
        "analysis_version": root_ai.get("analysis_version", "2.0"),
        "analysis_date": root_ai.get("analysis_date", ""),
        "provider": root_ai.get("provider", "gemini"),
        "scene_descriptions": filtered_scenes,
        "tags": tags,
        "faces": root_ai.get("faces", []) if rebased else copy.deepcopy(root_ai.get("faces", [])),
        "colors": copy.deepcopy(root_ai.get("colors", {})),
        "audio_analysis": copy.deepcopy(root_ai.get("audio_analysis", {})),
        "description": " ".join(s["description"] for s in filtered_scenes if s.get("description")),
        "confidence": root_ai.get("confidence", 0.0),
        "twelvelabs": copy.deepcopy(root_ai.get("twelvelabs", {})),
        "source_window": {"start": source_start, "end": source_end},
    }


def get_effective_ai_metadata(
    file_data: Optional[dict],
    clip_data: Optional[dict] = None,
    *,
    clip_ai_metadata: Optional[dict] = None,
    root_ai_metadata: Optional[dict] = None,
    rebased: bool = True,
) -> Dict[str, Any]:
    """
    Metadata corpus for a timeline placement's source trim window.

  Always derives from root parent ai_metadata + clip source window.
  Persisted clip_ai_metadata is validated and recomputed when source_window mismatches.
    """
    start_time, end_time = get_source_window(clip_data, file_data)

    root_ai = root_ai_metadata
    if root_ai is None and isinstance(file_data, dict):
        root_ai = file_data.get("ai_metadata")
    if not isinstance(root_ai, dict) or not root_ai.get("analyzed"):
        return {}

    # Recompute when clip metadata is missing, stale, or window-mismatched.
    if clip_metadata_is_valid(clip_ai_metadata, start_time, end_time):
        if rebased:
            scenes = clip_ai_metadata.get("scene_descriptions") or []
            return {
                **clip_ai_metadata,
                "scene_descriptions": [
                    {
                        "description": str(s.get("description", "")),
                        "source_time": float(s["source_time"]),
                        "time": max(0.0, float(s["source_time"]) - start_time),
                    }
                    for s in scenes
                    if isinstance(s, dict) and s.get("description")
                ],
                "source_window": {"start": start_time, "end": end_time},
            }
        return clip_ai_metadata

    return materialize_clip_ai_metadata(
        root_ai,
        start_time,
        end_time,
        rebased=rebased,
    )


def build_tags_preview(effective_metadata: Optional[dict], max_items: int = 5) -> str:
    """Compact tag string from effective metadata for agent listings."""
    if not isinstance(effective_metadata, dict):
        return ""
    tags = effective_metadata.get("tags") if isinstance(effective_metadata.get("tags"), dict) else {}
    parts = []
    for key in ("objects", "scenes", "activities"):
        vals = tags.get(key) or []
        if isinstance(vals, list):
            parts.extend(str(v).strip() for v in vals[:max_items] if v)
    if not parts:
        for sc in (effective_metadata.get("scene_descriptions") or [])[:2]:
            if isinstance(sc, dict) and sc.get("description"):
                parts.append(str(sc["description"])[:40])
    if not parts:
        desc = str(effective_metadata.get("description") or "").strip()
        if desc:
            parts.append(desc[:60])
    preview = ", ".join(parts[:max_items])
    if len(preview) > 80:
        preview = preview[:79].rstrip() + "…"
    return preview


def filter_tags_string_for_window(tags_str: str, effective_metadata: dict) -> str:
    """Filter comma-separated tags to those relevant to effective window."""
    if not tags_str:
        return build_tags_preview(effective_metadata)
    corpus = build_tags_preview(effective_metadata).lower()
    if not corpus:
        return tags_str
    kept = []
    for part in str(tags_str).split(","):
        token = part.strip()
        if not token:
            continue
        if token.lower() in corpus or any(w in corpus for w in token.lower().split() if len(w) > 2):
            kept.append(token)
    return ", ".join(kept) if kept else build_tags_preview(effective_metadata)


def is_ai_metadata_usable(ai_metadata: Dict[str, Any]) -> bool:
    """Return True when ai_metadata actually contains usable analysis content.

    The backend used to report ``analyzed=True`` even when the vision model
    failed, leaving empty objects/scenes/description/scene_descriptions. Such a
    result is worthless for the tags tab and chat, so we treat "analyzed but
    empty" as NOT usable — it should be re-tagged rather than trusted.
    """
    if not ai_metadata or not isinstance(ai_metadata, dict):
        return False
    if not ai_metadata.get("analyzed"):
        return False
    if ai_metadata.get("scene_descriptions"):
        return True
    if (ai_metadata.get("description") or "").strip():
        return True
    tags = ai_metadata.get("tags") or {}
    if isinstance(tags, dict):
        for key in ("objects", "scenes", "activities", "mood"):
            if tags.get(key):
                return True
    return False


def adjust_scene_descriptions_for_subclip(
    ai_metadata: Dict[str, Any],
    start_time: float,
    end_time: float,
) -> Dict[str, Any]:
    """Adjust scene descriptions in ai_metadata for a sub-clip."""
    return materialize_clip_ai_metadata(
        ai_metadata,
        start_time,
        end_time,
        rebased=True,
    )


def get_scene_descriptions_formatted(
    ai_metadata: Dict[str, Any],
    *,
    use_source_time: bool = False,
) -> List[str]:
    """Format scene descriptions with timestamps (clip-local or source)."""
    if not ai_metadata or not ai_metadata.get("analyzed"):
        return []

    scene_descriptions = ai_metadata.get("scene_descriptions", [])
    formatted = []
    sw = ai_metadata.get("source_window") if isinstance(ai_metadata.get("source_window"), dict) else None

    for scene in scene_descriptions:
        if not isinstance(scene, dict):
            continue
        if use_source_time:
            time_sec = scene_source_time(scene, ai_metadata)
        else:
            time_sec = float(scene.get("time", 0) or 0)
        description = scene.get("description", "")
        minutes = int(time_sec // 60)
        seconds = int(time_sec % 60)
        time_str = f"{minutes}:{seconds:02d}"
        prefix = "src " if use_source_time else ""
        formatted.append(f"[{prefix}{time_str}] {description}")

    return formatted


def collect_scene_descriptions_for_baked_segment(
    ai_metadata: Dict[str, Any],
    seg_start: float,
    seg_end: float,
    baked_offset: float,
) -> List[Dict[str, Any]]:
    """Map source-file scene times into a baked timeline segment."""
    if not ai_metadata or not isinstance(ai_metadata, dict):
        return []

    scenes: List[Dict[str, Any]] = []
    for scene in ai_metadata.get("scene_descriptions") or []:
        if not isinstance(scene, dict) or not scene.get("description"):
            continue
        scene_time = scene_source_time(scene, ai_metadata)
        if seg_start <= scene_time <= seg_end:
            scenes.append({
                "time": max(0.0, scene_time - seg_start + baked_offset),
                "source_time": scene_time,
                "description": str(scene.get("description", "")),
            })
    return scenes


def apply_metadata_to_clip_data(
    clip_data: dict,
    root_ai: Optional[dict],
    file_data: Optional[dict] = None,
    *,
    rebased: bool = True,
    exclusive_start: bool = False,
) -> None:
    """Write validated clip ai_metadata from root analysis and clip trim window."""
    if not isinstance(clip_data, dict) or not isinstance(root_ai, dict):
        return
    if not root_ai.get("analyzed"):
        return
    start, end = get_source_window(clip_data, file_data)
    clip_data["ai_metadata"] = materialize_clip_ai_metadata(
        root_ai,
        start,
        end,
        rebased=rebased,
        exclusive_start=exclusive_start,
    )
