"""
 @file
 @brief Painter for clip marker overlays.
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2025 OpenShot Studios, LLC
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

from qt_api import QColor, QFont, QFontMetrics, QPen, QPixmap, QRectF, Qt
from qt_api import QPainter

from .base import BasePainter

# Marker color tags (classes.track_ops.MARKER_COLORS). "blue" -- the Add Marker
# default -- keeps the theme's icon; other tags tint it.
MARKER_TINTS = {
    "red": "#e5484d",
    "green": "#30a46c",
    "yellow": "#f5d90a",
    "orange": "#f76b15",
    "purple": "#8e4ec6",
    "pink": "#e93d82",
    "white": "#ffffff",
}
NAME_MAX_WIDTH = 140.0


class MarkerPainter(BasePainter):
    def update_theme(self):
        pix = getattr(self.w.theme, "marker_icon", None)
        width = getattr(self.w.theme, "marker_icon_width", 0) or 0
        height = getattr(self.w.theme, "marker_icon_height", 0) or 0
        self.icon_pix = None
        if pix and not pix.isNull():
            self.icon_pix = self.scaled_pixmap(pix, width, height)
        self.icon_width, self.icon_height = self.logical_size(self.icon_pix)
        self._tinted = {}
        ruler = getattr(self.w.theme, "ruler", None)
        color = getattr(ruler, "font_color", None)
        self.name_pen = QPen(color if isinstance(color, QColor) and color.isValid() else QColor("#ffffff"))
        self.name_font = QFont()
        size = getattr(ruler, "font_size", 0) or 0
        if size:
            self.name_font.setPointSize(int(size))

    def _icon_for(self, marker_obj):
        """The marker icon, tinted by the marker's color tag."""
        data = getattr(marker_obj, "data", None) or {}
        color = str(data.get("vector") or "").strip().lower()
        tint = MARKER_TINTS.get(color)
        if not tint:
            return self.icon_pix
        cached = self._tinted.get(color)
        if cached is None:
            cached = QPixmap(self.icon_pix.size())
            cached.setDevicePixelRatio(self.icon_pix.devicePixelRatio())
            cached.fill(Qt.transparent)
            tp = QPainter(cached)
            tp.drawPixmap(0, 0, self.icon_pix)
            tp.setCompositionMode(QPainter.CompositionMode_SourceIn)
            tp.fillRect(cached.rect(), QColor(tint))
            tp.end()
            self._tinted[color] = cached
        return cached

    def paint(self, painter: QPainter):
        self.w.geometry.ensure()
        ruler_area = QRectF(
            self.w.track_name_width,
            0.0,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.ruler_height,
        )
        if not self.icon_pix or self.icon_pix.isNull():
            return

        markers = list(self.w.geometry.iter_markers())
        if not markers:
            return

        painter.save()
        painter.setClipRect(ruler_area)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        metrics = QFontMetrics(self.name_font)
        drawn = [mr for mr in markers
                 if isinstance(mr, dict) and mr.get("icon_rect") and not mr["icon_rect"].isNull()]
        drawn.sort(key=lambda mr: mr["icon_rect"].left())
        for index, mr in enumerate(drawn):
            icon_rect = mr["icon_rect"]
            marker_obj = mr.get("marker")
            painter.drawPixmap(icon_rect.topLeft(), self._icon_for(marker_obj))
            name = str((getattr(marker_obj, "data", None) or {}).get("name") or "").strip()
            if not name:
                continue
            # The name runs up to the next marker's icon, never over it.
            room = NAME_MAX_WIDTH
            if index + 1 < len(drawn):
                room = min(room, drawn[index + 1]["icon_rect"].left() - icon_rect.right() - 6.0)
            if room < 12.0:
                continue
            text = metrics.elidedText(name, Qt.ElideRight, int(room))
            painter.setFont(self.name_font)
            painter.setPen(self.name_pen)
            painter.drawText(
                QRectF(icon_rect.right() + 2.0, icon_rect.top(), room, icon_rect.height()),
                Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.restore()
