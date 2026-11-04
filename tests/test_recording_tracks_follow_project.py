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
    stub.refresh_tracks = lambda **_kw: setattr(stub, "refreshed", stub.refreshed + 1)
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


class _Combo:
    """QComboBox stand-in: per-item text and role data, and a current index."""

    def __init__(self, role):
        self.role, self.items, self.current = role, [], -1

    def blockSignals(self, _on):
        pass

    def clear(self):
        self.items, self.current = [], -1

    def addItem(self, text, data=None):
        self.items.append({"text": text, self.role: data})
        if self.current < 0:
            self.current = 0

    def setItemData(self, index, value, role):
        self.items[index][role] = value

    def count(self):
        return len(self.items)

    def itemData(self, index, role=None):
        return self.items[index].get(self.role if role is None else role)

    def currentData(self, role=None):
        return self.itemData(self.current, role) if self.current >= 0 else None

    def setCurrentIndex(self, index):
        self.current = index


def _track(track_id, number):
    return types.SimpleNamespace(id=track_id, data={"id": track_id, "number": number})


def _recording_dock(dock_cls, monkeypatch, tracks):
    from windows import audio_recording

    monkeypatch.setattr(audio_recording, "get_app", lambda: types.SimpleNamespace(_tr=lambda s: s))
    monkeypatch.setattr(audio_recording.Track, "filter", staticmethod(lambda **_k: list(tracks)))
    dock = types.SimpleNamespace(_context_track=None, _tracks_refresh_queued=False, _track_labels=dict,
                                 track_combo=_Combo(audio_recording.Qt.UserRole))
    for name in ("refresh_tracks", "set_recording_context", "_refresh_tracks_once"):
        setattr(dock, name, types.MethodType(getattr(dock_cls, name), dock))
    return dock


def test_a_renumber_keeps_the_track_the_user_picked(dock_cls, monkeypatch):
    tracks = [_track("A", 1), _track("B", 2)]
    dock = _recording_dock(dock_cls, monkeypatch, tracks)
    dock.refresh_tracks()
    dock.track_combo.setCurrentIndex(2)                 # "No Track", B (2), A (1): the user picks A
    assert dock.track_combo.currentData() == 1
    tracks[:] = [_track("NEW", 1), _track("A", 2), _track("B", 3)]   # a track inserted at the bottom
    dock._refresh_tracks_once()
    assert dock.track_combo.currentData() == 2, "still track A, at its new number"


def test_a_recording_context_still_picks_by_number(dock_cls, monkeypatch):
    tracks = [_track("A", 1), _track("B", 2)]
    dock = _recording_dock(dock_cls, monkeypatch, tracks)
    dock.refresh_tracks()
    dock.track_combo.setCurrentIndex(2)                 # A
    dock.set_recording_context(track_number=2)         # recording on B from the timeline
    assert dock.track_combo.currentData() == 2


def test_a_renumber_keeps_no_track_even_with_a_recording_context(dock_cls, monkeypatch):
    from windows import audio_recording

    tracks = [_track("A", 1), _track("B", 2)]
    dock = _recording_dock(dock_cls, monkeypatch, tracks)
    dock.set_recording_context(track_number=2)         # B
    dock.track_combo.setCurrentIndex(0)                 # then the user picks "No Track"
    tracks[:] = [_track("NEW", 1), _track("A", 2), _track("B", 3)]
    dock._refresh_tracks_once()
    assert dock.track_combo.currentData() == audio_recording.NO_RECORDING_TRACK
    dock.refresh_tracks()                               # the dock shown again
    assert dock.track_combo.currentData() == audio_recording.NO_RECORDING_TRACK


def test_a_deleted_track_does_not_move_the_recording_context(dock_cls, monkeypatch):
    tracks = [_track("A", 1), _track("B", 2)]
    dock = _recording_dock(dock_cls, monkeypatch, tracks)
    dock.set_recording_context(track_number=2)         # B
    tracks[:] = [_track("A", 1)]                        # B deleted
    dock._refresh_tracks_once()
    assert dock._context_track == 2, "left alone, not set to the fallback row"


def test_a_renumber_moves_the_recording_context_with_its_track(dock_cls, monkeypatch):
    tracks = [_track("A", 1), _track("B", 2)]
    dock = _recording_dock(dock_cls, monkeypatch, tracks)
    dock.set_recording_context(track_number=2)         # B
    tracks[:] = [_track("NEW", 1), _track("A", 2), _track("B", 3)]
    dock._refresh_tracks_once()
    assert dock.track_combo.currentData() == 3 and dock._context_track == 3
