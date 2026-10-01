"""Headless mode: the real editor, with no window on screen, driven over MCP.

``launch.py --headless [--project cut.zvn]`` starts the same app a user gets --
MainWindow, timeline, libopenshot, AGENT_TOOL_HANDLERS -- on Qt's offscreen
platform, so nothing is drawn on screen, and serves the in-app MCP server
(classes.agent_mcp_server). There is no second editing engine and no second tool
schema. Once the server is listening the session advertises itself in
``~/.openshot_qt/headless_mcp.json`` (see classes.mcp_discovery); the file goes
away again on exit.

It ends on ``shutdown_headless_tool`` (which can save first), SIGTERM, SIGINT
or SIGHUP. Nothing writes the project file unless a tool asked for it: autosave
and the post-indexing flush are off, and a signal exits without saving.

Nobody can answer a dialog here, so none may open. The known prompts have a
headless path of their own -- the sign-in gate exits with EXIT_NOT_SIGNED_IN,
missing media is left missing, crash recovery and the updater never run,
opening a project over unsaved changes is refused, and quitting never asks to
save. Anything else that tries to open a modal dialog is dismissed with its
conservative answer (Cancel / No / close) and reported on stderr.

The session shares ~/.openshot_qt with a desktop window that may be running
alongside, so it treats that directory as the window's: it never writes
openshot.settings, never touches the crash-detection lock file, and never
clears the shared thumbnail/title/backup scratch space.

Exit codes (see EXIT_*): 0 clean shutdown, 1 failure, 2 usage error, 3 not signed
in, 4 the project could not be opened, 5 another headless session is running.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
from typing import cast

from qt_api import QDialog, QEvent, QMessageBox, QObject, QProgressDialog, QTimer

from classes.logger import log

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2  # argparse's own code for a bad command line
EXIT_NOT_SIGNED_IN = 3
EXIT_PROJECT = 4
EXIT_ALREADY_RUNNING = 5

OFFSCREEN_PLATFORM = "offscreen"
SHUTDOWN_TOOL_NAME = "shutdown_headless_tool"
LOCK_FILE_NAME = "headless.lock"

# How long the shutdown tool's reply gets to reach the client before the
# server stops.
_REPLY_GRACE_MS = 300
# Python runs signal handlers between bytecodes on the main thread only; while
# Qt's event loop idles in C++ no bytecode runs, so a timer gives it a turn.
_SIGNAL_POLL_MS = 250
_SAVE_TIMEOUT_S = 120

_active = False
_runtime = None


def activate(environ=None) -> None:
    """Put this process in headless mode. Call before the QApplication exists:
    the platform plugin is chosen when it is constructed."""
    global _active
    env = os.environ if environ is None else environ
    previous = env.get("QT_QPA_PLATFORM")
    # Forced rather than defaulted: on a Wayland session launch.py has already
    # picked xcb, which would put the window on screen.
    env["QT_QPA_PLATFORM"] = OFFSCREEN_PLATFORM
    if previous and previous != OFFSCREEN_PLATFORM:
        log.info("Headless mode: QT_QPA_PLATFORM %s replaced with %s", previous, OFFSCREEN_PLATFORM)
    _active = True


def is_active() -> bool:
    """True in a ``launch.py --headless`` process."""
    return _active


def report(message: str) -> None:
    """One line on stderr (and in the log), prefixed so a CLI can pick it out."""
    line = "zenvi headless: %s" % message
    stream = sys.__stderr__
    if stream is not None:
        try:
            stream.write(line + "\n")
            stream.flush()
        except Exception:
            pass
    log.info(line)


def resolve_project_arg(path, project_exts):
    """Check a ``--project`` value before anything heavy starts.

    Returns (absolute path, None), (None, message) when it cannot be opened,
    or (None, None) when no project was given.
    """
    if not path:
        return None, None
    full = os.path.abspath(os.path.expanduser(path))
    if not full.endswith(tuple(project_exts)):
        return None, "--project must be a Zenvi project (%s): %s" % (", ".join(project_exts), path)
    if not os.path.isfile(full):
        return None, "project not found: %s" % full
    return full, None


def project_file_for_save(file_path, current_path, project_ext, project_exts):
    """Where a save should go: *file_path*, else the open project's own file.

    Mirrors save_project_tool: a path without a project extension gets
    *project_ext*. Returns None when there is nowhere to save (untitled, no path).
    """
    target = (file_path or "").strip() or (current_path or "")
    if not target:
        return None
    if not target.endswith(tuple(project_exts)):
        target += project_ext
    return os.path.abspath(os.path.expanduser(target))


def same_file(a, b) -> bool:
    """True when two project paths name the same file (empty never matches)."""
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def as_bool(value) -> bool:
    """MCP clients send true, "true" or 1 alike."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def plain_text(text) -> str:
    """Rich text as one plain line, for a report."""
    return " ".join(re.sub(r"<[^>]+>", " ", str(text or "")).split())


def describe_dialog(dialog) -> str:
    """'Title: text' for a report line (HTML stripped)."""
    parts = [plain_text(dialog.windowTitle())]
    for getter in ("text", "informativeText"):
        fn = getattr(dialog, getter, None)
        if callable(fn):
            parts.append(plain_text(fn()))
    return ": ".join(p for p in parts if p) or type(dialog).__name__


def conservative_button(box):
    """The button Escape would press on a QMessageBox (Cancel, the only
    button, the one reject-role or no-role button), or None.

    Clicked directly rather than by sending Escape: on macOS Escape runs an
    animated, asynchronous click, and a box closed without a clicked button
    returns NoButton -- which the unsaved-changes prompts read as "Don't Save".
    """
    explicit = box.escapeButton()
    if explicit is not None:
        return explicit
    cancel = box.button(QMessageBox.Cancel)
    if cancel is not None:
        return cancel
    buttons = box.buttons()
    if len(buttons) == 1:
        return buttons[0]
    for role in (QMessageBox.RejectRole, QMessageBox.NoRole):
        matching = [b for b in buttons if box.buttonRole(b) == role]
        if len(matching) == 1:
            return matching[0]
    return None


def dismiss_dialog(dialog) -> None:
    from qt_api import isdeleted
    if isdeleted(dialog) or not dialog.isVisible():
        return
    if isinstance(dialog, QMessageBox):
        button = conservative_button(dialog)
        if button is not None:
            button.click()
            return
    dialog.reject()


class DialogGuard(QObject):
    """Application event filter that closes every modal dialog as it opens.

    Safety net for prompts without a headless path of their own: offscreen,
    nobody could ever answer one, and its exec() would never return. Progress
    dialogs are left alone -- they do not block, and rejecting one cancels the
    work it reports on.
    """

    def eventFilter(self, obj, event):
        try:
            if (event.type() == QEvent.Show and isinstance(obj, QDialog)
                    and not isinstance(obj, QProgressDialog) and obj.isModal()):
                report("dismissed a dialog nobody can answer here: %s" % describe_dialog(obj))
                QTimer.singleShot(0, lambda: dismiss_dialog(obj))
        except Exception:
            log.debug("Headless dialog guard failed", exc_info=True)
        return False


class HeadlessRuntime(QObject):
    """Runs one headless session: checks, window, project, event loop, shutdown."""

    def __init__(self, app, project_path=None):
        super().__init__()
        self._app = app
        self._project_arg = project_path
        self._project = None
        self._guard = None
        self._signal_pump = None
        self._loop_started = False
        self._exit_request = None
        self._shutting_down = False

    # -- startup ------------------------------------------------------------
    def run(self) -> int:
        from classes import info
        project, error = resolve_project_arg(self._project_arg, info.ALL_PROJECT_EXTS)
        if error:
            report(error)
            return EXIT_PROJECT
        self._project = project
        lock = self._acquire_instance_lock()
        if lock is None:
            return EXIT_ALREADY_RUNNING
        try:
            return self._run()
        finally:
            lock.unlock()

    def _acquire_instance_lock(self):
        """One headless session per profile: they would share one discovery file.
        Returns the held QLockFile, or None when another session holds it.

        QLockFile notices a lock left by a process that no longer runs and
        takes it over, so a crashed session never blocks the next one.
        """
        from qt_api import QLockFile
        from classes import info
        lock = QLockFile(os.path.join(info.USER_PATH, LOCK_FILE_NAME))
        lock.setStaleLockTime(0)
        if lock.tryLock(0):
            return lock
        pid = None
        try:
            lock_info = lock.getLockInfo()
            if isinstance(lock_info, tuple) and len(lock_info) > 1 and lock_info[0]:
                pid = lock_info[1]
        except Exception:
            pass
        report("another headless Zenvi session is already running%s; stop it with %s or SIGTERM"
               % (" (pid %s)" % pid if pid else "", SHUTDOWN_TOOL_NAME))
        return None

    def _run(self) -> int:
        from classes import agent_mcp_server
        from classes.auth_manager import AuthManager

        # The desktop window would show its sign-in dialog here; a headless
        # session can only use the session the window stored.
        if not AuthManager.instance().is_authenticated():
            report("not signed in: open Zenvi once and sign in")
            return EXIT_NOT_SIGNED_IN

        self._install_signal_handlers()
        # Held here for the whole session: an event filter must outlive its use.
        self._guard = DialogGuard()
        self._app.installEventFilter(self._guard)
        # Before gui(): the window starts the server, and its tool list is
        # fixed when it starts.
        agent_mcp_server.register_extra_tool(SHUTDOWN_TOOL_NAME, shutdown_headless_tool)

        if not self._app.gui():
            report("the editor did not start; see %s" % _log_path())
            return EXIT_FAILURE
        # Again: libopenshot installs its own crash handlers with the window's
        # first Timeline, and the shutdown signals must stay ours.
        self._claim_signals()
        if not self._open_project():
            # Still through the event loop, so the window shuts down in order.
            self._exit_request = ("the project could not be opened", EXIT_PROJECT)

        QTimer.singleShot(0, self._on_loop_started)
        exec_fn = getattr(self._app, "exec", None) or getattr(self._app, "exec_")
        return exec_fn()

    def _open_project(self) -> bool:
        window = self._app.window
        if self._project is None:
            window._load_blank_project()
            return True
        # File > Open's own path (missing media, recents, chat rebinding).
        window.open_project(self._project)
        if not same_file(self._app.project.current_filepath, self._project):
            report("could not open %s; see %s" % (self._project, _log_path()))
            return False
        return True

    def _on_loop_started(self):
        from classes import agent_mcp_server, mcp_discovery
        self._loop_started = True
        if self._exit_request is not None:
            self._begin_shutdown()
            return
        # MainWindow started the server on this same first pass; this is a
        # no-op when that worked and a retry that raises when it did not.
        try:
            server = agent_mcp_server.get_mcp_server().start()
        except Exception as exc:
            report("the MCP server did not start: %s" % exc)
            self._app.exit(EXIT_FAILURE)
            return
        # Advertised only now, with the project open and the event loop
        # running, so a CLI that finds the file can call tools straight away.
        # "ready" is printed once the file is actually on disk.
        url, project = server.url(), self._project or "untitled"
        agent_mcp_server.enable_discovery(
            mcp_discovery.HEADLESS,
            on_written=lambda path: report("ready: MCP %s (pid %d, project %s, discovery file %s)"
                                           % (url, os.getpid(), project, path)))

    def _on_signal(self, signum, _frame):
        # A second signal gets the default action, so Ctrl+C twice still ends
        # a shutdown that hangs.
        signal.signal(signum, signal.SIG_DFL)
        self.request_shutdown(signal.Signals(signum).name)

    def _claim_signals(self):
        if self._exit_request is not None:
            return  # one already arrived; leave the default for a second one
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                signal.signal(signum, self._on_signal)
            except (OSError, RuntimeError, ValueError):
                log.debug("Could not handle %s", name, exc_info=True)

    def _install_signal_handlers(self):
        self._claim_signals()
        self._signal_pump = QTimer(self)
        self._signal_pump.timeout.connect(lambda: None)
        self._signal_pump.start(_SIGNAL_POLL_MS)

    # -- shutdown -----------------------------------------------------------
    def request_shutdown(self, reason, code=EXIT_OK, delay_ms=0):
        """Exit the event loop with *code*. GUI thread only (signal handlers
        run there; the shutdown tool marshals here)."""
        if self._exit_request is None:
            self._exit_request = (reason, code)
        if self._loop_started and not self._shutting_down:
            QTimer.singleShot(delay_ms, self._begin_shutdown)

    def _begin_shutdown(self):
        from classes.agent_mcp_server import get_mcp_server
        if self._shutting_down:
            return
        self._shutting_down = True
        reason, code = self._exit_request or ("shutdown", EXIT_OK)
        if self._app.project.needs_save():
            report("shutting down (%s); unsaved changes were not saved" % reason)
        else:
            report("shutting down (%s)" % reason)
        # First, so headless_mcp.json is gone before the editor tears down.
        get_mcp_server().stop()
        self._app.exit(code)

    def shutdown_from_tool(self, save, file_path) -> dict:
        """shutdown_headless_tool's work, on the MCP worker thread."""
        from classes import info
        from classes.qt_main_thread import call_on_gui, invoke_on_gui

        if self._shutting_down or self._exit_request is not None:
            return {"ok": True, "shutting_down": True, "saved_to": None,
                    "discarded_unsaved_changes": False, "pid": os.getpid()}
        current, dirty = cast(tuple, call_on_gui(self._project_state))
        saved_to = None
        if save:
            target = project_file_for_save(file_path, current, info.PROJECT_EXT, info.ALL_PROJECT_EXTS)
            if target is None:
                return {"ok": False, "error": "the project has never been saved: pass file_path "
                                              "to save it. The session is still running."}
            if not call_on_gui(self._save_project, target, timeout=_SAVE_TIMEOUT_S):
                return {"ok": False, "error": "saving the project to %s failed (see %s). The "
                                              "session is still running." % (target, _log_path())}
            saved_to, dirty = target, False
        invoke_on_gui(self.request_shutdown, SHUTDOWN_TOOL_NAME, EXIT_OK, _REPLY_GRACE_MS)
        return {"ok": True, "shutting_down": True, "saved_to": saved_to,
                "discarded_unsaved_changes": bool(dirty), "pid": os.getpid()}

    def _project_state(self):
        project = self._app.project
        return project.current_filepath, bool(project.needs_save())

    def _save_project(self, target) -> bool:
        # The window's own save (history, recovery zip, recents). It logs and
        # reports failures instead of raising, so check the outcome -- marked
        # dirty first, so that only a save that really wrote clears the flag
        # (a clean project saved over its own file looks the same either way).
        project = self._app.project
        project.has_unsaved_changes = True
        self._app.window.save_project(target)
        return not project.needs_save() and same_file(project.current_filepath, target)


def _log_path() -> str:
    from classes import info
    return os.path.join(info.USER_PATH, "openshot-qt.log")


def shutdown_headless_tool(save: bool = False, file_path: str = "") -> str:
    """Shut down this headless Zenvi session; the process exits right after
    replying. With save=true the project is saved first, to file_path or else to
    the file it was opened from, and a failed save leaves the session running.
    Without save the project file is not touched and unsaved changes are
    discarded (the receipt says whether there were any).

    Returns a JSON receipt: {"ok", "shutting_down", "saved_to", "discarded_unsaved_changes", "pid"},
    or {"ok": false, "error"} when nothing was shut down.
    """
    runtime = _runtime
    if runtime is None:
        return json.dumps({"ok": False, "error": "this Zenvi is not a headless session"})
    try:
        receipt = runtime.shutdown_from_tool(as_bool(save), file_path or "")
    except Exception as exc:
        log.error("%s failed", SHUTDOWN_TOOL_NAME, exc_info=True)
        receipt = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return json.dumps(receipt)


def run(app, project_path=None) -> int:
    """Run a headless session on *app* (an OpenShotApp built after activate())
    until it shuts down; returns the process exit code."""
    global _runtime
    _runtime = HeadlessRuntime(app, project_path)
    return _runtime.run()
