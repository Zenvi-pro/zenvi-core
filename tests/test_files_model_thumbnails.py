"""Project Files thumbnails are made on the thumbnail worker, never the GUI thread.

Runs the real FilesModel thumbnail methods against a real
TimelineThumbnailManager worker thread, with GetThumbPath faked to be slow the
way libopenshot 1.0 is on long-GOP, stream-copy-trimmed media (it used to run
on the GUI thread during import and froze the editor for over a minute).
Real-Qt test: the files model subclasses QThread at import time, so this needs
ZENVI_REAL_QT=1; the stubbed suite auto-ignores importorskip files.
"""

from __future__ import annotations

import functools
import importlib
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication

from qt_api import (  # noqa: E402
    QApplication, QColor, QCoreApplication, QEventLoop, QImage, QPersistentModelIndex,
    QStandardItem, QStandardItemModel, Qt,
)

files_model_module = importlib.import_module("windows.models.files_model")
thumbnails_module = importlib.import_module("windows.views.timeline_backend.qwidget.thumbnails")
FilesModel = files_model_module.FilesModel

SLOW_DECODE_S = 0.8


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 50)
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Files:
    """Stand-in for classes.query.File: no project needed."""

    def __init__(self, files):
        self.files = files

    def get(self, id=None, **_kwargs):
        return self.files.get(id)


@pytest.fixture
def files(qapp, monkeypatch, tmp_path):
    png = tmp_path / "thumb.png"
    image = QImage(40, 26, QImage.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(str(png))

    main_thread = threading.get_ident()
    calls = []

    def slow_get_thumb_path(file_id, frame, clear_cache=False, attempts=1):
        calls.append({"file_id": file_id, "frame": frame, "clear_cache": clear_cache,
                      "attempts": attempts, "on_gui_thread": threading.get_ident() == main_thread})
        time.sleep(SLOW_DECODE_S)  # the thumbnail server decoding the frame
        return str(png)

    monkeypatch.setattr(thumbnails_module, "GetThumbPath", slow_get_thumb_path)
    monkeypatch.setattr(thumbnails_module, "existing_thumb_path", lambda *a, **k: "")
    monkeypatch.setattr(thumbnails_module, "prewarmed_thumb_path", lambda *a, **k: "")
    records = {"F1": SimpleNamespace(id="F1", data={
        "id": "F1", "path": "/media/trimmed_longgop.mp4", "media_type": "video",
        "fps": {"num": 25, "den": 1}})}
    monkeypatch.setattr(files_model_module, "File", _Files(records))

    manager = thumbnails_module.TimelineThumbnailManager(
        max_pending=None, attempts=FilesModel.PROJECT_FILE_THUMB_ATTEMPTS,
        thread_name="test_project_files_thumbnail")
    # The FilesModel state the thumbnail methods use, on a plain namespace:
    # constructing the real model needs the whole main window.
    model = SimpleNamespace(
        model=QStandardItemModel(), model_ids={}, ignore_updates=False, thumbnails=manager,
        _thumbnail_generation=0, _thumbnail_callbacks={}, _status_cache={},
        ModelRefreshed=SimpleNamespace(emit=lambda: None),
        _tooltip_for_file=lambda file, name: name,
        _thumbnail_frame_for_file=FilesModel._thumbnail_frame_for_file,
        _pending_thumbnail_icon=FilesModel._pending_thumbnail_icon,
    )
    for name in ("request_thumbnail", "_request_file_thumbnail", "_on_thumbnail_ready",
                 "_project_file_icon_for_file", "thumbnail_icon", "update_file_thumbnail",
                 "_update_file_thumbnail"):
        setattr(model, name, functools.partial(getattr(FilesModel, name), model))
    manager.thumbnail_ready.connect(model._on_thumbnail_ready, type=Qt.QueuedConnection)
    try:
        yield SimpleNamespace(model=model, calls=calls, records=records)
    finally:
        manager.shutdown()


def _add_row(model, file):
    """What FilesModel.update_model does for a new file."""
    icon, name, _media_type = model._project_file_icon_for_file(file)
    row = [QStandardItem(icon, name)] + [QStandardItem("") for _ in range(4)] + [QStandardItem(file.id)]
    model.model.appendRow(row)
    model.model_ids[file.id] = QPersistentModelIndex(row[5].index())
    return row[0]


def _is_red(icon):
    image = icon.pixmap(40, 26).toImage()
    return not image.isNull() and QColor(image.pixel(image.width() // 2, image.height() // 2)) == QColor("red")


def test_new_row_shows_a_placeholder_without_waiting_for_the_server(files):
    placeholder = FilesModel._pending_thumbnail_icon()
    assert not placeholder.isNull()

    started = time.monotonic()
    item = _add_row(files.model, files.records["F1"])
    assert time.monotonic() - started < SLOW_DECODE_S / 2
    assert item.icon().cacheKey() == placeholder.cacheKey()

    assert _pump_until(lambda: _is_red(item.icon()))
    assert files.calls == [{"file_id": "F1", "frame": 1, "clear_cache": False,
                            "attempts": FilesModel.PROJECT_FILE_THUMB_ATTEMPTS, "on_gui_thread": False}]
    assert _is_red(files.model.thumbnail_icon("F1"))


def test_a_thumbnail_arriving_is_not_an_edit(files):
    # The views save the file (an undo step) and emit FileUpdated -- which
    # regenerates the thumbnail -- on any itemChanged unless ignore_updates
    # is set; an arriving icon must not start that loop.
    seen = []
    files.model.model.itemChanged.connect(lambda _item: seen.append(files.model.ignore_updates))
    item = _add_row(files.model, files.records["F1"])

    assert _pump_until(lambda: _is_red(item.icon()))
    assert seen and all(seen)
    assert files.model.ignore_updates is False


def test_results_for_an_old_project_or_an_old_frame_are_dropped(files):
    item = _add_row(files.model, files.records["F1"])
    placeholder_key = item.icon().cacheKey()
    red = QImage(40, 26, QImage.Format_RGB32)
    red.fill(QColor("red"))

    files.model._on_thumbnail_ready("F1", 1, red, files.model._thumbnail_generation - 1)
    files.model._on_thumbnail_ready("F1", 99, red, files.model._thumbnail_generation)
    assert item.icon().cacheKey() == placeholder_key

    files.model._on_thumbnail_ready("F1", 1, QImage(), files.model._thumbnail_generation)
    assert item.icon().cacheKey() == placeholder_key  # a failed thumbnail keeps the placeholder


def test_file_updated_regenerates_off_the_gui_thread_and_keeps_the_current_icon(files):
    item = _add_row(files.model, files.records["F1"])
    assert _pump_until(lambda: _is_red(item.icon()))
    shown = item.icon().cacheKey()

    started = time.monotonic()
    files.model.update_file_thumbnail("F1")
    assert time.monotonic() - started < SLOW_DECODE_S / 2
    assert item.icon().cacheKey() == shown  # no flash back to the placeholder

    assert _pump_until(lambda: len(files.calls) == 2 and item.icon().cacheKey() != shown)
    assert files.calls[1]["clear_cache"] is True
    assert files.calls[1]["on_gui_thread"] is False
    assert _is_red(item.icon())


QUIT_WHILE_DECODING = r"""
import importlib.util, os, sys, threading, time, types
sys.path.insert(0, os.environ["ZENVI_SRC"])
sys.modules.setdefault("openshot", types.ModuleType("openshot"))  # no decoding here
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import qt_api
from qt_api import QApplication, QTimer
app = QApplication([])
spec = importlib.util.spec_from_file_location(
    "thumbnails", os.path.join(os.environ["ZENVI_SRC"], "windows/views/timeline_backend/qwidget/thumbnails.py"))
thumbnails = importlib.util.module_from_spec(spec)
spec.loader.exec_module(thumbnails)

started = threading.Event()
def still_decoding(file_id, frame, **kwargs):
    started.set()
    time.sleep(30)  # the thumbnail server is still decoding when the user quits
    return ""
thumbnails.GetThumbPath = still_decoding
thumbnails.existing_thumb_path = lambda *a, **k: ""
thumbnails.prewarmed_thumb_path = lambda *a, **k: ""

manager = thumbnails.TimelineThumbnailManager(max_pending=None)
manager.request_thumbnail("F1", "F1", 1, 0)
manager.request_thumbnail("F2", "F2", 1, 0)

def quit_once_stuck():
    if not started.is_set():
        QTimer.singleShot(50, quit_once_stuck)
        return
    t0 = time.monotonic()
    manager.shutdown()
    print("shutdown_s=%.1f" % (time.monotonic() - t0), flush=True)
    app.quit()

QTimer.singleShot(0, quit_once_stuck)
app.exec_()
sys.exit(0)
"""


def test_quitting_while_a_thumbnail_is_still_decoding_exits_cleanly(qapp):
    # The UI no longer freezes during a slow thumbnail, so the user can quit
    # in the middle of one. Destroying the still-running worker QThread used
    # to abort the process at exit (status 134).
    import subprocess

    proc = subprocess.run([sys.executable, "-c", QUIT_WHILE_DECODING],
                          env=dict(os.environ, ZENVI_SRC=str(SRC)),
                          capture_output=True, text=True, timeout=120)

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "Destroyed while thread is still running" not in proc.stderr
    assert "shutdown_s=" in proc.stdout
    assert float(proc.stdout.split("shutdown_s=")[1].split()[0]) < 5


def test_other_views_get_their_thumbnail_on_the_gui_thread(files):
    main_thread = threading.get_ident()
    got = []
    files.model.request_thumbnail(
        "selection-menu:C1", "F1", 5,
        on_ready=lambda image: got.append((image.isNull(), threading.get_ident() == main_thread)))

    assert _pump_until(lambda: got)
    assert got == [(False, True)]
    assert files.calls[0]["frame"] == 5 and files.calls[0]["on_gui_thread"] is False
    assert files.model._thumbnail_callbacks == {}
