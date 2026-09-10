"""import_files_tool: path/folder import without a dialog."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def test_import_files_without_paths_errors_and_opens_no_dialog(monkeypatch):
    from classes import tool_handlers as th

    win = MagicMock()
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))
    out = th.import_files()
    assert out.startswith("Error:")
    assert "paths is required" in out
    win.actionImportFiles_trigger.assert_not_called()
    win.files_model.add_files.assert_not_called()


def test_import_files_missing_path_errors(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    out = th.import_files(paths=str(tmp_path / "nope.mp4"))
    assert out.startswith("Error:")
    assert "Nothing to import" in out


def test_import_files_imports_folder_and_returns_file_id(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    imported = [
        SimpleNamespace(id="fid1", data={"name": "a.mp4", "path": str(clip), "duration": 1.5})
    ]
    win = MagicMock()
    win.files_model.add_files.return_value = imported
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, **kw: fn())

    out = th.import_files(path=str(tmp_path), chat_session_id="s1")
    assert "fid1" in out
    assert "file_id=" in out
    assert th._last_split_file_id_by_chat_session["s1"] == "fid1"
    win.files_model.add_files.assert_called_once()
    added = win.files_model.add_files.call_args[0][0]
    assert str(clip) in added


def test_import_files_accepts_file_url(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    clip = tmp_path / "via_url.mp4"
    clip.write_bytes(b"x")
    imported = [
        SimpleNamespace(id="fid2", data={"name": "via_url.mp4", "path": str(clip), "duration": 2.0})
    ]
    win = MagicMock()
    win.files_model.add_files.return_value = imported
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, **kw: fn())

    out = th.import_files(paths="file://" + str(clip))
    assert "fid2" in out
    added = win.files_model.add_files.call_args[0][0]
    assert str(clip) in added


def test_import_files_errors_when_add_files_returns_nothing(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    clip = tmp_path / "bad.mp4"
    clip.write_bytes(b"x")
    win = MagicMock()
    win.files_model.add_files.return_value = []
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, **kw: fn())

    out = th.import_files(paths=str(clip))
    assert out.startswith("Error:")
    assert "Nothing was added" in out
