"""One Zenvi session owns a project file at a time (PR #216 review, #20).

A headless session and the desktop window could open the same project, and
the later save silently replaced the other session's changes.
"""

import sys
from unittest.mock import MagicMock

import pytest

# Real Qt (QLockFile): conftest keeps this file out of the stubbed suite; the
# real-qt-tests job runs it, and pytest-suite adds the main-window tests.
pytest.importorskip("PyQt5.QtCore")

from PyQt5.QtCore import QLockFile  # noqa: E402

from classes import info, project_lock  # noqa: E402


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "profile"))
    project_lock.release()
    project_lock._overridden.clear()
    yield
    project_lock.release()
    project_lock._overridden.clear()


def _other_session_holds(path):
    """Another Zenvi process holding *path* (same lock file, its own QLockFile)."""
    import os
    os.makedirs(os.path.dirname(project_lock.lock_path(path)), exist_ok=True)
    lock = QLockFile(project_lock.lock_path(path))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)
    return lock


def test_a_project_another_session_holds_cannot_be_claimed(tmp_path):
    cut = str(tmp_path / "cut.zvn")
    other = _other_session_holds(cut)
    try:
        ok, pid = project_lock.claim(cut)
        assert ok is False and pid  # the holder's process id, for the message
    finally:
        other.unlock()
    assert project_lock.claim(cut) == (True, None)


def test_opening_another_project_releases_the_first(tmp_path):
    a, b = str(tmp_path / "a.zvn"), str(tmp_path / "b.zvn")
    assert project_lock.claim(a)[0]
    assert project_lock.claim(a)[0], "claiming the project we hold is a no-op"
    assert project_lock.claim(b)[0]
    other = _other_session_holds(a)  # a is free again
    other.unlock()


def test_saving_over_a_project_another_session_holds_is_refused(tmp_path):
    cut = str(tmp_path / "cut.zvn")
    other = _other_session_holds(cut)
    try:
        ok, pid = project_lock.may_save(cut)
        assert ok is False and pid
        # The user chose "Open Anyway" for it: their saves go through.
        project_lock.override(cut)
        assert project_lock.may_save(cut) == (True, None)
    finally:
        other.unlock()
    assert project_lock.may_save(str(tmp_path / "new.zvn")) == (True, None)


def test_the_message_names_the_holder_and_the_way_out(tmp_path):
    text = project_lock.in_use_message(str(tmp_path / "cut.zvn"), 4242)
    assert "cut.zvn" in text and "4242" in text and "overwrite" in text


# --- the editor's open / save paths -----------------------------------------

def _main_window(monkeypatch):
    pytest.importorskip("openshot")
    import threading
    import types
    from unittest.mock import MagicMock

    from PyQt5.QtWidgets import QApplication

    qapp = QApplication.instance() or QApplication([])
    # As tests/test_save_project_error_dialog.py: main_window reads settings at import.
    if not hasattr(qapp, "get_settings"):
        qapp.get_settings = lambda: MagicMock(get=lambda key, default=None: default)
    if not hasattr(qapp, "_tr"):
        qapp._tr = lambda s: s
    if "windows.views.timeline" not in sys.modules:
        stub = types.ModuleType("windows.views.timeline")
        stub.TimelineView = type("TimelineView", (), {})
        sys.modules["windows.views.timeline"] = stub
    import windows.main_window as main_window

    app = MagicMock()
    app._tr = lambda s: s
    app.project.needs_save.return_value = False
    app.project.current_filepath = None
    monkeypatch.setattr(main_window, "get_app", lambda: app)
    win = MagicMock()
    win.lock = threading.Lock()
    return main_window, app, win


def test_saving_over_a_project_in_use_is_refused(tmp_path, monkeypatch):
    main_window, app, win = _main_window(monkeypatch)
    cut = str(tmp_path / "cut.zvn")
    other = _other_session_holds(cut)
    try:
        with pytest.raises(RuntimeError, match="open in another Zenvi session"):
            main_window.MainWindow.save_project(win, cut, raise_errors=True)
    finally:
        other.unlock()
    app.project.save.assert_not_called()


def test_headless_never_opens_a_project_in_use(tmp_path, monkeypatch):
    from classes import headless

    main_window, app, win = _main_window(monkeypatch)
    cut = tmp_path / "cut.zvn"
    cut.write_text("{}")
    reported = []
    monkeypatch.setattr(headless, "is_active", lambda: True)
    monkeypatch.setattr(headless, "report", reported.append)
    other = _other_session_holds(str(cut))
    try:
        assert main_window.MainWindow.open_project(win, str(cut)) is False
    finally:
        other.unlock()
    app.project.load.assert_not_called()
    assert reported and "open in another Zenvi session" in reported[0]


@pytest.mark.parametrize("answer, opens", [("Cancel", False), ("Open", True)])
def test_the_desktop_asks_before_opening_a_project_in_use(tmp_path, monkeypatch, answer, opens):
    from PyQt5.QtWidgets import QMessageBox

    from classes import headless

    main_window, app, win = _main_window(monkeypatch)
    monkeypatch.setattr(headless, "is_active", lambda: False)
    asked = []
    monkeypatch.setattr(main_window.QMessageBox, "warning", staticmethod(
        lambda *a, **k: (asked.append(a[2]), getattr(QMessageBox, answer))[1]))
    cut = tmp_path / "cut.zvn"
    cut.write_text("{}")
    class _Proceeded(Exception):
        """The open went on past the lock check (the real load needs a real window)."""

    # The next step after the lock check (a real QCursor dies under this harness).
    monkeypatch.setattr(main_window, "QCursor", MagicMock(side_effect=_Proceeded))
    other = _other_session_holds(str(cut))
    try:
        if opens:
            with pytest.raises(_Proceeded):
                main_window.MainWindow.open_project(win, str(cut))
        else:
            assert main_window.MainWindow.open_project(win, str(cut)) is False
        assert asked and "cut.zvn" in asked[0]
        # Opened anyway: this session's saves are the user's choice.
        assert project_lock.may_save(str(cut))[0] is opens
    finally:
        other.unlock()
