"""Mutating execute_tool pauses the window autosave timer for the whole call."""

from __future__ import annotations

import types

from classes.agent_tools import execute as ex
from classes.agent_tools.receipt import ToolReceipt


class _Timer:
    def __init__(self):
        self.active = True
        self.starts = 0
        self.stops = 0

    def isActive(self):
        return self.active

    def stop(self):
        self.stops += 1
        self.active = False

    def start(self):
        self.starts += 1
        self.active = True


def test_mutating_tool_pauses_and_resumes_autosave():
    timer = _Timer()
    seen_stopped = []

    def handler(**_kw):
        seen_stopped.append(not timer.active)
        return ToolReceipt.applied("test_mutating_tool", "ok")

    app = types.SimpleNamespace(
        updates=types.SimpleNamespace(actionHistory=[]),
        project={},
        thread=lambda: None,
        window=types.SimpleNamespace(auto_save_timer=timer),
        _agent_tool_in_progress=False,
    )
    ex.bind_runtime(
        handlers={"test_mutating_tool": handler},
        read_only=frozenset(),
        background_safe=frozenset(),
        ungrouped=frozenset({"test_mutating_tool"}),
        main_thread_timeouts={},
        get_app=lambda: app,
        run_on_main_thread=lambda fn, timeout=30: fn(),
        atomic=lambda _app, fn: fn,
        coerce_steps=lambda x: 1,
        qthread=None,
    )
    ex._autosave_pause_depth = 0
    ex._autosave_was_running = False

    ex.execute_tool_rich("test_mutating_tool", {})

    assert seen_stopped == [True]
    assert timer.stops == 1
    assert timer.starts == 1
    assert timer.active is True
    assert app._agent_tool_in_progress is False


def test_read_only_tool_does_not_pause_autosave():
    timer = _Timer()

    def handler(**_kw):
        return ToolReceipt.applied("test_readonly_tool", "ok")

    app = types.SimpleNamespace(
        updates=types.SimpleNamespace(actionHistory=[]),
        project={},
        thread=lambda: None,
        window=types.SimpleNamespace(auto_save_timer=timer),
    )
    ex.bind_runtime(
        handlers={"test_readonly_tool": handler},
        read_only=frozenset({"test_readonly_tool"}),
        background_safe=frozenset({"test_readonly_tool"}),
        ungrouped=frozenset({"test_readonly_tool"}),
        main_thread_timeouts={},
        get_app=lambda: app,
        run_on_main_thread=lambda fn, timeout=30: fn(),
        atomic=lambda _app, fn: fn,
        coerce_steps=lambda x: 1,
        qthread=None,
    )
    ex._autosave_pause_depth = 0
    ex._autosave_was_running = False

    ex.execute_tool_rich("test_readonly_tool", {})

    assert timer.stops == 0
    assert timer.starts == 0
    assert timer.active is True
