"""project-export: preferences (get/set/restore) and the editor layout."""

import copy
import json
import os
from unittest.mock import MagicMock

import pytest

_DEFAULTS = os.path.join(os.path.dirname(__file__), "..", "src", "settings", "_default.settings")


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


class _Store:
    """SettingStore over the real _default.settings list."""

    defaults_path = _DEFAULTS

    def __init__(self):
        with open(_DEFAULTS, encoding="utf-8") as fh:
            self._data = json.load(fh)
        self.saved = 0
        self.restored = []

    def get_all_settings(self):
        return self._data

    def get(self, key, default=None):
        for d in self._data:
            if d.get("setting", "").lower() == key.lower():
                return d.get("value")
        return default

    def set(self, key, value):
        for d in self._data:
            if d.get("setting", "").lower() == key.lower():
                d["value"] = value

    def save(self):
        self.saved += 1

    def restore(self, category_filter=None):
        self.restored.append(category_filter)
        with open(_DEFAULTS, encoding="utf-8") as fh:
            defaults = {d["setting"]: d for d in json.load(fh)}
        for d in self._data:
            if d.get("category") == category_filter and d["setting"] in defaults:
                d["value"] = copy.deepcopy(defaults[d["setting"]].get("value"))
        return False


@pytest.fixture
def prefs(editor, monkeypatch):
    store = _Store()
    editor.app.get_settings.return_value = store
    editor.app.get_settings.side_effect = None
    effects = MagicMock()
    monkeypatch.setattr("classes.preference_effects.apply_preference_side_effects", effects)
    editor.effects = effects
    return store


def test_get_preferences_by_tab_key_and_query(editor, prefs):
    data = _receipt(editor.call("get_preferences_tool", tab="Autosave"))
    keys = {p["key"] for p in data["preferences"]}
    assert keys == {"enable-auto-save", "autosave-interval", "history-limit", "recovery-limit"}
    interval = next(p for p in data["preferences"] if p["key"] == "autosave-interval")
    assert (interval["min"], interval["max"], interval["default"], interval["type"]) == (1.0, 60.0, 3.0, "spinner")
    data = _receipt(editor.call("get_preferences_tool", key="Cache Mode"))
    assert data["preference"]["choices"][0] == {"value": "CacheMemory", "name": "Memory"}
    data = _receipt(editor.call("get_preferences_tool"))
    tabs = {p["tab"] for p in data["preferences"]}
    assert "Keyboard" not in tabs, "shortcuts only with tab='Keyboard' or a query"
    assert not any(p["type"] == "hidden" for p in data["preferences"])
    data = _receipt(editor.call("get_preferences_tool", query="undo"))
    assert any(p["tab"] == "Keyboard" for p in data["preferences"])
    assert "internal setting" in editor.call("get_preferences_tool", key="unique_install_id")
    assert "Did you mean" in editor.call("get_preferences_tool", key="autosave")


def test_set_preference_validates_types_ranges_and_choices(editor, prefs):
    data = _receipt(editor.call("set_preference_tool", key="autosave-interval", value="5"))
    assert data["value"] == 5.0 and prefs.get("autosave-interval") == 5.0 and prefs.saved == 1
    editor.effects.assert_called_with("autosave-interval", 5.0)
    assert "between 1.0 and 60.0" in editor.call("set_preference_tool", key="autosave-interval", value="0")
    assert "whole number" in editor.call("set_preference_tool", key="history-limit", value="2.5")
    assert "true or false" in editor.call("set_preference_tool", key="enable-auto-save", value="maybe")
    _receipt(editor.call("set_preference_tool", key="enable-auto-save", value="off"))
    assert prefs.get("enable-auto-save") is False
    _receipt(editor.call("set_preference_tool", key="cache-mode", value="Disk"))
    assert prefs.get("cache-mode") == "CacheDisk"
    out = editor.call("set_preference_tool", key="cache-mode", value="Tape")
    assert out.startswith("Error") and "CacheMemory" in out
    data = _receipt(editor.call("set_preference_tool", key="default-samplerate", value="44100"))
    assert data["value"] == 44100 and data["restart_required"] is True
    data = _receipt(editor.call("set_preference_tool", key="default-samplerate", value="44100"))
    assert data["changed"] is False
    assert editor.undo_steps_since_mark() == 0


def test_protected_hidden_and_unknown_preferences_are_refused(editor, prefs):
    assert "protected" in editor.call("set_preference_tool", key="zenvi-backend-url", value="http://evil")
    assert prefs.get("zenvi-backend-url") == "https://api.zenvi.pro"
    assert "internal setting" in editor.call("set_preference_tool", key="recent_projects", value="[]")
    assert "no preference" in editor.call("set_preference_tool", key="warp-drive", value="1")
    assert "give key and value" in editor.call("set_preference_tool")


def test_keyboard_shortcuts_check_conflicts(editor, prefs):
    taken = prefs.get("actionUndo")
    out = editor.call("set_preference_tool", key="actionRedo", value=taken.split("|")[0].strip())
    assert out.startswith("Error") and "conflict" in out
    assert "not a key sequence" in editor.call("set_preference_tool", key="actionRedo", value="Ctrl++++")
    _receipt(editor.call("set_preference_tool", key="actionRedo", value="Ctrl+Alt+F12"))
    assert prefs.get("actionRedo") == "Ctrl+Alt+F12"
    editor.window.initShortcuts.assert_called()


def test_default_profile_takes_a_profile_name(editor, prefs):
    _receipt(editor.call("set_preference_tool", key="default-profile", value="instagram_reel"))
    assert prefs.get("default-profile").startswith("FHD Vertical 1080p")


def test_restore_tab_defaults(editor, prefs):
    prefs.set("autosave-interval", 42.0)
    data = _receipt(editor.call("set_preference_tool", restore_tab="Autosave"))
    assert prefs.restored == ["Autosave"] and prefs.get("autosave-interval") == 3.0
    assert data["restart_required"] is False
    assert "not both" in editor.call("set_preference_tool", key="volume", value="5", restore_tab="General")


# --- layout ---------------------------------------------------------------

class _Dock:
    def __init__(self, name, title, open_=True):
        self._name, self._title, self.open = name, title, open_

    def objectName(self):
        return self._name

    def windowTitle(self):
        return self._title

    def hide(self):
        self.open = False

    def raise_(self):
        pass


@pytest.fixture
def layout(editor):
    win = editor.window
    docks = [_Dock("dockFiles", "Project Files"), _Dock("dockProperties", "Properties", False),
             _Dock("dockHistogram", "Histogram", False), _Dock("dockTimeline", "Timeline")]
    win.getDocks.return_value = docks
    win.dockProperties = docks[1]
    win.propertyTableView.color_grade_wheels_dock = None
    win.HIDDEN_DOCK_OBJECT_NAMES = set()
    win._dock_is_open.side_effect = lambda d: d.open
    win._scope_docks.return_value = [docks[2]]
    win._anchor_and_show_scope_dock.side_effect = lambda d: setattr(d, "open", True)
    win._anchor_and_show_properties_dock.side_effect = lambda: setattr(docks[1], "open", True)
    views = [{"id": "v1", "name": "Review", "state": "x"}]
    win._custom_views.side_effect = lambda: list(views)
    win._set_custom_views.side_effect = lambda new: (views.clear(), views.extend(new))
    win._active_custom_view.return_value = None
    win._current_custom_view_data.side_effect = lambda vid, name: {"id": vid, "name": name, "state": "s"}
    win.isFullScreen.return_value = False
    s = MagicMock()
    s.get.side_effect = lambda k, d=None: {"active_builtin_view": "simple"}.get(k, d)
    editor.app.get_settings.return_value = s
    editor.app.get_settings.side_effect = None
    editor.docks, editor.views = docks, views
    return win


def test_switch_views_and_panels(editor, layout):
    data = _receipt(editor.call("set_editor_layout_tool", view="Color Grading"))
    layout.actionColor_Grade_View_trigger.assert_called_once()
    data = _receipt(editor.call("set_editor_layout_tool", show_panels=["properties", "Histogram"],
                                hide_panels=["Project Files"]))
    assert "Properties" in data["open_panels"] and "Histogram" in data["open_panels"]
    assert "Project Files" in data["closed_panels"]
    out = editor.call("set_editor_layout_tool", show_panels=["Holodeck"])
    assert out.startswith("Error") and "Project Files" in out
    assert "no view named" in editor.call("set_editor_layout_tool", view="cinema mode")
    _receipt(editor.call("set_editor_layout_tool", view="review"))
    layout.apply_custom_view.assert_called_once_with("v1")
    assert editor.undo_steps_since_mark() == 0


def test_scopes_toolbar_fullscreen(editor, layout):
    _receipt(editor.call("set_editor_layout_tool", scopes="show_all", toolbar="hide", fullscreen="on"))
    layout.show_all_scope_docks.assert_called_once()
    layout.toolBar.setVisible.assert_called_with(False)
    layout.actionFullscreen_trigger.assert_called_once()
    _receipt(editor.call("set_editor_layout_tool", scopes="hide_all"))
    layout.closeDocks.assert_called_once()
    assert "say what to change" in editor.call("set_editor_layout_tool")


def test_custom_views_save_and_delete(editor, layout):
    assert "already exists" in editor.call("set_editor_layout_tool", save_view_as="review")
    _receipt(editor.call("set_editor_layout_tool", save_view_as="Grade"))
    assert [v["name"] for v in editor.views] == ["Review", "Grade"]
    _receipt(editor.call("set_editor_layout_tool", delete_view="Review"))
    assert [v["name"] for v in editor.views] == ["Grade"]
    assert "no saved custom view" in editor.call("set_editor_layout_tool", delete_view="Review")
    assert "no custom view is active" in editor.call("set_editor_layout_tool", update_active_view=True)


def test_get_editor_layout_is_read_only(editor, layout):
    data = _receipt(editor.call("get_editor_layout_tool"))
    assert data["active_view"] == "simple" and "Project Files" in data["open_panels"]
    assert data["custom_views"] == [{"name": "Review", "id": "v1"}]
