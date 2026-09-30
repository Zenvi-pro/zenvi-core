"""Smart render: stream-copy untouched timeline spans (Premiere-style).

Eligibility uses the same identity defaults as the Properties panel:
  scale/volume/alpha/time → 1.0
  location/rotation/shear → 0.0

Modes:
  full_copy — spans match export width/fps/codec; ffmpeg stream-copy
  source_passthrough — single clean clip; trim+copy source even if export
    profile differs (MP4/MOV only)
  partial — mix of copy + encode segments, then concat
"""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from classes.ffmpeg_cli import run_ffmpeg
from classes.logger import log

# Format mismatches that source-passthrough may ignore for a single clean clip.
_FORMAT_REASONS = frozenset({"resolution-mismatch", "fps-mismatch", "codec-mismatch"})

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
    kind: str  # "copy" | "encode"
    start_frame: int
    end_frame: int
    clip: Optional[dict] = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SmartRenderDecision:
    mode: str  # "full_copy" | "source_passthrough" | "partial" | "none"
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
    """True if keyframe/scalar is animated or not at the given identity."""
    if obj is None:
        return False
    if isinstance(obj, (int, float)):
        return abs(float(obj) - float(identity)) > 1e-6
    if isinstance(obj, dict):
        points = obj.get("Points") or obj.get("points") or []
        if not points:
            return False
        if len(points) > 1:
            return True
        try:
            co = points[0].get("co") or {}
            y = float(co.get("Y", co.get("y", identity)))
            return abs(y - float(identity)) > 1e-6
        except Exception:
            return True
    return False


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


def clip_transform_reasons(clip: dict) -> list[str]:
    """Disqualify for transforms/effects/source only (ignore export format)."""
    reasons: list[str] = []
    if _effects_block_smart_render(clip.get("effects")):
        reasons.append("has-effects")

    for prop, label, identity in (
        ("scale_x", "scaled", 1.0),
        ("scale_y", "scaled", 1.0),
        ("location_x", "translated", 0.0),
        ("location_y", "translated", 0.0),
        ("rotation", "rotated", 0.0),
        ("shear_x", "sheared", 0.0),
        ("shear_y", "sheared", 0.0),
        ("volume", "volume-change", 1.0),
        ("alpha", "alpha-change", 1.0),
        ("time", "speed-change", 1.0),
    ):
        if _deviates_from_identity(clip.get(prop), identity):
            if label not in reasons:
                reasons.append(label)

    time_prop = clip.get("time")
    if isinstance(time_prop, str) and time_prop not in ("TIME_MAP_FORWARD", "none", ""):
        if "speed-change" not in reasons:
            reasons.append("speed-change")

    if _crop_is_active(clip):
        reasons.append("cropped")

    reader = clip.get("reader") or {}
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

    return reasons


def clip_smart_render_reasons(
    clip: dict,
    *,
    export_width: int,
    export_height: int,
    export_fps: float,
    export_vcodec: str,
    source_meta: Optional[dict] = None,
) -> list[str]:
    """Return disqualification reasons (empty => eligible for matched stream-copy)."""
    return (
        clip_transform_reasons(clip)
        + clip_format_reasons(
            clip,
            export_width=export_width,
            export_height=export_height,
            export_fps=export_fps,
            export_vcodec=export_vcodec,
            source_meta=source_meta,
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
    ignore_format: bool = False,
) -> list[SmartRenderSpan]:
    """Partition [start_frame, end_frame] into copy vs encode spans."""
    fps = export_fps if export_fps is not None else _fps_float(project_data)
    clips = list(project_data.get("clips") or [])
    transitions = list(project_data.get("transitions") or [])
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
            if ignore_format:
                reasons = clip_transform_reasons(clip)
            else:
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


def _reason_counts(spans: list[SmartRenderSpan]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for span in spans:
        for reason in span.reasons:
            counts[reason] += 1
    return dict(counts)


def _container_allows_passthrough(export_file_path: str, vformat: str) -> bool:
    ext = os.path.splitext(export_file_path)[1].lower().lstrip(".")
    fmt = (vformat or ext or "").lower()
    return fmt in ("mp4", "mov", "m4v") or ext in ("mp4", "mov", "m4v")


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
    allow_passthrough: bool = True,
    allow_partial: bool = True,
) -> SmartRenderDecision:
    """Choose full_copy, source_passthrough, partial, or none."""
    spans = analyze_smart_render_spans(
        project_data,
        export_width=export_width,
        export_height=export_height,
        export_fps=export_fps,
        export_vcodec=export_vcodec,
        start_frame=start_frame,
        end_frame=end_frame,
        ignore_format=False,
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

    # Source passthrough: one clean clip covering the whole range; format may differ.
    if allow_passthrough and _container_allows_passthrough(export_file_path, vformat):
        clean_spans = analyze_smart_render_spans(
            project_data,
            export_width=export_width,
            export_height=export_height,
            export_fps=export_fps,
            export_vcodec=export_vcodec,
            start_frame=start_frame,
            end_frame=end_frame,
            ignore_format=True,
        )
        if (
            clean_spans
            and all(s.kind == "copy" for s in clean_spans)
            and len({id(s.clip) for s in clean_spans if s.clip is not None}) == 1
        ):
            clip = next(s.clip for s in clean_spans if s.clip is not None)
            # Only use passthrough when format mismatch (or empty reasons under match)
            # was the obstacle — not when transforms failed.
            transform_only = clip_transform_reasons(clip)
            if not transform_only:
                reader = clip.get("reader") or {}
                detail = "source={}x{} codec={}; export={}x{} codec={}".format(
                    reader.get("width"),
                    reader.get("height"),
                    reader.get("vcodec") or reader.get("video_codec"),
                    export_width,
                    export_height,
                    export_vcodec,
                )
                return SmartRenderDecision(
                    mode="source_passthrough",
                    spans=tuple(clean_spans),
                    clip=clip,
                    reason_counts=counts,
                    detail=detail,
                )

    return SmartRenderDecision(
        mode="none",
        spans=tuple(spans),
        reason_counts=counts,
        detail="fallback-encode",
    )


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
    allow_passthrough: bool = True,
    allow_partial: bool = True,
) -> Optional[dict]:
    """Attempt smart render. Returns metrics dict on success, None to fall back."""
    if not enabled:
        return None

    fps_dict = video_settings.get("fps") or {}
    fps = float(fps_dict.get("num", 30)) / float(fps_dict.get("den", 1) or 1)
    export_width = int(video_settings.get("width", 1920))
    export_height = int(video_settings.get("height", 1080))
    export_vcodec = str(video_settings.get("vcodec") or "libx264")
    vformat = str(video_settings.get("vformat") or "mp4")

    # Partial needs a working encode callback.
    if allow_partial and encode_span is None:
        allow_partial = False

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
        allow_passthrough=allow_passthrough,
        allow_partial=allow_partial,
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

    if decision.mode == "source_passthrough" and decision.clip is not None:
        ok = _stream_copy_span(
            decision.clip,
            start_frame=start_frame,
            end_frame=end_frame,
            fps=fps,
            output_path=export_file_path,
        )
        if not ok:
            log.warning("Smart render passthrough failed; falling back to full encode")
            return None
        return {
            "mode": "source_passthrough",
            "spans": 1,
            "copy_spans": 1,
            "encode_spans": 0,
            "path": export_file_path,
            "detail": decision.detail,
        }

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
            out = os.path.join(tmp_dir, f"seg_{index:04d}.mp4")
            if span.kind == "copy" and span.clip is not None:
                ok = _stream_copy_span(
                    span.clip,
                    start_frame=span.start_frame,
                    end_frame=span.end_frame,
                    fps=fps,
                    output_path=out,
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
            segment_paths.append(out)

        if not _concat_copy(segment_paths, export_file_path):
            log.warning("Smart render concat failed; falling back")
            return None

        copy_spans = sum(1 for s in spans if s.kind == "copy")
        return {
            "mode": decision.mode,
            "spans": len(spans),
            "copy_spans": copy_spans,
            "encode_spans": len(spans) - copy_spans,
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
