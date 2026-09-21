"""OS file-drop and local-path import helpers.

Used by the Project Files views, the timeline, and agent ``import_files_tool``
so Finder drops and “import this folder” share the same path resolution.
"""

from __future__ import annotations

import glob as _glob
import os
import re
import sys

# Card-view list is fitted to its contents; keep a drop zone when the bin is empty.
EMPTY_FILES_DROP_MIN_HEIGHT = 120

# Git Bash / MSYS / Cygwin paths agents often paste on Windows.
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])(/.*)?$")
_CYGDRIVE_RE = re.compile(r"^/cygdrive/([A-Za-z])(/.*)?$", re.IGNORECASE)

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


def msys_path_to_windows(path: str) -> str | None:
    """Map ``/c/Users/...`` or ``/cygdrive/c/...`` to ``C:\\Users\\...``.

    Returns ``None`` when *path* is not an MSYS/Cygwin drive path. Pure helper —
    callers decide when to apply it (typically ``os.name == "nt"``).
    """
    raw = (path or "").strip().strip("'\"")
    if not raw:
        return None
    # Agents often mix separators; normalize before the drive regex.
    posix = raw.replace("\\", "/")
    match = _CYGDRIVE_RE.match(posix) or _MSYS_DRIVE_RE.match(posix)
    if not match:
        return None
    drive = match.group(1).upper()
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{drive}:{rest}" if rest else f"{drive}:\\"


def _running_on_windows() -> bool:
    return os.name == "nt"


def _windows_drive_root_exists(drive: str) -> bool:
    """True when ``drive`` (e.g. ``C:``) is mounted. Isolated for unit tests."""
    return bool(drive) and os.path.exists(drive + "\\")


def _msys_windows_path_if_usable(path: str) -> str | None:
    """Convert MSYS/Cygwin paths only when the target drive exists on Windows.

    Avoids turning Unix paths like ``/Users/...`` into ``U:\\sers\\...``.
    """
    if not _running_on_windows():
        return None
    converted = msys_path_to_windows(path)
    if not converted:
        return None
    # ntpath so drive letters parse correctly on POSIX hosts (unit tests).
    import ntpath

    drive = ntpath.splitdrive(converted)[0]
    if _windows_drive_root_exists(drive):
        return converted
    return None


def normalize_agent_fs_path(path: str, home: str | None = None) -> str:
    """Normalize an agent-supplied filesystem path for import / exists checks.

    Handles ``file://`` URLs, ``~``, relative media-folder names, native
    Windows paths, and common Git Bash / MSYS ``/c/...`` forms when running
    on Windows. Prefer forward-slash Windows paths in tool args
    (``C:/Users/...``) so JSON backslash escapes cannot mangle them.
    """
    raw = (path or "").strip().strip("'\"")
    if not raw:
        return ""
    on_windows = _running_on_windows()
    if raw.lower().startswith("file:"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(raw)
        path_part = unquote(parsed.path or "")
        # file:///C:/Users/... → /C:/Users/... on some parsers
        if on_windows and re.match(r"^/[A-Za-z]:", path_part):
            path_part = path_part.lstrip("/")
        elif on_windows:
            converted = _msys_windows_path_if_usable(path_part)
            if converted:
                path_part = converted
        raw = path_part or raw
    elif on_windows:
        converted = _msys_windows_path_if_usable(raw)
        if converted:
            raw = converted
    return resolve_user_path(raw, home=home)


def resolve_user_path(path: str, home: str | None = None) -> str:
    """Expand ``~`` and, for relative names, look under home / common media dirs."""
    raw = (path or "").strip().strip("'\"")
    if not raw:
        return ""
    if home is None:
        home = os.path.expanduser("~")
        if not home or home == "~":
            home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""

    # Native Windows paths with backslashes: normalize separators early so
    # exists/isabs checks are consistent across MSYS and Win32 Python.
    if os.name == "nt" and re.match(r"^[A-Za-z]:[\\/]", raw):
        raw = os.path.normpath(raw)

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


# Cap adjacent guesses so import never becomes a whole-disk search.
_ADJACENT_CANDIDATE_CAP = 5
_ADJACENT_MAX_DISTANCE = 2


def _norm_name_key(name: str) -> str:
    """Collapse case and hyphen/underscore/space so dirty_test ≈ dirty-test."""
    return re.sub(r"[-_\s]+", "_", (name or "").strip().lower())


def _edit_distance(a: str, b: str, limit: int = _ADJACENT_MAX_DISTANCE) -> int:
    """Levenshtein distance with early exit when above *limit*."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    # Ensure a is the shorter row for less memory.
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for j, bj in enumerate(b, start=1):
        cur = [j]
        row_min = j
        for i, ai in enumerate(a, start=1):
            ins = cur[i - 1] + 1
            delete = prev[i] + 1
            sub = prev[i - 1] + (0 if ai == bj else 1)
            val = min(ins, delete, sub)
            cur.append(val)
            if val < row_min:
                row_min = val
        if row_min > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def names_are_adjacent(wanted: str, actual: str) -> bool:
    """True when *actual* is a close spelling of *wanted* (typo / separator).

    Uses normalized equality or edit distance ≤ 2. Deliberately does **not**
    use broad prefix matching (that wrongly maps ``Down`` → ``Downloads``).
    """
    w = (wanted or "").strip()
    a = (actual or "").strip()
    if not w or not a:
        return False
    if w == a or w.lower() == a.lower():
        return True
    w_stem, w_ext = os.path.splitext(w)
    a_stem, a_ext = os.path.splitext(a)
    # Different extensions → different files (clip.mp4 must not become clip.mov).
    if w_ext and a_ext and w_ext.lower() != a_ext.lower():
        return False
    wk, ak = _norm_name_key(w), _norm_name_key(a)
    if not wk or not ak:
        return False
    if wk == ak:
        return True
    if _edit_distance(wk, ak) <= _ADJACENT_MAX_DISTANCE:
        return True
    # File typos: compare stems when extensions match (ree.mp4 ≈ reel.mp4).
    if w_ext and a_ext and w_ext.lower() == a_ext.lower():
        wsk, ask = _norm_name_key(w_stem), _norm_name_key(a_stem)
        if wsk and ask and (
            wsk == ask or _edit_distance(wsk, ask) <= _ADJACENT_MAX_DISTANCE
        ):
            return True
    return False


def _adjacent_distance(wanted: str, actual: str) -> int:
    """Sort key for adjacent candidates (lower is closer)."""
    wk = _norm_name_key(os.path.basename(wanted))
    ak = _norm_name_key(os.path.basename(actual))
    if wk == ak:
        return 0
    dist = _edit_distance(wk, ak, limit=8)
    w_stem, w_ext = os.path.splitext(wanted)
    a_stem, a_ext = os.path.splitext(actual)
    if w_ext and a_ext and w_ext.lower() == a_ext.lower():
        stem_dist = _edit_distance(_norm_name_key(w_stem), _norm_name_key(a_stem), limit=8)
        dist = min(dist, stem_dist)
    return dist


def _listdir_safe(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def find_adjacent_paths(path: str, home: str | None = None) -> list[str]:
    """After an exact miss: siblings in the parent dir + basename under media dirs.

    Returns at most ``_ADJACENT_CANDIDATE_CAP`` absolute paths, closest first.
    Does not walk the whole home tree.
    """
    raw = (path or "").strip().strip("'\"")
    if not raw:
        return []
    if home is None:
        home = os.path.expanduser("~")
        if not home or home == "~":
            home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""

    # Prefer the normalized absolute form even when it does not exist yet.
    normalized = normalize_agent_fs_path(raw, home=home) or raw
    wanted = os.path.basename(normalized.rstrip("\\/"))
    if not wanted or wanted in (".", ".."):
        return []

    found: list[str] = []
    seen: set[str] = set()
    media_roots = {os.path.abspath(p) for p in media_add_dirs(home)}
    home_abs = os.path.abspath(home) if home else ""

    def _consider(candidate: str):
        if not candidate or not os.path.exists(candidate):
            return
        abs_path = os.path.abspath(candidate)
        if abs_path in seen:
            return
        # Never remap a typo onto the entire Desktop/Downloads/… root or $HOME
        # (e.g. Down → Downloads would import a whole user library).
        if home_abs and abs_path == home_abs:
            return
        name = os.path.basename(abs_path)
        if abs_path in media_roots and _norm_name_key(name) != _norm_name_key(wanted):
            return
        if not names_are_adjacent(wanted, name):
            return
        seen.add(abs_path)
        found.append(abs_path)

    parent = os.path.dirname(normalized)
    if parent and os.path.isdir(parent):
        for name in _listdir_safe(parent):
            _consider(os.path.join(parent, name))

    search_roots: list[str] = []
    if home and os.path.isdir(home):
        search_roots.append(os.path.abspath(home))
    for folder in media_add_dirs(home):
        root = os.path.abspath(folder)
        if root not in search_roots:
            search_roots.append(root)

    for root in search_roots:
        _consider(os.path.join(root, wanted))
        for name in _listdir_safe(root):
            _consider(os.path.join(root, name))

    found.sort(key=lambda p: (_adjacent_distance(wanted, os.path.basename(p)), p))
    return found[:_ADJACENT_CANDIDATE_CAP]


def resolve_agent_import_target(path: str, home: str | None = None) -> dict:
    """Resolve a user/agent path for import: exact first, then adjacent.

    Returns a dict:
    - ``status``: ``ok`` | ``ambiguous`` | ``missing``
    - ``path``: absolute path when status is ``ok``
    - ``match``: ``exact`` or ``adjacent`` when ok
    - ``from``: original input when match is adjacent
    - ``candidates``: list when ambiguous
    - ``tried``: human-readable hint when missing
    """
    raw = (path or "").strip().strip("'\"")
    if not raw:
        return {"status": "missing", "tried": "(empty path)"}

    exact = normalize_agent_fs_path(raw, home=home)
    if exact and os.path.exists(exact):
        return {"status": "ok", "path": os.path.abspath(exact), "match": "exact"}

    adjacent = find_adjacent_paths(raw, home=home)
    # Drop the non-existent exact path if it somehow appeared.
    adjacent = [p for p in adjacent if os.path.exists(p)]
    if len(adjacent) == 1:
        return {
            "status": "ok",
            "path": adjacent[0],
            "match": "adjacent",
            "from": raw,
        }
    if len(adjacent) > 1:
        return {"status": "ambiguous", "candidates": adjacent, "from": raw}

    tried = exact or raw
    return {
        "status": "missing",
        "tried": tried,
        "from": raw,
    }


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
        resolved = normalize_agent_fs_path(raw, home=home)
        if not resolved or not os.path.exists(resolved):
            # Fall back for non-file:// strings that local_path_from_url handles.
            resolved = local_path_from_url(raw) or resolve_user_path(raw, home=home)
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
