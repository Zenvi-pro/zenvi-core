"""
 @file
 @brief Helper for horizontal snapping.
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


class SnapHelper:
    """Compute horizontal snap offsets for dragged clips and transitions."""

    def __init__(self, widget, geometry):
        self.widget = widget
        self.geometry = geometry

    def snap_dx(self, delta_sec: float) -> float:
        """Return adjusted delta in seconds for horizontal snapping."""
        self.geometry.ensure()
        snap_px = self.widget.pixels_per_second * 1.5
        bbox = self.widget.drag_bbox
        cur_left = bbox.x() + delta_sec * self.widget.pixels_per_second
        cur_right = cur_left + bbox.width()

        best_diff_px = None
        # Consider both clips and transitions
        for rect, _ in (self.geometry.clip_rects + self.geometry.transition_rects):
            for edge in (rect.left(), rect.right()):
                for ours in (cur_left, cur_right):
                    diff = edge - ours
                    if abs(diff) <= snap_px:
                        if best_diff_px is None or abs(diff) < abs(best_diff_px):
                            best_diff_px = diff

        # Include playhead position as a snap target
        playhead_x = self.widget.track_name_width + (
            self.widget.current_frame / self.widget.fps_float
        ) * self.widget.pixels_per_second
        for ours in (cur_left, cur_right):
            diff = playhead_x - ours
            if abs(diff) <= snap_px:
                if best_diff_px is None or abs(diff) < abs(best_diff_px):
                    best_diff_px = diff
        if best_diff_px is not None:
            delta_sec += best_diff_px / self.widget.pixels_per_second
        return delta_sec

    def snap_edge(self, orig_edge_sec: float, delta_sec: float) -> float:
        """Snap a moving edge (in seconds) to nearby clip edges or playhead."""
        self.geometry.ensure()
        snap_px = self.widget.pixels_per_second * 1.5
        edge_px = (
            self.widget.track_name_width + (orig_edge_sec + delta_sec) * self.widget.pixels_per_second
        )

        best_diff_px = None
        for rect, _ in (self.geometry.clip_rects + self.geometry.transition_rects):
            for other_edge in (rect.left(), rect.right()):
                diff = other_edge - edge_px
                if abs(diff) <= snap_px:
                    if best_diff_px is None or abs(diff) < abs(best_diff_px):
                        best_diff_px = diff

        # Playhead snap target
        playhead_x = self.widget.track_name_width + (
            self.widget.current_frame / self.widget.fps_float
        ) * self.widget.pixels_per_second
        diff = playhead_x - edge_px
        if abs(diff) <= snap_px:
            if best_diff_px is None or abs(diff) < abs(best_diff_px):
                best_diff_px = diff

        if best_diff_px is not None:
            delta_sec += best_diff_px / self.widget.pixels_per_second
        return delta_sec
