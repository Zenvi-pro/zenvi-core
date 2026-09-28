"""Location keyframe migrations for the libopenshot 1.0 upgrade.

libopenshot 1.0 changed what ``location_x``/``location_y`` mean for
``SCALE_CROP`` clips: +/-1 now moves the scaled clip fully offscreen instead
of moving it by one canvas width/height.  Projects saved against an older
libopenshot must be converted once so their framing does not change.

Ported from OpenShot PR #6075 / #6109 (src/tests/test_project_data.py) and
adapted to the headless suite: ``openshot`` is a stub here, so the handful of
constants and the ``Keyframe`` evaluator the migration needs are provided by
the fixture below when the real extension is absent.
"""
import json
import sys

import pytest

from classes import project_data as pd


# libopenshot enum values (see Enums.h); only used when ``openshot`` is stubbed.
_CONSTANTS = {
    "SCALE_CROP": 0,
    "SCALE_FIT": 1,
    "SCALE_STRETCH": 2,
    "SCALE_NONE": 3,
    "GRAVITY_TOP_LEFT": 0,
    "GRAVITY_TOP": 1,
    "GRAVITY_TOP_RIGHT": 2,
    "GRAVITY_LEFT": 3,
    "GRAVITY_CENTER": 4,
    "GRAVITY_RIGHT": 5,
    "GRAVITY_BOTTOM_LEFT": 6,
    "GRAVITY_BOTTOM": 7,
    "GRAVITY_BOTTOM_RIGHT": 8,
    "BEZIER": 0,
    "LINEAR": 1,
    "CONSTANT": 2,
}


class _FakeKeyframe:
    """Minimal stand-in for openshot.Keyframe: linear between points."""

    def __init__(self):
        self._points = []

    def SetJson(self, text):
        data = json.loads(text)
        points = []
        for point in data.get("Points", []):
            co = point.get("co", {})
            points.append((float(co.get("X", 1.0)), float(co.get("Y", 0.0))))
        self._points = sorted(points)

    def GetValue(self, frame):
        if not self._points:
            return 0.0
        frame = float(frame)
        if frame <= self._points[0][0]:
            return self._points[0][1]
        if frame >= self._points[-1][0]:
            return self._points[-1][1]
        for (x0, y0), (x1, y1) in zip(self._points, self._points[1:]):
            if x0 <= frame <= x1:
                if x1 == x0:
                    return y1
                return y0 + (y1 - y0) * (frame - x0) / (x1 - x0)
        return self._points[-1][1]


@pytest.fixture
def openshot(monkeypatch):
    module = sys.modules["openshot"]
    for name, value in _CONSTANTS.items():
        if not hasattr(module, name):
            monkeypatch.setattr(module, name, value, raising=False)
    if not hasattr(module, "Keyframe"):
        monkeypatch.setattr(module, "Keyframe", _FakeKeyframe, raising=False)
    return module


def make_store(data):
    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store._data = data
    return store


def _project(openshot, *, libopenshot, clips, files=None, openshot_qt="1.0.150",
             width=1920, height=1080):
    return {
        # Zenvi stamps its own 1.0.x product version; the migration must key
        # off the libopenshot version only.
        "version": {"openshot-qt": openshot_qt, "libopenshot": libopenshot},
        "id": "P1",
        "width": width,
        "height": height,
        "files": files or [],
        "clips": clips,
        "effects": [],
    }


def _crop_clip(openshot, **overrides):
    clip = {
        "id": "C1",
        "scale": openshot.SCALE_CROP,
        "gravity": openshot.GRAVITY_CENTER,
        "reader": {"width": 1080, "height": 1920},
        "scale_x": {"Points": [{"co": {"X": 1, "Y": 1.0}}]},
        "scale_y": {"Points": [{"co": {"X": 1, "Y": 1.0}}]},
        "location_x": {"Points": [{"co": {"X": 1, "Y": 0.5}}]},
        "location_y": {"Points": [{"co": {"X": 1, "Y": -0.5}}]},
        "effects": [],
    }
    clip.update(overrides)
    return clip


def test_numeric_version_strips_suffixes():
    assert pd.ProjectDataStore._numeric_version("0.5.0") == (0, 5, 0)
    assert pd.ProjectDataStore._numeric_version("1.0.0-dev3") == (1, 0, 0)
    assert pd.ProjectDataStore._numeric_version("0.7") == (0, 7, 0)
    assert pd.ProjectDataStore._version_at_most("0.7.0", "0.7.0")
    assert not pd.ProjectDataStore._version_at_most("1.0.0", "0.7.0")


def test_upgrade_migrates_legacy_centered_crop_locations(openshot):
    store = make_store(_project(openshot, libopenshot="0.7.0", clips=[_crop_clip(openshot)]))

    store.upgrade_project_data_structures()

    clip = store._data["clips"][0]
    # Horizontal: crop scale fills the 1920 canvas exactly, so no change.
    assert clip["location_x"]["Points"][0]["co"]["Y"] == pytest.approx(0.5)
    crop_height = 1920 * (1920 / 1080)
    expected_y = -0.5 * (2 * 1080 / (1080 + crop_height))
    assert clip["location_y"]["Points"][0]["co"]["Y"] == pytest.approx(expected_y)


def test_upgrade_migrates_zenvi_project_saved_with_libopenshot_0_5(openshot):
    """The common Zenvi case: product 1.0.x, libopenshot 0.5.0."""
    store = make_store(_project(
        openshot, libopenshot="0.5.0", openshot_qt="1.0.188",
        clips=[_crop_clip(openshot)]))

    store.upgrade_project_data_structures()

    clip = store._data["clips"][0]
    assert clip["location_y"]["Points"][0]["co"]["Y"] != -0.5


def test_upgrade_migrates_crop_locations_for_gravity_and_keyframed_scale(openshot):
    clip = _crop_clip(
        openshot,
        file_id="F1",
        gravity=openshot.GRAVITY_TOP_RIGHT,
        scale_x={"Points": [
            {"co": {"X": 1, "Y": 1.0}, "interpolation": openshot.LINEAR},
            {"co": {"X": 11, "Y": 2.0}, "interpolation": openshot.LINEAR},
        ]},
        location_x={"Points": [
            {"co": {"X": 1, "Y": -0.25}},
            {"co": {"X": 11, "Y": 0.25}},
        ]},
        location_y={"Points": [{"co": {"X": 1, "Y": 0.25}}]},
    )
    del clip["reader"]
    store = make_store(_project(
        openshot, libopenshot="0.7.0",
        files=[{"id": "F1", "width": 1920, "height": 800}],
        clips=[clip]))

    store.upgrade_project_data_structures()

    clip = store._data["clips"][0]
    crop_width = 1920 * (1080 / 800)
    assert clip["location_x"]["Points"][0]["co"]["Y"] == pytest.approx(-0.25)
    assert clip["location_x"]["Points"][1]["co"]["Y"] == pytest.approx(
        0.25 * 1920 / (crop_width * 2.0))
    assert clip["location_y"]["Points"][0]["co"]["Y"] == pytest.approx(0.25)


@pytest.mark.parametrize("libopenshot_version, scale_name", [
    ("1.0.0", "SCALE_CROP"),
    ("1.0.0-dev2", "SCALE_CROP"),
    ("0.7.0", "SCALE_FIT"),
])
def test_upgrade_does_not_migrate_new_or_non_crop_locations(openshot, libopenshot_version, scale_name):
    clip = _crop_clip(openshot, scale=getattr(openshot, scale_name))
    del clip["scale_x"]
    del clip["scale_y"]
    store = make_store(_project(openshot, libopenshot=libopenshot_version, clips=[clip]))

    store.upgrade_project_data_structures()

    clip = store._data["clips"][0]
    assert clip["location_x"]["Points"][0]["co"]["Y"] == 0.5
    assert clip["location_y"]["Points"][0]["co"]["Y"] == -0.5


def test_upgrade_skips_clips_without_known_source_size(openshot):
    clip = _crop_clip(openshot, reader={})
    store = make_store(_project(openshot, libopenshot="0.5.0", clips=[clip]))

    store.upgrade_project_data_structures()

    clip = store._data["clips"][0]
    assert clip["location_y"]["Points"][0]["co"]["Y"] == -0.5


def test_migration_is_idempotent_after_resave(openshot):
    """Once re-saved against libopenshot 1.0 the values must not move again."""
    store = make_store(_project(openshot, libopenshot="0.5.0", clips=[_crop_clip(openshot)]))
    store.upgrade_project_data_structures()
    migrated_y = store._data["clips"][0]["location_y"]["Points"][0]["co"]["Y"]

    store._data["version"]["libopenshot"] = "1.0.0"
    store.upgrade_project_data_structures()

    assert store._data["clips"][0]["location_y"]["Points"][0]["co"]["Y"] == migrated_y


# --- OpenShot 4.0.0 non-Crop migration (PR #6109) -------------------------
# Zenvi never stamps "4.0.0", so this only matters when opening a project
# written by the OpenShot 4.0.0 release, whose libopenshot applied the
# offscreen-edge math to every scale mode.


def test_upgrade_migrates_400_fit_location_to_restored_canvas_units(openshot):
    fitted_height = 720.0 * 178.0 / 266.0
    scaled_height = fitted_height / 9.0
    clip = {
        "id": "C1",
        "scale": openshot.SCALE_FIT,
        "gravity": openshot.GRAVITY_CENTER,
        "reader": {"width": 266, "height": 178},
        "scale_x": {"Points": [{"co": {"X": 30, "Y": 1.0 / 9.0}}]},
        "scale_y": {"Points": [{"co": {"X": 30, "Y": 1.0 / 9.0}}]},
        "location_x": {"Points": [{"co": {"X": 30, "Y": -0.75}}]},
        "location_y": {"Points": [{"co": {
            "X": 30,
            "Y": -310.0 / ((720.0 + scaled_height) / 2.0),
        }}]},
        "effects": [],
    }
    store = make_store(_project(
        openshot, libopenshot="1.0.0", openshot_qt="4.0.0",
        width=720, height=720, clips=[clip]))

    store.upgrade_project_data_structures()

    clip = store._data["clips"][0]
    assert clip["location_x"]["Points"][0]["co"]["Y"] == pytest.approx(-5.0 / 12.0)
    assert clip["location_y"]["Points"][0]["co"]["Y"] == pytest.approx(-31.0 / 72.0)


@pytest.mark.parametrize("scale_name, expected", [
    ("SCALE_FIT", (0.16, -0.18)),
    ("SCALE_STRETCH", (0.18, -0.18)),
    ("SCALE_NONE", (0.08, -0.09)),
])
def test_upgrade_migrates_400_non_crop_modes_with_gravity_margin_and_scale(openshot, scale_name, expected):
    clip = {
        "id": "C1",
        "scale": getattr(openshot, scale_name),
        "gravity": openshot.GRAVITY_TOP_RIGHT,
        "reader": {"width": 80, "height": 40},
        "margin": {"Points": [{"co": {"X": 10, "Y": 0.1}}]},
        "scale_x": {"Points": [{"co": {"X": 10, "Y": 0.5}}]},
        "scale_y": {"Points": [{"co": {"X": 10, "Y": 0.75}}]},
        "location_x": {"Points": [{"co": {"X": 10, "Y": 0.4}}]},
        "location_y": {"Points": [{"co": {"X": 10, "Y": -0.3}}]},
        "effects": [],
    }
    store = make_store(_project(
        openshot, libopenshot="1.0.0", openshot_qt="4.0.0",
        width=200, height=100, clips=[clip]))

    store.upgrade_project_data_structures()
    clip = store._data["clips"][0]
    x = clip["location_x"]["Points"][0]["co"]["Y"]
    y = clip["location_y"]["Points"][0]["co"]["Y"]
    assert x == pytest.approx(expected[0])
    assert y == pytest.approx(expected[1])

    # Re-running the upgrade must not touch a project once its saved version
    # advances beyond the affected 4.0.0 release.
    store._data["version"]["openshot-qt"] = "4.0.1"
    store.upgrade_project_data_structures()
    assert clip["location_x"]["Points"][0]["co"]["Y"] == x
    assert clip["location_y"]["Points"][0]["co"]["Y"] == y


def test_upgrade_leaves_zenvi_non_crop_locations_alone(openshot):
    """Zenvi's own 1.0.x stamp is not the OpenShot 4.0.0 release."""
    clip = _crop_clip(openshot, scale=openshot.SCALE_FIT)
    store = make_store(_project(openshot, libopenshot="1.0.0", openshot_qt="1.0.190", clips=[clip]))

    store.upgrade_project_data_structures()

    clip = store._data["clips"][0]
    assert clip["location_x"]["Points"][0]["co"]["Y"] == 0.5
    assert clip["location_y"]["Points"][0]["co"]["Y"] == -0.5
