"""classes.keyframe_rules: the one definition of the clip keyframe rules (headless)."""

import pytest

from classes.keyframe_rules import (
    BEZIER, CONSTANT, COPY_KEYFRAME_GROUPS, LINEAR, curve_plateau, default_keyframe_value, keyframe_value,
    max_transform_multiple,
)


def _p(x, y, interp=BEZIER, **handles):
    point = {"co": {"X": x, "Y": y}, "interpolation": interp}
    point.update(handles)
    return point


def test_values_follow_libopenshot_semantics():
    linear = {"Points": [_p(1, 0.0), _p(11, 10.0, LINEAR)]}
    assert keyframe_value(linear, 0) == 0.0 and keyframe_value(linear, 6) == pytest.approx(5.0)
    assert keyframe_value(linear, 50) == 10.0
    step = {"Points": [_p(1, 0.0), _p(11, 10.0, CONSTANT)]}
    assert keyframe_value(step, 10) == 0.0 and keyframe_value(step, 11) == 10.0  # on a point: its own value
    # Default handles (0.5, 0) / (0.5, 1): an ease-in-out, symmetric around the middle.
    smooth = {"Points": [_p(1, 0.0), _p(11, 1.0)]}
    assert keyframe_value(smooth, 6) == pytest.approx(0.5, abs=0.01)
    assert keyframe_value(smooth, 3) < 0.2
    # Unsorted input and duplicate X: the last listed point at an X wins, like AddPoint.
    dup = {"Points": [_p(11, 1.0, LINEAR), _p(1, 0.0), _p(11, 5.0, LINEAR)]}
    assert keyframe_value(dup, 11) == 5.0 and keyframe_value(dup, 6) == pytest.approx(2.5)
    assert keyframe_value(0.7, 5) == 0.7 and keyframe_value({"Points": []}, 5, default=3.0) == 3.0


def test_plateau_is_the_level_between_fades():
    fade = {"Points": [_p(1, 0.0), _p(31, 1.0), _p(271, 1.0), _p(301, 0.0)]}
    assert curve_plateau(fade, 1, 301) == 1.0
    ducked = {"Points": [_p(1, 0.8), _p(100, 0.2), _p(200, 0.8)]}
    assert curve_plateau(ducked, 1, 301) == 0.8
    assert curve_plateau({"Points": [_p(1, 0.5)]}, 121, 421) == 0.5
    assert curve_plateau(None, 1, 30, default=1.0) == 1.0


def test_defaults_limits_and_groups_match_the_properties_dock_and_menu():
    assert default_keyframe_value("alpha") == 1.0 and default_keyframe_value("origin_y") == 0.5
    assert default_keyframe_value("rotation") == 0.0 and default_keyframe_value("has_video") == -1.0
    assert default_keyframe_value("wave_color", "green") == 123.0 and default_keyframe_value("corner_radius") is None
    assert max_transform_multiple(1920, 1080) == 52 and max_transform_multiple(1920, 1080, True) == 16
    assert max_transform_multiple(0, 0) == 50 and max_transform_multiple(None, None, True) == 15
    assert COPY_KEYFRAME_GROUPS["location"] == ("gravity", "location_x", "location_y")
    assert set(COPY_KEYFRAME_GROUPS["all"]) == {k for g in COPY_KEYFRAME_GROUPS.values() for k in g}
