"""slice_clip_at_best_match and search_clip_scenes must watch before cut/report."""

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


def test_search_clip_scenes_watch_before_report():
    resolved = MagicMock(ok=True, clip=MagicMock())
    resolved.clip.data = {"file_id": "sub-1", "start": 10.0, "end": 40.0, "title": "Sub"}
    watch_calls = []

    def fake_watch(path, start, end, query, **kw):
        watch_calls.append((start, end, query))
        return {
            "cut_source": 14.2,
            "matched": True,
            "used_fallback": False,
            "warning": "",
            "window_start": start,
            "window_end": end,
        }

    parent_ai = {
        "twelvelabs": {"status": "ready", "index_id": "idx", "video_id": "vid"},
        "scene_descriptions": [],
        "transcript_cues": [{"start": 13.0, "end": 14.0, "text": "hi"}],
    }
    sub_file = MagicMock(data={
        "zenvi_subclip": True, "parent_file_id": "root-1", "path": "/v.mp4", "duration": 120,
    })
    mock_backend = MagicMock()
    mock_backend.is_indexing_configured.return_value = True
    hit = {"start": 12.0, "end": 18.0, "rank": 1, "cut_source": 12.0, "overlap_ratio": 1.0}

    ctx = MagicMock()
    ctx.source_start = 10.0
    ctx.source_end = 40.0
    ctx.title = "Sub"
    ctx.file_id = "sub-1"
    ctx.source_path = "/v.mp4"

    with patch.object(tool_handlers, "_resolve_timeline_clip_for_tool", lambda **_kw: resolved):
        with patch.object(tool_handlers, "_tl_search_items_in_window", lambda *a, **k: ([hit], None)):
            with patch.object(tool_handlers, "_get_source_file_for_clip", return_value=sub_file):
                with patch.object(tool_handlers, "_watch_confirm_cut", fake_watch):
                    with patch("classes.api_client.get_backend_client", lambda: mock_backend):
                        with patch(
                            "classes.timeline_clip_context.build_timeline_clip_context",
                            return_value=ctx,
                        ):
                            with patch(
                                "classes.timeline_clip_context.resolve_parent_file_data",
                                return_value={"ai_metadata": parent_ai, "path": "/v.mp4", "duration": 120},
                            ):
                                with patch(
                                    "classes.twelvelabs_match.select_hits_for_display",
                                    return_value=[hit],
                                ):
                                    out = tool_handlers.search_clip_scenes(
                                        query="tennis",
                                        clip_query="sub clip",
                                    )
    assert watch_calls
    assert watch_calls[0][0] == 12.0
    assert "keep" in out.lower()


def test_slice_clip_at_best_match_calls_watch_before_slice():
    watch_calls = []
    sliced = []

    def fake_watch(path, start, end, query, **kw):
        watch_calls.append((float(start), float(end), query))
        return {"cut_source": 13.4, "matched": True, "used_fallback": False, "warning": ""}

    resolved = MagicMock(ok=True, clip=MagicMock())
    resolved.clip.id = "clip-1"
    resolved.clip.data = {
        "file_id": "file-1", "position": 0.0, "start": 10.0, "end": 40.0, "layer": 1,
    }
    sf = MagicMock()
    sf.data = {
        "path": "/v.mp4",
        "duration": 120,
        "ai_metadata": {
            "index": {"status": "ready", "index_id": "idx", "video_id": "vid"},
        },
    }
    chosen = {
        "start": 12.0, "end": 18.0, "cut_source": 12.0,
        "rank": 1, "overlap_ratio": 1.0,
    }

    def run_main(fn):
        name = getattr(fn, "__name__", "")
        if name == "_do_slice":
            sliced.append(True)
            return None
        return fn() if callable(fn) else None

    app = MagicMock()
    app.project.get.return_value = {"num": 30, "den": 1}
    app.window.timeline.Slice_Triggered = MagicMock()

    with patch.object(tool_handlers, "_watch_confirm_cut", fake_watch):
        with patch.object(tool_handlers, "_lookup_watch_meta", return_value=("/v.mp4", 120.0, [])):
            with patch.object(tool_handlers, "_run_on_main_thread", side_effect=run_main):
                with patch.object(tool_handlers, "_get_app", return_value=app):
                    with patch.object(tool_handlers, "_get_source_file_for_clip", return_value=sf):
                        with patch("classes.clip_resolver.resolve_timeline_clip", return_value=resolved):
                            with patch("classes.ai_metadata_utils.get_source_window", return_value=(10.0, 40.0)):
                                with patch(
                                    "classes.timeline_clip_context.resolve_parent_file_data",
                                    return_value=sf.data,
                                ):
                                    with patch("classes.api_client.get_backend_client") as mock_c:
                                        mock_c.return_value.is_indexing_configured.return_value = True
                                        with patch.object(
                                            tool_handlers, "_twelvelabs_search_in_window",
                                            return_value=([chosen], None),
                                        ):
                                            with patch(
                                                "classes.twelvelabs_match.select_twelvelabs_match",
                                                return_value=chosen,
                                            ):
                                                with patch(
                                                    "classes.twelvelabs_match.snap_source_time_to_frame",
                                                    lambda t, *a, **k: t,
                                                ):
                                                    with patch(
                                                        "classes.twelvelabs_match.snap_timeline_position",
                                                        lambda t, *a, **k: t,
                                                    ):
                                                        out = tool_handlers.slice_clip_at_best_match(
                                                            query="jump",
                                                            clip_query="sub",
                                                        )
    assert watch_calls, out
    assert watch_calls[0][0] == 12.0
    assert watch_calls[0][1] == 18.0
    assert sliced
    assert "Sliced" in out


def test_slice_explicit_time_skips_watch():
    watch_calls = []

    def fake_watch(*a, **k):
        watch_calls.append(1)
        return {"cut_source": 0}

    resolved = MagicMock(ok=True, clip=MagicMock())
    resolved.clip.id = "clip-1"
    resolved.clip.data = {
        "file_id": "file-1", "position": 0.0, "start": 0.0, "end": 30.0, "layer": 1,
    }
    sf = MagicMock()
    sf.data = {"path": "/v.mp4", "duration": 30}

    def run_main(fn):
        name = getattr(fn, "__name__", "")
        if name == "_do_time_slice":
            return "Sliced at 0:04 and 0:10 (source). Three segments: before, selected range, after."
        return fn() if callable(fn) else None

    with patch.object(tool_handlers, "_watch_confirm_cut", fake_watch):
        with patch.object(tool_handlers, "_run_on_main_thread", side_effect=run_main):
            with patch.object(tool_handlers, "_get_source_file_for_clip", return_value=sf):
                with patch("classes.clip_resolver.resolve_timeline_clip", return_value=resolved):
                    with patch("classes.ai_metadata_utils.get_source_window", return_value=(0.0, 30.0)):
                        with patch(
                            "classes.timeline_clip_context.resolve_parent_file_data",
                            return_value=sf.data,
                        ):
                            out = tool_handlers.slice_clip_at_best_match(
                                query="from 4 seconds to 10 seconds",
                                clip_query="sub",
                            )
    assert watch_calls == []
    assert "Sliced" in out


def test_watch_clip_window_in_handlers():
    assert "watch_clip_window_tool" in tool_handlers.AGENT_TOOL_HANDLERS
    assert "watch_clip_window_tool" in tool_handlers.BACKGROUND_SAFE_TOOLS
    assert "slice_clip_at_best_match_tool" in tool_handlers.BACKGROUND_SAFE_TOOLS
    assert "split_file_add_clip_tool" in tool_handlers.BACKGROUND_SAFE_TOOLS
    assert "add_clip_to_timeline_tool" in tool_handlers.BACKGROUND_SAFE_TOOLS


def test_split_file_add_clip_watches_before_save():
    watch_calls = []
    saved = []

    def fake_watch(path, start, end, query, **kw):
        watch_calls.append((float(start), float(end), query))
        return {
            "cut_source": 14.0,
            "in_source": 12.5,
            "out_source": 16.0,
            "matched": True,
            "used_fallback": False,
            "warning": "",
            "window_start": start,
            "window_end": end,
        }

    parent = MagicMock()
    parent.data = {
        "fps": {"num": 30, "den": 1},
        "video_length": 900,
        "start": 0.0,
        "end": 30.0,
        "duration": 30.0,
        "path": "/v.mp4",
        "name": "v.mp4",
    }

    class FakeFile:
        def __init__(self):
            self.data = {}
            self.id = None
            self.key = None
            self.type = None

        def save(self):
            self.id = "new-sub"
            saved.append(dict(self.data))

        @staticmethod
        def get(id=""):
            return parent

    with patch.object(tool_handlers, "_watch_confirm_cut", fake_watch):
        with patch.object(tool_handlers, "_lookup_watch_meta", return_value=("/v.mp4", 30.0, [])):
            query_mod = MagicMock()
            query_mod.File = FakeFile
            with patch.dict(sys.modules, {"classes.query": query_mod}):
                out = tool_handlers.split_file_add_clip(
                    file_id="parent",
                    start_seconds="12",
                    end_seconds="18",
                    query="handshake",
                    name="handshake",
                )
    assert watch_calls, out
    assert watch_calls[0][0] == 12.0
    assert watch_calls[0][1] == 18.0
    assert saved
    assert saved[0]["start"] == 12.5
    assert saved[0]["end"] == 16.0
    assert "new-sub" in out
    assert "duration_seconds" in out


def test_split_explicit_time_skips_watch():
    watch_calls = []
    saved = []

    def fake_watch(*a, **k):
        watch_calls.append(1)
        return {"in_source": 0, "out_source": 1, "cut_source": 0, "used_fallback": True}

    parent = MagicMock()
    parent.data = {
        "fps": {"num": 30, "den": 1},
        "start": 0.0,
        "end": 30.0,
        "duration": 30.0,
        "path": "/v.mp4",
        "name": "v.mp4",
    }

    class FakeFile:
        def __init__(self):
            self.data = {}
            self.id = None
            self.key = None
            self.type = None

        def save(self):
            self.id = "new-sub"
            saved.append(dict(self.data))

        @staticmethod
        def get(id=""):
            return parent

    with patch.object(tool_handlers, "_watch_confirm_cut", fake_watch):
        query_mod = MagicMock()
        query_mod.File = FakeFile
        with patch.dict(sys.modules, {"classes.query": query_mod}):
            out = tool_handlers.split_file_add_clip(
                file_id="parent",
                start_seconds="4",
                end_seconds="10",
                query="from 4 seconds to 10 seconds",
                name="explicit",
            )
    assert watch_calls == []
    assert saved
    assert saved[0]["start"] == 4.0
    assert saved[0]["end"] == 10.0
    assert "new-sub" in out


def test_split_wide_window_fallback_is_refused():
    saved = []

    def fake_watch(*a, **k):
        return {
            "cut_source": 0.0,
            "in_source": 0.0,
            "out_source": 40.0,
            "matched": False,
            "used_fallback": True,
            "warning": "",
            "window_start": 0.0,
            "window_end": 40.0,
        }

    parent = MagicMock()
    parent.data = {
        "fps": {"num": 30, "den": 1},
        "start": 0.0,
        "end": 60.0,
        "duration": 60.0,
        "path": "/v.mp4",
        "name": "v.mp4",
    }

    class FakeFile:
        def __init__(self):
            self.data = {}
            self.id = None
            self.key = None
            self.type = None

        def save(self):
            self.id = "new-sub"
            saved.append(dict(self.data))

        @staticmethod
        def get(id=""):
            return parent

    with patch.object(tool_handlers, "_watch_confirm_cut", fake_watch):
        with patch.object(tool_handlers, "_lookup_watch_meta", return_value=("/v.mp4", 60.0, [])):
            query_mod = MagicMock()
            query_mod.File = FakeFile
            with patch.dict(sys.modules, {"classes.query": query_mod}):
                out = tool_handlers.split_file_add_clip(
                    file_id="parent",
                    start_seconds="0",
                    end_seconds="40",
                    query="the moment",
                    name="wide",
                )
    assert out.startswith("Error:")
    assert "visual match" in out.lower()
    assert saved == []

