"""After Effects keyframes from libopenshot curves (classes.exporters.after_effects_keys).

The checks evaluate the AE keys the way After Effects draws them (a cubic
Bezier in time and value built from each key's KeyframeEase) and compare them
with the exact libopenshot curve, so a wrong ease formula or a bad split shows
up as a numeric error rather than a different-looking file.
"""

import math
import random

import pytest

from classes.exporters import after_effects_keys as K
from classes.handoff.keyframes import Curve

FPS = 30

# TASTE.md section 2 named easings (cubic-bezier control points).
TASTE = {"smooth": (0.33, 0.0, 0.20, 1.0), "out": (0.16, 1.0, 0.30, 1.0), "in": (0.70, 0.0, 0.84, 0.0),
         "in-out": (0.65, 0.0, 0.35, 1.0), "snap": (0.20, 0.0, 0.0, 1.0), "editor-default": (0.5, 0.0, 0.5, 1.0)}


def _curve(points, *, position=0.0, start=0.0, fps=FPS):
    """points: (X, Y, interpolation, (x1, y1) handle_right of this point, (x2, y2) handle_left of this point)."""
    raw = []
    for p in points:
        x, y = p[0], p[1]
        interp = p[2] if len(p) > 2 else 0
        hr = p[3] if len(p) > 3 and p[3] else (0.5, 0.0)
        hl = p[4] if len(p) > 4 and p[4] else (0.5, 1.0)
        raw.append({"co": {"X": x, "Y": y}, "interpolation": interp, "handle_right": {"X": hr[0], "Y": hr[1]},
                    "handle_left": {"X": hl[0], "Y": hl[1]}})
    return Curve.from_json({"Points": raw}, fps=fps, position=position, start=start)


def _eased(name, v0=0.0, v1=100.0, frames=30):
    x1, y1, x2, y2 = TASTE[name]
    return _curve([(1, v0, 0, (x1, y1)), (1 + frames, v1, 0, None, (x2, y2))])


def _track_for(curve, times):
    values, pieces = K.curve_pieces(curve, times)
    return K.combine_pieces(times, [values], [pieces])


def _worst(track, curve, t0, t1, n=400):
    worst = 0.0
    for i in range(n + 1):
        t = t0 + (t1 - t0) * i / n
        worst = max(worst, abs(K.track_value(track, t)[0] - K.exact_value(curve, t)))
    return worst


@pytest.mark.parametrize("name", sorted(TASTE))
def test_whole_segment_converts_with_the_taste_formula(name):
    x1, y1, x2, y2 = TASTE[name]
    curve = _eased(name, 10.0, 70.0, frames=30)  # dv = 60 over dt = 1 s
    track = _track_for(curve, [0.0, 1.0])
    assert track.kinds == [K.BEZIER]
    (s_out, i_out), = track.outs[0]
    (s_in, i_in), = track.ins[0]
    dv_dt = 60.0
    assert i_out == pytest.approx(max(x1, K.MIN_INFLUENCE))
    assert i_in == pytest.approx(max(1 - x2, K.MIN_INFLUENCE))
    assert s_out == pytest.approx((y1 / x1) * dv_dt if x1 else 0.0)
    assert s_in == pytest.approx(((1 - y2) / (1 - x2)) * dv_dt if x2 < 1 else 0.0)
    if x1 > 0 and x2 < 1:
        assert _worst(track, curve, 0.0, 1.0) < 1e-6


@pytest.mark.parametrize("cut", [(0.1, 0.9), (0.0, 0.37), (0.61, 1.0), (0.25, 0.26)])
def test_cutting_a_bezier_segment_stays_exact(cut):
    curve = _eased("in-out", -40.0, 260.0, frames=30)
    track = _track_for(curve, list(cut))
    assert track.kinds == [K.BEZIER]
    assert track.values[0][0] == pytest.approx(K.exact_value(curve, cut[0]))
    assert _worst(track, curve, *cut) < 1e-6


def test_a_clip_window_cuts_segments_at_its_edges_and_keeps_inner_keys():
    curve = _curve([(1, 0.0), (31, 100.0), (61, 20.0, 1), (91, 20.0, 2), (121, 80.0)], position=0.0)
    times = K.key_times([curve], 0.5, 3.5)
    assert times == pytest.approx([0.5, 1.0, 2.0, 3.0, 3.5])
    track = _track_for(curve, times)
    # a span takes the interpolation of the point that ends it (libopenshot InterpolateBetween)
    assert track.kinds == [K.BEZIER, K.LINEAR, K.HOLD, K.BEZIER]
    assert _worst(track, curve, 0.5, 3.5) < 1e-6


def test_hold_spans_jump_at_the_next_key():
    curve = _curve([(1, 5.0), (31, 9.0, 2)])
    track = _track_for(curve, [0.0, 1.0])
    assert track.kinds == [K.HOLD]
    assert K.track_value(track, 0.99)[0] == 5.0 and K.track_value(track, 1.0)[0] == 9.0


def test_points_outside_the_window_leave_a_constant():
    curve = _curve([(1, 0.0), (31, 50.0)])
    assert K.key_times([curve], 2.0, 4.0) == []
    assert not K.is_animated_in(curve, 2.0, 4.0)
    assert K.is_animated_in(curve, 0.5, 4.0)


def test_scale_dimensions_with_different_keys_share_exactly_split_keys():
    sx = _eased("out", 100.0, 140.0, frames=30)
    sy = _curve([(1, 100.0), (16, 90.0, 0, (0.2, 0.0), (0.9, 1.0)), (46, 120.0, 0, None, (0.1, 1.0))])
    times = K.key_times([sx, sy], 0.0, 1.5)
    assert times == pytest.approx([0.0, 0.5, 1.0, 1.5])
    vx, px = K.curve_pieces(sx, times)
    vy, py = K.curve_pieces(sy, times)
    track = K.combine_pieces(times, [vx, vy], [px, py])
    for i in range(301):
        t = 1.5 * i / 300
        got = K.track_value(track, t)
        assert got[0] == pytest.approx(K.exact_value(sx, t), abs=1e-6)
        assert got[1] == pytest.approx(K.exact_value(sy, t), abs=1e-6)


def test_a_dimension_holding_while_another_moves_cannot_share_keys():
    a = _curve([(1, 0.0), (31, 10.0, 2)])
    b = _curve([(1, 0.0), (31, 10.0, 1)])
    va, pa = K.curve_pieces(a, [0.0, 1.0])
    vb, pb = K.curve_pieces(b, [0.0, 1.0])
    assert K.combine_pieces([0.0, 1.0], [va, vb], [pa, pb]) is None


def test_position_moving_in_step_is_one_spatial_property_with_path_length_eases():
    x = _eased("smooth", 0.0, 300.0)
    y = _eased("smooth", 0.0, 400.0)
    vx, px = K.curve_pieces(x, [0.0, 1.0])
    vy, py = K.curve_pieces(y, [0.0, 1.0])
    track = K.spatial_track([0.0, 1.0], vx, vy, px, py)
    assert track is not None and track.spatial
    x1, y1, x2, y2 = TASTE["smooth"]
    (speed, infl), = track.outs[0]
    assert infl == pytest.approx(x1) and speed == pytest.approx(y1 / x1 * 500.0)
    for i in range(101):
        t = i / 100
        got = K.track_value(track, t)
        assert got[0] == pytest.approx(K.exact_value(x, t), abs=1e-6)
        assert got[1] == pytest.approx(K.exact_value(y, t), abs=1e-6)
    other = _eased("snap", 0.0, 400.0)
    vo, po = K.curve_pieces(other, [0.0, 1.0])
    assert K.spatial_track([0.0, 1.0], vx, vo, px, po) is None


def test_simplify_keeps_every_frame_within_tolerance():
    rng = random.Random(7)
    times = [k / FPS for k in range(300)]
    values = [(math.sin(t * 0.5) * 20 + rng.random() * 0.01, math.cos(t * 0.3) * 20) for t in times]
    tol = (0.05, 0.05)
    track = K.sampled_track(times, values, tol)
    assert len(track.times) < len(times) / 3
    for t, v in zip(times, values):
        got = K.track_value(track, t)
        assert abs(got[0] - v[0]) <= tol[0] + 1e-9 and abs(got[1] - v[1]) <= tol[1] + 1e-9


def test_build_property_eases_affine_values_and_samples_the_rest():
    curve = _eased("in-out", 0.0, 1.0, frames=30)
    frames = K.frame_times(0.0, 1.0, FPS)
    affine = [960.0 + 1920.0 * K.exact_value(curve, t) for t in frames]
    eased = K.build_property(frames, [K.Dimension(affine, curve)], t_in=0.0, t_out=1.0, tol=[0.01])
    assert isinstance(eased.keys, K.Track) and not eased.keys.sampled and eased.keys.kinds == [K.BEZIER]
    db = [20 * math.log10(max(1e-6, K.exact_value(curve, t))) for t in frames]
    sampled = K.build_property(frames, [K.Dimension(db, curve)], t_in=0.0, t_out=1.0, tol=[0.1])
    assert isinstance(sampled.keys, K.Track) and sampled.keys.sampled
    constant = K.build_property(frames, [K.Dimension([5.0] * len(frames), curve)], t_in=0.0, t_out=1.0, tol=[0.01])
    assert constant.keys == (5.0,)


def test_frame_times_cover_the_shown_frames_on_ntsc_rates():
    fps = 30000 / 1001
    start = 1001 * 7 / 30000
    times = K.frame_times(start, start + 1.0, fps)
    assert times[0] == pytest.approx(start)
    assert len(times) == 30  # 29.97 frames in a second, so 30 frame starts fall inside [start, start + 1)
    assert all(b > a for a, b in zip(times, times[1:]))


def test_piece_eases_for_linear_spans_are_straight_beziers():
    p = K.Piece(K.LINEAR, 10.0, 40.0)
    out, into = K.piece_eases(p, 2.0)
    assert out == (15.0, K.LINEAR_INFLUENCE) and into == (15.0, K.LINEAR_INFLUENCE)
    track = K.Track([0.0, 2.0], [(10.0,), (40.0,)], [K.BEZIER], [[out]], [[into]])
    assert K.track_value(track, 1.0)[0] == pytest.approx(25.0, abs=1e-9)


def test_extra_cuts_split_eased_keys_exactly():
    curve = _eased("smooth", 0.0, 90.0, frames=60)
    frames = K.frame_times(0.0, 2.0, FPS)
    samples = [K.exact_value(curve, t) for t in frames]
    built = K.build_property(frames, [K.Dimension(samples, curve)], t_in=0.0, t_out=2.0, tol=[1e-6],
                             cuts=[0.5, 0.5 + 1e-12, 1.2, 2.0, 7.0])
    track = built.keys
    assert isinstance(track, K.Track) and not track.sampled
    assert track.times == pytest.approx([0.0, 0.5, 1.2, 2.0]) and track.kinds == [K.BEZIER] * 3
    assert _worst(track, curve, 0.0, 2.0) < 1e-6


def test_pin_replaces_one_key_and_straightens_only_its_spans():
    curve = _eased("smooth", 0.0, 90.0, frames=60)
    times = [0.0, 0.5, 1.0, 2.0]
    track = _track_for(curve, times)
    pinned = K.pin(track, {2: (50.0,)})
    assert pinned.values[2] == (50.0,) and pinned.values[1] == track.values[1]
    assert pinned.kinds == [K.BEZIER, K.LINEAR, K.LINEAR] and pinned.outs[0] == track.outs[0]
    assert K.track_value(pinned, 1.0)[0] == 50.0
    assert K.track_value(pinned, 0.25)[0] == pytest.approx(K.exact_value(curve, 0.25), abs=1e-6)
    assert K.track_value(pinned, 1.5)[0] == pytest.approx((50.0 + track.values[3][0]) / 2.0)
    with pytest.raises(ValueError):
        K.pin(K.Track([0.0, 1.0], [(0.0, 0.0), (1.0, 1.0)], [K.LINEAR], [[(1.0, 0.3)]], [[(1.0, 0.3)]],
                      spatial=True), {0: (0.5, 0.5)})
