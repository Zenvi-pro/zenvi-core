"""
 @file
 @brief Global fingerprint-keyed cache for thumbnails, waveforms, and AI metadata
"""

import json
import os
import shutil

from classes import info
from classes.logger import log

_CACHE_SUBDIR = "media"
_DEFAULT_LIMIT_MB = 2048


def media_cache_root():
    """Return ``USER_PATH/cache/media`` and create it if needed."""
    root = os.path.join(info.CACHE_PATH, _CACHE_SUBDIR)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError:
        log.error("Could not create media cache root %s", root, exc_info=1)
    return root


def _fingerprint_key(fingerprint):
    if isinstance(fingerprint, dict):
        return fingerprint.get("sha256") or ""
    if isinstance(fingerprint, str):
        return fingerprint
    return ""


def entry_dir(fingerprint):
    """Directory for one media fingerprint (or empty string if none)."""
    key = _fingerprint_key(fingerprint)
    if not key:
        return ""
    # Fingerprint keys are opaque digests — reject path traversal payloads.
    if (
        not key
        or key in (".", "..")
        or os.path.isabs(key)
        or os.path.sep in key
        or (os.path.altsep and os.path.altsep in key)
    ):
        log.warning("Rejected unsafe media cache fingerprint key")
        return ""
    root = media_cache_root()
    path = os.path.join(root, key)
    try:
        resolved = os.path.realpath(path)
        root_real = os.path.realpath(root)
        if os.path.commonpath([resolved, root_real]) != root_real:
            log.warning("Rejected media cache path outside root: %s", path)
            return ""
    except ValueError:
        return ""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        log.error("Could not create cache entry %s", path, exc_info=1)
        return ""
    return path


def preferred_thumbnail_path(file_id, frame, fingerprint=None, thumb_root=None):
    """Canonical write path for a thumbnail frame.

    Prefers fingerprint cache when available; otherwise ``THUMBNAIL_PATH/{id}/{frame}.png``.
    """
    frame = int(frame or 1)
    entry = entry_dir(fingerprint) if fingerprint else ""
    if entry:
        return os.path.join(entry, "thumbs", "%s.png" % frame)
    root = thumb_root or info.THUMBNAIL_PATH
    return os.path.join(root, str(file_id), "%s.png" % frame)


def resolve_thumbnail_path(file_id, frame, fingerprint=None, thumb_root=None):
    """Locate an existing thumbnail across fingerprint cache and legacy layouts.

    Search order:
    1. fingerprint cache ``thumbs/{frame}.png``
    2. ``THUMBNAIL_PATH/{file_id}/{frame}.png``
    3. ``THUMBNAIL_PATH/{file_id}.png`` (frame 1 legacy)
    4. ``THUMBNAIL_PATH/{file_id}-{frame}.png`` (legacy)
    Returns "" when nothing exists.
    """
    frame = int(frame or 1)
    candidates = []
    entry = entry_dir(fingerprint) if fingerprint else ""
    if entry:
        candidates.append(os.path.join(entry, "thumbs", "%s.png" % frame))
    root = thumb_root or info.THUMBNAIL_PATH
    subdir = os.path.join(root, str(file_id))
    candidates.append(os.path.join(subdir, "%s.png" % frame))
    if frame == 1:
        candidates.append(os.path.join(root, "%s.png" % file_id))
    else:
        candidates.append(os.path.join(root, "%s-%s.png" % (file_id, frame)))
    for path in candidates:
        if path and os.path.exists(path):
            if entry and path.startswith(entry):
                _touch(entry)
            return path
    return ""


def waveform_path(fingerprint):
    entry = entry_dir(fingerprint)
    if not entry:
        return ""
    return os.path.join(entry, "waveform.json")


def load_waveform(fingerprint):
    path = waveform_path(fingerprint)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _touch(path)
        entry = entry_dir(fingerprint)
        if entry:
            _touch(entry)
        return data
    except Exception:
        log.debug("Could not load waveform cache %s", path, exc_info=1)
        return None


def save_waveform(fingerprint, audio_data):
    path = waveform_path(fingerprint)
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"audio_data": audio_data}, fh)
        _touch(path)
        entry = entry_dir(fingerprint)
        if entry:
            _touch(entry)
        return True
    except Exception:
        log.error("Could not save waveform cache %s", path, exc_info=1)
        return False


def ai_metadata_path(fingerprint):
    entry = entry_dir(fingerprint)
    if not entry:
        return ""
    return os.path.join(entry, "ai_metadata.json")


def load_ai_metadata(fingerprint):
    path = ai_metadata_path(fingerprint)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _touch(path)
        entry = entry_dir(fingerprint)
        if entry:
            _touch(entry)
        return data
    except Exception:
        log.debug("Could not load ai_metadata cache %s", path, exc_info=1)
        return None


def save_ai_metadata(fingerprint, metadata):
    path = ai_metadata_path(fingerprint)
    if not path or not isinstance(metadata, dict):
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Keep index handles out of the bulky cache payload — callers store those in project JSON.
        cached = {k: v for k, v in metadata.items() if k not in ("index", "twelvelabs")}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cached, fh)
        _touch(path)
        entry = entry_dir(fingerprint)
        if entry:
            _touch(entry)
        return True
    except Exception:
        log.error("Could not save ai_metadata cache %s", path, exc_info=1)
        return False


def _touch(path):
    try:
        os.utime(path, None)
    except OSError:
        pass


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def cache_size_bytes():
    root = media_cache_root()
    if not os.path.isdir(root):
        return 0
    return _dir_size(root)


def _limit_bytes():
    try:
        from classes.app import get_app
        app = get_app()
        if app:
            settings = app.get_settings()
            if settings:
                mb = settings.get("media-cache-limit-mb")
                if mb is not None:
                    return max(64, int(mb)) * 1024 * 1024
    except Exception:
        pass
    return _DEFAULT_LIMIT_MB * 1024 * 1024


def evict_if_needed(limit_bytes=None):
    """LRU-evict fingerprint entries until the cache is under *limit_bytes*."""
    root = media_cache_root()
    if not os.path.isdir(root):
        return 0
    limit = limit_bytes if limit_bytes is not None else _limit_bytes()
    entries = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        entries.append((mtime, path, _dir_size(path)))
    total = sum(size for _m, _p, size in entries)
    if total <= limit:
        return 0
    entries.sort()  # oldest first
    removed = 0
    for _mtime, path, size in entries:
        if total <= limit:
            break
        try:
            shutil.rmtree(path)
            if os.path.exists(path):
                log.error("Media cache entry still present after eviction: %s", path)
                continue
            total -= size
            removed += 1
            log.info("Evicted media cache entry %s", path)
        except Exception:
            log.error("Failed to evict media cache entry %s", path, exc_info=1)
    return removed
