"""Locate the ffmpeg/ffprobe CLI for frozen installs and MSYS2 UCRT64."""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from typing import List, Optional, Sequence


def _exe_name(name: str) -> str:
    if sys.platform == "win32" and not name.lower().endswith(".exe"):
        return name + ".exe"
    return name


def _candidate_dirs() -> List[str]:
    dirs: List[str] = []
    extra = os.environ.get("FFMPEG_BIN_DIR") or os.environ.get("ZENVI_FFMPEG_DIR")
    if extra:
        dirs.append(extra)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        dirs.extend([os.path.join(exe_dir, "lib"), exe_dir])
    for prefix in (getattr(sys, "base_prefix", ""), sys.prefix):
        if prefix:
            dirs.append(os.path.join(prefix, "bin"))
    dirs.extend(
        [
            r"C:\msys64\ucrt64\bin",
            r"C:\msys64\mingw64\bin",
            r"C:\msys64\usr\bin",
            "/ucrt64/bin",
            "/mingw64/bin",
            "/usr/bin",
            "/usr/local/bin",
            "/opt/homebrew/bin",
        ]
    )
    return dirs


@lru_cache(maxsize=None)
def find_ffmpeg(name: str = "ffmpeg") -> Optional[str]:
    exe = _exe_name(name)
    found = shutil.which(name) or shutil.which(exe)
    if found:
        return found
    for directory in _candidate_dirs():
        if not directory:
            continue
        for candidate in (os.path.join(directory, exe), os.path.join(directory, name)):
            if os.path.isfile(candidate):
                return candidate
    return None


def ensure_ffmpeg_on_path() -> Optional[str]:
    """Prepend the ffmpeg directory to PATH so bare `ffmpeg` subprocesses resolve."""
    ff = find_ffmpeg("ffmpeg")
    if not ff:
        return None
    bindir = os.path.dirname(os.path.abspath(ff))
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if bindir and bindir not in parts:
        os.environ["PATH"] = bindir + os.pathsep + path
        find_ffmpeg.cache_clear()
    return ff


def resolve_ffmpeg_args(args: Sequence[str]) -> List[str]:
    out = list(args)
    if not out:
        return out
    tool = os.path.basename(str(out[0])).lower()
    if tool in ("ffmpeg", "ffmpeg.exe"):
        found = find_ffmpeg("ffmpeg")
        if found:
            out[0] = found
    elif tool in ("ffprobe", "ffprobe.exe"):
        found = find_ffmpeg("ffprobe")
        if found:
            out[0] = found
    return out
