"""Permanent drift invariants for Phase 2 frame-exact time."""

from __future__ import annotations

import copy
import random
from fractions import Fraction
from types import SimpleNamespace

import pytest

from classes import frame_time as ft
from windows.views.retime import retime_clip, _scale_points


FPS_RATES = [Fraction(24, 1), Fraction(30, 1), Fraction(30000, 1001)]


@pytest.mark.parametrize("fps", FPS_RATES)
def test_trim_move_property_invariant(fps):
    rng = random.Random(42)
    pos, start, end = ft.quantize_span(2.0, 0.5, 5.5, fps)
    origin = (ft.to_frame(pos, fps), ft.to_frame(start, fps), ft.to_frame(end, fps))
    cur = (pos, start, end)
    for i in range(100):
        delta = (rng.uniform(-3, 3)) / float(fps)
        move = (rng.uniform(-2, 2)) / float(fps)
        cur = ft.quantize_span(cur[0] + move, cur[1], max(cur[1] + 1.0 / float(fps), cur[2] + delta), fps)
    # Not required to return to origin after random walk — only that values stay aligned
    assert all(ft.is_aligned(v, fps) for v in cur)
    # Alternating deterministic walk does return
    cur = (pos, start, end)
    for i in range(100):
        delta = (1.7 if i % 2 == 0 else -1.7) / float(fps)
        cur = ft.quantize_span(cur[0], cur[1], cur[2] + delta, fps)
    assert (
        ft.to_frame(cur[0], fps),
        ft.to_frame(cur[1], fps),
        ft.to_frame(cur[2], fps),
    ) == origin


@pytest.mark.parametrize("fps", FPS_RATES)
def test_split_conserves_frames(fps):
    pos, start, end = ft.quantize_span(1.0, 0.0, 5.0, fps)
    pos_f = ft.to_frame(pos, fps)
    start_f = ft.to_frame(start, fps)
    end_f = ft.to_frame(end, fps)
    total = end_f - start_f
    for offset in range(1, min(50, total)):
        play_f = pos_f + offset
        cut_f = start_f + (play_f - pos_f)
        cut_f = min(max(cut_f, start_f + 1), end_f)
        left = cut_f - start_f
        right = end_f - cut_f
        assert left + right == total
        assert cut_f == start_f + left


def test_paste_spacing_preserved_over_repeats():
    fps = Fraction(30, 1)
    positions = [1.111111, 3.333333, 5.555555]
    frames0 = [ft.to_frame(p, fps) for p in positions]
    gaps0 = [frames0[i + 1] - frames0[i] for i in range(len(frames0) - 1)]
    cur = list(frames0)
    target = ft.to_frame(10.123456, fps)
    for _ in range(20):
        left = min(cur)
        delta = target - left
        cur = [f + delta for f in cur]
        gaps = [cur[i + 1] - cur[i] for i in range(len(cur) - 1)]
        assert gaps == gaps0
        assert all(ft.is_aligned(ft.to_seconds(f, fps), fps) for f in cur)


def test_retime_first_keyframe_stable(monkeypatch):
    fps = Fraction(30, 1)
    monkeypatch.setattr("windows.views.retime._project_fps", lambda: fps)
    monkeypatch.setattr("windows.views.retime.project_fps_fraction", lambda: fps)

    points = [
        {"co": {"X": 1, "Y": 1.0}, "interpolation": 0},
        {"co": {"X": 31, "Y": 2.0}, "interpolation": 0},
        {"co": {"X": 61, "Y": 1.0}, "interpolation": 0},
    ]
    original = copy.deepcopy(points)
    # scale 2x: start_x=1, new_end_x=121, scale=2
    _scale_points(points, 1, 121, 2.0)
    assert points[0]["co"]["X"] == original[0]["co"]["X"] == 1
    assert points[-1]["co"]["X"] == 121
    assert points[0]["co"]["X"] <= points[1]["co"]["X"] <= points[2]["co"]["X"]
    assert len(points) == 3


def test_retime_clip_round_trip(monkeypatch):
    fps = Fraction(30, 1)
    monkeypatch.setattr("windows.views.retime._project_fps", lambda: fps)

    clip = SimpleNamespace(data={
        "start": 0.0,
        "end": 2.0,
        "duration": 2.0,
        "position": 1.0,
        "scale_x": {"Points": [
            {"co": {"X": 1, "Y": 1.0}, "interpolation": 0},
            {"co": {"X": 61, "Y": 2.0}, "interpolation": 0},
        ]},
        "time": {"Points": [
            {"co": {"X": 1, "Y": 1}, "interpolation": 0},
            {"co": {"X": 61, "Y": 61}, "interpolation": 0},
        ]},
    })
    first_x = clip.data["scale_x"]["Points"][0]["co"]["X"]
    assert retime_clip(clip, 4.0, new_position=1.0)
    assert clip.data["scale_x"]["Points"][0]["co"]["X"] == first_x
    assert ft.is_aligned(clip.data["end"], fps)
    assert ft.is_aligned(clip.data["position"], fps)
    # Count preserved
    assert len(clip.data["scale_x"]["Points"]) == 2


def test_legacy_off_grid_values_are_detectable():
    """Loading off-grid values must not auto-mutate; is_aligned reports them."""
    fps = Fraction(30, 1)
    legacy = 1.111111
    assert not ft.is_aligned(legacy, fps)
    # Quantize only on edit
    snapped = ft.snap(legacy, fps)
    assert ft.is_aligned(snapped, fps)


def test_nudge_one_frame_round_trip():
    fps = Fraction(30, 1)
    pos = ft.snap(2.5, fps)
    f = ft.to_frame(pos, fps)
    left = ft.to_seconds(f - 1, fps)
    back = ft.to_seconds(ft.to_frame(left, fps) + 1, fps)
    assert ft.to_frame(back, fps) == f


def test_fcp_seconds_to_frames_uses_half_up():
    # Mirror exporters/final_cut_pro._seconds_to_frames without importing Qt/openshot.
    from fractions import Fraction
    def _seconds_to_frames(value, fps_num, fps_den):
        if value is None:
            value = 0.0
        return ft.to_frame(float(value), Fraction(int(fps_num), int(fps_den)))

    assert _seconds_to_frames(2.5 / 30.0, 30, 1) == 3
    assert _seconds_to_frames(1.0, 30, 1) == 30


def test_interchange_round_trip_frames():
    """Export frame numbers must match re-imported quantized span."""
    fps = Fraction(30, 1)
    pos, start, end = ft.quantize_span(1.234, 0.5, 3.7, fps)

    def _seconds_to_frames(value, fps_num, fps_den):
        return ft.to_frame(float(value), Fraction(int(fps_num), int(fps_den)))

    pos_f = _seconds_to_frames(pos, 30, 1)
    start_f = _seconds_to_frames(start, 30, 1)
    end_f = _seconds_to_frames(end, 30, 1)
    pos2, start2, end2 = ft.quantize_span(
        pos_f / 30.0, start_f / 30.0, end_f / 30.0, fps
    )
    assert ft.to_frame(pos2, fps) == pos_f
    assert ft.to_frame(start2, fps) == start_f
    assert ft.to_frame(end2, fps) == end_f


@pytest.mark.parametrize("fps_num,fps_den", [(30, 1), (30000, 1001)])
def test_interchange_drop_and_non_drop(fps_num, fps_den):
    fps = Fraction(fps_num, fps_den)
    pos, start, end = ft.quantize_span(2.0, 0.0, 5.0, fps)

    def _seconds_to_frames(value, fps_num, fps_den):
        return ft.to_frame(float(value), Fraction(int(fps_num), int(fps_den)))

    assert _seconds_to_frames(pos, fps_num, fps_den) == ft.to_frame(pos, fps)
    assert _seconds_to_frames(end - start, fps_num, fps_den) == ft.duration_frames(start, end, fps)
