"""A timed-out GUI-thread hop either did nothing (safe to retry) or is waited out.

Before: when the GUI thread was slow, _run_on_main_thread raised
MAIN_THREAD_TIMEOUT while the queued call stayed in the event queue and ran
later, so the agent was told an edit failed that then landed -- and a retry
applied it twice (seen live with add_clip_to_timeline_tool and undo_tool).
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from classes import tool_handlers


class _Thread:
    pass


class _LateDispatcher:
    """Runs each payload on a separate thread after *delay* seconds (a busy GUI thread)."""

    def __init__(self, delay, run_seconds=0.0):
        self.delay = delay
        self.run_seconds = run_seconds
        self._dispatch = self
        self.threads = []

    def emit(self, payload):
        func, args, result_box, error_box, done = payload

        def _run():
            time.sleep(self.delay)
            try:
                result_box[0] = func(*args)
            except Exception as exc:
                error_box[0] = exc
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        self.threads.append(t)


def _patched(dispatcher):
    app = MagicMock()
    app.thread.return_value = _Thread()          # the GUI thread
    app.updates.transaction_id = None
    qthread = MagicMock()
    qthread.currentThread.return_value = _Thread()  # the caller is a worker
    return (patch.object(tool_handlers, "QThread", qthread),
            patch.object(tool_handlers, "_get_app", return_value=app),
            patch.object(tool_handlers, "_get_dispatcher", return_value=dispatcher))


def test_a_call_picked_up_after_the_deadline_is_skipped_and_says_nothing_changed():
    ran = []
    d = _LateDispatcher(delay=0.4)
    a, b, c = _patched(d)
    with a, b, c:
        with pytest.raises(tool_handlers.MainThreadTimeout) as err:
            tool_handlers._run_on_main_thread(lambda: ran.append(1), timeout=0.1)
    for t in d.threads:
        t.join(2)
    assert ran == [], "the edit must not land after the caller was told it failed"
    assert "nothing was changed" in str(err.value)


def test_a_call_that_started_in_time_is_waited_out_and_returns_its_result():
    d = _LateDispatcher(delay=0.05)

    def slow_edit():
        time.sleep(0.4)
        return "done"

    a, b, c = _patched(d)
    with a, b, c, patch.object(tool_handlers, "MAIN_THREAD_GRACE_SECONDS", 5):
        assert tool_handlers._run_on_main_thread(slow_edit, timeout=0.2) == "done"


def test_the_fast_path_is_unchanged():
    d = _LateDispatcher(delay=0.0)
    a, b, c = _patched(d)
    with a, b, c:
        assert tool_handlers._run_on_main_thread(lambda x: x * 2, 21, timeout=2) == 42
