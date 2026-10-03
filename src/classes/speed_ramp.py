"""Build Bezier ``time`` curves for speed ramps (no Qt).

v1: linear stretch of audio with the picture (no optical flow, no pitch-hold).
First keyframe X is preserved via frame_time.keyframe_x (Phase 2 invariant).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from classes import frame_time as ft

# libopenshot Interpolation: BEZIER=0, LINEAR=1, CONSTANT=2 (common OpenShot JSON)
_BEZIER = 0
_LINEAR = 1


def build_speed_ramp_time_points(
    keypoints: Sequence[Mapping[str, Any]],
    *,
    clip_start: float,
    clip_end: float,
    fps: Any,
    interpolation: int = _BEZIER,
) -> dict[str, Any]:
    """Return a ``time`` keyframe dict for a speed ramp.

    Each keypoint is ``{seconds|source_frame|timeline_frame, speed}``.
    Speeds must be finite and > 0. Caps extreme values (Phase 6 intensity lesson).
    """
    if not keypoints:
        raise ValueError("keypoints required")
    start_f = ft.to_frame(float(clip_start), fps)
    end_f = ft.to_frame(float(clip_end), fps)
    if end_f <= start_f:
        raise ValueError("clip_end must be after clip_start")
    start_x = ft.keyframe_x(float(clip_start), fps)
    dur_f = end_f - start_f

    parsed: list[tuple[int, float]] = []
    for kp in keypoints:
        speed = float(kp.get("speed", 1.0))
        if not (speed == speed) or speed <= 0:  # NaN / non-positive
            raise ValueError(f"speed must be > 0, got {speed!r}")
        if speed > 100.0:
            raise ValueError("speed > 100 refused (cap)")
        if speed < 0.01:
            raise ValueError("speed < 0.01 refused (cap)")
        if "timeline_frame" in kp and kp["timeline_frame"] is not None:
            t_f = int(kp["timeline_frame"])
        elif "seconds" in kp and kp["seconds"] is not None:
            t_f = ft.to_frame(float(kp["seconds"]), fps)
        elif "source_frame" in kp and kp["source_frame"] is not None:
            # Treat as timeline-relative progress marker in source frame units → map to span
            t_f = start_f + int(kp["source_frame"])
        else:
            raise ValueError("keypoint needs seconds, timeline_frame, or source_frame")
        parsed.append((t_f, speed))
    parsed.sort(key=lambda item: item[0])
    # Ensure endpoints cover the clip
    if parsed[0][0] > start_f:
        parsed.insert(0, (start_f, parsed[0][1]))
    if parsed[-1][0] < end_f:
        parsed.append((end_f, parsed[-1][1]))

    # Integrate source frames along timeline using piecewise constant speed between knots.
    points = []
    source_y = float(start_x)  # Y in Point space mirrors source frame index (1-based start)
    # OpenShot time curve: X=timeline keyframe, Y=source frame index
    prev_t = start_f
    prev_speed = parsed[0][1]
    # Emit first point at start_x with Y = start_x (identity at in-point)
    points.append({
        "co": {"X": int(start_x), "Y": float(start_x)},
        "interpolation": int(interpolation),
    })
    first_x = int(start_x)
    for t_f, speed in parsed[1:]:
        span = max(0, t_f - prev_t)
        source_y += span * prev_speed
        x = ft.frames_to_keyframe_x(t_f)
        points.append({
            "co": {"X": int(x), "Y": float(source_y)},
            "interpolation": int(interpolation),
        })
        prev_t = t_f
        prev_speed = speed

    # Invariant: first keyframe X unchanged
    if int(points[0]["co"]["X"]) != first_x:
        raise RuntimeError("speed ramp moved first keyframe X")
    _ = dur_f
    return {"Points": points}


def apply_speed_ramp_to_clip_data(
    clip_data: Mapping[str, Any],
    keypoints: Sequence[Mapping[str, Any]],
    *,
    fps: Any,
) -> dict[str, Any]:
    """Return a shallow-copied clip dict with ``time`` set from the ramp."""
    data = dict(clip_data)
    curve = build_speed_ramp_time_points(
        keypoints,
        clip_start=float(data.get("start", 0.0) or 0.0),
        clip_end=float(data.get("end", 0.0) or 0.0),
        fps=fps,
    )
    data["time"] = curve
    return data
