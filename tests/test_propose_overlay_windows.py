"""Unit tests for propose_overlay_windows (no Qt)."""

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


def test_propose_overlay_windows_no_copy_fields():
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
        out = th.propose_overlay_windows(beat_count="4")

    data = json.loads(out)
    assert "windows" in data
    assert len(data["windows"]) == 4
    assert "title_hint" not in data
    assert "block_query" not in data
    assert "beats" not in data
    for w in data["windows"]:
        assert "t" in w
        assert "track_hint" in w
        assert "layout_region" in w
        assert w["layout_region"] in (
            "lower_third",
            "corner_br",
            "corner_tr",
            "full_frame",
            "mid_plate",
        )
        assert "place_mode" in w
        assert w["place_mode"] in ("overlay", "gap", "cut_in")
        assert "suggest_transparent" in w
        assert "title_hint" not in w
        assert "block_query" not in w
    assert (
        "beats_json" in data["guidance"]
        or "propose_overlay" in data["guidance"].lower()
        or "invent" in data["guidance"].lower()
        or "place_motion_graphic" in data["guidance"].lower()
        or "layout_region" in data["guidance"].lower()
    )


def test_suggest_motion_graphics_placements_deprecated():
    out = th.suggest_motion_graphics_placements(brief="trailer", beat_count="4")
    assert out.startswith("Error:")
    assert "propose_overlay_windows" in out


def test_fetch_in_background_safe():
    assert "fetch_motion_graphics_video_tool" in th.BACKGROUND_SAFE_TOOLS
    assert "propose_overlay_windows_tool" in th.BACKGROUND_SAFE_TOOLS
    assert "propose_overlay_windows_tool" in th.READ_ONLY_TOOLS


def test_propose_continuous_footage_no_late_dump():
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
        data = json.loads(th.propose_overlay_windows(beat_count="4"))

    end = data["timeline_end"]
    assert end == 16.0
    for w in data["windows"]:
        assert w["t"] <= end + 0.01
        assert w["t"] >= 0.0
    positions = [w["t"] for w in data["windows"]]
    assert min(positions) <= end * 0.35
    assert max(positions) >= end * 0.6


def test_propose_prefers_wide_over_face_window():
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
        data = json.loads(th.propose_overlay_windows(beat_count="2"))

    positions = [w["t"] for w in data["windows"]]
    assert positions
    assert any(abs(p - 7.0) < abs(p - 1.0) for p in positions)


def test_propose_empty_timeline_spaced():
    fake_mod = MagicMock()
    fake_mod.enumerate_timeline_contexts.return_value = []
    sys.modules["classes.timeline_clip_context"] = fake_mod

    with _layers_patch() as app:
        app.return_value.project.get.return_value = [{"number": 1000000}]
        data = json.loads(th.propose_overlay_windows(beat_count="3"))

    assert len(data["windows"]) == 3
    assert data["clip_count"] == 0
    positions = [w["t"] for w in data["windows"]]
    assert positions == sorted(positions)
