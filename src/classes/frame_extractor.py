"""Extract JPEG frames from local video files for backend tagging (ffmpeg)."""

import subprocess
from typing import List, Tuple

from classes.tagging_interval import get_tagging_interval

# Keep frames small for /tags/analyze-frames payload (backend cap is 200 KB/frame).
_MAX_FRAME_WIDTH = 960
_MAX_FRAME_BYTES = 200 * 1024
_JPEG_QUALITY = 8  # ffmpeg q:v (2=best, 31=worst)


def _ffmpeg_run(args: List[str]) -> Tuple[bool, bytes, str]:
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"ffmpeg failed").decode("utf-8", errors="replace")
            return False, b"", err.strip()
        return True, proc.stdout or b"", ""
    except FileNotFoundError:
        return False, b"", "ffmpeg not found."
    except Exception as exc:
        return False, b"", str(exc)


def _extract_frame_jpeg(video_path: str, timestamp: float) -> Tuple[bool, bytes, str]:
    """Extract one downscaled JPEG frame at *timestamp* seconds."""
    for width, quality in ((_MAX_FRAME_WIDTH, _JPEG_QUALITY), (640, 12), (480, 15)):
        ok, data, err = _ffmpeg_run([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", str(timestamp),
            "-i", video_path,
            "-frames:v", "1",
            "-vf", f"scale='min({width},iw)':-2",
            "-q:v", str(quality),
            "-f", "image2",
            "pipe:1",
        ])
        if not ok:
            return False, b"", err or "ffmpeg frame extraction failed"
        if data and len(data) <= _MAX_FRAME_BYTES:
            return True, data, ""
    if data:
        return True, data, ""
    return False, b"", "ffmpeg returned empty frame data"


def extract_tagging_frames(
    video_path: str,
    duration: float,
) -> Tuple[List[Tuple[float, bytes]], str]:
    """Return (frames, error). Each frame is (timestamp_seconds, jpeg_bytes)."""
    if not video_path:
        return [], "Missing video path."
    try:
        interval, max_frames = get_tagging_interval(float(duration or 0))
    except ValueError as exc:
        return [], str(exc)

    frames: List[Tuple[float, bytes]] = []
    t = 0.0
    dur = float(duration or 0)
    while t <= dur and len(frames) < max_frames:
        ok, data, err = _extract_frame_jpeg(video_path, t)
        if not ok:
            if frames:
                break
            return [], err or "ffmpeg frame extraction failed"
        if not data:
            break
        if len(data) > _MAX_FRAME_BYTES:
            return [], f"Frame at {t:.1f}s is {len(data) // 1024} KB after compression (limit {_MAX_FRAME_BYTES // 1024} KB)."
        frames.append((t, data))
        t += interval

    if not frames:
        return [], "No frames could be extracted from the video."
    return frames, ""
