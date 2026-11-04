"""A playback tick moves only the playhead: the native timeline must not
repaint every clip, waveform and thumbnail ~30 times a second (#267).
Real-Qt test."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")

from PyQt5.QtCore import QRect  # noqa: E402
from PyQt5.QtGui import QPaintEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

_APP = QApplication.instance() or QApplication([])

qwidget = importlib.import_module("windows.views.timeline_backend.qwidget")

PAINTERS = (
    "bg_painter", "track_painter", "keyframe_panel_painter", "clip_painter",
    "transition_painter", "playback_cache_painter", "keyframe_painter",
    "selection_painter", "ruler_painter", "marker_painter", "playhead_painter",
    "scrollbar_painter",
)


@pytest.fixture
def timeline():
    widget = qwidget.TimelineWidget.__new__(qwidget.TimelineWidget)
    QWidget.__init__(widget)
    widget.resize(400, 120)
    widget._in_paint_event = False
    widget._repaint_after_paint = False
    widget._dragging_panel_keyframes = True  # skip the panel signature refresh
    widget._dragging_keyframe = None
    widget._keyboard_focus = False
    widget.h_scroll_offset = 0.0
    widget.pixels_per_second = 100.0
    widget.current_frame = 1
    widget.geometry = MagicMock()
    for name in ("_ensure_keyframe_markers", "_apply_panel_drag_marker_override",
                 "_apply_keyframe_drag_panel_override", "_paint_drag_preview"):
        setattr(widget, name, lambda *a, **k: None)
    for name in PAINTERS:
        setattr(widget, name, MagicMock())
    app = SimpleNamespace(window=SimpleNamespace(timeline=object()))
    with patch.object(qwidget.base, "get_app", lambda: app):
        yield widget
    widget.deleteLater()


def _paint(widget):
    widget.paintEvent(QPaintEvent(QRect(0, 0, widget.width(), widget.height())))


def test_playhead_tick_repaints_only_the_playhead(timeline):
    _paint(timeline)
    assert timeline.clip_painter.paint.call_count == 1

    for frame in (2, 3, 4):
        timeline.update_playhead_pos(frame)
        _paint(timeline)

    assert timeline.clip_painter.paint.call_count == 1
    assert timeline.track_painter.paint_background.call_count == 1
    assert timeline.playhead_painter.paint.call_count == 4
    assert timeline.ruler_painter.paint_overlay.call_count == 4  # the time readout follows


def test_any_other_change_repaints_everything(timeline):
    _paint(timeline)
    timeline.update()  # edits, hover, selection, cache bar, theme...
    _paint(timeline)
    assert timeline.clip_painter.paint.call_count == 2

    timeline.h_scroll_offset = 50.0  # auto-follow scrolls during playback
    timeline.update_playhead_pos(90)
    _paint(timeline)
    assert timeline.clip_painter.paint.call_count == 3

    timeline.resize(500, 120)
    _paint(timeline)
    assert timeline.clip_painter.paint.call_count == 4
