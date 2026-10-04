"""
 @file
 @brief A spectrogram strip of a stretch of a file's audio, as a PNG an agent can look at.

 Made on first request from the original file (22.05 kHz mono, log frequency, log scale, with axes and a
 dBFS legend so the picture can be read without a key) and kept on the shelf, so asking again is free.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, Optional

from classes.ffmpeg_cli import run_ffmpeg
from classes.logger import log
from classes.media_index.store import Shelf

MAX_SECONDS = 90.0          # wider than this stops being readable in one strip
MIN_SECONDS = 0.5
SIZE = (1400, 420)
TIMEOUT_SECONDS = 120


def strip_name(start: float, end: float) -> str:
    return f"spectro_{int(round(start * 1000))}_{int(round(end * 1000))}.png"


def make_spectrogram(path: str, sha: str, shelf: Shelf, start: Optional[float], end: Optional[float], duration: float,
                     *, has_audio: bool = True) -> Dict[str, Any]:
    """{"ok", "path", "start", "end", "cached"} or {"ok": False, "error"}. Never raises."""
    if not has_audio:
        return {"ok": False, "error": "this file has no audio track"}
    lo = max(0.0, float(start or 0.0))
    hi = float(duration) if end is None else float(end)
    if duration:
        hi = min(hi, float(duration))
    if hi - lo < MIN_SECONDS:
        return {"ok": False, "error": "that range is too short to show (need at least half a second)"}
    if hi - lo > MAX_SECONDS:
        return {"ok": False, "error": f"that range is {hi - lo:.0f} s; a strip shows at most {MAX_SECONDS:.0f} s "
                                      f"(ask for start={lo:.0f}, end={lo + MAX_SECONDS:.0f}, then the next part)"}
    name = strip_name(lo, hi)
    cached = shelf.read_bytes(sha, name)
    entry = shelf.entry_dir(sha, create=True)
    if cached and entry:
        return {"ok": True, "path": os.path.join(entry, name), "start": lo, "end": hi, "cached": True}
    if not entry:
        return {"ok": False, "error": "no safe place to save the picture"}
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="spectro_")
    os.close(fd)
    try:
        args = ["ffmpeg", "-y", "-v", "error", "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}", "-i", path, "-vn", "-ac", "1",
                "-ar", "22050", "-lavfi",
                f"showspectrumpic=s={SIZE[0]}x{SIZE[1]}:legend=1:scale=log:fscale=log:color=intensity",
                "-frames:v", "1", tmp]
        proc = run_ffmpeg(args, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
        if proc.returncode != 0 or not os.path.getsize(tmp):
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg failed"]
            return {"ok": False, "error": "could not draw the spectrogram: " + tail[0][:200]}
        with open(tmp, "rb") as fh:
            blob = fh.read()
        if not shelf.write_bytes(sha, name, blob):
            return {"ok": False, "error": "could not save the picture"}
        return {"ok": True, "path": os.path.join(entry, name), "start": lo, "end": hi, "cached": False}
    except Exception as exc:  # noqa: BLE001 - reported to the agent, never raised into the tool call
        log.warning("Spectrogram of %s failed", path, exc_info=True)
        return {"ok": False, "error": f"could not draw the spectrogram: {exc}"[:240]}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
