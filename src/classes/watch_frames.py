"""Editor-side ffmpeg JPEG extraction for the watch window.

A watch sends a handful of small stills (never native video) to the backend,
which forwards them to Gemini. Keep the count and width low - the point is a
cheap "is the query actually visible here?" check, not an index pass.
"""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
from typing import List, Optional, Tuple

from classes.index_chunker import _ffmpeg_run
from classes.logger import log

WATCH_FRAME_COUNT = 6
WATCH_FRAME_WIDTH = 512
WATCH_FRAME_MAX = 12  # core/indexing/watch_window.py _MAX_FRAMES

# Below this the index window is already tight enough to cut on. Indexing ran
# Gemini over the whole file at full fidelity; re-asking flash-lite about six
# downsampled stills of a short span is a weaker answer, not a second opinion.
WATCH_MIN_WINDOW_SEC = 8.0


def plan_frame_times(start, end, *, count: int = WATCH_FRAME_COUNT) -> List[float]:
    """Evenly spaced sample times strictly inside [start, end).

    Endpoints are inset by half a step: the backend prompt warns the model not
    to pick the first frame unless the query is really there, so handing it the
    window edge as a sample biases exactly the mistake it is told to avoid.
    """
    try:
        s = float(start)
        e = float(end)
    except (TypeError, ValueError):
        return []
    if e < s:
        s, e = e, s
    span = e - s
    if span <= 0:
        return [s]

    try:
        n = int(count)
    except (TypeError, ValueError):
        n = WATCH_FRAME_COUNT
    n = max(2, min(n, WATCH_FRAME_MAX))
    # A very short window cannot carry six meaningfully different stills.
    n = min(n, max(2, int(span * 2)))

    step = span / n
    return [s + step * (i + 0.5) for i in range(n)]


def _encode(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    return base64.b64encode(blob).decode("ascii") if blob else None


def extract_watch_frames(
    video_path: str,
    start,
    end,
    *,
    count: int = WATCH_FRAME_COUNT,
    width: int = WATCH_FRAME_WIDTH,
) -> Tuple[List[dict], str]:
    """Extract stills across [start, end). Returns (frames, error).

    Frames are [{"timestamp": float, "image_base64": str}, ...]. A partial
    extraction is still useful, so only a total failure reports an error.
    """
    if not video_path or not os.path.isfile(video_path):
        return [], f"File not found: {video_path}"

    times = plan_frame_times(start, end, count=count)
    if not times:
        return [], "Invalid watch window"

    try:
        w = max(64, int(width))
    except (TypeError, ValueError):
        w = WATCH_FRAME_WIDTH

    work_dir = tempfile.mkdtemp(prefix="zenvi_watch_")
    frames: List[dict] = []
    last_err = ""
    try:
        for i, ts in enumerate(times):
            out_path = os.path.join(work_dir, f"watch_{i:02d}.jpg")
            # -ss before -i is the accurate-seek form and keeps each still on
            # the timestamp we label it with.
            ok, err = _ffmpeg_run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{ts:.3f}",
                "-i", video_path,
                "-frames:v", "1",
                "-vf", f"scale={w}:-2",
                "-q:v", "5",
                "-f", "image2",
                out_path,
            ])
            if not ok:
                last_err = err
                continue
            encoded = _encode(out_path)
            if encoded:
                frames.append({"timestamp": float(ts), "image_base64": encoded})
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if not frames:
        return [], last_err or "ffmpeg produced no watch frames"
    kb = sum(len(f["image_base64"]) for f in frames) * 3 // 4096
    log.info(
        "watch: extracted %d/%d JPEGs @%dpx over [%.2f-%.2f]s (~%d KB) from %s",
        len(frames), len(times), w,
        frames[0]["timestamp"], frames[-1]["timestamp"],
        kb, os.path.basename(video_path),
    )
    return frames, ""
