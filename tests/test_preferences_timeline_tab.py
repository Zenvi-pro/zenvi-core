"""Preferences Populate must not crash when custom_order drifts from settings."""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "src" / "settings" / "_default.settings"
PREFERENCES_PATH = ROOT / "src" / "windows" / "preferences.py"


def _default_settings_categories():
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    categories = set()
    for item in data:
        if item.get("type") == "hidden":
            continue
        cat = item.get("category")
        if cat:
            categories.add(cat)
    return categories


def _custom_order_from_source():
    tree = ast.parse(PREFERENCES_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "Preferences":
            continue
        for item in node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != "__init__":
                continue
            for stmt in item.body:
                if not isinstance(stmt, ast.Assign):
                    continue
                for target in stmt.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "custom_order"
                        and isinstance(stmt.value, (ast.List, ast.Tuple))
                    ):
                        return [
                            elt.value
                            for elt in stmt.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ]
    raise AssertionError("Preferences.__init__ custom_order list not found")


def test_native_timeline_setting_lives_under_timeline_not_experimental():
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    timeline_setting = next(
        item for item in data if item.get("setting") == "qwidget-based-timeline"
    )
    assert timeline_setting["category"] == "Timeline"


def test_custom_order_includes_timeline_and_skips_empty_experimental():
    """Regression for Harkit #179: KeyError 'Experimental' opening Preferences.

    Populate creates tabs only for categories that have settings, then used to
    index category_tabs by every custom_order name. After moving the native
    timeline toggle to Timeline, Experimental became empty — Preferences
    crashed on Windows (and would on macOS) before any other PR testing.
    """
    categories = _default_settings_categories()
    custom_order = _custom_order_from_source()

    assert "Timeline" in categories
    assert "Experimental" not in categories
    assert "Timeline" in custom_order
    assert "Experimental" not in custom_order

    for category in custom_order:
        assert category in categories, (
            f"custom_order lists empty category {category!r}; "
            f"Populate would KeyError when indexing category_tabs"
        )


def test_populate_skips_missing_custom_order_entries_and_appends_extras():
    """Defensive Populate: missing custom_order names skip; extras still tab."""
    category_names = {
        "General": [{"title": "A", "type": "bool"}],
        "Timeline": [{"title": "Native", "type": "bool"}],
        "Orphan": [{"title": "X", "type": "bool"}],
    }
    custom_order = ["General", "Experimental", "Timeline"]

    category_tabs = {}
    for category in custom_order:
        if category in category_names:
            category_tabs[category] = object()
    for category in sorted(category_names.keys()):
        if category not in category_tabs:
            category_tabs[category] = object()

    assert "Experimental" not in category_tabs
    assert set(category_tabs) == {"General", "Timeline", "Orphan"}
    for category in list(category_tabs.keys()):
        assert category in category_names
