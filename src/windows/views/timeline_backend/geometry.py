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
    def _reset_cache(self):
        self.track_rects.clear()
        self.clip_rects.clear()
        self.selected_rects.clear()
        self.transition_rects.clear()
        self.selected_transitions.clear()
        self.marker_rects.clear()

    def _build_layer_index(self):
        self.track_list = list(reversed(sorted(Track.filter())))
        layers = {}
        for idx, layer in enumerate(self.track_list):
            layers[layer.data.get("number")] = idx
        return layers

    def _update_vertical_factor(self, layers, view_h):
        if self.widget.track_height:
            self.widget.vertical_factor = self.widget.track_height
            return
        tracks = len(layers) if layers else 1
        self.widget.vertical_factor = max(1, view_h / tracks)

    def _update_horizontal_scrollbar(self, timeline_w, view_w):
        w = self.widget
        w.scrollbar_position[2] = timeline_w
        w.scrollbar_position[3] = view_w
        view_ratio = view_w / timeline_w if timeline_w else 1.0
        max_left = max(0.0, 1.0 - view_ratio)
        left = max(0.0, min(w.scrollbar_position[0], max_left))
        scroll_px = left * timeline_w
        max_scroll = max(0.0, timeline_w - view_w)
        if max_scroll:
            scroll_px = min(scroll_px, max_scroll)
            left = scroll_px / timeline_w
        right = left + view_ratio
        w.scrollbar_position[0] = left
        w.scrollbar_position[1] = right
        if view_ratio < 1.0:
            handle_w = max(20.0, view_ratio * view_w)
            avail = view_w - handle_w
            handle_x = w.track_name_width
            if max_scroll:
                handle_x += (scroll_px / max_scroll) * avail
            w.scroll_bar_rect = QRectF(
                handle_x,
                w.height() - w.scroll_bar_thickness,
                handle_w,
                w.scroll_bar_thickness,
            )
            return scroll_px
        w.scroll_bar_rect = QRectF()
        return 0.0

    def _update_vertical_scrollbar(self, content_h, view_h):
        w = self.widget
        w.v_scrollbar_position[2] = content_h
        w.v_scrollbar_position[3] = view_h
        v_ratio = view_h / content_h if content_h else 1.0
        max_top = max(0.0, 1.0 - v_ratio)
        top = max(0.0, min(w.v_scrollbar_position[0], max_top))
        scroll_py = top * content_h
        max_vscroll = max(0.0, content_h - view_h)
        if max_vscroll:
            scroll_py = min(scroll_py, max_vscroll)
            top = scroll_py / content_h
        bottom = top + v_ratio
        w.v_scrollbar_position[0] = top
        w.v_scrollbar_position[1] = bottom
        if v_ratio < 1.0:
            handle_h = max(20.0, v_ratio * view_h)
            avail = view_h - handle_h
            handle_y = w.ruler_height
            if max_vscroll:
                handle_y += (scroll_py / max_vscroll) * avail
            w.v_scroll_bar_rect = QRectF(
                w.width() - w.scroll_bar_thickness,
                handle_y,
                w.scroll_bar_thickness,
                handle_h,
            )
            return scroll_py
        w.v_scroll_bar_rect = QRectF()
        return 0.0

    def _calculate_view_context(self, layers):
        w = self.widget
        proj = get_app().project
        duration = proj.get("duration")
        tick_px = proj.get("tick_pixels") or 100
        w.pixels_per_second = tick_px / float(w.zoom_factor or 1)
        view_w = w.width() - w.track_name_width - w.scroll_bar_thickness
        view_h = w.height() - w.ruler_height - w.scroll_bar_thickness
        timeline_w = max(view_w, duration * w.pixels_per_second)
        self._update_vertical_factor(layers, view_h)
        spacing = self.widget.vertical_factor + getattr(w, "track_gap", 0)
        content_h = len(self.track_list) * spacing - getattr(w, "track_gap", 0)
        content_h = max(content_h, 0.0)
        h_offset = self._update_horizontal_scrollbar(timeline_w, view_w)
        v_offset = self._update_vertical_scrollbar(content_h, view_h)
        return {
            "view_w": view_w,
            "view_h": view_h,
            "timeline_w": timeline_w,
            "spacing": spacing,
            "content_h": content_h,
            "h_offset": h_offset,
            "v_offset": v_offset,
        }

    def _populate_track_rects(self, layers, ctx):
        w = self.widget
        for track in self.track_list:
            layer_index = layers.get(track.data.get("number"), 0)
            y = (
                w.ruler_height
                + layer_index * ctx["spacing"]
                - ctx["v_offset"]
            )
            if (
                y + w.vertical_factor <= w.ruler_height
                or y >= w.ruler_height + ctx["view_h"]
            ):
                continue
            track_rect = QRectF(
                w.track_name_width - ctx["h_offset"],
                y,
                ctx["timeline_w"],
                w.vertical_factor,
            )
            name_rect = QRectF(0, y, w.track_name_width, w.vertical_factor)
            self.track_rects.append((track_rect, track, name_rect))

        w.resize_handle_rect = QRectF(
            w.track_name_width - w._resize_handle_width / 2,
            w.ruler_height,
            w._resize_handle_width,
            ctx["content_h"],
        )

    def _populate_clip_rects(self, layers, ctx, win):
        w = self.widget
        for clip in Clip.filter():
            cx = (
                w.track_name_width
                + clip.data.get("position", 0.0) * w.pixels_per_second
                - ctx["h_offset"]
            )
            layer_idx = layers.get(clip.data.get("layer", 0), 0)
            cy = (
                w.ruler_height
                + layer_idx * ctx["spacing"]
                - ctx["v_offset"]
            )
            cw = (
                clip.data.get("end", 0.0) - clip.data.get("start", 0.0)
            ) * w.pixels_per_second
            if (
                cx + cw <= w.track_name_width
                or cy + w.vertical_factor <= w.ruler_height
                or cy >= w.ruler_height + ctx["view_h"]
            ):
                continue
            rect = QRectF(cx, cy, cw, w.vertical_factor)
            if clip.id in win.selected_clips:
                self.selected_rects.append((rect, clip))
            else:
                self.clip_rects.append((rect, clip))

    def _populate_transition_rects(self, layers, ctx, win):
        w = self.widget
        for tr in Transition.filter():
            tx = (
                w.track_name_width
                + tr.data.get("position", 0.0) * w.pixels_per_second
                - ctx["h_offset"]
            )
            layer_idx = layers.get(tr.data.get("layer", 0), 0)
            ty = (
                w.ruler_height
                + layer_idx * ctx["spacing"]
                - ctx["v_offset"]
            )
            tw = (
                tr.data.get("end", 0.0) - tr.data.get("start", 0.0)
            ) * w.pixels_per_second
            if (
                tx + tw <= w.track_name_width
                or ty + w.vertical_factor <= w.ruler_height
                or ty >= w.ruler_height + ctx["view_h"]
            ):
                continue
            rect = QRectF(tx, ty, tw, w.vertical_factor)
            if tr.id in win.selected_transitions:
                self.selected_transitions.append((rect, tr))
            else:
                self.transition_rects.append((rect, tr))

    def _populate_marker_rects(self, ctx):
        w = self.widget
        for marker in Marker.filter():
            mx = (
                w.track_name_width
                + marker.data.get("position", 0.0) * w.pixels_per_second
                - ctx["h_offset"]
            )
            rect = QRectF(
                mx,
                w.ruler_height - ctx["v_offset"],
                0.5,
                ctx["content_h"],
            )
            self.marker_rects.append(rect)

    def _rebuild(self):
        win = get_app().window

        self._reset_cache()
        layers = self._build_layer_index()

        if not hasattr(win, "timeline"):
            self.dirty = False
            return

        ctx = self._calculate_view_context(layers)
        self._populate_track_rects(layers, ctx)
        self._populate_clip_rects(layers, ctx, win)
        self._populate_transition_rects(layers, ctx, win)
        self._populate_marker_rects(ctx)

        self.dirty = False

    # ------------------------------------------------------------------
    # Hit testing
    # ------------------------------------------------------------------
    def hit(self, pos: QPointF):
        """Return a string describing what lies under *pos*."""
        self.ensure()
        if (
            pos.x() >= self.widget.track_name_width
            and pos.y() >= self.widget.ruler_height
        ):
            for rect, _ in (
                self.selected_transitions
                + self.transition_rects
                + self.selected_rects
                + self.clip_rects
            ):
                if rect.contains(pos):
                    return "clip"
        if self.widget.scroll_bar_rect.contains(pos):
            return "h-scroll"
        if getattr(self.widget, "v_scroll_bar_rect", QRectF()).contains(pos):
            return "v-scroll"
        if self.widget.resize_handle_rect.contains(pos):
            return "handle"
        if pos.y() <= self.widget.ruler_height:
            return "ruler"
        return "background"

    def calc_item_rect(self, item):
        """Return QRectF for *item* (Clip or Transition)."""
        layers = {t.data.get("number"): idx for idx, t in enumerate(self.track_list)}
        spacing = self.widget.vertical_factor + getattr(self.widget, "track_gap", 0)
        view_w = self.widget.scrollbar_position[3] or 1.0
        timeline_w = self.widget.scrollbar_position[2] or view_w
        left = self.widget.scrollbar_position[0]
        h_offset = left * timeline_w
        max_scroll = max(0.0, timeline_w - view_w)
        if h_offset > max_scroll:
            h_offset = max_scroll
        view_h = self.widget.v_scrollbar_position[3] or 1.0
        content_h = self.widget.v_scrollbar_position[2] or view_h
        top = self.widget.v_scrollbar_position[0]
        v_offset = top * content_h
        max_vscroll = max(0.0, content_h - view_h)
        if v_offset > max_vscroll:
            v_offset = max_vscroll
        x = (
            self.widget.track_name_width
            + item.data.get("position", 0.0) * self.widget.pixels_per_second
            - h_offset
        )
        y = (
            self.widget.ruler_height
            + layers.get(item.data.get("layer", 0), 0) * spacing
            - v_offset
        )
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
            for i, (_rect, c) in enumerate(lst):
                if c.id == item.id:
                    lst[i] = (rect, item)
                    return
