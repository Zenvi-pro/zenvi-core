"""
 @file
 @brief What a media file is: duration, streams, display size, colour tags (ffprobe).

 The index measures from the original file, so it first needs to know how the file is
 laid out: the size as displayed (after rotation), whether there is audio, and how the
 colour is tagged (a log or HDR file must not be read as ordinary Rec.709).
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Optional

from classes.ffmpeg_cli import run_ffmpeg

_HDR_TRANSFERS = ("smpte2084", "arib-std-b67")


def _frac(text: Any) -> float:
    try:
        num, _, den = str(text).partition("/")
        value = float(num) / float(den or 1)
        return value if value > 0 else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _rotation(stream: Dict[str, Any]) -> int:
    for item in stream.get("side_data_list") or []:
        if isinstance(item, dict) and "rotation" in item:
            try:
                return int(round(float(item["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    tags = stream.get("tags") or {}
    try:
        return int(round(float(tags.get("rotate", 0)))) % 360
    except (TypeError, ValueError):
        return 0


def parse_probe(data: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce ffprobe's JSON to what the index uses (pure, so it is tested without ffprobe)."""
    streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and int((s.get("disposition") or {}).get("attached_pic", 0) or 0) == 0), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    raw_fmt = data.get("format")
    fmt: Dict[str, Any] = raw_fmt if isinstance(raw_fmt, dict) else {}

    duration = _float(fmt.get("duration"))
    if duration <= 0.0:
        for s in (video, audio):
            if s and _float(s.get("duration")) > 0.0:
                duration = _float(s.get("duration"))
                break

    out: Dict[str, Any] = {
        "ok": bool(video or audio),
        "duration": duration,
        "format": str(fmt.get("format_name") or ""),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video": None,
        "audio": None,
    }
    if video:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        rotation = _rotation(video)
        if rotation in (90, 270):
            width, height = height, width
        transfer = str(video.get("color_transfer") or "")
        pix_fmt = str(video.get("pix_fmt") or "")
        bits = int(video.get("bits_per_raw_sample") or 0) or (10 if "10" in pix_fmt else 8)
        out["video"] = {
            "codec": str(video.get("codec_name") or ""),
            "width": width,
            "height": height,
            "rotation": rotation,
            "fps": _frac(video.get("avg_frame_rate")) or _frac(video.get("r_frame_rate")),
            "pix_fmt": pix_fmt,
            "bit_depth": bits,
            "color_range": str(video.get("color_range") or ""),
            "color_space": str(video.get("color_space") or ""),
            "color_transfer": transfer,
            "color_primaries": str(video.get("color_primaries") or ""),
            "hdr": transfer in _HDR_TRANSFERS,
            "orientation": "square" if width == height else ("portrait" if height > width else "landscape"),
        }
    if audio:
        out["audio"] = {
            "codec": str(audio.get("codec_name") or ""),
            "sample_rate": int(_float(audio.get("sample_rate"))),
            "channels": int(audio.get("channels") or 0),
        }
    return out


def probe_media(path: str) -> Dict[str, Any]:
    """ffprobe *path*; returns ``{"ok": False, "error": ...}`` instead of raising."""
    try:
        proc = run_ffmpeg(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=60,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe not found"}
    except Exception as exc:  # timeout, OS error
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "ffprobe failed").strip()[:300]}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": "ffprobe returned invalid JSON"}
    parsed = parse_probe(data)
    if not parsed["ok"]:
        parsed["error"] = "no audio or video stream"
    return parsed


def analysis_size(width: int, height: int, long_edge: int) -> Optional[tuple]:
    """(w, h) of the analysis frame: the display aspect at *long_edge*, even sides, at least 16."""
    if width <= 0 or height <= 0:
        return None
    if width >= height:
        w, h = long_edge, long_edge * height / width
    else:
        w, h = long_edge * width / height, long_edge
    w, h = max(16, int(round(w / 2.0)) * 2), max(16, int(round(h / 2.0)) * 2)
    return w, h

