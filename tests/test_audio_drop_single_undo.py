"""Dropping an audio-only file on the timeline is ONE undo step (RC v1.2.0 P4).

addClip starts a waveform job for audio-only clips. The job's file and clip
saves land later, from a worker thread, under whatever transaction id the job
was given. It was given none, so they formed a second undo step: the first
Undo after a drop only cleared the waveform data, a visible no-op.

Real-Qt test with libopenshot: it runs the real TimelineView.addClip on a
generated WAV file, then replays the waveform callbacks with the job's id.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QPointF  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


class _TestApp(QApplication):
    """Minimal stand-in for classes.app.OpenShotApp at module import time."""

    def _tr(self, text):
        return text


@pytest.fixture(scope="module")
def timeline_module():
    # qt_api imports QtWebEngine, which Qt requires before any QApplication exists.
    importlib.import_module("qt_api")
    app = QApplication.instance()
    if app is None:
        app = _TestApp([])
    elif not hasattr(app, "_tr"):
        app._tr = lambda text: text
    parked = sys.modules.get("windows.views.timeline")
    if parked is not None and not hasattr(getattr(parked, "TimelineView", None), "addClip"):
        # test_save_project_error_dialog.py parks a stand-in under this name.
        del sys.modules["windows.views.timeline"]
    return importlib.import_module("windows.views.timeline")


def _write_wav(path):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x01" * 8000)  # 1 s


class _Store:
    """Project clips/files as plain dicts, saved through the real UpdateManager."""

    def __init__(self, updates):
        self.updates = updates
        self.clips = {}

    def clip(self, clip_id):
        if clip_id not in self.clips:
            return None
        store = self

        class _Clip:
            id = clip_id
            data = dict(store.clips[clip_id])

            def save(self):
                store.clips[clip_id] = dict(self.data)
                store.updates.update(["clips", {"id": clip_id}], dict(self.data))

        return _Clip()


class _Timeline:
    """The parts of TimelineView that addClip and the waveform slots touch."""

    def __init__(self, store):
        self.store = store
        self.item_ids = []
        self.item_type = "clip"
        self.waveform_jobs = []

    def _ensure_layers_exist(self, layers):
        pass

    def update_clip_data(self, clip_json, only_basic_props=True, ignore_refresh=False, **_kw):
        clip_id = clip_json["id"]
        self.store.clips[clip_id] = dict(clip_json)
        self.store.updates.insert(["clips"], dict(clip_json))

    def _waveform_new_audio_clip(self, new_clip, transaction_id):
        from windows.views.timeline import TimelineView
        TimelineView._waveform_new_audio_clip(self, new_clip, transaction_id)

    def Show_Waveform_Triggered(self, clip_ids, transaction_id=None):
        self.waveform_jobs.append((list(clip_ids), transaction_id))

    def get_uuid(self):
        return "waveform-token"

    def update(self):
        pass


@pytest.fixture
def editor(timeline_module, tmp_path):
    from classes.updates import UpdateManager

    wav = tmp_path / "tone.wav"
    _write_wav(wav)
    updates = UpdateManager()
    store = _Store(updates)
    file_data = {
        "id": "F1", "path": str(wav), "name": "tone.wav", "media_type": "audio",
        "has_audio": True, "has_video": False, "duration": 1.0,
    }
    audio_file = types.SimpleNamespace(
        id="F1", data=file_data, absolute_path=lambda: str(wav))
    window = types.SimpleNamespace(actionClearWaveformData=types.SimpleNamespace(setEnabled=lambda _on: None))
    settings = types.SimpleNamespace(get=lambda _key: None)
    app = types.SimpleNamespace(
        project=types.SimpleNamespace(
            get=lambda key: {"num": 30, "den": 1} if key == "fps" else None,
            generate_id=lambda: "G1"),
        updates=updates,
        window=window,
        get_settings=lambda: settings,
    )
    with patch.object(timeline_module, "get_app", return_value=app), \
            patch.object(timeline_module.File, "get", return_value=audio_file), \
            patch.object(timeline_module.Clip, "get", side_effect=lambda id=None: store.clip(id)):
        yield types.SimpleNamespace(
            view=timeline_module.TimelineView, timeline=_Timeline(store), updates=updates, store=store)


def _drop(editor):
    clip = editor.view.addClip(
        editor.timeline, "F1", QPointF(0.0, 0.0), 1, call_manual_move=False)
    assert clip and editor.timeline.waveform_jobs, "an audio-only clip asks for its waveform"
    (clip_ids, tid), = editor.timeline.waveform_jobs
    return clip["id"], tid


def _land_waveform(editor, clip_id, tid):
    """What get_waveform_thread emits: an empty placeholder, then the samples."""
    editor.view.clipAudioDataReady_Triggered(editor.timeline, clip_id, {"ui": {"audio_data": None}}, tid)
    editor.view.clipAudioDataReady_Triggered(editor.timeline, clip_id, {"ui": {"audio_data": [0.1, 0.4]}}, tid)


def test_waveform_saves_share_the_insert_transaction(editor):
    clip_id, tid = _drop(editor)
    assert tid, "the waveform job needs the insert's transaction id"
    _land_waveform(editor, clip_id, tid)

    history = editor.updates.actionHistory
    assert [a.type for a in history] == ["insert", "update", "update"]
    assert {a.transaction for a in history} == {tid}


def test_one_undo_removes_the_dropped_audio_clip(editor):
    clip_id, tid = _drop(editor)
    _land_waveform(editor, clip_id, tid)

    tail = editor.updates._tail_transaction(editor.updates.actionHistory)
    assert len(tail) == len(editor.updates.actionHistory), "undo reverts the whole drop at once"
    assert any(a.type == "insert" for a in tail)


def test_a_waveform_that_lands_after_a_newer_edit_stays_out_of_undo(editor):
    clip_id, tid = _drop(editor)
    editor.updates.transaction_id = "newer-edit"
    editor.updates.insert(["markers"], {"id": "M1", "position": 1.0})
    editor.updates.transaction_id = None

    _land_waveform(editor, clip_id, tid)

    # The waveform is on the clip, but Undo still takes the newer edit first.
    assert editor.store.clips[clip_id]["ui"]["audio_data"] == [0.1, 0.4]
    history = editor.updates.actionHistory
    assert [a.transaction for a in history] == [tid, "newer-edit"]
    assert [a.transaction for a in editor.updates._tail_transaction(history)] == ["newer-edit"]
    assert editor.updates.ignore_history is False


def test_a_drop_transaction_is_joined_not_split(editor, timeline_module):
    from classes.updates import nested_transaction

    with nested_transaction(editor.updates) as drop_tid:
        clip_id, tid = _drop(editor)
    assert tid == drop_tid
    _land_waveform(editor, clip_id, tid)
    assert {a.transaction for a in editor.updates.actionHistory} == {drop_tid}
