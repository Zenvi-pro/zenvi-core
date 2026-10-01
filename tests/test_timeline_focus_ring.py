"""The native timeline outlines itself while it has keyboard focus from Tab
(RC v1.2.0 B5), and not after a mouse click, so the main editing surface does
not wear a ring all session. Real-Qt test; the timeline module needs libopenshot.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication

from PyQt5.QtCore import QEvent, Qt  # noqa: E402
from PyQt5.QtGui import QColor, QFocusEvent, QImage, QPainter  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

_APP = QApplication.instance() or QApplication([])

qwidget_base = importlib.import_module("windows.views.timeline_backend.qwidget.base")
TimelineWidgetBase = qwidget_base.TimelineWidgetBase


@pytest.fixture
def timeline():
    # A real TimelineWidgetBase without its heavy __init__ (painters, app wiring).
    widget = TimelineWidgetBase.__new__(TimelineWidgetBase)
    QWidget.__init__(widget)
    widget._keyboard_focus = False
    widget.resize(200, 80)
    yield widget
    widget.deleteLater()


def _ring_pixel(widget, has_focus):
    widget.hasFocus = lambda: has_focus
    image = QImage(widget.size(), QImage.Format_ARGB32)
    image.fill(QColor("#0d0d0d"))
    painter = QPainter(image)
    TimelineWidgetBase._paint_focus_ring(widget, painter)
    painter.end()
    return image.pixelColor(1, widget.height() // 2)


@pytest.mark.parametrize("reason", [Qt.TabFocusReason, Qt.BacktabFocusReason])
def test_tab_focus_draws_the_ring(timeline, reason):
    TimelineWidgetBase.focusInEvent(timeline, QFocusEvent(QEvent.FocusIn, reason))
    assert _ring_pixel(timeline, True) == QColor("#4d9cf6")


@pytest.mark.parametrize("reason", [Qt.MouseFocusReason, Qt.OtherFocusReason])
def test_click_or_programmatic_focus_draws_no_ring(timeline, reason):
    TimelineWidgetBase.focusInEvent(timeline, QFocusEvent(QEvent.FocusIn, reason))
    assert _ring_pixel(timeline, True) == QColor("#0d0d0d")


def test_ring_goes_with_focus(timeline):
    TimelineWidgetBase.focusInEvent(timeline, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))
    TimelineWidgetBase.focusOutEvent(timeline, QFocusEvent(QEvent.FocusOut, Qt.TabFocusReason))
    assert _ring_pixel(timeline, False) == QColor("#0d0d0d")
