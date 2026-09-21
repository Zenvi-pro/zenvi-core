"""Storyboard sheet: dHash dedupe + coverage floor + 6-column compose."""

from __future__ import annotations

from typing import List, Optional, Sequence

TILE_W = 160
TILE_H = 90
COLUMNS = 6
MAX_TILES = 36
DEFAULT_HAMMING = 10


def dhash64(gray_8x9: bytes) -> int:
    if len(gray_8x9) < 72:
        raise ValueError("dhash64 expects at least 72 grayscale bytes (8x9)")
    value = 0
    bit = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            if gray_8x9[base + col] > gray_8x9[base + col + 1]:
                value |= 1 << bit
            bit += 1
    return value


def hamming64(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def should_keep(fingerprint: int, last_kept: Optional[int], *, threshold: int = DEFAULT_HAMMING) -> bool:
    if last_kept is None:
        return True
    return hamming64(fingerprint, last_kept) > threshold


def select_storyboard_indexes(
    fingerprints: Sequence[Optional[int]],
    timestamps: Sequence[float],
    *,
    must_keep: Optional[Sequence[bool]] = None,
    max_tiles: int = MAX_TILES,
    hamming: int = DEFAULT_HAMMING,
    coverage_floor_sec: Optional[float] = None,
) -> List[int]:
    n = len(fingerprints)
    if n == 0:
        return []
    if len(timestamps) != n:
        raise ValueError("fingerprints and timestamps length mismatch")
    flags = list(must_keep) if must_keep is not None else [False] * n
    span = float(timestamps[-1]) - float(timestamps[0]) if n > 1 else 0.0
    if coverage_floor_sec is None:
        coverage_floor_sec = max(2.0, span / 12.0) if span > 0 else 2.0

    kept: List[int] = []
    last_fp: Optional[int] = None
    last_t: Optional[float] = None

    def _append(i: int) -> None:
        nonlocal last_fp, last_t
        kept.append(i)
        fp = fingerprints[i]
        if fp is not None:
            last_fp = fp
        last_t = float(timestamps[i])

    for i in range(n):
        force = flags[i] or i == 0 or i == n - 1
        fp = fingerprints[i]
        t = float(timestamps[i])
        if force:
            _append(i)
            continue
        if last_t is not None and (t - last_t) >= coverage_floor_sec:
            _append(i)
            continue
        if fp is None:
            continue
        if should_keep(fp, last_fp, threshold=hamming):
            _append(i)

    seen = set()
    uniq = []
    for i in kept:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    kept = uniq
    if len(kept) <= max_tiles:
        return kept
    step = len(kept) / float(max_tiles)
    return [kept[int(i * step)] for i in range(max_tiles)]


def compose_sheet_qimage(tiles: Sequence, timestamps: Sequence[float]):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor, QFont, QImage, QPainter

    if not tiles:
        return None
    cols = min(COLUMNS, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    sheet = QImage(cols * TILE_W, rows * TILE_H, QImage.Format_RGB32)
    sheet.fill(QColor(0, 0, 0))
    painter = QPainter(sheet)
    font = QFont("Helvetica", 10)
    font.setBold(True)
    painter.setFont(font)
    for i, tile in enumerate(tiles):
        col = i % cols
        row = i // cols
        x = col * TILE_W
        y = row * TILE_H
        scaled = tile.scaled(TILE_W, TILE_H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        cx = max(0, (scaled.width() - TILE_W) // 2)
        cy = max(0, (scaled.height() - TILE_H) // 2)
        painter.drawImage(x, y, scaled, cx, cy, TILE_W, TILE_H)
        label = _time_label(float(timestamps[i]) if i < len(timestamps) else 0.0)
        metrics = painter.fontMetrics()
        tw = metrics.horizontalAdvance(label)
        painter.fillRect(x, y + TILE_H - 14, tw + 8, 14, QColor(0, 0, 0, 165))
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(x + 4, y + TILE_H - 3, label)
    painter.end()
    return sheet


def _time_label(t: float) -> str:
    s = int(round(t))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def gray_8x9_from_qimage(image) -> bytes:
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage

    small = image.convertToFormat(QImage.Format_Grayscale8).scaled(
        9, 8, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
    )
    bits = small.bits()
    bits.setsize(small.byteCount())
    out = bytearray(72)
    bpl = small.bytesPerLine()
    raw = bytes(bits)
    for row in range(8):
        start = row * bpl
        out[row * 9: row * 9 + 9] = raw[start: start + 9]
    return bytes(out)
