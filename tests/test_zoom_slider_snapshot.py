"""ZoomSlider._build_snapshot reads live project data and copies only the minimap fields."""

import copy
from types import SimpleNamespace
from unittest import mock

import pytest

import qt_api

# conftest stubs QThread as None, and the minimap worker subclasses it at import time.
with mock.patch.object(qt_api, "QThread", object):
    from windows.views import zoom_slider
from classes import project_data, query
from classes.query import Clip, Marker, Track, Transition


def _project_data():
    return {
        "duration": 10.0,
        # Listed out of order: the layer rows must come from the layer numbers.
        "layers": [
            {"id": "L2", "number": 2, "label": "V2"},
            {"id": "L1", "number": 1, "label": "V1"},
        ],
        "clips": [
            {"id": "c1", "position": 0.0, "layer": 1, "start": 0.0, "end": 5.0, "reader": {"path": "a.mp4"}},
            {"id": "c2", "position": 5.0, "layer": 2, "start": 1.0, "end": 3.5, "reader": {"path": "b.mp4"}},
        ],
        "effects": [
            {"id": "t1", "position": 4.0, "layer": 1, "start": 0.0, "end": 1.0, "reader": {"path": "wipe.svg"}},
        ],
        "markers": [{"id": "m1", "position": 7.5, "comment": "beat"}],
    }


@pytest.fixture
def project(monkeypatch):
    """Live project store plus a fake app, wired into every module that calls get_app()."""
    store = project_data.ProjectDataStore.__new__(project_data.ProjectDataStore)
    store._data = _project_data()
    app = SimpleNamespace(
        project=store,
        window=SimpleNamespace(timeline=object(), selected_clips=["c2"], selected_transitions=["t1"]),
        updates=SimpleNamespace(data_version=0),
    )
    monkeypatch.setattr(zoom_slider, "get_app", lambda: app)
    monkeypatch.setattr(query, "get_app", lambda: app)
    monkeypatch.setattr(query.QueryObject, "_cache", {})
    return store._data


def _build_snapshot(overrides=None):
    # _build_snapshot only reads width(), height() and _drag_overrides from the widget.
    widget = SimpleNamespace(_drag_overrides=overrides, width=lambda: 200, height=lambda: 20)
    return zoom_slider.ZoomSlider._build_snapshot(widget)


def _boom(*args, **kwargs):
    raise AssertionError("query filter ran on the GUI thread")


@mock.patch.object(Clip, "filter", new=_boom)
@mock.patch.object(Track, "filter", new=_boom)
@mock.patch.object(Transition, "filter", new=_boom)
@mock.patch.object(Marker, "filter", new=_boom)
def test_snapshot_does_not_run_query_filters(project):
    assert _build_snapshot()["clips"]


def test_snapshot_has_minimap_fields_and_old_layer_order(project):
    assert _build_snapshot() == {
        "clips": [
            {"id": "c1", "position": 0.0, "layer": 1, "start": 0.0, "end": 5.0},
            {"id": "c2", "position": 5.0, "layer": 2, "start": 1.0, "end": 3.5},
        ],
        "transitions": [{"id": "t1", "position": 4.0, "layer": 1, "start": 0.0, "end": 1.0}],
        "markers": [{"id": "m1", "position": 7.5}],
        "selected_ids": {"c2", "t1"},
        "layer_index": {2: 0, 1: 1},
        "width": 200.0,
        "height": 20.0,
        "duration": 10.0,
        "overrides": None,
    }


def test_snapshot_is_detached_from_live_data(project):
    overrides = {"c1": {"position": 1.0}}
    snapshot = _build_snapshot(overrides)
    expected = copy.deepcopy(snapshot)

    project["clips"][0]["position"] = 99.0
    project["layers"][0]["number"] = 7
    project["markers"][0]["position"] = 1.0
    project["effects"].clear()
    project["clips"].append({"id": "c3", "position": 0.0, "layer": 1, "start": 0.0, "end": 1.0})
    overrides["c1"]["position"] = 2.0

    assert snapshot == expected


def test_unused_clip_fields_are_not_copied(project):
    project["clips"][0]["waveform_samples"] = list(range(100_000))

    assert _build_snapshot()["clips"][0] == {"id": "c1", "position": 0.0, "layer": 1, "start": 0.0, "end": 5.0}
