"""Audio-only clips paint a fallback thumbnail; the painter must not NameError on it."""

from __future__ import annotations

import os

import pytest

from windows.views.timeline_backend.paint.clip import ClipPainter


@pytest.fixture(scope="module")
def qapp():
    # Under the headless stub QPixmap is a MagicMock; with ZENVI_REAL_QT=1 a
    # real QPixmap needs a QGuiApplication first.
    if os.environ.get("ZENVI_REAL_QT") != "1":
        yield None
        return
    from PyQt5.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


class _StubPainter:
    def __init__(self):
        self.thumb_cache = {}

    _audio_thumbnail_pixmap = ClipPainter._audio_thumbnail_pixmap


def test_audio_thumbnail_pixmap_builds_and_caches_the_fallback(qapp):
    painter = _StubPainter()

    # Used to raise NameError: name 'os' is not defined on every paint of an
    # audio-only clip (the port dropped paint/clip.py's `import os`).
    painter._audio_thumbnail_pixmap()

    assert ("_fallback_", "audio") in painter.thumb_cache
