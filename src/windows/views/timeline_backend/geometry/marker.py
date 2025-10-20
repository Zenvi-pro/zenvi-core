"""
 @file
 @brief Marker geometry helpers for the timeline widget.
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

from classes.query import Marker


class MarkerGeometryMixin:
    """Populate cached marker rectangles."""

    def _populate_marker_rects(self, ctx):
        w = self.widget
        top_margin = ctx.get("top_margin", 0.0)
        height = max(0.0, ctx["content_h"] - top_margin)
        for marker in Marker.filter():
            mx = (
                w.track_name_width
                + marker.data.get("position", 0.0) * w.pixels_per_second
                - ctx["h_offset"]
            )
            rect = QRectF(
                mx,
                w.ruler_height + top_margin - ctx["v_offset"],
                0.5,
                height,
            )
            self.marker_rects.append(rect)
