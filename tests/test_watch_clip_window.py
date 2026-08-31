"""watch_clip_window: the agent-callable vision check.

Read-only by contract - it reports in/out/peak and must never mutate the
timeline, and must degrade rather than fail when the watch is unavailable.
"""

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

import pytest  # noqa: E402

from classes import tool_handlers  # noqa: E402

CLIP_START, CLIP_END = 100.0, 130.0


def _matched(**over):
    out = {
        "cut_source": 112.9, "in_source": 111.2, "out_source": 114.8,
        "matched": True, "used_fallback": False, "confidence": 0.82, "reason": "peak",
    }
    out.update(over)
    return out


def _run(watch_result=None, frames=(("t", "b64"),), **kwargs):
    """Call watch_clip_window with the clip lookup and backend patched."""
    clip = MagicMock()
    clip.id = "C3"
    clip.data = {"id": "C3", "file_id": "F1", "start": CLIP_START, "end": CLIP_END,
                 "title": "Clip C3", "position": 0.0, "layer": 1}
    file_obj = MagicMock()
    file_obj.data = {"id": "F1", "path": "/m/broll.mp4", "duration": 300.0}

    resolved = MagicMock(ok=True, clip=clip, error=None)
    client = MagicMock()
    client.watch_window.return_value = watch_result if watch_result is not None else _matched()

    with patch.object(tool_handlers, "_resolve_timeline_clip_for_tool", return_value=resolved), \
            patch.object(tool_handlers, "_get_source_file_for_clip", return_value=file_obj), \
            patch.dict(sys.modules, {
                "classes.api_client": MagicMock(get_backend_client=lambda: client),
                "classes.watch_frames": MagicMock(
                    WATCH_MIN_WINDOW_SEC=8.0,
                    extract_watch_frames=lambda *a, **k: (
                        [{"timestamp": 1.0, "image_base64": "x"}] if frames else [], ""
                    )
                ),
            }):
        return tool_handlers.watch_clip_window(**kwargs), client


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_a_match_reports_clip_relative_in_out_and_cut():
    out, _c = _run(query="the jump", start="10", end="20")
    assert "in=11.20" in out
    assert "out=14.80" in out
    assert "cut=12.90" in out
    assert "matched" in out


def test_the_result_says_the_times_are_clip_relative():
    out, _c = _run(query="the jump", start="10", end="20")
    assert "clip-relative" in out


def test_the_window_sent_to_the_backend_is_source_absolute():
    """start/end are clip-relative in, but the media file needs absolute."""
    _out, client = _run(query="q", start="10", end="20")
    kwargs = client.watch_window.call_args.kwargs
    assert kwargs["window_start"] == pytest.approx(CLIP_START + 10.0)
    assert kwargs["window_end"] == pytest.approx(CLIP_START + 20.0)


def test_blank_bounds_watch_the_whole_clip_window():
    _out, client = _run(query="q")
    kwargs = client.watch_window.call_args.kwargs
    assert kwargs["window_start"] == pytest.approx(CLIP_START)
    assert kwargs["window_end"] == pytest.approx(CLIP_END)


def test_bounds_past_the_clip_are_clamped_not_rejected():
    _out, client = _run(query="q", start="0", end="9999")
    assert client.watch_window.call_args.kwargs["window_end"] == pytest.approx(CLIP_END)


def test_an_inverted_window_falls_back_to_the_whole_clip():
    _out, client = _run(query="q", start="20", end="10")
    kwargs = client.watch_window.call_args.kwargs
    assert kwargs["window_start"] == pytest.approx(CLIP_START)
    assert kwargs["window_end"] == pytest.approx(CLIP_END)


# --------------------------------------------------------------------------
# Seconds parsing - never a raw float crash
# --------------------------------------------------------------------------

def test_a_timecode_bound_is_accepted():
    """The clip is 30s long, so 0:05-0:20 is a real window inside it."""
    _out, client = _run(query="q", start="0:05", end="0:20")
    kwargs = client.watch_window.call_args.kwargs
    assert kwargs["window_start"] == pytest.approx(CLIP_START + 5.0)
    assert kwargs["window_end"] == pytest.approx(CLIP_START + 20.0)


def test_a_bound_past_the_clip_end_clamps_to_the_whole_window():
    """1:02 is beyond a 30s clip - clamp, do not invent a window past the end."""
    _out, client = _run(query="q", start="1:02", end="1:10")
    kwargs = client.watch_window.call_args.kwargs
    assert kwargs["window_start"] == pytest.approx(CLIP_START)
    assert kwargs["window_end"] == pytest.approx(CLIP_END)


@pytest.mark.parametrize("bad", ["soon", "end of clip", "auto"])
def test_a_non_time_bound_names_itself(bad):
    out, client = _run(query="q", start=bad, end="20")
    assert out.startswith("Error:")
    assert "could not convert string to float" not in out
    assert client.watch_window.call_count == 0


# --------------------------------------------------------------------------
# Degradation - a watch is a refinement, never a gate
# --------------------------------------------------------------------------

def test_no_match_is_reported_plainly_not_as_an_error():
    out, _c = _run(watch_result={"matched": False, "used_fallback": True,
                                 "reason": "not visible", "cut_source": 105.0,
                                 "in_source": 100.0, "out_source": 130.0},
                   query="unicorn")
    assert not out.startswith("Error")
    assert "no match" in out
    assert "not visible" in out


def test_a_backend_error_degrades_to_no_match():
    out, _c = _run(watch_result={"error": "500 Server Error"}, query="q")
    assert not out.startswith("Error")
    assert "no match" in out


def test_no_frames_extracted_degrades_to_no_match():
    out, client = _run(frames=(), query="q")
    assert not out.startswith("Error")
    assert "no match" in out
    assert client.watch_window.call_count == 0, "must not upload an empty frame set"


def test_an_unresolvable_clip_returns_the_resolver_error():
    resolved = MagicMock(ok=False, clip=None, error="Error: no such clip")
    with patch.object(tool_handlers, "_resolve_timeline_clip_for_tool", return_value=resolved):
        out = tool_handlers.watch_clip_window(query="q", timeline_clip_id="nope")
    assert out == "Error: no such clip"


def test_a_clip_with_no_media_path_is_reported():
    clip = MagicMock(id="C3")
    clip.data = {"id": "C3", "start": 0.0, "end": 10.0}
    file_obj = MagicMock()
    file_obj.data = {"id": "F1", "path": ""}
    resolved = MagicMock(ok=True, clip=clip, error=None)
    with patch.object(tool_handlers, "_resolve_timeline_clip_for_tool", return_value=resolved), \
            patch.object(tool_handlers, "_get_source_file_for_clip", return_value=file_obj):
        out = tool_handlers.watch_clip_window(query="q")
    assert out.startswith("Error:")
    assert "path" in out


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def test_the_tool_is_registered_and_labelled():
    """Both maps, or the assert at import time would already have failed."""
    assert tool_handlers.AGENT_TOOL_HANDLERS["watch_clip_window_tool"] is \
        tool_handlers.watch_clip_window
    assert "watch_clip_window_tool" in tool_handlers.TOOL_DISPLAY_LABELS


def test_the_tool_is_reachable_through_execute_tool():
    """It previously returned 'Unknown tool' - that is the bug being fixed."""
    assert "watch_clip_window_tool" in tool_handlers.TOOL_HANDLERS
