"""OS file-drop and local-path import helpers.

Used by the Project Files views, the timeline, and agent ``import_files_tool``
so Finder drops and “import this folder” share the same path resolution.
"""

from __future__ import annotations

import glob as _glob
import os
import sys

# Card-view list is fitted to its contents; keep a drop zone when the bin is empty.
EMPTY_FILES_DROP_MIN_HEIGHT = 120

# Typical footage locations passed to Claude Code as ``--add-dir``.
_MEDIA_DIR_NAMES = (
    "Desktop",
    "Downloads",
    "Movies",
    "Videos",
    "Documents",
    "Pictures",
)

_FILE_FORMAT_HINTS = (
    "uri-list",
    "file-url",
    "filename",
    "promised-file",
    "nsfilenames",
)


def media_add_dirs(home: str | None = None) -> list[str]:
    """Existing well-known media folders under the user's home directory."""
    if not home:
        home = os.path.expanduser("~")
    if not home or home == "~":
        home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if not home:
        return []
    out = []
    for name in _MEDIA_DIR_NAMES:
        path = os.path.join(home, name)
        if os.path.isdir(path):
            out.append(os.path.abspath(path))
    return out


def _split_path_blob(text: str) -> list[str]:
    """Split a blob on newlines; split on commas only when the whole line is not a path."""
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        stripped = line.strip("'\"")
        expanded = os.path.expanduser(stripped)
        if os.path.exists(expanded) or "," not in line:
            if stripped:
                parts.append(stripped)
            continue
        for bit in line.split(","):
            bit = bit.strip().strip("'\"")
            if bit:
                parts.append(bit)
    return parts


def flatten_path_args(*values) -> list[str]:
    """Turn tool args (string, list, comma/newline-separated) into path strings."""
    chunks: list[str] = []
    for value in values:
        if value is None or value is False:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                chunks.extend(flatten_path_args(item))
            continue
        text = str(value).strip()
        if not text:
            continue
        chunks.extend(_split_path_blob(text))
    return chunks


def resolve_user_path(path: str, home: str | None = None) -> str:
    """Expand ``~`` and, for relative names, look under home / common media dirs."""
    raw = (path or "").strip().strip("'\"")
    if not raw:
        return ""
    if home is None:
        home = os.path.expanduser("~")
        if not home or home == "~":
            home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""

    expanded = os.path.expanduser(raw)
    if os.path.exists(expanded):
        return os.path.abspath(expanded)

    if os.path.isabs(expanded):
        return os.path.abspath(expanded)

    candidates = []
    if home:
        candidates.append(os.path.join(home, expanded))
        for folder in media_add_dirs(home):
            candidates.append(os.path.join(folder, expanded))
    for candidate in candidates:
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return os.path.abspath(expanded)


def collect_import_paths(raw_paths, home: str | None = None) -> tuple[list[str], list[str]]:
    """Expand files, directories, and globs into existing file paths.

    Returns ``(files, notes)`` where *notes* describes missing / unreadable paths.
    Directories are walked recursively. Duplicate paths are dropped.
    """
    files: list[str] = []
    notes: list[str] = []
    seen = set()

    def _add_file(path: str):
        path = os.path.abspath(path)
        if path in seen:
            return
        if not os.path.isfile(path):
            return
        seen.add(path)
        files.append(path)

    def _add_dir(path: str):
        try:
            for root, _, names in os.walk(path):
                for name in names:
                    _add_file(os.path.join(root, name))
        except OSError as exc:
            notes.append(f"Could not read directory {path}: {exc}")

    for raw in flatten_path_args(raw_paths):
        resolved = local_path_from_url(raw)
        if not resolved or not os.path.exists(resolved):
            resolved = resolve_user_path(raw, home=home)
        magic = _glob.has_magic(raw) or _glob.has_magic(resolved)
        if magic:
            matches = _glob.glob(resolved, recursive=True)
            if not matches:
                matches = _glob.glob(os.path.expanduser(raw), recursive=True)
            if not matches:
                notes.append(f"No files matched: {raw}")
                continue
            for match in matches:
                if os.path.isdir(match):
                    _add_dir(match)
                else:
                    _add_file(match)
            continue
        if os.path.isdir(resolved):
            _add_dir(resolved)
            continue
        if os.path.isfile(resolved):
            _add_file(resolved)
            continue
        notes.append(f"Not found: {raw}")

    files.sort()
    return files, notes


def _macos_posix_path_from_url_string(url_string: str) -> str:
    """Resolve ``file:///.file/id=…`` reference URLs via CoreFoundation."""
    if sys.platform != "darwin" or not url_string:
        return ""
    try:
        import ctypes
        import ctypes.util
        from ctypes import (
            c_bool, c_char_p, c_int, c_long, c_void_p, POINTER, create_string_buffer,
        )

        lib = ctypes.util.find_library("CoreFoundation")
        if not lib:
            return ""
        cf = ctypes.CDLL(lib)
        kCFStringEncodingUTF8 = 0x08000100
        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_int]
        cf.CFURLCreateWithString.restype = c_void_p
        cf.CFURLCreateWithString.argtypes = [c_void_p, c_void_p, c_void_p]
        cf.CFURLCreateFilePathURL.restype = c_void_p
        cf.CFURLCreateFilePathURL.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        cf.CFURLGetFileSystemRepresentation.restype = c_bool
        cf.CFURLGetFileSystemRepresentation.argtypes = [c_void_p, c_bool, c_char_p, c_long]
        cf.CFRelease.argtypes = [c_void_p]

        raw = url_string.encode("utf-8")
        cf_str = cf.CFStringCreateWithCString(None, raw, kCFStringEncodingUTF8)
        if not cf_str:
            return ""
        cf_url = cf.CFURLCreateWithString(None, cf_str, None)
        cf.CFRelease(cf_str)
        if not cf_url:
            return ""
        err = c_void_p()
        path_url = cf.CFURLCreateFilePathURL(None, cf_url, ctypes.byref(err))
        cf.CFRelease(cf_url)
        if not path_url:
            return ""
        buf = create_string_buffer(4096)
        ok = cf.CFURLGetFileSystemRepresentation(path_url, True, buf, 4096)
        cf.CFRelease(path_url)
        if ok and buf.value:
            path = buf.value.decode("utf-8", "replace")
            if path and os.path.exists(path):
                return os.path.abspath(path)
    except Exception:
        return ""
    return ""


def local_path_from_url(url) -> str:
    """Best-effort local filesystem path from a ``QUrl`` or string."""
    if url is None:
        return ""
    if isinstance(url, str):
        if url.startswith("file:") and "/.file/id=" in url:
            return _macos_posix_path_from_url_string(url)
        if url.startswith("file://"):
            try:
                from PyQt5.QtCore import QUrl
                return local_path_from_url(QUrl(url))
            except ImportError:
                stripped = url[7:]  # file://
                if stripped.startswith("/") and os.path.exists(stripped):
                    return os.path.abspath(stripped)
        return resolve_user_path(url)

    path = ""
    url_string = ""
    try:
        if hasattr(url, "toString"):
            url_string = url.toString() or ""
        scheme = ""
        if hasattr(url, "scheme"):
            scheme = (url.scheme() or "").lower()
        is_local = bool(hasattr(url, "isLocalFile") and url.isLocalFile())
        if not is_local and scheme not in ("file", ""):
            return ""
        if hasattr(url, "toLocalFile"):
            path = url.toLocalFile() or ""
        if not path and hasattr(url, "path"):
            candidate = url.path() or ""
            if candidate and not candidate.startswith("/.file/"):
                path = candidate
    except Exception:
        return ""

    if url_string and (path.startswith("/.file/") or "/.file/id=" in url_string):
        resolved = _macos_posix_path_from_url_string(url_string)
        if resolved:
            return resolved

    if not path:
        return ""
    path = os.path.expanduser(path)
    if os.path.exists(path):
        return os.path.abspath(path)
    return path


def mime_has_file_drop(mime) -> bool:
    """True when *mime* looks like an OS file drag, including macOS promised files."""
    if mime is None:
        return False
    try:
        if mime.hasUrls():
            return True
    except Exception:
        pass
    try:
        for fmt in mime.formats() or []:
            fl = str(fmt).lower()
            if any(hint in fl for hint in _FILE_FORMAT_HINTS):
                return True
    except Exception:
        pass
    return False


def urls_from_mime(mime):
    """``QUrl`` list for an OS file drop (urls, text/uri-list, or local-path text)."""
    try:
        from PyQt5.QtCore import QUrl
    except ImportError:
        return []

    if mime is None:
        return []

    urls = []
    seen = set()

    def _add(url):
        if url is None:
            return
        path = local_path_from_url(url)
        key = path or (url.toString() if hasattr(url, "toString") else str(url))
        if not key or key in seen:
            return
        seen.add(key)
        urls.append(url)

    try:
        if mime.hasUrls():
            for url in mime.urls() or []:
                _add(url)
    except Exception:
        pass

    try:
        if mime.hasFormat("text/uri-list"):
            raw = bytes(mime.data("text/uri-list")).decode("utf-8", "replace")
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                _add(QUrl(line))
    except Exception:
        pass

    try:
        if mime.hasText():
            text = mime.text() or ""
            for part in text.replace(",", "\n").splitlines():
                part = part.strip()
                if not part:
                    continue
                if part.startswith("file://"):
                    _add(QUrl(part))
                else:
                    resolved = resolve_user_path(part)
                    if resolved and os.path.exists(resolved):
                        _add(QUrl.fromLocalFile(resolved))
    except Exception:
        pass

    existing = [u for u in urls if os.path.exists(local_path_from_url(u))]
    return existing or urls


def accept_os_file_drag(event) -> bool:
    """Accept a drag if it looks like files. Returns False when the event is ignored."""
    try:
        from PyQt5.QtCore import Qt
    except ImportError:
        return False
    if event is None:
        return False
    mime = event.mimeData() if hasattr(event, "mimeData") else None
    if not mime_has_file_drop(mime):
        event.ignore()
        return False
    event.setDropAction(Qt.CopyAction)
    event.accept()
    return True
