"""The Recording dock's track list follows the open project instead of keeping the last one's tracks."""

import types

import pytest


@pytest.fixture
def dock_cls(monkeypatch):
    from windows import audio_recording

    queued = []
    monkeypatch.setattr(audio_recording, "QTimer", types.SimpleNamespace(
        singleShot=lambda _ms, callback: queued.append(callback)))
    audio_recording.AudioRecordingDockContent.queued = queued
    return audio_recording.AudioRecordingDockContent


def _stub(dock_cls):
    stub = types.SimpleNamespace(refreshed=0, _tracks_refresh_queued=False)
    stub.refresh_tracks = lambda: setattr(stub, "refreshed", stub.refreshed + 1)
    for name in ("changed", "_refresh_tracks_once"):
        setattr(stub, name, types.MethodType(getattr(dock_cls, name), stub))
    return stub


def _run_event_loop(dock_cls):
    while dock_cls.queued:
        dock_cls.queued.pop(0)()


@pytest.mark.parametrize("action", [
    types.SimpleNamespace(type="load", key=[], values={}),                      # New / Open project
    types.SimpleNamespace(type="insert", key=["layers"], values={}),            # track added
    types.SimpleNamespace(type="delete", key=["layers", {"id": "L1"}], values={}),
    types.SimpleNamespace(type="update", key=["layers", {"id": "L1"}], values={}),  # renamed
])
def test_project_and_track_changes_refresh_the_track_list(dock_cls, action):
    stub = _stub(dock_cls)
    stub.changed(action)
    _run_event_loop(dock_cls)
    assert stub.refreshed == 1


def test_a_renumber_refreshes_once_after_the_whole_change(dock_cls):
    """Renumbering updates one layer at a time; mid-way two tracks can share a number."""
    stub = _stub(dock_cls)
    for layer in ("L1", "L2", "L3"):
        stub.changed(types.SimpleNamespace(type="update", key=["layers", {"id": layer}], values={}))
    assert stub.refreshed == 0
    _run_event_loop(dock_cls)
    assert stub.refreshed == 1


def test_clip_edits_leave_the_track_list_alone(dock_cls):
    stub = _stub(dock_cls)
    stub.changed(types.SimpleNamespace(type="update", key=["clips", {"id": "C1"}], values={}))
    stub.changed(None)
    _run_event_loop(dock_cls)
    assert stub.refreshed == 0
