"""Smart render: stream-copy untouched timeline spans (Premiere-style).

Eligibility is intentionally strict. A span qualifies only when it is a single
clip with no effects, transitions, scale/crop, speed, or volume changes, and
the source codec/resolution/fps/pixel format exactly match the export target.
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


def _has_nonzero_keyframes(obj: Any) -> bool:
    """Return True if a keyframed property has any non-default animation."""
    if obj is None:
        return False
    if isinstance(obj, (int, float)):
        return abs(float(obj) - 1.0) > 1e-6 and abs(float(obj)) > 1e-6
    if isinstance(obj, dict):
        points = obj.get("Points") or obj.get("points") or []
        if not points:
            # Constant scale/location dict without Points — treat scalars.
            for key in ("scale_x", "scale_y", "location_x", "location_y", "rotation", "alpha"):
                if key in obj and _has_nonzero_keyframes(obj[key]):
                    return True
            return False
        if len(points) > 1:
            return True
        # Single point: check if it's a non-identity value for scale-like props.
        try:
            co = points[0].get("co") or {}
            y = float(co.get("Y", co.get("y", 1.0)))
            return abs(y - 1.0) > 1e-6
        except Exception:
            return True
    return False


def clip_smart_render_reasons(
    clip: dict,
    *,
    export_width: int,
    export_height: int,
    export_fps: float,
    export_vcodec: str,
    source_meta: Optional[dict] = None,
) -> list[str]:
    """Return disqualification reasons (empty => eligible for stream-copy)."""
    reasons: list[str] = []
    if clip.get("effects"):
        reasons.append("has-effects")
    # Transitions are project-level; caller also checks overlaps.

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

    # Scale / location / rotation / time / volume
    for prop, label in (
        ("scale_x", "scaled"),
        ("scale_y", "scaled"),
        ("location_x", "translated"),
        ("location_y", "translated"),
        ("rotation", "rotated"),
        ("shear_x", "sheared"),
        ("shear_y", "sheared"),
    ):
        if _has_nonzero_keyframes(clip.get(prop)):
            if label not in reasons:
                reasons.append(label)

    # Gravity / scale mode other than none/stretch identity
    scale = clip.get("scale")
    if scale not in (None, 0, "SCALE_FIT", "SCALE_NONE", "none", "fit"):
        # SCALE_STRETCH etc. that change geometry
        if scale not in (1,):  # permissive
            pass

    time_prop = clip.get("time")
    if time_prop not in (None, 0, "TIME_MAP_FORWARD"):
        reasons.append("speed-change")

    volume = clip.get("volume")
    if isinstance(volume, (int, float)) and abs(float(volume) - 1.0) > 1e-6:
        reasons.append("volume-change")
    elif _has_nonzero_keyframes(volume):
        # volume default is 1.0; treat multi-point as change
        points = (volume or {}).get("Points") if isinstance(volume, dict) else None
        if points and len(points) > 1:
            reasons.append("volume-change")

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
) -> list[SmartRenderSpan]:
    """Partition [start_frame, end_frame] into copy vs encode spans."""
    fps = export_fps if export_fps is not None else _fps_float(project_data)
    clips = list(project_data.get("clips") or [])
    transitions = list(project_data.get("transitions") or [])
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
    return spans


def _stream_copy_span(
    clip: dict,
    *,
    start_frame: int,
    end_frame: int,
    fps: float,
    output_path: str,
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
) -> Optional[dict]:
    """Attempt smart render. Returns metrics dict on success, None to fall back.

    *encode_span(start, end, out_path)* must render a normal encoded segment.
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
                )
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
