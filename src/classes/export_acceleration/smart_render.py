"""Smart render: stream-copy untouched timeline spans (Premiere-style).

Eligibility uses the same identity defaults as the Properties panel:
  scale/volume/alpha/time → 1.0
  location/rotation/shear → 0.0

Modes:
  full_copy — spans match export width/fps/codec; ffmpeg stream-copy
  partial — mix of copy + encode segments, then concat

A copy is only made when the source already is what the export settings ask
for (codec, size, fps, audio): copying a source that differs would silently
ignore the user's settings. A stream copy also starts on the keyframe before a
cut and overshoots a cut end with B-frames, so only a whole clip that plays its
whole source file is copied, and the copy is counted before it is used.
"""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from classes.export_acceleration.hw_encode import is_hardware_encoder
from classes.ffmpeg_cli import run_ffmpeg, run_ffmpeg_with_progress
from classes.logger import log

# libopenshot defaults (Clip::init_settings) for the other clip properties that
# change the picture, and the audio-only ones (checked when audio is exported).
_PICTURE_DEFAULTS = (
    ("margin", "margin", 0.0),
    ("corner_radius", "rounded-corners", 0.0),
) + tuple(
    ("perspective_c%d_%s" % (corner, axis), "perspective", -1.0)
    for corner in range(1, 5)
    for axis in ("x", "y")
)
_AUDIO_DEFAULTS = (
    ("volume", "volume-change", 1.0),
    ("channel_filter", "audio-channels", -1.0),
    ("channel_mapping", "audio-channels", -1.0),
)

# yuv420p and yuvj420p: what a default H.264 export writes.
_COPYABLE_PIXEL_FORMATS = (0, 12)

# Default for export_audio: the caller did not say, so treat the source audio
# as exported (check audio properties, but not the audio format).
_KEEP_AUDIO = object()

# Neutral ColorGrade knob defaults (Y values).
_COLOR_GRADE_IDENTITY = {
    "contrast": 0.0,
    "exposure": 0.0,
    "temperature": 0.0,
    "tint": 0.0,
    "shadows": 0.0,
    "highlights": 0.0,
    "vibrance": 0.0,
    "saturation": 1.0,
    "mix": 1.0,
    "lut_intensity": 1.0,
}


@dataclass(frozen=True)
class SmartRenderSpan:
    kind: str  # "copy" | "normalize" | "encode"
    start_frame: int
    end_frame: int
    clip: Optional[dict] = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SmartRenderDecision:
    mode: str  # "full_copy" | "partial" | "none"
    spans: tuple[SmartRenderSpan, ...] = ()
    clip: Optional[dict] = None
    reason_counts: dict[str, int] = field(default_factory=dict)
    detail: str = ""


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


def _deviates_from_identity(obj: Any, identity: float) -> bool:
    """True if a keyframe/scalar leaves the given identity at any point."""
    if obj is None:
        return False
    if isinstance(obj, (int, float)):
        return abs(float(obj) - float(identity)) > 1e-6
    if isinstance(obj, dict):
        points = obj.get("Points") or obj.get("points") or []
        try:
            for point in points:
                co = point.get("co") or {}
                y = float(co.get("Y", co.get("y", identity)))
                if abs(y - float(identity)) > 1e-6:
                    return True
        except Exception:
            return True
        return False
    return False



def _time_curve_is_identity(obj: Any) -> bool:
    """True when clip.time is a no-op (forward 1x), including post-double-reverse."""
    if obj is None:
        return True
    if isinstance(obj, str):
        return obj in ("TIME_MAP_FORWARD", "none", "", "0")
    if isinstance(obj, (int, float)):
        return float(obj) in (0.0, 1.0)
    if not isinstance(obj, dict):
        return False
    points = obj.get("Points") or obj.get("points") or []
    if not points:
        return True
    try:
        coords = []
        for point in points:
            co = point.get("co") or {}
            coords.append(
                (
                    float(co.get("X", co.get("x", 0.0))),
                    float(co.get("Y", co.get("y", 0.0))),
                )
            )
    except (TypeError, ValueError):
        return False
    if len(coords) == 1:
        x, y = coords[0]
        return abs(y - 1.0) <= 1e-3 or abs(y - x) <= 1.0 + 1e-6
    coords.sort(key=lambda pair: pair[0])
    for index in range(1, len(coords)):
        if coords[index][1] + 1e-6 < coords[index - 1][1]:
            return False  # reverse / rewind
    for x, y in coords:
        if abs(y - x) > 1.0 + 1e-6:
            return False
    return True


def _keyframe_y(obj: Any, default: float) -> Optional[float]:
    if obj is None:
        return default
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict):
        points = obj.get("Points") or obj.get("points") or []
        if not points:
            return default
        if len(points) > 1:
            return None  # animated
        try:
            co = points[0].get("co") or {}
            return float(co.get("Y", co.get("y", default)))
        except Exception:
            return None
    return None


def _is_identity_color_grade(effect: dict) -> bool:
    if (effect.get("class_name") or effect.get("type")) != "ColorGrade":
        return False
    lut = (effect.get("lut_path") or "").strip()
    if lut:
        return False
    for key, identity in _COLOR_GRADE_IDENTITY.items():
        if key not in effect:
            continue
        if _deviates_from_identity(effect.get(key), identity):
            return False
    # Curves: any multi-point or non-passthrough is a grade.
    for curve_key in ("curve_all", "curve_red", "curve_green", "curve_blue"):
        curve = effect.get(curve_key)
        if not curve:
            continue
        if isinstance(curve, dict):
            points = curve.get("Points") or curve.get("points") or []
            if len(points) > 2:
                return False
    return True


def _effects_block_smart_render(effects: Any) -> bool:
    if not effects:
        return False
    for effect in effects:
        if not isinstance(effect, dict):
            return True
        if _is_identity_color_grade(effect):
            continue
        return True
    return False


def _crop_is_active(clip: dict) -> bool:
    """True when crop geometry is non-identity."""
    crop = clip.get("crop")
    if isinstance(crop, dict):
        # Nested form: x/y identity 0, right/bottom (width/height) identity 1
        for key, identity in (("x", 0.0), ("y", 0.0), ("right", 1.0), ("bottom", 1.0)):
            if key in crop and _deviates_from_identity(crop.get(key), identity):
                return True
        # Also accept crop_width/height naming inside crop
        for key, identity in (("crop_x", 0.0), ("crop_y", 0.0), ("crop_width", 1.0), ("crop_height", 1.0)):
            if key in crop and _deviates_from_identity(crop.get(key), identity):
                return True
        return False

    for key, identity in (
        ("crop_x", 0.0),
        ("crop_y", 0.0),
        ("crop_width", 1.0),
        ("crop_height", 1.0),
    ):
        if key in clip and _deviates_from_identity(clip.get(key), identity):
            return True
    return False


def _audio_family(codec: str) -> str:
    name = (codec or "").lower()
    for family in ("aac", "mp3", "ac3", "opus", "vorbis", "flac"):
        if family in name:
            return family
    return name


def clip_transform_reasons(
    clip: dict,
    *,
    export_fps: Optional[float] = None,
    export_audio: Any = _KEEP_AUDIO,
) -> list[str]:
    """Disqualify for transforms/effects/source only (ignore export format).

    *export_audio* None means the export has no audio, so audio-only
    properties do not matter (the copy drops the source audio).
    """
    reasons: list[str] = []
    if _effects_block_smart_render(clip.get("effects")):
        reasons.append("has-effects")

    reader = clip.get("reader") or {}
    if (reader.get("type") not in (None, "FFmpegReader")
            or reader.get("has_video") is False
            or reader.get("has_single_image")):
        reasons.append("not-a-video-file")

    audio_matters = export_audio is not None
    for prop, label, identity in (
        ("scale_x", "scaled", 1.0),
        ("scale_y", "scaled", 1.0),
        ("location_x", "translated", 0.0),
        ("location_y", "translated", 0.0),
        ("rotation", "rotated", 0.0),
        ("shear_x", "sheared", 0.0),
        ("shear_y", "sheared", 0.0),
        ("alpha", "alpha-change", 1.0),
    ) + _PICTURE_DEFAULTS + (_AUDIO_DEFAULTS if audio_matters else ()):
        if _deviates_from_identity(clip.get(prop), identity):
            if label not in reasons:
                reasons.append(label)

    # 0 switches the clip's video or audio off; -1 (auto) and 1 leave it on.
    for prop, label in (("has_video", "video-off"), ("has_audio", "audio-off")):
        if prop == "has_audio" and not audio_matters:
            continue
        if _keyframe_y(clip.get(prop), -1.0) in (0.0, None) and clip.get(prop) is not None:
            reasons.append(label)

    if clip.get("waveform"):
        reasons.append("waveform")

    # A copy can only begin on a keyframe and overshoots a cut end, so the clip
    # has to play its whole source file.
    half_frame = 0.5 / max(float(export_fps or 30.0), 1.0)
    try:
        start = float(clip.get("start", 0.0))
        end = float(clip.get("end", 0.0))
        source_duration = float(reader.get("duration") or 0.0)
    except (TypeError, ValueError):
        start = end = source_duration = 0.0
    if (start > half_frame or source_duration <= 0
            or abs(end - source_duration) > half_frame):
        reasons.append("trimmed")

    # Multi-point Y≈X (double-reverse restore) is identity; reverse/speed is not.
    if not _time_curve_is_identity(clip.get("time")):
        if "speed-change" not in reasons:
            reasons.append("speed-change")

    if _crop_is_active(clip):
        reasons.append("cropped")

    path = (reader.get("path") or clip.get("path") or "")
    if not path or not os.path.isfile(path):
        reasons.append("missing-source")

    return reasons


def clip_format_reasons(
    clip: dict,
    *,
    export_width: int,
    export_height: int,
    export_fps: float,
    export_vcodec: str,
    source_meta: Optional[dict] = None,
    export_audio: Any = _KEEP_AUDIO,
) -> list[str]:
    """Disqualify when source format does not match the export target."""
    reasons: list[str] = []
    reader = clip.get("reader") or {}
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

    # The copy keeps the source audio as it is, so it must already be what
    # the export asks for.
    if isinstance(export_audio, dict) and reader.get("has_audio"):
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

    return reasons


def clip_smart_render_reasons(
    clip: dict,
    *,
    export_width: int,
    export_height: int,
    export_fps: float,
    export_vcodec: str,
    source_meta: Optional[dict] = None,
    export_audio: Any = _KEEP_AUDIO,
) -> list[str]:
    """Return disqualification reasons (empty => eligible for matched stream-copy)."""
    return (
        clip_transform_reasons(clip, export_fps=export_fps, export_audio=export_audio)
        + clip_format_reasons(
            clip,
            export_width=export_width,
            export_height=export_height,
            export_fps=export_fps,
            export_vcodec=export_vcodec,
            source_meta=source_meta,
            export_audio=export_audio,
        )
    )


def analyze_smart_render_spans(
    project_data: dict,
    *,
    export_width: int,
    export_height: int,
    export_fps: Optional[float] = None,
    export_vcodec: str = "libx264",
    start_frame: int = 1,
    end_frame: Optional[int] = None,
    export_audio: Any = _KEEP_AUDIO,
) -> list[SmartRenderSpan]:
    """Partition [start_frame, end_frame] into copy vs encode spans.

    *export_audio* is the export's audio settings, or None without audio.
    """
    fps = export_fps if export_fps is not None else _fps_float(project_data)
    clips = list(project_data.get("clips") or [])
    # Project data keeps transitions under "effects".
    transitions = list(project_data.get("effects") or []) + list(
        project_data.get("transitions") or [])
    if end_frame is None:
        duration = float(project_data.get("duration") or 0) or 0
        end_frame = max(start_frame, int(round(duration * fps)))

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

        # Keep encode/normalize candidates with different reasons separate so a
        # clean half (format-only) is not merged into a reversed/effect half.
        reasons_changed = reasons != current_reasons
        if (
            kind != current_kind
            or (kind == "copy" and clip is not current_clip)
            or (kind == "encode" and reasons_changed and current_kind is not None)
        ):
            flush(frame - 1)
            current_kind = kind
            current_start = frame
            current_clip = clip
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


def _reason_counts(spans: list[SmartRenderSpan]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for span in spans:
        for reason in span.reasons:
            counts[reason] += 1
    return dict(counts)


_NORMALIZE_OK_REASONS = frozenset({
    "resolution-mismatch",
    "fps-mismatch",
    "codec-mismatch",
    "codec-unknown",
    "pixel-format-mismatch",
    "trimmed",
    "audio-mismatch",
})


def _promote_normalize_spans(spans: list[SmartRenderSpan]) -> list[SmartRenderSpan]:
    """Turn format-only encode spans into ffmpeg normalize spans."""
    out: list[SmartRenderSpan] = []
    for span in spans:
        if (
            span.kind == "encode"
            and span.clip is not None
            and span.reasons
            and set(span.reasons) <= _NORMALIZE_OK_REASONS
        ):
            out.append(
                SmartRenderSpan(
                    kind="normalize",
                    start_frame=span.start_frame,
                    end_frame=span.end_frame,
                    clip=span.clip,
                    reasons=span.reasons,
                )
            )
        else:
            out.append(span)
    return out


def decide_smart_render(
    project_data: dict,
    *,
    export_width: int,
    export_height: int,
    export_fps: float,
    export_vcodec: str,
    start_frame: int,
    end_frame: int,
    export_file_path: str = "",
    vformat: str = "mp4",
    allow_partial: bool = True,
    export_audio: Any = _KEEP_AUDIO,
) -> SmartRenderDecision:
    """Choose full_copy, partial, or none."""
    spans = analyze_smart_render_spans(
        project_data,
        export_width=export_width,
        export_height=export_height,
        export_fps=export_fps,
        export_vcodec=export_vcodec,
        start_frame=start_frame,
        end_frame=end_frame,
        export_audio=export_audio,
    )
    counts = _reason_counts(spans)
    if not spans:
        return SmartRenderDecision(mode="none", reason_counts=counts, detail="no-spans")

    if all(s.kind == "copy" for s in spans):
        return SmartRenderDecision(
            mode="full_copy",
            spans=tuple(spans),
            reason_counts=counts,
            detail="matched-export",
        )

    copy_n = sum(1 for s in spans if s.kind == "copy")
    encode_n = len(spans) - copy_n
    if allow_partial and copy_n > 0 and encode_n > 0:
        return SmartRenderDecision(
            mode="partial",
            spans=tuple(spans),
            reason_counts=counts,
            detail=f"copy={copy_n} encode={encode_n}",
        )

    # Format mismatch / trimmed: stream-copy is unsafe, but transform-clean
    # spans can still be ffmpeg-normalized to the export profile (faster than
    # OpenShot) while dirty spans (reverse, FX) use encode_span.
    if allow_partial:
        promoted = _promote_normalize_spans(spans)
        norm_n = sum(1 for s in promoted if s.kind == "normalize")
        dirty_n = sum(1 for s in promoted if s.kind == "encode")
        if norm_n > 0 and dirty_n > 0:
            return SmartRenderDecision(
                mode="partial",
                spans=tuple(promoted),
                reason_counts=counts,
                detail=f"normalize={norm_n} encode={dirty_n}",
            )

    return SmartRenderDecision(
        mode="none",
        spans=tuple(spans),
        reason_counts=counts,
        detail="fallback-encode",
    )



def _span_source_window(
    clip: dict, *, start_frame: int, end_frame: int, fps: float
) -> tuple[Optional[str], float, float]:
    reader = clip.get("reader") or {}
    path = reader.get("path") or clip.get("path")
    if not path:
        return None, 0.0, 0.0
    clip_start = float(clip.get("start", 0.0))
    position = float(clip.get("position", 0.0))
    timeline_start_sec = (start_frame - 1) / fps
    timeline_end_sec = end_frame / fps
    source_start = clip_start + max(0.0, timeline_start_sec - position)
    duration = max(0.001, timeline_end_sec - timeline_start_sec)
    return path, source_start, duration


def _ffmpeg_vcodec(export_vcodec: str) -> str:
    name = (export_vcodec or "libx264").lower()
    if "265" in name or "hevc" in name:
        return "libx265"
    return "libx264"


def _ffmpeg_rate_args(video_bitrate: Any) -> list[str]:
    if video_bitrate is None:
        return ["-crf", "20"]
    if isinstance(video_bitrate, (int, float)):
        value = int(video_bitrate)
        if 0 <= value <= 51:
            return ["-crf", str(value)]
        return ["-b:v", str(value)]
    text = str(video_bitrate).strip().lower()
    parts = text.split()
    if len(parts) >= 2 and ("crf" in parts[1] or "cqp" in parts[1] or parts[1] == "qp"):
        try:
            return ["-crf", str(int(float(parts[0])))]
        except (TypeError, ValueError):
            return ["-crf", "20"]
    return ["-crf", "20"]


def _transcode_span_to_export(
    clip: dict,
    *,
    start_frame: int,
    end_frame: int,
    fps: float,
    output_path: str,
    export_width: int,
    export_height: int,
    export_fps: float,
    export_vcodec: str,
    video_bitrate: Any = None,
    include_audio: bool = True,
    progress_cb: Optional[Callable[[int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> bool:
    path, source_start, duration = _span_source_window(
        clip, start_frame=start_frame, end_frame=end_frame, fps=fps
    )
    if not path:
        return False
    width = max(2, int(export_width))
    height = max(2, int(export_height))
    if width % 2:
        width += 1
    if height % 2:
        height += 1
    out_fps = float(export_fps) if export_fps and export_fps > 0 else fps
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={out_fps:.6f}"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{source_start:.6f}", "-i", path, "-t", f"{duration:.6f}",
        "-vf", vf, "-c:v", _ffmpeg_vcodec(export_vcodec),
        *_ffmpeg_rate_args(video_bitrate),
        "-pix_fmt", "yuv420p",
    ]
    if include_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
    else:
        cmd.append("-an")
    cmd.extend(["-movflags", "+faststart", output_path])
    frame_span = max(1, int(end_frame) - int(start_frame))

    def _on_frac(frac: float) -> None:
        if progress_cb is None:
            return
        frame = int(start_frame) + int(round(max(0.0, min(1.0, frac)) * frame_span))
        progress_cb(min(int(end_frame), frame))

    result = run_ffmpeg_with_progress(
        cmd,
        on_progress=_on_frac if progress_cb is not None else None,
        should_cancel=cancel_cb,
    )
    if result.returncode != 0:
        log.warning("Smart-render export-normalize failed: %s", result.stderr)
        return False
    return os.path.isfile(output_path) and os.path.getsize(output_path) > 0


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
    """True when a written file holds exactly the *expected* frames."""
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
    if len(paths) == 1:
        # Single segment: move/copy into place without concat demuxer.
        try:
            import shutil

            shutil.copyfile(paths[0], output_path)
            return os.path.isfile(output_path) and os.path.getsize(output_path) > 0
        except Exception:
            log.warning("Smart-render single-segment install failed", exc_info=True)
            return False

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        list_path = handle.name
        for path in paths:
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
    encode_span: Optional[Callable[[int, int, str], bool]] = None,
    enabled: bool = True,
    allow_partial: bool = True,
    audio_settings: Any = _KEEP_AUDIO,
    progress_cb: Optional[Callable[[int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> Optional[dict]:
    """Attempt smart render. Returns metrics dict on success, None to fall back.

    *audio_settings* is the export's audio settings, or None for an export
    without audio (copies then drop the source audio).
    """
    if not enabled:
        return None

    fps_dict = video_settings.get("fps") or {}
    fps = float(fps_dict.get("num", 30)) / float(fps_dict.get("den", 1) or 1)
    export_width = int(video_settings.get("width", 1920))
    export_height = int(video_settings.get("height", 1080))
    export_vcodec = str(video_settings.get("vcodec") or "libx264")
    vformat = str(video_settings.get("vformat") or "mp4")

    # Partial needs a working encode callback. Its segments are written by a
    # writer of their own, outside run_export's encoder checks, so a hardware
    # encoder (h264_videotoolbox aborts the app on libopenshot 1.0) is not used.
    if allow_partial and (encode_span is None or is_hardware_encoder(export_vcodec)):
        allow_partial = False
    include_audio = audio_settings is not None

    decision = decide_smart_render(
        project_data,
        export_width=export_width,
        export_height=export_height,
        export_fps=fps,
        export_vcodec=export_vcodec,
        start_frame=start_frame,
        end_frame=end_frame,
        export_file_path=export_file_path,
        vformat=vformat,
        allow_partial=allow_partial,
        export_audio=audio_settings,
    )

    if decision.mode == "none":
        log.info(
            "Smart render skipped: reasons=%s detail=%s",
            decision.reason_counts or {},
            decision.detail,
        )
        return None

    log.info(
        "Smart render decision: mode=%s detail=%s reasons=%s",
        decision.mode,
        decision.detail,
        decision.reason_counts or {},
    )

    spans = list(decision.spans)
    if not spans:
        return None

    # full_copy or partial
    if decision.mode == "partial" and encode_span is None:
        log.info("Smart render partial unavailable (no encode_span); skipping")
        return None

    tmp_dir = tempfile.mkdtemp(prefix="zenvi-smart-render-")
    segment_paths: list[str] = []
    try:
        for index, span in enumerate(spans):
            if cancel_cb is not None and cancel_cb():
                log.info("Smart render cancelled before span %s", index)
                return None
            if progress_cb is not None:
                progress_cb(span.start_frame)
            out = os.path.join(tmp_dir, f"seg_{index:04d}.mp4")
            if span.kind == "copy" and span.clip is not None:
                ok = _stream_copy_span(
                    span.clip,
                    start_frame=span.start_frame,
                    end_frame=span.end_frame,
                    fps=fps,
                    output_path=out,
                    include_audio=include_audio,
                ) and _has_frames(out, span.end_frame - span.start_frame + 1)
                if not ok and decision.mode == "partial" and encode_span is not None:
                    ok = encode_span(span.start_frame, span.end_frame, out)
            elif span.kind == "normalize" and span.clip is not None:
                log.info(
                    "Smart render normalizing span %s (%s-%s) to export %sx%s",
                    index, span.start_frame, span.end_frame, export_width, export_height,
                )
                ok = _transcode_span_to_export(
                    span.clip,
                    start_frame=span.start_frame,
                    end_frame=span.end_frame,
                    fps=fps,
                    output_path=out,
                    export_width=export_width,
                    export_height=export_height,
                    export_fps=fps,
                    export_vcodec=export_vcodec,
                    video_bitrate=video_settings.get("video_bitrate"),
                    include_audio=include_audio,
                    progress_cb=progress_cb,
                    cancel_cb=cancel_cb,
                )
                if not ok and encode_span is not None:
                    ok = encode_span(span.start_frame, span.end_frame, out)
            else:
                if encode_span is None:
                    ok = False
                else:
                    ok = encode_span(span.start_frame, span.end_frame, out)
            if not ok:
                log.warning("Smart render aborted; falling back to full encode")
                return None
            if progress_cb is not None:
                progress_cb(span.end_frame)
            segment_paths.append(out)

        if not _concat_copy(segment_paths, export_file_path):
            log.warning("Smart render concat failed; falling back")
            return None
        if not _has_frames(export_file_path, end_frame - start_frame + 1):
            return None

        copy_spans = sum(1 for s in spans if s.kind == "copy")
        normalize_spans = sum(1 for s in spans if s.kind == "normalize")
        return {
            "mode": decision.mode,
            "spans": len(spans),
            "copy_spans": copy_spans,
            "normalize_spans": normalize_spans,
            "encode_spans": len(spans) - copy_spans - normalize_spans,
            "path": export_file_path,
            "detail": decision.detail,
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
