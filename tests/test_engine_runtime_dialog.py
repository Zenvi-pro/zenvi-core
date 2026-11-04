"""Zenvi Engine Tools dialog: status and install run off the GUI thread, with progress and cancel.

Real Qt only (ZENVI_REAL_QT=1). engine_runtime's status/install are fakes;
the dialog, its QThread and the queued signals back to the GUI are real.
"""

import threading
import time
import types
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt5.QtWidgets")
from PyQt5.QtWidgets import QApplication  # noqa: E402

from classes import engine_runtime  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def dialog_env(monkeypatch):
    import windows.engine_runtime_dialog as dlg_mod

    app = MagicMock()
    app._tr = lambda s: s
    monkeypatch.setattr(dlg_mod, "get_app", lambda: app)
    monkeypatch.setenv("ZENVI_ENGINE_BRIDGE", "auto")
    calls = types.SimpleNamespace(threads=[], refreshed=0, gate=threading.Event(), ready=False)

    def status():
        calls.threads.append(threading.current_thread() is threading.main_thread())
        return {"ready": calls.ready, "home": "/tmp/engine", "source": "/tmp/engine-cli.json", "node_needed": True,
                "cli_version": "0.1.0", "protocol": 2, "node": "/tmp/node", "node_version": "v24", "managed": True}

    def install(progress=None, cancel=None):
        calls.threads.append(threading.current_thread() is threading.main_thread())
        progress("Downloading Node.js 24.22.1", 1 << 20, 4 << 20)
        while not calls.gate.wait(0.01):
            if cancel.is_set():
                raise engine_runtime.InstallCancelled("Cancelled: nothing was installed")
        calls.ready = True
        return status()

    def refresh():
        calls.refreshed += 1
    monkeypatch.setattr(engine_runtime, "status", status)
    monkeypatch.setattr(engine_runtime, "install", install)
    monkeypatch.setattr("classes.tool_handlers", types.SimpleNamespace(refresh_bridged_tools=refresh), raising=False)
    return dlg_mod, calls


def test_install_shows_progress_lists_the_tools_and_runs_off_the_gui_thread(dialog_env):
    dlg_mod, calls = dialog_env
    dlg = dlg_mod.EngineRuntimeDialog()
    assert _wait(lambda: dlg._label.text().startswith("Not installed"))
    assert "Node.js 24.22.1" in dlg._label.text() and dlg._install_btn.isEnabled()
    dlg._install_btn.click()
    assert _wait(lambda: "1.0 of 4.2 MB" in dlg._progress_text.text())
    assert not dlg._close_btn.isEnabled()
    # The status check's queued end, delivered only now, leaves the running install alone.
    dlg._on_worker_done(MagicMock())
    assert dlg._busy() and not dlg._close_btn.isEnabled()
    dlg.reject()                                    # closing while installing cancels instead
    assert _wait(lambda: dlg._progress_text.text() == "Cancelled: nothing was installed")
    assert _wait(lambda: dlg._worker is None) and dlg._close_btn.isEnabled() and calls.refreshed == 0
    calls.gate.set()
    dlg._install_btn.click()
    assert _wait(lambda: dlg._label.text().startswith("Ready: zenvi CLI 0.1.0"))
    assert calls.refreshed == 1 and dlg._install_btn.text() == "Update"
    assert calls.threads == [False, False, False, False]   # status, install, install(+status): never the GUI
    assert _wait(lambda: dlg._worker is None)
    dlg.reject()


def test_an_outdated_install_says_update_not_not_installed(dialog_env, monkeypatch):
    dlg_mod, calls = dialog_env
    monkeypatch.setattr(engine_runtime, "status", lambda: {
        "ready": False, "outdated": True, "home": "/tmp/engine", "source": "/tmp/engine-cli.json",
        "node_needed": False, "protocol": 2})
    dlg = dlg_mod.EngineRuntimeDialog()
    assert _wait(lambda: "update it" in dlg._label.text())
    assert "bridge protocol (2)" in dlg._label.text() and dlg._install_btn.text() == "Update"
    assert _wait(lambda: dlg._worker is None)
    dlg.reject()


def test_close_during_the_status_check_is_instant_and_the_check_ends_detached(dialog_env, monkeypatch):
    import gc

    dlg_mod, calls = dialog_env
    slow = threading.Event()
    answered = []

    def status():
        slow.wait(10)                      # a cold ``node --version``
        answered.append(True)
        return {"ready": False, "home": "/tmp/engine", "source": "", "node_needed": False, "protocol": 2}
    monkeypatch.setattr(engine_runtime, "status", status)
    dlg = dlg_mod.EngineRuntimeDialog()
    assert dlg._busy()
    started = time.monotonic()
    dlg.reject()
    assert time.monotonic() - started < 0.5 and dlg.result() == 0
    assert dlg._worker is None and len(dlg_mod._DETACHED) == 1
    (worker,) = dlg_mod._DETACHED
    assert worker.parent() is None
    del dlg
    gc.collect()                           # the dialog goes; its running check does not take the process with it
    slow.set()
    assert _wait(lambda: answered and not dlg_mod._DETACHED)
