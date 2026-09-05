"""Create a compressed MP4 proxy for TwelveLabs direct upload (ffmpeg)."""

import os
import subprocess
import tempfile
from typing import Tuple

from classes.ffmpeg_cli import run_ffmpeg

_MAX_LONG_EDGE = 720
_CRF = 28
_SKIP_IF_MAX_BYTES = 40 * 1024 * 1024
_PRESET = "veryfast"


def _ffprobe_dimensions(path: str) -> Tuple[int, int]:
    try:
        proc = run_ffmpeg(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x", path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        line = (proc.stdout or "").strip().splitlines()
        if not line:
            return 0, 0
        parts = line[0].split("x")
        if len(parts) != 2:
            return 0, 0
        return int(parts[0]), int(parts[1])
    except Exception:
        return 0, 0


def _ffprobe_has_audio(path: str) -> bool:
    try:
        proc = run_ffmpeg(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return bool((proc.stdout or "").strip())
    except Exception:
        return False


def _ffmpeg_run(args: list) -> Tuple[bool, str]:
    try:
        proc = run_ffmpeg(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or b"ffmpeg failed").decode("utf-8", errors="replace")
            return False, err.strip()
        return True, ""
    except FileNotFoundError:
        return False, "ffmpeg not found."
    except Exception as exc:
        return False, str(exc)


def create_index_proxy(video_path: str) -> Tuple[str, bool, int, str]:
    """Return (upload_path, is_temp, size_bytes, error)."""
    if not video_path or not os.path.isfile(video_path):
        return "", False, 0, f"File not found: {video_path}"

    try:
        size = os.path.getsize(video_path)
    except OSError as exc:
        return "", False, 0, str(exc)

    w, h = _ffprobe_dimensions(video_path)
    long_edge = max(w, h) if w and h else 0
    if size <= _SKIP_IF_MAX_BYTES and long_edge and long_edge <= _MAX_LONG_EDGE:
        return video_path, False, size, ""

    fd, out_path = tempfile.mkstemp(suffix="_index_proxy.mp4", prefix="zenvi_")
    os.close(fd)

    vf = f"scale='min({_MAX_LONG_EDGE},iw)':-2,format=yuv420p"
    has_audio = _ffprobe_has_audio(video_path)
    if has_audio:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", _PRESET, "-crf", str(_CRF),
            "-c:a", "aac", "-b:a", "96k", "-ac", "2",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            out_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", video_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", _PRESET, "-crf", str(_CRF),
            "-an",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            out_path,
        ]

    ok, err = _ffmpeg_run(cmd)
    if not ok:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return video_path, False, size, err or "Proxy encode failed"

    try:
        proxy_size = os.path.getsize(out_path)
    except OSError:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return video_path, False, size, "Could not read proxy file size"

    if proxy_size <= 0:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        return video_path, False, size, "Proxy encode produced empty file"

    return out_path, True, proxy_size, ""
