"""
 @file
 @brief Process-wide handlers that keep an unhandled traceback from silently
        killing the application (Linux, macOS and Windows).
 @author Zenvi

 @section LICENSE

 Copyright (c) 2008-2018 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

# Why this module exists
# ----------------------
# Frozen builds are GUI subsystem binaries (cx_Freeze base="Win32GUI" on
# Windows, a .app bundle on macOS), so sys.stdout/sys.stderr can be None and
# nothing the interpreter prints on its own reaches the user. Combined with the
# fact that nothing installed a sys.excepthook, an unhandled traceback made the
# app disappear with no message and no log entry. This module makes every
# unhandled exception land in the log file (and, when a GUI is up, in a dialog)
# instead of taking the process down quietly.
#
# Deliberately conservative: no PyQt import at module scope, every handler is
# wrapped so it can never raise, and the dialog is rate-limited and marshalled
# to the GUI thread (sys.excepthook also fires on QThread worker threads).

import os
import sys
import threading
import traceback

MAX_TRACEBACK_CHARS = 12000

# Seconds to wait before showing another crash dialog, and how many distinct
# tracebacks we are willing to pop up in one session. A repeating exception in a
# paint or timer handler fires hundreds of times a second; without these the
# dialogs themselves would wedge the app.
DIALOG_MIN_INTERVAL = 20.0
DIALOG_MAX_COUNT = 12

_installed = False
_prev_excepthook = None
_prev_threading_excepthook = None
_faulthandler_stream = None

# Re-entrancy guard: our handler can itself raise (broken logging, dead Qt), and
# reporting that failure through the same path would recurse forever.
_reporting = threading.local()

_dialog_lock = threading.Lock()
_dialog_state = {"last_time": 0.0, "count": 0, "seen": set()}


def _log():
    """Return the app logger, or None when logging is not usable yet."""
    try:
        from classes.logger import log
        return log
    except Exception:
        return None


def _user_path():
    try:
        from classes import info
        return info.USER_PATH
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".openshot_qt")


def log_file_path():
    """Best-effort path of the rotating app log, for display to the user."""
    return os.path.join(_user_path(), "openshot-qt.log")


def _fallback_write(text):
    """Last-resort sink used when the logging subsystem is unavailable.

    Frozen GUI builds have no stderr, so a file is the only place this can go.
    """
    try:
        path = os.path.join(_user_path(), "crash.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
    except Exception:
        pass


def format_exception(exc_type, exc_value, exc_tb, limit=MAX_TRACEBACK_CHARS):
    """Format a traceback, clamped so a runaway recursion can't blow up the log."""
    try:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception:
        text = "%s: %s\n" % (getattr(exc_type, "__name__", exc_type), exc_value)
    if limit and len(text) > limit:
        head = limit // 2
        tail = limit - head
        text = "%s\n... [traceback truncated] ...\n%s" % (text[:head], text[-tail:])
    return text


def _summary(exc_type, exc_value):
    name = getattr(exc_type, "__name__", None) or str(exc_type)
    try:
        detail = str(exc_value)
    except Exception:
        detail = "<unprintable exception>"
    return "%s: %s" % (name, detail) if detail else name


def report(exc_type, exc_value, exc_tb, context="unhandled exception", show_dialog=True,
           blocking=False):
    """Log an exception (and optionally surface it) without letting it kill us.

    Set blocking=True only on paths that are about to exit (startup failures):
    the dialog is then shown synchronously, because the deferred one would never
    be delivered -- there is no event loop left to deliver it on.
    """
    if getattr(_reporting, "active", False):
        # We are already reporting; write straight to the fallback file.
        _fallback_write("Recursive crash report suppressed (%s): %s"
                        % (context, _summary(exc_type, exc_value)))
        return
    _reporting.active = True
    try:
        tb_text = format_exception(exc_type, exc_value, exc_tb)
        message = "Zenvi %s: %s\n%s" % (context, _summary(exc_type, exc_value), tb_text)

        log = _log()
        if log is not None:
            try:
                log.error(message)
            except Exception:
                _fallback_write(message)
        else:
            _fallback_write(message)

        _report_to_sentry(exc_type, exc_value, exc_tb)

        if show_dialog:
            _queue_dialog(_summary(exc_type, exc_value), tb_text, blocking=blocking)
    except Exception:
        try:
            _fallback_write("crash_handler.report() failed:\n" + traceback.format_exc())
        except Exception:
            pass
    finally:
        _reporting.active = False


def _report_to_sentry(exc_type, exc_value, exc_tb):
    try:
        import sentry_sdk
    except Exception:
        return
    try:
        sentry_sdk.capture_exception((exc_type, exc_value, exc_tb))
    except Exception:
        pass


def _should_show_dialog(tb_text):
    """Rate-limit dialogs so a repeating exception can't bury the UI."""
    import time

    with _dialog_lock:
        state = _dialog_state
        if state["count"] >= DIALOG_MAX_COUNT:
            return False
        key = tb_text[-2000:]
        if key in state["seen"]:
            return False
        now = time.monotonic()
        if state["count"] and (now - state["last_time"]) < DIALOG_MIN_INTERVAL:
            return False
        state["seen"].add(key)
        state["last_time"] = now
        state["count"] += 1
        return True


def _queue_dialog(summary, tb_text, blocking=False):
    """Show the error dialog on the GUI thread, if there is a GUI to show it on."""
    try:
        from PyQt5.QtCore import QCoreApplication, QThread, QTimer
        from PyQt5.QtWidgets import QApplication
    except Exception:
        return

    app = QApplication.instance()
    if app is None:
        # No GUI yet (or a headless/unittest run): the log entry is all we can do.
        return

    if not _should_show_dialog(tb_text):
        return

    on_gui_thread = False
    try:
        on_gui_thread = QThread.currentThread() is QCoreApplication.instance().thread()
    except Exception:
        pass

    if blocking and on_gui_thread:
        # QMessageBox.exec_() spins its own event loop, so this works even before
        # app.exec_() has started -- which is exactly the startup-failure case.
        _show_dialog(summary, tb_text)
        return

    def _show():
        _show_dialog(summary, tb_text)

    # sys.excepthook also fires on QThread workers, and widgets are main-thread
    # only -- the 3-argument singleShot runs the callable in the context
    # object's thread, which for the QApplication is always the GUI thread.
    # Deferring also keeps us from opening a nested event loop from inside a
    # paint or timer handler, which is where these exceptions often originate.
    try:
        QTimer.singleShot(0, app, _show)
    except Exception:
        _fallback_write("crash_handler could not queue the error dialog:\n"
                        + traceback.format_exc())


def _show_dialog(summary, tb_text):
    """Non-fatal error dialog. Never raises, never exits the app."""
    try:
        from PyQt5.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            return

        box = QMessageBox()
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Zenvi ran into a problem")
        box.setText(
            "Something went wrong, but Zenvi is still running.\n\n%s" % summary
        )
        box.setInformativeText(
            "Your project has not been closed. If the app is misbehaving, save "
            "your work and restart.\n\nDetails were written to:\n%s"
            % log_file_path()
        )
        box.setDetailedText(tb_text)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec_()
    except Exception:
        _fallback_write("crash_handler._show_dialog() failed:\n" + traceback.format_exc())


def _call_previous_excepthook(exc_type, exc_value, exc_tb):
    """Hand control to the hook we displaced, without risking a loop.

    install() is called twice on purpose (once early, once after sentry_sdk
    wraps sys.excepthook), and sentry's wrapper chains back to whatever it
    displaced -- which is us. Without the guard below, sentry -> us -> sentry
    would recurse forever.
    """
    if getattr(_reporting, "chaining", False):
        return

    prev = _prev_excepthook
    if prev is None or prev is _excepthook:
        prev = sys.__excepthook__

    _reporting.chaining = True
    try:
        prev(exc_type, exc_value, exc_tb)
    except Exception:
        pass
    finally:
        _reporting.chaining = False


def _excepthook(exc_type, exc_value, exc_tb):
    # SystemExit / KeyboardInterrupt are normal control flow, not crashes.
    if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
        _call_previous_excepthook(exc_type, exc_value, exc_tb)
        return
    report(exc_type, exc_value, exc_tb, context="unhandled exception")


def _threading_excepthook(args):
    exc_type = getattr(args, "exc_type", None)
    if exc_type is not None and issubclass(exc_type, SystemExit):
        return
    thread = getattr(args, "thread", None)
    name = getattr(thread, "name", "?")
    # Background-thread failures are logged but never pop a dialog: they are
    # usually a cancelled network or indexing job, not something the user can act on.
    report(exc_type, getattr(args, "exc_value", None), getattr(args, "exc_traceback", None),
           context="unhandled exception in thread %r" % name, show_dialog=False)


def enable_faulthandler():
    """Dump native (SIGSEGV/SIGABRT) stacks somewhere they can actually be read.

    faulthandler defaults to sys.stderr, which is None in frozen GUI builds --
    faulthandler.enable() then raises and we got no native crash dumps at all on
    Windows. Point it at a file whenever stderr is missing.
    """
    global _faulthandler_stream

    try:
        import faulthandler
    except Exception:
        return False

    if getattr(sys, "stderr", None) is not None:
        try:
            faulthandler.enable(all_threads=True)
            return True
        except Exception:
            pass

    try:
        path = os.path.join(_user_path(), "faulthandler.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Kept open for the process lifetime on purpose: faulthandler writes to
        # this fd from a signal handler, so it must stay valid until exit.
        _faulthandler_stream = open(path, "a", buffering=1, encoding="utf-8")
        faulthandler.enable(file=_faulthandler_stream, all_threads=True)
        return True
    except Exception:
        _faulthandler_stream = None
        return False


def install():
    """Install the process-wide handlers. Safe to call more than once.

    Call this *after* anything else that wraps sys.excepthook (sentry_sdk does),
    so our handler runs first and still chains to theirs.
    """
    global _installed, _prev_excepthook, _prev_threading_excepthook

    if _installed and sys.excepthook is _excepthook:
        return False

    _prev_excepthook = sys.excepthook
    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        _prev_threading_excepthook = threading.excepthook
        threading.excepthook = _threading_excepthook

    _installed = True
    return True


def uninstall():
    """Restore the previous handlers (used by the tests)."""
    global _installed, _prev_excepthook, _prev_threading_excepthook

    if not _installed:
        return
    if _prev_excepthook is not None:
        sys.excepthook = _prev_excepthook
    if _prev_threading_excepthook is not None and hasattr(threading, "excepthook"):
        threading.excepthook = _prev_threading_excepthook
    _prev_excepthook = None
    _prev_threading_excepthook = None
    _installed = False


def reset_dialog_throttle():
    """Clear the dialog rate-limiter (used by the tests)."""
    with _dialog_lock:
        _dialog_state["last_time"] = 0.0
        _dialog_state["count"] = 0
        _dialog_state["seen"] = set()
