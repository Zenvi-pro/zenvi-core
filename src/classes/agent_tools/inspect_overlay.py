"""0-1 coordinate grid overlay math + caption formatting for inspect frames."""

from __future__ import annotations

from typing import List, Sequence, Tuple

COORDINATE_GRID_NOTE = "0-1, origin top-left"


def minor_ticks() -> List[float]:
    return [i / 20.0 for i in range(21)]


def major_ticks() -> List[float]:
    return [0.0, 0.5, 1.0]


def label_ticks() -> List[float]:
    return [i / 10.0 for i in range(11)]


def format_timecode(seconds: float) -> str:
    s = max(0.0, float(seconds))
    whole = int(s)
    cent = int(round((s - whole) * 100))
    if cent >= 100:
        whole += 1
        cent = 0
    mm, ss = divmod(whole, 60)
    return f"{mm:02d}:{ss:02d}.{cent:02d}"


def format_frame_caption(project_frame: int, seconds: float) -> str:
    return f"f{int(project_frame)}  {format_timecode(seconds)}"


def fit_size(width: int, height: int, longest_edge: int = 512) -> Tuple[int, int]:
    w, h = max(1, int(width)), max(1, int(height))
    longest = max(w, h)
    if longest <= longest_edge:
        return w, h
    scale = longest_edge / float(longest)
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def apply_overlay_qimage(image, caption: str | None = None):
    from PyQt5.QtGui import QColor, QFont, QPainter, QPen

    if image is None or image.isNull():
        return image
    w, h = image.width(), image.height()
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, False)

    def _stroke(width: float, gray: int, alpha: int, ticks: Sequence[float], major: bool) -> None:
        pen = QPen(QColor(gray, gray, gray, alpha))
        pen.setWidthF(width)
        painter.setPen(pen)
        for i, t in enumerate(ticks):
            is_major = (i % 10 == 0)
            if is_major != major:
                continue
            x = min(max(0.5, t * w), w - 0.5)
            painter.drawLine(int(x), 0, int(x), h)
            y = min(max(0.5, t * h), h - 0.5)
            painter.drawLine(0, int(y), w, int(y))

    ticks = minor_ticks()
    _stroke(2.0, 0, 140, ticks, major=False)
    _stroke(1.0, 255, 190, ticks, major=False)
    _stroke(2.5, 0, 165, ticks, major=True)
    _stroke(1.5, 255, 240, ticks, major=True)

    font_size = max(8, min(11, min(w, h) // 42))
    font = QFont("Helvetica", font_size)
    font.setBold(True)
    painter.setFont(font)

    def _chip(text: str, x: float, y: float) -> None:
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(text)
        th = metrics.height()
        painter.fillRect(int(x), int(y), tw + 4, th + 3, QColor(0, 0, 0, 165))
        painter.setPen(QColor(255, 255, 255, 255))
        painter.drawText(int(x) + 2, int(y) + th, text)

    for t in label_ticks():
        label = "0" if t == 0 else ("1" if t == 1 else f"{t:.1f}")
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(label)
        if t != 1.0:
            x = min(max(0, t * w - tw / 2), w - tw - 2)
            _chip(label, x, 2)
        y = min(max(2, t * h - metrics.height() / 2), h - metrics.height() - 4)
        _chip(label, w - tw - 6, y)

    if caption:
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(caption)
        th = metrics.height()
        painter.fillRect(0, 0, tw + 10, th + 4, QColor(0, 0, 0, 165))
        painter.setPen(QColor(255, 255, 255, 255))
        painter.drawText(5, th, caption)
    painter.end()
    return image
