"""Keyboard focus is visible on the main and timeline toolbar buttons (RC v1.2.0 B5).

The Cosmic theme styles those buttons by toolbar ID with "border: none", which
outranks the generic "QToolBar QToolButton:focus" ring, so a Tab stop there
showed nothing. Real Qt: apply the theme's stylesheet, Tab-focus a button and
read the pixel where the ring is drawn.
"""

import os
import types
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qt_api  # noqa: E402,F401  (QtWebEngine must load before any QApplication)
from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QColor  # noqa: E402
from PyQt5.QtWidgets import QApplication, QMainWindow, QToolBar, QWidget  # noqa: E402

RING = QColor("#4d9cf6")


@pytest.fixture(scope="module")
def cosmic_css():
    app = QApplication.instance() or QApplication([])
    import classes.app

    with patch.object(classes.app, "get_app", return_value=types.SimpleNamespace(_tr=lambda s: s)):
        from themes.cosmic.theme import CosmicTheme

        css = CosmicTheme(app).style_sheet
    yield css
    app.setStyleSheet("")


def _close(a, b, tol=40):
    return all(abs(x - y) <= tol for x, y in zip(a.getRgb()[:3], b.getRgb()[:3]))


def _has_ring(button):
    """The ring's left edge, past the button's own margin, on the middle row."""
    image = button.grab().toImage()
    row = image.height() // 2
    return any(_close(image.pixelColor(x, row), RING) for x in range(0, 8))


@pytest.mark.parametrize("toolbar_name", ["toolBar", "timelineToolbar", "videoToolbar"])
def test_tab_focused_toolbar_button_draws_the_focus_ring(cosmic_css, toolbar_name):
    app = QApplication.instance()
    app.setStyleSheet(cosmic_css)
    win = QMainWindow()
    win.setCentralWidget(QWidget())
    bar = QToolBar(win)
    bar.setObjectName(toolbar_name)
    win.addToolBar(bar)
    first = bar.addAction("One")
    second = bar.addAction("Two")
    buttons = [bar.widgetForAction(first), bar.widgetForAction(second)]
    for button in buttons:
        button.setFocusPolicy(Qt.StrongFocus)  # what classes/tabstops.py does
    win.resize(400, 200)
    win.show()
    QApplication.setActiveWindow(win)
    try:
        button = buttons[1]
        button.setFocus(Qt.TabFocusReason)
        QApplication.processEvents()
        assert button.hasFocus()

        assert _has_ring(button), "no focus ring on a %s button" % toolbar_name

        buttons[0].setFocus(Qt.TabFocusReason)
        QApplication.processEvents()
        assert not _has_ring(button), "the ring must follow focus away"
    finally:
        win.close()
        win.deleteLater()
        QApplication.processEvents()
