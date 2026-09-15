"""export_video_tool must not hit the default 30s main-thread dispatch wait.

run_export() is synchronous on the GUI thread and can legitimately take
minutes to hours for a real project. After the MCP-harness merge,
export_video_tool is BACKGROUND_SAFE so execute_tool() does not wrap it
in the 30s dispatcher; export_video() marshals the encode itself with
_EXPORT_MAIN_THREAD_TIMEOUT (6 hours). These tests cover both the
dispatch wiring (right tool -> right timeout) and the real cross-thread
timeout mechanics (using small durations standing in for the real 30s /
6h, so the tests stay fast).
"""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

pytest.importorskip("PyQt5.QtCore")

import classes.tool_handlers as th  # noqa: E402


class _FakeThread:
    """Stands in for QThread.currentThread() returning a non-GUI thread."""


def _force_background_thread(monkeypatch):
    """Make execute_tool()'s GUI-thread check take the off-thread branch."""
    fake_app = MagicMock()
    fake_app.thread.return_value = object()
    monkeypatch.setattr(th, "_get_app", lambda: fake_app)

    class _QThreadStub:
        @staticmethod
        def currentThread():
            return _FakeThread()

    monkeypatch.setattr(th, "QThread", _QThreadStub)


def test_export_video_marshals_itself_with_the_extended_timeout(monkeypatch):
    """execute_tool skips the 30s wrap (BACKGROUND_SAFE); export_video
    passes the 6-hour ceiling into _run_on_main_thread."""
    assert "export_video_tool" in th.BACKGROUND_SAFE_TOOLS
    assert th._EXPORT_MAIN_THREAD_TIMEOUT == 6 * 60 * 60

    _force_background_thread(monkeypatch)
    seen_timeouts = []

    def fake_run_on_main_thread(func, *args, timeout=30):
        seen_timeouts.append(timeout)
        return func(*args)

    monkeypatch.setattr(th, "_run_on_main_thread", fake_run_on_main_thread)

    export_mod = types.ModuleType("windows.export")
    export_mod.export_video_headless = MagicMock(return_value=None)
    export_mod.get_default_export_settings = lambda: (None, None, None, "/tmp/out.mp4")
    monkeypatch.setitem(sys.modules, "windows.export", export_mod)

    result = th.export_video(show_dialog="false", output_path="/tmp/out.mp4")

    assert "Error" not in result
    assert seen_timeouts == [6 * 60 * 60], (
        "export_video must use the extended timeout, not the 30s default"
    )


def test_execute_tool_does_not_wrap_export_in_the_30s_dispatcher(monkeypatch):
    """BACKGROUND_SAFE means execute_tool must not impose the 30s wait."""
    _force_background_thread(monkeypatch)
    seen_timeouts = []

    def fake_run_on_main_thread(func, *args, timeout=30):
        seen_timeouts.append(timeout)
        return func(*args)

    monkeypatch.setattr(th, "_run_on_main_thread", fake_run_on_main_thread)
    monkeypatch.setitem(th.TOOL_HANDLERS, "export_video_tool", lambda **k: "exported")

    result = th.execute_tool("export_video_tool", {})

    assert result == "exported"
    assert seen_timeouts == [], (
        "execute_tool must not wrap export_video_tool; the handler marshals itself"
    )


def test_other_dispatched_tools_keep_the_default_30s_timeout(monkeypatch):
    """The extended timeout must be scoped to export_video_tool, not leak to
    every other main-thread-dispatched tool."""
    _force_background_thread(monkeypatch)
    seen_timeouts = []

    def fake_run_on_main_thread(func, *args, timeout=30):
        seen_timeouts.append(timeout)
        return func(*args)

    monkeypatch.setattr(th, "_run_on_main_thread", fake_run_on_main_thread)
    some_other_tool = next(
        name for name in th.TOOL_HANDLERS
        if name != "export_video_tool"
        and name not in th.READ_ONLY_TOOLS
        and name not in th.BACKGROUND_SAFE_TOOLS
    )
    monkeypatch.setitem(th.TOOL_HANDLERS, some_other_tool, lambda **k: "ok")

    th.execute_tool(some_other_tool, {})

    assert seen_timeouts == [30]


def test_real_dispatch_survives_past_the_old_default_with_the_override(monkeypatch):
    """Exercises the real _run_on_main_thread/_MainThreadDispatcher (no
    mocking of the dispatch mechanism itself) with small stand-in durations:
    a handler slower than the *old* uniform 30s-style wait must still
    succeed once export's own timeout override applies, and a tool without
    the override must still time out under the same slow handler -- proving
    the override, not some other change, is what saves the export path."""
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _force_background_thread(monkeypatch)
    monkeypatch.setattr(th, "_get_app", lambda: MagicMock(thread=lambda: app.thread()))

    # Stand-in for "the old flat default": every non-export tool call still
    # waits at most this long. export's override (patched down from 6h to
    # comfortably above the slow handler) must let it through.
    monkeypatch.setattr(th, "_EXPORT_MAIN_THREAD_TIMEOUT", 2.0)

    slow_handler_delay = 0.5  # "slower than a short default wait"
    default_style_timeout = 0.1  # stand-in for "the old default was too short"

    def slow_handler():
        time.sleep(slow_handler_delay)
        return "done"

    def _run_and_pump(target, result_box, key, join_timeout):
        """Run target() on a worker thread while pumping the GUI event loop
        (single dispatch at a time -- avoids racing two queued dispatches
        against the one shared _MainThreadDispatcher singleton)."""
        t = threading.Thread(target=lambda: result_box.__setitem__(key, target()))
        t.start()
        deadline = time.time() + join_timeout
        while key not in result_box and time.time() < deadline:
            app.processEvents()
            time.sleep(0.005)
        t.join(timeout=1)

    # Control: a tool without export's override still times out
    # under the same slow handler.
    other_box = {}

    def call_other():
        try:
            return th._run_on_main_thread(
                slow_handler,
                timeout=default_style_timeout,
            )
        except TimeoutError as exc:
            return exc

    _run_and_pump(call_other, other_box, "other", join_timeout=5)
    assert isinstance(other_box.get("other"), TimeoutError), (
        "a tool without the override should time out under a slow handler "
        "(control: proves the override, not something else, is what fixes export)"
    )

    # export's own override lets the same slow handler finish.
    export_box = {}

    def call_export():
        return th._run_on_main_thread(
            slow_handler,
            timeout=th._EXPORT_MAIN_THREAD_TIMEOUT,
        )

    _run_and_pump(call_export, export_box, "export", join_timeout=5)
    assert export_box.get("export") == "done", (
        "export's extended timeout should let the slow handler finish"
    )
