"""MCP harness tools (#150): headless import, main-thread liveness, assistant turn.

A scheduled routine drives Zenvi only over the in-app MCP server -- no clicks, no
file picker, no chat dock.  These tests pin the behaviour that makes such a run
able to finish: importing by path without a dialog, a typed timeout when the Qt
main thread is wedged, and one assistant turn driven over MCP.
"""

import os
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub Qt before importing tool_handlers (headless CI).
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

from classes import agent_api_proxy  # noqa: E402
from classes import tool_handlers  # noqa: E402


# --- helpers ---------------------------------------------------------------

def _install_fake_query(by_path, monkeypatch):
    """Avoid importing real classes.query (pulls PyQt). Maps path -> file object.

    Installed via monkeypatch so the real module is restored afterwards -- a
    leaked stub breaks unrelated tests later in the session.
    """
    mod = types.ModuleType("classes.query")

    class File:
        @staticmethod
        def get(**kwargs):
            return by_path.get(kwargs.get("path"))

        @staticmethod
        def filter(**kwargs):
            return list(by_path.values())

    mod.File = File
    monkeypatch.setitem(sys.modules, "classes.query", mod)
    return mod


def _fake_file(file_id, path, analyzed=False):
    f = MagicMock()
    f.id = file_id
    f.data = {"id": file_id, "path": path,
              "ai_metadata": {"analyzed": analyzed}}
    return f


def _mock_app():
    """An app whose import dialog is a spy we assert is never triggered."""
    app = MagicMock()
    app.window.files_model.add_files = MagicMock()
    app.window.actionImportFiles_trigger = MagicMock()
    return app


def _media(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\x00" * 16)
    return str(p)


# --- 1. headless import ----------------------------------------------------

def test_import_files_by_path_never_opens_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_handlers, "QThread", None, raising=False)
    clip = _media(tmp_path, "a.mp4")
    _install_fake_query({clip: _fake_file("f1", clip)}, monkeypatch)
    app = _mock_app()

    with patch.object(tool_handlers, "_get_app", return_value=app):
        out = tool_handlers.import_files(paths=[clip])

    app.window.actionImportFiles_trigger.assert_not_called()
    app.window.files_model.add_files.assert_called_once()
    args, kwargs = app.window.files_model.add_files.call_args
    assert list(args[0]) == [clip]
    assert kwargs["quiet"] is True
    assert "f1" in out and clip in out


def test_import_files_without_paths_errors_and_opens_no_dialog(monkeypatch):
    monkeypatch.setattr(tool_handlers, "QThread", None, raising=False)
    app = _mock_app()

    with patch.object(tool_handlers, "_get_app", return_value=app):
        out = tool_handlers.import_files()

    assert out.startswith("Error:")
    assert "paths is required" in out
    app.window.actionImportFiles_trigger.assert_not_called()
    app.window.files_model.add_files.assert_not_called()


def test_import_files_recurses_a_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_handlers, "QThread", None, raising=False)
    a = _media(tmp_path, "a.mp4")
    b = _media(tmp_path, "b.mov")
    _media(tmp_path, "notes.txt")          # filtered out: not a media extension
    _install_fake_query({a: _fake_file("f1", a), b: _fake_file("f2", b)}, monkeypatch)
    app = _mock_app()

    with patch.object(tool_handlers, "_get_app", return_value=app):
        out = tool_handlers.import_files(paths=str(tmp_path))

    app.window.actionImportFiles_trigger.assert_not_called()
    sent = list(app.window.files_model.add_files.call_args[0][0])
    assert sorted(sent) == sorted([a, b])
    assert "f1" in out and "f2" in out


# --- 2. main-thread liveness ----------------------------------------------

class _DeadThread:
    """Stands in for QThread when the GUI thread never drains its event loop."""

    @staticmethod
    def currentThread():
        return "worker"


def _wedged_dispatcher():
    """A dispatcher whose emit is swallowed, so `done` is never set."""
    d = MagicMock()
    d._dispatch.emit = MagicMock()
    return d


def test_run_on_main_thread_raises_typed_timeout(monkeypatch):
    monkeypatch.setattr(tool_handlers, "QThread", _DeadThread, raising=False)
    app = MagicMock()
    app.thread.return_value = "gui"

    with patch.object(tool_handlers, "_get_app", return_value=app):
        with patch.object(tool_handlers, "_get_dispatcher", _wedged_dispatcher):
            with pytest.raises(tool_handlers.MainThreadTimeout) as exc:
                tool_handlers._run_on_main_thread(lambda: "never", timeout=0.2)

    assert "MAIN_THREAD_TIMEOUT" in str(exc.value)


def test_execute_tool_reports_main_thread_timeout_without_hanging(monkeypatch):
    monkeypatch.setattr(tool_handlers, "QThread", _DeadThread, raising=False)
    monkeypatch.setattr(tool_handlers, "MAIN_THREAD_TIMEOUT_SECONDS", 0.2,
                        raising=False)
    app = MagicMock()
    app.thread.return_value = "gui"

    with patch.object(tool_handlers, "_get_app", return_value=app):
        with patch.object(tool_handlers, "_get_dispatcher", _wedged_dispatcher):
            out = tool_handlers.execute_tool("add_track_tool", {})

    assert out.startswith("Error:")
    assert "MAIN_THREAD_TIMEOUT" in out


# --- 3. assistant turn over MCP -------------------------------------------

def _install_fake_api_client(send_impl, cancel_spy, monkeypatch):
    mod = types.ModuleType("classes.api_client")

    class ZenviBackendClient:
        def __init__(self, base_url=None):
            pass

        def send_message_ws(self, message, **kwargs):
            return send_impl(message, **kwargs)

        def cancel_current_request(self):
            cancel_spy()

        @staticmethod
        def _get_backend_url():
            return "http://127.0.0.1:8500"

    mod.ZenviBackendClient = ZenviBackendClient
    monkeypatch.setitem(sys.modules, "classes.api_client", mod)
    return mod


def test_send_assistant_prompt_returns_final_text(monkeypatch):
    def send(message, **kwargs):
        kwargs["on_response"]("here is the plan", "sess-99")
        return "here is the plan"

    _install_fake_api_client(send, MagicMock(), monkeypatch)
    monkeypatch.setattr(agent_api_proxy, "_access_token", lambda: "tok")

    out = agent_api_proxy.send_assistant_prompt_tool(message="make a 1 min video")

    assert "here is the plan" in out
    assert "sess-99" in out
    assert "status=complete" in out


def test_send_assistant_prompt_times_out_and_cancels(monkeypatch):
    release = threading.Event()
    cancel_spy = MagicMock()

    def send(message, **kwargs):
        release.wait(30)          # hung turn; released when the test finishes
        return "too late"

    _install_fake_api_client(send, cancel_spy, monkeypatch)
    monkeypatch.setattr(agent_api_proxy, "_access_token", lambda: "tok")

    try:
        out = agent_api_proxy.send_assistant_prompt_tool(
            message="hang", timeout_seconds=1)
    finally:
        release.set()

    assert "status=timeout" in out
    cancel_spy.assert_called_once()
    assert "openshot-qt.log" in out          # log pointers for the harness


def test_send_assistant_prompt_wires_a_tool_handler(monkeypatch):
    """Without on_tool_call the backend acks every tool with an error."""
    seen = {}

    def send(message, **kwargs):
        seen.update(kwargs)
        kwargs["on_response"]("ok", "s1")
        return "ok"

    _install_fake_api_client(send, MagicMock(), monkeypatch)
    monkeypatch.setattr(agent_api_proxy, "_access_token", lambda: "tok")

    agent_api_proxy.send_assistant_prompt_tool(message="hi")

    assert callable(seen.get("on_tool_call"))
    with patch.object(tool_handlers, "execute_tool",
                      return_value="tool ran") as ex:
        assert seen["on_tool_call"]("list_files_tool", {}, "call-1") == "tool ran"
    assert ex.call_args[0][0] == "list_files_tool"


def test_assistant_prompt_is_mcp_extra_not_an_agent_tool():
    from classes.agent_mcp_server import iter_tool_defs

    assert "send_assistant_prompt_tool" in agent_api_proxy.MCP_EXTRA_TOOLS
    # The assistant must not be able to prompt itself.
    assert "send_assistant_prompt_tool" not in tool_handlers.AGENT_TOOL_HANDLERS
    assert "send_assistant_prompt_tool" in {d["name"] for d in iter_tool_defs()}


# --- 4. index wait ---------------------------------------------------------

def test_wait_until_project_indexed_reports_done(monkeypatch):
    monkeypatch.setattr(tool_handlers, "QThread", None, raising=False)
    p = "/x/a.mp4"
    _install_fake_query({p: _fake_file("f1", p, analyzed=True)}, monkeypatch)
    app = _mock_app()

    with patch.object(tool_handlers, "_get_app", return_value=app):
        with patch.object(tool_handlers, "_wait_for_file_indexing",
                          return_value="") as waiter:
            out = tool_handlers.wait_until_project_indexed(timeout_seconds=5)

    assert waiter.called
    assert "pending" not in out.lower()


def test_wait_until_project_indexed_reports_pending(monkeypatch):
    monkeypatch.setattr(tool_handlers, "QThread", None, raising=False)
    a, b = "/x/a.mp4", "/x/b.mp4"
    _install_fake_query({a: _fake_file("f1", a, analyzed=True),
                         b: _fake_file("f2", b, analyzed=False)}, monkeypatch)
    app = _mock_app()

    def waiter(file_id, files_model, timeout_sec=1800):
        return "" if file_id == "f1" else "timed out after %ss" % timeout_sec

    with patch.object(tool_handlers, "_get_app", return_value=app):
        with patch.object(tool_handlers, "_wait_for_file_indexing", waiter):
            out = tool_handlers.wait_until_project_indexed(timeout_seconds=1)

    assert "pending" in out.lower()
    assert "f2" in out


def test_index_wait_is_background_safe():
    """Its poll loop must never run on the GUI thread."""
    assert "wait_until_project_indexed_tool" in tool_handlers.BACKGROUND_SAFE_TOOLS
    assert "wait_until_project_indexed_tool" in tool_handlers.AGENT_TOOL_HANDLERS


# --- 5. headless export ----------------------------------------------------

def test_export_video_headless_does_not_open_the_dialog(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_handlers, "QThread", None, raising=False)
    app = _mock_app()
    app.window.actionExportVideo_trigger = MagicMock()
    out_file = str(tmp_path / "out.mp4")

    export_mod = types.ModuleType("windows.export")
    export_mod.export_video_headless = MagicMock(return_value=None)
    export_mod.get_default_export_settings = lambda: (None, None, None, out_file)
    monkeypatch.setitem(sys.modules, "windows.export", export_mod)

    with patch.object(tool_handlers, "_get_app", return_value=app):
        out = tool_handlers.export_video(show_dialog="false", output_path=out_file)

    app.window.actionExportVideo_trigger.assert_not_called()
    export_mod.export_video_headless.assert_called_once()
    assert export_mod.export_video_headless.call_args[0][0] == out_file
    assert "Error" not in out


def _import_real_export_module(monkeypatch):
    """Import windows.export headlessly.

    It pulls openshot plus a Qt/app cascade at module level, so stub just enough
    of that to reach the pure range/codec decisions we care about. The stubs go
    in via monkeypatch so nothing leaks into later tests.
    """
    for name in ("openshot", "classes.openshot_rc", "classes.ui_util",
                 "classes.metrics", "classes.app", "classes.query",
                 "PyQt5.QtGui", "PyQt5"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, MagicMock())
    import windows.export as export_mod
    return export_mod


def _headless_export_env(monkeypatch, stored_settings, max_frame, export_type):
    """Drive export_video_headless with the project's stored settings stubbed."""
    export_mod = _import_real_export_module(monkeypatch)

    captured = {}

    class _Win:
        timeline = MagicMock()
        exporting = False
        _headless = False

        def run_export(self, path, video_settings, audio_settings, et, **kw):
            captured["path"] = path
            captured["video_settings"] = dict(video_settings)
            captured["export_type"] = et

    _Win.timeline.GetMaxFrame.return_value = max_frame

    monkeypatch.setattr(export_mod, "Export", _Win)
    monkeypatch.setattr(export_mod, "get_default_export_settings",
                        lambda: (stored_settings, {}, export_type, "/tmp/d.mp4"))
    monkeypatch.setattr(export_mod, "File", MagicMock(get=lambda **k: None))
    monkeypatch.setattr(export_mod, "get_app",
                        lambda: MagicMock(_tr=lambda s: s), raising=False)
    return export_mod, captured


def test_headless_export_covers_the_whole_timeline(tmp_path, monkeypatch):
    """A stale stored range must not silently truncate an unattended export."""
    export_mod, captured = _headless_export_env(
        monkeypatch, {"start_frame": 1, "end_frame": 300}, 1950, "Video & Audio")

    export_mod.export_video_headless(str(tmp_path / "out.mp4"))

    # 1950 frames of timeline, not the stored 300.
    assert captured["video_settings"]["end_frame"] == 1950


def test_headless_export_keeps_audio(tmp_path, monkeypatch):
    """dialog_test is verified by its transcript, so audio must survive."""
    export_mod, captured = _headless_export_env(
        monkeypatch, {"start_frame": 1, "end_frame": 300}, 1950, "Video & Audio")

    export_mod.export_video_headless(str(tmp_path / "out.mp4"))

    assert captured["export_type"] == "Video & Audio"


def test_audio_codec_prefers_aac_over_ac3(monkeypatch):
    """AC-3 in an .mp4 plays silent in most players, so it must be the last resort.

    ffprobe and Whisper both decode ac3 happily, so an export can look correct in
    every automated check and still have no sound for a human.
    """
    export_mod = _import_real_export_module(monkeypatch)

    # A modern FFmpeg build: no libfaac / libvo_aacenc / libfdk_aac, but aac and
    # ac3 are both present. The old order picked ac3 here.
    available = {"aac", "ac3", "libmp3lame"}

    # Replace the export module's own `openshot` reference rather than reaching
    # through whatever stub another test happened to install first.
    fake_openshot = types.SimpleNamespace(
        FFmpegWriter=types.SimpleNamespace(
            IsValidCodec=lambda c: c in available),
        LAYOUT_STEREO=2,
    )
    monkeypatch.setattr(export_mod, "openshot", fake_openshot)

    assert export_mod._resolve_audio_codec("aac") == "aac"

    # ac3 is still reachable when nothing else is.
    available.clear()
    available.add("ac3")
    assert export_mod._resolve_audio_codec("aac") == "ac3"

    # And no audio codec at all means video-only rather than a crash.
    available.clear()
    assert export_mod._resolve_audio_codec("aac") is None


def test_export_is_background_safe():
    """A real render outlasts the 30s dispatcher budget for interactive edits."""
    assert "export_video_tool" in tool_handlers.BACKGROUND_SAFE_TOOLS
    assert tool_handlers._EXPORT_MAIN_THREAD_TIMEOUT > 30


# --- 6. tools/list contract ------------------------------------------------

def test_tools_list_advertises_the_harness_tools():
    from classes.agent_mcp_server import iter_tool_defs

    defs = {d["name"]: d for d in iter_tool_defs()}

    for name in ("import_files_tool", "wait_until_project_indexed_tool",
                 "send_assistant_prompt_tool", "mcp_health_tool",
                 "get_log_paths_tool"):
        assert name in defs, name

    imp = defs["import_files_tool"]
    assert "paths" in imp["inputSchema"]["properties"]
    # A harness must be able to tell from tools/list that this needs no GUI.
    assert "dialog" in imp["description"].lower()
