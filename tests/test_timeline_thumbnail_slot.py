"""The native timeline's thumbnail slot must accept the payload the thumbnail
worker emits (a QImage), or Qt refuses the connect and the app aborts at
startup with the native timeline enabled. Real-Qt test."""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication

qwidget_base = importlib.import_module("windows.views.timeline_backend.qwidget.base")
thumbnails = importlib.import_module("windows.views.timeline_backend.qwidget.thumbnails")


def _arg_types(signature: str) -> str:
    return re.search(r"\((.*)\)", signature).group(1)


def _slot_signatures(cls, name):
    """Signatures pyqtSlot registered for *name* in the class's static meta-object."""
    meta = cls.staticMetaObject
    found = []
    for index in range(meta.methodCount()):
        signature = bytes(meta.method(index).methodSignature()).decode()
        if signature.startswith(name + "("):
            found.append(signature)
    return found


def test_thumbnail_ready_slot_matches_worker_signal_signature():
    slot_args = {_arg_types(sig) for sig in _slot_signatures(qwidget_base.TimelineWidgetBase, "_handle_thumbnail_ready")}
    assert slot_args, "slot is no longer registered with pyqtSlot"

    manager_signal = _arg_types(thumbnails.TimelineThumbnailManager.thumbnail_ready.signatures[0])
    worker_signal = _arg_types(thumbnails._ThumbnailWorker.thumbnail_ready.signatures[0])

    assert worker_signal == manager_signal
    assert manager_signal in slot_args, (slot_args, manager_signal)
    assert "PyQt_PyObject" in manager_signal  # the worker hands over a QImage, not a path
