"""Process-wide crash handling.

An unhandled traceback used to end the process with nothing shown and nothing
logged: frozen builds are GUI binaries (cx_Freeze base="Win32GUI" on Windows, a
.app bundle on macOS) whose sys.stdout/sys.stderr are None, and no
sys.excepthook was ever installed. These tests pin the pieces that make a
traceback survivable and visible on all three platforms.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import crash_handler  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_handler_state():
    """Never leave the interpreter's hooks or the throttle mutated."""
    saved_excepthook = sys.excepthook
    saved_threading_hook = getattr(threading, "excepthook", None)
    crash_handler.reset_dialog_throttle()
    yield
    crash_handler.uninstall()
    sys.excepthook = saved_excepthook
    if saved_threading_hook is not None:
        threading.excepthook = saved_threading_hook
    crash_handler.reset_dialog_throttle()


@pytest.fixture(scope="module")
def qapp():
    """A QApplication that stays alive: a discarded one is collected at once."""
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def captured(monkeypatch):
    """Collect what report() would log and show, instead of doing either."""
    records = {"logged": [], "dialogs": [], "fallback": []}

    class _Log:
        def error(self, msg, *args):
            records["logged"].append(msg % args if args else msg)

    monkeypatch.setattr(crash_handler, "_log", lambda: _Log())
    monkeypatch.setattr(crash_handler, "_queue_dialog",
                        lambda summary, tb, blocking=False:
                        records["dialogs"].append((summary, tb, blocking)))
    monkeypatch.setattr(crash_handler, "_fallback_write",
                        lambda text: records["fallback"].append(text))
    return records


def _raise(exc):
    try:
        raise exc
    except type(exc):
        return sys.exc_info()


# --- installation -----------------------------------------------------------


def test_install_replaces_both_interpreter_hooks():
    crash_handler.install()
    assert sys.excepthook is crash_handler._excepthook
    assert threading.excepthook is crash_handler._threading_excepthook


def test_install_is_idempotent():
    assert crash_handler.install() is True
    assert crash_handler.install() is False
    assert sys.excepthook is crash_handler._excepthook


def test_install_chains_to_the_hook_it_displaced():
    # sentry_sdk installs its own excepthook; ours must not lose it, because
    # SystemExit and KeyboardInterrupt still have to reach the previous handler.
    seen = []
    sys.excepthook = lambda *args: seen.append(args[0])
    crash_handler.install()

    crash_handler._excepthook(*_raise(SystemExit(0)))

    assert seen == [SystemExit]


def test_reinstalling_over_a_wrapping_hook_does_not_recurse():
    """sentry_sdk wraps sys.excepthook and chains back to what it displaced.

    launch.py installs us, sentry wraps that, and we install again on top --
    so the chain sentry -> us -> sentry has to be broken explicitly.
    """
    crash_handler.install()
    our_hook = sys.excepthook

    # Stand in for sentry_sdk's ExcepthookIntegration.
    def wrapping_hook(*args):
        our_hook(*args)

    sys.excepthook = wrapping_hook
    crash_handler.install()

    # Would blow the stack before the chaining guard existed.
    crash_handler._excepthook(*_raise(SystemExit(0)))


def test_chaining_falls_back_to_the_interpreter_default():
    # Nothing to chain to: still must not call itself.
    crash_handler.install()
    crash_handler._prev_excepthook = crash_handler._excepthook

    crash_handler._excepthook(*_raise(SystemExit(0)))


def test_uninstall_restores_the_previous_hooks():
    marker = lambda *args: None  # noqa: E731
    sys.excepthook = marker
    crash_handler.install()
    crash_handler.uninstall()
    assert sys.excepthook is marker


# --- reporting --------------------------------------------------------------


def test_report_logs_the_full_traceback(captured):
    crash_handler.report(*_raise(ValueError("kaboom")))

    assert len(captured["logged"]) == 1
    logged = captured["logged"][0]
    assert "ValueError: kaboom" in logged
    assert "Traceback (most recent call last)" in logged


def test_report_surfaces_a_dialog_by_default(captured):
    crash_handler.report(*_raise(ValueError("kaboom")))

    assert len(captured["dialogs"]) == 1
    summary, tb_text, blocking = captured["dialogs"][0]
    assert summary == "ValueError: kaboom"
    assert "kaboom" in tb_text
    # Runtime failures defer the dialog; only about-to-exit paths block.
    assert blocking is False


def test_report_can_stay_silent(captured):
    crash_handler.report(*_raise(ValueError("kaboom")), show_dialog=False)

    assert captured["logged"]
    assert captured["dialogs"] == []


def test_report_falls_back_to_a_file_when_logging_is_broken(monkeypatch, captured):
    class _BrokenLog:
        def error(self, *args, **kwargs):
            raise OSError("log file is gone")

    monkeypatch.setattr(crash_handler, "_log", lambda: _BrokenLog())

    crash_handler.report(*_raise(ValueError("kaboom")))

    assert any("kaboom" in text for text in captured["fallback"])


def test_report_never_raises_even_if_everything_fails(monkeypatch):
    monkeypatch.setattr(crash_handler, "_log", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(crash_handler, "_fallback_write", lambda text: None)

    # The whole point of the handler is that it cannot itself take the app down.
    crash_handler.report(*_raise(ValueError("kaboom")))


def test_recursive_reports_are_suppressed(monkeypatch, captured):
    # A handler that raises while reporting must not recurse forever.
    def _reenter():
        crash_handler.report(*_raise(ValueError("inner")))
        raise OSError("outer failed")

    class _Log:
        def error(self, *args, **kwargs):
            _reenter()

    monkeypatch.setattr(crash_handler, "_log", lambda: _Log())

    crash_handler.report(*_raise(ValueError("outer")))

    assert any("Recursive crash report suppressed" in text
               for text in captured["fallback"])


# --- hook behaviour ---------------------------------------------------------


def test_excepthook_reports_ordinary_exceptions(captured):
    crash_handler._excepthook(*_raise(ValueError("kaboom")))

    assert captured["logged"]


@pytest.mark.parametrize("exc", [SystemExit(0), KeyboardInterrupt()])
def test_excepthook_treats_control_flow_as_normal(captured, exc):
    # sys.exit() and Ctrl-C are not crashes; reporting them would pop a dialog
    # every time the user quits.
    crash_handler._excepthook(*_raise(exc))

    assert captured["logged"] == []
    assert captured["dialogs"] == []


def test_threading_excepthook_logs_without_a_dialog(captured):
    class _Args:
        exc_type, exc_value, exc_traceback = _raise(ValueError("kaboom"))
        thread = threading.current_thread()

    crash_handler._threading_excepthook(_Args())

    assert any("kaboom" in text for text in captured["logged"])
    # Background failures are usually cancelled jobs the user can't act on.
    assert captured["dialogs"] == []


def test_threading_excepthook_names_the_thread(captured):
    class _Args:
        exc_type, exc_value, exc_traceback = _raise(ValueError("kaboom"))
        thread = threading.Thread(name="indexing-worker")

    crash_handler._threading_excepthook(_Args())

    assert any("indexing-worker" in text for text in captured["logged"])


# --- dialog throttling ------------------------------------------------------


def test_identical_tracebacks_only_pop_one_dialog():
    tb_text = "Traceback ...\nValueError: same\n"

    assert crash_handler._should_show_dialog(tb_text) is True
    assert crash_handler._should_show_dialog(tb_text) is False


def test_a_burst_of_distinct_tracebacks_is_rate_limited():
    # An exception inside a paint or timer handler fires hundreds of times a
    # second; one dialog per occurrence would wedge the app worse than the bug.
    assert crash_handler._should_show_dialog("first\n") is True
    assert crash_handler._should_show_dialog("second\n") is False


def test_dialog_count_is_capped(monkeypatch):
    monkeypatch.setattr(crash_handler, "DIALOG_MIN_INTERVAL", 0.0)

    shown = sum(crash_handler._should_show_dialog("tb-%d\n" % i)
                for i in range(crash_handler.DIALOG_MAX_COUNT + 5))

    assert shown == crash_handler.DIALOG_MAX_COUNT


# --- traceback formatting ---------------------------------------------------


def test_format_exception_includes_type_and_message():
    text = crash_handler.format_exception(*_raise(ValueError("kaboom")))

    assert "ValueError: kaboom" in text


def test_format_exception_clamps_runaway_tracebacks():
    def recurse(n):
        if n:
            return recurse(n - 1)
        raise ValueError("deep")

    try:
        recurse(200)
    except ValueError:
        exc_info = sys.exc_info()

    text = crash_handler.format_exception(*exc_info, limit=500)

    assert len(text) < 700
    assert "traceback truncated" in text
    # Both ends are kept: the innermost frame and the exception itself.
    assert "ValueError: deep" in text


def test_format_exception_survives_an_unprintable_exception():
    class Nasty(Exception):
        def __str__(self):
            raise RuntimeError("cannot render me")

    text = crash_handler.format_exception(*_raise(Nasty()))

    assert "Nasty" in text


# --- faulthandler -----------------------------------------------------------


def test_faulthandler_uses_a_file_when_there_is_no_stderr(monkeypatch, tmp_path):
    # Frozen GUI builds have sys.stderr is None, which makes a plain
    # faulthandler.enable() raise -- so native crashes were never dumped there.
    import faulthandler

    monkeypatch.setattr(crash_handler, "_user_path", lambda: str(tmp_path))
    monkeypatch.setattr(sys, "stderr", None)
    try:
        assert crash_handler.enable_faulthandler() is True

        dump = tmp_path / "faulthandler.log"
        assert dump.exists()
        assert faulthandler.is_enabled()
    finally:
        # Don't leave the rest of the session dumping into a temp dir.
        faulthandler.disable()
        stream = crash_handler._faulthandler_stream
        crash_handler._faulthandler_stream = None
        if stream is not None:
            stream.close()


def test_log_file_path_points_at_the_app_log(monkeypatch, tmp_path):
    monkeypatch.setattr(crash_handler, "_user_path", lambda: str(tmp_path))

    assert crash_handler.log_file_path() == str(tmp_path / "openshot-qt.log")


# --- end to end -------------------------------------------------------------


def test_a_slot_exception_is_reported_and_the_event_loop_keeps_running(monkeypatch, qapp):
    """The behaviour the whole change exists for.

    A traceback raised from a Qt slot must be logged and the app must still be
    alive afterwards -- including when there is no console to print to, which is
    the frozen-build case on Windows and macOS.
    """
    from PyQt5.QtCore import QTimer

    app = qapp
    reported = []
    monkeypatch.setattr(crash_handler, "_log", lambda: None)
    monkeypatch.setattr(crash_handler, "_fallback_write", reported.append)
    monkeypatch.setattr(crash_handler, "_queue_dialog", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "stderr", None)

    crash_handler.install()

    survived = []

    def boom():
        raise RuntimeError("kaboom-from-slot")

    def later():
        survived.append(True)
        app.quit()

    QTimer.singleShot(0, boom)
    QTimer.singleShot(50, later)
    app.exec_()

    assert survived == [True], "the event loop died on an unhandled slot exception"
    assert any("kaboom-from-slot" in text for text in reported)


def test_startup_failures_ask_for_a_blocking_dialog(captured):
    crash_handler.report(*_raise(ValueError("kaboom")), blocking=True)

    assert captured["dialogs"][0][2] is True


def test_a_blocking_dialog_is_shown_synchronously(monkeypatch, qapp):
    """A deferred dialog is never delivered on a path that is about to exit."""
    shown = []
    monkeypatch.setattr(crash_handler, "_show_dialog",
                        lambda summary, tb: shown.append(summary))

    crash_handler._queue_dialog("ValueError: kaboom", "traceback\n", blocking=True)

    assert shown == ["ValueError: kaboom"]


def test_a_worker_thread_dialog_is_never_shown_inline(monkeypatch, qapp):
    """Widgets are main-thread only, so an off-thread report must be deferred."""
    shown = []
    monkeypatch.setattr(crash_handler, "_show_dialog",
                        lambda summary, tb: shown.append(summary))

    worker = threading.Thread(
        target=crash_handler._queue_dialog,
        args=("ValueError: kaboom", "traceback\n"),
        kwargs={"blocking": True},
    )
    worker.start()
    worker.join()

    assert shown == []
