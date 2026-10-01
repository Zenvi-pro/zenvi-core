"""Overlay math for inspect frames (no libopenshot)."""

from __future__ import annotations

from fractions import Fraction

from classes.agent_tools.inspect_overlay import (
    COORDINATE_GRID_NOTE,
    fit_size,
    format_frame_caption,
    format_timecode,
    label_ticks,
    major_ticks,
    minor_ticks,
)
from classes.frame_time import to_seconds


def test_tick_lists_and_grid_note():
    assert COORDINATE_GRID_NOTE == "0-1, origin top-left"
    assert minor_ticks() == [i / 20.0 for i in range(21)]
    assert major_ticks() == [0.0, 0.5, 1.0]
    assert label_ticks()[0] == 0.0 and label_ticks()[-1] == 1.0


def test_caption_at_30fps():
    # frame 148 at 30 fps → 148/30 = 4.933… → caption uses that seconds value
    seconds = to_seconds(148, Fraction(30, 1))
    assert format_frame_caption(148, seconds) == f"f148  {format_timecode(seconds)}"
    assert format_timecode(4.27) == "00:04.27"


def test_caption_framing_at_23976():
    fps = Fraction(24000, 1001)
    seconds = to_seconds(148, fps)
    cap = format_frame_caption(148, seconds)
    assert cap.startswith("f148  ")
    assert format_timecode(seconds) in cap


def test_fit_size_longest_edge():
    assert fit_size(1920, 1080, 512) == (512, 288)
    assert fit_size(400, 300, 512) == (400, 300)


def test_overlay_qpainter_optional():
    import pytest

    try:
        from PyQt5.QtGui import QColor, QImage
        from classes.agent_tools.inspect_overlay import apply_overlay_qimage
    except Exception:
        pytest.skip("PyQt5 Gui unavailable")

    fmt = getattr(QImage, "Format_RGB32", None)
    if fmt is None:
        pytest.skip("QImage stubbed")
    img = QImage(100, 100, fmt)
    if not hasattr(img, "fill") or getattr(img, "isNull", lambda: True)():
        pytest.skip("QImage stubbed")
    img.fill(QColor(200, 40, 40))
    apply_overlay_qimage(img, caption="f0  00:00.00")
    c = img.pixelColor(2, 2)
    assert c.red() < 180 or c.green() < 40
