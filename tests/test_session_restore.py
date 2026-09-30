"""Session restore helpers: last project, untitled backup, draft chat key."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import info, session_restore  # noqa: E402


class _Settings:
    def __init__(self, values=None):
        self._values = dict(values or {})
        self.saved = 0

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value

    def save(self):
        self.saved += 1


def test_normalize_restore_project_path_ignores_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(info, "BACKUP_FILE", str(tmp_path / "backup.zvn"))
    assert session_restore.normalize_restore_project_path("") == ""
    assert session_restore.normalize_restore_project_path(None) == ""
    assert session_restore.normalize_restore_project_path(info.BACKUP_FILE) == ""
    named = str(tmp_path / "edit.zvn")
    assert session_restore.normalize_restore_project_path(named) == os.path.abspath(named)


def test_set_restore_project_path_persists_once(monkeypatch, tmp_path):
    monkeypatch.setattr(info, "BACKUP_FILE", str(tmp_path / "backup.zvn"))
    settings = _Settings({"restore_project_path": ""})
    named = str(tmp_path / "edit.zvn")

    assert session_restore.set_restore_project_path(settings, named) is True
    assert settings.get("restore_project_path") == os.path.abspath(named)
    assert settings.saved == 1

    assert session_restore.set_restore_project_path(settings, named) is False
    assert settings.saved == 1

    assert session_restore.set_restore_project_path(settings, info.BACKUP_FILE) is True
    assert settings.get("restore_project_path") == ""


def test_normalize_draft_history_key():
    assert session_restore.normalize_draft_history_key("") == ""
    assert session_restore.normalize_draft_history_key("not-a-draft") == ""
    assert session_restore.normalize_draft_history_key("draft:abc") == "draft:abc"


def test_choose_restore_target_prefers_untitled_backup(tmp_path):
    settings = _Settings({
        "restore_project_path": str(tmp_path / "named.zvn"),
    })
    assert session_restore.choose_restore_target(settings, True) == (
        "untitled_backup", None
    )


def test_choose_restore_target_named(tmp_path):
    named = tmp_path / "last.zvn"
    named.write_text("{}")
    settings = _Settings({"restore_project_path": str(named)})
    assert session_restore.choose_restore_target(settings, False) == (
        "named", str(named)
    )


def test_choose_restore_target_missing(tmp_path):
    missing = str(tmp_path / "gone.zvn")
    settings = _Settings({"restore_project_path": missing})
    assert session_restore.choose_restore_target(settings, False) == (
        "missing", missing
    )


def test_choose_restore_target_blank():
    settings = _Settings({"restore_project_path": ""})
    assert session_restore.choose_restore_target(settings, False) == ("blank", None)


def test_flush_target_named_and_untitled(monkeypatch, tmp_path):
    monkeypatch.setattr(info, "BACKUP_FILE", str(tmp_path / "backup.zvn"))
    named = str(tmp_path / "proj.zvn")
    assert session_restore.flush_target(named) == (named, False)
    assert session_restore.flush_target(None) == (info.BACKUP_FILE, True)
    assert session_restore.flush_target("") == (info.BACKUP_FILE, True)


def test_should_flush_after_indexing():
    assert session_restore.should_flush_after_indexing(True, False) is True
    assert session_restore.should_flush_after_indexing(False, False) is False
    assert session_restore.should_flush_after_indexing(True, True) is False
