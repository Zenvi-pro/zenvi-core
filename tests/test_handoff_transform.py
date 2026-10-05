"""classes.handoff.transform: clip geometry like libopenshot 1.0's Clip::get_transform.

The matrices below were computed once with the web engine's parity-tested
``clipTransform`` (zenvi-web packages/engine/src/render/plan/transform.ts,
run with tsx) and hard-coded; the engine works in float32, so values agree
to ~1e-5 px.
"""

import math

import pytest

from classes.handoff import transform as tf
from classes.handoff.timeline_view import TimelineSnapshot

# name: (src_w, src_h, canvas_w, canvas_h, kwargs, engine matrix m11, m12, m21, m22, dx, dy)
GOLDEN = {
    "fit_identity": (1920, 1080, 1920, 1080, {}, [1, 0, 0, 1, 0, 0]),
    "fit_4k_down": (3840, 2160, 1920, 1080, {}, [0.5, 0, 0, 0.5, 0, 0]),
    "fit_vertical_moved_rotated": (
        1080, 1920, 1920, 1080,
        dict(scale_x=0.8, scale_y=0.8, location_x=0.1, location_y=-0.05, rotation=15),
        [0.43430887634317683, 0.11637271268182255, -0.1164685749241795, 0.43466663910218045, 1029.2830491723205,
         5.878778102377396]),
    "crop_topleft_located": (
        1280, 720, 1080, 1920, dict(scale_mode=0, gravity=0, location_x=0.2, location_y=-0.1),
        [2.6664061546325684, 0, 0, 2.6666667461395264, 216, -192]),
    "stretch_rot90_origin_shear": (
        640, 480, 1920, 1080, dict(scale_mode=2, rotation=90, origin_x=0.25, origin_y=0.75, shear_x=0.1),
        [0, 3, -2.25, 0.22500000335276127, 1290, 248.99999879300594]),
    "none_bottomright_margin": (
        800, 600, 1920, 1080, dict(scale_mode=3, gravity=8, scale_x=1.5, scale_y=1.5, margin=0.05),
        [1.5, 0, 0, 1.5, 666, 126]),
    "fit_odd_size_left": (1000, 999, 1280, 720, dict(gravity=3, location_x=-0.25),
                          [0.7200000286102295, 0, 0, 0.7207207083702087, -320, 0]),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_matrix_matches_the_engine(name):
    sw, sh, w, h, kwargs, expected = GOLDEN[name]
    g = tf.geometry(sw, sh, w, h, **kwargs)
    assert list(g.matrix) == pytest.approx(expected, abs=1e-4)


def test_centered_fit_reports_center_size_and_scale():
    g = tf.geometry(3840, 2160, 1920, 1080)
    assert (g.center_x, g.center_y) == (960.0, 540.0)
    assert (g.width, g.height, g.scale_x, g.scale_y) == (1920.0, 1080.0, 0.5, 0.5)
    assert (g.anchor_x, g.anchor_y) == (960.0, 540.0)
    assert g.map_point(3840, 2160) == (1920.0, 1080.0)


def test_rotation_turns_about_the_origin_point():
    g = tf.geometry(1920, 1080, 1920, 1080, rotation=90, origin_x=0.0, origin_y=0.0)
    # pivot = top-left of the image at the canvas origin; the image swings clockwise to x < 0
    assert (g.anchor_x, g.anchor_y) == (0.0, 0.0)
    assert g.map_point(1920, 0) == pytest.approx((0.0, 1920.0))
    assert (g.center_x, g.center_y) == pytest.approx((-540.0, 960.0))
    centered = tf.geometry(1920, 1080, 1920, 1080, rotation=33)
    assert (centered.center_x, centered.center_y) == pytest.approx((960.0, 540.0))


def test_location_moves_by_canvas_fractions_except_crop():
    fit = tf.geometry(1920, 1080, 1920, 1080, location_x=0.25, location_y=-0.5)
    assert (fit.center_x, fit.center_y) == pytest.approx((960 + 480, 540 - 540))
    crop = tf.geometry(1080, 1080, 1920, 1080, scale_mode=tf.SCALE_CROP, location_x=0.5)
    # crop: 1920x1920 box centred (y = -420); location 0.5 moves by 0.5 * (canvas - anchored) = 0.5 * 1920
    assert crop.x == pytest.approx(960.0)


def test_legacy_fcp_helpers_are_unchanged():
    assert tf.scale_mode_size(1280, 720, 1920, 1080, tf.SCALE_FIT) == (1920.0, 1080.0)
    assert tf.scale_mode_size(1080, 1920, 1920, 1080, tf.SCALE_CROP) == (1920.0, pytest.approx(3413.333333))
    assert tf.scale_mode_size(10, 10, 1920, 1080, tf.SCALE_STRETCH) == (1920.0, 1080.0)
    assert tf.scale_mode_size(0, 10, 1920, 1080, tf.SCALE_FIT) == (0, 10)
    assert tf.gravity_offset(tf.GRAVITY_BOTTOM_RIGHT, 1920, 1080, 960, 540) == (960.0, 540.0)
    assert tf.gravity_offset(tf.GRAVITY_TOP_LEFT, 1920, 1080, 960, 540) == (0.0, 0.0)
    assert tf.gravity_offset(tf.GRAVITY_CENTER, "x", 1080, 960, 540) == (0.0, 0.0)
    assert tf.normalized_to_center_pixels(0.5, -0.5, 1920, 1080) == (1440.0, 270.0)


def test_exporter_and_importer_use_the_moved_helpers():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "classes"
    for rel in ("exporters/final_cut_pro.py", "importers/final_cut_pro.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "def _scale_mode_size" not in text and "def _gravity_offset" not in text
        assert "from classes.handoff.transform import" in text


def _snapshot_with_clip(**clip_overrides):
    k = lambda pts: {"Points": [{"co": {"X": x, "Y": y}, "interpolation": 1} for x, y in pts]}  # noqa: E731
    clip = {"id": "C1", "file_id": "F1", "layer": 1000000, "position": 1.0, "start": 0.0, "end": 2.0,
            "scale": 1, "gravity": 4, "location_x": k([(1, 0.0), (31, 0.25)]), "alpha": k([(1, 0.0), (16, 1.0)])}
    clip.update(clip_overrides)
    project = {"fps": {"num": 30, "den": 1}, "width": 1920, "height": 1080,
               "layers": [{"id": "L1", "number": 1000000, "label": "", "lock": False}],
               "files": [{"id": "F1", "path": "/media/a.mp4", "media_type": "video", "width": 3840, "height": 2160,
                          "duration": 10.0, "fps": {"num": 30, "den": 1}}],
               "clips": [clip]}
    return TimelineSnapshot.from_project(project).clip("C1")


def test_clip_geometry_evaluates_curves_at_timeline_times():
    clip = _snapshot_with_clip()
    g0 = tf.clip_geometry(clip, 1.0, 1920, 1080)
    g1 = tf.clip_geometry(clip, 2.0, 1920, 1080)
    assert (g0.center_x, g0.opacity, g0.frame) == (960.0, 0.0, 1.0)
    assert g1.center_x == pytest.approx(960 + 0.25 * 1920) and g1.opacity == 1.0
    keys = tf.clip_geometry_keys(clip, 1920, 1080)
    assert [round(k.time, 4) for k in keys] == [1.0, 1.5, 2.0, round(3.0 - 1 / 30, 4)]
    assert all(math.isclose(k.scale_x, 0.5) for k in keys)
