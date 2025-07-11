"""
 @file
 @brief Painter classes for timeline layers.
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

from PyQt5.QtCore import QPointF
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush


class ClipPainter:
    def __init__(self, widget):
        self.w = widget

    def paint(self, painter: QPainter):
        clip_pen = QPen(QBrush(self.w.theme["clip_border"]), 1.5)
        clip_pen.setCosmetic(True)
        sel_pen = QPen(QBrush(self.w.theme["clip_selected"]), 1.5)
        sel_pen.setCosmetic(True)

        for rect, clip in self.w.geometry.clip_rects:
            painter.fillRect(rect, self.w.theme["clip_bg"])
            painter.setPen(clip_pen)
            painter.drawRect(rect)
            painter.setPen(self.w.theme["clip_text"])
            painter.drawText(rect.adjusted(2, 2, -2, -2),
                             self.w._clip_text_flags,
                             clip.data.get("title", ""))

        for rect, clip in self.w.geometry.selected_rects:
            painter.fillRect(rect, self.w.theme["clip_bg"])
            painter.setPen(sel_pen)
            painter.drawRect(rect)
            painter.setPen(self.w.theme["clip_text"])
            painter.drawText(rect.adjusted(2, 2, -2, -2),
                             self.w._clip_text_flags,
                             clip.data.get("title", ""))


class TransitionPainter:
    def __init__(self, widget):
        self.w = widget

    def paint(self, painter: QPainter):
        blue = QColor("#4b73ff")
        blue_pen = QPen(QBrush(blue), 1.5)
        blue_pen.setCosmetic(True)
        sel_pen = QPen(QBrush(self.w.theme["clip_selected"]), 1.5)
        sel_pen.setCosmetic(True)

        for rect, tr in self.w.geometry.transition_rects:
            painter.fillRect(rect, blue)
            painter.setPen(blue_pen)
            painter.drawRect(rect)

        for rect, tr in self.w.geometry.selected_transitions:
            painter.fillRect(rect, blue)
            painter.setPen(sel_pen)
            painter.drawRect(rect)


class MarkerPainter:
    def __init__(self, widget):
        self.w = widget

    def paint(self, painter: QPainter):
        painter.setPen(self.w.theme["ruler_tick"])
        for mr in self.w.geometry.marker_rects:
            painter.drawRect(mr)


class PlayheadPainter:
    def __init__(self, widget):
        self.w = widget

    def paint(self, painter: QPainter):
        playhead_col = QColor(self.w.theme["playhead"])
        playhead_col.setAlphaF(0.5)
        pen = QPen(QBrush(playhead_col), 2.0)
        pen.setCosmetic(True)
        x = self.w.track_name_width + (
            self.w.current_frame / self.w.fps_float) * self.w.pixels_per_second
        painter.setPen(pen)
        painter.drawLine(QPointF(x, 0), QPointF(x, self.w.height()))

