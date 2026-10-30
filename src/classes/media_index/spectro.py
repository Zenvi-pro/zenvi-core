"""
 @file
 @brief A spectrogram strip of a stretch of a file's audio, as a PNG an agent can look at.

 Made on first request from the original file (22.05 kHz mono, log frequency, log scale, with axes and a
 dBFS legend so the picture can be read without a key) and kept on the shelf, so asking again is free.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, Optional, Sequence

from classes.ffmpeg_cli import run_ffmpeg
from classes.logger import log
from classes.media_index.store import Shelf

MAX_SECONDS = 60.0          # wider than this stops being readable in one strip
MAX_STRIPS = 4              # one call returns at most this many strips (four minutes of audio)
LOW_EDGE_CONFIDENCE = 0.45  # a section edge below this is uncertain: a gradual ramp's start (0.4); the weakest sharp change measured was 0.45 and right
SUGGEST_CONTEXT = 5.0       # seconds either side of an uncertain edge worth looking at
MIN_SECONDS = 0.5
SIZE = (1400, 420)
TIMEOUT_SECONDS = 120


def strip_name(start: float, end: float) -> str:
    return f"spectro_{int(round(start * 1000))}_{int(round(end * 1000))}.png"


def draw_strip(path: str, lo: float, hi: float, out_png: str) -> "tuple[bool, str]":
    """Draw [lo, hi) of *path*'s audio to *out_png* (nothing is kept or cached). Returns (True, "") or (False, why)."""
    args = ["ffmpeg", "-y", "-v", "error", "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}", "-i", path, "-vn", "-ac", "1",
            "-ar", "22050", "-lavfi", f"showspectrumpic=s={SIZE[0]}x{SIZE[1]}:legend=1:scale=log:fscale=log:color=intensity",
            "-frames:v", "1", out_png]
    try:
        proc = run_ffmpeg(args, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not draw the spectrogram: {exc}"[:240]
    if proc.returncode != 0 or not os.path.isfile(out_png) or not os.path.getsize(out_png):
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg failed"]
        return False, "could not draw the spectrogram: " + tail[0][:200]
    return True, ""


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
        drawn, why = draw_strip(path, lo, hi, tmp)
        if not drawn:
            return {"ok": False, "error": why}
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


def plan_strips(lo: float, hi: float, duration: float = 0.0) -> list:
    """The strips ([start, end] each at most ``MAX_SECONDS``) that cover [lo, hi); raises ValueError when more than ``MAX_STRIPS`` are needed."""
    lo = max(0.0, float(lo))
    hi = min(float(hi), float(duration)) if duration else float(hi)
    if hi - lo < MIN_SECONDS:
        raise ValueError("that range is too short to show (need at least half a second)")
    count = max(1, int(-(-(hi - lo) // MAX_SECONDS)))
    if count > MAX_STRIPS:
        raise ValueError(f"that range needs {count} strips and one call returns at most {MAX_STRIPS} ({MAX_STRIPS * MAX_SECONDS:.0f} s): ask for "
                         f"start={lo:.0f}, end={lo + MAX_STRIPS * MAX_SECONDS:.0f}, then the next part")
    step = (hi - lo) / count
    return [[round(lo + step * i, 3), round(lo + step * (i + 1), 3)] for i in range(count)]


def suggest(sections: Sequence[Dict[str, Any]], duration: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Where a picture of the sound would help: ranges around section edges the numbers are not sure of, or None.

    The numbers come first: loudness, beats, tempo and sections answer most questions. An edge is uncertain when it came from a
    gradual build or fade (the exact start of a ramp is a matter of degrees) or from a weak change. In a blind comparison of
    section finding, the spectrogram turned the last errors of the numbers into correct answers, and those errors were these edges.
    """
    weak = [s for s in sections[1:] if s.get("confidence") is not None and float(s["confidence"]) < LOW_EDGE_CONFIDENCE]
    if not weak:
        return None
    spans: list = []
    for s in weak:
        a, b = max(0.0, float(s["start"]) - SUGGEST_CONTEXT), float(s["start"]) + SUGGEST_CONTEXT
        if duration:
            b = min(b, float(duration))
        if spans and a <= spans[-1][1] + 1e-6:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([round(a, 2), round(b, 2)])
    return {"ranges": spans, "edges": [{"at": s["start"], "confidence": s["confidence"], "source": s.get("source")} for s in weak],
            "why": f"{len(weak)} section edge(s) come from a gradual build or fade or a weak change, so their exact place is uncertain: look at the spectrogram "
                   "of these ranges (view_audio_tool) before cutting to them"}
