"""An assistant export renders on the calling thread; only its Qt objects live on the GUI thread."""

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

from PyQt5.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])
if not hasattr(_app, "get_settings"):
    _app.get_settings = lambda: MagicMock()

import windows.export as export  # noqa: E402


class _FakeExport:
    """Stands in for the Export dialog: records which thread did what."""

    def __init__(self):
        self.init_thread = threading.get_ident()
        self.run_thread = None
        self.deleted = False
        self.started = threading.Event()
        self.timeline = MagicMock()
        self.timeline.GetMaxFrame.return_value = 90
        made.append(self)

    def run_export(self, *args, **kwargs):
        self.run_thread = threading.get_ident()
        self.started.set()
        while self.exporting and self.hold:
            time.sleep(0.005)

    def deleteLater(self):
        self.deleted = True

    hold = False


made: list = []


@pytest.fixture
def headless(monkeypatch, tmp_path):
    made.clear()
    monkeypatch.setattr(export, "Export", _FakeExport)
    monkeypatch.setattr(_FakeExport, "hold", False)
    monkeypatch.setattr("classes.app.get_app", lambda: MagicMock(_tr=lambda s: s))
    monkeypatch.setattr(export, "get_default_export_settings", lambda: (
        {"start_frame": 1, "end_frame": 90}, {}, "Video & Audio", str(tmp_path / "default.mp4")))
    monkeypatch.setattr(export.File, "get", lambda **k: None)
    return str(tmp_path / "out.mp4")


def _export_on_a_worker(path):
    """Start export_video_headless on a worker thread, as the assistant's tool call does."""
    box = {}

    def _run():
        try:
            box["result"] = export.export_video_headless(path, {"start_frame": 1, "end_frame": 90}, {}, "Video & Audio")
        except BaseException as exc:
            box["result"] = exc

    worker = threading.Thread(target=_run, daemon=True)  # a failing test must not hang the run
    worker.start()
    return worker, box


def _pump(until, timeout=10):
    deadline = time.time() + timeout
    while not until() and time.time() < deadline:
        _app.processEvents()
        time.sleep(0.005)
    assert until(), "timed out"


def test_the_dialog_is_built_on_the_gui_thread_and_the_encode_runs_on_the_caller(headless):
    worker, box = _export_on_a_worker(headless)
    _pump(lambda: not worker.is_alive())

    assert box["result"] is None
    win = made[0]
    assert win.init_thread == threading.get_ident(), "Export is a QDialog: it must be created on the GUI thread"
    assert win.run_thread == worker.ident, "the encode must not run on the GUI thread"
    assert win._headless and win.deleted
    assert export._headless_exports == []


def test_cancelling_stops_the_render_and_is_reported_as_a_failure(headless, monkeypatch):
    """Closing the editor mid-export must not tear libopenshot down under a running encode."""
    monkeypatch.setattr(_FakeExport, "hold", True)
    worker, box = _export_on_a_worker(headless)
    _pump(lambda: made and made[0].started.is_set())
    assert export._headless_exports == [made[0]]

    assert export.cancel_headless_exports(wait_seconds=10) is True

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert isinstance(box["result"], str) and "cancel" in box["result"].lower()
    assert export._headless_exports == []
