"""Timeline marker icons and the playback cache bar stay pinned to the ruler
while the tracks scroll vertically (OpenShot #6042). Real-Qt test."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtGui")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QRectF  # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from windows.views.timeline_backend.geometry.base import GeometryBase  # noqa: E402
from windows.views.timeline_backend.paint.cache import PlaybackCachePainter  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_marker_icon_viewport_ignores_vertical_track_scroll():
    widget = types.SimpleNamespace(
        track_name_width=140.0,
        ruler_height=40.0,
        scroll_bar_thickness=15.0,
        scrollbar_position=[0.2, 0.7, 500.0, 250.0],
        v_scrollbar_position=[0.25, 0.5, 400.0, 100.0],
        width=lambda: 405.0,
        height=lambda: 155.0,
        h_scroll_offset=0.0,
    )
    geometry = GeometryBase(widget)
    geometry.marker_rects = [{
        "line_rect": QRectF(190.0, 40.0, 0.5, 400.0),
        "icon_rect": QRectF(184.0, 26.0, 12.0, 14.0),
        "hit_rect": QRectF(180.0, 22.0, 20.0, 22.0),
    }]

    marker = next(geometry.iter_markers())

    assert marker["icon_rect"].x() == pytest.approx(84.0)
    assert marker["icon_rect"].y() == pytest.approx(26.0)  # not shifted by the vertical scroll
    assert marker["hit_rect"].x() == pytest.approx(80.0)
    assert marker["hit_rect"].y() == pytest.approx(22.0)
    assert widget.h_scroll_offset == pytest.approx(100.0)


def test_playback_cache_paints_fixed_lane_background(qapp):
    image = QImage(140, 80, QImage.Format_ARGB32)
    image.fill(QColor("#00ff00"))
    widget = types.SimpleNamespace(
        theme=types.SimpleNamespace(
            playback_cache_color=QColor("#0000ff"),
            playback_cache_height=5.0,
            track=types.SimpleNamespace(background=QColor("#ff0000"), background2=QColor()),
        ),
        _playback_cache_ranges=[(0.0, 50.0)],
        pixels_per_second=1.0,
        track_name_width=20.0,
        ruler_height=10.0,
        scroll_bar_thickness=0.0,
        track_margin_top=8.0,
        h_scroll_offset=0.0,
        width=lambda: 140,
        height=lambda: 80,
    )
    cache_painter = PlaybackCachePainter(widget)

    painter = QPainter(image)
    try:
        cache_painter.paint(painter)
    finally:
        painter.end()

    assert image.pixelColor(30, 12).name() == "#0000ff"  # cached range bar
    assert image.pixelColor(90, 12).name() == "#ff0000"  # lane background beyond the range
    assert image.pixelColor(30, 16).name() == "#ff0000"  # lane keeps the track background below the bar
    assert image.pixelColor(30, 19).name() == "#00ff00"  # nothing painted outside the lane
