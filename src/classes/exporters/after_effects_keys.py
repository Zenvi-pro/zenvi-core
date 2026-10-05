"""Keyframes for the After Effects export: libopenshot curves as AE keys with temporal ease.

After Effects draws the span between two keyframes A and B of a one-dimensional
property as a cubic Bezier in (time, value) whose inner control points come
from A's outgoing and B's incoming ``KeyframeEase`` (speed in value units per
second, influence as a fraction of the span; After Effects Scripting Guide,
KeyframeEase object and ``Property.setTemporalEaseAtKey``)::

    P1 = (tA + iA*dt, vA + sA*iA*dt)        P2 = (tB - iB*dt, vB - sB*iB*dt)

A libopenshot bezier segment is the same kind of curve: ``InterpolateBezierCurve``
(libopenshot src/KeyFrame.cpp, ported in ``classes.handoff.keyframes``) places
its control points at ``left.handle_right`` / ``right.handle_left`` as fractions
of the segment's width and height. A whole segment therefore converts exactly
(TASTE.md section 2)::

    influence_out = x1        speed_out = y1 * dv / (x1 * dt)
    influence_in  = 1 - x2    speed_in  = (1 - y2) * dv / ((1 - x2) * dt)

AE keys can only sit at times every dimension of a property agrees on (Scale
is one property with an ease per dimension), and a clip shows only part of a
curve, so a curve is cut at every key time the AE property needs: each cut
splits the Bezier exactly with de Casteljau's algorithm in (time, value)
space and the pieces convert with the formula above. The C1 notes warn that
``Segment.bezier`` is only valid for whole segments; this module never uses
it on a clipped span.

Values that are not an affine function of one curve -- decibels from a
volume, a position that depends on both location and scale (non-centre
gravity, origin not 0.5, crop mode), an opacity multiplied by a fade
transition -- are sampled once per frame and reduced to linear keys within a
tolerance (Ramer-Douglas-Peucker). Every eased track is checked against the
exact value at every frame the clip shows; one that misses falls back to
the samples, so a conversion is never silently approximate.

Pure Python: no Qt, no project access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, List, Mapping, Optional, Sequence, Tuple, Union

from classes.handoff.keyframes import CONSTANT, LINEAR as OS_LINEAR, Curve, interpolate_between

BEZIER, LINEAR, HOLD = "b", "l", "h"

# AE refuses an influence outside [0.1, 100] percent (KeyframeEase object).
MIN_INFLUENCE = 0.001
LINEAR_INFLUENCE = 1.0 / 3.0
_EPS = 1e-9

Value = Tuple[float, ...]


# ---------------------------------------------------------------------------
# Exact curve values
# ---------------------------------------------------------------------------

def exact_value(curve: Curve, t: float) -> float:
    """The curve's value at timeline second *t*, with bezier segments solved to 1e-9 frames.

    ``Curve.value_at`` mirrors libopenshot's own 0.01-frame bisection; the
    export maps the curve itself, so it compares against the exact Bezier.
    """
    pts = curve.points
    if not pts:
        return curve.default
    x = curve.frame_at(t)
    if x <= pts[0].frame:
        return pts[0].value
    if x >= pts[-1].frame:
        return pts[-1].value
    lo, hi = 0, len(pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid].frame <= x:
            lo = mid
        else:
            hi = mid
    left, right = pts[lo], pts[hi]
    if x == left.frame:
        return left.value
    return interpolate_between(left, right, x, allowed_error=1e-9)


def is_animated_in(curve: Optional[Curve], t0: float, t1: float) -> bool:
    """True when *curve* changes value somewhere inside the window [t0, t1]."""
    if curve is None or len(curve.points) < 2 or curve.is_constant:
        return False
    pts = curve.points
    if pts[-1].time <= t0 + _EPS or pts[0].time >= t1 - _EPS:
        return False
    inside = [p.value for p in pts if t0 - _EPS <= p.time <= t1 + _EPS]
    edges = [exact_value(curve, t0), exact_value(curve, t1)]
    values = inside + edges
    if max(values) - min(values) > 1e-12:
        return True
    # equal values at every key and edge: a bezier overshoot can still move in between
    return any(p.interpolation not in (CONSTANT, OS_LINEAR) for p in pts)


# ---------------------------------------------------------------------------
# Pieces: one dimension between two consecutive keys
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Piece:
    """One dimension of a property between two consecutive AE keys.

    ``x1``/``x2`` are the bezier control points' times as fractions of the
    span, ``c1``/``c2`` their absolute values (so a flat span with an
    overshoot is still representable). Linear and hold pieces ignore them.
    """

    kind: str
    v0: float
    v1: float
    x1: float = LINEAR_INFLUENCE
    c1: float = 0.0
    x2: float = 1.0 - LINEAR_INFLUENCE
    c2: float = 0.0

    @property
    def flat(self) -> bool:
        if abs(self.v1 - self.v0) > 1e-12:
            return False
        if self.kind == BEZIER:
            return abs(self.c1 - self.v0) <= 1e-12 and abs(self.c2 - self.v0) <= 1e-12
        return True

    def mapped(self, a: float, b: float) -> "Piece":
        """The piece under the affine map ``v -> a*v + b`` (Beziers are affine invariant)."""
        return Piece(self.kind, a * self.v0 + b, a * self.v1 + b, self.x1, a * self.c1 + b, self.x2,
                     a * self.c2 + b)


def _bezier_point(p: Sequence[Tuple[float, float]], u: float) -> Tuple[float, float]:
    w = 1.0 - u
    b0, b1, b2, b3 = w * w * w, 3 * w * w * u, 3 * w * u * u, u * u * u
    return (b0 * p[0][0] + b1 * p[1][0] + b2 * p[2][0] + b3 * p[3][0],
            b0 * p[0][1] + b1 * p[1][1] + b2 * p[2][1] + b3 * p[3][1])


def _solve_u(p: Sequence[Tuple[float, float]], t: float) -> float:
    """Parameter u with time(u) == t on a time-monotone cubic (bisection, 1e-12 of the span)."""
    t0, t3 = p[0][0], p[3][0]
    if t <= t0:
        return 0.0
    if t >= t3:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _bezier_point(p, mid)[0] < t:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def curve_pieces(curve: Curve, times: Sequence[float]) -> Tuple[List[float], List[Piece]]:
    """Exact values of *curve* at *times* and the piece of curve between each pair.

    *times* must be sorted and contain every curve point strictly between its
    first and last entry (``key_times`` builds such a list), so each span lies
    inside one libopenshot segment or in a constant region. A bezier span cut
    by a key time is ``Segment.bezier_between`` (the exact de Casteljau piece,
    classes.handoff.keyframes); a flat cut of an overshooting segment comes
    back linear there, which the per-frame check in ``build_property`` catches.
    """
    values = [exact_value(curve, t) for t in times]
    pts = curve.points
    segments = curve.segments()
    pieces: List[Piece] = []
    for j in range(len(times) - 1):
        ta, tb = times[j], times[j + 1]
        va, vb = values[j], values[j + 1]
        if len(pts) < 2 or tb <= pts[0].time + _EPS or ta >= pts[-1].time - _EPS:
            pieces.append(Piece(LINEAR, va, vb))
            continue
        k = 0
        while k < len(segments) - 1 and segments[k].end.time <= ta + _EPS:
            k += 1
        segment = segments[k]
        if segment.end.interpolation == CONSTANT:
            pieces.append(Piece(HOLD, va, vb))
        elif segment.end.interpolation == OS_LINEAR:
            pieces.append(Piece(LINEAR, va, vb))
        else:
            bezier = segment.bezier_between(ta, tb)
            if bezier is None:
                pieces.append(Piece(HOLD, va, vb))
                continue
            x1, y1, x2, y2 = bezier
            dv = vb - va
            pieces.append(Piece(BEZIER, va, vb, x1, va + y1 * dv, x2, va + y2 * dv))
    return values, pieces


def key_times(curves: Sequence[Optional[Curve]], t_in: float, t_out: float) -> List[float]:
    """AE key times for *curves* inside a clip window [t_in, t_out].

    Every point strictly inside the window, plus a window edge wherever a
    curve's segment crosses it (that segment is then split there). A curve
    whose points all lie before or after the window is constant inside it.
    """
    seen: dict = {}

    def add(t: float) -> None:
        seen.setdefault(round(t, 9), t)

    for curve in curves:
        if curve is None or len(curve.points) < 2:
            continue
        pts = curve.points
        for p in pts:
            if t_in + _EPS < p.time < t_out - _EPS:
                add(p.time)
        if pts[0].time < t_in - _EPS and pts[-1].time > t_in + _EPS:
            add(t_in)
        elif t_in - _EPS <= pts[0].time <= t_in + _EPS:
            add(t_in)
        if pts[0].time < t_out - _EPS and pts[-1].time > t_out + _EPS:
            add(t_out)
        elif t_out - _EPS <= pts[-1].time <= t_out + _EPS:
            add(t_out)
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------------------
# Eases and tracks
# ---------------------------------------------------------------------------

def piece_eases(p: Piece, dt: float) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """((speed, influence) out of the first key, (speed, influence) into the second) for one piece.

    Influence is a fraction (AE percent / 100), clamped to [0.001, 1]; a zero
    handle width gives speed 0 (TASTE.md section 2).
    """
    if dt <= 0:
        return (0.0, LINEAR_INFLUENCE), (0.0, LINEAR_INFLUENCE)
    if p.kind == BEZIER:
        x1, x2 = p.x1, p.x2
        out_speed = (p.c1 - p.v0) / (x1 * dt) if x1 > 1e-9 else 0.0
        in_speed = (p.v1 - p.c2) / ((1.0 - x2) * dt) if (1.0 - x2) > 1e-9 else 0.0
        return ((out_speed, min(1.0, max(MIN_INFLUENCE, x1))),
                (in_speed, min(1.0, max(MIN_INFLUENCE, 1.0 - x2))))
    if p.kind == LINEAR:
        slope = (p.v1 - p.v0) / dt
        return (slope, LINEAR_INFLUENCE), (slope, LINEAR_INFLUENCE)
    return (0.0, LINEAR_INFLUENCE), (0.0, LINEAR_INFLUENCE)


@dataclass
class Track:
    """Keyframes of one AE property.

    ``values`` hold one tuple per key (one float per dimension), ``kinds``
    one interpolation per span, ``outs``/``ins`` one list per span with a
    ``(speed, influence)`` ease per ease dimension (one for a spatial or 1-D
    property, one per dimension otherwise). ``spatial`` marks a Position
    keyed as one spatial property (linear motion path, ease along it).
    """

    times: List[float]
    values: List[Value]
    kinds: List[str]
    outs: List[List[Tuple[float, float]]]
    ins: List[List[Tuple[float, float]]]
    spatial: bool = False
    sampled: bool = False

    @property
    def dims(self) -> int:
        return len(self.values[0]) if self.values else 0


def combine_pieces(times: Sequence[float], dim_values: Sequence[Sequence[float]],
                   dim_pieces: Sequence[Sequence[Piece]]) -> Optional[Track]:
    """A non-spatial track from per-dimension pieces sharing *times* (None when AE cannot hold it).

    AE's interpolation type is per key, shared by every dimension: a span
    where one moving dimension holds and another does not cannot be keyed.
    """
    n = len(times)
    values = [tuple(float(dim_values[d][i]) for d in range(len(dim_values))) for i in range(n)]
    kinds, outs, ins = [], [], []
    for j in range(n - 1):
        dt = times[j + 1] - times[j]
        span = [dim_pieces[d][j] for d in range(len(dim_pieces))]
        moving = [p for p in span if not p.flat]
        if moving and any(p.kind == HOLD for p in moving):
            if not all(p.kind == HOLD for p in moving):
                return None
            kind = HOLD
        elif any(p.kind == BEZIER for p in moving):
            kind = BEZIER
        else:
            kind = LINEAR if moving or not any(p.kind == HOLD for p in span) else HOLD
        o, i = [], []
        for p in span:
            if kind == BEZIER and p.kind == HOLD:  # a flat hold inside an eased span
                p = Piece(LINEAR, p.v0, p.v1)
            eo, ei = piece_eases(p, dt)
            o.append(eo)
            i.append(ei)
        kinds.append(kind)
        outs.append(o)
        ins.append(i)
    return Track(list(times), values, kinds, outs, ins)


def spatial_track(times: Sequence[float], xs: Sequence[float], ys: Sequence[float],
                  px: Sequence[Piece], py: Sequence[Piece]) -> Optional[Track]:
    """Position as one spatial property when both dimensions move along straight lines in step.

    Each span needs the same interpolation and the same normalized easing in
    every moving dimension (motion along a straight line); its ease is then
    one KeyframeEase on the path length (TASTE.md section 2: spatial
    properties use the path length as dv). None when that does not hold.
    """
    kinds, outs, ins = [], [], []
    for j in range(len(times) - 1):
        dt = times[j + 1] - times[j]
        a, b = px[j], py[j]
        moving = [p for p in (a, b) if not p.flat]
        if not moving:
            kinds.append(LINEAR)
            outs.append([(0.0, LINEAR_INFLUENCE)])
            ins.append([(0.0, LINEAR_INFLUENCE)])
            continue
        if any(p.kind != moving[0].kind for p in moving):
            return None
        kind = moving[0].kind
        if kind == HOLD:
            kinds.append(HOLD)
            outs.append([(0.0, LINEAR_INFLUENCE)])
            ins.append([(0.0, LINEAR_INFLUENCE)])
            continue
        length = math.hypot(a.v1 - a.v0, b.v1 - b.v0)
        if kind == LINEAR:
            kinds.append(LINEAR)
            speed = length / dt if dt > 0 else 0.0
            outs.append([(speed, LINEAR_INFLUENCE)])
            ins.append([(speed, LINEAR_INFLUENCE)])
            continue
        norm = None
        for p in moving:
            dv = p.v1 - p.v0
            if abs(dv) <= 1e-12:
                return None  # an overshoot with no net move: not a straight path
            this = (p.x1, (p.c1 - p.v0) / dv, p.x2, (p.c2 - p.v0) / dv)
            if norm is None:
                norm = this
            elif any(abs(u - v) > 1e-6 for u, v in zip(norm, this)):
                return None
        assert norm is not None
        x1, y1, x2, y2 = norm
        out_speed = y1 * length / (x1 * dt) if x1 > 1e-9 and dt > 0 else 0.0
        in_speed = (1.0 - y2) * length / ((1.0 - x2) * dt) if (1.0 - x2) > 1e-9 and dt > 0 else 0.0
        kinds.append(BEZIER)
        outs.append([(out_speed, min(1.0, max(MIN_INFLUENCE, x1)))])
        ins.append([(in_speed, min(1.0, max(MIN_INFLUENCE, 1.0 - x2)))])
    values = [(float(x), float(y)) for x, y in zip(xs, ys)]
    return Track(list(times), values, kinds, outs, ins, spatial=True)


# ---------------------------------------------------------------------------
# Evaluating a track the way After Effects does (for verification and tests)
# ---------------------------------------------------------------------------

def _ease_curve_value(t0: float, v0: float, t1: float, v1: float, out_ease: Tuple[float, float],
                      in_ease: Tuple[float, float], t: float) -> float:
    dt = t1 - t0
    so, io = out_ease
    si, ii = in_ease
    ctrl = [(t0, v0), (t0 + io * dt, v0 + so * io * dt), (t1 - ii * dt, v1 - si * ii * dt), (t1, v1)]
    return _bezier_point(ctrl, _solve_u(ctrl, t))[1]


def track_value(track: Track, t: float) -> Value:
    """The track's value at time *t* as After Effects interpolates it."""
    times, values = track.times, track.values
    if t <= times[0]:
        return values[0]
    if t >= times[-1]:
        return values[-1]
    j = 0
    while j < len(times) - 2 and times[j + 1] <= t:
        j += 1
    t0, t1 = times[j], times[j + 1]
    a, b = values[j], values[j + 1]
    kind = track.kinds[j]
    if kind == HOLD:
        return a
    if kind == LINEAR or t1 <= t0:
        s = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        return tuple(x + (y - x) * s for x, y in zip(a, b))
    if track.spatial:
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length <= 1e-12:
            return a
        dist = _ease_curve_value(t0, 0.0, t1, length, track.outs[j][0], track.ins[j][0], t)
        s = dist / length
        return tuple(x + (y - x) * s for x, y in zip(a, b))
    return tuple(_ease_curve_value(t0, a[d], t1, b[d], track.outs[j][d], track.ins[j][d], t)
                 for d in range(len(a)))


def max_error(track: Track, frame_times: Sequence[float], model: Sequence[Value]) -> List[float]:
    """Per-dimension largest |AE value - model value| over the frames."""
    worst = [0.0] * len(model[0]) if model else []
    for t, m in zip(frame_times, model):
        v = track_value(track, t)
        for d in range(len(m)):
            worst[d] = max(worst[d], abs(v[d] - m[d]))
    return worst


# ---------------------------------------------------------------------------
# Sampling fallback
# ---------------------------------------------------------------------------

def simplify(frame_times: Sequence[float], model: Sequence[Value], tol: Sequence[float]) -> List[int]:
    """Indices of the samples to keep so linear interpolation stays within *tol* (per dimension).

    Ramer-Douglas-Peucker on a function of time: the error of a dropped
    sample is its vertical distance (per dimension, relative to its
    tolerance) from the line between the kept neighbours.
    """
    n = len(frame_times)
    if n <= 2:
        return list(range(n))
    keep = {0, n - 1}
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        ti, tj = frame_times[i], frame_times[j]
        best, best_k = 0.0, -1
        for k in range(i + 1, j):
            s = (frame_times[k] - ti) / (tj - ti) if tj > ti else 0.0
            err = 0.0
            for d in range(len(model[k])):
                line = model[i][d] + (model[j][d] - model[i][d]) * s
                err = max(err, abs(model[k][d] - line) / max(tol[d], 1e-12))
            if err > best:
                best, best_k = err, k
        if best > 1.0:
            keep.add(best_k)
            stack.append((i, best_k))
            stack.append((best_k, j))
    return sorted(keep)


def sampled_track(frame_times: Sequence[float], model: Sequence[Value], tol: Sequence[float],
                  *, hold_jumps: bool = False) -> Track:
    """Linear keys through the per-frame samples, reduced within *tol*."""
    keep = simplify(frame_times, model, tol)
    times = [frame_times[k] for k in keep]
    values = [tuple(float(x) for x in model[k]) for k in keep]
    kinds, outs, ins = [], [], []
    dims = len(values[0]) if values else 0
    for j in range(len(times) - 1):
        dt = times[j + 1] - times[j]
        kinds.append(LINEAR)
        o, i = [], []
        for d in range(dims):
            slope = (values[j + 1][d] - values[j][d]) / dt if dt > 0 else 0.0
            o.append((slope, LINEAR_INFLUENCE))
            i.append((slope, LINEAR_INFLUENCE))
        outs.append(o)
        ins.append(i)
    return Track(times, values, kinds, outs, ins, sampled=True)


def pin(track: Track, pins: Mapping[int, Value]) -> Track:
    """*track* with the value of each key index in *pins* replaced; the spans touching those keys become linear.

    For a non-spatial track (one ease per dimension).
    """
    if track.spatial:
        raise ValueError("pin() takes a non-spatial track")
    values = list(track.values)
    for j, value in pins.items():
        values[j] = tuple(float(x) for x in value)
    kinds, outs, ins = list(track.kinds), list(track.outs), list(track.ins)
    for s in sorted({s for j in pins for s in (j - 1, j) if 0 <= s < len(kinds)}):
        dt = track.times[s + 1] - track.times[s]
        eases = [piece_eases(Piece(LINEAR, values[s][d], values[s + 1][d]), dt) for d in range(len(values[s]))]
        kinds[s] = LINEAR
        outs[s] = [e[0] for e in eases]
        ins[s] = [e[1] for e in eases]
    return Track(list(track.times), values, kinds, outs, ins, spatial=track.spatial, sampled=track.sampled)


def is_constant(model: Sequence[Value], tol: Sequence[float]) -> bool:
    if not model:
        return True
    first = model[0]
    return all(abs(m[d] - first[d]) <= tol[d] for m in model for d in range(len(first)))


# ---------------------------------------------------------------------------
# Building one property
# ---------------------------------------------------------------------------

Static = Value
PropertyKeys = Union[Static, Track]


@dataclass
class Dimension:
    """How one AE dimension is computed: its exact per-frame values and the curve it follows, if any."""

    samples: List[float]
    curve: Optional[Curve] = None


@dataclass
class BuildResult:
    keys: PropertyKeys
    notes: List[str] = field(default_factory=list)

    @property
    def animated(self) -> bool:
        return isinstance(self.keys, Track)


def _affine_fit(xs: Sequence[float], ys: Sequence[float]) -> Optional[Tuple[float, float, float]]:
    """Least-squares y = a*x + b; returns (a, b, max residual) or None when x does not vary."""
    n = len(xs)
    if n == 0:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-18:
        return None
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    b = my - a * mx
    worst = max(abs(y - (a * x + b)) for x, y in zip(xs, ys))
    return a, b, worst


def build_property(frame_times: Sequence[float], dims: Sequence[Dimension], *, t_in: float, t_out: float,
                   tol: Sequence[float], spatial: bool = False, cuts: Sequence[float] = ()) -> BuildResult:
    """Static value, eased keys or sampled keys for one AE property (see the module docstring).

    *frame_times* are the times of the frames the clip shows; each
    dimension's ``samples`` its exact value at those frames and ``curve`` the
    libopenshot curve it is an affine function of, when it follows one.
    *cuts* are extra key times for eased keys (the curve is split there exactly).
    """
    model = [tuple(d.samples[k] for d in dims) for k in range(len(frame_times))]
    if not model:
        return BuildResult(tuple(0.0 for _ in dims))
    if is_constant(model, tol):
        return BuildResult(model[0])

    fits: List[Optional[Tuple[float, float]]] = []
    eased = True
    for d, dim in enumerate(dims):
        column = [m[d] for m in model]
        if max(column) - min(column) <= tol[d]:
            fits.append(None)  # flat in this window
            continue
        if not is_animated_in(dim.curve, t_in, t_out):
            eased = False
            break
        assert dim.curve is not None
        xs = [exact_value(dim.curve, t) for t in frame_times]
        fit = _affine_fit(xs, column)
        if fit is None or fit[2] > tol[d] * 0.25:
            eased = False
            break
        fits.append((fit[0], fit[1]))

    if eased:
        curves = [dim.curve for dim, f in zip(dims, fits) if f is not None]
        times = key_times(curves, t_in, t_out)
        if cuts and len(times) >= 2:
            merged = {round(t, 9): t for t in cuts if times[0] + _EPS < t < times[-1] - _EPS}
            merged.update({round(t, 9): t for t in times})
            times = [merged[k] for k in sorted(merged)]
        if len(times) >= 2:
            dim_values, dim_pieces = [], []
            for dim, f, d in zip(dims, fits, range(len(dims))):
                if f is None:
                    value = model[0][d]
                    dim_values.append([value] * len(times))
                    dim_pieces.append([Piece(LINEAR, value, value)] * (len(times) - 1))
                    continue
                assert dim.curve is not None
                values, pieces = curve_pieces(dim.curve, times)
                dim_values.append([f[0] * v + f[1] for v in values])
                dim_pieces.append([p.mapped(f[0], f[1]) for p in pieces])
            track = None
            if spatial and len(dims) == 2:
                track = spatial_track(times, dim_values[0], dim_values[1], dim_pieces[0], dim_pieces[1])
            if track is None:
                track = combine_pieces(times, dim_values, dim_pieces)
            if track is not None:
                errors = max_error(track, frame_times, model)
                if all(e <= t for e, t in zip(errors, tol)):
                    return BuildResult(track)
    return BuildResult(sampled_track(frame_times, model, tol), notes=["sampled"])


def frame_times(t_in: float, t_out: float, fps: float) -> List[float]:
    """Times of the frames a clip shows in [t_in, t_out) (the last frame starts before t_out)."""
    if t_out <= t_in or fps <= 0:
        return [t_in]
    first = int(math.ceil(t_in * fps - 1e-6))
    last = int(math.ceil(t_out * fps - 1e-6)) - 1
    out = [k / fps for k in range(first, max(first, last) + 1)]
    return out or [t_in]


Sampler = Callable[[float], Value]

__all__ = [
    "BEZIER", "LINEAR", "HOLD", "Piece", "Track", "Dimension", "BuildResult", "exact_value", "is_animated_in",
    "curve_pieces", "key_times", "piece_eases", "combine_pieces", "spatial_track", "track_value", "max_error",
    "simplify", "sampled_track", "pin", "build_property", "frame_times", "is_constant",
]
