"""
 @file
 @brief The exact frame of a cut, found on demand at the file's own frame rate.

 The index finds shot boundaries from ten small frames a second, which is good to about a tenth of a second. To cut cleanly an
 editor needs the frame. ``refine_cut`` decodes a short window around a boundary at every frame (with each frame's real
 timestamp, so variable frame rate is handled) and finds the one step where the picture changes: the first frame of the new
 shot for a hard cut, the whole span for a dissolve or a fade. It is computed only for the range about to be cut, never for
 every cut of every file, and the answer is measured, not guessed.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from classes.ffmpeg_cli import run_ffmpeg
from classes.media_index import schema as S
from classes.media_index import structure as st

DEFAULT_RADIUS = 0.5
MAX_RADIUS = 2.0
MAX_FRAMES = 240              # a window never holds more frames than this (a 120 fps clip still gets 2 s)
SPIKE_RATIO = 0.5             # neighbours below this share of the biggest step make it an isolated spike: a hard cut
FLASH_SECONDS = 0.2           # a bright flash lasts no longer than this before the old picture returns
EDGE_CONTEXT_SECONDS = 0.3    # a step found this close to the window's edge is looked at again with this much more picture around it
_PTS = re.compile(r"pts_time:\s*(-?[\d.]+)")


def decode_window(path: str, start: float, end: float, size: Tuple[int, int], *, start_time: float = 0.0
                  ) -> Tuple[np.ndarray, List[float]]:
    """Every frame of [start, end) as small grey pictures and their timestamps (seconds from the file's start).

    Raises RuntimeError when ffmpeg cannot read the file. Frames are not dropped or repeated, so the numbers are the file's own.
    """
    w, h = size
    length = max(0.05, float(end) - float(start))
    cmd = ["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{max(0.0, start):.3f}", "-copyts", "-t", f"{length:.3f}", "-i", path, "-an",
           "-vf", f"scale={w}:{h}:flags=area,format=gray,showinfo", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = run_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    raw = proc.stdout or b""
    times = [float(m.group(1)) - start_time for m in _PTS.finditer((proc.stderr or b"").decode("utf-8", "replace"))]
    if proc.returncode != 0 or not raw or not times:
        raise RuntimeError((proc.stderr or b"could not decode the picture").decode("utf-8", "replace")[-200:])
    count = min(len(times), len(raw) // (w * h))
    frames = np.frombuffer(raw[: count * w * h], dtype=np.uint8).reshape(count, h, w)
    # ffmpeg may read past the end with timestamps preserved: keep only the frames whose own time is inside the window
    lo, hi = start - start_time, end - start_time
    keep = [i for i in range(count) if lo - 1e-3 <= times[i] < hi - 1e-3][:MAX_FRAMES]
    if not keep:
        raise RuntimeError("no frames in that window")
    return frames[keep], [times[i] for i in keep]


def step_scores(frames: np.ndarray) -> List[Dict[str, float]]:
    """For each pair of neighbouring frames, how much the picture changed: pixel difference ``a``, histogram change ``b``,
    and ``score`` (the larger of each over its cut threshold, so 1.0 is at the edge of being a cut)."""
    out: List[Dict[str, float]] = []
    prev_hist: Optional[np.ndarray] = None
    for i in range(len(frames)):
        hist = st.gray_hist32(frames[i])
        if i:
            a, b = st.cut_features(frames[i - 1], frames[i], prev_hist, hist)
            out.append({"a": float(a), "b": float(b), "score": float(max(a / st.CUT_A_ABS, b / st.CUT_B_ABS))})
        prev_hist = hist
    return out


def classify(frames: np.ndarray, times: Sequence[float]) -> Dict[str, Any]:
    """What happens in this window: ``hard`` (one step), ``dissolve``, ``fade`` (through black) or ``none``, and where, to the frame."""
    n = len(frames)
    if n < 3:
        return {"kind": "none", "reason": "too few frames to compare"}
    steps = step_scores(frames)
    lumas = [float(f.mean()) / 255.0 for f in frames]
    # a fade through black: the picture darkens to nearly black and comes back
    span = float(times[-1]) - float(times[0])
    rate = (n - 1) / span if span > 0 else 0.0
    dips = st.find_dips(lumas, rate) if rate > 0 else []          # times in a dip are seconds from the window's first frame
    if dips:
        d = min(dips, key=lambda x: lumas[int(round(x["t"] * rate))])
        i, lo_i, hi_i = int(round(d["t"] * rate)), int(round(d["start"] * rate)), min(n - 1, int(round(d["end"] * rate)))
        return {"kind": "fade", "t": round(float(times[i]), 4), "frame_index": i, "start": round(float(times[lo_i]), 4),
                "end": round(float(times[hi_i]), 4), "evidence": {"darkest_luma": round(lumas[i], 3)}}
    best = max(range(len(steps)), key=lambda k: steps[k]["score"])
    top = steps[best]
    neighbours = [steps[k]["score"] for k in (best - 1, best + 1) if 0 <= k < len(steps)]
    # a dissolve: judged at the cadence the thresholds were calibrated at (about ten steps a second), a run of steps where the
    # histogram keeps moving but the pixels barely do
    stride = max(1, int(round(rate / S.ANALYSIS_FPS))) if rate > 0 else 1
    coarse = list(range(0, n, stride))
    slow = step_scores(frames[coarse]) if len(coarse) >= 3 else []
    consecutive = _longest_run([k for k, s_ in enumerate(slow) if s_["b"] >= st.GRADUAL_B and s_["a"] < st.GRADUAL_MAX_A])
    if len(consecutive) >= st.GRADUAL_MIN_STEPS:
        lo, hi = coarse[consecutive[0] + 1], coarse[min(len(coarse) - 1, consecutive[-1] + 1)]          # step k is between samples k and k+1
        mid = (lo + hi) // 2
        return {"kind": "dissolve", "t": round(float(times[mid]), 4), "frame_index": mid, "start": round(float(times[lo]), 4),
                "end": round(float(times[hi]), 4), "evidence": {"steps": len(consecutive), "pixel_change": round(slow[consecutive[0]]["a"], 3)}}
    if top["score"] >= 1.0 and (not neighbours or max(neighbours) < SPIKE_RATIO * top["score"]):
        later = frames[best + 2:best + 2 + max(2, int(round(FLASH_SECONDS * rate)))]       # does the old picture come straight back?
        came_back = any(float(np.abs(f.astype(np.int16) - frames[best].astype(np.int16)).mean()) / 255.0 < st.FLASH_RETURN for f in later)
        if came_back:
            return {"kind": "none", "reason": "a flash: the picture is back as it was within a few frames", "evidence": {"biggest_step_score": round(top["score"], 2)}}
        i = best + 1                                                  # the first frame of the new shot
        return {"kind": "hard", "t": round(float(times[i]), 4), "frame_index": i, "evidence": {"pixel_change": round(top["a"], 3),
                                                                                              "histogram_change": round(top["b"], 3), "score": round(top["score"], 2)}}
    return {"kind": "none", "reason": "the picture does not change like a cut anywhere in this window", "evidence": {"biggest_step_score": round(top["score"], 2)}}


def _longest_run(indices: Sequence[int]) -> List[int]:
    best: List[int] = []
    cur: List[int] = []
    for k in indices:
        cur = cur + [k] if cur and k == cur[-1] + 1 else [k]
        if len(cur) > len(best):
            best = cur
    return best


def refine_cut(path: str, probe: Dict[str, Any], near: float, radius: float = DEFAULT_RADIUS,
               decode: Callable[..., Tuple[np.ndarray, List[float]]] = decode_window) -> Dict[str, Any]:
    """The exact cut near *near* seconds. Widens the window once (to 2x the radius) when nothing is found in the first."""
    video = (probe or {}).get("video") or {}
    size = _size(video)
    fps = float(video.get("fps") or 0.0)
    start_time = float((probe or {}).get("start_time") or 0.0)
    duration = float((probe or {}).get("duration") or 0.0)
    radius = min(MAX_RADIUS, max(0.1, float(radius)))
    result: Dict[str, Any] = {}
    for attempt, r in enumerate((radius, min(MAX_RADIUS, radius * 2))):
        lo, hi = max(0.0, near - r), (min(duration, near + r) if duration else near + r)
        frames, times = decode(path, lo + start_time, hi + start_time, size, start_time=start_time)
        result = classify(frames, times)
        edge = max(3, int(round(EDGE_CONTEXT_SECONDS * (len(frames) / max(1e-6, (hi - lo))))))
        if result["kind"] == "hard" and (result["frame_index"] <= edge or result["frame_index"] >= len(frames) - edge):
            # a step this close to the edge of the window may be the end of a flash that began outside it: look again with context
            lo, hi = max(0.0, lo - EDGE_CONTEXT_SECONDS), (min(duration, hi + EDGE_CONTEXT_SECONDS) if duration else hi + EDGE_CONTEXT_SECONDS)
            frames, times = decode(path, lo + start_time, hi + start_time, size, start_time=start_time)
            result = classify(frames, times)
        result.update(window=[round(lo, 3), round(hi, 3)], frames=int(len(frames)), widened=bool(attempt))
        if result["kind"] != "none" or r >= MAX_RADIUS:
            break
    if fps and "t" in result:
        result["frame"] = int(round(result["t"] * fps))
        result["fps"] = round(fps, 3)
    result["hint_error"] = round(result["t"] - near, 3) if "t" in result else None
    result["measured"] = True
    return result


def _size(video: Dict[str, Any]) -> Tuple[int, int]:
    from classes.media_index.probe import analysis_size
    size = analysis_size(int(video.get("width") or 0), int(video.get("height") or 0), S.ANALYSIS_LONG_EDGE)
    return size or (160, 90)
