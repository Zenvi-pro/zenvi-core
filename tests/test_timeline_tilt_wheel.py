"""Mouse wheel tilt (left/right) and SHIFT+wheel scroll the native timeline
horizontally (OpenShot #6052). Real-Qt test."""

from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication

from PyQt5.QtCore import Qt  # noqa: E402

qwidget_base = importlib.import_module("windows.views.timeline_backend.qwidget.base")


class _TimerStub:
    def __init__(self):
        self.started = 0
        self.active = False

    def start(self, *_args):
        self.started += 1
        self.active = True

    def isActive(self):
        return self.active


class _Delta:
    def __init__(self, x=0.0, y=0.0, is_null=False):
        self._x, self._y, self._null = float(x), float(y), bool(is_null)

    def x(self):
        return self._x

    def y(self):
        return self._y

    def isNull(self):
        return self._null


class _WheelEvent:
    def __init__(self, y_delta=0.0, x_delta=0.0, modifiers=Qt.NoModifier, pixel_x=None):
        self._modifiers = modifiers
        self._angle = _Delta(x=x_delta, y=y_delta)
        self._pixel = _Delta(is_null=True) if pixel_x is None else _Delta(x=pixel_x)
        self.accepted = False
        self.ignored = False

    def modifiers(self):
        return self._modifiers

    def angleDelta(self):
        return self._angle

    def pixelDelta(self):
        return self._pixel

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class _Helper:
    """Just enough TimelineWidgetBase state for wheelEvent + the h-scroll flush."""

    def __init__(self):
        self._pending_hscroll_delta = 0.0
        self._pending_vscroll_delta = 0.0
        self._hscroll_timer = _TimerStub()
        self._vscroll_timer = _TimerStub()
        self.scrollbar_position = [0.20, 0.60, 400.0, 100.0]
        self.v_scrollbar_position = [0.0, 0.0, 0.0, 0.0]
        self.h_scroll_offset = 80.0
        self.is_auto_center = True
        self.scrollbar_updates = 0
        self.viewport_reset_calls = 0
        self.update_calls = 0
        self.dirty = 0
        self.geometry = types.SimpleNamespace(mark_dirty=self._mark_dirty)

    def _mark_dirty(self):
        self.dirty += 1

    def _update_scrollbar_handles(self):
        self.scrollbar_updates += 1

    def _schedule_viewport_thumbnail_reset(self):
        self.viewport_reset_calls += 1

    def update(self):
        self.update_calls += 1


def _scroll(helper, event):
    scrolled = []
    app = types.SimpleNamespace(window=types.SimpleNamespace(
        TimelineScrolled=types.SimpleNamespace(emit=lambda positions: scrolled.append(list(positions)))))
    with patch.object(qwidget_base, "get_app", return_value=app):
        qwidget_base.TimelineWidgetBase.wheelEvent(helper, event)
        helper._hscroll_timer.active = False
        qwidget_base.TimelineWidgetBase._flush_pending_horizontal_scroll(helper)
    return scrolled


def test_tilt_wheel_scrolls_horizontally_without_shift():
    helper = _Helper()
    event = _WheelEvent(x_delta=-120.0)

    scrolled = _scroll(helper, event)

    assert event.accepted and not event.ignored
    assert helper._hscroll_timer.started == 1
    assert helper.scrollbar_position[0] == pytest.approx(0.24)
    assert helper.scrollbar_position[1] == pytest.approx(0.64)
    assert helper.h_scroll_offset == pytest.approx(0.24 * 400.0)
    assert helper.is_auto_center is False
    assert scrolled == [helper.scrollbar_position]
    assert helper._pending_vscroll_delta == 0.0  # not treated as a vertical scroll


def test_trackpad_pixel_delta_wins_over_angle_delta():
    helper = _Helper()
    event = _WheelEvent(x_delta=-120.0, pixel_x=60.0)  # swipe right -> scroll left

    _scroll(helper, event)

    assert event.accepted
    assert helper.scrollbar_position[0] == pytest.approx(0.18)


def test_shift_wheel_scrolls_horizontally():
    helper = _Helper()
    event = _WheelEvent(y_delta=-120.0, modifiers=Qt.ShiftModifier)

    _scroll(helper, event)

    assert event.accepted and not event.ignored
    assert helper.scrollbar_position[0] == pytest.approx(0.24)


def test_plain_vertical_wheel_is_untouched_by_horizontal_path():
    helper = _Helper()
    helper.v_scrollbar_position = [0.0, 0.5, 400.0, 200.0]
    event = _WheelEvent(y_delta=-120.0)

    _scroll(helper, event)

    assert helper._hscroll_timer.started == 0
    assert helper._pending_vscroll_delta == pytest.approx(1.0)
    assert helper.scrollbar_position[0] == pytest.approx(0.20)
