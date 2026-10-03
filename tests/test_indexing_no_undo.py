"""Background indexing must never become an undo step (AGENTS.md: one user intent = one step).

Drives the real UpdateManager. Before this, ``_on_intermediate`` and ``_on_complete`` called
``f.save()``: every indexed file added two undo entries ("indexing..." then the result) and
cleared the redo stack, so the user's next Ctrl+Z undid an index they never asked for.
"""

from __future__ import annotations

import copy
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5.QtCore")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")
files_model = importlib.import_module("windows.models.files_model")

from unittest.mock import MagicMock  # noqa: E402

from classes import updates as updates_module  # noqa: E402
from classes.update_queue import UpdateQueue, UpdatesRouter  # noqa: E402
from classes.updates import UpdateManager  # noqa: E402

FILE_KEY = ["files", {"id": "F1"}]


class FileStore:
    """The project's files list, reversible like the real ProjectDataStore (update = merge)."""

    def __init__(self):
        self.files = {}
        self.changes = 0

    def changed(self, action):
        if action.type == "load" or not action.key or action.key[0] != "files":
            return
        self.changes += 1
        if action.type == "insert":
            action.set_old_values(None)
            self.files[action.values["id"]] = copy.deepcopy(action.values)
        elif action.type == "update":
            fid = action.key[1]["id"]
            action.set_old_values(copy.deepcopy(self.files[fid]))
            self.files[fid].update(copy.deepcopy(action.values))


@pytest.fixture
def world(monkeypatch):
    """The real UpdateManager behind the real UpdatesRouter, exactly as the running app wires it."""
    manager = UpdateManager()
    store = FileStore()
    manager.add_listener(store)
    app = MagicMock()
    app.updates = UpdatesRouter(manager, UpdateQueue(manager))
    monkeypatch.setattr(files_model, "get_app", lambda: app)
    monkeypatch.setattr(updates_module, "get_app", lambda: app)  # undo() reaches for app.window
    manager.insert(["files"], {"id": "F1", "path": "/m/clip.mp4", "tags": ""})  # the import: one real step
    return manager, store


def _index_result():
    return SimpleNamespace(key=FILE_KEY, data={"id": "F1", "ai_metadata": {"analyzed": True, "short_summary": "a street"}})


def _save(obj):
    files_model.FilesModel._save_file_untracked(None, obj)


def test_saving_an_index_result_adds_no_undo_step(world):
    manager, store = world
    steps = len(manager.actionHistory)
    _save(SimpleNamespace(key=FILE_KEY, data={"id": "F1", "ai_metadata": {"index": {"status": "indexing"}}}))
    _save(_index_result())
    assert len(manager.actionHistory) == steps, "indexing wrote undo entries"
    assert store.files["F1"]["ai_metadata"]["short_summary"] == "a street", "the result must still reach the project"


def test_the_project_is_still_marked_changed_so_it_saves(world):
    manager, store = world
    before = store.changes
    _save(_index_result())
    assert store.changes == before + 1, "listeners (the project store) must see the update"


def test_indexing_does_not_throw_away_the_redo_stack(world):
    manager, store = world
    manager.update(FILE_KEY, {"tags": "beach"})  # a user edit
    manager.undo()
    assert len(manager.redoHistory) == 1
    _save(_index_result())
    assert len(manager.redoHistory) == 1, "a finishing index must not cost the user their redo"
    manager.redo()
    assert store.files["F1"]["tags"] == "beach"


def test_the_old_tracked_save_did_both_bad_things(world):
    """The control: proves the assertions above would have caught the original bug."""
    manager, store = world
    manager.update(FILE_KEY, {"tags": "beach"})
    manager.undo()
    steps = len(manager.actionHistory)
    manager.update(FILE_KEY, _index_result().data)  # what f.save() did
    assert len(manager.actionHistory) == steps + 1
    assert manager.redoHistory == []


def test_undo_after_indexing_undoes_the_users_edit_not_the_index(world):
    manager, store = world
    manager.update(FILE_KEY, {"tags": "beach"})
    _save(_index_result())
    manager.undo()
    assert store.files["F1"]["tags"] == "", "undo reversed the user's edit"
    assert store.files["F1"]["ai_metadata"]["short_summary"] == "a street", "and left the index alone"


def test_saving_inside_an_open_undo_group_does_not_join_it(world):
    manager, store = world
    manager.transaction_id = "tool-call-1"
    try:
        steps = len(manager.actionHistory)
        _save(_index_result())
        assert len(manager.actionHistory) == steps
    finally:
        manager.transaction_id = None


def test_a_user_flag_set_on_the_manager_is_left_as_it_was(world):
    manager, store = world
    manager.ignore_history = False
    _save(_index_result())
    assert manager.ignore_history is False
    manager.ignore_history = True
    _save(_index_result())
    assert manager.ignore_history is True
    manager.ignore_history = False
