"""Ken Burns never pans a crop-filled clip past its edge (no black bar), at either end of the move."""

import pytest

from classes.camera_motion import (
    KEN_BURNS_AUTO, KEN_BURNS_BOTTOM_TO_TOP, KEN_BURNS_LEFT_TO_RIGHT, _crop_base_size, _safe_location,
    ken_burns_keyframes,
)

CASES = [
    # project, source (w, h): a 3:2 photo in 720p is the case that showed a 12 px bar
    ((1280, 720), (1880, 1253)),
    ((1920, 1080), (1880, 1253)),
    ((1920, 1080), (4000, 3000)),
    ((1080, 1920), (1920, 1080)),
    ((1920, 1080), (1920, 1080)),
    ((1920, 1080), (3840, 1080)),
]


@pytest.mark.parametrize("zoom_in", [True, False])
@pytest.mark.parametrize("direction", [KEN_BURNS_AUTO, KEN_BURNS_LEFT_TO_RIGHT, KEN_BURNS_BOTTOM_TO_TOP])
@pytest.mark.parametrize("project,source", CASES)
def test_ken_burns_stays_inside_the_crop_room(project, source, direction, zoom_in):
    pw, ph = project
    kf = ken_burns_keyframes(zoom_in, direction, pw, ph, *source)
    bw, bh = _crop_base_size(pw, ph, *source)
    for i in (0, 1):
        scale = kf.scale_x[i]
        assert abs(kf.location_x[i]) <= _safe_location(pw, bw, scale) + 1e-9
        assert abs(kf.location_y[i]) <= _safe_location(ph, bh, scale) + 1e-9


def test_the_photo_still_drifts_and_zooms():
    kf = ken_burns_keyframes(True, KEN_BURNS_AUTO, 1280, 720, 1880, 1253)
    assert kf.scale_x == (1.0, pytest.approx(1.22, abs=0.05))
    assert kf.location_y[0] < 0 < kf.location_y[1] and abs(kf.location_y[0]) > 0.05
