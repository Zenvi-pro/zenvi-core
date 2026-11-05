"""The files model's background indexing results land without undo steps.

Real Qt (ZENVI_REAL_QT=1): the files model subclasses QThread at import time.
Drives FilesModel._start_indexing_worker with a fake worker and fires its
intermediate and final results, as BackendIndexingWorker does.
"""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")

from windows.models import files_model as fm  # noqa: E402
from windows.models.files_model import FilesModel  # noqa: E402


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _Worker:
    def __init__(self, *_args, **_kwargs):
        self.progress, self.intermediate_save = _Signal(), _Signal()
        self.completed, self.finished = _Signal(), _Signal()
        _Worker.last = self

    def start(self):
        pass


def _files_model():
    model = FilesModel.__new__(FilesModel)
    model._active_indexers, model._indexing_queue = [], []
    model._indexing_progress, model._status_cache = {}, {}
    model.indexingProgress = MagicMock()
    return model


def test_indexing_results_leave_the_users_last_edit_on_top_of_undo(editor):
    fid = editor.add_file("video", duration=4)
    clip_id = editor.add_clip(fid, position=0.0, end=2.0)  # the user's last edit
    steps = len(editor.manager.actionHistory)

    model = _files_model()
    with patch.object(fm, "BackendIndexingWorker", _Worker), patch.object(fm, "get_app", return_value=editor.app):
        model._start_indexing_worker(fid)
        worker = _Worker.last
        worker.intermediate_save.emit(fid, {"analyzed": False, "index": {"status": "indexing"}})
        worker.completed.emit({}, {"analyzed": True, "scenes": [{"start": 0, "end": 2}]}, None)
        worker.finished.emit()

    assert len(editor.manager.actionHistory) == steps
    assert editor.file(fid)["ai_metadata"]["analyzed"] is True
    editor.undo()
    assert editor.clip(clip_id) is None
    assert editor.file(fid)["ai_metadata"]["analyzed"] is True
