"""A call the Qt GUI thread never started must not run behind its caller's back.

Testing RC v1.2.0 (#216), Claude Code put the same clip on the timeline three
times. The GUI thread was stalled past _run_on_main_thread's 30 s wait, so the
tool answered MAIN_THREAD_TIMEOUT -- but the queued call stayed queued and ran
once the GUI thread came back, and so did each retry the agent made.

A timed-out call is now withdrawn if the GUI thread has not started it, and one
that has started is reported as still running, with a job id the agent can wait
on instead of retrying.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from classes import agent_api_proxy
from classes import tool_handlers
from classes.updates import UpdateManager


class _WorkerThread:
    """QThread stand-in: the caller is never the GUI thread, so calls marshal."""

    @staticmethod
    def currentThread():
        return "worker"


def _marshal_through(monkeypatch, deliver):
    """Send _run_on_main_thread's jobs to *deliver*, which stands in for the GUI
    thread's event loop: it decides when -- or whether -- each job runs."""
    app = MagicMock()
    app.thread.return_value = "gui"
    app.updates = UpdateManager()
    monkeypatch.setattr(tool_handlers, "QThread", _WorkerThread)
    monkeypatch.setattr(tool_handlers, "_get_app", lambda: app)
    dispatcher = SimpleNamespace(_dispatch=SimpleNamespace(emit=deliver))
    monkeypatch.setattr(tool_handlers, "_get_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(tool_handlers, "_late_jobs", {})
    return app


class _SlowGuiThread:
    """Starts each job on its own thread and holds it mid-run until released,
    like a GUI thread stuck inside a slow edit."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.threads = []

    def deliver(self, job):
        thread = threading.Thread(target=job.run, daemon=True)
        thread.start()
        self.threads.append(thread)
        assert self.started.wait(5), "the stand-in GUI thread never started the job"

    def join(self):
        self.release.set()
        for thread in self.threads:
            thread.join(5)


def _add_clip(app, gui=None):
    """A place + trim edit: two mutations that must stay one undo step."""
    def edit():
        if gui is not None:
            gui.started.set()
            gui.release.wait(5)
        app.updates.insert(["clips"], {"id": "c1"})
        app.updates.update(["clips", {"id": "c1"}], {"start": 1.0})
        return "Added clip to timeline"
    return edit


# --------------------------------------------------------------------------
# Not started when the wait ran out: withdrawn, and it never runs
# --------------------------------------------------------------------------

def test_a_call_the_gui_thread_never_started_is_withdrawn(monkeypatch):
    queued = []
    app = _marshal_through(monkeypatch, queued.append)

    with pytest.raises(tool_handlers.MainThreadTimeout) as exc:
        tool_handlers._run_on_main_thread(_add_clip(app), timeout=0.05)

    assert not isinstance(exc.value, tool_handlers.MainThreadStillRunning)
    assert "MAIN_THREAD_TIMEOUT" in str(exc.value)
    assert "withdrawn before it started" in str(exc.value)

    # The GUI thread drains its queue at last: the withdrawn call stays dead.
    queued[0].run()
    assert app.updates.actionHistory == []


def test_a_timed_out_tool_call_leaves_history_untouched(monkeypatch):
    """Through execute_tool, as the MCP server calls it: no edit, no undo step."""
    queued = []
    app = _marshal_through(monkeypatch, queued.append)
    monkeypatch.setattr(tool_handlers, "_MAIN_THREAD_TIMEOUT_DEFAULT", 0.05)
    calls = []

    def add_track(**_kw):
        calls.append("add_track")
        app.updates.insert(["layers"], {"id": "L9"})
        return "Added track"

    monkeypatch.setitem(tool_handlers.TOOL_HANDLERS, "add_track_tool", add_track)

    out = tool_handlers.execute_tool("add_track_tool", {})

    assert out.startswith("Error: MAIN_THREAD_TIMEOUT")
    queued[0].run()
    assert calls == []
    assert app.updates.actionHistory == []


# --------------------------------------------------------------------------
# Already running when the wait ran out: reported, finishes once, waitable
# --------------------------------------------------------------------------

def test_a_call_already_running_is_reported_and_finishes_once(monkeypatch):
    gui = _SlowGuiThread()
    app = _marshal_through(monkeypatch, gui.deliver)
    # The caller's undo group; transaction_id is per thread, so the hop has to
    # carry it onto the GUI thread for the late edit to join it.
    app.updates.transaction_id = "tool-call-1"

    with pytest.raises(tool_handlers.MainThreadStillRunning) as exc:
        tool_handlers._run_on_main_thread(_add_clip(app, gui), timeout=0.05)

    job_id = exc.value.job_id
    message = str(exc.value)
    assert "MAIN_THREAD_STILL_RUNNING" in message
    assert "Do NOT retry" in message
    assert job_id and job_id in message
    assert isinstance(exc.value, TimeoutError)

    assert tool_handlers.wait_for_main_thread_job(job_id, 0.01)["state"] == "running"
    gui.join()
    outcome = tool_handlers.wait_for_main_thread_job(job_id, 5)

    assert outcome["state"] == "done"
    assert outcome["result"] == "Added clip to timeline"
    assert outcome["error"] is None
    # It ran exactly once, and its place + trim are one undo step.
    assert [a.type for a in app.updates.actionHistory] == ["insert", "update"]
    assert {a.transaction for a in app.updates.actionHistory} == {"tool-call-1"}


def test_a_late_error_is_reported_by_the_wait(monkeypatch):
    gui = _SlowGuiThread()
    _marshal_through(monkeypatch, gui.deliver)

    def failing_edit():
        gui.started.set()
        gui.release.wait(5)
        raise ValueError("no such clip")

    with pytest.raises(tool_handlers.MainThreadStillRunning) as exc:
        tool_handlers._run_on_main_thread(failing_edit, timeout=0.05)
    gui.join()

    outcome = tool_handlers.wait_for_main_thread_job(exc.value.job_id, 5)
    assert outcome["state"] == "done"
    assert isinstance(outcome["error"], ValueError)


def test_wait_for_editor_job_tool_reports_the_late_outcome(monkeypatch):
    """The MCP tool the still-running error points the agent at."""
    gui = _SlowGuiThread()
    app = _marshal_through(monkeypatch, gui.deliver)

    with pytest.raises(tool_handlers.MainThreadStillRunning) as exc:
        tool_handlers._run_on_main_thread(_add_clip(app, gui), timeout=0.05)
    job_id = exc.value.job_id

    running = agent_api_proxy.wait_for_editor_job_tool(job_id=job_id, timeout_seconds=0)
    assert running.startswith("status=running job_id=%s" % job_id)
    assert "Do not retry" in running

    gui.join()
    done = agent_api_proxy.wait_for_editor_job_tool(job_id=job_id, timeout_seconds=5)
    assert done.startswith("status=done job_id=%s" % job_id)
    assert done.endswith("Added clip to timeline")

    assert agent_api_proxy.wait_for_editor_job_tool(job_id="nope").startswith("status=unknown")
    assert agent_api_proxy.wait_for_editor_job_tool().startswith("Error:")


def test_wait_for_editor_job_tool_is_an_mcp_only_tool():
    """Called straight on the MCP worker thread, never marshalled: it has to
    answer while the GUI thread is still busy with the job it waits on."""
    from classes.agent_mcp_server import iter_tool_defs

    assert "wait_for_editor_job_tool" in agent_api_proxy.MCP_EXTRA_TOOLS
    assert "wait_for_editor_job_tool" not in tool_handlers.AGENT_TOOL_HANDLERS
    defs = {d["name"]: d for d in iter_tool_defs()}
    schema = defs["wait_for_editor_job_tool"]["inputSchema"]
    assert schema["properties"]["job_id"]["type"] == "string"
    assert schema["properties"]["timeout_seconds"]["type"] == "integer"


# --------------------------------------------------------------------------
# A GUI thread that answers in time: exactly as before
# --------------------------------------------------------------------------

def test_a_fast_call_returns_its_result_and_raises_its_error(monkeypatch):
    _marshal_through(monkeypatch, lambda job: job.run())

    assert tool_handlers._run_on_main_thread(lambda a, b: a + b, 2, 3, timeout=1) == 5

    def boom():
        raise ValueError("no such clip")

    with pytest.raises(ValueError, match="no such clip"):
        tool_handlers._run_on_main_thread(boom, timeout=1)
    assert tool_handlers._late_jobs == {}


def test_a_job_that_finished_cannot_be_withdrawn():
    """The wait can run out just as the job finishes; the result then stands."""
    ran = []
    job = tool_handlers._MainThreadJob(lambda: ran.append(1) or "ok", ())
    job.run()
    assert job.withdraw() == job.DONE
    assert (job.result, ran) == ("ok", [1])

    withdrawn = tool_handlers._MainThreadJob(lambda: ran.append(2), ())
    assert withdrawn.withdraw() == withdrawn.WITHDRAWN
    withdrawn.run()
    assert ran == [1]
