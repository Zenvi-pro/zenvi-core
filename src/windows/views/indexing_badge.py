"""
 @file
 @brief Per-file indexing badge painted on media-bin rows (list and tree)
 @author Zenvi Development Team

 @section LICENSE

 Copyright (c) 2008-2024 OpenShot Studios, LLC
 This file is part of OpenShot Video Editor (http://www.openshot.org)
"""

from PyQt5.QtCore import Qt, QRect, QTimer
from PyQt5.QtGui import QColor, QPen, QPainterPath
from PyQt5.QtWidgets import QStyledItemDelegate, QToolTip

from classes.app import get_app
from classes.indexing_status import FAILED, PENDING, RUNNING, SUCCESS

BADGE_SIZE = 12
BADGE_MARGIN = 3

_COLORS = {
    SUCCESS: QColor("#4ade80"),
    FAILED: QColor("#ef4444"),
    RUNNING: QColor("#4d9cf6"),
    PENDING: QColor("#8a8a8a"),
}


def badge_rect(item_rect):
    """Top-right corner square of a media-bin row."""
    return QRect(
        item_rect.right() - BADGE_SIZE - BADGE_MARGIN,
        item_rect.top() + BADGE_MARGIN,
        BADGE_SIZE,
        BADGE_SIZE,
    )


def paint_status_badge(painter, rect, state, angle=0):
    """Draw the status mark for one file. No-op for files with no status."""
    color = _COLORS.get(state)
    if color is None:
        return

    painter.save()
    painter.setRenderHint(painter.Antialiasing, True)

    if state == RUNNING:
        # Rotating arc, driven by the view's pulse timer
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(rect.adjusted(1, 1, -1, -1), -angle * 16, 270 * 16)
        painter.restore()
        return

    if state == PENDING:
        painter.setPen(QPen(color, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        painter.restore()
        return

    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(rect)

    pen = QPen(QColor("#0d0d0d"), 1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    path = QPainterPath()
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    if state == SUCCESS:
        path.moveTo(x + w * 0.27, y + h * 0.52)
        path.lineTo(x + w * 0.44, y + h * 0.70)
        path.lineTo(x + w * 0.75, y + h * 0.32)
    else:
        path.moveTo(x + w * 0.32, y + h * 0.32)
        path.lineTo(x + w * 0.68, y + h * 0.68)
        path.moveTo(x + w * 0.68, y + h * 0.32)
        path.lineTo(x + w * 0.32, y + h * 0.68)
    painter.drawPath(path)
    painter.restore()


class IndexingBadgeDelegate(QStyledItemDelegate):
    """Paints the indexing badge on every media-bin row, live.

    Status comes from FilesModel (cached), so paint stays cheap. A single
    pulse timer animates the in-progress arc and only runs while something
    is actually indexing.
    """

    PULSE_MS = 90

    def __init__(self, view):
        super().__init__(view)
        self._angle = 0
        self._pulse = QTimer(view)
        self._pulse.setInterval(self.PULSE_MS)
        self._pulse.timeout.connect(self._on_pulse)

    # ── status lookup ─────────────────────────────────────────────────────

    def _file_id(self, index):
        if not index.isValid():
            return ""
        model = index.model()
        return str(model.data(index.sibling(index.row(), 5), Qt.DisplayRole) or "")

    def _status(self, index):
        try:
            files_model = get_app().window.files_model
        except Exception:
            return None
        file_id = self._file_id(index)
        if not file_id:
            return None
        return files_model.file_indexing_status(file_id)

    # ── pulse ─────────────────────────────────────────────────────────────

    def _on_pulse(self):
        self._angle = (self._angle + 30) % 360
        view = self.parent()
        if view is not None:
            view.viewport().update()

    def _sync_pulse(self, running):
        if running and not self._pulse.isActive():
            self._pulse.start()
        elif not running and self._pulse.isActive():
            self._pulse.stop()

    # ── painting / tooltip ────────────────────────────────────────────────

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        self.paint_badge(painter, option, index)

    def paint_badge(self, painter, option, index):
        status = self._status(index)
        if status is None or not status.state:
            return
        self._sync_pulse(status.state == RUNNING)
        paint_status_badge(painter, badge_rect(option.rect), status.state, self._angle)

    def helpEvent(self, event, view, option, index):
        status = self._status(index)
        if status is not None and status.tooltip:
            name = str(index.model().data(index.sibling(index.row(), 1), Qt.DisplayRole) or "")
            text = f"{name}\n{status.tooltip}" if name else status.tooltip
            QToolTip.showText(event.globalPos(), text, view)
            return True
        return super().helpEvent(event, view, option, index)
