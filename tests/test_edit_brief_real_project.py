"""The edit brief in the real project store: accepted, saved, reloaded, never an undo step."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt5.QtCore")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")

from classes import project_data as pd, updates as um  # noqa: E402


@pytest.fixture
def world(monkeypatch):
    app = MagicMock()
    monkeypatch.setattr(um, "get_app", lambda: app)
    monkeypatch.setattr(pd, "get_app", lambda: app)
    store = pd.ProjectDataStore()
    manager = um.UpdateManager()
    app.updates = manager
    manager.add_listener(store)
    return store, manager


def test_a_new_project_starts_with_an_empty_brief(world):
    store, _ = world
    assert store._data.get("edit_brief") == {}


def test_writing_the_brief_is_accepted_and_is_not_an_undo_step(world):
    store, manager = world
    manager.update_untracked(["edit_brief"], {"form": "YouTube vlog", "beat_map": [{"label": "hook", "start": 0, "end": 3}]})
    assert store._data["edit_brief"]["form"] == "YouTube vlog" and store._data["edit_brief"]["beat_map"][0]["label"] == "hook"
    assert len(manager.actionHistory) == 0, "nothing the user could undo"
    manager.update_untracked(["edit_brief"], {"vibe": "moody"})
    assert store._data["edit_brief"] == {"form": "YouTube vlog", "beat_map": [{"label": "hook", "start": 0, "end": 3}], "vibe": "moody"}, "keys merge"


def test_the_brief_is_saved_with_the_project_and_comes_back(world, tmp_path):
    store, manager = world
    manager.update_untracked(["edit_brief"], {"form": "film", "done": ["scene 1"]})
    path = str(tmp_path / "p.zvn")
    store.write_to_file(path, store._data, path_mode="relative")
    assert json.loads(Path(path).read_text())["edit_brief"] == {"form": "film", "done": ["scene 1"]}
    assert store.read_from_file(path, path_mode="absolute")["edit_brief"] == {"form": "film", "done": ["scene 1"]}


def test_a_key_set_to_null_stays_in_the_file_and_the_tool_reads_it_as_absent(world, tmp_path, monkeypatch):
    store, manager = world
    manager.update_untracked(["edit_brief"], {"form": "film", "vibe": "warm"})
    manager.update_untracked(["edit_brief"], {"vibe": None})
    path = str(tmp_path / "p.zvn")
    store.write_to_file(path, store._data, path_mode="relative")
    saved = store.read_from_file(path, path_mode="absolute")["edit_brief"]
    assert saved == {"form": "film", "vibe": None}
    from classes.editor_tools import media_index_tools_edit as TE
    monkeypatch.setattr(TE, "get_app", lambda: MagicMock(project=MagicMock(get=lambda k, d=None: saved if k == "edit_brief" else d)))
    assert TE.read_brief() == {"form": "film"}


def test_a_project_saved_before_the_brief_existed_opens_with_an_empty_one(world, tmp_path):
    store, _ = world
    data = json.loads(json.dumps(store._data))
    data.pop("edit_brief")
    path = str(tmp_path / "old.zvn")
    Path(path).write_text(json.dumps(data))
    store.load(path, interactive=False)
    assert store._data.get("edit_brief") == {}
