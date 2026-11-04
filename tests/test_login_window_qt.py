"""The sign-in dialog with real Qt: closing it any way never leaves a callback behind.

Run with ZENVI_REAL_QT=1 (auto-skipped under the headless Qt stub). Escape
used to close the dialog without cancelling its browser poll; when the poll
finished (or timed out) it signalled the deleted dialog and Zenvi crashed.
"""

import gc
import threading
import time

import pytest

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from classes import auth_manager as am  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeAuth:
    """Records the poll so the test can answer it after the dialog is gone."""

    def __init__(self):
        self.callbacks = None
        self.cancelled = []

    def start_auth_flow(self):
        return "https://zenvi.invalid/login?state=s&v=2", "s"

    def poll_for_session(self, state, on_success, on_timeout, **kwargs):
        self.callbacks = (on_success, on_timeout)
        handle = threading.Event()
        return handle

    def cancel_poll(self, handle=None):
        self.cancelled.append(handle)


def _spin(qapp, seconds=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


@pytest.mark.parametrize("close", ["escape", "cancel", "close"])
def test_a_poll_that_finishes_after_the_dialog_closed_is_ignored(qapp, monkeypatch, close):
    fake = FakeAuth()
    monkeypatch.setattr(am.AuthManager, "_instance", fake)
    from windows.login_window import LoginWindow

    dlg = LoginWindow(parent=None)
    dlg.show()
    _spin(qapp)
    assert fake.callbacks is not None, "the dialog started the browser sign-in"
    on_success, on_timeout = fake.callbacks

    if close == "escape":
        QTest.keyClick(dlg, Qt.Key_Escape)
    elif close == "cancel":
        dlg._cancel()
    else:
        dlg.close()
    _spin(qapp)
    del dlg
    gc.collect()
    _spin(qapp)

    assert fake.cancelled, "closing the dialog cancels its poll"
    # The poll answers late, from its own thread, as the real one does.
    threading.Thread(target=lambda: (on_timeout(), on_success({"access_token": "x"}))).start()
    _spin(qapp, 0.5)  # a crash here was the bug


def test_the_dialog_shows_the_confirmation_code(qapp, monkeypatch):
    fake = FakeAuth()
    monkeypatch.setattr(am.AuthManager, "_instance", fake)
    from windows.login_window import LoginWindow

    dlg = LoginWindow(parent=None)
    dlg.show()
    _spin(qapp, 1.8)  # the waiting text appears after 1.4 s
    assert am.confirmation_code("s") in dlg._browser_sub.text()
    dlg._cancel()
    _spin(qapp)


def test_a_sign_in_that_ended_waits_for_a_click(qapp, monkeypatch):
    fake = FakeAuth()
    monkeypatch.setattr(am.AuthManager, "_instance", fake)
    from windows.login_window import LoginWindow

    dlg = LoginWindow(parent=None, auto_open_browser=False, message="Your Zenvi sign-in has ended")
    dlg.show()
    _spin(qapp)
    assert fake.callbacks is None, "no browser tab until the user asks"
    dlg._browser_btn.click()
    _spin(qapp)
    assert fake.callbacks is not None
    dlg._cancel()
    _spin(qapp)
