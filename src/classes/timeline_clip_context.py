"""Timeline placement context: trim-aware metadata and index lookup per clip."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from classes.ai_metadata_utils import (
    build_tags_preview,
    get_effective_ai_metadata,
    get_source_window,
)
from classes.track_display import layer_number_to_display_index
from classes.twelvelabs_match import twelvelabs_is_indexed

# Per-request caches — cleared at start of bulk enumeration to avoid repeated File.get/filter.
_parent_id_cache: Dict[str, str] = {}
_parent_data_cache: Dict[str, dict] = {}
_file_data_cache: Dict[str, dict] = {}


def clear_metadata_lookup_cache() -> None:
    """Drop cached file/parent lookups (call after project mutations if needed)."""
    _parent_id_cache.clear()
    _parent_data_cache.clear()
    _file_data_cache.clear()


@dataclass
class TimelineClipContext:
    timeline_clip_id: str
    file_id: str
    parent_file_id: str
    source_path: str
    source_start: float
    source_end: float
    timeline_position: float
    timeline_duration: float
    timeline_end: float
    layer: Any
    ui_track: Optional[int]
    title: str
    file_name: str
    effective_metadata: Dict[str, Any] = field(default_factory=dict)
    tags_preview: str = ""
    index_status: str = ""
    score: float = 0.0

    def to_candidate_dict(self) -> Dict[str, Any]:
        return {
            "timeline_clip_id": self.timeline_clip_id,
            "title": self.title,
            "layer": self.layer,
            "position": self.timeline_position,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "score": self.score,
            "parent_file_id": self.parent_file_id,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "ui_track": self.ui_track,
            "tags_preview": self.tags_preview,
        }


def resolve_parent_file_id(file_data: Optional[dict], *, file_id: str = "") -> str:
    """Return root media-bin file id for indexing (follow parent_file_id chain)."""
    fid = str(file_id or (file_data or {}).get("id") or "")
    if fid and fid in _parent_id_cache:
        return _parent_id_cache[fid]

    if not isinstance(file_data, dict):
        result = fid
        if fid:
            _parent_id_cache[fid] = result
        return result

    explicit = str(file_data.get("parent_file_id") or "").strip()
    if explicit:
        _parent_id_cache[fid] = explicit
        return explicit
    if not file_data.get("zenvi_subclip"):
        result = fid or str(file_data.get("id") or "")
        if fid:
            _parent_id_cache[fid] = result
        return result

    from classes.query import File

    parent_id = str(file_data.get("parent_file_id") or "").strip()
    if parent_id:
        _parent_id_cache[fid] = parent_id
        return parent_id

    path = str(file_data.get("path") or "")
    if path and fid:
        for candidate in File.filter():
            if not isinstance(candidate.data, dict):
                continue
            if candidate.data.get("zenvi_subclip"):
                continue
            cand_path = str(candidate.data.get("path") or "")
            if cand_path and os.path.normpath(cand_path) == os.path.normpath(path):
                root = str(candidate.id or candidate.data.get("id") or "")
                _parent_id_cache[fid] = root
                return root

    result = fid or str(file_data.get("id") or "")
    if fid:
        _parent_id_cache[fid] = result
    return result


def resolve_parent_file_data(file_data: Optional[dict], *, file_id: str = "") -> Optional[dict]:
    """Load root parent file data dict for TwelveLabs / full metadata."""
    fid = str(file_id or (file_data or {}).get("id") or "")
    parent_id = resolve_parent_file_id(file_data, file_id=fid)
    if not parent_id:
        return file_data
    if parent_id == fid:
        return file_data
    if parent_id in _parent_data_cache:
        return _parent_data_cache[parent_id]

    from classes.query import File

    fobj = File.get(id=parent_id)
    if fobj and isinstance(fobj.data, dict):
        _parent_data_cache[parent_id] = fobj.data
        return fobj.data
    return file_data


def resolve_root_ai_metadata(
    file_data: Optional[dict],
    *,
    file_id: str = "",
) -> Tuple[Optional[dict], Optional[dict]]:
    """Return (root_ai_metadata dict, root_file_data dict)."""
    parent_data = resolve_parent_file_data(file_data, file_id=file_id)
    if not isinstance(parent_data, dict):
        return None, None
    root_ai = parent_data.get("ai_metadata")
    if isinstance(root_ai, dict) and root_ai.get("analyzed"):
        return root_ai, parent_data
    return None, parent_data


def _cached_file_data(file_id: str) -> Optional[dict]:
    if not file_id:
        return None
    if file_id in _file_data_cache:
        return _file_data_cache[file_id]
    from classes.query import File

    fobj = File.get(id=file_id)
    if fobj and isinstance(fobj.data, dict):
        _file_data_cache[file_id] = fobj.data
        return fobj.data
    return None


def build_timeline_clip_context(
    clip_obj: Any,
    clip_data: dict,
    file_data: Optional[dict],
    *,
    layers: Optional[list] = None,
    root_ai: Optional[dict] = None,
    parent_data: Optional[dict] = None,
) -> TimelineClipContext:
    fid = str(clip_data.get("file_id") or "")
    if parent_data is None:
        parent_data = resolve_parent_file_data(file_data, file_id=fid)
    parent_id = resolve_parent_file_id(file_data, file_id=fid)
    if root_ai is None:
        root_ai, _ = resolve_root_ai_metadata(file_data, file_id=fid)

    source_start, source_end = get_source_window(clip_data, file_data)
    timeline_position = float(clip_data.get("position", 0.0) or 0.0)
    timeline_duration = max(0.01, source_end - source_start)
    clip_ai = clip_data.get("ai_metadata") if isinstance(clip_data.get("ai_metadata"), dict) else None
    effective = get_effective_ai_metadata(
        parent_data or file_data,
        clip_data=clip_data,
        clip_ai_metadata=clip_ai,
        root_ai_metadata=root_ai,
        rebased=True,
    )
    path = ""
    if file_data:
        path = str(file_data.get("path") or "")
    layer = clip_data.get("layer")
    lid_int = None
    try:
        lid_int = int(layer) if layer is not None and layer != "" else None
    except (TypeError, ValueError):
        lid_int = None
    ui_track = layer_number_to_display_index(lid_int, layers or []) if lid_int is not None else None
    fname = str(
        (file_data or {}).get("name")
        or os.path.basename(path)
        or ""
    ).strip()
    title = str(clip_data.get("title") or clip_data.get("label") or fname or "Clip").strip()
    tl_meta = (parent_data or {}).get("twelvelabs") or (file_data or {}).get("twelvelabs") or {}
    if not isinstance(tl_meta, dict):
        tl_meta = {}
    index_status = str(tl_meta.get("status") or "")
    if twelvelabs_is_indexed(tl_meta):
        index_status = "ready"
    return TimelineClipContext(
        timeline_clip_id=str(clip_obj.id or clip_data.get("id", "")),
        file_id=fid,
        parent_file_id=parent_id,
        source_path=path,
        source_start=source_start,
        source_end=source_end,
        timeline_position=timeline_position,
        timeline_duration=timeline_duration,
        timeline_end=timeline_position + timeline_duration,
        layer=layer,
        ui_track=ui_track,
        title=title,
        file_name=fname,
        effective_metadata=effective,
        tags_preview=build_tags_preview(effective),
        index_status=index_status,
    )


def enumerate_timeline_contexts(*, layers: Optional[list] = None) -> List[TimelineClipContext]:
    from classes.query import Clip

    clear_metadata_lookup_cache()

    if layers is None:
        try:
            from classes.app import get_app
            layers = get_app().project.get("layers") or []
        except Exception:
            layers = []

    contexts: List[TimelineClipContext] = []
    for clip_obj in Clip.filter() or []:
        data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        fid = str(data.get("file_id") or "")
        file_data = _cached_file_data(fid) if fid else None
        root_ai, parent_data = resolve_root_ai_metadata(file_data, file_id=fid)
        contexts.append(
            build_timeline_clip_context(
                clip_obj,
                data,
                file_data,
                layers=layers,
                root_ai=root_ai,
                parent_data=parent_data,
            )
        )
    return contexts
