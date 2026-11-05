"""Play reaches the end of a timeline an update batch lengthened.

RC v1.3.0 e2e: Assistant tools edit inside an update batch (#288), where TimelineSync holds the
diffs back from libopenshot until the batch ends. The preview read the timeline's length on each
change, i.e. before those diffs landed, and never again: after add_clips_to_timeline_tool on an
empty timeline Play stopped after one frame, and a timeline extended from 900 to 1395 frames
stopped at 900.
"""

from types import SimpleNamespace

import pytest

# Real Qt and libopenshot (PreviewParent is a QObject, and its module imports the scopes and libopenshot):
# conftest keeps this file out of the stubbed suite; ci.yml runs it in the pytest-suite job.
pytest.importorskip("openshot")
pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402

if not hasattr(QApplication, "instance"):  # conftest's Qt stub, not real Qt
    pytest.skip("real Qt required (run with ZENVI_REAL_QT=1)", allow_module_level=True)

from classes import update_batch  # noqa: E402


class _Timeline:
    """libopenshot's timeline as far as the preview cares: a length that changes when diffs apply."""

    def __init__(self, frames):
        self.frames = frames

    def GetMaxFrame(self):
        return self.frames


def _preview_thread():
    import windows.preview_thread as module
    return module


def _action(kind="insert", key=("clips",)):
    return SimpleNamespace(type=kind, key=list(key), json=lambda is_array=False: "[]")


@pytest.fixture
def preview(monkeypatch):
    PreviewParent = _preview_thread().PreviewParent
    parent = PreviewParent.__new__(PreviewParent)
    parent.timeline = _Timeline(0)
    parent.timeline_max_length = 0
    held_back = []

    def timeline_sync_flush():      # what TimelineSync.flush_batch does: apply the held-back diffs
        for frames in held_back:
            parent.timeline.frames = frames
        held_back.clear()

    # The window creates TimelineSync before PreviewParent, so their flushes register in this order.
    update_batch.register(timeline_sync_flush)
    update_batch.register(parent._after_update_batch)
    yield parent, held_back
    update_batch.unregister(parent._after_update_batch)
    update_batch.unregister(timeline_sync_flush)


def test_an_edit_inside_a_batch_updates_the_play_range_when_the_batch_ends(preview):
    parent, held_back = preview
    update_batch.begin()
    try:
        held_back.append(682)               # add_clips_to_timeline_tool on an empty timeline
        parent.changed(_action())
        assert parent.timeline_max_length == 0     # libopenshot has not seen the clips yet
    finally:
        update_batch.end()
    assert parent.timeline_max_length == 682


def test_a_tool_that_extends_the_timeline_moves_the_end_of_play(preview):
    parent, held_back = preview
    parent.timeline.frames = 900
    parent.changed(_action())
    assert parent.timeline_max_length == 900
    update_batch.begin()
    try:
        held_back.append(1395)
        parent.changed(_action("update"))
    finally:
        update_batch.end()
    assert parent.timeline_max_length == 1395


def test_outside_a_batch_the_length_is_read_on_each_change(preview):
    parent, _ = preview
    parent.timeline.frames = 300
    parent.changed(_action())
    assert parent.timeline_max_length == 300
    parent.timeline.frames = 120
    parent.changed(_action("delete"))
    assert parent.timeline_max_length == 120


def test_changes_that_do_not_touch_libopenshot_are_ignored(preview):
    parent, _ = preview
    parent.timeline.frames = 50
    parent.changed(_action(key=("files",)))
    assert parent.timeline_max_length == 0


def test_stop_unregisters_the_batch_hook(monkeypatch):
    preview_thread = _preview_thread()

    parent = preview_thread.PreviewParent.__new__(preview_thread.PreviewParent)
    update_batch.register(parent._after_update_batch)
    monkeypatch.setattr(preview_thread, "get_app",
                        lambda: SimpleNamespace(updates=SimpleNamespace(disconnect_listener=lambda listener: None)))
    parent.worker = SimpleNamespace(Stop=lambda: None, kill=lambda: None)
    parent.background = SimpleNamespace(isRunning=lambda: False, quit=lambda: None, wait=lambda ms: True)
    parent.Stop()
    assert parent._after_update_batch not in update_batch._flushers
