"""TAB skips tabified docks that are behind another tab (RC v1.2.0 B5).

Qt does not hide a tabified dock that is not the current tab: it moves it
off-screen, so its widgets stay "visible" and Tab walks into them. tabstops
takes them out of the chain once it knows the group's current tab, but it
matched tabs by text, and the Cosmic theme draws icon-only dock tabs: empty
text, title in the tooltip. Real Qt widgets and real Tab key presses.
"""

import os

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("PyQt5.QtTest")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication,
    QDockWidget,
    QLineEdit,
    QMainWindow,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from classes import tabstops  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _dock(win, name):
    dock = QDockWidget(name, win)
    dock.setObjectName("dock" + name)
    body = QWidget()
    layout = QVBoxLayout(body)
    field = QLineEdit()
    field.setObjectName("field" + name)
    layout.addWidget(field)
    dock.setWidget(body)
    win.addDockWidget(Qt.LeftDockWidgetArea, dock)
    return dock, field


def _icon_only_tabs(win):
    """What themes/cosmic does to dock tab bars: icon-only, title in tooltip."""
    for bar in win.findChildren(QTabBar):
        for i in range(bar.count()):
            bar.setTabToolTip(i, bar.tabText(i))
            bar.setTabText(i, "")


@pytest.fixture
def window():
    win = QMainWindow()
    central = QLineEdit()
    central.setObjectName("central")
    win.setCentralWidget(central)
    files, files_field = _dock(win, "Files")
    effects, effects_field = _dock(win, "Effects")
    emojis, emojis_field = _dock(win, "Emojis")
    win.tabifyDockWidget(files, effects)
    win.tabifyDockWidget(effects, emojis)
    files.raise_()
    win.resize(800, 500)
    win.show()
    QApplication.setActiveWindow(win)
    QApplication.processEvents()
    _icon_only_tabs(win)
    yield win, {"central": central, "Files": files_field, "Effects": effects_field, "Emojis": emojis_field}
    win.close()
    win.deleteLater()
    QApplication.processEvents()


def _tab_stops(start, presses=14):
    start.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    stops = []
    for _ in range(presses):
        QTest.keyClick(QApplication.focusWidget(), Qt.Key_Tab)
        QApplication.processEvents()
        stops.append(QApplication.focusWidget().objectName())
    return stops


def test_tab_titles_fall_back_to_the_tooltip(window):
    win, _fields = window
    bars = [b for b in win.findChildren(QTabBar) if b.count() == 3]
    assert bars, "the three docks share one tab bar"
    assert tabstops.tab_titles(bars[0]) == ["Files", "Effects", "Emojis"]


def test_tab_skips_docks_behind_the_current_tab(window):
    win, fields = window
    # Qt keeps the docks behind the current tab "visible", just off-screen.
    assert fields["Effects"].isVisible() and fields["Emojis"].isVisible()

    tabstops.apply_auto_tab_order(win, include_hidden=True, include_disabled=True)
    stops = _tab_stops(fields["central"])
    assert "fieldFiles" in stops
    assert "fieldEffects" not in stops and "fieldEmojis" not in stops, stops


def test_switching_tabs_brings_that_dock_into_the_chain(window):
    win, fields = window
    win.findChild(QDockWidget, "dockEmojis").raise_()
    QApplication.processEvents()
    _icon_only_tabs(win)  # Qt rewrites tab texts on a switch; the theme redoes this

    tabstops.apply_auto_tab_order(win, include_hidden=True, include_disabled=True)
    stops = _tab_stops(fields["central"])
    assert "fieldEmojis" in stops
    assert "fieldFiles" not in stops and "fieldEffects" not in stops, stops
