"""GUI-thread marshal helper (macOS / Windows / Linux)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.qt_main_thread import call_on_gui, invoke_on_gui, is_gui_thread  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt5.QtWidgets")
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_is_gui_thread_on_the_app_thread(qapp):
    assert is_gui_thread() is True


def test_invoke_on_gui_runs_immediately_on_the_gui_thread(qapp):
    seen = []
    invoke_on_gui(seen.append, "ok", context=qapp)
    assert seen == ["ok"]


def test_call_on_gui_runs_immediately_on_the_gui_thread(qapp):
    assert call_on_gui(lambda: 7, context=qapp) == 7


def test_invoke_on_gui_from_a_worker_runs_on_the_app_thread(qapp):
    from PyQt5.QtCore import QThread

    app_thread = qapp.thread()
    seen = []

    def work():
        def mark():
            seen.append(QThread.currentThread() is app_thread)

        invoke_on_gui(mark, context=qapp)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(timeout=2)
    assert worker.is_alive() is False

    deadline = time.time() + 2
    while not seen and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert seen == [True]
