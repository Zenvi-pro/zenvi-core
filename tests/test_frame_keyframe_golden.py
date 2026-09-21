"""Golden values for generated fade/scale Point X at known fps rates."""

from __future__ import annotations

from fractions import Fraction

import pytest

from classes import frame_time as ft


@pytest.mark.parametrize(
    "fps,start,end,fade,expected_in_end,expected_out_start",
    [
        (Fraction(30, 1), 0.0, 5.0, 1.0, 31, 121),  # fade 1s: in ends at frame 31, out starts at 5s-1s
        (Fraction(24000, 1001), 0.0, 5.0, 1.0, None, None),  # computed below
    ],
)
def test_fade_keyframe_x_golden(fps, start, end, fade, expected_in_end, expected_out_start):
    in_start = ft.keyframe_x(start, fps)
    in_end = ft.keyframe_x(start + fade, fps)
    out_start = ft.keyframe_x(end - fade, fps)
    out_end = ft.keyframe_x(end, fps)
    assert in_start == 1
    if expected_in_end is not None:
        assert in_end == expected_in_end
        assert out_start == expected_out_start
    assert out_end == ft.keyframe_x(end, fps)
    assert in_start < in_end <= out_start < out_end


def test_scale_animation_keyframe_x_at_23976():
    fps = Fraction(24000, 1001)
    start, end = 0.0, 2.0
    assert ft.keyframe_x(start, fps) == 1
    # 2 seconds at 24000/1001 ≈ 47.952 frames → half-up to 48 → X=49
    assert ft.keyframe_x(end, fps) == ft.to_frame(end, fps) + 1


def test_agent_placement_quantizes_fractional_seconds(monkeypatch):
    from classes.clip_placement import quantize_placement_seconds

    monkeypatch.setattr(
        "classes.clip_utils.project_fps_fraction",
        lambda: Fraction(30, 1),
    )
    start, end = quantize_placement_seconds(1.111, 3.777)
    assert ft.is_aligned(start, Fraction(30, 1))
    assert ft.is_aligned(end, Fraction(30, 1))
    assert ft.duration_frames(start, end, Fraction(30, 1)) == ft.duration_frames(1.111, 3.777, Fraction(30, 1))


def test_timecode_agrees_with_frame_math():
    from classes.time_parts import timecodeToSeconds

    fps_num, fps_den = 24000, 1001
    fps = Fraction(fps_num, fps_den)
    secs = timecodeToSeconds("00:00:02:07", fps_num, fps_den)
    # Same frame the FCP path would derive after quantize
    frame = ft.to_frame(secs, fps)
    assert ft.to_seconds(frame, fps) == ft.snap(secs, fps)
