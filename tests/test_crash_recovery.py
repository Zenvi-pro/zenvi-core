"""Crash bookkeeping must never become the next crash.

Two cascades used to be possible. An exception anywhere in the long shutdown
sequence skipped destroy_lock_file(), so the next launch found a stale .lock and
reported a crash that never happened. That report runs
libopenshot_crash_recovery(), which decoded libopenshot.log strictly -- and a
hard crash mid-write leaves exactly the truncated or binary tail that makes a
strict decode raise, from inside MainWindow.__init__. So one crash could leave
the app permanently unable to start.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import exceptions, info  # noqa: E402

PROBE = Path(__file__).resolve().parent / "_main_window_lock_probe.py"


@pytest.fixture
def user_path(monkeypatch, tmp_path):
    """Point the app's user directory at a temp dir for the whole call chain."""
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture(scope="module")
def settings_app():
    """A QApplication that answers get_settings(), kept alive for the module.

    classes.metrics reads get_app().get_settings() at import time, and a
    QApplication with no Python reference is collected immediately.
    """
    from _qt_support import skip_without_pyqt5

    skip_without_pyqt5(allow_module_level=False)
    from PyQt5.QtWidgets import QApplication

    class _Settings:
        def get(self, key, default=None):
            return default

    class _App(QApplication):
        def get_settings(self):
            return _Settings()

    app = QApplication.instance()
    if app is None:
        app = _App([])
    if not hasattr(app, "get_settings"):
        # Another module already made a plain QApplication; only one can exist.
        app.get_settings = lambda: _Settings()
    yield app


@pytest.fixture
def no_metrics(monkeypatch, settings_app):
    """libopenshot_crash_recovery() reports a metric; don't touch the network."""
    from classes import metrics

    sent = []
    monkeypatch.setattr(metrics, "track_metric_error",
                        lambda name, fatal=False: sent.append(name))
    return sent


def _write_libopenshot_log(user_path, payload: bytes):
    (user_path / "libopenshot.log").write_bytes(payload)


def run_lock_probe(scenario, home):
    """Run one MainWindow lock-file scenario in a clean interpreter.

    See _main_window_lock_probe.py for why this can't happen in-process.
    """
    env = dict(os.environ)
    # info.HOME_PATH is expanduser("~"), and on Windows that reads USERPROFILE
    # (then HOMEDRIVE + HOMEPATH) and ignores HOME entirely. Without these the
    # probe would resolve to the real home directory and the shutdown scenarios
    # would delete the developer's own ~/.openshot_qt/.lock.
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    drive, tail = os.path.splitdrive(str(home))
    env["HOMEDRIVE"] = drive
    env["HOMEPATH"] = tail or str(home)
    env["QT_QPA_PLATFORM"] = "offscreen"

    proc = subprocess.run([sys.executable, str(PROBE), scenario],
                          capture_output=True, text=True, timeout=180, env=env)

    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE:")), None)
    if line is None:
        pytest.skip("could not import MainWindow out-of-process:\n%s"
                    % (proc.stderr[-2000:] or proc.stdout[-2000:]))
    result = json.loads(line[len("PROBE:"):])
    assert result["ok"], result.get("error")
    return result


# --- the log parser ---------------------------------------------------------


def test_recovery_survives_invalid_utf8(user_path, no_metrics):
    # What a hard crash mid-write actually leaves behind.
    _write_libopenshot_log(user_path, b"libopenshot logging:\n"
                                      b"FFmpegReader::Open \xff\xfe\x80 garbage\n")

    result = exceptions.libopenshot_crash_recovery()

    assert isinstance(result, str)


def test_recovery_survives_invalid_utf8_inside_a_stack_trace(user_path, no_metrics):
    _write_libopenshot_log(user_path, b"libopenshot logging:\n"
                                      b"Unhandled Exception: Stack Trace\n"
                                      b"Timeline::Open \x80\x81 \xfe\n"
                                      b"End of Stack Trace\n")

    result = exceptions.libopenshot_crash_recovery()

    assert isinstance(result, str)


def test_recovery_handles_an_empty_log(user_path, no_metrics):
    _write_libopenshot_log(user_path, b"")

    assert exceptions.libopenshot_crash_recovery() == ""


def test_recovery_returns_none_when_there_is_no_log(user_path, no_metrics):
    assert exceptions.libopenshot_crash_recovery() is None


def test_recovery_handles_a_line_shorter_than_the_mac_slice(
        user_path, no_metrics, monkeypatch):
    # The Darwin branch slices [58:] off the line without checking its length.
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    _write_libopenshot_log(user_path, b"libopenshot logging:\nshort\n")

    assert isinstance(exceptions.libopenshot_crash_recovery(), str)


def test_recovery_swallows_a_failure_while_normalizing(
        user_path, no_metrics, monkeypatch):
    # The normalization is shape-dependent slicing on a log we don't control, and
    # it only exists to make the metric roll up neatly.
    def _boom():
        raise RuntimeError("platform lookup failed")

    monkeypatch.setattr(platform, "system", _boom)
    _write_libopenshot_log(user_path, b"libopenshot logging:\nTimeline::Open (arg)\n")

    assert exceptions.libopenshot_crash_recovery() == ""


def test_recovery_still_reports_a_metric(user_path, no_metrics):
    _write_libopenshot_log(user_path, b"libopenshot logging:\nTimeline::Open (arg)\n")

    exceptions.libopenshot_crash_recovery()

    assert no_metrics
    assert no_metrics[0].startswith("unhandled-crash")


# --- the lock file ----------------------------------------------------------


def test_a_failing_recovery_does_not_block_startup(tmp_path):
    """create_lock_file() runs inside MainWindow.__init__.

    A raising recovery there used to take the whole launch down, so a single
    crash made the app unstartable.
    """
    result = run_lock_probe("recovery_raises", tmp_path)

    # A fresh lock was still written for this session.
    assert result["lock_exists"]
    assert result["lock_text"] != "stale"


def test_close_event_releases_the_lock_when_shutdown_raises(tmp_path):
    """A stale lock makes the next launch report a phantom crash."""
    result = run_lock_probe("shutdown_raises", tmp_path)

    assert result["ran"]
    assert not result["lock_exists"]


def test_close_event_releases_the_lock_on_a_clean_shutdown(tmp_path):
    result = run_lock_probe("clean_shutdown", tmp_path)

    assert result["ran"]
    assert not result["lock_exists"]


def test_close_event_is_idempotent(tmp_path):
    # Some Qt versions fire closeEvent() twice.
    result = run_lock_probe("already_shutting_down", tmp_path)

    assert not result["ran"]
    assert result["lock_exists"], "the first closeEvent owns the lock file, not the second"


# --- a failed launch has to look failed -------------------------------------


def test_a_fatal_startup_error_exits_non_zero(settings_app):
    """launch.py's own sys.exit(1) is unreachable for a queued fatal error:
    StartupError.show raises SystemExit straight out through show_errors(). A
    bare sys.exit() there reported success, so a startup that never finished
    looked fine to the shell, to packaging smoke tests and to any supervisor."""
    from classes.app import StartupError

    shown = []
    err = StartupError("Startup Error", "could not start", level="error")
    err.levels = {"error": lambda parent, title, message: shown.append(title)}

    with pytest.raises(SystemExit) as caught:
        err.show()

    assert caught.value.code == 1
    assert shown == ["Startup Error"], "the user still has to see the message"


def test_a_warning_level_startup_error_does_not_exit(settings_app):
    from classes.app import StartupError

    shown = []
    err = StartupError("Heads up", "something minor", level="warning")
    err.levels = {"warning": lambda parent, title, message: shown.append(title)}

    err.show()

    assert shown == ["Heads up"]


def test_show_errors_ends_the_launch_with_a_failure_code(settings_app):
    """The fatal exit has to survive the loop that displays the queue."""
    from classes.app import OpenShotApp, StartupError

    fatal = StartupError("Import Error", "no module", level="error")
    fatal.levels = {"error": lambda parent, title, message: None}

    class _Stub:
        errors = [fatal]
        log = None

    with pytest.raises(SystemExit) as caught:
        OpenShotApp.show_errors(_Stub())

    assert caught.value.code == 1
