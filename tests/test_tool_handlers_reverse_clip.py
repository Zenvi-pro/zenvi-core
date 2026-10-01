"""Unit tests for reverse_clip_tool."""

import os
import sys
import types
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


_FORWARD = {"Points": [{"co": {"X": 1, "Y": 1}}]}
_REVERSED = {"Points": [{"co": {"X": 1, "Y": 300}}, {"co": {"X": 300, "Y": 1}}]}


def _clip(clip_id="c1", time=None):
    c = MagicMock()
    c.id = clip_id
    c.data = {"id": clip_id, "title": "Clip", "position": 0.0, "start": 0.0, "end": 10.0,
              "time": time if time is not None else _FORWARD}
    return c


def _query_module(clip):
    """A classes.query whose Clip.get finds *clip* (other tests stub the real one)."""
    module = types.ModuleType("classes.query")
    module.Clip = types.SimpleNamespace(get=lambda **_k: clip)
    return module


def _run(clip, **kwargs):
    """reverse_clip with the clip resolved and the GUI-thread hop run inline."""
    timeline = MagicMock()
    app = MagicMock()
    app.window.timeline = timeline
    with patch.object(tool_handlers, "_get_app", return_value=app), \
            patch.object(tool_handlers, "_resolve_timeline_clip_for_tool",
                         return_value=ResolveResult(ok=True, clip=clip)), \
            patch.object(tool_handlers, "_run_on_main_thread",
                         side_effect=lambda fn, *a, **k: fn()), \
            patch.dict(sys.modules, {"classes.query": _query_module(clip)}):
        out = tool_handlers.reverse_clip(timeline_clip_id=clip.id, **kwargs)
    return out, timeline


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
    out, timeline = _run(_clip("c9"), mode="reverse")
    assert out.startswith("Reversed timeline_clip_id=c9")
    timeline.Time_Triggered.assert_called_once()
    args = timeline.Time_Triggered.call_args[0]
    assert args[1] == ["c9"]
    assert args[0].name == "REVERSE"


def test_reset_mode_uses_none():
    out, timeline = _run(_clip("c2", time=_REVERSED), mode="reset")
    assert "Reset time on timeline_clip_id=c2" in out
    assert timeline.Time_Triggered.call_args[0][0].name == "NONE"


def test_reversing_a_reversed_clip_changes_nothing():
    """Timeline > Speed > Reverse toggles; the tool's "reverse" must not play it forward."""
    out, timeline = _run(_clip("c3", time=_REVERSED), mode="reverse")
    assert "already reversed" in out
    timeline.Time_Triggered.assert_not_called()


def test_resetting_a_forward_clip_changes_nothing():
    out, timeline = _run(_clip("c4"), mode="reset")
    assert "already plays forward" in out
    timeline.Time_Triggered.assert_not_called()
