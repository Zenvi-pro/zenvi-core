"""Preview transform handles follow libopenshot 1.0's location contract.

Crop clips move by the distance to the offscreen edge (+/-1 = fully
offscreen); every other scale mode keeps canvas-relative coordinates.
Ported from OpenShot PR #6109 (src/tests/test_video_widget_transform.py).

Real Qt is required for QSizeF/QRectF math, so this module is skipped by the
headless stub suite and runs under ZENVI_REAL_QT=1.
"""
import types

import pytest

pytest.importorskip("PyQt5.QtCore")
openshot = pytest.importorskip("openshot")

from PyQt5.QtCore import QRect  # noqa: E402

from windows.video_widget import VideoWidget  # noqa: E402


def clip_with(scale_mode, gravity=None):
    if gravity is None:
        gravity = openshot.GRAVITY_CENTER
    return types.SimpleNamespace(data={"scale": scale_mode, "gravity": gravity})


def props(location_x=0.0, location_y=0.0, scale_x=1.0, scale_y=1.0):
    return {
        "scale_x": {"value": scale_x},
        "scale_y": {"value": scale_y},
        "location_x": {"value": location_x},
        "location_y": {"value": location_y},
        "parentObjectId": {"memo": ""},
    }


def bare_widget():
    """Enough of a VideoWidget to run the pure geometry helpers."""
    widget = types.SimpleNamespace()
    widget._clip_location_geometry = types.MethodType(
        VideoWidget._clip_location_geometry, widget)
    widget._location_offset = VideoWidget._location_offset
    return widget


VIEWPORT = QRect(0, 0, 160, 90)


def rect_for(scale_mode, base_width=320, base_height=90, viewport=VIEWPORT, **kwargs):
    return VideoWidget._clip_display_rect(
        bare_widget(), base_width, base_height, clip_with(scale_mode),
        props(**kwargs), viewport)


@pytest.mark.parametrize("location", [-1.0, -0.35, 0.0, 0.2, 1.0])
@pytest.mark.parametrize("anchored, canvas, clip", [
    (0.0, 160.0, 320.0),
    (-80.0, 160.0, 320.0),
    (20.0, 90.0, 50.0),
])
def test_location_offset_round_trip(location, anchored, canvas, clip):
    offset = VideoWidget._location_offset(location, anchored, canvas, clip)
    assert VideoWidget._location_value_from_offset(
        offset, anchored, canvas, clip) == pytest.approx(location)


def test_location_value_from_offset_guards_zero_basis():
    assert VideoWidget._location_value_from_offset(10.0, 0.0, 0.0, 0.0) == 0.0


@pytest.mark.parametrize("scale_mode", [
    openshot.SCALE_FIT, openshot.SCALE_STRETCH, openshot.SCALE_NONE,
])
def test_non_crop_locations_remain_canvas_relative(scale_mode):
    anchored = rect_for(scale_mode)
    moved = rect_for(scale_mode, location_x=-0.25, location_y=0.25)

    assert moved.x() - anchored.x() == pytest.approx(-40.0)
    assert moved.y() - anchored.y() == pytest.approx(22.5)


def test_crop_unit_location_moves_clip_fully_offscreen():
    # 320x90 source cropped into a 160x90 viewport keeps 320x90, centred at x=-80.
    anchored = rect_for(openshot.SCALE_CROP)
    assert anchored.x() == pytest.approx(-80.0)
    assert anchored.width() == pytest.approx(320.0)

    left = rect_for(openshot.SCALE_CROP, location_x=-1.0)
    right = rect_for(openshot.SCALE_CROP, location_x=1.0)
    assert left.x() + left.width() == pytest.approx(0.0)
    assert right.x() == pytest.approx(VIEWPORT.width())


def test_crop_location_uses_offscreen_distance_not_canvas_size():
    moved = rect_for(openshot.SCALE_CROP, location_x=0.5)
    anchored = rect_for(openshot.SCALE_CROP)
    # Half the distance to the right edge: (160 - (-80)) / 2 = 120, not 80.
    assert moved.x() - anchored.x() == pytest.approx(120.0)


def test_fit_handles_match_reported_legacy_project_position():
    viewport = QRect(0, 0, 720, 720)
    rect = VideoWidget._clip_display_rect(
        bare_widget(), 266, 178, clip_with(openshot.SCALE_FIT),
        props(location_x=-5.0 / 12.0, location_y=-31.0 / 72.0,
              scale_x=1.0 / 9.0, scale_y=1.0 / 9.0),
        viewport)

    assert rect.x() == pytest.approx(20.0, abs=1e-5)
    assert rect.y() == pytest.approx(23.233083, abs=1e-5)
    assert rect.width() == pytest.approx(720.0)
    assert rect.height() == pytest.approx(481.804511, abs=1e-5)


def test_geometry_respects_viewport_origin():
    viewport = QRect(40, 10, 160, 90)
    (_, _, _, _, anchored_x, anchored_y, layout_x, layout_y,
     layout_width, layout_height) = bare_widget()._clip_location_geometry(
        320, 90, clip_with(openshot.SCALE_CROP), props(), viewport)

    assert (layout_x, layout_y) == (40, 10)
    assert (layout_width, layout_height) == (160.0, 90.0)
    assert anchored_x == pytest.approx(40 - 80)
    assert anchored_y == pytest.approx(10)
