"""Unit tests for classes.frame_time — the single conversion authority."""

from __future__ import annotations

import math
import random
from fractions import Fraction

import pytest

from classes import frame_time as ft
from classes.clip_utils import video_length_to_project_frames


FPS_RATES = [
    Fraction(24, 1),
    Fraction(25, 1),
    Fraction(30, 1),
    Fraction(24000, 1001),
    Fraction(30000, 1001),
    Fraction(60000, 1001),
]


@pytest.mark.parametrize("fps", FPS_RATES)
def test_round_trip_stability(fps):
    for n in range(0, 10_000, 97):
        secs = ft.to_seconds(n, fps)
        assert ft.to_frame(secs, fps) == n


@pytest.mark.parametrize("fps", FPS_RATES)
def test_round_trip_large_frames(fps):
    for n in (0, 1, 10**6, 10**7):
        assert ft.to_frame(ft.to_seconds(n, fps), fps) == n


def test_half_up_parity_cases():
    fps = Fraction(30, 1)
    # 0.5 frames -> 1, 1.5 -> 2, 2.5 -> 3 (not banker's 2)
    assert ft.to_frame(0.5 / 30.0, fps) == 1
    assert ft.to_frame(1.5 / 30.0, fps) == 2
    assert ft.to_frame(2.5 / 30.0, fps) == 3
    assert ft.round_half_up(2.5) == 3
    assert ft.round_half_up(-2.5) == -3


def test_snap_is_idempotent():
    fps = Fraction(30, 1)
    for raw in (0.0, 0.01, 1.234567, 99.999, 2.5 / 30.0):
        once = ft.snap(raw, fps)
        assert ft.snap(once, fps) == once
        assert ft.is_aligned(once, fps)


def test_quantize_span_preserves_duration_random():
    rng = random.Random(42)
    fps = Fraction(30, 1)
    for _ in range(1000):
        position = rng.uniform(0, 100)
        start = rng.uniform(0, 50)
        end = start + rng.uniform(1 / 30.0, 20)
        orig_dur = ft.duration_frames(start, end, fps)
        _p, qs, qe = ft.quantize_span(position, start, end, fps)
        assert ft.duration_frames(qs, qe, fps) == orig_dur
        assert ft.is_aligned(_p, fps)
        assert ft.is_aligned(qs, fps)
        assert ft.is_aligned(qe, fps)


def test_duration_frames_never_less_than_one_when_equal():
    fps = Fraction(24, 1)
    assert ft.duration_frames(1.0, 1.0, fps) == 1


def test_keyframe_x_is_one_indexed():
    fps = Fraction(30, 1)
    assert ft.keyframe_x(0.0, fps) == 1
    assert ft.keyframe_x(1.0, fps) == 31
    assert ft.keyframe_x_to_seconds(31, fps) == pytest.approx(1.0)


def test_rejects_non_finite():
    fps = Fraction(30, 1)
    with pytest.raises(ValueError):
        ft.to_frame(float("nan"), fps)
    with pytest.raises(ValueError):
        ft.to_frame(float("inf"), fps)
    with pytest.raises(ValueError):
        ft.snap(-math.inf, fps)
    with pytest.raises(ValueError):
        ft._as_fraction(0)
    with pytest.raises(ValueError):
        ft._as_fraction(-24)


def test_mixed_source_project_fps_agrees_with_clip_utils():
    # 23.976 media (24000/1001), 1000 source frames, project 30fps
    source_fps = Fraction(24000, 1001)
    project_fps = Fraction(30, 1)
    frames = video_length_to_project_frames(
        video_length=1000,
        fps={"num": 24000, "den": 1001},
        project_fps=project_fps,
    )
    # Same mapping via Fraction arithmetic used by frame_time callers.
    scaled = int(round(float(Fraction(1000) * project_fps / source_fps)))
    assert frames == max(scaled, 1)


def test_is_aligned_false_for_off_grid():
    fps = Fraction(30, 1)
    assert ft.is_aligned(1.0 / 30.0, fps)
    assert not ft.is_aligned(1.0 / 30.0 + 1e-4, fps)
