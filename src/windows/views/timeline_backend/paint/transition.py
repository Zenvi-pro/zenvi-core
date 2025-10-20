"""
 @file
 @brief Painter for transition items.
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

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QBrush,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from .base import BasePainter


class TransitionPainter(BasePainter):
    def update_theme(self):
        self.col = self.w.theme.transition.background
        self.col2 = self.w.theme.transition.background2
        self.pen = QPen(QBrush(self.w.theme.transition.border_color), 1.5)
        self.pen.setCosmetic(True)
        self.img = self.w.theme.transition.background_image
        self.sel_pen = QPen(QBrush(self.w.theme.clip_selected), 1.5)
        self.sel_pen.setCosmetic(True)
        self.menu_pix = None
        if self.w.theme.menu_icon:
            size = self.w.theme.menu_size or self.w.theme.menu_icon.width()
            self.menu_pix = self.scaled_pixmap(self.w.theme.menu_icon, size, size)
        self.menu_margin = self.w.theme.menu_margin
        # Cache of fully rendered transition pixmaps
        self.transition_cache = {}

    def clear_cache(self):
        """Clear cached rendered transition pixmaps."""
        self.transition_cache.clear()

    def paint(self, painter: QPainter):
        area = QRectF(
            self.w.track_name_width,
            self.w.ruler_height,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        painter.save()
        painter.setClipRect(area)
        for rect, _tran, selected in self.w.geometry.iter_transitions():
            if not rect.intersects(area):
                continue
            pen = self.sel_pen if selected else self.pen
            pix = self._transition_pixmap(rect, pen)
            if pix:
                painter.drawPixmap(rect.topLeft(), pix)
        painter.restore()

    def _transition_pixmap(self, rect, pen):
        """Return cached pixmap of a transition, rendering if needed."""
        w = int(rect.width())
        h = int(rect.height())
        if w <= 0 or h <= 0:
            return None

        key = (w, h, pen.color().rgba())
        if key in self.transition_cache:
            return self.transition_cache[key]

        small = w < 20
        tiny = w < 2
        radius = self.w.theme.transition.border_radius if not small else 0

        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(0, 0, w, h)
        path = None
        if radius:
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)

        if not tiny:
            if self.col2.isValid() and self.col2 != self.col:
                grad = QLinearGradient(QPointF(0, 0), QPointF(0, h))
                grad.setColorAt(0, self.col)
                grad.setColorAt(1, self.col2)
                brush = QBrush(grad)
            else:
                brush = QBrush(self.col)

            if path is not None:
                p.fillPath(path, brush)
            else:
                p.fillRect(rect, brush)

            if self.img and not small:
                scaled = self.img.scaled(
                    w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
                )
                if path is not None:
                    p.save()
                    p.setClipPath(path)
                    p.drawPixmap(0, 0, scaled)
                    p.restore()
                else:
                    p.drawPixmap(0, 0, scaled)

        if pen.color().isValid():
            p.setPen(pen)
            if path is not None:
                p.drawPath(path)
            else:
                p.drawRect(rect)

        if self.menu_pix and not small:
            p.drawPixmap(
                QPointF(self.menu_margin, self.menu_margin),
                self.menu_pix,
            )

        p.end()

        pix = QPixmap.fromImage(img)
        self.transition_cache[key] = pix
        return pix
