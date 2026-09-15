"""Regression tests for first-N-seconds guard and source-window trim."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

from classes.clip_placement import compute_clip_trim_bounds, source_window_for_file  # noqa: E402
from classes import tool_handlers  # noqa: E402


def test_chained_subclip_uses_inner_start_end():
    inner = {"start": 12.0, "end": 18.0, "duration": 60.0, "reader": {"duration": 60.0}}
    s, e = source_window_for_file(inner)
    assert abs(s - 12.0) < 1e-9
    assert abs(e - 18.0) < 1e-9
    start, end = compute_clip_trim_bounds(
        e - s, trim_start=0.0, trim_dur=10.0, file_start=s,
    )
    assert start >= 12.0 - 1e-9
    assert end <= 18.0 + 1e-9


def test_watch_wider_than_trim_dur_recentres_on_peak():
    src_start, src_end = 0.0, 30.0
    in_s, out_s = 4.0, 16.0
    trim_dur = 5.0
    peak = 10.0
    in_s = max(src_start, peak - trim_dur / 2.0)
    out_s = min(src_end, in_s + trim_dur)
    if out_s - in_s < trim_dur:
        in_s = max(src_start, out_s - trim_dur)
    assert abs((out_s - in_s) - trim_dur) < 1e-9
    assert src_start <= in_s < out_s <= src_end
    assert in_s <= peak <= out_s


def test_add_clip_duration_without_watch_errors():
    file_obj = MagicMock()
    file_obj.id = "F1"
    file_obj.data = {
        "path": "/clips/long.mp4",
        "name": "long.mp4",
        "duration": 120.0,
        "start": 0.0,
        "end": 120.0,
        "has_video": True,
    }
    query_mod = MagicMock()
    query_mod.File.get.return_value = file_obj
    with patch.object(tool_handlers, "_get_app", return_value=MagicMock()):
        with patch.dict(sys.modules, {"classes.query": query_mod}):
            out = tool_handlers.add_clip_to_timeline(
                file_id="F1",
                position_seconds="0",
                duration_seconds="60",
            )
    assert out.startswith("Error:")
    assert "place_moment" in out
    assert "duration_seconds" in out
