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
from classes.logger import log


class GeometryBase:
    """Shared cache and hit-testing helpers for timeline geometry."""

    def __init__(self, widget):
        self.widget = widget
        self.dirty = True
        self.track_rects = []
        self.clip_entries = []
        self.transition_entries = []
        self.marker_rects = []
        self.track_list = []
        self.panel_rects = {}
        self._view_context = {}

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------
    def mark_dirty(self):
        """Invalidate all cached geometry."""
        self.dirty = True
        if hasattr(self.widget, "_keyframes_dirty"):
            self.widget._keyframes_dirty = True

    def ensure(self):
        """Rebuild cached geometry if marked dirty."""
        if self.dirty:
            self._rebuild()

    # ------------------------------------------------------------------
    # Geometry building
    # ------------------------------------------------------------------
    def _reset_cache(self):
        self.track_rects.clear()
        self.clip_entries.clear()
        self.transition_entries.clear()
        self.marker_rects.clear()
        self.panel_rects.clear()

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
        duration = self.widget._current_project_duration()
        tick_px = proj.get("tick_pixels") or 100
        w.pixels_per_second = tick_px / float(w.zoom_factor or 1)
        view_w = w.width() - w.track_name_width - w.scroll_bar_thickness
        view_h = w.height() - w.ruler_height - w.scroll_bar_thickness
        timeline_w = max(view_w, duration * w.pixels_per_second)
        self._update_vertical_factor(layers, view_h)
        track_gap = float(getattr(w, "track_gap", 0.0) or 0.0)
        top_margin = float(getattr(w, "track_margin_top", 0.0) or 0.0)
        track_offsets = {}
        track_heights = {}
        cumulative = 0.0
        base_height = float(self.widget.vertical_factor or 0.0)
        for idx, track in enumerate(self.track_list):
            if idx > 0:
                cumulative += track_gap
            track_num = w.normalize_track_number(track.data.get("number"))
            extra = float(w.get_track_panel_height(track_num))
            extra = max(0.0, extra)
            track_offsets[track_num] = cumulative
            track_height = base_height + extra
            track_heights[track_num] = track_height
            cumulative += track_height
            if extra > 0.0:
                log.debug(
                    "Geometry: track %s base=%.2f extra=%.2f total=%.2f",
                    track_num,
                    base_height,
                    extra,
                    track_height,
                )
        content_h = max(cumulative, 0.0)
        spacing = base_height + track_gap
        content_h = max(content_h, 0.0) + top_margin
        h_offset = self._update_horizontal_scrollbar(timeline_w, view_w)
        if getattr(w, "_project_resize_keep_right", False):
            view_ratio = view_w / timeline_w if timeline_w else 1.0
            view_ratio = min(1.0, max(0.0, view_ratio))
            left = 0.0
            if view_ratio < 1.0:
                left = max(0.0, 1.0 - view_ratio)
            right = min(1.0, left + view_ratio)
            w.scrollbar_position[0] = left
            w.scrollbar_position[1] = right
            w.h_scroll_offset = left * timeline_w
            if view_ratio < 1.0:
                handle_w = max(20.0, view_ratio * view_w)
                avail = view_w - handle_w
                handle_x = w.track_name_width
                max_scroll = max(0.0, timeline_w - view_w)
                scroll_px = w.h_scroll_offset
                if max_scroll > 0.0 and avail > 0.0:
                    handle_x += (scroll_px / max_scroll) * avail
                w.scroll_bar_rect = QRectF(
                    handle_x,
                    w.height() - w.scroll_bar_thickness,
                    handle_w,
                    w.scroll_bar_thickness,
                )
            else:
                w.scroll_bar_rect = QRectF()
            h_offset = w.h_scroll_offset
        v_offset = self._update_vertical_scrollbar(content_h, view_h)
        w.h_scroll_offset = h_offset
        ctx = {
            "view_w": view_w,
            "view_h": view_h,
            "timeline_w": timeline_w,
            "spacing": spacing,
            "top_margin": top_margin,
            "content_h": content_h,
            "h_offset": h_offset,
            "v_offset": v_offset,
            "track_offsets": track_offsets,
            "track_heights": track_heights,
        }
        self._view_context = ctx
        return ctx

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
            for rect, _obj, _sel, _type in self.iter_items(reverse=True):
                if rect.contains(pos):
                    return "clip"
        for _track_rect, track, name_rect in self.track_rects:
            track_num = self.widget.normalize_track_number(track.data.get("number"))
            panel_rect = self.panel_rects.get(track_num)
            if not panel_rect or panel_rect.height() <= 0.0:
                continue
            if panel_rect.contains(pos):
                return "panel"
            combined = QRectF(
                name_rect.x(),
                panel_rect.y(),
                name_rect.width() + panel_rect.width(),
                panel_rect.height(),
            )
            if combined.contains(pos):
                return "panel"
        if self.widget.scroll_bar_rect.contains(pos):
            return "h-scroll"
        if getattr(self.widget, "v_scroll_bar_rect", QRectF()).contains(pos):
            return "v-scroll"
        timeline_handle = getattr(self.widget, "timeline_resize_handle_rect", QRectF())
        if timeline_handle.contains(pos):
            return "timeline-handle"
        if self.widget.resize_handle_rect.contains(pos):
            return "handle"
        if pos.y() <= self.widget.ruler_height:
            return "ruler"
        return "background"

    def calc_item_rect(self, item):
        """Return QRectF for *item* (Clip or Transition)."""
        layers = {t.data.get("number"): idx for idx, t in enumerate(self.track_list)}
        spacing = self.widget.vertical_factor + getattr(self.widget, "track_gap", 0)
        offsets = getattr(self, "_view_context", {}).get("track_offsets", {})
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
        layer_val = item.data.get("layer", 0)
        offset = offsets.get(
            self.widget.normalize_track_number(layer_val),
            layers.get(layer_val, 0) * spacing,
        )
        y = (
            self.widget.ruler_height
            + getattr(self.widget, "track_margin_top", 0.0)
            + offset
            - v_offset
        )
        w = (item.data.get("end", 0.0) - item.data.get("start", 0.0)) * self.widget.pixels_per_second
        return QRectF(x, y, w, self.widget.vertical_factor)

    def update_item_rect(self, item, rect):
        """Replace cached rect for *item* if present."""
        for idx, (existing_rect, existing, selected) in enumerate(self.clip_entries):
            if existing.id == item.id:
                self.clip_entries[idx] = (rect, item, selected)
                return
        for idx, (existing_rect, existing, selected) in enumerate(self.transition_entries):
            if existing.id == item.id:
                self.transition_entries[idx] = (rect, item, selected)
                return

    # ------------------------------------------------------------------
    # Iteration helpers
    # ------------------------------------------------------------------
    def iter_clips(self, reverse=False):
        """Yield (rect, clip, selected) tuples for cached clips."""
        yield from self._iter_entries(self.clip_entries, reverse)

    def iter_transitions(self, reverse=False):
        """Yield (rect, transition, selected) tuples for cached transitions."""
        yield from self._iter_entries(self.transition_entries, reverse)

    def iter_items(self, reverse=False):
        """Yield (rect, obj, selected, type) for transitions then clips."""
        for rect, tran, selected in self.iter_transitions(reverse=reverse):
            yield rect, tran, selected, "transition"
        for rect, clip, selected in self.iter_clips(reverse=reverse):
            yield rect, clip, selected, "clip"

    def _iter_entries(self, entries, reverse=False):
        """Yield entries grouped by selection state while preserving stacking order."""
        if reverse:
            for selected_flag in (True, False):
                for rect, obj, selected in reversed(entries):
                    if selected == selected_flag:
                        yield rect, obj, selected
        else:
            for selected_flag in (False, True):
                for rect, obj, selected in entries:
                    if selected == selected_flag:
                        yield rect, obj, selected
