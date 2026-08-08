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


def _layers_patch():
    return patch.object(th, "_get_app")


def test_suggest_motion_graphics_placements_json_shape():
    ctx = SimpleNamespace(
        timeline_position=0.0,
        timeline_end=5.0,
        source_start=0.0,
        summary_preview="wide establishing shot of city",
        title="Broll",
        layer=1000000,
        file_id="F1",
        effective_metadata={
            "short_summary": "wide establishing b-roll",
            "chapters": [{"title": "Wide", "summary": "establishing exterior", "start": 0}],
            "scene_descriptions": [{"time": 1.0, "description": "wide skyline establishing"}],
        },
    )
    ctx2 = SimpleNamespace(
        timeline_position=5.0,
        timeline_end=10.0,
        source_start=0.0,
        summary_preview="close-up talking head interview",
        title="Interview",
        layer=1000000,
        file_id="F2",
        effective_metadata={
            "short_summary": "close-up face talking head",
            "chapters": [{"title": "Face", "summary": "close-up interview", "start": 0}],
            "scene_descriptions": [{"time": 1.0, "description": "close-up face"}],
        },
    )

    fake_mod = MagicMock()
    fake_mod.enumerate_timeline_contexts.return_value = [ctx, ctx2]
    sys.modules["classes.timeline_clip_context"] = fake_mod

    with _layers_patch() as app:
        app.return_value.project.get.return_value = [
            {"number": 1000000},
            {"number": 2000000},
            {"number": 3000000},
        ]
        out = th.suggest_motion_graphics_placements(
            brief="SHE SAW IT FIRST — trailer package",
            beat_count="4",
        )

    data = json.loads(out)
    assert "beats" in data
    assert len(data["beats"]) == 4
    for beat in data["beats"]:
        assert beat["mode"] in ("standalone", "overlay")
        assert "position_seconds" in beat
        assert "track_hint" in beat
        assert "transparent" in beat
        assert "block_query" in beat
        assert "title_hint" in beat
        assert beat["position_seconds"] <= data["timeline_end"] + 0.01
    assert any(b["mode"] == "overlay" and b["transparent"] for b in data["beats"])
    assert "chroma" in data["guidance"].lower() or "Z-order" in data["guidance"]


def test_suggest_continuous_footage_no_late_dump():
    """Continuous clips with no gaps must not place beats past timeline_end."""
    ctx = SimpleNamespace(
        timeline_position=0.0,
        timeline_end=8.0,
        source_start=0.0,
        summary_preview="action sequence",
        title="A",
        layer=1000000,
        file_id="F1",
        effective_metadata={"short_summary": "action", "chapters": [], "scene_descriptions": []},
    )
    ctx2 = SimpleNamespace(
        timeline_position=8.0,
        timeline_end=16.0,
        source_start=0.0,
        summary_preview="more action",
        title="B",
        layer=1000000,
        file_id="F2",
        effective_metadata={"short_summary": "action", "chapters": [], "scene_descriptions": []},
    )
    fake_mod = MagicMock()
    fake_mod.enumerate_timeline_contexts.return_value = [ctx, ctx2]
    sys.modules["classes.timeline_clip_context"] = fake_mod

    with _layers_patch() as app:
        app.return_value.project.get.return_value = [{"number": 1000000}, {"number": 3000000}]
        data = json.loads(
            th.suggest_motion_graphics_placements(brief="graphics package", beat_count="4")
        )

    end = data["timeline_end"]
    assert end == 16.0
    for beat in data["beats"]:
        assert beat["position_seconds"] <= end + 0.01, beat
        assert beat["position_seconds"] >= 0.0
    # Footage package: mostly transparent overlays
    assert sum(1 for b in data["beats"] if b["transparent"]) >= 3
    # Positions should span open→end (first near start, last near end)
    positions = [b["position_seconds"] for b in data["beats"]]
    assert min(positions) <= end * 0.35
    assert max(positions) >= end * 0.6


def test_suggest_prefers_wide_over_face_window():
    ctx_wide = SimpleNamespace(
        timeline_position=0.0,
        timeline_end=10.0,
        source_start=0.0,
        summary_preview="clip",
        title="C",
        layer=1000000,
        file_id="F1",
        effective_metadata={
            "short_summary": "mixed",
            "chapters": [],
            "scene_descriptions": [
                {"time": 1.0, "description": "close-up face talking head"},
                {"time": 7.0, "description": "wide establishing exterior b-roll"},
            ],
        },
    )
    fake_mod = MagicMock()
    fake_mod.enumerate_timeline_contexts.return_value = [ctx_wide]
    sys.modules["classes.timeline_clip_context"] = fake_mod

    with _layers_patch() as app:
        app.return_value.project.get.return_value = [{"number": 1000000}, {"number": 3000000}]
        data = json.loads(
            th.suggest_motion_graphics_placements(brief="name plate overlays", beat_count="2")
        )

    overlay_pos = [b["position_seconds"] for b in data["beats"] if b["transparent"]]
    assert overlay_pos
    # At least one overlay should land closer to the wide window (~7s) than the face (~1s)
    assert any(abs(p - 7.0) < abs(p - 1.0) for p in overlay_pos)


def test_suggest_empty_timeline_spaced():
    fake_mod = MagicMock()
    fake_mod.enumerate_timeline_contexts.return_value = []
    sys.modules["classes.timeline_clip_context"] = fake_mod

    with _layers_patch() as app:
        app.return_value.project.get.return_value = [{"number": 1000000}]
        data = json.loads(
            th.suggest_motion_graphics_placements(brief="title package", beat_count="3")
        )

    assert len(data["beats"]) == 3
    assert data["clip_count"] == 0
    positions = [b["position_seconds"] for b in data["beats"]]
    assert positions == sorted(positions)
    assert all("block_query" in b and "title_hint" in b for b in data["beats"])
