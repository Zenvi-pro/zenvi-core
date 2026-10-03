"""File > Collect Media / Reclaim Duplicate Media, driven through the window's handlers.

Review #216: the copy and the full-file hashing froze the editor, reclaim
deleted the copies before the project that pointed at them was saved, and a
detached worker let the user edit or close the project mid-operation.
"""

import os
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _qt_support import skip_without_pyqt5  # noqa: E402

skip_without_pyqt5()
pytest.importorskip("openshot")

from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture
def window(tmp_path, monkeypatch):
    qapp = QApplication.instance() or QApplication([])
    if not hasattr(qapp, "get_settings"):
        qapp.get_settings = lambda: MagicMock(get=lambda key, default=None: default)
    if not hasattr(qapp, "_tr"):
        qapp._tr = lambda s: s
    if "windows.views.timeline" not in sys.modules:
        stub = types.ModuleType("windows.views.timeline")
        stub.TimelineView = type("TimelineView", (), {})
        sys.modules["windows.views.timeline"] = stub
    import windows.main_window as main_window
    from classes import info

    monkeypatch.setattr(info, "PATH", str(tmp_path / "app"))
    outside = tmp_path / "Downloads" / "clip.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"video-bytes")
    data = {"files": [{"id": "f1", "path": str(outside)}],
            "clips": [{"id": "c1", "file_id": "f1", "reader": {"path": str(outside)}}]}
    app = MagicMock()
    app._tr = lambda s: s
    app.project._data = data
    app.project.current_filepath = str(tmp_path / "Proj.zvn")
    monkeypatch.setattr(main_window, "get_app", lambda: app)
    # A real QCursor dies under this harness; the handlers only pass it on.
    monkeypatch.setattr(main_window, "QCursor", MagicMock())
    shown = []
    monkeypatch.setattr(main_window.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[2])))
    win = MagicMock()
    return types.SimpleNamespace(main_window=main_window, win=win, app=app, data=data,
                                 outside=str(outside), shown=shown)


def test_collect_copies_off_the_gui_thread_then_repoints(window, monkeypatch):
    from classes import media_collect

    copy_threads = []
    real_copy = media_collect.shutil.copy2
    monkeypatch.setattr(media_collect.shutil, "copy2",
                        lambda *a, **k: (copy_threads.append(threading.current_thread()), real_copy(*a, **k))[1])
    window.main_window.MainWindow.actionCollectMedia_trigger(window.win)

    collected = window.data["files"][0]["path"]
    assert collected != window.outside and os.path.isfile(collected)
    assert window.data["clips"][0]["reader"]["path"] == collected
    assert copy_threads and threading.main_thread() not in copy_threads
    assert window.shown and "Copied 1" in window.shown[0]


def test_reclaim_saves_before_deleting_and_only_writes_the_project_on_the_gui_thread(window):
    window.main_window.MainWindow.actionCollectMedia_trigger(window.win)
    collected = window.data["files"][0]["path"]
    saves = []

    def save_project(path, raise_errors=False):
        saves.append((window.data["files"][0]["path"], os.path.isfile(collected),
                      threading.current_thread() is threading.main_thread()))

    window.win.save_project = save_project
    window.main_window.MainWindow.actionReclaimMedia_trigger(window.win)

    # Saved pointing at the original while the copy still existed, off the GUI thread.
    assert saves == [(window.outside, True, False)]
    assert not os.path.exists(collected)
    assert window.data["clips"][0]["reader"]["path"] == window.outside
    assert "Removed 1" in window.shown[-1]


def test_reclaim_keeps_everything_when_the_save_fails(window):
    window.main_window.MainWindow.actionCollectMedia_trigger(window.win)
    collected = window.data["files"][0]["path"]

    def save_project(path, raise_errors=False):
        raise OSError("disk full")

    window.win.save_project = save_project
    window.main_window.MainWindow.actionReclaimMedia_trigger(window.win)
    assert os.path.isfile(collected)
    assert window.data["files"][0]["path"] == collected
    assert "Removed 0" in window.shown[-1] and "Errors: 1" in window.shown[-1]
