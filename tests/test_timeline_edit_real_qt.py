"""The timeline-edit tools driving the REAL Clip menu handlers (real Qt + libopenshot).

Run with ``ZENVI_REAL_QT=1`` and libopenshot 1.0 on PYTHONPATH, e.g.::

    ZENVI_REAL_QT=1 QT_QPA_PLATFORM=offscreen PYTHONPATH=~/zenvi-deps-1.0/python \\
        .venv/bin/python -m pytest tests/test_timeline_edit_real_qt.py -q -p no:cacheprovider

The headless suite covers the tools against FakeTimeline; this proves the real
Slice/Time/Repeat/Split-audio handlers join the tool's single undo step and make
the edits the receipts describe.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")
openshot = pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QThread  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

T1, T2, T3 = 1000000, 2000000, 3000000


class _TestApp(QApplication):
    def _tr(self, text):
        return text


@pytest.fixture(scope="module")
def timeline_module():
    importlib.import_module("qt_api")
    app = QApplication.instance()
    if app is None:
        app = _TestApp([])
    elif not hasattr(app, "_tr"):
        app._tr = lambda text: text
    return importlib.import_module("windows.views.timeline")


def _real_timeline(tm, editor):
    """An object carrying the real TimelineView handlers, minus the widget."""
    view = tm.TimelineView
    names = ("Slice_Triggered", "Time_Triggered", "Repeat_Triggered", "Split_Audio_Triggered",
             "Hide_Waveform_Triggered", "update_clip_data", "update_transition_data", "ripple_delete_gap",
             "_assign_new_effect_ids", "AddPoint", "get_uuid", "delete_invalid_timeline_item",
             "_apply_effect_colors", "_find_missing_transition_details")
    ns = {n: getattr(view, n) for n in names}
    ns.update({
        "_apply_clip_override_fields": lambda self, data, _cid: data,
        "Show_Waveform_Triggered": lambda self, ids, transaction_id=None: self.waveforms.append((ids, transaction_id)),
        "_extend_timeline_to_fit_items": lambda self: None,
    })
    helper = type("RealTimelineHandlers", (), ns)()
    helper.window = MagicMock(name="window")
    helper.window.timeline_sync.timeline.GetClip.return_value = None
    helper.redraw_audio_timer = MagicMock()
    helper.show_wait_spinner = False
    helper.waveforms = []
    return helper


@pytest.fixture
def ed(timeline_module):
    from classes import clip_utils
    from editor_tools_harness import make_editor
    from windows.views import repeat, retime

    editor = make_editor()
    editor.app.thread.return_value = QThread.currentThread()
    editor.app._tr = lambda text: text

    def create_track_below(layer):
        numbers = sorted(t["number"] for t in editor.get("layers"))
        below = [n for n in numbers if n < int(layer)]
        return below[-1] if below else editor.add_track(max(1, int(layer) // 2))

    editor.window.create_track_below = create_track_below
    patches = [patch.object(mod, "get_app", return_value=editor.app)
               for mod in (timeline_module, retime, repeat, clip_utils)]
    for p in patches:
        p.start()
    editor.window.timeline = _real_timeline(timeline_module, editor)
    try:
        yield editor
    finally:
        for p in patches:
            p.stop()
        editor.stop()


def receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.partition("\n")[2] or "{}")


def _clip(ed, duration=10.0, position=0.0, layer=T2, **file_overrides):
    f = ed.add_file("video", duration=20.0, **file_overrides)
    return ed.add_clip(f, position=position, end=duration, layer=layer)


def test_slice_tool_runs_the_real_slice_as_one_undo_step(ed):
    a = _clip(ed)
    r = receipt(ed.call("slice_clips_tool", timeline_clip_ids=[a], at_seconds=4.0))
    right = r["pieces"][0]["right"]
    assert ed.clip(a)["end"] == pytest.approx(4.0) and ed.clip(right)["start"] == pytest.approx(4.0)
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert ed.clip(right) is None and ed.clip(a)["end"] == pytest.approx(10.0)


def test_double_slice_inside_one_transaction_is_one_undo_step(ed):
    """Regression: Slice_Triggered minted and cleared its own id, so the explicit
    range of slice_clip_at_best_match_tool ("4 s to 7 s") was two undo steps."""
    from classes import tool_handlers
    a = _clip(ed)
    data = ed.clip(a)
    with tool_handlers._transaction(ed.app):
        out = tool_handlers._slice_timeline_clip_at_source_times(
            a, data["start"], data["end"], data["position"], T2, data["file_id"], 4.0, 7.0)
    assert not out.startswith("Error"), out
    assert len(ed.clips()) == 3
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert len(ed.clips()) == 1


def test_real_speed_reverse_and_undo(ed):
    a = _clip(ed, duration=6.0)
    r = receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=2))
    assert ed.clip(a)["end"] - ed.clip(a)["start"] == pytest.approx(3.0)
    assert r["clips"][0]["effective_speed"] == pytest.approx(2.0, abs=0.01)
    assert ed.undo_steps_since_mark() == 1
    ed.mark()
    r = receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], reverse=True))
    assert r["clips"][0]["direction"] == "backward"
    ed.undo()
    ed.undo()
    assert ed.clip(a)["end"] == pytest.approx(6.0)


def test_real_freeze_on_last_frame(ed):
    a = _clip(ed, duration=6.0)
    receipt(ed.call("freeze_frame_tool", timeline_clip_id=a, frame="last", hold_seconds=2))
    data = ed.clip(a)
    assert data["end"] - data["start"] == pytest.approx(8.0)
    ys = [p["co"]["Y"] for p in sorted(data["time"]["Points"], key=lambda p: p["co"]["X"])]
    assert len(ys) >= 3 and ys[-2] == ys[-3]  # a flat hold before the end
    assert ed.undo_steps_since_mark() == 1


def test_real_repeat_undo_then_repeat_again(ed):
    a = _clip(ed, duration=2.0)
    receipt(ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=3))
    assert ed.clip(a)["end"] - ed.clip(a)["start"] == pytest.approx(6.0, abs=0.05)
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert ed.clip(a)["end"] - ed.clip(a)["start"] == pytest.approx(2.0)
    ed.mark()
    receipt(ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=2))
    assert ed.clip(a)["end"] - ed.clip(a)["start"] == pytest.approx(4.0, abs=0.05)
    ed.mark()
    receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], reset=True))
    assert ed.clip(a)["repeat_cache"] == {}
    assert ed.undo_steps_since_mark() == 1


def test_real_separate_audio(ed):
    a = _clip(ed, duration=6.0, has_audio=True, channels=2)
    r = receipt(ed.call("separate_clip_audio_tool", timeline_clip_ids=[a]))
    new = r["audio_clips"][0]
    assert new["track"] == 1 and new["video"] == "off"
    assert ed.clip(a)["has_audio"]["Points"][0]["co"]["Y"] == 0.0
    assert ed.undo_steps_since_mark() == 1
    ed.undo()
    assert len(ed.clips()) == 1
