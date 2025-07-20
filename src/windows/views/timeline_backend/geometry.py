"""
 @file
 @brief Geometry caching helpers for the experimental timeline widget.
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

from PyQt5.QtCore import QPointF, QRectF

from classes.app import get_app
from classes.query import Clip, Track, Transition, Marker


class Geometry:
    """Cache of timeline geometry and hit-testing helper."""

    def __init__(self, widget):
        self.widget = widget
        self.dirty = True
        self.track_rects = []
        self.clip_rects = []
        self.selected_rects = []
        self.transition_rects = []
        self.selected_transitions = []
        self.marker_rects = []
        self.track_list = []

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------
    def mark_dirty(self):
        """Invalidate all cached geometry."""
        self.dirty = True

    def ensure(self):
        """Rebuild cached geometry if marked dirty."""
        if self.dirty:
            self._rebuild()

    # ------------------------------------------------------------------
    # Geometry building
    # ------------------------------------------------------------------
    def _rebuild(self):
        w = self.widget
        win = get_app().window

        self.track_rects.clear()
        self.clip_rects.clear()
        self.selected_rects.clear()
        self.transition_rects.clear()
        self.selected_transitions.clear()
        self.marker_rects.clear()

        layers = {}
        self.track_list = list(reversed(sorted(Track.filter())))
        for idx, layer in enumerate(self.track_list):
            layers[layer.data.get("number")] = idx

        if hasattr(win, "timeline"):
            proj = get_app().project
            duration = proj.get("duration")
            tick_px = proj.get("tick_pixels") or 100
            w.pixels_per_second = tick_px / float(w.zoom_factor or 1)
            width = max(w.width() - w.track_name_width,
                        duration * w.pixels_per_second)

            if w.track_height:
                w.vertical_factor = w.track_height
            else:
                tracks = len(layers.keys() or [1])
                w.vertical_factor = max(1, (w.height() - w.ruler_height) / tracks)

            spacing = w.vertical_factor + getattr(w, 'track_gap', 0)
            for track in self.track_list:
                y = w.ruler_height + layers[track.data.get("number")] * spacing
                track_rect = QRectF(w.track_name_width, y, width, w.vertical_factor)
                name_rect = QRectF(0, y, w.track_name_width, w.vertical_factor)
                self.track_rects.append((track_rect, track, name_rect))

            w.resize_handle_rect = QRectF(
                w.track_name_width - w._resize_handle_width / 2,
                w.ruler_height,
                w._resize_handle_width,
                len(self.track_list) * spacing - getattr(w, 'track_gap', 0),
            )

            for clip in Clip.filter():
                cx = w.track_name_width + clip.data.get("position", 0.0) * w.pixels_per_second
                cy = w.ruler_height + layers.get(clip.data.get("layer", 0), 0) * spacing
                cw = (clip.data.get("end", 0.0) - clip.data.get("start", 0.0)) * w.pixels_per_second
                rect = QRectF(cx, cy, cw, w.vertical_factor)
                if clip.id in win.selected_clips:
                    self.selected_rects.append((rect, clip))
                else:
                    self.clip_rects.append((rect, clip))

            for tr in Transition.filter():
                tx = w.track_name_width + tr.data.get("position", 0.0) * w.pixels_per_second
                ty = w.ruler_height + layers.get(tr.data.get("layer", 0), 0) * spacing
                tw = (tr.data.get("end", 0.0) - tr.data.get("start", 0.0)) * w.pixels_per_second
                rect = QRectF(tx, ty, tw, w.vertical_factor)
                if tr.id in win.selected_transitions:
                    self.selected_transitions.append((rect, tr))
                else:
                    self.transition_rects.append((rect, tr))

            for marker in Marker.filter():
                mx = w.track_name_width + marker.data.get("position", 0.0) * w.pixels_per_second
                rect = QRectF(mx, w.ruler_height, 0.5, len(layers) * spacing - getattr(w, 'track_gap', 0))
                self.marker_rects.append(rect)

        self.dirty = False

    # ------------------------------------------------------------------
    # Hit testing
    # ------------------------------------------------------------------
    def hit(self, pos: QPointF):
        """Return a string describing what lies under *pos*."""
        self.ensure()

        for rect, _ in (
            self.selected_rects + self.clip_rects +
            self.selected_transitions + self.transition_rects
        ):
            if rect.contains(pos):
                return "clip"
        if self.widget.resize_handle_rect.contains(pos):
            return "handle"
        if pos.y() <= self.widget.ruler_height:
            return "ruler"
        return "background"

    def calc_item_rect(self, item):
        """Return QRectF for *item* (Clip or Transition)."""
        layers = {t.data.get("number"): idx for idx, t in enumerate(self.track_list)}
        x = self.widget.track_name_width + item.data.get("position", 0.0) * self.widget.pixels_per_second
        spacing = self.widget.vertical_factor + getattr(self.widget, 'track_gap', 0)
        y = self.widget.ruler_height + layers.get(item.data.get("layer", 0), 0) * spacing
        w = (item.data.get("end", 0.0) - item.data.get("start", 0.0)) * self.widget.pixels_per_second
        return QRectF(x, y, w, self.widget.vertical_factor)

    def update_item_rect(self, item, rect):
        """Replace cached rect for *item* if present."""
        for lst in (
                self.selected_rects,
                self.clip_rects,
                self.selected_transitions,
                self.transition_rects,
        ):
            for i, (r, c) in enumerate(lst):
                if c.id == item.id:
                    lst[i] = (rect, item)
                    return
