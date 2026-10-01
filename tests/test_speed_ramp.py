"""Speed ramp time-curve builder."""

from __future__ import annotations

from classes import frame_time as ft
from classes.speed_ramp import apply_speed_ramp_to_clip_data, build_speed_ramp_time_points


def test_first_keyframe_x_stable_after_repeated_apply():
    fps = 24
    clip = {"id": "c1", "position": 0.0, "start": 1.0, "end": 5.0}
    kps = [
        {"seconds": 1.0, "speed": 1.0},
        {"seconds": 5.0, "speed": 0.2},
    ]
    first_x = None
    data = clip
    for _ in range(100):
        data = apply_speed_ramp_to_clip_data(data, kps, fps=fps)
        x0 = data["time"]["Points"][0]["co"]["X"]
        if first_x is None:
            first_x = x0
        assert x0 == first_x
        assert x0 == ft.keyframe_x(1.0, fps)


def test_refuses_zero_or_insane_speed():
    try:
        build_speed_ramp_time_points(
            [{"seconds": 0.0, "speed": 0.0}],
            clip_start=0.0,
            clip_end=2.0,
            fps=24,
        )
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        build_speed_ramp_time_points(
            [{"seconds": 0.0, "speed": 1000.0}],
            clip_start=0.0,
            clip_end=2.0,
            fps=24,
        )
        assert False, "expected ValueError"
    except ValueError:
        pass
