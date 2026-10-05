"""import_files_tool: path/folder import without a dialog."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


def test_import_files_dry_run_accepts_the_backend_boolean(monkeypatch, tmp_path):
    """zenvi-backend types dry_run as a boolean, so the Zenvi Assistant sends
    True/False rather than the "true" string Claude Code sends over MCP."""
    from classes import tool_handlers as th

    footage = tmp_path / "footage"
    footage.mkdir()
    clip = footage / "a.mp4"
    clip.write_bytes(b"v")
    (footage / "notes.txt").write_bytes(b"n")

    win = MagicMock()
    win.files_model.add_files.return_value = [
        SimpleNamespace(id="fid1", data={"name": "a.mp4", "path": str(clip)})
    ]
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, **kw: fn())

    out = th.import_files(paths=str(footage), dry_run=True)
    assert out.startswith("dry_run=true")
    assert "would_import=1" in out
    assert "skipped_non_media=1" in out
    win.files_model.add_files.assert_not_called()

    out = th.import_files(paths=str(footage), dry_run=False)
    assert out.startswith("Imported 1 file(s)")
    win.files_model.add_files.assert_called_once()


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


def test_import_files_adjacent_single_match_dry_run(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    real = tmp_path / "dirty_test"
    real.mkdir()
    (real / "clip.mp4").write_bytes(b"v")
    (real / "notes.txt").write_bytes(b"n")

    win = MagicMock()
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))

    out = th.import_files(path=str(tmp_path / "dirty_tes"), dry_run="true")
    assert out.startswith("dry_run=true")
    assert "match=adjacent" in out
    assert "would_import=1" in out
    assert "clip.mp4" in out
    assert "skipped_non_media=1" in out
    win.files_model.add_files.assert_not_called()


def test_import_files_ambiguous_asks_without_importing(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    (tmp_path / "clip_a").mkdir()
    (tmp_path / "clip_b").mkdir()
    ((tmp_path / "clip_a") / "a.mp4").write_bytes(b"x")
    ((tmp_path / "clip_b") / "b.mp4").write_bytes(b"y")

    win = MagicMock()
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))

    out = th.import_files(path=str(tmp_path / "clip_x"), dry_run="true")
    assert out.startswith("Error:")
    assert "Multiple paths match" in out
    assert "Do not guess" in out
    win.files_model.add_files.assert_not_called()


def test_import_files_missing_mentions_adjacent_and_no_mnt(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    win = MagicMock()
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))

    out = th.import_files(path=str(tmp_path / "totally_missing_xyz"))
    assert out.startswith("Error:")
    assert "Nothing to import" in out
    assert "/mnt/c" in out
    assert "Ask the user" in out
    win.files_model.add_files.assert_not_called()


def test_agent_runners_prompt_source_steers_windows_import():
    """Prompt rules live in agent_runners (Qt-gated tests); assert source here."""
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath(
        "src", "windows", "agent_runners.py"
    ).read_text(encoding="utf-8")
    assert "IMMEDIATELY" in text
    assert "/mnt/c" in text
    assert "import_files_tool" in text
    assert "dry_run=true" in text
    assert "media_types=video" in text
    assert 'folder=\\"Downloads\\"' in text
    assert "individual file paths" in text
    assert "_agent_import_prompt" in text
    # Codex must receive the same steering (no --append-system-prompt).
    assert "_stdin_prompt = _agent_import_prompt()" in text


def test_import_files_downloads_alias_and_videos_only(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    (downloads / "a.mp4").write_bytes(b"v")
    (downloads / "b.mov").write_bytes(b"v")
    (downloads / "still.png").write_bytes(b"i")
    (downloads / "song.wav").write_bytes(b"a")
    (downloads / "notes.txt").write_bytes(b"n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda p: str(home) if p in ("~", "") else (
            str(home) + p[1:] if isinstance(p, str) and p.startswith("~/") else p
        ),
    )

    win = MagicMock()
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))

    out = th.import_files(folder="downloads", dry_run="true", media_types="video")
    assert out.startswith("dry_run=true")
    assert str(downloads) in out or "Downloads" in out
    assert "would_import=2" in out
    assert "video=2" in out
    assert "audio=0" in out
    assert "image=0" in out
    assert "media_types=video" in out
    assert "skipped_non_media=3" in out
    win.files_model.add_files.assert_not_called()


def test_import_files_refuses_home_root(monkeypatch, tmp_path):
    from classes import tool_handlers as th

    home = tmp_path / "home"
    home.mkdir()
    (home / "secret.txt").write_bytes(b"x")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    win = MagicMock()
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))

    out = th.import_files(path=str(home), dry_run="true")
    assert out.startswith("Error:")
    assert "entire home folder" in out
    win.files_model.add_files.assert_not_called()


def test_import_files_userprofile_downloads_alias(monkeypatch, tmp_path):
    """Windows-style USERPROFILE home with bare Downloads alias."""
    from classes import tool_handlers as th

    home = tmp_path / "Users" / "alice"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    (downloads / "clip.mp4").write_bytes(b"v")

    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr("os.path.expanduser", lambda p: str(home) if p == "~" else p)

    win = MagicMock()
    monkeypatch.setattr(th, "_get_app", lambda: SimpleNamespace(window=win))

    out = th.import_files(folder="Downloads", dry_run=True, media_types="video")
    assert "would_import=1" in out
    assert "clip.mp4" in out
    win.files_model.add_files.assert_not_called()


@pytest.mark.parametrize("url, expected", [
    # Backslashes are not URL separators: the whole path parses as the host.
    ("file://C:\\clips\\a.mp4", "C:\\clips\\a.mp4"),
    ("file://C:/clips/a.mp4", "C:/clips/a.mp4"),
    ("file:///C:/clips/a.mp4", "C:/clips/a.mp4"),
    ("file://C:%5Cclips%5Cmy%20clip.mp4", "C:\\clips\\my clip.mp4"),
])
def test_normalize_agent_fs_path_windows_file_urls(monkeypatch, url, expected):
    from classes import file_drop as fd

    monkeypatch.setattr(fd, "_running_on_windows", lambda: True)
    monkeypatch.setattr(fd.os.path, "expanduser", lambda p: p)
    monkeypatch.setattr(fd.os.path, "exists", lambda p: False)
    monkeypatch.setattr(fd.os.path, "isabs", lambda p: True)
    monkeypatch.setattr(fd.os.path, "abspath", lambda p: p)
    assert fd.normalize_agent_fs_path(url) == expected
