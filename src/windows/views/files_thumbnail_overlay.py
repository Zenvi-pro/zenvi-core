"""
 @file
 @brief Thumbnail tiles and overlays for the project files views (Optimize Preview badge).
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

from qt_api import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPointF
from qt_api import QRect, QRectF, QSize, QStyle, QStyledItemDelegate, Qt

from themes.cosmic.tokens import TOKENS, qcolor

TILE_INSET = 4    # half of the 8px gap between neighbouring tiles
TILE_PAD = 4      # inside the tile frame, around the thumbnail and the name
NAME_LINE = 16    # one line of 12px text
TILE_RADIUS = 8
THUMB_RADIUS = 6

# Optimize Preview bolt on a 16x16 grid (the glyph of tool-optimize-preview.svg)
_BOLT = ((8.16, 0.8), (3.92, 8.32), (7.08, 8.32), (5.92, 15.2), (12.08, 6.72), (8.76, 6.72), (10.24, 0.8))


token_color = qcolor  # kept: the media panels import it from here


def tile_grid_size(icon_size):
    """Grid cell for thumbnails of ``icon_size``: the icon, its tile padding, and 8px between tiles."""
    return QSize(icon_size.width() + 2 * (TILE_PAD + TILE_INSET),
                 icon_size.height() + 3 * TILE_PAD + NAME_LINE + 2 * TILE_INSET)


def _tile_frame(item_rect):
    return QRect(item_rect).adjusted(TILE_INSET, TILE_INSET, -TILE_INSET, -TILE_INSET)


def tile_thumb_rect(item_rect):
    """Where the thumbnail sits in a tile cell; badges and quick actions anchor to it."""
    frame = _tile_frame(item_rect)
    return QRect(frame.left() + TILE_PAD, frame.top() + TILE_PAD,
                 frame.width() - 2 * TILE_PAD, frame.height() - 3 * TILE_PAD - NAME_LINE)


def _tile_name_rect(item_rect):
    frame = _tile_frame(item_rect)
    thumb = tile_thumb_rect(item_rect)
    return QRect(frame.left() + TILE_PAD, thumb.bottom() + 1 + TILE_PAD,
                 frame.width() - 2 * TILE_PAD, NAME_LINE)


def paint_text(painter, rect, text, pixel_size, token, align=Qt.AlignLeft | Qt.AlignVCenter):
    """One line of text in a design-token color, elided to ``rect``."""
    font = QFont(painter.font())
    font.setPixelSize(pixel_size)
    painter.setFont(font)
    painter.setPen(token_color(token))
    painter.drawText(rect, align, QFontMetrics(font).elidedText(text, Qt.ElideRight, rect.width()))


def paint_caption(painter, rect, name, meta):
    """Stock card caption: the name (12px text) over its metadata (11px text_faint)."""
    paint_text(painter, QRect(rect.left(), rect.top(), rect.width(), NAME_LINE), name, 12, "text")
    paint_text(painter, QRect(rect.left(), rect.top() + NAME_LINE + 2, rect.width(), 14), meta, 11, "text_faint")


def paint_tile(painter, option, index):
    """One thumbnail tile: an 8px rounded frame that shows a 1px line only on hover
    (line_strong) or selection (accent, on accent_soft), the icon on a sunken well,
    and the name in 12px text below."""
    selected = bool(option.state & QStyle.State_Selected)
    hovered = bool(option.state & QStyle.State_MouseOver)
    frame = QRectF(_tile_frame(option.rect)).adjusted(0.5, 0.5, -0.5, -0.5)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setBrush(token_color("accent_soft") if selected else Qt.NoBrush)
    if selected or hovered:
        painter.setPen(QPen(token_color("accent" if selected else "line_strong"), 1))
    else:
        painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(frame, TILE_RADIUS, TILE_RADIUS)

    thumb = tile_thumb_rect(option.rect)
    painter.setPen(Qt.NoPen)
    painter.setBrush(token_color("sunken"))
    painter.drawRoundedRect(QRectF(thumb), THUMB_RADIUS, THUMB_RADIUS)
    icon = index.data(Qt.DecorationRole)
    if isinstance(icon, QPixmap):
        icon = QIcon(icon)
    if isinstance(icon, QIcon):
        icon.paint(painter, thumb, Qt.AlignCenter)

    # ponytail: name only; the models expose no metadata role, add an 11px text_faint line when they do.
    paint_text(painter, _tile_name_rect(option.rect), str(index.data(Qt.DisplayRole) or ""), 12, "text")
    painter.restore()


class TileDelegate(QStyledItemDelegate):
    """Draws every item of a thumbnail grid as a tile (see paint_tile)."""

    def __init__(self, view):
        super().__init__(view)
        view.setMouseTracking(True)  # hover reaches the tile only with mouse tracking

    def sizeHint(self, option, index):
        # QListView sizes items from this, capped at gridSize; the default hint was smaller
        return tile_grid_size(self.parent().iconSize())

    def paint(self, painter, option, index):
        paint_tile(painter, option, index)


def paint_proxy_badge(painter, deco_rect, proxy_state):
    """Optimize Preview mark: a muted pill with a bolt in the thumbnail's bottom-right corner."""
    proxy_state = str(proxy_state or "").strip().lower()
    if proxy_state not in ("ready", "missing"):
        return
    if not deco_rect or not deco_rect.isValid():
        return

    size = max(14.0, min(deco_rect.width(), deco_rect.height()) * 0.24)
    pill = QRectF(deco_rect.right() - size - 1.5, deco_rect.bottom() - size - 4.0, size, size)
    scale = size * 0.6 / 16.0
    origin = pill.center() - QPointF(8 * scale, 8 * scale)
    points = [QPointF(origin.x() + x * scale, origin.y() + y * scale) for x, y in _BOLT]
    bolt = QPainterPath()
    bolt.moveTo(points[0])
    for point in points[1:]:
        bolt.lineTo(point)
    bolt.closeSubpath()

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(token_color("raised"))
    painter.drawEllipse(pill)
    painter.setBrush(token_color("text_muted" if proxy_state == "ready" else "text_faint"))
    painter.drawPath(bolt)
    painter.restore()
