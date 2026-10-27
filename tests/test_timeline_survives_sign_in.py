"""The timeline is not deleted when the sign-in dialog closes (Log Out, sign in again).

Log Out hides the main window and shows the sign-in dialog. When that dialog
closes, Qt says the last window closed, and TimelineView answered by deleting
itself. Its thumbnail thread was still running, so the process aborted with
"QThread: Destroyed while thread is still running" right after
"[zenvi-auth] login successful".

Real-Qt test with libopenshot: it runs the real TimelineView handler against a
stand-in for the timeline.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402


class _TestApp(QApplication):
    """Minimal stand-in for classes.app.OpenShotApp at module import time."""

    def _tr(self, text):
        return text


@pytest.fixture(scope="module")
def timeline_module():
    # qt_api imports QtWebEngine, which Qt requires before any QApplication exists.
    importlib.import_module("qt_api")
    app = QApplication.instance()
    if app is None:
        app = _TestApp([])
    elif not hasattr(app, "_tr"):
        app._tr = lambda text: text
    parked = sys.modules.get("windows.views.timeline")
    if parked is not None and not hasattr(getattr(parked, "TimelineView", None), "addClip"):
        # test_save_project_error_dialog.py parks a stand-in under this name.
        del sys.modules["windows.views.timeline"]
    return importlib.import_module("windows.views.timeline")


class _Timeline:
    """What the handler touches: the main window's flag and deleteLater."""

    def __init__(self, shutting_down):
        self.window = types.SimpleNamespace(shutting_down=shutting_down)
        self.deleted = 0

    def deleteLater(self):
        self.deleted += 1


def test_the_timeline_survives_the_sign_in_dialog_closing(timeline_module):
    timeline = _Timeline(shutting_down=False)

    timeline_module.TimelineView._delete_on_shutdown(timeline)

    assert timeline.deleted == 0


def test_the_timeline_is_still_deleted_when_the_editor_shuts_down(timeline_module):
    timeline = _Timeline(shutting_down=True)

    timeline_module.TimelineView._delete_on_shutdown(timeline)

    assert timeline.deleted == 1


def test_last_window_closed_is_not_wired_straight_to_delete(timeline_module):
    source = inspect.getsource(timeline_module.TimelineView.__init__)

    assert "lastWindowClosed.connect(self._delete_on_shutdown)" in source
    assert "lastWindowClosed.connect(self.deleteLater)" not in source
