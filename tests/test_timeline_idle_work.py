"""Idle timeline work: the cache poll stops while paused, and routine edits redraw
clips without clearing thumbnails. Real-Qt test."""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")
openshot = pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")

from PyQt5.QtCore import QPointF, QRectF, QTimer, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

_APP = QApplication.instance() or QApplication([])

qwidget = importlib.import_module("windows.views.timeline_backend.qwidget")
query = importlib.import_module("classes.query")


@pytest.fixture(scope="module")
def tl():
    parked = sys.modules.get("windows.views.timeline")
    if parked is not None and not hasattr(getattr(parked, "TimelineView", None), "addClip"):
        del sys.modules["windows.views.timeline"]  # a stand-in parked there by another test module
    if not hasattr(_APP, "_tr"):
        _APP._tr = lambda text: text
    return importlib.import_module("windows.views.timeline")


def _set_cache_version(view, version):
    cache = view.window.timeline_sync.timeline.GetCache.return_value
    cache.Json.return_value = json.dumps({"version": version})


def _cache_view(tl, *, playing):
    """The cache-poll attributes of a TimelineView, with the real methods bound."""
    view = SimpleNamespace(
        cache_renderer=QTimer(), cache_renderer_version=None, cache_renderer_idle_ticks=0,
        update_playback_cache=MagicMock(), run_js=MagicMock(), window=MagicMock(),
    )
    view.cache_renderer.setInterval(300)
    view.window.preview_thread.player.Mode.return_value = (
        openshot.PLAYBACK_PLAY if playing else openshot.PLAYBACK_PAUSED
    )
    _set_cache_version(view, 1)
    for name in ("render_cache_json", "poll_cache_renderer", "resume_cache_renderer"):
        setattr(view, name, types.MethodType(getattr(tl.TimelineView, name), view))
    return view


def test_cache_poll_stops_only_after_a_few_idle_seconds_while_paused(tl):
    # A slow paused fill can leave the cache version unchanged for a second; stopping
    # after 0.9 s left the cached-ranges bar stale
    view = _cache_view(tl, playing=False)
    view.cache_renderer.setInterval(300)
    view.cache_renderer.start()
    view.poll_cache_renderer()  # first render picks up the cache version
    assert tl.CACHE_POLL_IDLE_TICKS * view.cache_renderer.interval() >= 3000
    for _ in range(tl.CACHE_POLL_IDLE_TICKS - 1):
        view.poll_cache_renderer()
    assert view.cache_renderer.isActive()
    view.poll_cache_renderer()
    assert not view.cache_renderer.isActive()


def test_cache_poll_keeps_running_while_playing(tl):
    view = _cache_view(tl, playing=True)
    view.cache_renderer.start()
    for _ in range(6):
        view.poll_cache_renderer()
    assert view.cache_renderer.isActive()


def test_cache_change_while_paused_restarts_the_idle_count(tl):
    view = _cache_view(tl, playing=False)
    view.cache_renderer.start()
    for _ in range(3):
        view.poll_cache_renderer()
    _set_cache_version(view, 2)
    view.poll_cache_renderer()  # the cache filled in: count starts over
    for _ in range(tl.CACHE_POLL_IDLE_TICKS - 1):
        view.poll_cache_renderer()
    assert view.cache_renderer.isActive()
    view.poll_cache_renderer()
    assert not view.cache_renderer.isActive()


def test_play_seek_or_timeline_change_restarts_a_stopped_poll(tl):
    view = _cache_view(tl, playing=False)
    view.cache_renderer.start()
    for _ in range(tl.CACHE_POLL_IDLE_TICKS + 1):
        view.poll_cache_renderer()
    assert not view.cache_renderer.isActive()

    view.resume_cache_renderer()
    assert view.cache_renderer.isActive()
    assert view.cache_renderer_idle_ticks == 0


@pytest.fixture
def widget():
    w = qwidget.TimelineWidget.__new__(qwidget.TimelineWidget)
    QWidget.__init__(w)
    w.win = MagicMock()
    w.clip_painter = MagicMock()
    w.geometry = MagicMock()
    w.snap = MagicMock()
    w.cursors = {"hand": None}
    w.changed = MagicMock()
    w._fix_cursor = MagicMock()
    w._release_cursor = MagicMock()
    w._keyframes_dirty = False
    yield w
    w.deleteLater()


def _clip_item(clip_id):
    return SimpleNamespace(id=clip_id, data={"position": 0.0, "start": 0.0, "end": 1.0, "layer": 0})


def _mouse_event(pos, modifiers):
    event = MagicMock()
    event.position.return_value = pos
    event.modifiers.return_value = modifiers
    return event


def test_shift_click_redraws_clips_without_clearing_thumbnails(widget):
    clip = _clip_item("7")
    widget._last_click_pos = {"type": "clip", "position": 0.0, "end_position": 1.0, "layer": 0}
    widget._last_event = _mouse_event(QPointF(10, 10), Qt.ShiftModifier)
    widget.geometry.iter_items.return_value = [(QRectF(0, 0, 100, 40), clip, False, "clip")]
    widget._selection_overlaps_locked_tracks = lambda items: True  # stop before the drag setup
    with patch.object(query.Clip, "filter", return_value=[clip]), \
            patch.object(query.Transition, "filter", return_value=[]):
        widget._startClipDrag()
    widget.win.addSelection.assert_called_once_with("7", "clip", False)
    widget.clip_painter.clear_render_cache.assert_called_once_with()
    widget.clip_painter.clear_cache.assert_not_called()


def test_deselect_redraws_clips_without_clearing_thumbnails(widget):
    widget._deselect_timeline_item("7", "clip")
    widget.win.removeSelection.assert_called_once_with("7", "clip")
    widget.clip_painter.clear_render_cache.assert_called_once_with()
    widget.clip_painter.clear_cache.assert_not_called()


def test_audio_redraw_after_a_slice_or_zoom_keeps_thumbnails(widget):
    qwidget.TimelineWidget.redraw_audio_data(widget)
    widget.clip_painter.clear_render_cache.assert_called_once_with()
    widget.clip_painter.clear_cache.assert_not_called()


def test_waveform_arrival_redraws_clips_without_clearing_thumbnails(tl):
    view = SimpleNamespace(clip_painter=MagicMock(), update=MagicMock())
    app = SimpleNamespace(updates=SimpleNamespace(transaction_id=None), window=MagicMock())
    with patch.object(tl, "get_app", return_value=app), \
            patch.object(tl.Clip, "get", return_value=SimpleNamespace(data={})), \
            patch.object(tl, "_save_waveform"), patch.object(tl, "QTimer"):
        tl.TimelineView.clipAudioDataReady_Triggered(
            view, "7", {"ui": {"audio_data": [0.1, 0.2]}}, "tid-1")
    view.clip_painter.clear_render_cache.assert_called_once_with()
    view.clip_painter.clear_cache.assert_not_called()
