"""Ctrl+Y is Redo (it used to add a track, and Redo was Ctrl+Shift+Z only)."""

from __future__ import annotations

import json
import os

import pytest

DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "src", "settings", "_default.settings")


def _keys(value):
    return [k.strip() for k in str(value).split("|")]


def test_ctrl_y_is_redo_and_nothing_else_by_default():
    with open(DEFAULTS, encoding="utf-8") as f:
        keyboard = {s["setting"]: s["value"] for s in json.load(f) if s.get("category") == "Keyboard"}
    assert _keys(keyboard["actionRedo"]) == ["Ctrl+Shift+Z", "Ctrl+Y"]
    assert [name for name, value in keyboard.items() if "Ctrl+Y" in _keys(value)] == ["actionRedo"]
    taken = [k for name, value in keyboard.items() if name != "actionAddTrack" for k in _keys(value)]
    assert keyboard["actionAddTrack"] and not set(_keys(keyboard["actionAddTrack"])) & set(taken)


@pytest.mark.parametrize("saved, kept", [
    # A shortcut still on its old default follows the new one...
    ({"actionRedo": "Ctrl+Shift+Z", "actionAddTrack": "Ctrl+Y"}, []),
    # ...one the user chose is kept, and never left sharing Ctrl+Y with Redo.
    ({"actionRedo": "Ctrl+Alt+F12", "actionAddTrack": "Ctrl+Y"}, ["actionRedo"]),
    ({"actionRedo": "Ctrl+Shift+Z", "actionAddTrack": "Ctrl+Alt+F11"}, ["actionAddTrack"]),
    ({"actionAddTrack": "Ctrl+Y"}, []),
])
def test_existing_installs_get_ctrl_y_redo_unless_they_customised(tmp_path, monkeypatch, saved, kept):
    pytest.importorskip("PyQt5.QtWidgets")
    pytest.importorskip("openshot")  # classes.settings imports it
    from classes import info
    from classes.settings import SettingStore

    user_dir = tmp_path / "user"
    user_dir.mkdir()
    monkeypatch.setattr(info, "USER_PATH", str(user_dir))
    (user_dir / "openshot.settings").write_text(
        json.dumps([{"setting": name, "value": value} for name, value in saved.items()]), encoding="utf-8")

    store = SettingStore()
    store.load()
    with open(DEFAULTS, encoding="utf-8") as f:
        defaults = {s["setting"]: s["value"] for s in json.load(f) if "setting" in s}
    for name in ("actionRedo", "actionAddTrack"):
        assert store.get(name) == (saved[name] if name in kept else defaults[name])
