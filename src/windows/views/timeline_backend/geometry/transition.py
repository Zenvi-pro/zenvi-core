"""
 @file
 @brief Transition geometry helpers for the timeline widget.
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

from classes.query import Transition


class TransitionGeometryMixin:
    """Populate cached transition rectangles."""

    def _populate_transition_rects(self, layers, ctx, win):
        w = self.widget
        overrides_map = getattr(w, "_pending_transition_overrides", {})
        entries = []
        selected_ids = set(getattr(win, "selected_transitions", []) or [])
        for tr in Transition.filter():
            tr_data = tr.data if isinstance(tr.data, dict) else {}
            override = overrides_map.get(tr.id, {})

            position = override.get("position", tr_data.get("position", 0.0))
            start = override.get("start", tr_data.get("start", 0.0))
            end = override.get("end", tr_data.get("end", start))
            layer_val = override.get("layer", tr_data.get("layer", 0))

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

            tx = (
                w.track_name_width
                + position * w.pixels_per_second
                - ctx["h_offset"]
            )
            layer_idx = layers.get(layer_key, 0)
            offset = ctx.get("track_offsets", {}).get(
                w.normalize_track_number(layer_key),
                layer_idx * ctx["spacing"],
            )
            ty = (
                w.ruler_height
                + ctx.get("top_margin", 0.0)
                + offset
                - ctx["v_offset"]
            )
            tw = (end - start) * w.pixels_per_second
            if (
                tx + tw <= w.track_name_width
                or ty + w.vertical_factor <= w.ruler_height
                or ty >= w.ruler_height + ctx["view_h"]
            ):
                continue
            rect = QRectF(tx, ty, tw, w.vertical_factor)
            entries.append((position, rect, tr))

        def _transition_sort_key(entry):
            pos, rect, tran = entry
            try:
                pos_val = float(pos)
            except (TypeError, ValueError):
                pos_val = 0.0
            return pos_val, rect.x(), getattr(tran, "id", "")

        entries.sort(key=_transition_sort_key)
        transition_entries = []
        for _, rect, tran in entries:
            is_selected = tran.id in selected_ids
            transition_entries.append((rect, tran, is_selected))
        self.transition_entries = transition_entries
