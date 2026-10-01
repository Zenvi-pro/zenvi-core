"""Smart render: stream-copy untouched timeline spans (Premiere-style).

Eligibility is intentionally strict. A span qualifies only when it is a single
clip with no effects, transitions, scale/crop, speed, or volume changes, and
the source codec/resolution/fps/pixel format exactly match the export target.

A stream copy can only start on a keyframe and, with B-frames, overshoots a cut
end by a few frames, so a span is copied only when it is a whole clip that uses
its whole source file; everything else is encoded.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from classes.ffmpeg_cli import run_ffmpeg
from classes.logger import log


@dataclass(frozen=True)
class SmartRenderSpan:
    kind: str  # "copy" | "encode"
    start_frame: int
    end_frame: int
    clip: Optional[dict] = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _fps_float(project: dict) -> float:
    fps = project.get("fps") or {"num": 30, "den": 1}
    return float(fps.get("num", 30)) / float(fps.get("den", 1) or 1)


def _clip_timeline_range(clip: dict, fps: float) -> tuple[int, int]:
    position = float(clip.get("position", 0.0))
    start = float(clip.get("start", 0.0))
    end = float(clip.get("end", 0.0))
    duration = max(0.0, end - start)
    start_frame = int(round(position * fps)) + 1
    end_frame = int(round((position + duration) * fps))
    return start_frame, max(start_frame, end_frame)


# libopenshot's defaults for the clip properties that change the picture or
# the sound (Clip::init_settings). A property that sits at its default at
# every point leaves the source frames as they are.
_UNTOUCHED_DEFAULTS = (
    ("scale_x", 1.0, "scaled"),
    ("scale_y", 1.0, "scaled"),
    ("location_x", 0.0, "translated"),
    ("location_y", 0.0, "translated"),
    ("rotation", 0.0, "rotated"),
    ("shear_x", 0.0, "sheared"),
    ("shear_y", 0.0, "sheared"),
    ("alpha", 1.0, "faded"),
    ("margin", 0.0, "margin"),
    ("corner_radius", 0.0, "rounded-corners"),
    ("volume", 1.0, "volume-change"),
    ("channel_filter", -1.0, "audio-channels"),
    ("channel_mapping", -1.0, "audio-channels"),
) + tuple(
    ("perspective_c%d_%s" % (corner, axis), -1.0, "perspective")
    for corner in range(1, 5)
    for axis in ("x", "y")
)

# yuv420p and yuvj420p: what a default H.264 export writes.
_COPYABLE_PIXEL_FORMATS = (0, 12)


def _keyframe_values(obj: Any) -> Optional[list[float]]:
    """The Y value of every point of a keyframed property (None if unreadable)."""
    if obj is None:
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if not isinstance(obj, dict):
        return None
    values = []
    for point in obj.get("Points") or obj.get("points") or []:
        try:
            co = point.get("co") or {}
            values.append(float(co.get("Y", co.get("y"))))
        except (AttributeError, TypeError, ValueError):
            return None
    return values


def _differs_from(obj: Any, default: float) -> bool:
    """True when a keyframed property leaves *default* anywhere."""
    values = _keyframe_values(obj)
    if values is None:
        return True
    return any(abs(value - default) > 1e-6 for value in values)


def _audio_family(codec: str) -> str:
    name = (codec or "").lower()
    for family in ("aac", "mp3", "ac3", "opus", "vorbis", "flac"):
        if family in name:
            return family
    return name


def clip_smart_render_reasons(
    clip: dict,
    *,
    export_width: int,
    export_height: int,
    export_fps: float,
    export_vcodec: str,
    source_meta: Optional[dict] = None,
    export_audio: Optional[dict] = None,
) -> list[str]:
    """Return disqualification reasons (empty => eligible for stream-copy).

    *export_audio* is the export's audio settings, or None when the export has
    no audio (the copy then drops the source audio, so audio-only properties
    do not matter).
    """
    reasons: list[str] = []
    if clip.get("effects"):
        reasons.append("has-effects")
    # Transitions are project-level; caller also checks overlaps.

    reader = clip.get("reader") or {}
    if (reader.get("type") not in (None, "FFmpegReader")
            or reader.get("has_video") is False
            or reader.get("has_single_image")):
        reasons.append("not-a-video-file")
    width = int(reader.get("width") or clip.get("width") or 0)
    height = int(reader.get("height") or clip.get("height") or 0)
    if width and height and (width != export_width or height != export_height):
        reasons.append("resolution-mismatch")

    src_fps = reader.get("fps") or {}
    if src_fps:
        try:
            src = float(src_fps.get("num", 30)) / float(src_fps.get("den", 1) or 1)
            if abs(src - export_fps) > 0.01:
                reasons.append("fps-mismatch")
        except Exception:
            reasons.append("fps-mismatch")

    vcodec = (reader.get("vcodec") or reader.get("video_codec") or "").lower()
    export_family = "h264"
    if "265" in export_vcodec or "hevc" in export_vcodec:
        export_family = "hevc"
    if vcodec:
        if export_family == "h264" and not any(t in vcodec for t in ("h264", "avc", "x264")):
            reasons.append("codec-mismatch")
        if export_family == "hevc" and not any(t in vcodec for t in ("hevc", "h265", "x265", "hvc1")):
            reasons.append("codec-mismatch")
    elif source_meta and source_meta.get("codec_name"):
        name = str(source_meta["codec_name"]).lower()
        if export_family == "h264" and name not in ("h264", "avc1"):
            reasons.append("codec-mismatch")
    else:
        reasons.append("codec-unknown")

    pixel_format = reader.get("pixel_format")
    if pixel_format is not None and pixel_format not in _COPYABLE_PIXEL_FORMATS:
        reasons.append("pixel-format-mismatch")

    # Scale / location / rotation / opacity / volume, against their defaults
    for prop, default, label in _UNTOUCHED_DEFAULTS:
        if export_audio is None and label in ("volume-change", "audio-channels"):
            continue
        if prop in clip and _differs_from(clip.get(prop), default):
            if label not in reasons:
                reasons.append(label)

    # 0 switches the clip's video or audio off; -1 (auto) and 1 leave it on.
    for prop, label in (("has_video", "video-off"), ("has_audio", "audio-off")):
        if prop == "has_audio" and export_audio is None:
            continue
        values = _keyframe_values(clip.get(prop))
        if values is None or any(abs(value) < 1e-6 for value in values):
            reasons.append(label)

    if clip.get("waveform"):
        reasons.append("waveform")

    # Gravity / scale mode other than none/stretch identity
    scale = clip.get("scale")
    if scale not in (None, 0, "SCALE_FIT", "SCALE_NONE", "none", "fit"):
        # SCALE_STRETCH etc. that change geometry
        if scale not in (1,):  # permissive
            pass

    time_prop = clip.get("time")
    if isinstance(time_prop, dict):
        # libopenshot only remaps time once the curve has two or more points.
        if len(time_prop.get("Points") or time_prop.get("points") or []) > 1:
            reasons.append("speed-change")
    elif time_prop not in (None, 0, "TIME_MAP_FORWARD"):
        reasons.append("speed-change")

    # A copy can only begin on a keyframe and overshoots a cut end, so the
    # clip has to play its whole source file.
    half_frame = 0.5 / max(float(export_fps or 0), 1.0)
    try:
        start = float(clip.get("start", 0.0))
        end = float(clip.get("end", 0.0))
        source_duration = float(reader.get("duration") or 0.0)
    except (TypeError, ValueError):
        start = end = source_duration = 0.0
    if (start > half_frame or source_duration <= 0
            or abs(end - source_duration) > half_frame):
        reasons.append("trimmed")

    if export_audio is not None and reader.get("has_audio"):
        try:
            same_audio = (
                _audio_family(reader.get("acodec") or "")
                == _audio_family(export_audio.get("acodec") or "aac")
                and int(reader.get("sample_rate") or 0) == int(export_audio.get("sample_rate") or 0)
                and int(reader.get("channels") or 0) == int(export_audio.get("channels") or 0)
            )
        except (TypeError, ValueError):
            same_audio = False
        if not same_audio:
            reasons.append("audio-mismatch")

    crop = clip.get("crop") or clip.get("crop_x") or clip.get("crop_width")
    if crop:
        reasons.append("cropped")

    path = (reader.get("path") or clip.get("path") or "")
    if not path or not os.path.isfile(path):
        reasons.append("missing-source")

    return reasons


def analyze_smart_render_spans(
    project_data: dict,
    *,
    export_width: int,
    export_height: int,
    export_fps: Optional[float] = None,
    export_vcodec: str = "libx264",
    start_frame: int = 1,
    end_frame: Optional[int] = None,
    export_audio: Optional[dict] = None,
) -> list[SmartRenderSpan]:
    """Partition [start_frame, end_frame] into copy vs encode spans.

    *export_audio* is the export's audio settings, or None for an export
    without audio.
    """
    fps = export_fps if export_fps is not None else _fps_float(project_data)
    clips = list(project_data.get("clips") or [])
    # Project data keeps transitions under "effects".
    transitions = list(project_data.get("effects") or []) + list(
        project_data.get("transitions") or [])
    if end_frame is None:
        duration = float(project_data.get("duration") or 0) or 0
        end_frame = max(start_frame, int(round(duration * fps)))

    # Map each frame to at most one covering clip (overlap => encode).
    frame_clips: dict[int, list[dict]] = {}
    for clip in clips:
        sf, ef = _clip_timeline_range(clip, fps)
        sf = max(sf, start_frame)
        ef = min(ef, end_frame)
        for frame in range(sf, ef + 1):
            frame_clips.setdefault(frame, []).append(clip)

    def transition_covers(frame: int) -> bool:
        for tr in transitions:
            pos = float(tr.get("position", 0.0))
            start = float(tr.get("start", 0.0))
            end = float(tr.get("end", 0.0))
            tr_start = int(round(pos * fps)) + 1
            tr_end = int(round((pos + max(0.0, end - start)) * fps))
            if tr_start <= frame <= tr_end:
                return True
        return False

    spans: list[SmartRenderSpan] = []
    current_kind = None
    current_start = start_frame
    current_clip = None
    current_reasons: list[str] = []

    def flush(up_to: int) -> None:
        nonlocal current_kind, current_start, current_clip, current_reasons
        if current_kind is None or up_to < current_start:
            return
        spans.append(
            SmartRenderSpan(
                kind=current_kind,
                start_frame=current_start,
                end_frame=up_to,
                clip=current_clip,
                reasons=tuple(current_reasons),
            )
        )

    for frame in range(start_frame, end_frame + 1):
        covering = frame_clips.get(frame, [])
        reasons: list[str] = []
        clip = None
        kind = "encode"
        if len(covering) == 0:
            reasons = ["empty"]
        elif len(covering) > 1:
            reasons = ["overlapping-clips"]
        elif transition_covers(frame):
            clip = covering[0]
            reasons = ["transition"]
        else:
            clip = covering[0]
            reasons = clip_smart_render_reasons(
                clip,
                export_width=export_width,
                export_height=export_height,
                export_fps=fps,
                export_vcodec=export_vcodec,
                export_audio=export_audio,
            )
            if not reasons:
                kind = "copy"

        if kind != current_kind or (kind == "copy" and clip is not current_clip):
            flush(frame - 1)
            current_kind = kind
            current_start = frame
            current_clip = clip if kind == "copy" else None
            current_reasons = reasons
        else:
            current_reasons = reasons

    flush(end_frame)

    # Only a whole clip is copied (see the module docstring): a span cut short
    # by the export range or by an overlapping clip is encoded.
    whole: list[SmartRenderSpan] = []
    for span in spans:
        if span.kind == "copy" and span.clip is not None:
            if (span.start_frame, span.end_frame) != _clip_timeline_range(span.clip, fps):
                span = SmartRenderSpan(
                    kind="encode",
                    start_frame=span.start_frame,
                    end_frame=span.end_frame,
                    reasons=("partial-clip",),
                )
        whole.append(span)
    return whole


def _stream_copy_span(
    clip: dict,
    *,
    start_frame: int,
    end_frame: int,
    fps: float,
    output_path: str,
    include_audio: bool = True,
) -> bool:
    reader = clip.get("reader") or {}
    path = reader.get("path") or clip.get("path")
    if not path:
        return False
    clip_start = float(clip.get("start", 0.0))
    position = float(clip.get("position", 0.0))
    # Map timeline frames back into source time.
    timeline_start_sec = (start_frame - 1) / fps
    timeline_end_sec = end_frame / fps
    source_start = clip_start + max(0.0, timeline_start_sec - position)
    duration = max(0.001, timeline_end_sec - timeline_start_sec)

    result = run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{source_start:.6f}",
            "-i",
            path,
            "-t",
            f"{duration:.6f}",
            # One video and (optionally) one audio stream, like an export writes.
            "-map",
            "0:v:0",
            *(["-map", "0:a:0?"] if include_audio else ["-an"]),
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            output_path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.warning("Smart-render stream-copy failed: %s", result.stderr)
        return False
    return os.path.isfile(output_path) and os.path.getsize(output_path) > 0


def _video_frame_count(path: str) -> Optional[int]:
    """Packets (one per frame) in *path*'s first video stream.

    Counted by demuxing rather than read from the header: Matroska has no
    per-stream frame count, and a counted pre-roll packet is what this guards.
    """
    result = run_ffmpeg(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=nb_read_packets",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return int((result.stdout or "").strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def _has_frames(path: str, expected: int) -> bool:
    """True when a copied file holds exactly the *expected* frames."""
    actual = _video_frame_count(path)
    if actual != expected:
        log.warning(
            "Smart render: %s has %s frames, expected %s; falling back to encode",
            os.path.basename(path), actual, expected,
        )
        return False
    return True


def _concat_copy(paths: list[str], output_path: str) -> bool:
    if not paths:
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        list_path = handle.name
        for path in paths:
            # ffmpeg concat demuxer requires escaped single quotes
            escaped = path.replace("'", r"'\''")
            handle.write(f"file '{escaped}'\n")
    try:
        result = run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                output_path,
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and os.path.isfile(output_path)
    finally:
        try:
            os.unlink(list_path)
        except Exception:
            pass


def try_smart_render_export(
    project_data: dict,
    *,
    export_file_path: str,
    video_settings: dict,
    start_frame: int,
    end_frame: int,
    encode_span: Callable[[int, int, str], bool],
    enabled: bool = True,
    audio_settings: Optional[dict] = None,
) -> Optional[dict]:
    """Attempt smart render. Returns metrics dict on success, None to fall back.

    *encode_span(start, end, out_path)* must render a normal encoded segment.
    *audio_settings* is None for an export without audio.
    """
    if not enabled:
        return None

    fps_dict = video_settings.get("fps") or {}
    fps = float(fps_dict.get("num", 30)) / float(fps_dict.get("den", 1) or 1)
    spans = analyze_smart_render_spans(
        project_data,
        export_width=int(video_settings.get("width", 1920)),
        export_height=int(video_settings.get("height", 1080)),
        export_fps=fps,
        export_vcodec=str(video_settings.get("vcodec") or "libx264"),
        start_frame=start_frame,
        end_frame=end_frame,
        export_audio=audio_settings,
    )
    copy_spans = [s for s in spans if s.kind == "copy"]
    if not copy_spans:
        log.info("Smart render: no eligible spans")
        return None

    # If everything must encode, skip the concat machinery.
    if not any(s.kind == "copy" for s in spans):
        return None

    log.info(
        "Smart render: %s spans (%s copy, %s encode)",
        len(spans),
        len(copy_spans),
        len(spans) - len(copy_spans),
    )

    tmp_dir = tempfile.mkdtemp(prefix="zenvi-smart-render-")
    segment_paths: list[str] = []
    try:
        for index, span in enumerate(spans):
            out = os.path.join(tmp_dir, f"seg_{index:04d}.mp4")
            if span.kind == "copy" and span.clip is not None:
                ok = _stream_copy_span(
                    span.clip,
                    start_frame=span.start_frame,
                    end_frame=span.end_frame,
                    fps=fps,
                    output_path=out,
                    include_audio=audio_settings is not None,
                ) and _has_frames(out, span.end_frame - span.start_frame + 1)
                if not ok:
                    # Fall back to encode for this span.
                    ok = encode_span(span.start_frame, span.end_frame, out)
            else:
                ok = encode_span(span.start_frame, span.end_frame, out)
            if not ok:
                log.warning("Smart render aborted; falling back to full encode")
                return None
            segment_paths.append(out)

        if not _concat_copy(segment_paths, export_file_path):
            log.warning("Smart render concat failed; falling back")
            return None
        if not _has_frames(export_file_path, end_frame - start_frame + 1):
            return None

        return {
            "spans": len(spans),
            "copy_spans": len(copy_spans),
            "encode_spans": len(spans) - len(copy_spans),
            "path": export_file_path,
        }
    finally:
        for path in segment_paths:
            try:
                os.unlink(path)
            except Exception:
                pass
        try:
            os.rmdir(tmp_dir)
        except Exception:
            pass
