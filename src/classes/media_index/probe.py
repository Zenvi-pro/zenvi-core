"""
 @file
 @brief What a media file is: duration, streams, display size, colour tags (ffprobe).

 The index measures from the original file, so it first needs to know how the file is
 laid out: the size as displayed (after rotation), whether there is audio, and how the
 colour is tagged (a log or HDR file must not be read as ordinary Rec.709).
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
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


_ISO6709 = re.compile(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?(?:CRS\w+)?/?$")
_TZ_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")
MIN_YEAR = 1995        # camera clocks that were never set read 1970, 1980 or 1904
_LOCATION_KEYS = ("com.apple.quicktime.location.iso6709", "location", "location-eng")
_DATE_KEYS = ("com.apple.quicktime.creationdate", "creation_time")
_CAMERA_KEYS = {"make": ("com.apple.quicktime.make", "make", "manufacturer"),
                "model": ("com.apple.quicktime.model", "model"),
                "software": ("com.apple.quicktime.software", "software"),
                "lens": ("com.apple.quicktime.lens", "lens", "lensmodel")}
INTERLACED_ORDERS = ("tt", "bb", "tb", "bt")
VFR_TOLERANCE = 0.01          # the average and the nominal frame rate differ by more than this share: variable frame rate


def parse_iso6709(text: Any) -> Optional[Dict[str, float]]:
    """{"lat", "lon"[, "alt"]} from a tag like ``+37.7749-122.4194+012.000/``; None when absent or nonsense.

    A position of exactly 0, 0 is how many cameras say "no fix", so it is treated as absent.
    """
    m = _ISO6709.match(str(text or "").strip())
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or (lat == 0.0 and lon == 0.0):
        return None
    out = {"lat": round(lat, 6), "lon": round(lon, 6)}
    if m.group(3) is not None:
        out["alt"] = round(float(m.group(3)), 1)
    return out


def parse_capture_time(text: Any) -> Optional[str]:
    """ISO 8601 UTC (``2024-05-01T14:03:09+00:00``) from a container date tag; None for unset clocks.

    A tag without a zone is taken as UTC (ffmpeg writes UTC); one with a zone is converted.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00").replace(" ", "T", 1)
    raw = _TZ_NO_COLON.sub(r"\1:\2", raw) if re.search(r"T.*[+-]\d{4}$", raw) else raw
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    if when.year < MIN_YEAR or when > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return when.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_capture(fmt_tags: Any, stream_tags: Any = ()) -> Dict[str, Any]:
    """When and (if the camera knew) where a clip was shot, from container and stream tags.

    ``captured_at`` is as tagged: a file that was re-exported may carry the export time, so
    treat it as a hint for ordering, not proof. Location stays on this machine (see store).
    """
    tag_sets = [{str(k).lower(): v for k, v in (t or {}).items()} for t in [fmt_tags, *list(stream_tags or [])]
                if isinstance(t, dict)]
    captured, source = None, None
    for key in _DATE_KEYS:
        for tags in tag_sets:
            when = parse_capture_time(tags.get(key))
            if when:
                captured, source = when, key
                break
        if captured:
            break
    gps = None
    for key in _LOCATION_KEYS:
        for tags in tag_sets:
            gps = parse_iso6709(tags.get(key))
            if gps:
                break
        if gps:
            break
    return {"captured_at": captured, "captured_source": source, "gps": gps}


def parse_camera(fmt_tags: Any, stream_tags: Any = ()) -> Dict[str, str]:
    """Camera make, model, lens and the software that wrote the file, from container and stream tags (only what is tagged)."""
    tag_sets = [{str(k).lower(): v for k, v in (t or {}).items()} for t in [fmt_tags, *list(stream_tags or [])] if isinstance(t, dict)]
    out: Dict[str, str] = {}
    for field, keys in _CAMERA_KEYS.items():
        for key in keys:
            value = next((str(t[key]).strip() for t in tag_sets if str(t.get(key) or "").strip()), "")
            if value:
                out[field] = value[:80]
                break
    return out


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
        "capture": parse_capture(fmt.get("tags"), [s.get("tags") for s in streams]),
        "camera": parse_camera(fmt.get("tags"), [s.get("tags") for s in streams]),
        "bit_rate": int(_float(fmt.get("bit_rate"))),
    }
    if video:
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        rotation = _rotation(video)
        if rotation in (90, 270):
            width, height = height, width
        transfer = str(video.get("color_transfer") or "")
        pix_fmt = str(video.get("pix_fmt") or "")
        bits = int(video.get("bits_per_raw_sample") or 0) or (10 if "10" in pix_fmt else 8)
        avg_fps, nominal_fps = _frac(video.get("avg_frame_rate")), _frac(video.get("r_frame_rate"))
        field_order = str(video.get("field_order") or "")
        out["video"] = {
            "codec": str(video.get("codec_name") or ""),
            "width": width,
            "height": height,
            "rotation": rotation,
            "fps": avg_fps or nominal_fps,
            "nominal_fps": nominal_fps,
            "vfr": bool(avg_fps and nominal_fps and abs(avg_fps - nominal_fps) / nominal_fps > VFR_TOLERANCE),
            "field_order": field_order,
            "interlaced": field_order in INTERLACED_ORDERS,
            "bit_rate": int(_float(video.get("bit_rate"))),
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
            "channel_layout": str(audio.get("channel_layout") or ""),
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

