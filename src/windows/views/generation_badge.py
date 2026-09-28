"""
 @file
 @brief Generation-progress overlay for Project Files rows (ComfyUI jobs).

 Adapted from OpenShot's FilesListProgressDelegate / FilesTreeProgressDelegate
 (OpenShot PR #5932) so it plugs into Zenvi's existing IndexingBadgeDelegate
 instead of replacing the view delegates.
 """

from qt_api import Qt
from qt_api import QColor, QFontMetrics
from qt_api import QStyleOptionViewItem, QStyle

from classes.app import get_app

PLACEHOLDER_PREFIX = "__genjob__:"
ACTIVE_STATUSES = ("queued", "running", "canceling")


def is_generation_placeholder(file_id):
    """True for the synthetic Project Files rows that stand in for queued jobs."""
    return str(file_id or "").startswith(PLACEHOLDER_PREFIX)


def job_id_from_placeholder(file_id):
    file_id = str(file_id or "")
    if not is_generation_placeholder(file_id):
        return None
    return file_id.split(":", 1)[1]


def file_id_for_index(index):
    """Return the file id stored in column 5 of the row behind ``index``."""
    if index is None or not index.isValid():
        return ""
    return str(index.sibling(index.row(), 5).data(Qt.DisplayRole) or "")


def generation_badge_for_file(file_id):
    """Return the queue badge dict for ``file_id`` (or a placeholder row), else None."""
    queue = getattr(getattr(get_app(), "window", None), "generation_queue", None)
    if not file_id or queue is None:
        return None
    badge = queue.get_file_badge(file_id)
    if not badge and is_generation_placeholder(file_id):
        job = queue.get_job(job_id_from_placeholder(file_id))
        if job and job.get("status") in ACTIVE_STATUSES:
            badge = {
                "status": job.get("status"),
                "progress": int(job.get("progress", 0)),
                "label": "Queued" if job.get("status") == "queued" else "Generating",
                "job_id": job.get("id"),
            }
    return badge or None


def paint_generation_progress(delegate, painter, option, index):
    """Paint a thin progress line (and a Queued tag) over a thumbnail cell."""
    if index.column() != 0:
        return
    badge = generation_badge_for_file(file_id_for_index(index))
    if not badge:
        return

    progress = int(badge.get("progress", 0))
    status = str(badge.get("status", "")).strip().lower()
    if status in ACTIVE_STATUSES:
        # Keep active jobs visible even before numeric progress starts.
        progress = max(progress, 2)
    if progress <= 0:
        return

    opt = QStyleOptionViewItem(option)
    delegate.initStyleOption(opt, index)
    widget = opt.widget
    style = widget.style() if widget else delegate.parent().style()
    deco_rect = style.subElementRect(QStyle.SE_ItemViewItemDecoration, opt, widget)
    if not deco_rect.isValid():
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
    painter.setBrush(QColor("#283241"))
    painter.drawRect(full_rect)
    painter.setBrush(QColor("#53A0ED"))
    painter.drawRect(fill_rect)
    if status == "queued":
        label = "Queued"
        fm = QFontMetrics(painter.font())
        text_w = fm.horizontalAdvance(label)
        text_h = fm.height()
        pad_x = 5
        pad_y = 2
        badge_w = text_w + (pad_x * 2)
        badge_h = text_h + (pad_y * 2)
        badge_bottom = full_rect.top() - 3
        badge_top = max(deco_rect.top() + 3, badge_bottom - badge_h + 1)
        badge_rect = deco_rect.adjusted(3, badge_top - deco_rect.top(), 0, 0)
        badge_rect.setWidth(badge_w)
        badge_rect.setHeight(badge_h)
        painter.setBrush(QColor(18, 22, 30, 220))
        painter.drawRoundedRect(badge_rect, 4, 4)
        painter.setPen(QColor("#EAF5FF"))
        painter.drawText(badge_rect, Qt.AlignCenter, label)
    painter.restore()
