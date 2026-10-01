"""launch.py --headless (classes.headless): startup checks, the dialog guard,
shutdown and shutdown_headless_tool -- with a fake app, no Qt event loop."""

import json
import os
import types

import pytest

from classes import headless


@pytest.fixture(autouse=True)
def _reset_headless(monkeypatch):
    monkeypatch.setattr(headless, "_active", False)
    monkeypatch.setattr(headless, "_runtime", None)
    reported = []
    monkeypatch.setattr(headless, "report", reported.append)
    return reported


@pytest.fixture
def reported(_reset_headless):
    return _reset_headless


class FakeProject:
    def __init__(self, path=None, dirty=False):
        self.current_filepath = path
        self.has_unsaved_changes = dirty

    @property
    def dirty(self):
        return self.has_unsaved_changes

    @dirty.setter
    def dirty(self, value):
        self.has_unsaved_changes = value

    def needs_save(self):
        return self.has_unsaved_changes


class FakeWindow:
    def __init__(self, project, save_works=True):
        self.project = project
        self.save_works = save_works
        self.saved = []
        self.opened = []
        self.blank = 0

    def save_project(self, path):
        self.saved.append(path)
        if self.save_works:
            self.project.current_filepath = path
            self.project.dirty = False

    def open_project(self, path):
        self.opened.append(path)
        if os.path.exists(path):
            self.project.current_filepath = path

    def _load_blank_project(self):
        self.blank += 1


class FakeApp:
    def __init__(self, project=None, save_works=True):
        self.project = project or FakeProject()
        self.window = FakeWindow(self.project, save_works)
        self.exit_codes = []
        self.filters = []

    def exit(self, code):
        self.exit_codes.append(code)

    def installEventFilter(self, obj):
        self.filters.append(obj)


class FakeServer:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


@pytest.fixture
def fake_server(monkeypatch):
    import classes.agent_mcp_server as srv_mod
    server = FakeServer()
    monkeypatch.setattr(srv_mod, "get_mcp_server", lambda: server)
    return server


@pytest.fixture
def timers(monkeypatch):
    """Collect QTimer.singleShot calls instead of running an event loop."""
    shots = []
    monkeypatch.setattr(headless, "QTimer",
                        types.SimpleNamespace(singleShot=lambda ms, fn: shots.append((ms, fn))))
    return shots


def _runtime(app, **kw):
    runtime = headless.HeadlessRuntime(app, **kw)
    headless._runtime = runtime
    return runtime


# --- mode switch --------------------------------------------------------------

def test_activate_forces_the_offscreen_platform():
    env = {"QT_QPA_PLATFORM": "xcb"}
    headless.activate(env)
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    assert headless.is_active()


# --- argument checks ------------------------------------------------------------

EXTS = (".zvn", ".osp", ".flow")


def test_project_arg_checks(tmp_path):
    project = tmp_path / "cut.zvn"
    project.write_text("{}", encoding="utf-8")
    assert headless.resolve_project_arg(None, EXTS) == (None, None)
    assert headless.resolve_project_arg(str(project), EXTS) == (str(project), None)

    path, error = headless.resolve_project_arg(str(tmp_path / "gone.zvn"), EXTS)
    assert path is None and error.startswith("project not found:")
    path, error = headless.resolve_project_arg(str(tmp_path / "clip.mp4"), EXTS)
    assert path is None and "must be a Zenvi project" in error


def test_relative_project_arg_is_made_absolute(tmp_path, monkeypatch):
    (tmp_path / "cut.osp").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert headless.resolve_project_arg("cut.osp", EXTS) == (str(tmp_path / "cut.osp"), None)


def test_save_target_rules(tmp_path):
    cut = str(tmp_path / "cut.zvn")
    assert headless.project_file_for_save("", cut, ".zvn", EXTS) == cut
    assert headless.project_file_for_save(str(tmp_path / "new"), cut, ".zvn", EXTS) == str(tmp_path / "new.zvn")
    assert headless.project_file_for_save(str(tmp_path / "x.osp"), None, ".zvn", EXTS) == str(tmp_path / "x.osp")
    assert headless.project_file_for_save("  ", None, ".zvn", EXTS) is None


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), ("true", True), ("True", True), ("1", True),
    ("false", False), ("", False), (0, False), (1, True), (None, False),
])
def test_as_bool(value, expected):
    assert headless.as_bool(value) is expected


# --- startup exit codes -------------------------------------------------------------

class FakeLock:
    def __init__(self, acquires=True, pid=4242):
        self.acquires = acquires
        self.pid = pid
        self.unlocked = False

    def setStaleLockTime(self, _ms):
        pass

    def tryLock(self, _ms):
        return self.acquires

    def getLockInfo(self):
        return (True, self.pid, "host", "Python")

    def unlock(self):
        self.unlocked = True


@pytest.fixture
def lock(monkeypatch, tmp_path):
    import qt_api
    from classes import info
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))
    holder = {"lock": FakeLock()}
    monkeypatch.setattr(qt_api, "QLockFile", lambda path: holder["lock"], raising=False)
    return holder


@pytest.fixture
def signed_in(monkeypatch):
    from classes import auth_manager
    state = {"ok": True}
    fake = types.SimpleNamespace(is_authenticated=lambda: state["ok"])
    monkeypatch.setattr(auth_manager.AuthManager, "instance", classmethod(lambda cls: fake))
    return state


def test_bad_project_exits_before_anything_starts(reported, lock):
    runtime = _runtime(FakeApp(), project_path="/nowhere/cut.zvn")
    assert runtime.run() == headless.EXIT_PROJECT
    assert reported == ["project not found: /nowhere/cut.zvn"]


def test_second_headless_session_is_refused(reported, lock, signed_in):
    lock["lock"] = FakeLock(acquires=False, pid=777)
    assert _runtime(FakeApp()).run() == headless.EXIT_ALREADY_RUNNING
    assert "already running (pid 777)" in reported[0]


def test_not_signed_in_exits_with_its_own_code(reported, lock, signed_in):
    signed_in["ok"] = False
    app = FakeApp()
    assert _runtime(app).run() == headless.EXIT_NOT_SIGNED_IN
    assert reported == ["not signed in: open Zenvi once and sign in"]
    assert lock["lock"].unlocked
    assert app.filters == []  # nothing else was set up


def _no_real_signals(runtime, monkeypatch, calls=None):
    # Never touch the test process's own signal handlers.
    log = calls if calls is not None else []
    monkeypatch.setattr(runtime, "_install_signal_handlers", lambda: log.append("install"))
    monkeypatch.setattr(runtime, "_claim_signals", lambda: log.append("claim"))
    return log


def test_signals_are_claimed_again_once_the_window_exists(
        reported, lock, signed_in, timers, monkeypatch):
    # libopenshot installs its own handlers with the window's first Timeline.
    app = FakeApp()
    order = []
    app.gui = lambda: order.append("gui") or True
    app.exec_ = lambda: 0
    runtime = _runtime(app)
    _no_real_signals(runtime, monkeypatch, order)
    runtime.run()
    assert order == ["install", "gui", "claim"]


def test_startup_opens_the_project_and_registers_the_shutdown_tool(
        reported, lock, signed_in, timers, tmp_path, monkeypatch):
    import classes.agent_mcp_server as srv_mod
    monkeypatch.setattr(srv_mod, "_REGISTERED_EXTRA_TOOLS", {})
    project = tmp_path / "cut.zvn"
    project.write_text("{}", encoding="utf-8")

    app = FakeApp()
    app.gui = lambda: True
    app.exec_ = lambda: 0
    runtime = _runtime(app, project_path=str(project))
    _no_real_signals(runtime, monkeypatch)

    assert runtime.run() == 0
    assert app.window.opened == [str(project)]
    assert srv_mod._REGISTERED_EXTRA_TOOLS == {"shutdown_headless_tool": headless.shutdown_headless_tool}
    assert isinstance(app.filters[0], headless.DialogGuard)
    assert [ms for ms, _fn in timers] == [0]  # _on_loop_started queued
    assert runtime._exit_request is None


def test_untitled_session_starts_blank(reported, lock, signed_in, timers, monkeypatch):
    app = FakeApp()
    app.gui = lambda: True
    app.exec_ = lambda: 0
    runtime = _runtime(app)
    _no_real_signals(runtime, monkeypatch)
    assert runtime.run() == 0
    assert app.window.blank == 1


def test_a_project_that_does_not_load_shuts_down_through_the_loop(
        reported, lock, signed_in, timers, tmp_path, monkeypatch):
    project = tmp_path / "cut.zvn"
    project.write_text("{}", encoding="utf-8")
    app = FakeApp()
    app.gui = lambda: True
    app.exec_ = lambda: 0
    app.window.open_project = lambda path: None  # load failed; still untitled
    runtime = _runtime(app, project_path=str(project))
    _no_real_signals(runtime, monkeypatch)

    runtime.run()
    assert "could not open" in reported[0]
    assert runtime._exit_request == ("the project could not be opened", headless.EXIT_PROJECT)


def test_loop_start_advertises_the_session(reported, monkeypatch, tmp_path):
    import classes.agent_mcp_server as srv_mod
    started, enabled = [], []
    server = types.SimpleNamespace(start=lambda: started.append(1) or server,
                                   url=lambda: "http://127.0.0.1:7434/mcp")
    monkeypatch.setattr(srv_mod, "get_mcp_server", lambda: server)
    callbacks = []

    def fake_enable(kind, on_written=None):
        enabled.append(kind)
        callbacks.append(on_written)
        return str(tmp_path / "headless_mcp.json")

    monkeypatch.setattr(srv_mod, "enable_discovery", fake_enable)
    runtime = _runtime(FakeApp())
    runtime._on_loop_started()
    assert started == [1] and enabled == ["headless"]
    # "ready" waits until the file is on disk.
    assert not any(line.startswith("ready") for line in reported)
    callbacks[0](str(tmp_path / "headless_mcp.json"))
    assert reported[-1].startswith("ready: MCP http://127.0.0.1:7434/mcp")
    assert reported[-1].endswith("discovery file %s)" % (tmp_path / "headless_mcp.json"))


def test_loop_start_fails_the_session_when_mcp_cannot_start(reported, monkeypatch):
    import classes.agent_mcp_server as srv_mod

    def boom():
        raise RuntimeError("MCP server failed to bind")

    monkeypatch.setattr(srv_mod, "get_mcp_server", lambda: types.SimpleNamespace(start=boom))
    app = FakeApp()
    _runtime(app)._on_loop_started()
    assert app.exit_codes == [headless.EXIT_FAILURE]
    assert "did not start" in reported[-1]


# --- shutdown -----------------------------------------------------------------------

def test_signal_before_the_loop_runs_is_kept_until_it_does(fake_server, timers, reported):
    app = FakeApp()
    runtime = _runtime(app)
    runtime.request_shutdown("SIGTERM")
    assert timers == [] and app.exit_codes == []

    runtime._on_loop_started()
    assert fake_server.stopped == 1
    assert app.exit_codes == [0]
    assert reported == ["shutting down (SIGTERM)"]


def test_shutdown_stops_mcp_first_and_says_what_was_discarded(fake_server, timers, reported):
    app = FakeApp(FakeProject("/p/cut.zvn", dirty=True))
    runtime = _runtime(app)
    runtime._loop_started = True
    runtime.request_shutdown("SIGINT")
    runtime.request_shutdown("SIGTERM")  # a second request changes nothing
    assert [ms for ms, _fn in timers] == [0, 0]
    for _ms, fn in timers:
        fn()
    assert fake_server.stopped == 1
    assert app.exit_codes == [0]
    assert reported == ["shutting down (SIGINT); unsaved changes were not saved"]
    assert app.window.saved == []  # a signal never saves


def _tool(**kwargs):
    return json.loads(headless.shutdown_headless_tool(**kwargs))


def test_tool_outside_a_headless_session():
    assert _tool() == {"ok": False, "error": "this Zenvi is not a headless session"}


def test_tool_without_save_discards_and_shuts_down(fake_server, timers):
    app = FakeApp(FakeProject("/p/cut.zvn", dirty=True))
    runtime = _runtime(app)
    runtime._loop_started = True

    receipt = _tool()
    assert receipt == {"ok": True, "shutting_down": True, "saved_to": None,
                       "discarded_unsaved_changes": True, "pid": os.getpid()}
    assert app.window.saved == []
    # The reply goes out before the server stops.
    assert timers[0][0] == headless._REPLY_GRACE_MS
    timers[0][1]()
    assert fake_server.stopped == 1 and app.exit_codes == [0]


def test_tool_saves_to_the_opened_file(fake_server, timers, tmp_path):
    cut = str(tmp_path / "cut.zvn")
    app = FakeApp(FakeProject(cut, dirty=True))
    runtime = _runtime(app)
    runtime._loop_started = True

    receipt = _tool(save=True)
    assert receipt["ok"] is True and receipt["saved_to"] == cut
    assert receipt["discarded_unsaved_changes"] is False
    assert app.window.saved == [cut]
    assert len(timers) == 1


def test_tool_saves_to_a_new_path_and_accepts_string_booleans(fake_server, timers, tmp_path):
    app = FakeApp(FakeProject(None, dirty=True))
    runtime = _runtime(app)
    runtime._loop_started = True

    receipt = _tool(save="true", file_path=str(tmp_path / "fresh"))
    assert receipt["saved_to"] == str(tmp_path / "fresh.zvn")
    assert app.window.saved == [str(tmp_path / "fresh.zvn")]


def test_untitled_save_without_a_path_keeps_the_session(fake_server, timers):
    app = FakeApp(FakeProject(None, dirty=True))
    runtime = _runtime(app)
    runtime._loop_started = True

    receipt = _tool(save=True)
    assert receipt["ok"] is False and "never been saved" in receipt["error"]
    assert timers == [] and fake_server.stopped == 0


def test_failed_save_keeps_the_session(fake_server, timers, tmp_path):
    app = FakeApp(FakeProject(str(tmp_path / "cut.zvn"), dirty=True), save_works=False)
    runtime = _runtime(app)
    runtime._loop_started = True

    receipt = _tool(save=True)
    assert receipt["ok"] is False and "failed" in receipt["error"]
    assert "still running" in receipt["error"]
    assert timers == []


def test_failed_save_of_a_clean_project_over_its_own_file_is_caught(fake_server, timers, tmp_path):
    # Path and dirty flag are unchanged whether the write worked or not.
    cut = str(tmp_path / "cut.zvn")
    app = FakeApp(FakeProject(cut, dirty=False), save_works=False)
    runtime = _runtime(app)
    runtime._loop_started = True

    receipt = _tool(save=True)
    assert receipt["ok"] is False and "failed" in receipt["error"]
    assert timers == []


def test_tool_after_shutdown_began_is_harmless(fake_server, timers):
    runtime = _runtime(FakeApp())
    runtime._loop_started = True
    runtime.request_shutdown("SIGTERM")
    receipt = _tool(save=True)
    assert receipt["ok"] is True and receipt["shutting_down"] is True
    assert receipt["saved_to"] is None


def test_tool_schema_and_description():
    from classes.agent_mcp_server import _build_input_schema, _first_doc_paragraph
    schema = _build_input_schema(headless.shutdown_headless_tool)
    assert schema["properties"] == {"save": {"type": "boolean"}, "file_path": {"type": "string"}}
    assert "required" not in schema
    description = _first_doc_paragraph(headless.shutdown_headless_tool)
    assert "Shut down this headless Zenvi session" in description
    assert "save=true" in description and "discarded" in description


# --- the dialog guard ------------------------------------------------------------------

class _Button:
    def __init__(self, name):
        self.name = name
        self.clicked = 0

    def click(self):
        self.clicked += 1


def _qt_names(monkeypatch):
    class QDialog:
        pass

    class QProgressDialog(QDialog):
        pass

    class QMessageBox(QDialog):
        Cancel, RejectRole, NoRole, AcceptRole, YesRole = "Cancel", "Reject", "No", "Accept", "Yes"

    monkeypatch.setattr(headless, "QDialog", QDialog)
    monkeypatch.setattr(headless, "QProgressDialog", QProgressDialog)
    monkeypatch.setattr(headless, "QMessageBox", QMessageBox)
    monkeypatch.setattr(headless, "QEvent", types.SimpleNamespace(Show="Show"))
    return QDialog, QProgressDialog, QMessageBox


def _box(cls, buttons, cancel=None, escape=None):
    box = cls()
    box.escapeButton = lambda: escape
    box.button = lambda which: cancel if which == "Cancel" else None
    box.buttons = lambda: [b for b, _role in buttons]
    box.buttonRole = lambda b: dict((id(k), r) for k, r in buttons)[id(b)]
    return box


def test_conservative_button_matches_escape(monkeypatch):
    _QDialog, _QProgressDialog, QMessageBox = _qt_names(monkeypatch)
    yes, no, cancel, ok = _Button("yes"), _Button("no"), _Button("cancel"), _Button("ok")

    # Save changes? Yes / No / Cancel -> Cancel (never "Don't Save").
    box = _box(QMessageBox, [(yes, "Yes"), (no, "No"), (cancel, "Reject")], cancel=cancel)
    assert headless.conservative_button(box) is cancel
    # Replace the file? Yes / No -> No.
    assert headless.conservative_button(_box(QMessageBox, [(yes, "Yes"), (no, "No")])) is no
    # A lone OK.
    assert headless.conservative_button(_box(QMessageBox, [(ok, "Accept")])) is ok
    # Explicit escape button wins.
    assert headless.conservative_button(_box(QMessageBox, [(yes, "Yes")], escape=no)) is no
    # Skip all / Locate folder: nothing safe to press.
    skip, locate = _Button("skip"), _Button("locate")
    assert headless.conservative_button(_box(QMessageBox, [(skip, "Accept"), (locate, "Action")])) is None


def test_guard_dismisses_modal_dialogs_only(monkeypatch, timers, reported):
    QDialog, QProgressDialog, QMessageBox = _qt_names(monkeypatch)
    guard = headless.DialogGuard()
    show = types.SimpleNamespace(type=lambda: "Show")
    other = types.SimpleNamespace(type=lambda: "Paint")

    box = QMessageBox()
    box.isModal = lambda: True
    box.windowTitle = lambda: "Unsaved Changes"
    box.text = lambda: "<b>Save</b> changes to project first?"
    box.informativeText = lambda: ""
    assert guard.eventFilter(box, other) is False
    assert timers == []
    assert guard.eventFilter(box, show) is False
    assert reported == ["dismissed a dialog nobody can answer here: "
                        "Unsaved Changes: Save changes to project first?"]
    assert len(timers) == 1

    progress = QProgressDialog()
    progress.isModal = lambda: True
    modeless = QDialog()
    modeless.isModal = lambda: False
    guard.eventFilter(progress, show)
    guard.eventFilter(modeless, show)
    guard.eventFilter(object(), show)
    assert len(timers) == 1


def test_dismiss_clicks_the_safe_button_or_rejects(monkeypatch):
    import qt_api
    QDialog, _QProgressDialog, QMessageBox = _qt_names(monkeypatch)
    monkeypatch.setattr(qt_api, "isdeleted", lambda obj: False, raising=False)

    cancel = _Button("cancel")
    box = _box(QMessageBox, [(cancel, "Reject")], cancel=cancel)
    box.isVisible = lambda: True
    headless.dismiss_dialog(box)
    assert cancel.clicked == 1

    rejected = []
    dialog = QDialog()
    dialog.isVisible = lambda: True
    dialog.reject = lambda: rejected.append(1)
    headless.dismiss_dialog(dialog)
    assert rejected == [1]

    dialog.isVisible = lambda: False  # already answered
    headless.dismiss_dialog(dialog)
    assert rejected == [1]


def test_startup_errors_go_to_stderr_not_a_message_box(monkeypatch, reported):
    from classes import app as app_mod
    monkeypatch.setattr(headless, "_active", True)
    boxes = []
    monkeypatch.setattr(app_mod.StartupError, "levels", {"warning": boxes.append, "error": boxes.append})

    app_mod.StartupError("Settings Error", "Error loading <b>x</b>", level="warning").show()
    assert reported == ["Settings Error: Error loading x"]
    with pytest.raises(SystemExit) as exc:
        app_mod.StartupError("Import Error", "no openshot", level="error").show()
    assert exc.value.code == 1
    assert boxes == []


def test_activate_gives_the_session_its_own_title_folder(monkeypatch, tmp_path):
    """A desktop window clears ~/.openshot_qt/title when it opens a project; an
    untitled headless project's title SVGs must not be in there."""
    from classes import info
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))
    monkeypatch.setattr(info, "TITLE_PATH", str(tmp_path / "title"))
    monkeypatch.setitem(info._path_defaults, "TITLE_PATH", str(tmp_path / "title"))
    headless.activate({})
    assert info.TITLE_PATH == os.path.join(str(tmp_path), "title-headless")
    assert info.get_default_path("TITLE_PATH") == info.TITLE_PATH  # survives reset_userdirs()
