"""Locate the ffmpeg/ffprobe CLI for frozen installs and MSYS2 UCRT64."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from typing import Any, List, Optional, Sequence

# ffmpeg.exe is a console subsystem binary. A Win32GUI frozen app must hide that
# console or Windows flashes a terminal on every import/index probe.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


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


def _windows_kwargs(kwargs: dict) -> dict:
    if sys.platform != "win32":
        return kwargs
    out = dict(kwargs)
    out["creationflags"] = out.get("creationflags", 0) | _CREATE_NO_WINDOW
    startupinfo = out.get("startupinfo") or subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    out["startupinfo"] = startupinfo
    return out


def run_ffmpeg(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run for ffmpeg/ffprobe without flashing a Windows console."""
    return subprocess.run(resolve_ffmpeg_args(args), **_windows_kwargs(kwargs))


def run_ffmpeg_with_progress(
    args: Sequence[str],
    *,
    on_progress: Optional[Any] = None,
    should_cancel: Optional[Any] = None,
) -> subprocess.CompletedProcess:
    """Run ffmpeg, calling on_progress(fraction 0..1) from `-progress pipe:1`.

    stderr is discarded (not piped) so a full stderr buffer cannot deadlock ffmpeg
    while we wait on stdout progress lines. A reader thread + 50ms heartbeat keeps
    the GUI event loop alive even between progress updates.
    """
    cmd = list(resolve_ffmpeg_args(args))
    if "-progress" not in cmd:
        insert_at = max(1, len(cmd) - 1)
        cmd[insert_at:insert_at] = ["-nostats", "-progress", "pipe:1"]

    duration_us: Optional[float] = None
    if "-t" in cmd:
        try:
            duration_us = float(cmd[cmd.index("-t") + 1]) * 1_000_000.0
        except (ValueError, IndexError, TypeError):
            duration_us = None

    # DEVNULL stderr: never block ffmpeg on an unread pipe.
    popen_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "bufsize": 1,
    }
    proc = subprocess.Popen(cmd, **_windows_kwargs(popen_kwargs))
    last_frac = 0.0
    lines: queue.Queue = queue.Queue()

    def _reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=_reader, name="ffmpeg-progress", daemon=True).start()

    try:
        while True:
            if should_cancel is not None and should_cancel():
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="cancelled")

            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                # Heartbeat so callers can processEvents while ffmpeg works.
                if on_progress is not None:
                    on_progress(last_frac)
                if proc.poll() is not None and lines.empty():
                    break
                continue

            if line is None:
                break

            text = line.strip()
            if text.startswith("out_time_us="):
                try:
                    out_us = float(text.split("=", 1)[1])
                except (TypeError, ValueError):
                    continue
                if duration_us and duration_us > 0 and on_progress is not None:
                    last_frac = max(0.0, min(1.0, out_us / duration_us))
                    on_progress(last_frac)
            elif text.startswith("out_time_ms="):
                try:
                    out_ms = float(text.split("=", 1)[1])
                except (TypeError, ValueError):
                    continue
                if duration_us and duration_us > 0 and on_progress is not None:
                    last_frac = max(0.0, min(1.0, (out_ms * 1000.0) / duration_us))
                    on_progress(last_frac)
            elif text.startswith("progress=") and text.endswith("end"):
                last_frac = 1.0
                if on_progress is not None:
                    on_progress(1.0)

        returncode = proc.wait()
        if returncode == 0 and on_progress is not None and last_frac < 1.0:
            on_progress(1.0)
        return subprocess.CompletedProcess(cmd, returncode=returncode, stdout="", stderr="")
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
