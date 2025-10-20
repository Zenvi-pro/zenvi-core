"""
 @file
 @brief Clip geometry helpers for the timeline widget.
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

from classes.query import Clip


class ClipGeometryMixin:
    """Populate cached clip rectangles."""

    def _populate_clip_rects(self, layers, ctx, win):
        w = self.widget
        overrides_map = getattr(w, "_pending_clip_overrides", {})
        entries = []
        selected_ids = set(getattr(win, "selected_clips", []) or [])
        for clip in Clip.filter():
            clip_data = clip.data if isinstance(clip.data, dict) else {}
            override = overrides_map.get(clip.id, {})

            position = override.get("position", clip_data.get("position", 0.0))
            start = override.get("start", clip_data.get("start", 0.0))
            end = override.get("end", clip_data.get("end", start))
            layer_val = override.get("layer", clip_data.get("layer", 0))

            try:
                position = float(position)
            except (TypeError, ValueError):
                position = 0.0
            try:
                start = float(start)
            except (TypeError, ValueError):
                start = 0.0
            try:
                end = float(end)
            except (TypeError, ValueError):
                end = start
            if end < start:
                end = start
            try:
                layer_key = int(layer_val)
            except (TypeError, ValueError):
                layer_key = layer_val

            cx = (
                w.track_name_width
                + position * w.pixels_per_second
                - ctx["h_offset"]
            )
            layer_idx = layers.get(layer_key, 0)
            offset = ctx.get("track_offsets", {}).get(
                w.normalize_track_number(layer_key),
                layer_idx * ctx["spacing"],
            )
            cy = (
                w.ruler_height
                + ctx.get("top_margin", 0.0)
                + offset
                - ctx["v_offset"]
            )
            cw = (end - start) * w.pixels_per_second
            if (
                cx + cw <= w.track_name_width
                or cy + w.vertical_factor <= w.ruler_height
                or cy >= w.ruler_height + ctx["view_h"]
            ):
                continue
            rect = QRectF(cx, cy, cw, w.vertical_factor)
            entries.append((position, rect, clip))

        def _clip_sort_key(entry):
            pos, rect, clip = entry
            try:
                pos_val = float(pos)
            except (TypeError, ValueError):
                pos_val = 0.0
            return pos_val, rect.x(), getattr(clip, "id", "")

        entries.sort(key=_clip_sort_key)
        clip_entries = []
        for _, rect, clip in entries:
            is_selected = clip.id in selected_ids
            clip_entries.append((rect, clip, is_selected))
        self.clip_entries = clip_entries
