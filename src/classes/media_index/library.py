"""
 @file
 @brief A file's saved index, loaded from the shelf and joined by time into one record per shot.

 Every layer keeps its own boundaries on a shared per-file timeline in seconds; here they are
 joined by shot: structure (cuts, camera motion), watch (what happens), look (colour), speech
 (what is said) and the vectors. Loading is cached (float32 vector matrices, bounded by rows), so
 searching a project is a handful of matrix products, not disk reads.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from classes.media_index import schema as S
from classes.media_index.store import Shelf

MAX_CACHED_ROWS = 250_000      # about 750 MB of float32 at 768 dims
_lock = threading.Lock()
_cache: "OrderedDict[tuple, FileIndex]" = OrderedDict()


@dataclass
class FileIndex:
    sha: str
    file_id: str = ""
    name: str = ""
    path: str = ""
    media_type: str = "video"
    duration: float = 0.0
    orientation: str = ""
    shots: List[Dict[str, Any]] = field(default_factory=list)
    sentences: List[Dict[str, Any]] = field(default_factory=list)
    audio: Dict[str, Any] = field(default_factory=dict)
    look_file: Dict[str, Any] = field(default_factory=dict)
    pipeline: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    text_rows: List[Dict[str, Any]] = field(default_factory=list)
    text_matrix: Optional[np.ndarray] = None
    image_rows: List[Dict[str, Any]] = field(default_factory=list)
    image_matrix: Optional[np.ndarray] = None
    layers: Dict[str, bool] = field(default_factory=dict)
    not_applicable: List[str] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return len(self.text_rows) + len(self.image_rows)

    def shot_at(self, t: float) -> Optional[Dict[str, Any]]:
        for s in self.shots:
            if s["start"] <= t < s["end"]:
                return s
        return self.shots[-1] if self.shots and t >= self.shots[-1]["end"] - 1e-6 else None

    def shots_between(self, start: float, end: float) -> List[Dict[str, Any]]:
        return [s for s in self.shots if s["end"] > start and s["start"] < end]


def _matrix(shelf: Shelf, sha: str, name: str, rows: List[Dict[str, Any]], dims: int) -> Optional[np.ndarray]:
    if not rows:
        return None
    blob = shelf.read_bytes(sha, name)
    if not blob or len(blob) != len(rows) * dims * 2:
        return None          # a truncated or mismatched file is treated as no vectors, never as wrong ones
    return np.frombuffer(blob, dtype="<f2").reshape(len(rows), dims).astype(np.float32)


def load_file_index(shelf: Shelf, sha: str, *, file_id: str = "", name: str = "", path: str = "",
                    media_type: str = "video") -> Optional[FileIndex]:
    """The joined index for one file, or None when nothing of it is on the shelf."""
    manifest = shelf.manifest(sha)
    if not manifest.get("layers"):
        return None
    ready = {layer: shelf.layer_ready(sha, layer, version=v) for layer, v in S.LAYER_VERSIONS.items()}
    source = manifest.get("source") or {}
    fi = FileIndex(sha=sha, file_id=file_id, name=name, path=path, media_type=source.get("media_type") or media_type,
                   duration=float(source.get("duration") or 0.0), orientation=str(source.get("orientation") or ""),
                   layers=ready, not_applicable=[name for name in S.LAYER_VERSIONS if (shelf.layer(sha, name) or {}).get("status") == S.NOT_APPLICABLE])
    structure = shelf.read_json(sha, "structure.json") if ready[S.LAYER_STRUCTURE] else None
    look = shelf.read_json(sha, "look.json") if ready[S.LAYER_LOOK] else None
    watch = shelf.read_json(sha, "watch.json") if ready[S.LAYER_WATCH] else None
    speech = shelf.read_json(sha, "speech.json") if ready[S.LAYER_SPEECH] else None
    if ready[S.LAYER_AUDIO]:
        fi.audio = shelf.read_json(sha, "audio.json") or {}
    if look:
        fi.look_file = (look.get("file") or {})
        fi.pipeline = look.get("pipeline") or {}
        fi.warnings = list(look.get("warnings") or [])
    look_by_id = {s["id"]: s for s in (look or {}).get("shots") or []}
    watch_by_id = {s["id"]: s for s in (watch or {}).get("shots") or []}
    per_shot = (speech or {}).get("per_shot") or {}
    for shot in (structure or {}).get("shots") or []:
        sid = int(shot["id"])
        lk = look_by_id.get(sid) or {}
        fi.shots.append({
            "id": sid, "start": float(shot["start"]), "end": float(shot["end"]),
            "duration": float(shot.get("duration") or (shot["end"] - shot["start"])),
            "opens_with": shot.get("opens_with"), "black": bool(shot.get("black")),
            "motion": shot.get("motion") or {}, "watch": watch_by_id.get(sid),
            "look": lk.get("profile"), "look_extras": lk.get("extras"),
            "speech": per_shot.get(str(sid)),
        })
    fi.sentences = list((speech or {}).get("sentences") or [])
    if ready[S.LAYER_VECTORS]:
        index = shelf.read_json(sha, "vectors_index.json") or {}
        dims = int(index.get("dims") or S.EMBED_DIMS)
        text_rows, image_rows = index.get("text") or [], index.get("image") or []
        tm = _matrix(shelf, sha, "vectors_text.f16", text_rows, dims)
        im = _matrix(shelf, sha, "vectors_image.f16", image_rows, dims)
        fi.text_rows, fi.text_matrix = (text_rows, tm) if tm is not None else ([], None)
        fi.image_rows, fi.image_matrix = (image_rows, im) if im is not None else ([], None)
    return fi


def get_file_index(shelf: Shelf, sha: str, **kw: Any) -> Optional[FileIndex]:
    """Cached ``load_file_index``; the cache is invalidated when the shelf's manifest changes."""
    try:
        stamp = max([float(r.get("updated") or 0) for r in shelf.manifest(sha).get("layers", {}).values()] or [0.0])
    except Exception:
        stamp = 0.0
    key = (shelf.root, sha, stamp)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return _with_identity(hit, kw)
    fi = load_file_index(shelf, sha, **kw)
    if fi is None:
        return None
    with _lock:
        for old in [k for k in _cache if k[:2] == key[:2]]:
            _cache.pop(old, None)
        _cache[key] = fi
        total = sum(v.rows for v in _cache.values())
        while total > MAX_CACHED_ROWS and len(_cache) > 1:
            _, evicted = _cache.popitem(last=False)
            total -= evicted.rows
    return fi


def _with_identity(fi: FileIndex, kw: Dict[str, Any]) -> FileIndex:
    """The cached index re-labelled for this project's file (same content, different file id)."""
    if all(getattr(fi, k, None) == v for k, v in kw.items() if v):
        return fi
    clone = FileIndex(**{**fi.__dict__})
    for k, v in kw.items():
        if v:
            setattr(clone, k, v)
    return clone


def clear_cache() -> None:
    with _lock:
        _cache.clear()
