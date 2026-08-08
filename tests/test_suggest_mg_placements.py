"""Unit tests for suggest_motion_graphics_placements (no Qt)."""

from __future__ import annotations

import json
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

import classes.tool_handlers as th  # noqa: E402


def test_suggest_motion_graphics_placements_json_shape():
    ctx = SimpleNamespace(
        timeline_position=0.0,
        timeline_end=5.0,
        summary_preview="wide establishing shot of city",
        title="Broll",
        layer=1000000,
        file_id="F1",
        effective_metadata={
            "short_summary": "wide establishing b-roll",
            "chapters": [{"title": "Wide", "summary": "establishing exterior"}],
            "scene_descriptions": [],
        },
    )
    ctx2 = SimpleNamespace(
        timeline_position=5.0,
        timeline_end=10.0,
        summary_preview="close-up talking head interview",
        title="Interview",
        layer=1000000,
        file_id="F2",
        effective_metadata={
            "short_summary": "close-up face talking head",
            "chapters": [],
            "scene_descriptions": [],
        },
    )

    fake_mod = MagicMock()
    fake_mod.enumerate_timeline_contexts.return_value = [ctx, ctx2]
    sys.modules["classes.timeline_clip_context"] = fake_mod

    with patch.object(th, "_get_app") as app:
        app.return_value.project.get.return_value = [
            {"number": 1000000},
            {"number": 2000000},
            {"number": 3000000},
        ]
        out = th.suggest_motion_graphics_placements(
            brief="cinematic trailer with motion graphics",
            beat_count="4",
        )

    data = json.loads(out)
    assert "beats" in data
    assert len(data["beats"]) == 4
    for beat in data["beats"]:
        assert beat["role"] in ("opaque_title", "transparent_overlay", "transition")
        assert beat["mode"] in ("standalone", "overlay")
        assert "position_seconds" in beat
        assert "track_hint" in beat
        assert "transparent" in beat
    # At least one opaque standalone and one transparent overlay
    assert any(b["mode"] == "standalone" and not b["transparent"] for b in data["beats"])
    assert any(b["mode"] == "overlay" and b["transparent"] for b in data["beats"])
