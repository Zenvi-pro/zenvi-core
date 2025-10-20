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

from PyQt5.QtCore import QRectF
from PyQt5.QtGui import QBrush, QPainter, QPen

from .base import BasePainter


class MarkerPainter(BasePainter):
    def update_theme(self):
        self.pen = QPen(QBrush(self.w.theme.ruler.border_color), 1.0)
        self.pen.setCosmetic(True)

    def paint(self, painter: QPainter):
        area = QRectF(
            self.w.track_name_width,
            self.w.ruler_height,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        painter.save()
        painter.setPen(self.pen)
        for mr in self.w.geometry.marker_rects:
            vis = mr.intersected(area)
            if vis.isNull():
                continue
            painter.drawRect(vis)
        painter.restore()
