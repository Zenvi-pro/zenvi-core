"""The properties panel and the dialogs take their colors from the design tokens.

The source checks run in the headless suite. The delegate checks paint a real
PropertyDelegate into a QImage, so they run only with ZENVI_REAL_QT=1 and
libopenshot importable (the same setup as tests/test_timeline_focus_ring.py).
"""

import os
import re
from pathlib import Path
from xml.etree import ElementTree

import pytest

from themes.cosmic.tokens import TOKENS

SRC = Path(__file__).resolve().parents[1] / "src"
WINDOWS = SRC / "windows"
UI_DIR = WINDOWS / "ui"

OWNED_PY = [
    "views/properties_tableview.py",
    "models/properties_model.py",
    "process_effect.py",
    "update_panel.py",
    "login_window.py",
    "title_editor.py",
    "about.py",
    "cutting.py",
    "generate.py",
    "preferences.py",
    "color_picker.py",
    "views/tutorial.py",
    "export.py",
]

HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})(?![0-9A-Za-z_-])")
COLOR_STYLE = re.compile(r"color|background|#|rgba?\(|palette", re.IGNORECASE)

REAL_QT = os.environ.get("ZENVI_REAL_QT") == "1"
needs_real_qt = pytest.mark.skipif(not REAL_QT, reason="paints a real PropertyDelegate: run with ZENVI_REAL_QT=1")


def test_owned_python_has_no_hex_color_literals():
    import ast

    found = []
    for rel in OWNED_PY:
        tree = ast.parse((WINDOWS / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in HEX_COLOR.finditer(node.value):
                    found.append(f"{rel}:{node.lineno}: {match.group(0)}")
    assert found == []


def test_owned_ui_files_have_no_color_style_sheets():
    found = []
    for path in sorted(UI_DIR.glob("*.ui")):
        if path.name == "main-window.ui":
            continue
        for prop in ElementTree.parse(path).getroot().iter("property"):
            if prop.get("name") != "styleSheet":
                continue
            text = (prop.findtext("string") or "").strip()
            if COLOR_STYLE.search(text):
                found.append(f"{path.name}: {text}")
    assert found == []


class _LegacyTheme:
    """The Humanity theme's .property_value colors: the loud blue in the reported screenshot."""

    def get_color(self, selector, prop):
        from qt_api import QColor
        return QColor(33, 125, 212) if prop == "foreground-color" else QColor(86, 86, 86)


class _LegacyThemeManager:
    def get_current_theme(self):
        return _LegacyTheme()


@pytest.fixture
def rig(monkeypatch):
    """A real PropertyDelegate over one numeric row, and a painter for its cell."""
    if not REAL_QT:
        pytest.skip("paints a real PropertyDelegate: run with ZENVI_REAL_QT=1")
    import importlib

    if not hasattr(importlib.import_module("openshot"), "CONSTANT"):
        pytest.skip("an earlier test left a stub openshot module")  # e.g. test_windows_update_launch
    importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication
    from PyQt5.QtCore import QRect
    from PyQt5.QtGui import QColor, QImage, QPainter, QStandardItem, QStandardItemModel
    from PyQt5.QtWidgets import QApplication, QStyle, QStyleOptionViewItem

    app = QApplication.instance() or QApplication([])
    tableview = importlib.import_module("windows.views.properties_tableview")

    class Rig:
        WIDTH, HEIGHT = 160, 28

        def __init__(self):
            self.model = QStandardItemModel(1, 2)
            self.label = QStandardItem("Alpha")
            self.value = QStandardItem("0.500")
            self.set_property(points=1)
            self.model.setItem(0, 0, self.label)
            self.model.setItem(0, 1, self.value)
            self.delegate = tableview.PropertyDelegate(model=self.model)

        def set_property(self, points):
            self.label.setData(("alpha", {
                "type": "float", "min": 0.0, "max": 1.0, "readonly": False,
                "points": points, "interpolation": 0,
            }))

        def paint(self, selected=False, hovered=False):
            option = QStyleOptionViewItem()
            option.rect = QRect(0, 0, self.WIDTH, self.HEIGHT)
            state = QStyle.State_Enabled
            if selected:
                state |= QStyle.State_Selected
            if hovered:
                state |= QStyle.State_MouseOver
            option.state = state
            image = QImage(self.WIDTH, self.HEIGHT, QImage.Format_ARGB32)
            image.fill(QColor(TOKENS["bg"]))
            painter = QPainter(image)
            self.delegate.paint(painter, option, self.model.index(0, 1))
            painter.end()
            return image

        @staticmethod
        def pixels(image):
            return {image.pixel(x, y) for x in range(image.width()) for y in range(image.height())}

    monkeypatch.setattr(app, "theme_manager", _LegacyThemeManager(), raising=False)
    return Rig(), QColor


def _argb(color):
    return color.rgb()


@needs_real_qt
def test_unselected_numeric_row_paints_no_accent(rig):
    row, QColor = rig
    assert _argb(QColor(TOKENS["accent"])) not in row.pixels(row.paint())


@needs_real_qt
def test_numeric_row_ignores_the_legacy_theme_blue(rig):
    row, QColor = rig
    assert _argb(QColor(33, 125, 212)) not in row.pixels(row.paint())


@needs_real_qt
def test_unselected_track_is_active_and_fill_is_muted(rig):
    row, QColor = rig
    image = row.paint()
    track_y = row.HEIGHT - 2  # the 2px track sits 3px above the bottom edge
    assert image.pixel(140, track_y) == _argb(QColor(TOKENS["active"]))  # past the 0.5 fill
    assert image.pixel(40, track_y) == _argb(QColor(TOKENS["text_muted"]))  # inside the fill


@needs_real_qt
def test_selected_numeric_row_fills_track_in_accent(rig):
    row, QColor = rig
    image = row.paint(selected=True)
    assert image.pixel(40, row.HEIGHT - 2) == _argb(QColor(TOKENS["accent"]))


@needs_real_qt
def test_keyframed_numeric_row_has_an_accent_dot(rig):
    row, QColor = rig
    row.set_property(points=2)
    assert _argb(QColor(TOKENS["accent"])) in row.pixels(row.paint())


@needs_real_qt
@pytest.mark.parametrize("readonly, selected", [(True, False), (False, True)])
def test_color_rows_keep_their_swatch_and_show_selection(rig, readonly, selected):
    row, QColor = rig
    row.label.setData(("color", {"type": "color", "readonly": readonly, "points": 0, "interpolation": 0,
                                 "min": 0, "max": 255,
                                 "red": {"value": 200}, "green": {"value": 30}, "blue": {"value": 40}}))
    pixels = row.pixels(row.paint(selected=selected))
    assert QColor(200, 30, 40).rgb() in pixels
    if selected:
        assert QColor(TOKENS["accent"]).rgb() in pixels
