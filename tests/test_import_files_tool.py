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


def test_import_files_dry_run_does_not_call_add_files(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    footage = tmp_path / "footage"
    footage.mkdir()
    (footage / "a.mp4").write_bytes(b"v")
    (footage / "b.wav").write_bytes(b"a")
    (footage / "c.png").write_bytes(b"i")
    (footage / "notes.txt").write_bytes(b"n")
    (footage / "readme.pdf").write_bytes(b"p")

    win = MagicMock()
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))

    out = th.import_files(path=str(footage), dry_run="true")
    assert out.startswith("dry_run=true")
    assert "nothing imported" in out
    assert "would_import=3" in out
    assert "video=1" in out
    assert "audio=1" in out
    assert "image=1" in out
    assert "skipped_non_media=2" in out
    assert "a.mp4" in out
    assert "Ask the user to confirm" in out
    win.files_model.add_files.assert_not_called()


def test_import_files_real_import_caps_response_at_25(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    folder = tmp_path / "many"
    folder.mkdir()
    imported = []
    for i in range(30):
        clip = folder / ("clip_%02d.mp4" % i)
        clip.write_bytes(b"x")
        imported.append(
            SimpleNamespace(
                id="fid%d" % i,
                data={"name": clip.name, "path": str(clip), "duration": 1.0},
            )
        )

    win = MagicMock()
    win.files_model.add_files.return_value = imported
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, **kw: fn())

    out = th.import_files(path=str(folder))
    assert "Imported 30 file(s)" in out
    assert out.count("file_id=") == 25
    assert "... and 5 more" in out
    assert "list_files_tool" in out
    win.files_model.add_files.assert_called_once()


def test_import_files_uses_normalize_agent_fs_path(monkeypatch, tmp_path):
    """Folder import must go through Windows/MSYS path normalization."""
    from classes import file_drop as fd
    from classes import tool_handlers as th

    clip = tmp_path / "via_msys.mp4"
    clip.write_bytes(b"x")
    seen = []

    real = fd.normalize_agent_fs_path

    def tracking(path, home=None):
        seen.append(path)
        if str(path).startswith("/c/"):
            return str(tmp_path)
        return real(path, home=home)

    monkeypatch.setattr(fd, "normalize_agent_fs_path", tracking)
    imported = [
        SimpleNamespace(
            id="fid_msys",
            data={"name": "via_msys.mp4", "path": str(clip), "duration": 1.0},
        )
    ]
    win = MagicMock()
    win.files_model.add_files.return_value = imported
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, **kw: fn())

    out = th.import_files(paths="/c/Users/alice/Desktop/clips", dry_run="true")
    assert any(str(p).startswith("/c/") for p in seen)
    assert out.startswith("dry_run=true")
    assert "would_import=1" in out
    win.files_model.add_files.assert_not_called()


def test_import_files_doc_advertises_dry_run_and_no_dialog():
    from classes.tool_handlers import import_files

    doc = (import_files.__doc__ or "").lower()
    assert "dry_run" in doc
    assert "dialog" in doc
    assert "c:/" in doc or "forward" in doc
