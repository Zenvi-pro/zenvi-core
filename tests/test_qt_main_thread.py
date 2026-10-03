"""GUI-thread marshal helper (macOS / Windows / Linux)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.qt_main_thread import call_on_gui, invoke_on_gui, is_gui_thread, run_off_gui  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_is_gui_thread_on_the_app_thread(qapp):
    assert is_gui_thread() is True


def test_invoke_on_gui_runs_immediately_on_the_gui_thread(qapp):
    seen = []
    invoke_on_gui(seen.append, "ok", context=qapp)
    assert seen == ["ok"]


def test_call_on_gui_runs_immediately_on_the_gui_thread(qapp):
    assert call_on_gui(lambda: 7, context=qapp) == 7


def test_invoke_on_gui_from_a_worker_runs_on_the_app_thread(qapp):
    from PyQt5.QtCore import QThread

    app_thread = qapp.thread()
    seen = []

    def work():
        def mark():
            seen.append(QThread.currentThread() is app_thread)

        invoke_on_gui(mark, context=qapp)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(timeout=2)
    assert worker.is_alive() is False

    deadline = time.time() + 2
    while not seen and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert seen == [True]


def test_run_off_gui_keeps_the_gui_thread_serving_events(qapp):
    from PyQt5.QtCore import QTimer

    fired = []
    QTimer.singleShot(0, lambda: fired.append(True))

    def work():
        time.sleep(0.2)
        return threading.current_thread()

    worker = run_off_gui(work)
    assert worker is not threading.main_thread()
    assert fired == [True]  # the timer ran while the worker was busy
    with pytest.raises(ValueError):
        run_off_gui(lambda: (_ for _ in ()).throw(ValueError("boom")))


def test_run_off_gui_holds_other_threads_gui_calls_until_it_is_done(qapp):
    """PR #275 review: an agent tool's queued GUI call ran inside the wait, so
    it could edit the project a Collect Media / import was working on."""
    events = []

    def work():
        agent = threading.Thread(target=lambda: invoke_on_gui(events.append, "agent tool"))
        agent.start()
        agent.join()
        # The worker's own GUI calls must still run (or the two would deadlock).
        assert call_on_gui(lambda: "own", timeout=5) == "own"
        time.sleep(0.2)
        events.append("worker done")

    run_off_gui(work)
    events.append("returned")
    deadline = time.time() + 2
    while "agent tool" not in events and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert events == ["worker done", "returned", "agent tool"]


def test_a_held_call_on_gui_waits_instead_of_timing_out(qapp):
    """A blocking GUI call held behind run_off_gui must not report a timeout
    and then run anyway: it waits, runs once, and returns its result."""
    results, ran = [], []

    def agent():
        try:
            results.append(call_on_gui(lambda: ran.append(1) or "done", timeout=0.05))
        except Exception as exc:
            results.append(exc)

    thread = threading.Thread(target=agent)

    def work():
        thread.start()
        time.sleep(0.4)

    run_off_gui(work)
    deadline = time.time() + 2
    while thread.is_alive() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert results == ["done"] and ran == [1]
