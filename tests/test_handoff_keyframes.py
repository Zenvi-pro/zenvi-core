"""classes.handoff.keyframes: curves evaluated like libopenshot 1.0.

Golden values come from the web engine's parity-tested port
(zenvi-web packages/engine/src/keyframe/evaluate.ts), computed once with
``tsx`` and hard-coded here.
"""

from fractions import Fraction

import pytest

from classes.handoff.keyframes import (
    BEZIER, CONSTANT, LINEAR, Curve, float32, resolve_points, union_times,
)

MIXED = {"Points": [
    {"co": {"X": 1, "Y": 0}, "interpolation": 0, "handle_left": {"X": 0.5, "Y": 1}, "handle_right": {"X": 0.5, "Y": 0}},
    {"co": {"X": 31, "Y": 100}, "interpolation": 0, "handle_left": {"X": 0.5, "Y": 1},
     "handle_right": {"X": 0.16, "Y": 1}},
    {"co": {"X": 61, "Y": 50}, "interpolation": 1},
    {"co": {"X": 91, "Y": 80}, "interpolation": 2},
    {"co": {"X": 121, "Y": 10}, "interpolation": 0, "handle_left": {"X": 0.3, "Y": 1},
     "handle_right": {"X": 0.5, "Y": 0}},
]}
FRAMES = [0, 1, 5, 10, 16, 25, 31, 40, 46, 61, 70, 90, 91, 100, 110, 121, 140]
# engine evaluate(kf, frame) for FRAMES
ENGINE = [0, 0, 2.6747584342956543, 16.01126587484032, 50, 93.55414893943816, 100, 85, 75, 50, 50, 50, 80,
          65.41091298684478, 19.880824238061905, 10, 10]


def test_values_match_the_web_engine_port_of_libopenshot():
    curve = Curve.from_json(MIXED, fps=30)
    got = [curve.value_at_frame(f) for f in FRAMES]
    assert got == pytest.approx(ENGINE, abs=1e-9)


def test_segments_use_the_right_points_interpolation_and_css_control_points():
    segs = Curve.from_json(MIXED, fps=30).segments()
    assert [s.easing for s in segs] == ["bezier", "linear", "hold", "bezier"]
    # bezier = left.handle_right + right.handle_left
    assert segs[0].bezier == (0.5, 0.0, 0.5, 1.0)
    assert segs[3].bezier == (0.5, 0.0, 0.3, 1.0)
    assert segs[1].bezier == (0.0, 0.0, 1.0, 1.0)
    assert segs[2].bezier is None
    assert segs[0].duration == pytest.approx(1.0)


def test_point_times_include_the_trimmed_start_and_position():
    # clip at timeline 10 s, trimmed 2 s into its source, 30 fps: X=61 is source 2 s = first visible frame
    kf = {"Points": [{"co": {"X": 61, "Y": 0}}, {"co": {"X": 91, "Y": 1}}]}
    curve = Curve.from_json(kf, fps=30, position=10.0, start=2.0)
    a, b = curve.points
    assert (a.time, a.local) == pytest.approx((10.0, 0.0))
    assert (b.time, b.local) == pytest.approx((11.0, 1.0))
    assert curve.frame_at(10.0) == 61.0
    assert curve.time_of(91) == pytest.approx(11.0)
    assert curve.value_at(10.0) == 0.0 and curve.value_at(11.0) == 1.0
    assert curve.value_at_local(0.5) == pytest.approx(0.5)  # default handles: ease-in-out, symmetric at the middle


def test_value_at_between_frames_is_fractional_and_on_grid_is_exact():
    curve = Curve.from_json({"Points": [{"co": {"X": 1, "Y": 0}, "interpolation": LINEAR},
                                        {"co": {"X": 31, "Y": 30}, "interpolation": LINEAR}]}, fps=30)
    assert curve.value_at(0.5) == pytest.approx(15.0)
    assert curve.value_at(0.5 + 1 / 60) == pytest.approx(15.5)
    assert curve.value_at_frame(16.9) == 15.0  # GetValue(int64) truncates


def test_bare_numbers_empty_and_malformed_keyframes():
    assert resolve_points(0.7) == [(1.0, float32(0.7), CONSTANT, (0.5, 1.0), (0.5, 0.0), 0)]
    assert Curve.from_json(None, fps=30, default=1.0).value_at(3) == 1.0
    assert Curve.from_json({"Points": []}, fps=30).value_at(3) == 0.0
    assert Curve.from_json({"Points": "nope"}, fps=30, default=2.0).value_at(0) == 2.0
    # missing co -> libopenshot Point() defaults (X 1, Y 0)
    assert Curve.from_json({"Points": [{}]}, fps=30).points[0].frame == 1.0


def test_duplicate_x_last_one_wins_and_points_are_sorted():
    kf = {"Points": [{"co": {"X": 30, "Y": 3}}, {"co": {"X": 1, "Y": 1}}, {"co": {"X": 30, "Y": 9}}]}
    curve = Curve.from_json(kf, fps=30)
    assert [(p.frame, p.value) for p in curve.points] == [(1.0, 1.0), (30.0, 9.0)]


def test_constant_detection_and_scaling():
    assert Curve.constant(1.0, fps=24).is_constant
    flat = Curve.from_json({"Points": [{"co": {"X": 1, "Y": 2}}, {"co": {"X": 50, "Y": 2}}]}, fps=24)
    assert flat.is_constant and not flat.is_animated
    ramp = Curve.from_json({"Points": [{"co": {"X": 1, "Y": 0}}, {"co": {"X": 50, "Y": 1}}]}, fps=24)
    assert ramp.is_animated
    pct = ramp.scaled(100.0)
    assert pct.first_value == 0.0 and pct.last_value == 100.0
    assert pct.points[1].interpolation == BEZIER


def test_fps_accepts_dicts_and_fractions():
    a = Curve.from_json(MIXED, fps={"num": 30000, "den": 1001})
    b = Curve.from_json(MIXED, fps=Fraction(30000, 1001))
    assert a == b and a.fps == Fraction(30000, 1001)
    with pytest.raises(ValueError):
        Curve.from_json(MIXED, fps=0)


def test_union_times_skips_constant_curves_and_adds_edges():
    ramp = Curve.from_json({"Points": [{"co": {"X": 1, "Y": 0}}, {"co": {"X": 31, "Y": 1}}]}, fps=30, position=2.0)
    other = Curve.from_json({"Points": [{"co": {"X": 16, "Y": 0}}, {"co": {"X": 31, "Y": 1}}]}, fps=30,
                            position=2.0)
    flat = Curve.constant(1.0, fps=30, position=2.0)
    assert union_times(ramp, other, flat) == pytest.approx([2.0, 2.5, 3.0])
    assert union_times(ramp, start=2.0, end=2.8) == pytest.approx([2.0, 2.8])
    assert union_times(flat, None) == []


def test_round_trip_to_json_keeps_points():
    curve = Curve.from_json(MIXED, fps=30)
    again = Curve.from_json(curve.to_json(), fps=30)
    assert [p.value for p in again.points] == [p.value for p in curve.points]
    assert again.segments()[0].bezier == curve.segments()[0].bezier


def test_bezier_between_reproduces_the_clipped_part_of_a_segment():
    seg = Curve.from_json(MIXED, fps=30).segments()[0]  # bezier 0 -> 100 over frames 1..31
    curve = Curve.from_json(MIXED, fps=30)
    t0, t1 = 0.2, 0.7
    x1, y1, x2, y2 = seg.bezier_between(t0, t1)
    v0, v1 = curve.value_at(t0), curve.value_at(t1)
    for frac in (0.1, 0.25, 0.5, 0.75, 0.9):
        # evaluate the sub-easing like a CSS cubic-bezier and compare with the original curve
        lo, hi = 0.0, 1.0
        for _ in range(80):
            u = (lo + hi) / 2
            xu = 3 * (1 - u) ** 2 * u * x1 + 3 * (1 - u) * u * u * x2 + u ** 3
            lo, hi = (u, hi) if xu < frac else (lo, u)
        u = (lo + hi) / 2
        yu = 3 * (1 - u) ** 2 * u * y1 + 3 * (1 - u) * u * u * y2 + u ** 3
        assert v0 + yu * (v1 - v0) == pytest.approx(curve.value_at(t0 + frac * (t1 - t0)), abs=0.3)
    assert seg.bezier_between(seg.start.time, seg.end.time) == pytest.approx(seg.bezier, abs=1e-6)
    lin = Curve.from_json(MIXED, fps=30).segments()[1]
    assert lin.bezier_between(1.1, 1.5) == (0.0, 0.0, 1.0, 1.0)
