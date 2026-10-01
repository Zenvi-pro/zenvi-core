"""Single authority for seconds <-> project-frame conversion.

Project files still store position/start/end as float seconds (libopenshot
requires that). Every stored value must be exactly
``frame_index * fps_den / fps_num``. All placement, trim, split, retime and
interchange math must go through this module.

Rounding is half-up everywhere. Python's ``round()`` is half-to-even, which
makes a time on an exact half-frame boundary round by parity.
"""

from __future__ import annotations

import math
from fractions import Fraction

Number = int | float | Fraction


def _as_fraction(fps: Number) -> Fraction:
    if isinstance(fps, Fraction):
        if fps <= 0:
            raise ValueError(f"fps must be positive, got {fps}")
        return fps
    try:
        value = Fraction(fps)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid fps: {fps!r}") from exc
    if value <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    return value


def _require_finite(seconds: float, name: str = "seconds") -> float:
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number, got {seconds!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value}")
    return value


def round_half_up(value: float) -> int:
    """Round to nearest int; halves round away from zero (away from -inf for +)."""
    if not math.isfinite(value):
        raise ValueError(f"value must be finite, got {value}")
    if value >= 0:
        return math.floor(value + 0.5)
    return math.ceil(value - 0.5)


def to_frame(seconds: float, fps: Number) -> int:
    """Convert seconds to a 0-indexed project frame number (half-up)."""
    secs = _require_finite(seconds)
    rate = _as_fraction(fps)
    return round_half_up(secs * float(rate))


def to_seconds(frame: int, fps: Number) -> float:
    """Convert a 0-indexed project frame to seconds."""
    try:
        frame_i = int(frame)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"frame must be an int, got {frame!r}") from exc
    rate = _as_fraction(fps)
    return float(Fraction(frame_i) / rate)


def snap(seconds: float, fps: Number) -> float:
    """Snap seconds to the nearest frame boundary."""
    return to_seconds(to_frame(seconds, fps), fps)


def keyframe_x(seconds: float, fps: Number) -> int:
    """1-indexed openshot Point X for a time in seconds."""
    return to_frame(seconds, fps) + 1


def duration_frames(start: float, end: float, fps: Number) -> int:
    """Frame count spanned by [start, end), always at least 1 when end > start."""
    start_s = _require_finite(start, "start")
    end_s = _require_finite(end, "end")
    if end_s <= start_s:
        if end_s == start_s:
            return 1
        return max(1, to_frame(end_s, fps) - to_frame(start_s, fps))
    return max(1, round_half_up((end_s - start_s) * float(_as_fraction(fps))))


def quantize_span(
    position: float,
    start: float,
    end: float,
    fps: Number,
) -> tuple[float, float, float]:
    """Quantize position/start/end preserving duration in frames.

    Rounds duration rather than rounding end independently, so repeated trims
    cannot gain or lose frames.
    """
    rate = _as_fraction(fps)
    pos_f = to_frame(position, rate)
    start_f = to_frame(start, rate)
    dur_f = duration_frames(start, end, rate)
    return (
        to_seconds(pos_f, rate),
        to_seconds(start_f, rate),
        to_seconds(start_f + dur_f, rate),
    )


def is_aligned(seconds: float, fps: Number, *, tol: float | None = None) -> bool:
    """True when ``seconds`` sits on a frame boundary within ``tol``."""
    try:
        secs = _require_finite(seconds)
        rate = _as_fraction(fps)
    except ValueError:
        return False
    snapped = snap(secs, rate)
    if tol is None:
        # Half a frame of the smallest common rate component.
        tol = 0.5 / float(rate) / 1000.0
        tol = max(tol, 1e-9)
    return abs(secs - snapped) <= tol


def frames_to_keyframe_x(frame: int) -> int:
    """Convert a 0-indexed frame to a 1-indexed Point X."""
    return int(frame) + 1


def keyframe_x_to_seconds(x: int, fps: Number) -> float:
    """Convert a 1-indexed Point X to seconds."""
    return to_seconds(int(x) - 1, fps)
