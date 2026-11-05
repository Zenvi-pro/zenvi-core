"""paintEvent rescales the current frame only when the frame, the target size or
the device pixel ratio changes; repaints of an unchanged frame reuse the scaled
copy. Real-Qt test; VideoWidget needs PyQt5 and libopenshot.
"""

import importlib
import os
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")
openshot = pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
importlib.import_module("qt_api")  # QtWebEngine must load before any QApplication

from PyQt5.QtCore import QMutex  # noqa: E402
from PyQt5.QtGui import QImage, QPaintEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

_APP = QApplication.instance() or QApplication([])

video_widget = importlib.import_module("windows.video_widget")
VideoWidget = video_widget.VideoWidget


class CountingImage(QImage):
    """A frame that counts how many times paintEvent rescales it."""

    scale_calls = 0

    def scaled(self, *args):
        self.scale_calls += 1
        return super().scaled(*args)


@pytest.fixture
def widget(monkeypatch):
    # paintEvent reads the project fps through get_app(); there is no live app here.
    project = types.SimpleNamespace(get=lambda key: {"num": 30, "den": 1})
    monkeypatch.setattr(video_widget, "get_app", lambda: types.SimpleNamespace(project=project))

    # VideoWidget.__init__ needs the live app, window and settings. Build the bare
    # QWidget and set only what paintEvent reads.
    w = VideoWidget.__new__(VideoWidget)
    QWidget.__init__(w)
    w.mutex = QMutex()
    w.paint_fps_sec = None
    w.paint_fps_counter = 1
    w.aspect_ratio = openshot.Fraction(16, 9)
    w.pixel_ratio = openshot.Fraction(1, 1)
    w.zoom = 1.0
    w.pan_x = 0.0
    w.pan_y = 0.0
    w.transforming_clips = []
    w.transforming_effect = None
    w.region_enabled = False
    w.current_image = None
    w._scaled_cache = None
    w.resize(160, 90)
    yield w
    w.deleteLater()


def paint(widget):
    widget.paintEvent(QPaintEvent(widget.rect()))


def test_repaint_of_same_frame_scales_once(widget):
    frame = CountingImage(640, 360, QImage.Format_RGB32)
    widget.current_image = frame
    paint(widget)
    paint(widget)
    assert frame.scale_calls == 1


def test_new_frame_or_resize_rescales_once_then_reuses(widget):
    widget.current_image = CountingImage(640, 360, QImage.Format_RGB32)
    paint(widget)

    frame = CountingImage(640, 360, QImage.Format_RGB32)
    widget.current_image = frame
    paint(widget)
    assert frame.scale_calls == 1

    widget.resize(320, 180)
    paint(widget)
    paint(widget)
    assert frame.scale_calls == 2
