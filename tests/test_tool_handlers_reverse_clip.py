"""Unit tests for reverse_clip_tool."""

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

from classes import tool_handlers  # noqa: E402
from classes.clip_resolver import ResolveResult  # noqa: E402


def _clip(clip_id="c1"):
    c = MagicMock()
    c.id = clip_id
    c.data = {"id": clip_id, "title": "Clip", "position": 0.0, "start": 0.0, "end": 10.0}
    return c


def test_registered():
    assert "reverse_clip_tool" in tool_handlers.AGENT_TOOL_HANDLERS
    assert tool_handlers.AGENT_TOOL_HANDLERS["reverse_clip_tool"] is tool_handlers.reverse_clip
    assert "reverse_clip_tool" in tool_handlers.TOOL_DISPLAY_LABELS


def test_no_target_errors_without_resolve():
    with patch.object(tool_handlers, "_resolve_timeline_clip_for_tool") as resolver:
        out = tool_handlers.reverse_clip()
    assert out.startswith("Error:")
    assert "timeline_clip_id or clip_query" in out
    resolver.assert_not_called()


def test_bad_mode_errors_without_resolve():
    with patch.object(tool_handlers, "_resolve_timeline_clip_for_tool") as resolver:
        out = tool_handlers.reverse_clip(timeline_clip_id="c1", mode="warp")
    assert "mode must be" in out
    resolver.assert_not_called()


def test_reverse_calls_time_triggered():
    clip = _clip("c9")
    timeline = MagicMock()
    app = MagicMock()
    app.window.timeline = timeline

    with patch.object(tool_handlers, "_get_app", return_value=app):
        with patch.object(
            tool_handlers,
            "_resolve_timeline_clip_for_tool",
            return_value=ResolveResult(ok=True, clip=clip),
        ):
            with patch.object(tool_handlers, "_run_on_main_thread", side_effect=lambda fn, *a, **k: fn()):
                out = tool_handlers.reverse_clip(timeline_clip_id="c9", mode="reverse")

    assert out.startswith("Reversed timeline_clip_id=c9")
    timeline.Time_Triggered.assert_called_once()
    args = timeline.Time_Triggered.call_args[0]
    assert args[1] == ["c9"]
    assert args[0].name == "REVERSE"


def test_reset_mode_uses_none():
    clip = _clip("c2")
    timeline = MagicMock()
    app = MagicMock()
    app.window.timeline = timeline

    with patch.object(tool_handlers, "_get_app", return_value=app):
        with patch.object(
            tool_handlers,
            "_resolve_timeline_clip_for_tool",
            return_value=ResolveResult(ok=True, clip=clip),
        ):
            with patch.object(tool_handlers, "_run_on_main_thread", side_effect=lambda fn, *a, **k: fn()):
                out = tool_handlers.reverse_clip(timeline_clip_id="c2", mode="reset")

    assert "Reset time on timeline_clip_id=c2" in out
    assert timeline.Time_Triggered.call_args[0][0].name == "NONE"
