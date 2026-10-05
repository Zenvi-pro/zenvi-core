"""Keyframe curves for the handoff exporters, evaluated the way libopenshot 1.0 does.

A Zenvi keyframe is ``{"Points": [{"co": {"X": frame, "Y": value},
"interpolation": 0|1|2, "handle_left": {"X", "Y"}, "handle_right": {"X", "Y"},
"handle_type": 0|1}]}`` (0 BEZIER, 1 LINEAR, 2 CONSTANT). X is a 1-based
*clip frame*: frame 1 is the first frame of the clip's source, so a keyframe
on the first visible frame of a clip trimmed by ``start`` seconds sits at
``round(start * fps) + 1`` (see ``frame_time.keyframe_x`` and
``editor_tools._base.clip_frame_at``).

Evaluation follows libopenshot ``Keyframe::GetValue`` (src/KeyFrame.cpp:
``InterpolateBetween``, ``InterpolateLinearCurve``, ``InterpolateBezierCurve``)
and ``Point`` defaults (src/Point.cpp), as ported and parity-tested by the web
engine (zenvi-web ``packages/engine/src/keyframe/evaluate.ts`` and
``points.ts``). Two rules matter to every exporter:

* a segment between points ``a`` and ``b`` is drawn with ``b``'s
  interpolation (the *right* point's), and
* a bezier segment's control points are ``a.handle_right`` and
  ``b.handle_left``, fractions of the segment's width and height -- i.e. the
  CSS ``cubic-bezier(x1, y1, x2, y2)`` of that segment. The editor's default
  handles (0.5, 0) / (0.5, 1) are an ease-in-out.

``editor_tools._base.keyframe_value_at`` is a linear-only approximation used
for reporting; exporters need the real curve, so they use :class:`Curve`.
The C++ fuses ``a * b + c`` into one fused multiply-add; Python < 3.13 has
no ``math.fma``, so values here can differ from libopenshot in the last bit
or two of a double -- far below anything an exporter writes.

Pure Python: no Qt, no project access.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, List, Optional, Sequence, Tuple

BEZIER, LINEAR, CONSTANT = 0, 1, 2
INTERPOLATION_NAMES = {BEZIER: "bezier", LINEAR: "linear", CONSTANT: "hold"}

DEFAULT_HANDLE_LEFT = (0.5, 1.0)
DEFAULT_HANDLE_RIGHT = (0.5, 0.0)

# libopenshot passes 0.01 as the bezier X tolerance (KeyFrame.cpp GetValue).
BEZIER_ALLOWED_ERROR = 0.01
# The C++ bisection has no cap; after ~55 halvings t cannot change, so 100 never cuts it short.
BEZIER_MAX_ITERATIONS = 100

LINEAR_BEZIER = (0.0, 0.0, 1.0, 1.0)


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def float32(value: float) -> float:
    """``value`` narrowed to a C float (libopenshot's ``Point(float y)`` for bare numbers)."""
    return struct.unpack("f", struct.pack("f", float(value)))[0]


def as_fraction(fps: Any) -> Fraction:
    """A positive Fraction from a Fraction, number, (num, den) pair or ``{"num", "den"}`` dict."""
    if isinstance(fps, Fraction):
        value = fps
    elif isinstance(fps, dict):
        value = Fraction(int(fps.get("num") or 30), int(fps.get("den") or 1))
    elif isinstance(fps, (tuple, list)):
        if len(fps) != 2:
            raise ValueError(f"fps must be (num, den), got {fps!r}")
        value = Fraction(int(fps[0]), int(fps[1] or 1))
    elif isinstance(fps, int):
        value = Fraction(fps)
    elif isinstance(fps, str):
        value = Fraction(fps.strip())
    elif isinstance(fps, float):
        value = Fraction(fps).limit_denominator(1001000)
    else:
        raise ValueError(f"invalid fps {fps!r}")
    if value <= 0:
        raise ValueError(f"fps must be positive, got {fps!r}")
    return value


@dataclass(frozen=True)
class KeyPoint:
    """One keyframe point with its times.

    ``frame`` is libopenshot's X (1-based clip frame), ``time`` the timeline
    second it falls on and ``local`` the seconds since the clip's first
    visible frame (negative or past the clip's length for points outside the
    visible window). ``interpolation`` shapes the segment that ENDS here.
    """

    frame: float
    value: float
    interpolation: int = BEZIER
    handle_left: Tuple[float, float] = DEFAULT_HANDLE_LEFT
    handle_right: Tuple[float, float] = DEFAULT_HANDLE_RIGHT
    handle_type: int = 0
    time: float = 0.0
    local: float = 0.0

    def to_json(self) -> dict:
        """The point as project JSON (``Point::JsonValue`` keeps handles only for bezier)."""
        out: dict = {"co": {"X": self.frame, "Y": self.value}, "interpolation": self.interpolation}
        if self.interpolation == BEZIER:
            out["handle_left"] = {"X": self.handle_left[0], "Y": self.handle_left[1]}
            out["handle_right"] = {"X": self.handle_right[0], "Y": self.handle_right[1]}
            out["handle_type"] = self.handle_type
        return out


@dataclass(frozen=True)
class Segment:
    """The span between two consecutive points, drawn with the right point's interpolation."""

    start: KeyPoint
    end: KeyPoint

    @property
    def interpolation(self) -> int:
        return self.end.interpolation

    @property
    def easing(self) -> str:
        """``"bezier"``, ``"linear"`` or ``"hold"`` (libopenshot CONSTANT: keep ``start.value``)."""
        return INTERPOLATION_NAMES.get(self.interpolation, "linear")

    @property
    def bezier(self) -> Optional[Tuple[float, float, float, float]]:
        """CSS ``cubic-bezier(x1, y1, x2, y2)`` of the segment; linear gives (0, 0, 1, 1), hold None."""
        if self.interpolation == CONSTANT:
            return None
        if self.interpolation != BEZIER:
            return LINEAR_BEZIER
        return (self.start.handle_right[0], self.start.handle_right[1],
                self.end.handle_left[0], self.end.handle_left[1])

    @property
    def duration(self) -> float:
        return self.end.time - self.start.time


def _resolve_point(raw: Any) -> Tuple[float, float, int, Tuple[float, float], Tuple[float, float], int]:
    p: dict = raw if isinstance(raw, dict) else {}

    def _sub(key: str) -> dict:
        value = p.get(key)
        return value if isinstance(value, dict) else {}

    co, hl, hr = _sub("co"), _sub("handle_left"), _sub("handle_right")
    x = _num(co.get("X"))
    y = _num(co.get("Y"))
    interp = _num(p.get("interpolation"))
    htype = _num(p.get("handle_type"))
    hlx, hly, hrx, hry = _num(hl.get("X")), _num(hl.get("Y")), _num(hr.get("X")), _num(hr.get("Y"))
    return (
        1.0 if x is None else x,
        0.0 if y is None else y,
        BEZIER if interp is None else int(interp),  # jsoncpp asInt() truncates
        (DEFAULT_HANDLE_LEFT[0] if hlx is None else hlx, DEFAULT_HANDLE_LEFT[1] if hly is None else hly),
        (DEFAULT_HANDLE_RIGHT[0] if hrx is None else hrx, DEFAULT_HANDLE_RIGHT[1] if hry is None else hry),
        0 if htype is None else int(htype),
    )


def resolve_points(value: Any) -> List[tuple]:
    """Points as libopenshot holds them after ``Keyframe::SetJsonValue``.

    Sorted by X; a later point with the same X replaces the earlier one
    (``Keyframe::AddPoint``). A bare number is one CONSTANT point at X=1 with
    its value narrowed to float. Anything else (None, malformed) is empty.
    """
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return []
        return [(1.0, float32(value), CONSTANT, DEFAULT_HANDLE_LEFT, DEFAULT_HANDLE_RIGHT, 0)]
    if not isinstance(value, dict) or not isinstance(value.get("Points"), list):
        return []
    out: List[tuple] = []
    for raw in value["Points"]:
        p = _resolve_point(raw)
        lo, hi = 0, len(out)
        while lo < hi:
            mid = (lo + hi) // 2
            if out[mid][0] < p[0]:
                lo = mid + 1
            else:
                hi = mid
        if lo < len(out) and out[lo][0] == p[0]:
            out[lo] = p
        else:
            out.insert(lo, p)
    return out


def _linear(left: KeyPoint, right: KeyPoint, target: float) -> float:
    slope = (right.value - left.value) / (right.frame - left.frame)
    return slope * (target - left.frame) + left.value


def _bezier(left: KeyPoint, right: KeyPoint, target: float, allowed_error: float) -> float:
    x_diff = right.frame - left.frame
    y_diff = right.value - left.value
    p0x, p0y = left.frame, left.value
    p1x = left.handle_right[0] * x_diff + p0x
    p1y = left.handle_right[1] * y_diff + p0y
    p2x = right.handle_left[0] * x_diff + p0x
    p2y = right.handle_left[1] * y_diff + p0y
    p3x, p3y = right.frame, right.value
    t, t_step = 0.5, 0.25
    b0 = b1 = b2 = b3 = 0.0
    for _ in range(BEZIER_MAX_ITERATIONS):
        u = 1 - t
        tt, uu = t * t, u * u
        b1 = t * 3 * uu
        b2 = tt * 3 * u
        b3 = tt * t
        b0 = uu * u
        x = p3x * b3 + (p2x * b2 + (p0x * b0 + p1x * b1))
        if abs(target - x) < allowed_error:
            break
        if x > target:
            t -= t_step
        else:
            t += t_step
        t_step /= 2
    return p3y * b3 + (p2y * b2 + (p0y * b0 + p1y * b1))


def interpolate_between(left: KeyPoint, right: KeyPoint, target: float,
                        allowed_error: float = BEZIER_ALLOWED_ERROR) -> float:
    """libopenshot ``InterpolateBetween``: the right point's interpolation shapes the segment."""
    if left.frame > target:
        return left.value
    if target > right.frame:
        return right.value
    if right.interpolation == CONSTANT:
        return left.value
    if right.interpolation == BEZIER:
        return _bezier(left, right, target, allowed_error)
    return _linear(left, right, target)


class Curve:
    """A clip (or effect / transition) keyframe with its timeline mapping.

    ``fps`` is the project frame rate, ``position`` the owner's timeline
    start and ``start`` its trimmed source start, both in seconds (a
    transition's ``start`` is 0). ``default`` is the value of a curve with
    no points. Immutable once built.
    """

    __slots__ = ("points", "fps", "position", "start", "default")

    def __init__(self, points: Sequence[KeyPoint], *, fps: Any = 30, position: float = 0.0,
                 start: float = 0.0, default: float = 0.0):
        self.fps = as_fraction(fps)
        self.position = float(position or 0.0)
        self.start = float(start or 0.0)
        self.default = float(default)
        self.points: Tuple[KeyPoint, ...] = tuple(points)

    # -- construction -----------------------------------------------------
    @classmethod
    def from_json(cls, value: Any, *, fps: Any = 30, position: float = 0.0, start: float = 0.0,
                  default: float = 0.0) -> "Curve":
        """A curve from project JSON (keyframe dict or bare number); None or malformed = no points."""
        rate = as_fraction(fps)
        out = []
        for x, y, interp, hl, hr, htype in resolve_points(value):
            local = (x - 1.0) / float(rate) - float(start or 0.0)
            out.append(KeyPoint(frame=x, value=y, interpolation=interp, handle_left=hl, handle_right=hr,
                                handle_type=htype, time=float(position or 0.0) + local, local=local))
        return cls(out, fps=rate, position=position, start=start, default=default)

    @classmethod
    def constant(cls, value: float, *, fps: Any = 30, position: float = 0.0, start: float = 0.0) -> "Curve":
        return cls.from_json({"Points": [{"co": {"X": 1.0, "Y": float(value)}, "interpolation": BEZIER}]},
                             fps=fps, position=position, start=start, default=float(value))

    def to_json(self) -> dict:
        return {"Points": [p.to_json() for p in self.points]}

    # -- time mapping -----------------------------------------------------
    def frame_at(self, time: float) -> float:
        """Clip frame X shown at timeline second *time* (fractional off the frame grid).

        libopenshot: ``clip_frame = timeline_frame - clip_start_position + clip_start_frame``
        with both offsets ``round(seconds * fps) + 1``. On the grid this is an integer.
        """
        rate = float(self.fps)
        x = (float(time) - self.position + self.start) * rate + 1.0
        nearest = round(x)
        return float(nearest) if abs(x - nearest) < 1e-6 else x

    def time_of(self, frame: float) -> float:
        """Timeline second of clip frame *frame*."""
        return self.position + (float(frame) - 1.0) / float(self.fps) - self.start

    # -- evaluation -------------------------------------------------------
    def value_at_x(self, x: float) -> float:
        """Value at a (possibly fractional) X, like the web engine's ``evaluateAtX``."""
        pts = self.points
        if not pts:
            return self.default
        x = float(x)
        lo, hi = 0, len(pts)
        while lo < hi:
            mid = (lo + hi) // 2
            if pts[mid].frame < x:
                lo = mid + 1
            else:
                hi = mid
        if lo == len(pts):
            return pts[-1].value
        if lo == 0:
            return pts[0].value
        if pts[lo].frame == x:
            return pts[lo].value
        return interpolate_between(pts[lo - 1], pts[lo], x)

    def value_at_frame(self, frame: float) -> float:
        """libopenshot ``GetValue(int64 frame)``: the frame is truncated to an integer."""
        return self.value_at_x(float(math.trunc(float(frame))))

    def value_at(self, time: float) -> float:
        """Value at timeline second *time* (exact libopenshot value on the frame grid)."""
        return self.value_at_x(self.frame_at(time))

    def value_at_local(self, local: float) -> float:
        """Value *local* seconds after the clip's first visible frame."""
        return self.value_at(self.position + float(local))

    def sample(self, times: Iterable[float]) -> List[float]:
        """Values at each timeline second in *times*."""
        return [self.value_at(t) for t in times]

    # -- shape --------------------------------------------------------------
    @property
    def is_constant(self) -> bool:
        """True when the curve never changes value (no points, one point, or equal values)."""
        if len(self.points) < 2:
            return True
        first = self.points[0].value
        return all(abs(p.value - first) <= 1e-9 for p in self.points[1:])

    @property
    def is_animated(self) -> bool:
        return not self.is_constant

    @property
    def first_value(self) -> float:
        return self.points[0].value if self.points else self.default

    @property
    def last_value(self) -> float:
        return self.points[-1].value if self.points else self.default

    def segments(self) -> List[Segment]:
        """Consecutive point pairs; each is drawn with its end point's interpolation."""
        return [Segment(a, b) for a, b in zip(self.points, self.points[1:])]

    def times(self) -> List[float]:
        """Timeline seconds of every point."""
        return [p.time for p in self.points]

    def points_between(self, t0: float, t1: float) -> List[KeyPoint]:
        """Points whose timeline time lies in [t0, t1] (a clip's visible window)."""
        eps = 1e-9
        return [p for p in self.points if t0 - eps <= p.time <= t1 + eps]

    def scaled(self, factor: float, offset: float = 0.0) -> "Curve":
        """A copy with every value mapped to ``value * factor + offset`` (unit changes)."""
        pts = [KeyPoint(frame=p.frame, value=p.value * factor + offset, interpolation=p.interpolation,
                        handle_left=p.handle_left, handle_right=p.handle_right, handle_type=p.handle_type,
                        time=p.time, local=p.local) for p in self.points]
        return Curve(pts, fps=self.fps, position=self.position, start=self.start,
                     default=self.default * factor + offset)

    def __repr__(self) -> str:
        shown = ", ".join(f"{p.frame:g}:{p.value:g}" for p in self.points[:6])
        more = "..." if len(self.points) > 6 else ""
        return f"Curve([{shown}{more}], fps={self.fps}, position={self.position:g}, start={self.start:g})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Curve):
            return NotImplemented
        return (self.points, self.fps, self.position, self.start, self.default) == (
            other.points, other.fps, other.position, other.start, other.default)

    def __hash__(self) -> int:
        return hash((self.points, self.fps, self.position, self.start, self.default))


def union_times(*curves: Optional[Curve], start: Optional[float] = None, end: Optional[float] = None,
                animated_only: bool = True) -> List[float]:
    """Sorted, de-duplicated timeline times of the points of *curves* (within [start, end]).

    ``animated_only`` skips constant curves, whose single point is not a key
    an exporter should write. *start* / *end* are always included when given,
    so a sampler gets the clip's edges.
    """
    seen: dict = {}
    for curve in curves:
        if curve is None or (animated_only and curve.is_constant):
            continue
        for t in curve.times():
            if start is not None and t < start - 1e-9:
                continue
            if end is not None and t > end + 1e-9:
                continue
            seen.setdefault(round(t, 9), t)
    for edge in (start, end):
        if edge is not None:
            seen.setdefault(round(float(edge), 9), float(edge))
    return [seen[k] for k in sorted(seen)]
