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

import math

from classes.app import get_app


class SnapHelper:
    """Compute horizontal snap offsets for dragged clips and transitions."""

    def __init__(self, widget, geometry):
        self.widget = widget
        self.geometry = geometry

    # ---- Helpers -----------------------------------------------------
    def _h_offset(self) -> float:
        """Return current horizontal scroll offset in pixels."""
        view_w = self.widget.scrollbar_position[3] or 1.0
        timeline_w = self.widget.scrollbar_position[2] or view_w
        left = self.widget.scrollbar_position[0]
        offset = left * timeline_w
        max_scroll = max(0.0, timeline_w - view_w)
        if offset > max_scroll:
            offset = max_scroll
        return offset

    def _project_duration(self) -> float:
        app = get_app()
        if not app:
            return 0.0
        project = getattr(app, "project", None)
        if not project:
            return 0.0
        try:
            return float(project.get("duration") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _snap_tolerance_px(self) -> float:
        value = getattr(self.widget, "snap_tolerance_px", None)
        try:
            tol = float(value)
        except (TypeError, ValueError):
            tol = 12.0
        if tol <= 0.0:
            tol = 12.0
        return tol

    def _active_targets(self) -> dict:
        active = getattr(self.widget, "_snap_active_targets", None)
        if not isinstance(active, dict):
            active = {}
            self.widget._snap_active_targets = active
        return active

    def reset(self, labels=None):
        """Clear cached snap targets.

        If *labels* is provided, only the specified keys are removed.
        """

        active = getattr(self.widget, "_snap_active_targets", None)
        if not isinstance(active, dict):
            return
        if labels is None:
            active.clear()
            return
        for label in labels:
            active.pop(label, None)

    def _target_edges_px(self):
        self.geometry.ensure()
        pps = float(self.widget.pixels_per_second or 0.0)
        if pps <= 0.0:
            return []
        targets = set()
        h_offset = self._h_offset()
        left_edge = self.widget.track_name_width - h_offset

        ignore_ids = getattr(self.widget, "_snap_ignore_ids", set())
        rect_sources = (
            self.geometry.clip_rects
            + self.geometry.transition_rects
        )
        for rect, obj in rect_sources:
            obj_id = getattr(obj, "id", None)
            if obj_id in ignore_ids:
                continue
            targets.add(rect.left())
            targets.add(rect.right())

        selected_sources = (
            self.geometry.selected_rects + self.geometry.selected_transitions
        )
        for rect, obj in selected_sources:
            obj_id = getattr(obj, "id", None)
            if obj_id in ignore_ids:
                continue
            targets.add(rect.left())
            targets.add(rect.right())

        for rect in getattr(self.geometry, "marker_rects", []):
            targets.add(rect.left())

        duration = self._project_duration()
        if duration > 0.0:
            targets.add(left_edge + duration * pps)

        targets.add(left_edge)

        extra_seconds = getattr(self.widget, "_snap_keyframe_seconds", None)
        if extra_seconds:
            for value in extra_seconds:
                try:
                    sec = float(value)
                except (TypeError, ValueError):
                    continue
                targets.add(
                    self.widget.track_name_width
                    + sec * pps
                    - h_offset
                )

        playhead_x = (
            self.widget.track_name_width
            + (self.widget.current_frame / self.widget.fps_float)
            * pps
            - h_offset
        )
        targets.add(playhead_x)

        valid = []
        for value in targets:
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                valid.append(numeric)
        return valid

    def _diff_to_target(self, label: str, current_px: float, snap_px: float, targets, active):
        """Return (diff, target, reused_active) for a given cursor position."""

        if not math.isfinite(current_px):
            return None, None, False

        target = active.get(label)
        if target is not None and math.isfinite(target):
            diff = target - current_px
            if math.isfinite(diff) and abs(diff) <= snap_px:
                return diff, target, True

        best_target = None
        best_diff = None
        for candidate in targets:
            diff = candidate - current_px
            if not math.isfinite(diff):
                continue
            if abs(diff) <= snap_px:
                if best_diff is None or abs(diff) < abs(best_diff):
                    best_diff = diff
                    best_target = candidate

        if best_target is None:
            return None, None, False

        return best_diff, best_target, False

    def snap_dx(self, delta_sec: float) -> float:
        """Return adjusted delta in seconds for horizontal snapping."""
        self.geometry.ensure()
        pps = float(self.widget.pixels_per_second or 0.0)
        if pps <= 0.0:
            return delta_sec
        snap_px = self._snap_tolerance_px()
        bbox = self.widget.drag_bbox
        if not hasattr(bbox, "x"):
            return delta_sec

        targets = self._target_edges_px()
        if not targets:
            self.reset(["drag-left", "drag-right"])
            return delta_sec

        active = self._active_targets()
        start_left = bbox.x()
        width = bbox.width()
        current_positions = [
            ("drag-left", start_left + delta_sec * pps),
            ("drag-right", start_left + width + delta_sec * pps),
        ]

        chosen = None
        for label, current_px in current_positions:
            diff, target, reused = self._diff_to_target(label, current_px, snap_px, targets, active)
            if diff is None:
                continue
            priority = 0 if reused else 1
            if (
                chosen is None
                or priority < chosen[0]
                or (priority == chosen[0] and abs(diff) < abs(chosen[1]))
            ):
                chosen = (priority, diff, label, target)

        if chosen is None:
            self.reset(["drag-left", "drag-right"])
            return delta_sec

        _, diff_px, label, target_px = chosen
        active[label] = target_px
        for other in ("drag-left", "drag-right"):
            if other != label:
                active.pop(other, None)

        delta_sec += diff_px / self.widget.pixels_per_second
        return delta_sec

    def snap_edge(self, orig_edge_sec: float, delta_sec: float) -> float:
        """Snap a moving edge (in seconds) to nearby clip edges or playhead."""
        self.geometry.ensure()
        pps = float(self.widget.pixels_per_second or 0.0)
        if pps <= 0.0:
            return delta_sec
        snap_px = self._snap_tolerance_px()
        h_offset = self._h_offset()
        start_px = (
            self.widget.track_name_width
            + orig_edge_sec * self.widget.pixels_per_second
            - h_offset
        )
        edge_px = start_px + (delta_sec * self.widget.pixels_per_second)
        targets = self._target_edges_px()
        label = getattr(self.widget, "_resize_edge", None)
        if label in ("left", "right"):
            label = f"edge-{label}"
        else:
            label = "edge"

        if not targets:
            self.reset([label])
            return delta_sec

        active = self._active_targets()
        diff_px, target_px, _ = self._diff_to_target(label, edge_px, snap_px, targets, active)
        if diff_px is None:
            self.reset([label])
            return delta_sec

        active[label] = target_px
        delta_sec += diff_px / self.widget.pixels_per_second
        return delta_sec
