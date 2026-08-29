"""Default video export path is the user's Downloads folder."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from classes.info import get_downloads_path


def test_get_downloads_path_prefers_qt_standard_location(monkeypatch, tmp_path):
    downloads = tmp_path / "QtDownloads"
    downloads.mkdir()
    QtCore = pytest.importorskip("PyQt5.QtCore")

    monkeypatch.setattr(
        QtCore.QStandardPaths,
        "writableLocation",
        lambda location: str(downloads),
    )
    assert get_downloads_path() == str(downloads)


def test_get_downloads_path_falls_back_to_home_downloads(monkeypatch, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    try:
        from PyQt5.QtCore import QStandardPaths
        monkeypatch.setattr(QStandardPaths, "writableLocation", lambda location: "")
    except ImportError:
        pass

    def _expand(path):
        if path == "~":
            return str(tmp_path)
        return path

    monkeypatch.setattr(os.path, "expanduser", _expand)
    assert get_downloads_path() == str(downloads)


def _export_store(export_type=1, export_path="", project_path=""):
    pytest.importorskip("PyQt5.QtWidgets")
    from classes.settings import SettingStore

    store = SettingStore()
    store.app = MagicMock()
    store.app._tr = lambda s: s
    store.app.project.current_filepath = project_path
    store._data = [
        {"setting": "locationExportType", "value": export_type},
        {"setting": "locationExportPath", "value": export_path},
        {"setting": "locationImportType", "value": 1},
        {"setting": "locationImportPath", "value": ""},
        {"setting": "locationProjectType", "value": 1},
        {"setting": "locationProjectPath", "value": ""},
        {"setting": "exportDownloadsDefaultApplied", "value": True},
    ]
    return store


def test_get_default_export_path_uses_downloads(tmp_path, monkeypatch):
    pytest.importorskip("PyQt5.QtWidgets")
    from classes import info

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(info, "DOWNLOADS_PATH", str(downloads))

    store = _export_store()
    assert store.getDefaultPath(store.actionType.EXPORT) == str(downloads)


def test_get_default_export_path_uses_recent_folder(tmp_path, monkeypatch):
    pytest.importorskip("PyQt5.QtWidgets")
    from classes import info

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    recent = tmp_path / "Exports"
    recent.mkdir()
    monkeypatch.setattr(info, "DOWNLOADS_PATH", str(downloads))

    store = _export_store(export_type=1, export_path=str(recent))
    assert store.getDefaultPath(store.actionType.EXPORT) == str(recent)


def _write_settings(path, items):
    path.write_text(json.dumps(items), encoding="utf-8")


def test_load_migrates_project_folder_default(tmp_path, monkeypatch):
    pytest.importorskip("PyQt5.QtWidgets")
    from classes import info
    from classes.settings import SettingStore

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    monkeypatch.setattr(info, "USER_PATH", str(user_dir))

    defaults = [
        {"setting": "locationExportType", "value": 1},
        {"setting": "locationExportPath", "value": ""},
        {"setting": "exportDownloadsDefaultApplied", "value": True},
    ]
    defaults_path = tmp_path / "defaults.settings"
    _write_settings(defaults_path, defaults)
    _write_settings(
        user_dir / "openshot.settings",
        [
            {"setting": "locationExportType", "value": 2},
            {"setting": "locationExportPath", "value": ""},
        ],
    )

    store = SettingStore()
    store.defaults_path = str(defaults_path)
    store.load()

    assert store.get("locationExportType") == 1
    assert store.get("exportDownloadsDefaultApplied") is True


def test_load_keeps_project_folder_after_migration_flag(tmp_path, monkeypatch):
    pytest.importorskip("PyQt5.QtWidgets")
    from classes import info
    from classes.settings import SettingStore

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    monkeypatch.setattr(info, "USER_PATH", str(user_dir))

    defaults = [
        {"setting": "locationExportType", "value": 1},
        {"setting": "locationExportPath", "value": ""},
        {"setting": "exportDownloadsDefaultApplied", "value": True},
    ]
    defaults_path = tmp_path / "defaults.settings"
    _write_settings(defaults_path, defaults)
    _write_settings(
        user_dir / "openshot.settings",
        [
            {"setting": "locationExportType", "value": 2},
            {"setting": "locationExportPath", "value": ""},
            {"setting": "exportDownloadsDefaultApplied", "value": True},
        ],
    )

    store = SettingStore()
    store.defaults_path = str(defaults_path)
    store.load()

    assert store.get("locationExportType") == 2
