"""add_clip_to_timeline watches short video / bounded windows before place."""

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


def _run_add(file_data, **kwargs):
    watch_calls = []

    def fake_watch(path, start, end, query, **kw):
        watch_calls.append((float(start), float(end), query))
        return {
            "cut_source": (float(start) + float(end)) / 2.0,
            "in_source": float(start) + 0.4,
            "out_source": float(end) - 0.4,
            "matched": True,
            "used_fallback": False,
            "warning": "",
        }

    file_obj = MagicMock()
    file_obj.id = "F1"
    file_obj.data = file_data

    query_mod = MagicMock()
    query_mod.File.get.return_value = file_obj
    query_mod.Track.get.return_value = MagicMock(data={"number": 1000000})
    query_mod.Clip.filter.return_value = []

    app = MagicMock()

    def project_get(key, default=None):
        if key == "layers":
            return [{"number": 1000000, "label": "1"}]
        if key == "fps":
            return {"num": 30, "den": 1}
        return default

    app.project.get.side_effect = project_get
    app.window.selected_tracks = []
    placed = {"id": "C1", "start": 0.0, "end": float(file_data.get("end") or file_data.get("duration") or 5)}
    app.window.timeline.addClip.return_value = placed

    with patch.object(tool_handlers, "_watch_confirm_cut", fake_watch):
        with patch.object(
            tool_handlers,
            "_lookup_watch_meta",
            return_value=(file_data.get("path"), file_data.get("duration"), []),
        ):
            with patch.object(tool_handlers, "_get_app", return_value=app):
                with patch.dict(sys.modules, {"classes.query": query_mod}):
                    out = tool_handlers.add_clip_to_timeline(
                        file_id="F1", position_seconds="0", **kwargs
                    )
    return out, watch_calls, placed


def test_add_clip_watches_short_ai_clip_without_query():
    out, calls, placed = _run_add(
        {
            "path": "/clips/gen.mp4",
            "name": "gen.mp4",
            "duration": 5.0,
            "start": 0.0,
            "end": 5.0,
            "has_video": True,
            "ai_metadata": {"prompt": "a cat waving"},
        }
    )
    assert not out.startswith("Error:"), out
    assert calls, out
    assert calls[0][2] == "a cat waving"
    assert "watched" in out
    assert abs(placed["start"] - 0.4) < 1e-9
    assert abs(placed["end"] - 4.6) < 1e-9


def test_add_clip_watches_bounded_stock_window_without_query():
    out, calls, _placed = _run_add(
        {
            "path": "/clips/stock.mp4",
            "name": "city_traffic.mp4",
            "duration": 90.0,
            "start": 0.0,
            "end": 90.0,
            "has_video": True,
        },
        duration_seconds="6",
    )
    assert calls, out
    assert "traffic" in calls[0][2]
    assert abs(calls[0][1] - calls[0][0] - 6.0) < 1e-6 or calls[0][1] - calls[0][0] >= 4.0


def test_add_clip_snaps_watched_window_off_mid_sentence():
    out, calls, placed = _run_add(
        {
            "path": "/clips/talk.mp4",
            "name": "talk.mp4",
            "duration": 5.0,
            "start": 0.0,
            "end": 5.0,
            "has_video": True,
            "ai_metadata": {
                "prompt": "a person talking",
                "transcript_cues": [{"start": 0.0, "end": 0.8}, {"start": 3.0, "end": 5.0}],
            },
        }
    )
    assert calls, out
    assert "watched" in out
    assert placed["start"] == 0.0
    assert placed["end"] == 5.0


def test_add_clip_skips_watch_on_audio():
    out, calls, _placed = _run_add(
        {
            "path": "/clips/bed.mp3",
            "name": "bed.mp3",
            "duration": 8.0,
            "start": 0.0,
            "end": 8.0,
            "media_type": "audio",
            "has_video": False,
            "has_audio": True,
        }
    )
    assert calls == []
    assert "watched" not in out


def test_add_clip_skips_watch_on_already_watched_subclip():
    out, calls, _placed = _run_add(
        {
            "path": "/clips/sub.mp4",
            "name": "handshake",
            "duration": 4.0,
            "start": 12.0,
            "end": 16.0,
            "zenvi_subclip": True,
            "has_video": True,
        }
    )
    assert calls == []
    assert "watched" not in out


def test_add_clip_skips_watch_on_long_untrimmed_file():
    out, calls, _placed = _run_add(
        {
            "path": "/clips/long.mp4",
            "name": "interview.mp4",
            "duration": 120.0,
            "start": 0.0,
            "end": 120.0,
            "has_video": True,
        }
    )
    assert calls == []
    assert "watched" not in out


# --- #167: a named keep window must never be what arms the unwatched guard ---

def test_dialogue_heavy_keep_window_places_without_watch():
    """>= 60% speech skips the watch on purpose - the window is still bounded."""
    out, calls, placed = _run_add(
        {
            "path": "/clips/interview.mp4",
            "name": "interview.mp4",
            "duration": 60.0,
            "start": 0.0,
            "end": 60.0,
            "has_video": True,
            "ai_metadata": {
                "transcript_cues": [
                    {"start": 15.0, "end": 17.5},
                    {"start": 17.6, "end": 20.0},
                ]
            },
        },
        start_seconds="15",
        end_seconds="20",
        query="guy with an iPad appears",
    )
    assert not out.startswith("Error:"), out
    assert calls == []
    assert abs(placed["start"] - 15.0) < 1.0
    assert abs(placed["end"] - 20.0) < 1.0
    assert placed["end"] > placed["start"]


def test_keep_window_with_explicit_times_in_query_places_without_watch():
    out, calls, placed = _run_add(
        {
            "path": "/clips/b_roll.mp4",
            "name": "b_roll.mp4",
            "duration": 60.0,
            "start": 0.0,
            "end": 60.0,
            "has_video": True,
        },
        start_seconds="15",
        end_seconds="20",
        query="the iPad shot from 15 seconds to 20 seconds",
    )
    assert not out.startswith("Error:"), out
    assert calls == []
    assert abs(placed["start"] - 15.0) < 1e-6
    assert abs(placed["end"] - 20.0) < 1e-6


def test_keep_window_longer_than_watch_limit_places_without_watch():
    out, calls, placed = _run_add(
        {
            "path": "/clips/long.mp4",
            "name": "long.mp4",
            "duration": 600.0,
            "start": 0.0,
            "end": 600.0,
            "has_video": True,
        },
        start_seconds="0",
        end_seconds="60",
    )
    assert not out.startswith("Error:"), out
    assert calls == []
    assert abs(placed["end"] - placed["start"] - 60.0) < 1e-6


def test_duration_trim_from_an_explicit_in_point_places_without_watch():
    out, calls, placed = _run_add(
        {
            "path": "/clips/long.mp4",
            "name": "long.mp4",
            "duration": 600.0,
            "start": 0.0,
            "end": 600.0,
            "has_video": True,
        },
        start_seconds="15",
        duration_seconds="5",
        query="the iPad shot from 15 seconds to 20 seconds",
    )
    assert not out.startswith("Error:"), out
    assert calls == []
    assert abs(placed["start"] - 15.0) < 1e-6
    assert abs(placed["end"] - 20.0) < 1e-6


def test_times_named_only_in_the_query_still_reject_a_first_n_seconds_trim():
    """The query text never moves the in-point - placing 0..5 here is wrong."""
    out, calls, _placed = _run_add(
        {
            "path": "/clips/long.mp4",
            "name": "long.mp4",
            "duration": 600.0,
            "start": 0.0,
            "end": 600.0,
            "has_video": True,
        },
        duration_seconds="5",
        query="the iPad shot from 15 seconds to 20 seconds",
    )
    assert out.startswith("Error:"), out
    assert calls == []


def test_blind_duration_trim_error_does_not_name_an_already_supplied_remedy():
    out, calls, _placed = _run_add(
        {
            "path": "/clips/long.mp4",
            "name": "long.mp4",
            "duration": 600.0,
            "start": 0.0,
            "end": 600.0,
            "has_video": True,
        },
        duration_seconds="60",
    )
    assert out.startswith("Error:"), out
    assert calls == []
    # The remedy it names must be something the caller did NOT already do.
    assert "end_seconds" in out


def test_ignored_end_seconds_does_not_exempt_a_first_n_seconds_trim():
    """duration wins over end_seconds, so end did not bound anything here."""
    out, calls, _placed = _run_add(
        {
            "path": "/clips/interview.mp4",
            "name": "interview.mp4",
            "duration": 600.0,
            "start": 0.0,
            "end": 600.0,
            "has_video": True,
            "ai_metadata": {"transcript_cues": [{"start": 0.0, "end": 60.0}]},
        },
        duration_seconds="5",
        end_seconds="20",
        query="a person speaking",
    )
    assert out.startswith("Error:"), out
    assert calls == []


def test_duration_alone_on_a_dialogue_heavy_window_still_errors():
    out, calls, _placed = _run_add(
        {
            "path": "/clips/interview.mp4",
            "name": "interview.mp4",
            "duration": 600.0,
            "start": 0.0,
            "end": 600.0,
            "has_video": True,
            "ai_metadata": {"transcript_cues": [{"start": 0.0, "end": 60.0}]},
        },
        duration_seconds="30",
        query="a person speaking",
    )
    assert out.startswith("Error:"), out
    assert calls == []


def test_end_seconds_alone_still_bounds_the_window():
    out, calls, placed = _run_add(
        {
            "path": "/clips/interview.mp4",
            "name": "interview.mp4",
            "duration": 600.0,
            "start": 0.0,
            "end": 600.0,
            "has_video": True,
            "ai_metadata": {"transcript_cues": [{"start": 0.0, "end": 60.0}]},
        },
        end_seconds="50",
        query="a person speaking",
    )
    assert not out.startswith("Error:"), out
    assert calls == []
    # Snapping may widen to the phrase edge, but it must stay a window - not
    # the whole 600s file.
    assert placed["start"] < placed["end"]
    assert placed["end"] - placed["start"] < 100.0
