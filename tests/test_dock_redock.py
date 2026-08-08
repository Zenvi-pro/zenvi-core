"""Undock/re-dock behaviour for dock widgets (Windows/Linux docking fix).

A floating QDockWidget with no title bar widget gets *native* window
decorations, and window frame drags are never delivered to Qt on Windows or
Linux — so the panel turns into an unrelated window that can't be dragged back
in. These tests pin the two things that keep panels dockable there:

  1. floating panels keep a Qt-drawn (frameless) title bar, and
  2. re-docking works even when Qt has no saved position to restore.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtCore import Qt, QEvent, QPoint  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QDockWidget, QLabel, QMainWindow, QWidget,
)

from classes.docking import DockingMixin  # noqa: E402
from classes.title_bar import HiddenTitleBar  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class DockWindow(DockingMixin, QMainWindow):
    """MainWindow's docking behaviour, without the rest of the application.

    MainWindow.__init__ needs the whole app (project, libopenshot, settings),
    so mix DockingMixin into a bare QMainWindow instead.
    """

    def __init__(self):
        super().__init__()
        self.last_dock_areas = {}

    def style_dock_widgets(self):
        """MainWindow.style_dock_widgets, minus the theme lookup."""
        self.apply_dock_titlebars()


def make_dock(window, name, title, area, features=None, allowed=None):
    dock = QDockWidget(title, window)
    dock.setObjectName(name)
    if features is not None:
        dock.setFeatures(features)
    if allowed is not None:
        dock.setAllowedAreas(allowed)
    dock.setWidget(QLabel(title))
    window.addDockWidget(area, dock)
    return dock


@pytest.fixture
def window(qapp):
    win = DockWindow()
    win.files = make_dock(win, "dockFiles", "Files", Qt.TopDockWidgetArea)
    win.timeline = make_dock(
        win, "dockTimeline", "Timeline", Qt.BottomDockWidgetArea,
        features=QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetMovable)
    win.tutorial = make_dock(
        win, "dockTutorial", "Tutorial", Qt.LeftDockWidgetArea,
        allowed=Qt.NoDockWidgetArea)
    win.show()
    win.style_dock_widgets()
    yield win
    win.close()


def is_frameless(widget):
    return bool(int(widget.windowFlags()) & int(Qt.FramelessWindowHint))


def test_docked_area_is_remembered(window):
    assert window.last_dock_areas["dockFiles"] == Qt.TopDockWidgetArea


def test_floating_dock_keeps_a_qt_drawn_title_bar(window):
    window.files.setFloating(True)
    window.style_dock_widgets()

    titlebar = window.files.titleBarWidget()
    assert isinstance(titlebar, HiddenTitleBar)
    assert titlebar.title_label.text() == "Files"
    assert titlebar.float_btn.toolTip() == "Dock"
    # Frameless == Qt owns the title bar == the panel can be dragged back in
    assert is_frameless(window.files)


def test_title_bar_dock_button_redocks(window):
    window.files.setFloating(True)
    window.style_dock_widgets()

    window.files.titleBarWidget().float_btn.click()

    assert not window.files.isFloating()
    assert window.dockWidgetArea(window.files) == Qt.TopDockWidgetArea
    assert window.files.isVisible()


def test_double_click_on_title_bar_redocks(window):
    window.files.setFloating(True)
    window.style_dock_widgets()

    window.files.titleBarWidget().mouseDoubleClickEvent(QMouseEvent(
        QEvent.MouseButtonDblClick, QPoint(4, 4),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))

    assert not window.files.isFloating()


def test_redock_widget_rescues_a_dock_with_no_saved_position(window):
    """setFloating(False) alone can't dock a panel Qt has no placeholder for."""
    window.removeDockWidget(window.files)
    window.files.setFloating(True)
    window.files.show()

    window.files.setFloating(False)
    assert window.files.isFloating(), "expected Qt's own un-float to be a no-op here"

    window.redock_widget(window.files)

    assert not window.files.isFloating()
    assert window.dockWidgetArea(window.files) != Qt.NoDockWidgetArea


def test_floating_timeline_gets_a_drag_handle(window):
    """Docked, the timeline has a 0px title bar; floating, it needs a real one."""
    window.timeline.setFloating(True)
    window.style_dock_widgets()

    titlebar = window.timeline.titleBarWidget()
    assert isinstance(titlebar, HiddenTitleBar)
    assert titlebar.height() > 0
    # Timeline isn't closable, so it must not offer a close button
    assert titlebar.findChild(QWidget, "dock-close-button") is None

    window.redock_widget(window.timeline)

    assert not window.timeline.isFloating()
    assert window.dockWidgetArea(window.timeline) == Qt.BottomDockWidgetArea
    assert type(window.timeline.titleBarWidget()) is QWidget


def test_float_only_panels_keep_native_decorations(window):
    """The tutorial dock is float-only; it must not be turned into a panel."""
    window.tutorial.setFloating(True)
    window.style_dock_widgets()
    assert window.tutorial.titleBarWidget() is None

    window.redock_widget(window.tutorial)
    assert window.tutorial.isFloating()


def test_redock_all_widgets_docks_everything(window):
    window.files.setFloating(True)
    window.timeline.setFloating(True)
    window.tutorial.setFloating(True)
    window.style_dock_widgets()

    window.redock_all_widgets()

    assert not window.files.isFloating()
    assert not window.timeline.isFloating()
    # Float-only panels are left alone
    assert window.tutorial.isFloating()


def test_restyling_reuses_the_existing_title_bar(window):
    window.files.setFloating(True)
    window.style_dock_widgets()
    first = window.files.titleBarWidget()

    window.style_dock_widgets()

    assert window.files.titleBarWidget() is first


def test_fallback_area_respects_allowed_areas(window):
    dock = QDockWidget("Plan", window)
    dock.setObjectName("dockPlan")
    dock.setAllowedAreas(Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
    assert window.fallback_dock_area(dock) == Qt.RightDockWidgetArea

    dock.setAllowedAreas(Qt.BottomDockWidgetArea)
    assert window.fallback_dock_area(dock) == Qt.BottomDockWidgetArea


def test_assistant_docks_back_to_the_right(window):
    """Zenvi Assistant returns to the side _apply_default_ai_chat_dock() uses.

    It's built in code rather than the .ui file, so a session that has only
    ever seen it floating has no remembered area for it.
    """
    dock = QDockWidget("Zenvi Assistant", window)
    dock.setObjectName("AIChatWindow")
    window.addDockWidget(Qt.RightDockWidgetArea, dock)
    dock.setFloating(True)
    window.last_dock_areas.pop("AIChatWindow", None)

    assert window.fallback_dock_area(dock) == Qt.RightDockWidgetArea

    window.redock_widget(dock)

    assert not dock.isFloating()
    assert window.dockWidgetArea(dock) == Qt.RightDockWidgetArea
