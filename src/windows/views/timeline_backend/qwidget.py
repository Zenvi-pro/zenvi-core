"""
 @file
 @brief This file contains a custom QWidget-based timeline - to replace older, webview-based timelines
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

import json
import uuid

from PyQt5.QtCore import (
    Qt,
    QRectF,
    QTimer,
    QPointF,
    QSignalTransition,
    pyqtSignal,
    QObject,
)
from PyQt5.QtGui import (
    QPainter,
    QCursor,
    QIcon,
    QColor,
)
from PyQt5.QtWidgets import QSizePolicy, QWidget

from .geometry import Geometry
from .paint import (
    BackgroundPainter,
    ClipPainter,
    TransitionPainter,
    MarkerPainter,
    PlayheadPainter,
    RulerPainter,
    TrackPainter,
    SelectionPainter,
    ScrollbarPainter,
    KeyframePainter,
)
from .snap import SnapHelper
from .theme import DEFAULT_THEME, apply_theme as parse_theme
from .state import TimelineStateMachine
from .colors import effect_color_qcolor
from classes.waveform import SAMPLES_PER_SECOND as WAVEFORM_SAMPLES_PER_SECOND

from classes.app import get_app
from classes.query import Clip, Transition, File


class TimelineEvents(QObject):
    pressed = pyqtSignal(object)
    moved = pyqtSignal(object)
    released = pyqtSignal(object)


class _ConditionalTransition(QSignalTransition):
    def __init__(self, signal, target_state, condition):
        super().__init__(signal)
        self.setTargetState(target_state)
        self._cond = condition

    def eventTest(self, event):
        return super().eventTest(event) and self._cond()


class TimelineWidget(QWidget):
    def __init__(self, parent=None):
        super(TimelineWidget, self).__init__(parent)

        # Enable drag and drop
        self.new_item = None
        self.item_type = None
        self.setAcceptDrops(True)

        # Translate object
        _ = get_app()._tr

        # Init default values
        self.leftHandle = None
        self.rightHandle = None
        self.centerHandle = None
        self.mouse_pressed = False
        self.mouse_dragging = False
        self.mouse_position = None
        self.zoom_factor = 15.0
        self.scrollbar_position = [0.0, 0.0, 0.0, 0.0]
        self.scrollbar_position_previous = [0.0, 0.0, 0.0, 0.0]
        self.v_scrollbar_position = [0.0, 0.0, 0.0, 0.0]
        self.v_scrollbar_position_previous = [0.0, 0.0, 0.0, 0.0]
        self.h_scroll_offset = 0.0
        self.left_handle_rect = QRectF()
        self.left_handle_dragging = False
        self.right_handle_rect = QRectF()
        self.right_handle_dragging = False
        self.scroll_bar_rect = QRectF()
        self.scroll_bar_dragging = False
        self.v_scroll_bar_rect = QRectF()
        self.v_scroll_bar_dragging = False
        self.clip_rects = []
        self.clip_rects_selected = []
        self.marker_rects = []
        self.current_frame = 0
        self.is_auto_center = True
        self.min_distance = 0.02
        self.track_rects = []
        self.track_list = []
        self.pixels_per_second = 1.0
        self.vertical_factor = 1.0
        self.track_height = 48
        self.track_gap = 8
        self.track_margin_top = self.track_gap

        # Geometry constants
        self.ruler_height = 40
        self.track_name_width = 140
        self.scroll_bar_thickness = 12
        self._resize_handle_width = 6
        self.resizing_track_names = False
        self.resize_handle_rect = QRectF()

        # Drag/selection helpers
        self.selection_rect = QRectF()
        self.box_selecting = False
        self.box_start = QPointF()
        self.dragging_item = None
        self.drag_clip_offset = 0.0
        self.drag_clip_start = 0.0
        self.dragging_playhead = False
        self.drag_bbox = QRectF()
        self._drag_transaction_id = None

        # Resize / timing helpers
        self.enable_timing = False
        self.enable_snapping = True
        self._resizing_item = None
        self._resize_edge = None
        self._resize_initial_rect = QRectF()
        self._resize_initial = {}
        self._timing_original_start = 0.0
        self._fixed_cursor = None

        # Cached Qt text flags
        self._clip_text_flags = Qt.AlignLeft | Qt.AlignTop

        # Frames per second float value
        fps_info = get_app().project.get("fps")
        self.fps_float = float(fps_info.get("num", 24)) / float(fps_info.get("den", 1) or 1)

        # Theme settings
        self.theme = DEFAULT_THEME

        # Helpers for geometry, snapping and painting
        self.geometry = Geometry(self)
        self.snap = SnapHelper(self, self.geometry)
        self.bg_painter = BackgroundPainter(self)
        self.ruler_painter = RulerPainter(self)
        self.track_painter = TrackPainter(self)
        self.clip_painter = ClipPainter(self)
        self.transition_painter = TransitionPainter(self)
        self.marker_painter = MarkerPainter(self)
        self.playhead_painter = PlayheadPainter(self)
        self.keyframe_painter = KeyframePainter(self)
        self.selection_painter = SelectionPainter(self)
        self.scrollbar_painter = ScrollbarPainter(self)

        # Keyframe helpers
        self._keyframe_markers = []
        self._keyframes_dirty = True
        self._dragging_keyframe = None
        self._press_keyframe = None
        self._press_keyframe_clear = True
        self._press_effect_icon = None
        self._pending_clip_overrides = {}
        self._pending_transition_overrides = {}
        self._preserve_overrides_once = False
        self._drag_payload = None
        self._drag_preview_items = []
        self._drag_preview_type = None
        self._snap_ignore_ids = set()
        self._snap_keyframe_seconds = []
        self._snap_active_targets = {}

        # Apply default theme
        self.apply_theme("")

        # Load icon (using display DPI)
        self.cursors = {}
        for cursor_name in ["move", "resize_x", "hand"]:
            icon = QIcon(":/cursors/cursor_%s.png" % cursor_name)
            self.cursors[cursor_name] = QCursor(icon.pixmap(24, 24))

        # Init Qt widget's properties (background repainting, etc...)
        super().setAttribute(Qt.WA_OpaquePaintEvent)
        super().setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Add self as listener to project data updates (used to update the timeline)
        get_app().updates.add_listener(self)

        # Set mouse tracking
        self.setMouseTracking(True)

        # Get a reference to the window object
        self.win = get_app().window
        self.win.ThemeChangedSignal.connect(self.apply_theme)

        # Connect zoom functionality
        self.win.TimelineScrolled.connect(self.update_scrollbars)
        self.win.TimelineScroll.connect(self.set_scroll_left)
        self.win.TimelineZoom.connect(self._apply_external_zoom)

        self.win.TimelineResize.connect(self.delayed_resize_callback)

        # Connect Selection signals
        self.win.SelectionChanged.connect(self.handle_selection)

        # Show Property timer
        # Timer to use a delay before sending MaxSizeChanged signals (so we don't spam libopenshot)
        self.delayed_size = None
        self.delayed_resize_timer = QTimer(self)
        self.delayed_resize_timer.setInterval(100)
        self.delayed_resize_timer.setSingleShot(True)
        self.delayed_resize_timer.timeout.connect(self.delayed_resize_callback)

        # Initial geometry setup
        TimelineWidget.changed(self, None)

        # State machine for mouse interactions
        self.events = TimelineEvents()
        self._last_event = None
        self._press_hit = None
        self._buildStateMachine()

        # Effect icon hit targets (populated by the clip painter)
        self._effect_icon_rects = []

        # Middle-mouse panning helpers
        self._middle_panning = False
        self._middle_pan_anchor = QPointF()
        self._middle_pan_scroll_start = [0.0, 0.0, 0.0, 0.0]
        self._middle_pan_vscroll_start = [0.0, 0.0, 0.0, 0.0]

    def _buildStateMachine(self):
        sm = TimelineStateMachine(self)

        idle = sm.idle
        drag = sm.drag
        resize = sm.resize
        playhead = sm.playhead
        boxsel = sm.box
        keydrag = sm.keyframe

        drag.entered.connect(self._startClipDrag)
        drag.exited.connect(self._finishClipDrag)
        resize.entered.connect(self._startResize)
        resize.exited.connect(self._finishResize)
        playhead.entered.connect(self._startPlayhead)
        playhead.exited.connect(self._finishPlayhead)
        boxsel.entered.connect(self._startBoxSelect)
        boxsel.exited.connect(self._finishBoxSelect)
        keydrag.entered.connect(self._startKeyframeDrag)
        keydrag.exited.connect(self._finishKeyframeDrag)

        idle.addTransition(_ConditionalTransition(
            self.events.pressed, drag,
            lambda: self._press_hit == "clip"
        ))
        idle.addTransition(_ConditionalTransition(
            self.events.pressed, resize,
            lambda: self._press_hit in ("handle", "clip-edge")
        ))
        idle.addTransition(_ConditionalTransition(
            self.events.pressed, playhead,
            lambda: self._press_hit == "ruler"
        ))
        idle.addTransition(_ConditionalTransition(
            self.events.pressed, boxsel,
            lambda: self._press_hit == "background"
        ))
        idle.addTransition(_ConditionalTransition(
            self.events.pressed, keydrag,
            lambda: self._press_hit == "keyframe"
        ))

        drag.entered.connect(lambda: self.events.moved.connect(self._dragMove))
        drag.exited.connect(lambda: self._safe_disconnect(self.events.moved, self._dragMove))
        drag.addTransition(self.events.released, idle)

        resize.entered.connect(lambda: self.events.moved.connect(self._resizeMove))
        resize.exited.connect(lambda: self._safe_disconnect(self.events.moved, self._resizeMove))
        resize.addTransition(self.events.released, idle)

        playhead.entered.connect(lambda: self.events.moved.connect(self._playheadMove))
        playhead.exited.connect(lambda: self._safe_disconnect(self.events.moved, self._playheadMove))
        playhead.addTransition(self.events.released, idle)

        boxsel.entered.connect(lambda: self.events.moved.connect(self._boxMove))
        boxsel.exited.connect(lambda: self._safe_disconnect(self.events.moved, self._boxMove))
        boxsel.addTransition(self.events.released, idle)

        keydrag.entered.connect(lambda: self.events.moved.connect(self._keyframeMove))
        keydrag.exited.connect(lambda: self._safe_disconnect(self.events.moved, self._keyframeMove))
        keydrag.addTransition(self.events.released, idle)

        # repaint exactly once when any interactive state exits
        for s in (drag, resize, playhead, boxsel, keydrag):
            s.exited.connect(self.update)

        sm.setInitialState(idle)
        sm.start()
        self._sm = sm

    def _safe_disconnect(self, signal, slot):
        try:
            signal.disconnect(slot)
        except TypeError:
            pass

    def _apply_external_zoom(self, zoom_factor):
        """Apply zoom requests from the ZoomSlider without feedback."""
        self.setZoomFactor(zoom_factor, emit=False)
        project_duration = get_app().project.get("duration") or 0.0
        tick_pixels = 100.0
        self.scrollbar_position[2] = (
            project_duration * tick_pixels / zoom_factor if zoom_factor else 0.0
        )

    def setSnappingMode(self, enable):
        """Enable or disable snapping mode."""
        self.enable_snapping = bool(enable)

    def setTimingMode(self, enable):
        """Enable or disable timing (retime) mode."""
        self.enable_timing = bool(enable)
        if self.enable_timing:
            self._snap_keyframe_seconds = []

    def _fix_cursor(self, cursor):
        self._fixed_cursor = cursor
        self.setCursor(cursor)

    def _release_cursor(self):
        self._fixed_cursor = None

    def _snap_time(self, seconds):
        """Snap a time in seconds to the nearest frame boundary."""
        return round(seconds * self.fps_float) / self.fps_float

    def _seconds_from_x(self, x_pos):
        """Convert an x position in widget coordinates to timeline seconds."""
        pps = float(self.pixels_per_second or 0.0)
        if pps <= 0.0:
            return 0.0
        offset_px = getattr(self, "h_scroll_offset", 0.0)
        seconds = (x_pos - self.track_name_width + offset_px) / pps
        return max(0.0, seconds)

    def run_js(self, code, callback=None, retries=0):
        """Placeholder due to webview compatibility"""

    def apply_theme(self, css=None):
        """Apply CSS theme to this widget."""
        if not isinstance(css, str):
            # Signal from ThemeChangedSignal passes the theme instance.
            # The theme has already been applied directly, so simply
            # refresh painters.
            self._theme_changed()
            return

        if parse_theme(self, css):
            TimelineWidget.changed(self, None)
        self._theme_changed()

    def _theme_changed(self):
        for p in (
            self.bg_painter,
            self.ruler_painter,
            self.track_painter,
            self.clip_painter,
            self.transition_painter,
            self.marker_painter,
            self.playhead_painter,
            self.keyframe_painter,
            self.selection_painter,
            self.scrollbar_painter,
        ):
            p.update_theme()
        self._keyframes_dirty = True
        self.update()

    def setup_js_data(self):
        """Placeholder due to webview compatibility"""

    def get_html(self):
        """Placeholder due to webview compatibility"""

    # This method is invoked by the UpdateManager each time a change happens (i.e UpdateInterface)
    def changed(self, action):
        # Ignore changes that don't affect this
        if action and len(action.key) >= 1 and action.key[0].lower() in ["files", "history", "profile"]:
            return

        fps_info = get_app().project.get("fps")
        self.fps_float = float(fps_info.get("num", 24)) / float(fps_info.get("den", 1) or 1)

        # Invalidate caches and geometry
        self.clip_painter.clear_cache()
        self.transition_painter.clear_cache()
        self.geometry.mark_dirty()

        preserve_overrides = getattr(self, "_preserve_overrides_once", False)
        if preserve_overrides:
            self._preserve_overrides_once = False
        else:
            self._pending_clip_overrides.clear()
            self._pending_transition_overrides.clear()

        self.geometry.ensure()
        self._keyframes_dirty = True
        self._snap_keyframe_seconds = []

        # Mirror some attributes for compatibility
        self.track_list = self.geometry.track_list

        # Schedule repaint
        self.update()

    def paintEvent(self, event, *args):
        """Custom paint routine for the timeline widget."""
        event.accept()
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.Antialiasing |
            QPainter.SmoothPixmapTransform |
            QPainter.TextAntialiasing,
            True,
        )

        if not get_app().window.timeline:
            painter.end()
            return

        self.geometry.ensure()
        self._ensure_keyframe_markers()

        self.bg_painter.paint(painter, event.rect())
        self.track_painter.paint_background(painter)
        self.clip_painter.paint(painter)
        self.transition_painter.paint(painter)
        self.marker_painter.paint(painter)
        self.selection_painter.paint(painter)
        self.keyframe_painter.paint(painter)
        self.track_painter.paint_names(painter)
        self.ruler_painter.paint(painter)
        self.playhead_painter.paint(painter)
        self.ruler_painter.paint_overlay(painter)
        self.scrollbar_painter.paint(painter)

        painter.end()

    def dragEnterEvent(self, event):
        self._drag_payload = None
        mime = event.mimeData()

        if mime.hasUrls():
            event.accept()
            self.new_item = True
            self.item_type = "os_drop"
            self._drag_payload = {"type": "os_drop", "urls": mime.urls()}
            return

        mime_html = mime.html()
        if mime_html:
            if mime_html in ("clip", "transition"):
                try:
                    ids = json.loads(mime.text())
                except Exception:
                    ids = []
                if not isinstance(ids, list):
                    ids = [ids]
                self._drag_payload = {"type": mime_html, "ids": ids}
                self.item_type = mime_html
                self.new_item = True
                event.accept()
            elif mime_html == "effect":
                event.accept()
            else:
                event.ignore()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        event.accept()
        payload = self._ensure_drag_payload_from_event(event)

        if payload and payload.get("type") in {"clip", "transition"}:
            coords = self._event_seconds_track(event)
            if coords is None:
                self._reset_drag_preview(delete_items=True)
                return
            pos_seconds, track_num, _ = coords
            if not self._ensure_drag_preview(pos_seconds, track_num):
                return
            self._update_drag_preview_position(pos_seconds, track_num)
        else:
            if payload and payload.get("type") == "effect":
                return
            if payload and payload.get("type") == "os_drop":
                return
            self._reset_drag_preview(delete_items=True)

    def dropEvent(self, event):
        event.accept()

        if self._drag_preview_items:
            self._finalize_drag_preview()
            return

        file_ids = []
        effect_names = []
        mime = event.mimeData()
        mime_html = mime.html()
        if mime.hasUrls():
            urls = mime.urls()
            self.win.files_model.process_urls(urls, import_quietly=True, prevent_image_seq=True)
            for uri in urls:
                for f in File.filter(path=uri.toLocalFile()):
                    file_ids.append(f.id)
        elif mime_html == "clip":
            try:
                ids = json.loads(mime.text())
            except Exception:
                ids = []
            if not isinstance(ids, list):
                ids = [ids]
            file_ids.extend(ids)
        elif mime_html == "transition":
            try:
                ids = json.loads(mime.text())
            except Exception:
                ids = []
            if not isinstance(ids, list):
                ids = [ids]
            file_ids.extend(ids)
        elif mime_html == "effect":
            try:
                names = json.loads(mime.text())
            except Exception:
                names = []
            if not isinstance(names, list):
                names = [names]
            effect_names.extend(names)

        if not file_ids and not effect_names:
            self._reset_drag_preview()
            return

        coords = self._event_seconds_track(event)
        if coords is None:
            coords = (0.0, self.track_list[0].data.get("number") if self.track_list else 0, 0)
        pos_seconds, track_num, _ = coords
        pos = QPointF(pos_seconds, 0)

        if effect_names:
            self._apply_effect_drop(effect_names, pos_seconds, track_num)
            self._reset_drag_preview()
            return

        for idx, fid in enumerate(file_ids):
            ignore_refresh = idx < len(file_ids) - 1
            if mime_html == "transition":
                item = self.addTransition(
                    fid,
                    pos,
                    track_num,
                    ignore_refresh=ignore_refresh,
                    call_manual_move=False,
                )
                if item:
                    pos.setX(pos.x() + (item.get("end", 0.0) - item.get("start", 0.0)))
            else:
                clip = self.addClip(
                    fid,
                    pos,
                    track_num,
                    ignore_refresh=ignore_refresh,
                    call_manual_move=False,
                )
                if clip:
                    pos.setX(pos.x() + (clip.get("end", 0.0) - clip.get("start", 0.0)))
        self._reset_drag_preview()

    def dragLeaveEvent(self, event):
        event.accept()
        self._reset_drag_preview(delete_items=True)

    def _ensure_drag_payload_from_event(self, event):
        if self._drag_payload:
            return self._drag_payload
        mime = event.mimeData()
        if mime.hasUrls():
            self._drag_payload = {"type": "os_drop", "urls": mime.urls()}
            return self._drag_payload
        mime_html = mime.html()
        if mime_html in {"clip", "transition"}:
            try:
                ids = json.loads(mime.text())
            except Exception:
                ids = []
            if not isinstance(ids, list):
                ids = [ids]
            self._drag_payload = {"type": mime_html, "ids": ids}
            self.item_type = mime_html
            self.new_item = True
        elif mime_html == "effect":
            self._drag_payload = {"type": "effect"}
        return self._drag_payload

    def _viewport_offsets(self):
        view_w = self.scrollbar_position[3] or 1.0
        timeline_w = self.scrollbar_position[2] or view_w
        left = self.scrollbar_position[0]
        h_offset = left * timeline_w
        max_scroll = max(0.0, timeline_w - view_w)
        if h_offset > max_scroll:
            h_offset = max_scroll

        view_h = self.v_scrollbar_position[3] or 1.0
        content_h = self.v_scrollbar_position[2] or view_h
        top = self.v_scrollbar_position[0]
        v_offset = top * content_h
        max_vscroll = max(0.0, content_h - view_h)
        if v_offset > max_vscroll:
            v_offset = max_vscroll
        return h_offset, v_offset

    def _event_seconds_track(self, event):
        pos = event.pos()
        if pos.x() < self.track_name_width or pos.y() < self.ruler_height:
            return None
        if not self.track_list:
            return None
        pixels_per_second = float(self.pixels_per_second or 0.0)
        if pixels_per_second <= 0.0:
            return None
        vertical_factor = float(self.vertical_factor or 0.0)
        if vertical_factor <= 0.0:
            return None
        h_offset, v_offset = self._viewport_offsets()
        pos_seconds = (pos.x() - self.track_name_width + h_offset) / pixels_per_second
        pos_seconds = max(0.0, pos_seconds)
        track_idx = int((pos.y() - self.ruler_height + v_offset) / vertical_factor)
        if track_idx < 0 or track_idx >= len(self.track_list):
            return None
        track_num = self.track_list[track_idx].data.get("number")
        return pos_seconds, track_num, track_idx

    def _snap_new_item_start(self, seconds, duration):
        seconds = max(0.0, seconds)
        if not self.enable_snapping:
            return seconds
        self.geometry.ensure()
        pixels_per_second = float(self.pixels_per_second or 0.0)
        if pixels_per_second <= 0.0:
            return seconds

        h_offset, _ = self._viewport_offsets()
        left_px = self.track_name_width + seconds * pixels_per_second - h_offset
        width_px = max(0.0, duration) * pixels_per_second

        ignore_ids = {
            getattr(entry.get("model"), "id", None)
            for entry in self._drag_preview_items
        }

        original_bbox = getattr(self, "drag_bbox", QRectF())
        original_ignore = getattr(self, "_snap_ignore_ids", set())
        preview_bbox = QRectF(left_px, original_bbox.y(), width_px, original_bbox.height())
        if preview_bbox.height() <= 0.0:
            preview_bbox.setHeight(self.vertical_factor or 1.0)
        try:
            self._snap_ignore_ids = {obj_id for obj_id in ignore_ids if obj_id is not None}
            self.drag_bbox = preview_bbox
            delta = self.snap.snap_dx(0.0)
        finally:
            self._snap_ignore_ids = original_ignore
            self.drag_bbox = original_bbox

        snapped = seconds + float(delta)
        snapped = max(0.0, snapped)
        return self._snap_time(snapped)

    def _ensure_drag_preview(self, pos_seconds, track_num):
        if self._drag_preview_items:
            return True
        payload = self._drag_payload or {}
        ids = payload.get("ids")
        if not ids:
            return False
        if not hasattr(self, "item_ids"):
            self.item_ids = []
        self.item_ids.clear()
        if track_num is None:
            return False
        preview_items = []
        current_start = pos_seconds
        for idx, source_id in enumerate(ids):
            ignore_refresh = idx < len(ids) - 1
            if payload.get("type") == "transition":
                item = self.addTransition(
                    source_id,
                    QPointF(current_start, 0),
                    track_num,
                    ignore_refresh=ignore_refresh,
                    call_manual_move=False,
                )
                if not item:
                    continue
                model = Transition.get(id=item.get("id"))
                duration = max(0.0, float(item.get("end", 0.0)) - float(item.get("start", 0.0)))
            else:
                item = self.addClip(
                    source_id,
                    QPointF(current_start, 0),
                    track_num,
                    ignore_refresh=ignore_refresh,
                    call_manual_move=False,
                )
                if not item:
                    continue
                model = Clip.get(id=item.get("id"))
                duration = max(0.0, float(item.get("end", 0.0)) - float(item.get("start", 0.0)))
            if not model:
                continue
            offset = current_start - pos_seconds
            preview_items.append({
                "model": model,
                "offset": offset,
                "duration": duration,
            })
            self.item_ids.append(model.id)
            current_start += duration

        if not preview_items:
            return False

        self._drag_preview_items = preview_items
        self._drag_preview_type = payload.get("type")
        self.geometry.mark_dirty()
        self.update()
        return True

    def _update_drag_preview_position(self, pos_seconds, track_num):
        if not self._drag_preview_items:
            return
        min_offset = min(entry.get("offset", 0.0) for entry in self._drag_preview_items)
        max_end = max(
            entry.get("offset", 0.0) + entry.get("duration", 0.0)
            for entry in self._drag_preview_items
        )
        group_duration = max(0.0, max_end - min_offset)
        snapped_start = self._snap_new_item_start(pos_seconds, group_duration)
        total = len(self._drag_preview_items)
        for idx, entry in enumerate(self._drag_preview_items):
            model = entry.get("model")
            if not model:
                continue
            new_pos = max(0.0, snapped_start + entry.get("offset", 0.0))
            model.data["position"] = new_pos
            model.data["layer"] = track_num
            rect = self.geometry.calc_item_rect(model)
            self.geometry.update_item_rect(model, rect)
        self.drag_bbox = self._compute_preview_bbox()
        self._keyframes_dirty = True
        self.update()

    def _compute_preview_bbox(self):
        if not self._drag_preview_items:
            return QRectF()
        rects = []
        for entry in self._drag_preview_items:
            model = entry.get("model")
            if not model:
                continue
            rect = self.geometry.calc_item_rect(model)
            if rect:
                rects.append(QRectF(rect))
        if not rects:
            return QRectF()
        bbox = QRectF(rects[0])
        for rect in rects[1:]:
            bbox = bbox.united(rect)
        return bbox

    def _reset_drag_preview(self, delete_items=False):
        deleted_any = False
        if delete_items and self._drag_preview_items:
            for entry in self._drag_preview_items:
                model = entry.get("model")
                if isinstance(model, Clip) or isinstance(model, Transition):
                    try:
                        model.delete()
                        deleted_any = True
                    except Exception:
                        pass
        self._drag_preview_items = []
        self._drag_preview_type = None
        self._drag_payload = None
        if hasattr(self, "item_ids"):
            self.item_ids = []
        self.new_item = False
        self.item_type = None
        self.drag_bbox = QRectF()
        if deleted_any:
            self._update_project_duration()
        self.geometry.mark_dirty()
        self.update()

    def _finalize_drag_preview(self):
        total = len(self._drag_preview_items)
        if not total:
            self._reset_drag_preview()
            return
        for idx, entry in enumerate(self._drag_preview_items):
            model = entry.get("model")
            if not model:
                continue
            ignore_refresh = idx < total - 1
            if isinstance(model, Transition):
                self.update_transition_data(
                    model.data,
                    only_basic_props=False,
                    ignore_refresh=ignore_refresh,
                )
            else:
                self.update_clip_data(
                    model.data,
                    only_basic_props=False,
                    ignore_reader=True,
                    ignore_refresh=ignore_refresh,
                )
        self._update_project_duration()
        self._drag_preview_items = []
        self._drag_preview_type = None
        self._drag_payload = None
        if hasattr(self, "item_ids"):
            self.item_ids = []
        self.new_item = False
        self.item_type = None
        TimelineWidget.changed(self, None)
        self.update()

    def _apply_effect_drop(self, effect_names, pos_seconds, track_num):
        if not effect_names:
            return
        timeline = getattr(self.win, "timeline", None)
        if not timeline:
            return
        pos_seconds = max(0.0, float(pos_seconds))
        try:
            track_num = int(track_num)
        except (TypeError, ValueError):
            return
        candidates = Clip.filter(layer=track_num)
        for clip in candidates:
            data = clip.data if isinstance(clip.data, dict) else {}
            clip_position = float(data.get("position", 0.0) or 0.0)
            clip_start = float(data.get("start", 0.0) or 0.0)
            clip_end = float(data.get("end", clip_start) or clip_start)
            duration = clip_end - clip_start
            if duration <= 0.0:
                continue
            clip_finish = clip_position + duration
            if pos_seconds == 0.0 or clip_position <= pos_seconds <= clip_finish:
                timeline.addEffect(effect_names, QPointF(pos_seconds, track_num))
                break


    def resizeEvent(self, event):
        """Widget resize event"""
        event.accept()
        self.delayed_size = self.size()
        self.geometry.mark_dirty()
        self.update()
        self.delayed_resize_timer.start()

    def delayed_resize_callback(self):
        """Callback for resize event timer (to delay the resize event, and prevent lots of similar resize events)"""
        project = get_app().project
        project_duration = float(project.get("duration") or 0.0)
        tick_pixels = float(project.get("tick_pixels") or 100.0)

        if self.delayed_size:
            self.scrollbar_position[3] = self.delayed_size.width()
            self.v_scrollbar_position[3] = self.delayed_size.height()

        view_w = float(self.scrollbar_position[3] or 0.0)

        # Preserve the existing zoom factor and update the visible range instead of
        # recomputing zoom from the viewport size. This keeps manual zoom choices
        # intact when the dock is resized.
        self.pixels_per_second = tick_pixels / float(self.zoom_factor or 1.0)
        timeline_w = project_duration * self.pixels_per_second
        self.scrollbar_position[2] = timeline_w

        if project_duration > 0.0 and view_w > 0.0:
            visible_secs = self.zoom_factor * (view_w / tick_pixels)
            width_norm = max(0.0, min(visible_secs / project_duration, 1.0))
        else:
            width_norm = 1.0 if timeline_w > 0.0 else 0.0

        left_norm = self.scrollbar_position[0]
        right_norm = left_norm + width_norm
        if right_norm > 1.0:
            right_norm = 1.0
            left_norm = max(0.0, right_norm - width_norm)

        self.scrollbar_position[0] = left_norm
        self.scrollbar_position[1] = right_norm
        self.h_scroll_offset = left_norm * (timeline_w or 0.0)

        self.geometry.mark_dirty()
        self.update()
        get_app().window.TimelineScrolled.emit(list(self.scrollbar_position))

    # Capture wheel event to alter zoom/scale of widget
    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoomIn()
            else:
                self.zoomOut()
            event.accept()
            return

        # Vertical scrolling
        if self.v_scrollbar_position[3] > 0 and self.v_scrollbar_position[2] > self.v_scrollbar_position[3]:
            delta = -event.angleDelta().y() / 120.0
            view_ratio = self.v_scrollbar_position[1] - self.v_scrollbar_position[0]
            new_top = self.v_scrollbar_position[0] + delta * view_ratio * 0.1
            new_top = max(0.0, min(new_top, 1.0 - view_ratio))
            self.v_scrollbar_position[0] = new_top
            self.v_scrollbar_position[1] = new_top + view_ratio
            self.geometry.mark_dirty()
            self.update()
            event.accept()
        else:
            event.ignore()

    def setZoomFactor(self, zoom_factor, emit=True):
        """Set the current zoom factor"""
        # Force recalculation of clips
        self.zoom_factor = zoom_factor
        TimelineWidget.changed(self, None)

        # Update normalized scroll width to match new zoom
        project_duration = get_app().project.get("duration") or 0.0
        view_w = self.scrollbar_position[3]
        tick_pixels = float(get_app().project.get("tick_pixels") or 100.0)
        self.pixels_per_second = tick_pixels / float(self.zoom_factor or 1.0)
        timeline_w = project_duration * self.pixels_per_second
        self.scrollbar_position[2] = timeline_w
        if project_duration > 0.0 and view_w > 0.0 and timeline_w > 0.0:
            visible_secs = zoom_factor * (view_w / tick_pixels)
            width_norm = max(0.0, min(visible_secs / project_duration, 1.0))
        else:
            width_norm = 1.0 if timeline_w > 0.0 else 0.0

        anchor_seconds = 0.0
        if self.fps_float:
            anchor_seconds = max(0.0, (self.current_frame - 1) / self.fps_float)
        self._center_on_seconds(
            anchor_seconds,
            width_norm=width_norm,
            timeline_w=timeline_w,
            view_w=view_w,
        )

        slider_positions = list(self.scrollbar_position)
        slider = getattr(self.win, "sliderZoomWidget", None)
        if slider:
            if abs(slider.zoom_factor - zoom_factor) > 1e-6:
                slider.setZoomFactor(zoom_factor, emit=False)
            slider.update_scrollbars(slider_positions)

        if emit:
            # Persist zoom back to the project so dependent widgets (zoom slider, etc.)
            # remain synchronized with QWidget-originated zoom gestures.
            current_scale = float(get_app().project.get("scale") or 15.0)
            if abs(zoom_factor - current_scale) > 1e-6:
                get_app().updates.ignore_history = True
                get_app().updates.update(["scale"], zoom_factor)
                get_app().updates.ignore_history = False

            # Emit zoom and scrollbar signals
            get_app().window.TimelineZoom.emit(self.zoom_factor)
            get_app().window.TimelineScrolled.emit(slider_positions)

        # Schedule repaint
        self.update()

    def zoomIn(self):
        """Zoom into timeline"""
        if self.zoom_factor >= 10.0:
            new_factor = self.zoom_factor - 5.0
        elif self.zoom_factor >= 4.0:
            new_factor = self.zoom_factor - 2.0
        else:
            new_factor = self.zoom_factor * 0.8

        # Emit zoom signal
        self.setZoomFactor(new_factor)

    def zoomOut(self):
        """Zoom out of timeline"""
        if self.zoom_factor >= 10.0:
            new_factor = self.zoom_factor + 5.0
        elif self.zoom_factor >= 4.0:
            new_factor = self.zoom_factor + 2.0
        else:
            # Ensure zoom is reversable when using only keyboard zoom
            new_factor = min(self.zoom_factor * 1.25, 4.0)

        # Emit zoom signal
        self.setZoomFactor(new_factor)

    def update_scrollbars(self, new_positions):
        """Consume the current scroll bar positions from the webview timeline"""
        if self.mouse_dragging:
            return

        if list(new_positions) == self.scrollbar_position:
            return

        self.scrollbar_position = list(new_positions)
        timeline_w = self.scrollbar_position[2] or self.scrollbar_position[3] or 0.0
        self.h_scroll_offset = self.scrollbar_position[0] * timeline_w

        # Check for empty clip rectangles
        if not self.geometry.clip_entries:
            TimelineWidget.changed(self, None)

        # Recompute geometry for new scrollbar positions
        self.geometry.mark_dirty()

        # Disable auto center
        self.is_auto_center = False

        # Schedule repaint
        self.update()

    def set_scroll_left(self, new_left):
        width_norm = self.scrollbar_position[1] - self.scrollbar_position[0]
        left = max(0.0, min(new_left, 1.0 - width_norm))
        if abs(left - self.scrollbar_position[0]) < 1e-9:
            return
        self.scrollbar_position[0] = left
        self.scrollbar_position[1] = left + width_norm
        timeline_w = self.scrollbar_position[2] or self.scrollbar_position[3] or 0.0
        self.h_scroll_offset = left * timeline_w
        self.geometry.mark_dirty()
        self.update()

    def _center_on_seconds(self, seconds, width_norm=None, timeline_w=None, view_w=None):
        timeline_w = float(timeline_w or 0.0)
        view_w = float(view_w or 0.0)
        if timeline_w <= 0.0 or view_w <= 0.0:
            self.scrollbar_position[0] = 0.0
            self.scrollbar_position[1] = 1.0 if timeline_w > 0.0 else 0.0
            self.h_scroll_offset = 0.0
            return False

        if width_norm is None:
            width_norm = self.scrollbar_position[1] - self.scrollbar_position[0]
        width_norm = max(0.0, min(width_norm, 1.0))

        view_px = width_norm * timeline_w
        if view_px <= 0.0:
            view_px = min(view_w, timeline_w)
            width_norm = view_px / timeline_w if timeline_w else 0.0

        if timeline_w <= view_px + 1e-9:
            left_px = 0.0
            width_norm = 1.0
        else:
            anchor_px = max(0.0, min(seconds * self.pixels_per_second, timeline_w))
            half = view_px / 2.0
            left_px = anchor_px - half
            max_left = max(0.0, timeline_w - view_px)
            if left_px < 0.0:
                left_px = 0.0
            elif left_px > max_left:
                left_px = max_left

        left_norm = left_px / timeline_w if timeline_w else 0.0
        right_norm = left_norm + width_norm
        if right_norm > 1.0:
            right_norm = 1.0
            left_norm = max(0.0, right_norm - width_norm)

        changed = (
            abs(left_norm - self.scrollbar_position[0]) > 1e-6
            or abs(right_norm - self.scrollbar_position[1]) > 1e-6
        )

        self.scrollbar_position[0] = left_norm
        self.scrollbar_position[1] = right_norm
        self.h_scroll_offset = left_norm * timeline_w
        return changed

    def centerOnPlayhead(self, emit=True):
        anchor_seconds = 0.0
        if self.fps_float:
            anchor_seconds = max(0.0, (self.current_frame - 1) / self.fps_float)
        width_norm = self.scrollbar_position[1] - self.scrollbar_position[0]
        timeline_w = self.scrollbar_position[2] or 0.0
        view_w = self.scrollbar_position[3] or 0.0
        changed = self._center_on_seconds(
            anchor_seconds,
            width_norm=width_norm if width_norm > 0 else None,
            timeline_w=timeline_w,
            view_w=view_w,
        )
        if not changed:
            return

        slider_positions = list(self.scrollbar_position)
        slider = getattr(self.win, "sliderZoomWidget", None)
        if slider:
            slider.update_scrollbars(slider_positions)
        if emit:
            get_app().window.TimelineScrolled.emit(slider_positions)
        self.geometry.mark_dirty()
        self.update()

    def handle_selection(self):
        # Force recalculation of clips and repaint
        TimelineWidget.changed(self, None)
        self._keyframes_dirty = True
        self.update()

    def _move_playhead(self, x_pos):
        fps = get_app().project.get("fps")
        fps_float = float(fps.get("num", 24)) / float(fps.get("den", 1) or 1)
        offset_px = getattr(self, "h_scroll_offset", 0.0)
        pps = float(self.pixels_per_second or 0.0)
        if pps <= 0.0:
            return
        seconds = max(0.0, (x_pos - self.track_name_width + offset_px) / pps)
        if fps_float:
            frame = int(round(seconds * fps_float)) + 1
        else:
            frame = 1
        frame = max(1, frame)
        self.win.SeekSignal.emit(frame)

    def update_playhead_pos(self, currentFrame):
        """Callback when position is changed"""
        self.current_frame = currentFrame

        # Schedule repaint
        self.update()

    def handle_play(self):
        """Callback when play button is clicked"""
        self.is_auto_center = True

    def connect_playback(self):
        """Connect playback signals"""
        self.win.preview_thread.position_changed.connect(self.update_playhead_pos)
        self.win.PlaySignal.connect(self.handle_play)



    # ----- State machine helper methods -----

    def _hitTest(self, pos):
        return self.geometry.hit(pos)

    def _effect_icon_at(self, pos):
        for entry in reversed(self._effect_icon_rects):
            rect = entry.get("rect")
            if isinstance(rect, QRectF) and rect.contains(pos):
                return entry
        return None

    def _trigger_effect_context_menu(self, icon_entry, modifiers=None):
        """Handle context menu interaction on an effect badge."""
        if not isinstance(icon_entry, dict):
            return False
        effect = icon_entry.get("effect")
        effect_id = icon_entry.get("effect_id")
        if effect_id is None and isinstance(effect, dict):
            effect_id = effect.get("id")
        if effect_id is None:
            return False
        effect_id_str = str(effect_id)
        ctrl = False
        if modifiers is None and self._last_event and hasattr(self._last_event, "modifiers"):
            modifiers = self._last_event.modifiers()
        if modifiers is not None:
            ctrl = bool(modifiers & Qt.ControlModifier)
        self._select_timeline_item(effect_id_str, "effect", not ctrl)
        timeline = getattr(self.win, "timeline", None)
        if timeline:
            timeline.ShowEffectMenu(effect_id_str)
        return True

    def _selected_effect_ids(self):
        selected = getattr(self.win, "selected_effects", [])
        return {str(eff) for eff in selected if eff is not None}

    def _select_timeline_item(self, item_id, item_type, clear_existing):
        if item_id is None or not item_type:
            return
        item_id_str = str(item_id)
        if not item_id_str:
            return
        timeline = getattr(self.win, "timeline", None)
        if timeline:
            timeline.addSelection(item_id_str, item_type, clear_existing)
        self.win.addSelection(item_id_str, item_type, clear_existing)
        # Selection changes affect cached clip renders and keyframe visibility.
        self.clip_painter.clear_cache()
        self.geometry.mark_dirty()
        self._keyframes_dirty = True
        self.update()

    def _update_project_duration(self):
        timeline = getattr(self.win, "timeline", None)
        if not timeline:
            return

        furthest = 0.0

        for clip in Clip.filter():
            data = clip.data if isinstance(clip.data, dict) else {}
            position = float(data.get("position", 0.0) or 0.0)
            start = float(data.get("start", 0.0) or 0.0)
            end = float(data.get("end", start) or start)
            duration = max(0.0, end - start)
            finish = position + duration
            if finish > furthest:
                furthest = finish

        for tran in Transition.filter():
            data = tran.data if isinstance(tran.data, dict) else {}
            position = float(data.get("position", 0.0) or 0.0)
            start = float(data.get("start", 0.0) or 0.0)
            end = float(data.get("end", start) or start)
            duration = max(0.0, end - start)
            finish = position + duration
            if finish > furthest:
                furthest = finish

        min_length = 300.0
        padding = 10.0
        desired = max(min_length, furthest + padding)
        current = float(get_app().project.get("duration") or 0.0)
        if desired > current + 1e-3:
            timeline.resizeTimeline(desired)

    def _clip_menu_rect(self, rect):
        if not self.clip_painter.menu_pix:
            return QRectF()
        bw = self.clip_painter.clip_pen.widthF()
        width, height = self.clip_painter.logical_size(self.clip_painter.menu_pix)
        return QRectF(
            rect.x() + bw + self.clip_painter.menu_margin,
            rect.y() + bw + self.clip_painter.menu_margin,
            width,
            height,
        )

    def _transition_menu_rect(self, rect):
        if not self.transition_painter.menu_pix:
            return QRectF()
        bw = self.transition_painter.pen.widthF()
        width, height = self.transition_painter.logical_size(self.transition_painter.menu_pix)
        return QRectF(
            rect.x() + bw + self.transition_painter.menu_margin,
            rect.y() + bw + self.transition_painter.menu_margin,
            width,
            height,
        )

    def _track_menu_rect(self, name_rect):
        if not self.track_painter.menu_pix:
            return QRectF()
        width, height = self.track_painter.logical_size(self.track_painter.menu_pix)
        return QRectF(
            name_rect.x() + self.track_painter.name_border_width + self.track_painter.menu_margin,
            name_rect.y() + self.track_painter.menu_margin,
            width,
            height,
        )

    def _lookup_interpolation(self, value):
        try:
            idx = int(value)
        except (TypeError, ValueError):
            idx = 2
        if idx == 0:
            return "bezier"
        if idx == 1:
            return "linear"
        return "constant"

    def clip_has_pending_override(self, clip):
        if not isinstance(clip, Clip):
            return False
        return clip.id in self._pending_clip_overrides

    def clip_waveform_window(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        start = float(data.get("start", 0.0) or 0.0)
        end = float(data.get("end", start) or start)
        if end < start:
            end = start
        overrides = None
        if isinstance(clip, Clip):
            overrides = self._pending_clip_overrides.get(clip.id)

        pending_start = start
        pending_end = end
        initial_start = start
        initial_end = end
        scale_waveform = False
        if overrides:
            pending_start = float(overrides.get("start", pending_start) or pending_start)
            pending_end = float(overrides.get("end", pending_end) or pending_end)
            initial_start = float(overrides.get("initial_start", initial_start) or initial_start)
            initial_end = float(overrides.get("initial_end", initial_end) or initial_end)
            if pending_end < pending_start:
                pending_end = pending_start
            if initial_end < initial_start:
                initial_end = initial_start
            scale_waveform = bool(overrides.get("scale"))

        samples_per_second = getattr(self, "_waveform_samples_per_second", None)
        if not samples_per_second:
            try:
                samples_per_second = int(WAVEFORM_SAMPLES_PER_SECOND)
            except Exception:
                samples_per_second = 20
            if samples_per_second <= 0:
                samples_per_second = 20
            self._waveform_samples_per_second = samples_per_second

        ui_data = data.get("ui", {}) if isinstance(data, dict) else {}
        audio_data = ui_data.get("audio_data") if isinstance(ui_data, dict) else None
        sample_count = len(audio_data) if isinstance(audio_data, list) else 0
        media_duration = 0.0
        if sample_count:
            media_duration = float(sample_count) / float(samples_per_second)

        if media_duration <= 0.0:
            media_duration = max(initial_end, pending_end, end, start, 0.0)

        clip_span = max(initial_end - initial_start, 0.0)
        tolerance = 1.0 / float(samples_per_second)
        dataset_matches_clip = (
            media_duration > 0.0
            and clip_span > 0.0
            and abs(media_duration - clip_span) <= max(tolerance, clip_span * 1e-3)
        )
        origin = initial_start if dataset_matches_clip else 0.0

        def _ratio(value, offset):
            if media_duration <= 0.0:
                return 0.0
            relative = float(value) - float(offset)
            if relative < 0.0:
                relative = 0.0
            if relative > media_duration:
                relative = media_duration
            return relative / media_duration

        start_ratio = _ratio(pending_start, origin)
        end_ratio = _ratio(pending_end, origin)
        source_start_ratio = _ratio(initial_start, origin)
        source_end_ratio = _ratio(initial_end, origin)

        if end_ratio < start_ratio:
            end_ratio = start_ratio
        if source_end_ratio < source_start_ratio:
            source_end_ratio = source_start_ratio

        return {
            "start_ratio": start_ratio,
            "end_ratio": end_ratio,
            "scale": scale_waveform,
            "source_start_ratio": source_start_ratio,
            "source_end_ratio": source_end_ratio,
        }

    def clip_waveform_cache_token(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        ui_data = data.get("ui", {}) if isinstance(data, dict) else {}
        audio_data = ui_data.get("audio_data") if isinstance(ui_data, dict) else None
        if isinstance(audio_data, list):
            return len(audio_data)
        return 0

    def _normalize_color(self, value):
        if isinstance(value, QColor):
            col = QColor()
            col.setRgba(value.rgba())
            return col
        if isinstance(value, str):
            col = QColor(value)
            if col.isValid():
                return col
        if isinstance(value, (tuple, list)):
            try:
                r, g, b = value[:3]
                a = value[3] if len(value) > 3 else 255
                col = QColor()
                col.setRgb(int(r), int(g), int(b), int(a))
                return col
            except (TypeError, ValueError):
                return QColor()
        if isinstance(value, (int, float)):
            try:
                col = QColor()
                col.setRgba(int(value))
                return col
            except (TypeError, ValueError):
                return QColor()
        return QColor()

    def _effect_color(self, effect):
        color = self._normalize_color(effect_color_qcolor(effect))
        if not color.isValid():
            color = self._normalize_color(self.keyframe_painter.fill)
        return color

    def _keyframe_rect(self, clip_rect, seconds):
        size = max(2, self.keyframe_painter.size)
        pixels = max(self.pixels_per_second, 0.0001)
        x = clip_rect.left() + seconds * pixels
        baseline = clip_rect.bottom() - 0.5
        top = baseline - size / 2.0
        return QRectF(x - size / 2.0, top, size, size)

    def _collect_keyframes_from_data(
        self,
        data,
        *,
        clip_rect,
        clip,
        transition,
        clip_start,
        clip_end,
        owner_id,
        object_type,
        selected,
        color,
        effect=None,
        object_id=None,
        override=None,
        base_path=(),
    ):
        if not isinstance(data, (dict, list)):
            return []

        fps = self.fps_float or 1.0
        duration = max(0.0, clip_end - clip_start)
        override = override or {}
        initial_start = float(override.get("initial_start", clip_start) or clip_start)
        initial_end = float(override.get("initial_end", clip_end) or clip_end)
        initial_duration = max(0.0, initial_end - initial_start)
        scale_override = bool(override.get("scale")) and initial_duration > 0 and duration > 0
        show_outside = bool(override.get("show_outside"))
        markers = {}

        skip_keys = {"effects", "ui", "reader", "cache"}

        def store(frame_value, interpolation_value, point_obj=None, point_path=None):
            if frame_value is None:
                return
            try:
                frame_float = float(frame_value)
            except (TypeError, ValueError):
                return
            seconds_abs = frame_float - 1.0
            seconds_abs /= fps
            dimmed = False
            if scale_override:
                normalized = (seconds_abs - initial_start) / initial_duration
                if normalized < 0.0:
                    normalized = 0.0
                if normalized > 1.0:
                    normalized = 1.0
                local_seconds = normalized * duration
            else:
                local_seconds = seconds_abs - clip_start
                if not show_outside:
                    if local_seconds < -1e-6 or local_seconds > duration + 1e-6:
                        return
                elif local_seconds < -1e-6 or local_seconds > duration + 1e-6:
                    dimmed = True
            frame_int = int(round(frame_float))
            previous = markers.get(frame_int)
            path_value = None
            if point_path is not None:
                try:
                    path_value = tuple(point_path)
                except TypeError:
                    path_value = None

            previous_paths = []
            if previous:
                stored_paths = previous.get("paths")
                if isinstance(stored_paths, (list, tuple)):
                    previous_paths.extend(stored_paths)
                prev_single = previous.get("path")
                if prev_single is not None and prev_single not in previous_paths:
                    previous_paths.append(prev_single)
                if path_value is not None and path_value not in previous_paths:
                    previous_paths.append(path_value)
                if previous_paths:
                    previous["paths"] = tuple(previous_paths)
                    if len(previous_paths) == 1:
                        previous["path"] = previous_paths[0]
                    else:
                        previous["path"] = None
                elif "paths" in previous:
                    previous.pop("paths", None)
                if previous["selected"] and not selected:
                    return
            entry_paths = list(previous_paths)
            if not previous and path_value is not None:
                entry_paths.append(path_value)
            color_value = None
            if isinstance(point_obj, dict):
                for key in ("color", "colour", "icon_color"):
                    val = point_obj.get(key)
                    if val:
                        color_value = val
                        break
                if not color_value:
                    ui_data = point_obj.get("ui") if isinstance(point_obj.get("ui"), dict) else None
                    if ui_data:
                        for key in ("color", "colour", "icon_color"):
                            val = ui_data.get(key)
                            if val:
                                color_value = val
                                break
            entry = {
                "frame": frame_int,
                "seconds": local_seconds,
                "display_seconds": max(0.0, min(local_seconds, duration)) if duration > 0 else 0.0,
                "interpolation": self._lookup_interpolation(interpolation_value),
                "selected": bool(selected),
                "dimmed": dimmed,
            }
            if not color_value and previous:
                color_value = previous.get("color")
            if color_value:
                entry["color"] = color_value
            if entry_paths:
                entry["paths"] = tuple(entry_paths)
                if len(entry_paths) == 1:
                    entry["path"] = entry_paths[0]
            markers[frame_int] = entry

        def walk(obj, path):
            if isinstance(obj, dict):
                points = obj.get("Points")
                if isinstance(points, list) and len(points) > 1:
                    base_path = path + (("dict", "Points"),)
                    for index, point in enumerate(points):
                        co = point.get("co", {}) if isinstance(point, dict) else {}
                        store(
                            co.get("X"),
                            point.get("interpolation"),
                            point,
                            base_path + (("list", index),),
                        )
                red = obj.get("red")
                if isinstance(red, dict):
                    red_points = red.get("Points")
                    if isinstance(red_points, list) and len(red_points) > 1:
                        base_path = path + (("dict", "red"), ("dict", "Points"))
                        for index, point in enumerate(red_points):
                            co = point.get("co", {}) if isinstance(point, dict) else {}
                            store(
                                co.get("X"),
                                point.get("interpolation"),
                                point,
                                base_path + (("list", index),),
                            )
                for key, value in obj.items():
                    if key in skip_keys:
                        continue
                    if isinstance(value, (dict, list)):
                        walk(value, path + (("dict", key),))
            elif isinstance(obj, list):
                for index, item in enumerate(obj):
                    if isinstance(item, (dict, list)):
                        walk(item, path + (("list", index),))

        try:
            initial_path = tuple(base_path)
        except TypeError:
            initial_path = ()
        walk(data, initial_path)

        if not markers:
            return []

        object_id = object_id or (
            str(getattr(clip, "id", ""))
            if clip
            else str(getattr(transition, "id", owner_id))
        )
        base_color = self._normalize_color(color)
        if not base_color.isValid():
            base_color = self._normalize_color(self.keyframe_painter.fill)

        result = []
        for frame, info in markers.items():
            rect = self._keyframe_rect(clip_rect, info["seconds"])
            if object_type == "clip":
                color_obj = self._normalize_color(self.keyframe_painter.fill)
            else:
                color_obj = self._normalize_color(base_color)
                info_color = info.get("color")
                override = self._normalize_color(info_color)
                if override.isValid():
                    color_obj = override
                if not color_obj.isValid():
                    color_obj = self._normalize_color(self.keyframe_painter.fill)
            marker = {
                "type": object_type,
                "owner_id": str(owner_id),
                "clip": clip,
                "transition": transition,
                "effect": effect,
                "frame": info["frame"],
                "display_frame": info["frame"],
                "seconds": info["seconds"],
                "display_seconds": info.get("display_seconds", info["seconds"]),
                "interpolation": info["interpolation"],
                "selected": info["selected"],
                "color": color_obj,
                "clip_rect": clip_rect,
                "clip_start": clip_start,
                "clip_end": clip_end,
                "rect": rect,
                "object_id": str(object_id),
                "object_type": "clip" if object_type in ("clip", "effect") else "transition",
                "key": (object_type, str(owner_id), info["frame"]),
                "dimmed": info.get("dimmed", False),
            }
            if object_type == "effect":
                marker["effect_id"] = str(owner_id)
            paths = info.get("paths")
            if paths:
                try:
                    marker["data_paths"] = tuple(paths)
                except TypeError:
                    pass
                if len(paths) == 1:
                    marker["data_path"] = paths[0]
            else:
                path_value = info.get("path")
                if path_value:
                    marker["data_path"] = path_value
            result.append(marker)
        return result

    def _build_clip_keyframes(self, rect, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        base_start = float(data.get("start", 0.0) or 0.0)
        base_end = float(data.get("end", base_start) or base_start)
        if base_end < base_start:
            base_end = base_start
        clip_start = base_start
        clip_end = base_end
        override_ctx = None
        overrides = self._pending_clip_overrides.get(clip.id)
        if overrides:
            clip_start = overrides.get("start", clip_start)
            clip_end = overrides.get("end", clip_end)
            if clip_end < clip_start:
                clip_end = clip_start
            initial_start = overrides.get("initial_start", base_start)
            initial_end = overrides.get("initial_end", base_end)
            override_ctx = {
                "initial_start": initial_start,
                "initial_end": initial_end,
                "scale": bool(overrides.get("scale")),
                "show_outside": not bool(overrides.get("scale")),
            }

        clip_selected = clip.id in getattr(self.win, "selected_clips", [])
        effects = data.get("effects", []) if isinstance(data, dict) else []
        selected_effect_ids_global = self._selected_effect_ids()
        effect_selected_ids = set()
        for eff in effects:
            if not isinstance(eff, dict):
                continue
            eff_id = eff.get("id")
            eff_id_str = str(eff_id) if eff_id is not None else ""
            if not eff_id_str:
                continue
            if eff.get("selected") or eff_id_str in selected_effect_ids_global:
                effect_selected_ids.add(eff_id_str)
        if not clip_selected and not effect_selected_ids:
            return []

        markers = []
        base_selected = clip_selected and not bool(effect_selected_ids)
        markers.extend(
            self._collect_keyframes_from_data(
                data,
                clip_rect=rect,
                clip=clip,
                transition=None,
                clip_start=clip_start,
                clip_end=clip_end,
                owner_id=str(clip.id),
                object_type="clip",
                selected=base_selected,
                color=self.keyframe_painter.fill,
                object_id=str(clip.id),
                override=override_ctx,
            )
        )

        for eff_index, eff in enumerate(effects):
            if not isinstance(eff, dict):
                continue
            effect_id = eff.get("id")
            if effect_id is None:
                continue
            effect_id_str = str(effect_id)
            color = self._effect_color(eff)
            eff_selected = effect_id_str in effect_selected_ids
            markers.extend(
                self._collect_keyframes_from_data(
                    eff,
                    clip_rect=rect,
                    clip=clip,
                    transition=None,
                    clip_start=clip_start,
                    clip_end=clip_end,
                    owner_id=effect_id_str,
                    object_type="effect",
                    selected=eff_selected,
                    color=color,
                    effect=eff,
                    object_id=str(clip.id),
                    override=override_ctx,
                    base_path=(("dict", "effects"), ("list", eff_index)),
                )
            )

        return markers

    def _build_transition_keyframes(self, rect, transition):
        if transition.id not in getattr(self.win, "selected_transitions", []):
            return []
        data = transition.data if isinstance(transition.data, dict) else {}
        clip_start = float(data.get("start", 0.0) or 0.0)
        clip_end = float(data.get("end", clip_start) or clip_start)
        if clip_end < clip_start:
            clip_end = clip_start
        return self._collect_keyframes_from_data(
            data,
            clip_rect=rect,
            clip=None,
            transition=transition,
            clip_start=clip_start,
            clip_end=clip_end,
            owner_id=str(transition.id),
            object_type="transition",
            selected=True,
            color=self.keyframe_painter.fill,
            object_id=str(transition.id),
        )

    def _refresh_keyframe_markers(self):
        markers = []
        for rect, clip, _selected in self.geometry.iter_clips():
            markers.extend(self._build_clip_keyframes(rect, clip))
        for rect, tran, _selected in self.geometry.iter_transitions():
            markers.extend(self._build_transition_keyframes(rect, tran))

        drag = self._dragging_keyframe
        if drag and drag.get("key") and markers:
            pending_seconds = drag.get("pending_seconds")
            pending_frame = drag.get("pending_frame")
            for marker in markers:
                if marker.get("key") == drag.get("key"):
                    if pending_seconds is not None:
                        marker["seconds"] = pending_seconds
                        marker["display_seconds"] = pending_seconds
                        marker["rect"] = self._keyframe_rect(marker["clip_rect"], pending_seconds)
                        marker["dimmed"] = False
                    if pending_frame is not None:
                        marker["display_frame"] = pending_frame
                    break

        self._keyframe_markers = markers
        self._keyframes_dirty = False

    def _ensure_keyframe_markers(self):
        if self._keyframes_dirty:
            self._refresh_keyframe_markers()

    def _update_snap_keyframe_targets(self, clip):
        if not isinstance(clip, Clip) or self.enable_timing:
            self._snap_keyframe_seconds = []
            return

        clip_id = getattr(clip, "id", None)
        if clip_id is None:
            self._snap_keyframe_seconds = []
            return

        overrides = self._pending_clip_overrides.get(clip.id)
        position = None
        if overrides:
            position = overrides.get("position")
        if position is None:
            position = clip.data.get("position", 0.0)
        try:
            position = float(position)
        except (TypeError, ValueError):
            position = 0.0

        self._ensure_keyframe_markers()
        clip_id_str = str(clip_id)
        seconds = []
        active_edge = getattr(self, "_resize_edge", None)
        frame_epsilon = 0.0
        if self.fps_float:
            frame_epsilon = 1.0 / float(self.fps_float)

        for marker in getattr(self, "_keyframe_markers", []):
            if marker.get("object_id") != clip_id_str:
                continue
            marker_seconds = marker.get("display_seconds", marker.get("seconds"))
            if marker_seconds is None:
                continue
            try:
                local_seconds = float(marker_seconds)
            except (TypeError, ValueError):
                continue
            if active_edge == "left":
                epsilon = frame_epsilon if frame_epsilon > 0.0 else 1e-6
                if local_seconds <= epsilon + 1e-9:
                    # Skip the keyframe that sits at the clip's first frame when
                    # trimming from the left edge so we don't continually snap back
                    # to the original in-point before the user has moved away from
                    # it. Other keyframes (including ones very near the start) are
                    # still considered.
                    continue

            seconds.append(position + local_seconds)

        seconds.sort()
        self._snap_keyframe_seconds = seconds

    def _get_keyframe_at(self, pos):
        self._ensure_keyframe_markers()
        for marker in reversed(self._keyframe_markers):
            rect = marker.get("rect")
            if isinstance(rect, QRectF) and rect.contains(pos):
                return marker
        return None

    def _clamp_keyframe_seconds(self, seconds, clip_start, clip_end):
        max_sec = clip_end
        if self.fps_float:
            max_sec = max(clip_start, clip_end - (1.0 / self.fps_float))
        if seconds < clip_start:
            seconds = clip_start
        if seconds > max_sec:
            seconds = max_sec
        return seconds

    def _move_keyframes_in_object(self, obj, old_frame, new_frame):
        if isinstance(obj, dict):
            points = obj.get("Points")
            if isinstance(points, list):
                for point in points:
                    if not isinstance(point, dict):
                        continue
                    co = point.get("co")
                    if isinstance(co, dict):
                        x_val = co.get("X")
                        try:
                            frame = int(round(float(x_val)))
                        except (TypeError, ValueError):
                            continue
                        if frame == old_frame:
                            co["X"] = new_frame
            for channel in ("red", "green", "blue"):
                chan = obj.get(channel)
                if isinstance(chan, dict):
                    self._move_keyframes_in_object(chan, old_frame, new_frame)
            for key, value in obj.items():
                if key in ("ui",):
                    continue
                if isinstance(value, (dict, list)):
                    self._move_keyframes_in_object(value, old_frame, new_frame)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    self._move_keyframes_in_object(item, old_frame, new_frame)

    def _keyframe_base_position(self, info):
        clip = None
        transition = None
        if isinstance(info, dict):
            clip = info.get("clip")
            transition = info.get("transition")
        else:
            clip = getattr(info, "clip", None)
            transition = getattr(info, "transition", None)

        base_position = 0.0
        if clip:
            data = clip.data if isinstance(clip.data, dict) else {}
            try:
                base_position = float(data.get("position", 0.0) or 0.0)
            except (TypeError, ValueError):
                base_position = 0.0
        elif transition:
            data = transition.data if isinstance(transition.data, dict) else {}
            try:
                base_position = float(data.get("position", 0.0) or 0.0)
            except (TypeError, ValueError):
                base_position = 0.0
        return base_position

    def _marker_absolute_seconds(self, marker):
        if not isinstance(marker, dict):
            return None
        seconds = marker.get("seconds")
        if seconds is None:
            seconds = marker.get("display_seconds")
        try:
            local = float(seconds)
        except (TypeError, ValueError):
            return None
        base_position = self._keyframe_base_position(marker)
        return base_position + local

    def _compute_keyframe_snap_targets(self, marker):
        if marker is None:
            return []
        self._ensure_keyframe_markers()
        targets = []
        seen = set()

        def add_target(seconds, tolerance=None):
            try:
                value = float(seconds)
            except (TypeError, ValueError):
                return
            if value < 0.0:
                value = 0.0
            key = round(value, 6)
            if key in seen:
                return
            seen.add(key)
            if tolerance is not None:
                try:
                    tol = float(tolerance)
                except (TypeError, ValueError):
                    tol = None
                if tol and tol > 0.0:
                    targets.append({"seconds": value, "tolerance": tol})
                    return
            targets.append(value)

        current_key = marker.get("key")
        for other in getattr(self, "_keyframe_markers", []):
            if other is marker:
                continue
            if current_key is not None and other.get("key") == current_key:
                continue
            absolute = self._marker_absolute_seconds(other)
            if absolute is None:
                continue
            add_target(absolute)

        snap_helper = getattr(self, "snap", None)
        if snap_helper and hasattr(snap_helper, "keyframe_snap_seconds"):
            for entry in snap_helper.keyframe_snap_seconds(include_playhead=False):
                if isinstance(entry, dict):
                    add_target(entry.get("seconds"), entry.get("tolerance"))
                else:
                    add_target(entry)

        return targets

    def _apply_keyframe_snapping(self, drag, local_seconds):
        if not drag or not self.enable_snapping:
            return local_seconds
        targets = drag.get("snap_targets")
        if not targets:
            return local_seconds
        pps = float(self.pixels_per_second or 0.0)
        if pps <= 0.0:
            return local_seconds
        tolerance_px = 0.0
        snap_helper = getattr(self, "snap", None)
        if snap_helper and hasattr(snap_helper, "_snap_tolerance_px"):
            try:
                tolerance_px = float(snap_helper._snap_tolerance_px())
            except (TypeError, ValueError):
                tolerance_px = 0.0
        if tolerance_px <= 0.0:
            return local_seconds
        tolerance_sec = tolerance_px / pps
        try:
            current = float(local_seconds)
        except (TypeError, ValueError):
            return local_seconds
        base_position = self._keyframe_base_position(drag)
        absolute = base_position + current
        best = None
        min_diff = None
        for target in targets:
            tolerance_override = None
            if isinstance(target, dict):
                value = target.get("seconds")
                tolerance_override = target.get("tolerance")
            else:
                value = target
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            local_tol = tolerance_sec
            if tolerance_override is not None:
                try:
                    override = float(tolerance_override)
                except (TypeError, ValueError):
                    override = None
                if override is not None and override > 0.0:
                    local_tol = override
            diff = abs(value - absolute)
            if diff > local_tol + 1e-9:
                continue
            if min_diff is None or diff < min_diff:
                min_diff = diff
                best = value
        if best is None:
            return local_seconds
        snapped = best - base_position
        if snapped < 0.0:
            snapped = 0.0
        return snapped

    def _resolve_data_path(self, data, path):
        current = data
        if not path:
            return current
        for entry in path:
            if not isinstance(entry, tuple) or len(entry) != 2:
                return None
            kind, key = entry
            if kind == "dict":
                if isinstance(current, dict):
                    current = current.get(key)
                else:
                    return None
            elif kind == "list":
                if not isinstance(current, list):
                    return None
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    return None
                if index < 0 or index >= len(current):
                    return None
                current = current[index]
            else:
                return None
            if current is None:
                return None
        return current

    def _set_keyframe_frame_at_path(self, data, path, new_frame):
        target = self._resolve_data_path(data, path)
        if not isinstance(target, dict):
            return False
        co = target.get("co")
        if not isinstance(co, dict):
            return False
        co["X"] = new_frame
        return True

    def _begin_keyframe_transaction(self):
        if not self._dragging_keyframe or self._dragging_keyframe.get("transaction_started"):
            return
        tid = str(uuid.uuid4())
        self._dragging_keyframe["transaction_started"] = True
        self._dragging_keyframe["transaction_id"] = tid
        timeline = getattr(self.win, "timeline", None)
        if timeline:
            timeline.StartKeyframeDrag(
                self._dragging_keyframe.get("object_type", "clip"),
                self._dragging_keyframe.get("object_id", ""),
                tid,
            )

    def _playhead_icon_rect(self):
        """Return QRectF describing the full rendered playhead icon."""
        if not self.playhead_painter.icon_pix:
            return QRectF()
        offset_px = getattr(self, "h_scroll_offset", 0.0)
        frame_seconds = 0.0
        if self.fps_float:
            frame_seconds = max(
                0.0, (max(1, self.current_frame) - 1) / self.fps_float
            )
        x = (
            self.track_name_width
            + frame_seconds * self.pixels_per_second
            - offset_px
        )
        ix = int(round(x))
        icon_w, icon_h = self.playhead_painter.logical_size(
            self.playhead_painter.icon_pix
        )
        return QRectF(
            ix + self.playhead_painter.icon_offset_x,
            self.playhead_painter.icon_offset_y,
            icon_w,
            icon_h,
        )

    def _playhead_handle_rect(self):
        """Return QRectF describing the draggable portion of the playhead."""
        icon_rect = self._playhead_icon_rect()
        if icon_rect.isNull():
            return QRectF()
        timeline_width = (
            float(self.width()) - float(self.track_name_width) - float(self.scroll_bar_thickness)
        )
        if timeline_width <= 0.0:
            return QRectF()
        max_handle_height = min(float(self.ruler_height), icon_rect.height())
        if max_handle_height <= 0.0:
            return QRectF()
        handle_height = icon_rect.height() * 0.12
        handle_height = max(12.0, handle_height)
        handle_height = min(handle_height, max_handle_height)
        handle_area = QRectF(
            icon_rect.x(),
            icon_rect.y(),
            icon_rect.width(),
            handle_height,
        )
        visible_band = QRectF(
            self.track_name_width,
            0.0,
            timeline_width,
            max_handle_height,
        )
        handle_area = handle_area.intersected(visible_band)
        return handle_area if not handle_area.isNull() else QRectF()

    def _playhead_hit(self, pos):
        """Return True if *pos* intersects the draggable playhead handle."""
        handle_rect = self._playhead_handle_rect()
        if handle_rect.isNull():
            return False
        return handle_rect.contains(pos)

    def _updateCursor(self, pos):
        if self._fixed_cursor is not None:
            self.setCursor(self._fixed_cursor)
            return

        self.geometry.ensure()

        # Playhead icon
        handle_rect = self._playhead_handle_rect()
        if (self.playhead_painter.icon_pix and not handle_rect.isNull() and handle_rect.contains(pos)):
            self.setCursor(self.cursors["hand"])
            return

        icon_entry = self._effect_icon_at(pos)
        if icon_entry:
            self.setCursor(Qt.PointingHandCursor)
            return

        # Transition menu icons
        for rect, _tran, _selected in self.geometry.iter_transitions(reverse=True):
            if self._transition_menu_rect(rect).contains(pos):
                self.setCursor(Qt.PointingHandCursor)
                return

        marker = self._get_keyframe_at(pos)
        if marker:
            self.setCursor(self.cursors.get("resize_x", Qt.SizeHorCursor))
            return

        # Clip menu icons
        for rect, _clip, _selected in self.geometry.iter_clips(reverse=True):
            if self._clip_menu_rect(rect).contains(pos):
                self.setCursor(Qt.PointingHandCursor)
                return

        # Clip/transition edges and drags (transitions prioritized)
        edge = 5
        for rect, _item, _selected, _type in self.geometry.iter_items(reverse=True):
            if rect.contains(pos):
                if abs(pos.x() - rect.left()) <= edge or abs(pos.x() - rect.right()) <= edge:
                    self.setCursor(self.cursors["resize_x"])
                else:
                    self.setCursor(self.cursors["hand"])
                return

        # Track menu icons
        for _track_rect, _track, name_rect in self.geometry.track_rects:
            mrect = self._track_menu_rect(name_rect)
            if mrect.contains(pos):
                self.setCursor(Qt.PointingHandCursor)
                return

        self.unsetCursor()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._last_event = event
            icon_entry = self._effect_icon_at(event.pos())
            if icon_entry and self._trigger_effect_context_menu(
                icon_entry, event.modifiers() if hasattr(event, "modifiers") else None
            ):
                event.accept()
                return
            if self._showContextMenu(event.pos()):
                event.accept()
            else:
                event.ignore()
            return

        if event.button() == Qt.MiddleButton:
            if self._startMiddlePan(event.pos()):
                event.accept()
                return

        self.geometry.ensure()
        pos = event.pos()
        if self._handle_menu_icon_clicks(pos):
            return

        self._assign_press_target(event)

        if self._start_scroll_drag_if_needed(pos):
            return

        if self._press_hit == "effect-icon":
            event.accept()
            return

        self._last_event = event
        self.events.pressed.emit(event)

    def _handle_menu_icon_clicks(self, pos):
        return (
            self._trigger_track_menu_icon(pos)
            or self._trigger_transition_menu_icon(pos)
            or self._trigger_clip_menu_icon(pos)
        )

    def _trigger_track_menu_icon(self, pos):
        for _track_rect, track, name_rect in self.geometry.track_rects:
            if self._track_menu_rect(name_rect).contains(pos) and hasattr(self.win, "timeline"):
                self.win.timeline.ShowTrackMenu(track.id)
                return True
        return False

    def _trigger_transition_menu_icon(self, pos):
        for rect, tran, _selected in self.geometry.iter_transitions(reverse=True):
            if self._transition_menu_rect(rect).contains(pos) and hasattr(self.win, "timeline"):
                self.win.timeline.ShowTransitionMenu(tran.id)
                return True
        return False

    def _trigger_clip_menu_icon(self, pos):
        for rect, clip, _selected in self.geometry.iter_clips(reverse=True):
            if self._clip_menu_rect(rect).contains(pos) and hasattr(self.win, "timeline"):
                self.win.timeline.ShowClipMenu(clip.id)
                return True
        return False

    def _assign_press_target(self, event):
        pos = event.pos()
        modifiers = event.modifiers() if hasattr(event, "modifiers") else Qt.NoModifier
        ctrl = bool(modifiers & Qt.ControlModifier)
        marker = self._get_keyframe_at(pos)
        if marker:
            self._press_hit = "keyframe"
            self._press_keyframe = marker
            self._press_keyframe_clear = not ctrl
            self._select_marker_owner(marker, clear_existing=self._press_keyframe_clear)
            return
        self._press_keyframe = None
        self._press_keyframe_clear = True
        icon_entry = self._effect_icon_at(pos)
        if icon_entry:
            self._press_hit = "effect-icon"
            self._press_effect_icon = icon_entry
            return
        self._press_effect_icon = None
        edge = 5
        for rect, item, _selected, _type in self.geometry.iter_items(reverse=True):
            if not rect.contains(pos):
                continue
            if abs(pos.x() - rect.left()) <= edge:
                self._press_hit = "clip-edge"
                self._resizing_item = item
                self._resize_edge = "left"
                return
            if abs(pos.x() - rect.right()) <= edge:
                self._press_hit = "clip-edge"
                self._resizing_item = item
                self._resize_edge = "right"
                return
        self._resizing_item = None
        self._resize_edge = None
        self._press_hit = self._hitTest(pos)

    def _start_scroll_drag_if_needed(self, pos):
        if self._press_hit == "h-scroll":
            self.scroll_bar_dragging = True
            self.mouse_dragging = True
            self.mouse_position = pos.x()
            self.scrollbar_position_previous = list(self.scrollbar_position)
            return True
        if self._press_hit == "v-scroll":
            self.v_scroll_bar_dragging = True
            self.mouse_dragging = True
            self.mouse_position = pos.y()
            self.v_scrollbar_position_previous = list(self.v_scrollbar_position)
            return True
        return False

    def mouseMoveEvent(self, event):
        self._last_event = event

        if self.scroll_bar_dragging:
            view_w = self.scrollbar_position[3] or 1.0
            width_norm = self.scrollbar_position_previous[1] - self.scrollbar_position_previous[0]
            handle_w = width_norm * view_w
            avail = view_w - handle_w
            delta_px = self.mouse_position - event.pos().x()
            delta = 0.0
            if avail > 0:
                delta = (delta_px / avail) * (1.0 - width_norm)
            new_left = self.scrollbar_position_previous[0] - delta
            new_left = max(0.0, min(new_left, 1.0 - width_norm))
            self.scrollbar_position = [new_left, new_left + width_norm,
                                       self.scrollbar_position[2], self.scrollbar_position[3]]
            get_app().window.TimelineScrolled.emit(list(self.scrollbar_position))
            self.geometry.mark_dirty()
            self.update()
            return

        if self.v_scroll_bar_dragging:
            view_h = self.v_scrollbar_position[3] or 1.0
            height_norm = self.v_scrollbar_position_previous[1] - self.v_scrollbar_position_previous[0]
            handle_h = height_norm * view_h
            avail = view_h - handle_h
            delta_py = self.mouse_position - event.pos().y()
            delta = 0.0
            if avail > 0:
                delta = (delta_py / avail) * (1.0 - height_norm)
            new_top = self.v_scrollbar_position_previous[0] - delta
            new_top = max(0.0, min(new_top, 1.0 - height_norm))
            self.v_scrollbar_position[0] = new_top
            self.v_scrollbar_position[1] = new_top + height_norm
            self.geometry.mark_dirty()
            self.update()
            return

        if self._middle_panning:
            self._updateMiddlePan(event.pos())
            return

        self._updateCursor(event.pos())
        self.events.moved.emit(event)

    def mouseReleaseEvent(self, event):
        self._last_event = event
        if event.button() == Qt.MiddleButton and self._middle_panning:
            self._finishMiddlePan()
            return
        if self.scroll_bar_dragging or self.v_scroll_bar_dragging:
            self.scroll_bar_dragging = False
            self.v_scroll_bar_dragging = False
            self.mouse_dragging = False
            return
        if self._press_hit == "effect-icon":
            event.accept()
            self._handle_effect_icon_click(self._press_effect_icon)
            self._press_hit = None
            self._press_effect_icon = None
            return
        self.events.released.emit(event)
        self._press_hit = None

    def contextMenuEvent(self, event):
        icon_entry = self._effect_icon_at(event.pos())
        if icon_entry:
            if self._trigger_effect_context_menu(
                icon_entry, event.modifiers() if hasattr(event, "modifiers") else None
            ):
                event.accept()
                return
        if not self._showContextMenu(event.pos()):
            event.ignore()

    def _startMiddlePan(self, pos):
        view_w = self.scrollbar_position[3]
        timeline_w = self.scrollbar_position[2]
        view_h = self.v_scrollbar_position[3]
        content_h = self.v_scrollbar_position[2]
        if not any((view_w, timeline_w, view_h, content_h)):
            return False
        self._middle_panning = True
        self.mouse_dragging = True
        self._middle_pan_anchor = QPointF(pos)
        self._middle_pan_scroll_start = list(self.scrollbar_position)
        self._middle_pan_vscroll_start = list(self.v_scrollbar_position)
        self._fix_cursor(self.cursors.get("hand", self.cursor()))
        return True

    def _updateMiddlePan(self, pos):
        if not self._middle_panning:
            return
        posf = QPointF(pos)
        delta = posf - self._middle_pan_anchor
        new_positions = list(self._middle_pan_scroll_start)
        new_v_positions = list(self._middle_pan_vscroll_start)

        view_w = new_positions[3] or self.width()
        timeline_w = new_positions[2] or view_w
        width_norm = new_positions[1] - new_positions[0]
        if timeline_w > 0 and width_norm < 1.0:
            left = new_positions[0] - (delta.x() / timeline_w)
            left = max(0.0, min(left, 1.0 - width_norm))
            new_positions[0] = left
            new_positions[1] = left + width_norm

        view_h = new_v_positions[3] or self.height()
        content_h = new_v_positions[2] or view_h
        height_norm = new_v_positions[1] - new_v_positions[0]
        if content_h > 0 and height_norm < 1.0:
            top = new_v_positions[0] - (delta.y() / content_h)
            top = max(0.0, min(top, 1.0 - height_norm))
            new_v_positions[0] = top
            new_v_positions[1] = top + height_norm

        changed = new_positions[:2] != self.scrollbar_position[:2]
        v_changed = new_v_positions[:2] != self.v_scrollbar_position[:2]
        if changed:
            self.scrollbar_position = new_positions
            get_app().window.TimelineScrolled.emit(list(self.scrollbar_position))
        if v_changed:
            self.v_scrollbar_position = new_v_positions
        if changed or v_changed:
            self.geometry.mark_dirty()
            self.update()

    def _finishMiddlePan(self):
        if not self._middle_panning:
            return
        self._middle_panning = False
        self.mouse_dragging = False
        self._release_cursor()

    def _showContextMenu(self, pos):
        """Show appropriate context menu for the position. Returns True if handled."""
        self.geometry.ensure()

        # Playhead context menu
        if self._playhead_hit(pos) and hasattr(self.win, "timeline"):
            # Convert frame number to seconds for backend API
            seconds = 0.0
            if self.fps_float:
                seconds = max(0.0, (max(1, self.current_frame) - 1) / self.fps_float)
            self.win.timeline.ShowPlayheadMenu(seconds)
            return True

        # Transition context menu (prioritized over clips)
        for rect, tran, _selected in self.geometry.iter_transitions(reverse=True):
            if rect.contains(pos) and hasattr(self.win, "timeline"):
                if tran.id not in getattr(self.win, "selected_transitions", []):
                    self._select_timeline_item(tran.id, "transition", True)
                self.win.timeline.ShowTransitionMenu(tran.id)
                return True

        # Clip context menu
        for rect, clip, _selected in self.geometry.iter_clips(reverse=True):
            if rect.contains(pos) and hasattr(self.win, "timeline"):
                if clip.id not in getattr(self.win, "selected_clips", []):
                    self._select_timeline_item(clip.id, "clip", True)
                self.win.timeline.ShowClipMenu(clip.id)
                return True

        # Track context menu
        for track_rect, track, name_rect in self.geometry.track_rects:
            if (track_rect.contains(pos) or name_rect.contains(pos)) and hasattr(self.win, "timeline"):
                self.win.timeline.ShowTrackMenu(track.id)
                return True

        return False

    def _startKeyframeDrag(self):
        marker = self._press_keyframe
        self._press_keyframe = None
        if not marker:
            return
        self.mouse_dragging = True
        self._dragging_keyframe = {
            "marker": marker,
            "key": marker.get("key"),
            "current_frame": marker.get("frame"),
            "pending_frame": marker.get("frame"),
            "pending_seconds": marker.get("display_seconds"),
            "transaction_started": False,
            "object_type": marker.get("object_type", "clip"),
            "object_id": marker.get("object_id", ""),
            "clip": marker.get("clip"),
            "transition": marker.get("transition"),
            "effect_id": marker.get("effect_id"),
            "clip_start": marker.get("clip_start", 0.0),
            "clip_end": marker.get("clip_end", 0.0),
            "moved": False,
            "data_path": marker.get("data_path"),
            "data_paths": tuple(marker.get("data_paths", ()) or ()),
            "clear_existing": bool(getattr(self, "_press_keyframe_clear", True)),
        }
        if not self._dragging_keyframe["data_paths"] and marker.get("data_path"):
            self._dragging_keyframe["data_paths"] = (marker.get("data_path"),)
        self._dragging_keyframe["snap_targets"] = tuple(self._compute_keyframe_snap_targets(marker))
        self._fix_cursor(self.cursors.get("resize_x", Qt.SizeHorCursor))
        self._keyframes_dirty = True

    def _keyframeMove(self, event):
        drag = self._dragging_keyframe
        if not drag:
            return
        marker = drag.get("marker", {})
        clip_rect = marker.get("clip_rect", QRectF())
        clip_start = drag.get("clip_start", 0.0)
        clip_end = drag.get("clip_end", clip_start)
        if clip_rect.isNull() or clip_end <= clip_start or self.pixels_per_second <= 0:
            return

        x = event.pos().x()
        x = max(clip_rect.left(), min(x, clip_rect.right()))
        local_px = x - clip_rect.left()
        seconds = clip_start + local_px / self.pixels_per_second
        seconds = self._clamp_keyframe_seconds(seconds, clip_start, clip_end)
        relative_seconds = max(0.0, seconds - clip_start)
        relative_seconds = self._apply_keyframe_snapping(drag, relative_seconds)
        seconds = clip_start + relative_seconds
        seconds = self._clamp_keyframe_seconds(seconds, clip_start, clip_end)
        seconds = self._snap_time(seconds)
        relative_seconds = max(0.0, seconds - clip_start)
        drag["pending_seconds"] = relative_seconds
        if self.fps_float:
            new_frame = int(round(seconds * self.fps_float)) + 1
        else:
            new_frame = drag.get("current_frame")
        drag["pending_frame"] = new_frame
        if new_frame != drag.get("current_frame"):
            self._begin_keyframe_transaction()
            if drag.get("transaction_started") and new_frame is not None:
                self._apply_keyframe_delta(drag, ignore_refresh=True)
        self._seek_to_marker_frame(marker, new_frame)
        self._keyframes_dirty = True
        self.update()

    def _apply_keyframe_delta(self, drag, ignore_refresh=False, force=False):
        marker = drag.get("marker")
        if not marker:
            return
        new_frame = drag.get("pending_frame")
        old_frame = drag.get("current_frame")
        if new_frame is None or old_frame is None:
            return
        do_move = new_frame != old_frame
        if not do_move and not force:
            return
        timeline = getattr(self.win, "timeline", None)
        if not timeline:
            return
        transaction_id = drag.get("transaction_id")
        data_paths = tuple(drag.get("data_paths") or ())
        data_path = drag.get("data_path") if drag.get("data_path") else None
        if marker.get("type") == "transition":
            transition = marker.get("transition")
            if not transition:
                return
            data_copy = json.loads(json.dumps(transition.data))
            moved_specific = False
            target_paths = data_paths if data_paths else (() if data_path is None else (data_path,))
            if target_paths:
                for path in target_paths:
                    if path and self._set_keyframe_frame_at_path(data_copy, path, new_frame):
                        moved_specific = True
                if (do_move or force) and isinstance(transition.data, (dict, list)) and moved_specific:
                    for path in target_paths:
                        if path:
                            self._set_keyframe_frame_at_path(transition.data, path, new_frame)
            if (do_move or force) and not moved_specific:
                self._move_keyframes_in_object(data_copy, old_frame, new_frame)
                if isinstance(transition.data, (dict, list)):
                    self._move_keyframes_in_object(transition.data, old_frame, new_frame)
            timeline.update_transition_data(
                data_copy,
                only_basic_props=False,
                ignore_refresh=ignore_refresh,
                transaction_id=transaction_id,
            )
        else:
            clip = marker.get("clip")
            if not clip:
                return
            data_copy = json.loads(json.dumps(clip.data))
            moved_specific = False
            target_paths = data_paths if data_paths else (() if data_path is None else (data_path,))
            if target_paths:
                for path in target_paths:
                    if path and self._set_keyframe_frame_at_path(data_copy, path, new_frame):
                        moved_specific = True
                if (do_move or force) and isinstance(clip.data, (dict, list)) and moved_specific:
                    for path in target_paths:
                        if path:
                            self._set_keyframe_frame_at_path(clip.data, path, new_frame)
            if (do_move or force) and not moved_specific:
                if marker.get("type") == "effect":
                    effect_id = marker.get("owner_id")
                    for eff in data_copy.get("effects", []):
                        if str(eff.get("id")) == str(effect_id):
                            self._move_keyframes_in_object(eff, old_frame, new_frame)
                            break
                    if isinstance(clip.data, dict):
                        for eff in clip.data.get("effects", []):
                            if str(eff.get("id")) == str(effect_id):
                                self._move_keyframes_in_object(eff, old_frame, new_frame)
                                break
                else:
                    self._move_keyframes_in_object(data_copy, old_frame, new_frame)
                    if isinstance(clip.data, (dict, list)):
                        self._move_keyframes_in_object(clip.data, old_frame, new_frame)
            timeline.update_clip_data(
                data_copy,
                only_basic_props=False,
                ignore_reader=True,
                ignore_refresh=ignore_refresh,
                transaction_id=transaction_id,
            )

        drag["current_frame"] = new_frame
        marker["frame"] = new_frame
        marker["display_frame"] = new_frame
        if self.fps_float:
            seconds_abs = (new_frame - 1.0) / self.fps_float
            clip_start = drag.get("clip_start", 0.0)
            marker["seconds"] = max(0.0, seconds_abs - clip_start)
            marker["display_seconds"] = marker["seconds"]
        if do_move or force:
            drag["moved"] = True

    def _select_marker_owner(self, marker, *, seek=False, clear_existing=True):
        if not marker:
            return

        marker_type = marker.get("type")
        target_id = None
        target_type = None

        if marker_type == "effect":
            target_id = marker.get("owner_id") or marker.get("effect_id")
            target_type = "effect"
        elif marker_type == "transition":
            transition = marker.get("transition")
            if transition:
                target_id = getattr(transition, "id", None)
                target_type = "transition"
        else:
            clip = marker.get("clip")
            if clip:
                target_id = getattr(clip, "id", None)
                target_type = "clip"

        if target_id is not None and target_type:
            self._select_timeline_item(target_id, target_type, clear_existing)

        if not seek:
            return

        timeline = getattr(self.win, "timeline", None)
        if not timeline:
            return

        clip = marker.get("clip")
        transition = marker.get("transition")
        clip_start = marker.get("clip_start", 0.0)
        frame = marker.get("frame", 1)
        fps = self.fps_float or 1.0
        base_position = 0.0
        if clip:
            data = clip.data if isinstance(clip.data, dict) else {}
            base_position = float(data.get("position", 0.0) or 0.0)
        elif transition:
            data = transition.data if isinstance(transition.data, dict) else {}
            base_position = float(data.get("position", 0.0) or 0.0)
        absolute = round(base_position * fps) + frame - round(clip_start * fps)
        absolute = max(1, int(absolute))
        timeline.SeekToKeyframe(absolute)

    def _handle_keyframe_click(self, marker, clear_existing=True):
        if not marker:
            return
        self._select_marker_owner(marker, seek=True, clear_existing=clear_existing)

    def _seek_to_marker_frame(self, marker, frame):
        if marker is None or frame is None:
            return
        fps = self.fps_float or 1.0
        clip = marker.get("clip")
        transition = marker.get("transition")
        clip_start = marker.get("clip_start", 0.0)
        base_position = 0.0
        if clip:
            data = clip.data if isinstance(clip.data, dict) else {}
            base_position = float(data.get("position", 0.0) or 0.0)
        elif transition:
            data = transition.data if isinstance(transition.data, dict) else {}
            base_position = float(data.get("position", 0.0) or 0.0)
        absolute = round(base_position * fps) + frame - round(clip_start * fps)
        absolute = max(1, int(absolute))
        self.win.SeekSignal.emit(absolute)

    def _finishKeyframeDrag(self):
        drag = self._dragging_keyframe
        if not drag:
            return
        started = drag.get("transaction_started")
        changed = drag.get("pending_frame") != drag.get("current_frame")
        moved = drag.get("moved")
        marker = drag.get("marker")
        timeline = getattr(self.win, "timeline", None)
        if started:
            if moved:
                if changed:
                    self._apply_keyframe_delta(drag)
                else:
                    self._apply_keyframe_delta(drag, force=True)
            if timeline:
                timeline.FinalizeKeyframeDrag(
                    drag.get("object_type", "clip"),
                    drag.get("object_id", ""),
                )
            if moved and hasattr(self.win, "show_property_timeout"):
                QTimer.singleShot(0, self.win.show_property_timeout)
        else:
            clear_existing = drag.get("clear_existing", True)
            self._handle_keyframe_click(marker, clear_existing=clear_existing)

        self._dragging_keyframe = None
        self.mouse_dragging = False
        self._keyframes_dirty = True
        self._release_cursor()
        self.update()

    def _handle_effect_icon_click(self, entry):
        if not isinstance(entry, dict):
            return
        effect = entry.get("effect")
        if not isinstance(effect, dict):
            return
        effect_id = entry.get("effect_id")
        if effect_id is None:
            effect_id = effect.get("id")
        if effect_id is None:
            return
        effect_id_str = str(effect_id)
        modifiers = Qt.NoModifier
        if self._last_event and hasattr(self._last_event, "modifiers"):
            modifiers = self._last_event.modifiers()
        ctrl = bool(modifiers & Qt.ControlModifier)
        self._select_timeline_item(effect_id_str, "effect", not ctrl)

    # ---- Clip drag ----
    def _startClipDrag(self):
        """Begin a drag operation on one or many selected clips/transitions."""
        e = self._last_event

        self.snap.reset()

        # Identify the item under the cursor (include clips and transitions)
        clicked_item = None
        for rect, item, _selected, _type in self.geometry.iter_items(reverse=True):
            if rect.contains(e.pos()):
                clicked_item = item
                break
        if clicked_item is None:
            return

        self._fix_cursor(self.cursors["hand"])

        # Each drag operation is grouped under a single undo transaction
        self._drag_transaction_id = str(uuid.uuid4())

        ctrl = bool(e.modifiers() & Qt.ControlModifier)
        already = (
            clicked_item.id in self.win.selected_clips or
            clicked_item.id in self.win.selected_transitions
        )

        if not already:
            sel_type = "transition" if isinstance(clicked_item, Transition) else "clip"
            # Replace existing selections unless the user is multi-selecting
            self.win.addSelection(clicked_item.id, sel_type, not ctrl)
            TimelineWidget.changed(self, None)

        # All selected clips and transitions participate in the drag
        self.dragging_items = [
            itm
            for _rect, itm, selected, _type in self.geometry.iter_items()
            if selected
        ]
        if not self.dragging_items:
            self.dragging_items = [clicked_item]

        # Map track number → index
        self._track_index_from_num = { t.data["number"]: idx for idx, t in enumerate(self.track_list) }
        self._track_num_from_index = { idx: t.data["number"] for idx, t in enumerate(self.track_list) }

        # Record each item’s starting position and layer index
        fps = float(self.fps_float or 0.0)
        use_frames = fps > 0.0
        self._drag_initial = {}
        for itm in self.dragging_items:
            data = itm.data if isinstance(itm.data, dict) else {}
            position = float(data.get("position", 0.0) or 0.0)
            start = float(data.get("start", 0.0) or 0.0)
            end = float(data.get("end", start) or start)
            duration = max(0.0, end - start)
            index = self._track_index_from_num.get(data.get("layer", 0), 0)

            entry = {
                "position": position,
                "index": index,
                "duration": duration,
            }

            if use_frames:
                entry["position_frames"] = int(round(position * fps))
                entry["duration_frames"] = int(round(duration * fps))

            self._drag_initial[itm.id] = entry

        # Seed pending overrides so geometry rebuilds use drag positions
        for itm in self.dragging_items:
            if isinstance(itm, Clip):
                override = self._pending_clip_overrides.setdefault(itm.id, {})
                override["position"] = float(itm.data.get("position", 0.0) or 0.0)
                override.setdefault("start", float(itm.data.get("start", 0.0) or 0.0))
                override.setdefault("end", float(itm.data.get("end", 0.0) or 0.0))
                override["layer"] = itm.data.get("layer", 0)
            elif isinstance(itm, Transition):
                override = self._pending_transition_overrides.setdefault(itm.id, {})
                override["position"] = float(itm.data.get("position", 0.0) or 0.0)
                override["start"] = float(itm.data.get("start", 0.0) or 0.0)
                override["end"] = float(itm.data.get("end", 0.0) or 0.0)
                override["layer"] = itm.data.get("layer", 0)

        # Bounding box for snapping calculations
        self.drag_bbox = self._compute_selected_bounding()

        # Horizontal offset from cursor to bbox-left
        self.drag_clip_offset = e.pos().x() - self.drag_bbox.x()

        # Starting track index
        self._drag_layer_idx_start = int(
            (e.pos().y() - self.ruler_height) / self.vertical_factor
        )

    def _dragMove(self):
        """Apply identical horizontal/vertical deltas to every dragged item."""
        if not getattr(self, "dragging_items", None):
            return
        e = self._last_event

        # -------- Horizontal delta (seconds) --------
        pps = float(self.pixels_per_second or 0.0)
        if pps <= 0.0:
            return

        new_bbox_x = e.pos().x() - self.drag_clip_offset
        delta_sec = (new_bbox_x - self.drag_bbox.x()) / pps

        # Snap horizontally ±1.5 s (pure x-axis)
        if self.enable_snapping:
            delta_sec = self._snap_delta(delta_sec)

        # -------- Vertical delta (track indexes) ----
        new_idx_under_cursor = int(
            (e.pos().y() - self.ruler_height) / self.vertical_factor
        )
        delta_idx = new_idx_under_cursor - self._drag_layer_idx_start

        # Clamp delta_idx so *all* items stay within valid index range
        orig_indices = [info["index"] for info in self._drag_initial.values()]
        if orig_indices:
            if min(orig_indices) + delta_idx < 0:
                delta_idx = -min(orig_indices)
            if max(orig_indices) + delta_idx >= len(self.track_list):
                delta_idx = (len(self.track_list) - 1) - max(orig_indices)

        # Clamp horizontal delta so all clips remain inside the timeline bounds
        start_positions = [info["position"] for info in self._drag_initial.values()]
        if start_positions:
            min_delta_sec = -min(start_positions)
            if delta_sec < min_delta_sec:
                delta_sec = min_delta_sec

        project_duration = 0.0
        project = get_app().project if get_app() else None
        if project:
            try:
                project_duration = float(project.get("duration") or 0.0)
            except Exception:
                project_duration = 0.0

        end_positions = [
            info["position"] + info.get("duration", 0.0)
            for info in self._drag_initial.values()
        ]
        if project_duration > 0.0 and end_positions:
            max_delta_sec = project_duration - max(end_positions)
            if delta_sec > max_delta_sec:
                delta_sec = max_delta_sec

        fps = float(self.fps_float or 0.0)
        frame_offset = None
        if fps > 0.0:
            frame_offset = int(round(delta_sec * fps))

            start_frames = [
                info.get("position_frames")
                for info in self._drag_initial.values()
                if info.get("position_frames") is not None
            ]
            if start_frames:
                min_frame_offset = -min(start_frames)
                if frame_offset < min_frame_offset:
                    frame_offset = min_frame_offset

            if project_duration > 0.0:
                timeline_frames = int(round(project_duration * fps))
            else:
                timeline_frames = None

            end_frames = []
            for info in self._drag_initial.values():
                start_frame = info.get("position_frames")
                if start_frame is None:
                    start_frame = int(round(info["position"] * fps))
                duration_frames = info.get("duration_frames")
                if duration_frames is None:
                    duration_frames = int(round(info.get("duration", 0.0) * fps))
                end_frames.append(start_frame + duration_frames)

            if timeline_frames is not None and end_frames:
                max_frame_offset = timeline_frames - max(end_frames)
                if frame_offset > max_frame_offset:
                    frame_offset = max_frame_offset

            delta_sec = frame_offset / fps

        # Reapply second-based bounds to account for frame rounding
        if start_positions:
            min_delta_sec = -min(start_positions)
            if delta_sec < min_delta_sec:
                delta_sec = min_delta_sec
        if project_duration > 0.0 and end_positions:
            max_delta_sec = project_duration - max(end_positions)
            if delta_sec > max_delta_sec:
                delta_sec = max_delta_sec

        # -------- Apply identical deltas ------------
        for itm in self.dragging_items:
            info = self._drag_initial[itm.id]
            start_pos_sec = info["position"]
            start_idx = info["index"]

            # New values
            if frame_offset is not None:
                start_frame = info.get("position_frames")
                if start_frame is None:
                    start_frame = int(round(start_pos_sec * fps))
                new_frame = max(0, start_frame + frame_offset)
                new_pos_sec = new_frame / fps
            else:
                new_pos_sec = start_pos_sec + delta_sec
            new_pos_sec = max(0.0, new_pos_sec)
            new_pos_sec = self._snap_time(new_pos_sec)
            new_idx = start_idx + delta_idx
            new_idx = max(0, min(new_idx, len(self.track_list) - 1))
            new_layer_num = self._track_num_from_index[new_idx]

            itm.data["position"] = new_pos_sec
            itm.data["layer"] = new_layer_num

            if isinstance(itm, Clip):
                override = self._pending_clip_overrides.setdefault(itm.id, {})
            else:
                override = self._pending_transition_overrides.setdefault(itm.id, {})
            override["position"] = new_pos_sec
            override["layer"] = new_layer_num

            # Update cached rect
            rect = self.geometry.calc_item_rect(itm)
            self.geometry.update_item_rect(itm, rect)

        # Immediate visual feedback
        self._keyframes_dirty = True
        self.update()

    def _finishClipDrag(self):
        """Persist all moved clips/transitions and refresh geometry."""
        if getattr(self, "dragging_items", None):
            self._preserve_overrides_once = True
            total = len(self.dragging_items)
            transaction_id = self._drag_transaction_id
            for idx, itm in enumerate(self.dragging_items):
                ignore_refresh = idx < total - 1
                if isinstance(itm, Transition):
                    self.update_transition_data(
                        itm.data,
                        only_basic_props=True,
                        ignore_refresh=ignore_refresh,
                        transaction_id=transaction_id,
                    )
                else:
                    self.update_clip_data(
                        itm.data,
                        only_basic_props=True,
                        ignore_reader=True,
                        ignore_refresh=ignore_refresh,
                        transaction_id=transaction_id,
                    )

        self.dragging_items = []
        self._drag_transaction_id = None
        self.snap.reset()
        self._update_project_duration()
        # Recompute geometry (snap may have shifted) and repaint
        TimelineWidget.changed(self, None)
        self.update()
        self._release_cursor()
        if self._last_event:
            self._updateCursor(self._last_event.pos())

    def _compute_selected_bounding(self):
        """Return a QRectF encompassing all currently-selected clips and transitions."""
        rects = [
            rect
            for rect, _item, selected, _type in self.geometry.iter_items()
            if selected
        ]
        if not rects:
            return QRectF()
        bbox = QRectF(rects[0])
        for rect in rects[1:]:
            bbox = bbox.united(rect)
        return bbox

    # ---------- Helper: horizontal snap (±1 sec) ----------
    # ---------- Helper: horizontal snap (±1.5 s) ----------
    def _snap_delta(self, delta_seconds):
        """
        Given a proposed horizontal delta (seconds) for the group drag, adjust it
        so the selection’s left or right edge “snaps” to the nearest clip edge
        within ±1.5 seconds.  Snapping is strictly horizontal—layer movement is
        unaffected.
        """
        original_ignore = getattr(self, "_snap_ignore_ids", set())
        try:
            ignore_ids = {
                getattr(item, "id", None)
                for item in getattr(self, "dragging_items", [])
            }
            self._snap_ignore_ids = {obj_id for obj_id in ignore_ids if obj_id is not None}
            return self.snap.snap_dx(delta_seconds)
        finally:
            self._snap_ignore_ids = original_ignore

    # ---- Resize track names ----
    def _startResize(self):
        if self._press_hit == "clip-edge" and self._resizing_item:
            self._startItemResize()
        else:
            self._resize_start = self.track_name_width

    def _resizeMove(self):
        if self._press_hit == "clip-edge" and self._resizing_item:
            self._itemResizeMove()
        else:
            new_width = max(40, self._last_event.pos().x())
            if new_width != self.track_name_width:
                self.track_name_width = new_width
                TimelineWidget.changed(self, None)

    def _finishResize(self):
        if self._press_hit == "clip-edge" and self._resizing_item:
            self._finishItemResize()
        else:
            pass

    # ---- Clip / Transition resize ----
    def _startItemResize(self):
        item = self._resizing_item
        if not item:
            return
        self.snap.reset()
        self._fix_cursor(self.cursors["resize_x"])
        rect = self.geometry.calc_item_rect(item)
        self._resize_initial_rect = rect
        self._resize_initial = {
            "start": float(item.data.get("start", 0.0)),
            "end": float(item.data.get("end", 0.0)),
            "position": float(item.data.get("position", 0.0)),
            "duration": float(item.data.get("duration", item.data.get("end", 0.0) - item.data.get("start", 0.0))),
        }
        self._resize_snap_ignore_backup = set(getattr(self, "_snap_ignore_ids", set()))
        item_id = getattr(item, "id", None)
        if item_id is not None:
            updated_ignore = set(self._resize_snap_ignore_backup)
            updated_ignore.add(item_id)
            self._snap_ignore_ids = updated_ignore
        if isinstance(item, Clip):
            self._timing_original_start = self._resize_initial["start"]
            self._pending_clip_overrides[item.id] = {
                "start": self._resize_initial["start"],
                "end": self._resize_initial["end"],
                "position": self._resize_initial["position"],
                "initial_start": self._resize_initial["start"],
                "initial_end": self._resize_initial["end"],
                "scale": bool(self.enable_timing),
            }
            sel_type = "clip"
        else:
            sel_type = "transition"
            self._snap_keyframe_seconds = []
        # Ensure item is selected
        self.win.addSelection(item.id, sel_type, False)

        if isinstance(item, Clip) and not self.enable_timing:
            self._update_snap_keyframe_targets(item)

    def _itemResizeMove(self):
        item = self._resizing_item
        if not item:
            return
        if isinstance(item, Transition):
            rect, start, end, position = self._compute_transition_resize(item)
        else:
            rect, start, end, position = self._compute_clip_resize(item)

        self._resize_new_start = start
        self._resize_new_end = end
        self._resize_new_position = position
        self.geometry.update_item_rect(item, rect)
        if isinstance(item, Clip):
            override = self._pending_clip_overrides.setdefault(
                item.id,
                {
                    "start": start,
                    "end": end,
                    "position": position,
                    "initial_start": self._resize_initial.get("start", start),
                    "initial_end": self._resize_initial.get("end", end),
                },
            )
            override["start"] = start
            override["end"] = end
            override["position"] = position
            override["scale"] = bool(self.enable_timing)
            self._keyframes_dirty = True
            if not self.enable_timing:
                timeline = getattr(self.win, "timeline", None)
                clip_id = getattr(item, "id", None)
                if timeline and self.fps_float and clip_id:
                    if self._resize_edge == "left":
                        frame_seconds = self._snap_time(start)
                    else:
                        frame_seconds = self._snap_time(end)
                    frame = int(round(frame_seconds * self.fps_float)) + 1
                    timeline.PreviewClipFrame(str(clip_id), max(1, frame))
                self._update_snap_keyframe_targets(item)
            else:
                self._snap_keyframe_seconds = []
        self.update()

    def _compute_transition_resize(self, item):
        event = self._last_event
        pps = self.pixels_per_second
        min_len = 1.0 / self.fps_float
        rect = self._resize_initial_rect
        width = self._resize_initial["end"]
        pos = self._resize_initial["position"]
        offset_px = getattr(self, "h_scroll_offset", 0.0)

        if self._resize_edge == "left":
            delta_sec = (event.pos().x() - rect.left()) / pps
            if self.enable_snapping:
                delta_sec = self.snap.snap_edge(pos, delta_sec)
            max_delta = width - min_len
            if delta_sec > max_delta:
                delta_sec = max_delta
            new_position = pos + delta_sec
            new_end = width - delta_sec
            if new_position < 0:
                new_position = 0
                new_end = (pos + width) - new_position
            rect_left = self.track_name_width + new_position * pps - offset_px
        else:
            delta_sec = (event.pos().x() - rect.right()) / pps
            if self.enable_snapping:
                delta_sec = self.snap.snap_edge(pos + width, delta_sec)
            min_delta = -(width - min_len)
            if delta_sec < min_delta:
                delta_sec = min_delta
            new_end = width + delta_sec
            new_position = pos
            rect_left = self.track_name_width + new_position * pps - offset_px

        rect_width = new_end * pps
        geom_rect = QRectF(rect_left, rect.y(), rect_width, rect.height())
        return geom_rect, 0.0, new_end, new_position

    def _compute_clip_resize(self, item):
        event = self._last_event
        pps = float(self.pixels_per_second or 0.0)
        rect = self._resize_initial_rect
        start = self._resize_initial["start"]
        end = self._resize_initial["end"]
        pos = self._resize_initial["position"]
        duration = self._resize_initial["duration"]
        offset_px = getattr(self, "h_scroll_offset", 0.0)
        fps = self.fps_float or 1.0
        min_len = 1.0 / fps

        if event is None or pps <= 0.0:
            geom_rect = QRectF(rect)
            return geom_rect, start, end, pos

        cursor_sec = self._seconds_from_x(event.pos().x())
        clip_span = max(end - start, min_len)

        if self._resize_edge == "left":
            delta_sec = cursor_sec - pos
            if self.enable_snapping:
                delta_sec = self.snap.snap_edge(pos, delta_sec)
            new_position = pos + delta_sec
            new_start = start + delta_sec
            new_end = end

            max_start = end - min_len
            if new_start < 0.0:
                new_start = 0.0
                new_position = pos - start
            if new_start > max_start:
                new_start = max_start
                new_position = pos + (max_start - start)
            if new_position < 0.0:
                diff = -new_position
                new_position = 0.0
                new_start += diff
            rect_left = self.track_name_width + new_position * pps - offset_px
        else:
            timeline_right = pos + clip_span
            delta_sec = cursor_sec - timeline_right
            if self.enable_snapping:
                delta_sec = self.snap.snap_edge(pos + (end - start), delta_sec)
            new_end = end + delta_sec
            new_start = start
            new_position = pos

            min_end = start + min_len
            if new_end < min_end:
                new_end = min_end
            if not self.enable_timing:
                max_end = start + duration
                if new_end > max_end:
                    new_end = max_end
            rect_left = self.track_name_width + new_position * pps - offset_px

        rect_width = (new_end - new_start) * pps
        geom_rect = QRectF(rect_left, rect.y(), rect_width, rect.height())
        return geom_rect, new_start, new_end, new_position

    def _finishItemResize(self):
        item = self._resizing_item
        if not item:
            return
        start = self._resize_new_start
        end = self._resize_new_end
        position = self._resize_new_position
        if isinstance(item, Clip):
            if self.enable_timing:
                duration = end - start
                item.data["start"] = self._timing_original_start
                item.data["end"] = self._snap_time(self._timing_original_start + duration)
                item.data["position"] = self._snap_time(position)
                self.RetimeClip(item.id, item.data["end"], item.data["position"])
            else:
                item.data["start"] = self._snap_time(start)
                item.data["end"] = self._snap_time(end)
                item.data["position"] = self._snap_time(position)
                self.update_clip_data(item.data, only_basic_props=True, ignore_reader=True)
        else:
            item.data["position"] = self._snap_time(position)
            item.data["start"] = 0.0
            item.data["end"] = self._snap_time(end)
            self.update_transition_data(item.data, only_basic_props=True)

        self._resizing_item = None
        self._snap_keyframe_seconds = []
        self.snap.reset()
        if hasattr(self, "_resize_snap_ignore_backup"):
            self._snap_ignore_ids = self._resize_snap_ignore_backup
            del self._resize_snap_ignore_backup
        self._update_project_duration()
        TimelineWidget.changed(self, None)
        self._release_cursor()
        if self._last_event:
            self._updateCursor(self._last_event.pos())

    # ---- Playhead move ----
    def _startPlayhead(self):
        self.dragging_playhead = True
        self._fix_cursor(self.cursors["hand"])
        self._move_playhead(self._last_event.pos().x())

    def _playheadMove(self):
        if self.dragging_playhead:
            self._move_playhead(self._last_event.pos().x())

    def _finishPlayhead(self):
        self.dragging_playhead = False
        self._release_cursor()
        if self._last_event:
            self._updateCursor(self._last_event.pos())

    # ---- Box selection ----
    def _startBoxSelect(self):
        e = self._last_event
        if not (e.modifiers() & Qt.ControlModifier):
            # Starting a new box selection clears existing selections
            self.win.clearSelections()
        self.box_start = e.pos()
        self.selection_rect = QRectF()

    def _boxMove(self):
        self.selection_rect = QRectF(self.box_start, self._last_event.pos()).normalized()
        self.update()

    def _finishBoxSelect(self):
        """Finalize box-select: add items intersecting the selection rectangle."""
        # Ensure geometry is up-to-date
        self.geometry.mark_dirty()
        self.geometry.ensure()

        # Add any item whose rect intersects selection_rect
        for rect, item, _selected, _type in self.geometry.iter_items():
            if rect.intersects(self.selection_rect):
                sel_type = "transition" if isinstance(item, Transition) else "clip"
                # False = don’t emit SelectionChanged (we’ll handle it ourselves)
                self.win.addSelection(item.id, sel_type, False)

        # Clear the box
        self.selection_rect = QRectF()

        # Recompute all clip/track geometry and repaint immediately
        TimelineWidget.changed(self, None)
        self.update()

