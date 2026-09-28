"""
 @file
 @brief Per-file indexing badge painted on media-bin rows (list and tree)
 @author Zenvi Development Team

 @section LICENSE

 Copyright (c) 2008-2024 OpenShot Studios, LLC
 This file is part of OpenShot Video Editor (http://www.openshot.org)
"""

from qt_api import Qt, QRect, QTimer
from qt_api import QColor, QPen, QPainterPath
from qt_api import QStyle, QStyledItemDelegate, QStyleOptionViewItem, QToolTip

from classes.app import get_app
from classes.indexing_status import FAILED, PENDING, RUNNING, SKIPPED, SUCCESS
from classes.query import File
from .files_thumbnail_overlay import paint_proxy_badge

# Optimize Preview progress bar (thin line along the bottom of the thumbnail)
PROXY_BAR_COLOR = QColor("#3AA1FF")
PROXY_BAR_TRACK_COLOR = QColor("#283241")

BADGE_SIZE = 12
BADGE_MARGIN = 3

_COLORS = {
    SUCCESS: QColor("#4ade80"),
    FAILED: QColor("#ef4444"),
    RUNNING: QColor("#4d9cf6"),
    PENDING: QColor("#8a8a8a"),
    SKIPPED: QColor("#f59e0b"),
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
    elif state == SKIPPED:
        path.moveTo(x + w * 0.28, y + h * 0.5)
        path.lineTo(x + w * 0.72, y + h * 0.5)
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

    @staticmethod
    def _row_column(index, column):
        """Index of `column` on this row, unwrapping single-column view proxies.

        The thumbnail list view sits behind a proxy that exposes one column
        (upstream tabstops/accessibility work), so sibling() there cannot reach
        the hidden id/name columns; map back to a source that still has them.
        """
        model = index.model()
        while (
            index.isValid()
            and model is not None
            and model.columnCount(index.parent()) <= column
            and hasattr(model, "mapToSource")
        ):
            index = model.mapToSource(index)
            model = index.model()
        return index.sibling(index.row(), column)

    def _file_id(self, index):
        if not index.isValid():
            return ""
        id_index = self._row_column(index, 5)
        if not id_index.isValid():
            return ""
        return str(id_index.model().data(id_index, Qt.DisplayRole) or "")

    def _files_model(self):
        try:
            return get_app().window.files_model
        except Exception:
            return None

    def _status(self, index):
        files_model = self._files_model()
        if files_model is None:
            return None
        file_id = self._file_id(index)
        if not file_id:
            return None
        return files_model.file_indexing_status(file_id)

    def _any_indexing(self):
        """Aggregate state, so a finished row painted after a running one
        cannot stop the shared timer while that running badge is still on screen."""
        files_model = self._files_model()
        if files_model is None:
            return False
        try:
            return bool(files_model.has_active_indexing())
        except Exception:
            return False

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
        self.paint_proxy_state(painter, option, index)

    def paint_badge(self, painter, option, index):
        status = self._status(index)
        if status is None or not status.state:
            return
        self._sync_pulse(self._any_indexing())
        paint_status_badge(painter, badge_rect(option.rect), status.state, self._angle)

    # ── Optimize Preview (proxy) badge + progress ─────────────────────────

    def _decoration_rect(self, option, index):
        """Rect of the thumbnail (decoration) inside the row; row rect as fallback."""
        try:
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            widget = getattr(opt, "widget", None)
            style = widget.style() if widget else self.parent().style()
            deco_rect = style.subElementRect(QStyle.SE_ItemViewItemDecoration, opt, widget)
            if deco_rect.isValid():
                return deco_rect
        except Exception:
            pass
        return option.rect

    def paint_proxy_state(self, painter, option, index):
        try:
            proxy_service = getattr(get_app().window, "proxy_service", None)
        except Exception:
            proxy_service = None
        if proxy_service is None:
            return
        file_id = self._file_id(index)
        if not file_id:
            return

        job_badge = proxy_service.get_file_badge(file_id)
        file_obj = File.get(id=file_id)
        if not job_badge and (not file_obj or not proxy_service.has_proxy_reader(file_obj)):
            return

        deco_rect = self._decoration_rect(option, index)
        if file_obj:
            paint_proxy_badge(painter, deco_rect, proxy_service.get_proxy_state(file_obj))
        self._paint_proxy_progress(painter, deco_rect, job_badge)

    @staticmethod
    def _paint_proxy_progress(painter, deco_rect, badge):
        if not badge:
            return
        progress = int(badge.get("progress", 0))
        status = str(badge.get("status", "")).strip().lower()
        if status in ("queued", "running", "canceling"):
            # Keep active jobs visible even before numeric progress starts.
            progress = max(progress, 2)
        if progress <= 0:
            return

        bar_height = 3
        bar_margin = 2
        full_rect = deco_rect.adjusted(1, 0, -1, 0)
        full_rect.setTop(deco_rect.bottom() - bar_height - bar_margin + 1)
        full_rect.setHeight(bar_height)
        if full_rect.width() <= 2:
            return

        fill_width = max(1, int((full_rect.width() * min(progress, 100)) / 100.0))
        fill_rect = full_rect.adjusted(0, 0, -(full_rect.width() - fill_width), 0)

        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(PROXY_BAR_TRACK_COLOR)
        painter.drawRect(full_rect)
        painter.setBrush(PROXY_BAR_COLOR)
        painter.drawRect(fill_rect)
        painter.restore()

    def helpEvent(self, event, view, option, index):
        status = self._status(index)
        if (
            status is not None
            and status.tooltip
            and badge_rect(option.rect).contains(event.pos())
        ):
            name_index = self._row_column(index, 1)
            name = ""
            if name_index.isValid():
                name = str(name_index.model().data(name_index, Qt.DisplayRole) or "")
            text = f"{name}\n{status.tooltip}" if name else status.tooltip
            QToolTip.showText(event.globalPos(), text, view)
            return True
        return super().helpEvent(event, view, option, index)
