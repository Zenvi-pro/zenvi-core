"""export_video_tool must not hit the default 30s main-thread dispatch wait.

A render can legitimately take minutes to hours for a real project.
export_video_tool is BACKGROUND_SAFE so execute_tool() does not wrap it in
the 30s dispatcher, and export_video() renders on its own QThread rather
than on the GUI thread. These tests cover both the dispatch wiring and the
real cross-thread timeout mechanics (using small durations standing in for
the real 30s / 6h, so the tests stay fast).
"""

from __future__ import annotations

import os
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


def test_export_video_renders_outside_the_main_thread_dispatcher(monkeypatch):
    """execute_tool skips the 30s wrap (BACKGROUND_SAFE), and export_video_tool
    does not put the render on the GUI thread itself either: the editor would
    be frozen for the whole encode."""
    from classes.editor_tools import project_export_render as render

    assert "export_video_tool" in th.BACKGROUND_SAFE_TOOLS

    _force_background_thread(monkeypatch)
    on_gui_thread = []

    def fake_run_on_main_thread(func, *args, timeout=30):
        on_gui_thread.append(True)
        try:
            return func(*args)
        finally:
            on_gui_thread.pop()

    monkeypatch.setattr(th, "_run_on_main_thread", fake_run_on_main_thread)

    out_file = "/tmp/zenvi-export-timeout-test.mp4"
    export_mod = types.ModuleType("windows.export")
    rendered_on_gui_thread = []

    def fake_headless(path, *a, **k):
        rendered_on_gui_thread.append(bool(on_gui_thread))
        with open(path, "wb") as fh:
            fh.write(b"\x00" * 8)

    export_mod.export_video_headless = fake_headless
    monkeypatch.setitem(sys.modules, "windows.export", export_mod)
    monkeypatch.setattr(render, "build_export_plan", lambda *a, **k: {
        "path": out_file, "export_type": "video_audio", "vformat": "mp4", "vcodec": "libx264", "acodec": "aac",
        "width": 1920, "height": 1080, "fps_num": 30, "fps_den": 1, "fps": 30.0,
        "pixel_ratio": {"num": 1, "den": 1}, "video_bitrate": "20 crf", "audio_bitrate": "160 kb/s",
        "sample_rate": 48000, "channels": 2, "channel_layout": 3, "interlaced": False, "start_seconds": 0.0,
        "end_seconds": 1.0, "start_frame": 1, "end_frame": 30, "range": "whole", "profile": "", "profile_path": None,
        "notes": [], "preset": "MP4 (h.264)", "preset_category": "All Formats", "quality": "High"})
    monkeypatch.setattr(render, "window", lambda: MagicMock())
    # No project here: the "is it one of the inputs?" check sees no files.
    from classes import query
    monkeypatch.setattr(query.File, "filter", classmethod(lambda cls, **kw: []))
    monkeypatch.setattr(query.File, "get", classmethod(lambda cls, **kw: None))
    try:
        result = th.execute_tool("export_video_tool", {"overwrite": True})
    finally:
        if os.path.exists(out_file):
            os.remove(out_file)

    assert "Error" not in result, result
    assert rendered_on_gui_thread == [False], (
        "export_video_tool must not render on the GUI thread"
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

    # execute_tool wraps the handler's reply in a contract-3 receipt (#183).
    assert "exported" in result
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
