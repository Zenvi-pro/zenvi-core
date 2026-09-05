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
import classes.tool_handlers as th  # noqa: E402


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


def _run_generate(place_result="Added clip to track 1 at 3.0s"):
    """generate_video_and_add_to_timeline with generation + download mocked out."""
    fake_file = SimpleNamespace(
        id="F42", data={"duration": 5.0}, save=MagicMock(),
        absolute_path=lambda: "/tmp/gen.mp4",
    )
    calls = {}

    def _add_clip(**kwargs):
        calls["add_clip"] = kwargs
        return place_result

    client = MagicMock()
    client.generate_video.return_value = {"video_url": "https://x/gen.mp4"}

    fake_query = MagicMock()
    fake_query.Clip.filter.return_value = []

    with patch.object(th, "_get_app") as app, \
            patch.object(th, "QThread", MagicMock()), \
            patch.object(th, "QEventLoop", MagicMock()), \
            patch.object(th, "_run_on_main_thread", side_effect=lambda fn, **kw: fn()), \
            patch.object(th, "_atomic", side_effect=lambda app, fn, tid=None: fn), \
            patch.object(th, "_new_transaction_id", return_value="T1"), \
            patch.object(th, "normalize_track_or_layer_arg", return_value=(1000000, None)), \
            patch.object(th, "_pause_auto_save", return_value=False), \
            patch.object(th, "_resume_auto_save"), \
            patch.object(th, "_pause_player", return_value=False), \
            patch.object(th, "_resume_player"), \
            patch.object(th, "_canonical_media_path", side_effect=lambda p: p), \
            patch.object(th, "_output_path_for_generated_video", return_value="/tmp/gen.mp4"), \
            patch.object(th, "_download_video_url_to_path", return_value=None), \
            patch.object(th, "_import_generated_video", return_value=(fake_file, None)), \
            patch.object(th, "add_clip_to_timeline", side_effect=_add_clip), \
            patch.dict(
                "sys.modules",
                {
                    "classes.credits_client": MagicMock(
                        check_operation=MagicMock(return_value=(None, None, None)),
                        charge_operation_on_success=MagicMock(),
                        credits=MagicMock(),
                    ),
                    "classes.api_client": MagicMock(
                        get_backend_client=MagicMock(return_value=client)
                    ),
                    "classes.query": fake_query,
                },
            ):
        app.return_value.window = MagicMock()
        msg = th.generate_video_and_add_to_timeline(
            prompt="a paper plane over a neon city",
            duration_seconds="5",
            position_seconds="3",
            track="1",
        )
    return msg, fake_file, calls


def test_generated_clip_is_placed_on_the_timeline_not_left_in_the_bin():
    msg, fake_file, calls = _run_generate()
    assert not msg.lower().startswith("error"), msg
    assert calls["add_clip"]["file_id"] == "F42"
    assert calls["add_clip"]["track"] == "1"
    assert calls["add_clip"]["position_seconds"] == "3"
    # and the clip carries the prompt as its summary without waiting on Gemini
    assert "paper plane" in fake_file.data["ai_metadata"]["short_summary"]
    assert fake_file.data["ai_metadata"]["analyzed"] is True


def test_place_failure_reports_the_file_id_and_says_do_not_regenerate():
    msg, _, _ = _run_generate(place_result="Error: no such track")
    assert msg.startswith("Error:")
    assert "file_id=F42" in msg
    assert "Do NOT regenerate" in msg
    assert "add_clip_to_timeline_tool" in msg
