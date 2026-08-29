"""save_project()'s error dialog must survive being deferred across threads."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

pytest.importorskip("openshot")
pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtCore import Qt, QCoreApplication  # noqa: E402

# windows/views/timeline.py picks a WebEngine/WebKit backend at import time,
# which requires this attribute set before the QApplication is constructed.
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

from PyQt5.QtWidgets import QApplication, QMessageBox  # noqa: E402


class _Settings:
    def get(self, key, default=None):
        return default


_app = QApplication.instance() or QApplication([])
if not hasattr(_app, "get_settings"):
    _app.get_settings = lambda: _Settings()
if not hasattr(_app, "_tr"):
    _app._tr = lambda s: s

import windows.main_window as main_window  # noqa: E402


def test_save_project_error_dialog_survives_deferred_gui_dispatch(monkeypatch):
    """Regression: save_project() runs on a background thread; its except
    block used to build a closure over the `ex` exception variable and defer
    it via invoke_on_gui(). Python deletes an `except ... as ex` binding the
    moment the except block exits, so by the time the deferred callback ran
    on the GUI thread, reading `ex` raised NameError and the user never saw
    their save failure. The fix captures the message before the closure."""
    win = main_window.MainWindow.__new__(main_window.MainWindow)
    win.lock = threading.Lock()
    win.save_recovery = lambda file_path: None

    mock_app = MagicMock()
    mock_app._tr = lambda s: s
    mock_app.get_settings.return_value = _Settings()
    mock_app.project.current_filepath = None
    mock_app.project.save = MagicMock(side_effect=RuntimeError("disk full"))
    monkeypatch.setattr(main_window, "get_app", lambda: mock_app)

    captured = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: captured.append(a[-1]))
    )

    errors = []
    worker = threading.Thread(
        target=lambda: errors.append(_run(win)), daemon=True
    )
    worker.start()
    worker.join(timeout=2)
    assert worker.is_alive() is False

    deadline = time.time() + 2
    while not captured and time.time() < deadline:
        _app.processEvents()
        time.sleep(0.01)

    assert errors == [None], f"save_project raised on the worker thread: {errors}"
    assert captured == ["disk full"], (
        "the deferred warning dialog must still see the error message"
    )


def _run(win):
    try:
        win.save_project("/tmp/whatever.osp")
        return None
    except Exception as exc:  # pragma: no cover - surfaced via assertion above
        return exc
