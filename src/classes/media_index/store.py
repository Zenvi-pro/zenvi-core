"""
 @file
 @brief The shelf: one never-evicted folder per media file content.

 Layout, under ``<root>/<sha256>/``::

     manifest.json   schema_version, per-layer {version, status, model, updated, ...}
     v1_index.json   the v1 (Gemini chapters/cues/moments) ai_metadata, durable
     <layer>.json    later layers (facts, colour, watch, ...) share the same helpers

 Why not ``media_cache``: that cache is an LRU with a size limit, so paid index data
 could be evicted and bought again. The shelf is never trimmed automatically.

 Thread-safe inside one process (an RLock guards each manifest read-modify-write) and
 crash-safe: every file is written to a temp name in the same folder, fsynced and
 renamed over the target, so a reader never sees half a file.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from classes.logger import log

SCHEMA_VERSION = 1
LAYER_V1 = "v1_index"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.json$")
_BIN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(json|f16|png|jpg)$")
_LAYER_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_MANIFEST = "manifest.json"
_tmp_counter = itertools.count()


def _dict(value: Any) -> Dict[str, Any]:
    """*value* when it is a dict, else an empty one (JSON on disk can hold anything)."""
    return value if isinstance(value, dict) else {}


def sha_of(fingerprint: Any) -> str:
    """The sha256 key of a fingerprint dict (or bare digest); '' when it is not a valid key.

    Strict on purpose: the digest becomes a folder name, so anything that is not 64
    lowercase hex characters (a path, '..', an absolute path) is refused.
    """
    key = fingerprint.get("sha256") if isinstance(fingerprint, dict) else fingerprint
    key = str(key or "").strip().lower()
    return key if _SHA_RE.match(key) else ""


def _atomic_write_bytes(path: str, data: bytes) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".%s.%d.%d.%d.partial" % (
        os.path.basename(path), os.getpid(), threading.get_ident(), next(_tmp_counter)))
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _atomic_write_json(path: str, data: Any) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".%s.%d.%d.%d.partial" % (
        os.path.basename(path), os.getpid(), threading.get_ident(), next(_tmp_counter)))
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class Shelf:
    """The per-user, fingerprint-keyed media index store."""

    def __init__(self, root: Optional[str] = None) -> None:
        self.root = root or _default_root()
        self._lock = threading.RLock()

    # -- locations ---------------------------------------------------------
    def entry_dir(self, fingerprint: Any, *, create: bool = False) -> str:
        key = sha_of(fingerprint)
        if not key:
            return ""
        path = os.path.join(self.root, key)
        if create:
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                log.error("Could not create media index entry %s", path, exc_info=True)
                return ""
        return path

    def has_entry(self, fingerprint: Any) -> bool:
        path = self.entry_dir(fingerprint)
        return bool(path) and os.path.isfile(os.path.join(path, _MANIFEST))

    def list_entries(self) -> List[str]:
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        return sorted(n for n in names if _SHA_RE.match(n)
                      and os.path.isfile(os.path.join(self.root, n, _MANIFEST)))

    # -- plain json files in an entry ------------------------------------------
    def read_json(self, fingerprint: Any, name: str) -> Optional[Any]:
        path = self._file(fingerprint, name)
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            log.warning("Unreadable media index file %s", path, exc_info=True)
            return None

    def write_json(self, fingerprint: Any, name: str, data: Any) -> bool:
        if not self.entry_dir(fingerprint, create=True):
            return False
        path = self._file(fingerprint, name)
        if not path:
            return False
        try:
            with self._lock:
                _atomic_write_json(path, data)
            return True
        except Exception:
            log.error("Could not write media index file %s", path, exc_info=True)
            return False

    def write_bytes(self, fingerprint: Any, name: str, data: bytes) -> bool:
        """Write a binary file (vectors, images) into an entry, atomically."""
        if not self.entry_dir(fingerprint, create=True) or not _BIN_RE.match(str(name or "")):
            return False
        try:
            with self._lock:
                _atomic_write_bytes(os.path.join(self.entry_dir(fingerprint), name), bytes(data))
            return True
        except Exception:
            log.error("Could not write media index file %s", name, exc_info=True)
            return False

    def read_bytes(self, fingerprint: Any, name: str) -> Optional[bytes]:
        entry = self.entry_dir(fingerprint)
        if not entry or not _BIN_RE.match(str(name or "")):
            return None
        path = os.path.join(entry, name)
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def _file(self, fingerprint: Any, name: str) -> str:
        entry = self.entry_dir(fingerprint)
        if not entry or not _NAME_RE.match(str(name or "")):
            return ""
        return os.path.join(entry, name)

    # -- manifest and layers ------------------------------------------------------
    def manifest(self, fingerprint: Any) -> Dict[str, Any]:
        data = self.read_json(fingerprint, _MANIFEST)
        if not isinstance(data, dict):
            return {"schema_version": SCHEMA_VERSION, "layers": {}}
        if not isinstance(data.get("layers"), dict):
            data["layers"] = {}
        return data

    def set_layer(self, fingerprint: Any, layer: str, *, version: int, status: str,
                  **extra: Any) -> bool:
        """Record a layer's state (ready / failed / running) in the manifest."""
        if not _LAYER_RE.match(str(layer or "")):
            return False
        with self._lock:
            manifest = self.manifest(fingerprint)
            manifest["schema_version"] = SCHEMA_VERSION
            row = {"version": int(version), "status": str(status), "updated": time.time()}
            row.update({k: v for k, v in extra.items() if v is not None})
            manifest["layers"][layer] = row
            return self.write_json(fingerprint, _MANIFEST, manifest)

    def set_source(self, fingerprint: Any, **source: Any) -> bool:
        """Record what the file looked like when indexed (duration, size, media type)."""
        with self._lock:
            manifest = self.manifest(fingerprint)
            current = _dict(manifest.get("source"))
            current.update({k: v for k, v in source.items() if v is not None})
            manifest["source"] = current
            manifest["schema_version"] = SCHEMA_VERSION
            return self.write_json(fingerprint, _MANIFEST, manifest)

    def layer(self, fingerprint: Any, layer: str) -> Optional[Dict[str, Any]]:
        row = self.manifest(fingerprint)["layers"].get(layer)
        return row if isinstance(row, dict) else None

    def layer_ready(self, fingerprint: Any, layer: str, *, version: Optional[int] = None) -> bool:
        row = self.layer(fingerprint, layer)
        if not row or row.get("status") != "ready":
            return False
        return version is None or int(row.get("version") or 0) == int(version)

    # -- the v1 (Gemini chapters/cues/moments) index ------------------------------
    def save_v1_index(self, fingerprint: Any, ai_metadata: Dict[str, Any], *,
                      duration: float = 0.0, media_type: str = "video",
                      project_id: str = "", file_id: str = "") -> bool:
        """Keep a finished v1 analysis for good, so no other project pays for it again."""
        if not isinstance(ai_metadata, dict) or not ai_metadata.get("analyzed"):
            return False
        ok = self.write_json(fingerprint, "v1_index.json", {
            "ai_metadata": ai_metadata,
            "source": {"duration": float(duration or 0.0), "media_type": str(media_type or "video"),
                       "project_id": str(project_id or ""), "file_id": str(file_id or "")},
            "saved_at": time.time(),
        })
        if not ok:
            return False
        self.set_source(fingerprint, duration=float(duration or 0.0), media_type=str(media_type or "video"))
        return self.set_layer(fingerprint, LAYER_V1, version=1, status="ready")

    def load_v1_index(self, fingerprint: Any) -> Optional[Dict[str, Any]]:
        if not self.layer_ready(fingerprint, LAYER_V1, version=1):
            return None
        data = self.read_json(fingerprint, "v1_index.json")
        if not isinstance(data, dict) or not isinstance(data.get("ai_metadata"), dict):
            return None
        return data

    # -- moving entries between the shelf and a project ----------------------------
    def export_entries(self, fingerprints: Iterable[Any], dest_root: str) -> List[str]:
        """Copy entries into *dest_root* (a project's index folder); returns the keys copied."""
        copied: List[str] = []
        for fp in fingerprints:
            key = sha_of(fp)
            src = self.entry_dir(key)
            if not key or not src or not os.path.isfile(os.path.join(src, _MANIFEST)):
                continue
            if _merge_entry(src, os.path.join(dest_root, key)):
                copied.append(key)
        return copied

    def import_entries(self, src_root: str) -> List[str]:
        """Bring a project's index folder into the shelf; layers the shelf lacks (or has older) win."""
        imported: List[str] = []
        try:
            names = os.listdir(src_root)
        except OSError:
            return imported
        for key in sorted(n for n in names if _SHA_RE.match(n)):
            src = os.path.join(src_root, key)
            if not os.path.isfile(os.path.join(src, _MANIFEST)):
                continue
            with self._lock:
                if _merge_entry(src, os.path.join(self.root, key)):
                    imported.append(key)
        return imported


def _read_manifest_file(folder: str) -> Dict[str, Any]:
    try:
        with open(os.path.join(folder, _MANIFEST), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge_entry(src: str, dst: str) -> bool:
    """Copy *src* into *dst*, layer by layer: a layer is taken when *dst* lacks it or has an
    older one. Returns True when anything changed. Never deletes anything in *dst*."""
    src_manifest = _read_manifest_file(src)
    src_layers = _dict(src_manifest.get("layers"))
    dst_manifest = _read_manifest_file(dst) if os.path.isdir(dst) else {}
    dst_layers = _dict(dst_manifest.get("layers"))
    changed = False
    os.makedirs(dst, exist_ok=True)
    for layer, row in src_layers.items():
        if not isinstance(row, dict) or not _LAYER_RE.match(str(layer)):
            continue
        mine = dst_layers.get(layer)
        if isinstance(mine, dict) and float(mine.get("updated") or 0) >= float(row.get("updated") or 0):
            continue
        for name in _layer_files(src, layer):
            try:
                shutil.copy2(os.path.join(src, name), os.path.join(dst, name))
            except OSError:
                log.warning("Could not copy media index file %s", name, exc_info=True)
                break
        else:
            dst_layers[layer] = row
            changed = True
    if changed:
        merged = dict(dst_manifest or src_manifest)
        merged["schema_version"] = SCHEMA_VERSION
        merged["layers"] = dst_layers
        src_source = _dict(src_manifest.get("source"))
        if src_source:
            source = dict(_dict(merged.get("source")))
            source.update(src_source)
            merged["source"] = source
        _atomic_write_json(os.path.join(dst, _MANIFEST), merged)
    return changed


def _layer_files(folder: str, layer: str) -> List[str]:
    """The files that belong to *layer*: ``<layer>.json`` and ``<layer>_*``, plus a v1 alias."""
    names = []
    try:
        listing = os.listdir(folder)
    except OSError:
        return names
    for name in listing:
        if name == _MANIFEST or name.startswith("."):
            continue
        base = os.path.splitext(name)[0]
        if base == layer or name.startswith(layer + "_") or (layer == LAYER_V1 and base == "v1_index"):
            if os.path.isfile(os.path.join(folder, name)):
                names.append(name)
    return names


def _default_root() -> str:
    try:
        from classes import info
        base = getattr(info, "USER_PATH", None) or os.path.join(os.path.expanduser("~"), ".openshot_qt")
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".openshot_qt")
    return os.path.join(base, "media_index")


_default: Optional[Shelf] = None
_default_lock = threading.Lock()


def default_shelf() -> Shelf:
    """The process-wide shelf under ``USER_PATH/media_index`` (follows a changed USER_PATH)."""
    global _default
    root = _default_root()
    with _default_lock:
        if _default is None or _default.root != root:
            _default = Shelf(root)
        return _default
