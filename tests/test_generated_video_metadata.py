"""AI-generated clips carry agent-stamped metadata without Gemini (issue #86)."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
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

from classes.tool_handlers import _stamp_generated_video_metadata  # noqa: E402


def _stamp(prompt, data=None):
    f = SimpleNamespace(data=dict(data or {}), id="FILE1", save=MagicMock())
    with patch("classes.tool_handlers._get_app") as app:
        app.return_value.window = MagicMock()
        _stamp_generated_video_metadata(f, prompt)
    return f


def test_prompt_becomes_a_searchable_summary():
    f = _stamp("a paper plane gliding over a neon city at night")
    ai = f.data["ai_metadata"]
    assert "paper plane" in ai["short_summary"]
    assert ai["description"] == ai["short_summary"]
    # analyzed=True is what makes the scene panel and clip search show the clip
    # without waiting on Gemini (import already uses skip_indexing=True).
    assert ai["analyzed"] is True
    assert ai["source"] == "ai_video_generation"
    assert "ai_generated" in f.data["tags"]
    assert f.data["name"]
    f.save.assert_called_once()


def test_existing_ai_metadata_is_not_clobbered():
    f = _stamp("a paper plane", data={"ai_metadata": {"embedding_id": "E1"}})
    assert f.data["ai_metadata"]["embedding_id"] == "E1"
    assert f.data["ai_metadata"]["analyzed"] is True


def test_empty_prompt_does_not_stamp_analyzed():
    f = _stamp("   ")
    assert "ai_metadata" not in f.data
    f.save.assert_not_called()
