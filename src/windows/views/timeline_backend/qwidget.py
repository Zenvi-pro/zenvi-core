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
import math
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
    KeyframePanelPainter,
    SelectionPainter,
    ScrollbarPainter,
    KeyframePainter,
)
from .snap import SnapHelper
from .theme import DEFAULT_THEME, apply_theme as parse_theme
from .state import TimelineStateMachine
from .colors import effect_color_qcolor


TRACK_TOOLBAR_LEFT_OFFSET = 8.0
TRACK_TOOLBAR_SPACING_REDUCTION = 2.0
from classes.waveform import SAMPLES_PER_SECOND as WAVEFORM_SAMPLES_PER_SECOND

from classes.app import get_app
from classes.query import Clip, Transition, Effect, File
from classes.logger import log


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
        self._track_panel_enabled = {}
        self._panel_properties = {}
        self._panel_heights = {}
        self._panel_refresh_signature = None
        self._panel_selected_keyframes = {}
        self._panel_box_track = None
        self._panel_box_bounds = QRectF()
        self._panel_press_info = None
        self._dragging_panel_keyframes = None
        self.keyframe_panel_row_height = 24.0
        self.keyframe_panel_row_spacing = 4.0
        self.keyframe_panel_padding = 6.0

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

        # Track toolbar interaction state
        self._toolbar_hover_key = None
        self._toolbar_pressed_key = None
        self._toolbar_pressed_inside = False

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
        self.keyframe_panel_painter = KeyframePanelPainter(self)
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
            lambda: self._press_hit in ("background", "panel")
        ))
        idle.addTransition(_ConditionalTransition(
            self.events.pressed, keydrag,
            lambda: self._press_hit in ("keyframe", "panel-keyframe")
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
            self.keyframe_panel_painter,
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

        self._update_track_panel_properties()
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

        signature = self._panel_current_signature()
        if signature != self._panel_refresh_signature:
            self._panel_refresh_signature = signature
            if self._update_track_panel_properties():
                self.geometry.mark_dirty()

        self.geometry.ensure()
        self._ensure_keyframe_markers()

        self.bg_painter.paint(painter, event.rect())
        self.track_painter.paint_background(painter)
        self.keyframe_panel_painter.paint(painter, mode="underlay")
        self.clip_painter.paint(painter)
        self.transition_painter.paint(painter)
        self.marker_painter.paint(painter)
        self.keyframe_painter.paint(painter)
        self.track_painter.paint_names(painter)
        self.keyframe_panel_painter.paint(painter, mode="overlay")
        self.selection_painter.paint(painter)
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

    def _track_toolbar_buttons(self, track, name_rect):
        painter = self.track_painter
        order = getattr(painter, "toolbar_order", ())
        icons = getattr(painter, "toolbar_pixmaps", {})
        if not order or not icons or name_rect.isNull():
            return []

        margin = float(getattr(painter, "toggle_margin", 0.0) or 0.0)
        border = float(getattr(painter, "name_border_width", 0.0) or 0.0)
        menu_margin = float(getattr(painter, "menu_margin", 0.0) or 0.0)
        menu_w = 0.0
        if painter.menu_pix:
            menu_w, _ = painter.logical_size(painter.menu_pix)

        track_num = self.normalize_track_number(track.data.get("number"))

        base_height = min(float(self.vertical_factor or 0.0) or 0.0, name_rect.height())
        if base_height <= 0.0:
            base_height = name_rect.height()
        anchor_bottom = name_rect.y() + base_height

        buttons = []
        start_x = name_rect.x() + border + menu_margin * 2.0 + menu_w - TRACK_TOOLBAR_LEFT_OFFSET
        min_start = name_rect.x() + border
        if start_x < min_start:
            start_x = min_start
        current_x = start_x
        right_limit = name_rect.right()

        for key in order:
            pix_info = icons.get(key)
            if not pix_info:
                continue
            if key == "lock-toggle":
                variant = pix_info.get("locked") or pix_info.get("unlocked") or {}
                base_pix = variant.get("enabled") or variant.get("disabled")
            else:
                base_pix = pix_info.get("enabled") or pix_info.get("disabled")
            if not base_pix:
                continue
            pix_w, pix_h = painter.logical_size(base_pix)
            margin_x = max(0.0, margin - TRACK_TOOLBAR_SPACING_REDUCTION)
            width = max(0.0, pix_w + margin_x * 2.0)
            height = max(0.0, pix_h + margin * 2.0)
            if width <= 0.0 or height <= 0.0:
                continue

            top = anchor_bottom - height
            min_top = name_rect.y() + margin
            if top < min_top:
                top = min_top
            max_top = name_rect.bottom() - height
            if top > max_top:
                top = max_top
            available_height = name_rect.bottom() - top
            rect = QRectF(current_x, top, width, min(height, available_height))

            if rect.right() > right_limit:
                overflow = rect.right() - right_limit
                rect.setWidth(max(0.0, rect.width() - overflow))
                rect.moveLeft(right_limit - rect.width())

            if rect.width() <= 0.0:
                break

            buttons.append({
                "key": key,
                "track_id": track.id,
                "track_num": track_num,
                "rect": rect,
                "margin": margin,
                "margin_x": margin_x,
                "margin_y": margin,
                "pixmaps": pix_info,
            })
            current_x = rect.right() + margin_x
        return buttons

    def _track_toggle_rect(self, track, name_rect):
        buttons = self._track_toolbar_buttons(track, name_rect)
        return buttons[0]["rect"] if buttons else QRectF()

    def _get_toolbar_button(self, track_id, key):
        self.geometry.ensure()
        for _track_rect, track, name_rect in self.geometry.track_rects:
            if track.id != track_id:
                continue
            for button in self._track_toolbar_buttons(track, name_rect):
                if button["key"] == key:
                    info = dict(button)
                    info["track"] = track
                    info["name_rect"] = name_rect
                    return info
        return None

    def _track_toolbar_button_at(self, pos):
        self.geometry.ensure()
        for _track_rect, track, name_rect in self.geometry.track_rects:
            for button in self._track_toolbar_buttons(track, name_rect):
                if button["rect"].contains(pos):
                    info = dict(button)
                    info["track"] = track
                    info["name_rect"] = name_rect
                    return info
        return None

    def _toolbar_button_pixmap(self, track, button, hovered=False, pressed=False):
        pixmaps = button.get("pixmaps") or {}
        key = button.get("key")

        if key == "lock-toggle":
            locked = bool(getattr(track, "data", {}).get("lock"))
            variant = pixmaps.get("locked" if locked else "unlocked") or {}
            state = "enabled" if locked else "disabled"
            if hovered or pressed:
                state = "enabled"
            pix = variant.get(state) or variant.get("enabled") or variant.get("disabled")
            return pix

        if key == "keyframe-panel":
            track_num = button.get("track_num")
            enabled = bool(self._track_panel_enabled.get(track_num, False))
            state = "enabled" if enabled else "disabled"
            if hovered or pressed:
                state = "enabled"
            pix = pixmaps.get(state) or pixmaps.get("enabled") or pixmaps.get("disabled")
            return pix

        state = "enabled" if (hovered or pressed) else "disabled"
        pix = pixmaps.get(state) or pixmaps.get("enabled") or pixmaps.get("disabled")
        return pix

    def _find_track_by_id(self, track_id):
        for track in self.track_list:
            if getattr(track, "id", None) == track_id:
                return track
        return None

    def _toggle_track_panel(self, track_num):
        current = self._track_panel_enabled.get(track_num, False)
        new_state = not current
        self._track_panel_enabled[track_num] = new_state
        if not new_state:
            self._clear_panel_selection(track_num)
        self._update_track_panel_properties()
        self.geometry.mark_dirty()
        self.update()

    def _select_track_for_action(self, track_id):
        if not track_id or not hasattr(self.win, "selected_tracks"):
            return
        if getattr(self.win, "selected_tracks", None) != [track_id]:
            self.win.selected_tracks = [track_id]

    def _activate_track_toolbar_button(self, button):
        key = button.get("key")
        track_id = button.get("track_id")
        track_num = button.get("track_num")
        track = button.get("track") or self._find_track_by_id(track_id)

        if key == "keyframe-panel":
            if track_num is not None:
                self._toggle_track_panel(track_num)
            return

        if not self.win:
            return

        if key == "insert-above":
            action = getattr(self.win, "actionAddTrackAbove_trigger", None)
            if action and track_id:
                self._select_track_for_action(track_id)
                action()
            return

        if key == "insert-below":
            action = getattr(self.win, "actionAddTrackBelow_trigger", None)
            if action and track_id:
                self._select_track_for_action(track_id)
                action()
            return

        if key == "delete-track":
            action = getattr(self.win, "actionRemoveTrack_trigger", None)
            if action and track_id:
                self._select_track_for_action(track_id)
                action()
            return

        if key == "lock-toggle" and track_id:
            self._select_track_for_action(track_id)
            locked = bool(getattr(track, "data", {}).get("lock")) if track else False
            if locked:
                action = getattr(self.win, "actionUnlockTrack_trigger", None)
                if action:
                    action()
                if track:
                    track.data["lock"] = False
            else:
                action = getattr(self.win, "actionLockTrack_trigger", None)
                if action:
                    action()
                if track:
                    track.data["lock"] = True
            self.geometry.mark_dirty()
            self.update()

    def _update_toolbar_hover(self, pos):
        button = self._track_toolbar_button_at(pos)
        key = None
        if button:
            key = (button.get("track_id"), button.get("key"))
        if key != self._toolbar_hover_key:
            self._toolbar_hover_key = key
            self.update()

    def _update_toolbar_pressed_state(self, pos):
        if not self._toolbar_pressed_key:
            return
        button = self._get_toolbar_button(*self._toolbar_pressed_key)
        inside = bool(button and button.get("rect") and button["rect"].contains(pos))
        if inside != self._toolbar_pressed_inside:
            self._toolbar_pressed_inside = inside
            self.update()

    def _track_display_label(self, track):
        if not track or not isinstance(track.data, dict):
            return ""
        label = track.data.get("label")
        if label:
            return label
        layers = list(get_app().project.get("layers") or [])
        track_id = track.data.get("id")
        try:
            layers_sorted = sorted(layers, key=lambda item: item.get("number", 0))
        except Exception:
            layers_sorted = layers
        display_index = len(layers_sorted)
        for layer in reversed(layers_sorted):
            if layer.get("id") == track_id:
                break
            display_index -= 1
        if display_index <= 0:
            fallback_number = track.data.get("number")
            display_index = fallback_number if fallback_number not in (None, "") else 0
        if not display_index:
            display_index = 1
        _ = get_app()._tr
        return _("Track %s") % display_index

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

    def normalize_track_number(self, track_num):
        try:
            return int(track_num)
        except (TypeError, ValueError):
            return track_num

    def _panel_float(self, value, default=0.0):
        try:
            result = float(value)
        except (TypeError, ValueError):
            return default
        if math.isnan(result) or math.isinf(result):
            return default
        return result

    def get_track_panel_height(self, track_num):
        key = self.normalize_track_number(track_num)
        if not self._track_panel_enabled.get(key):
            return 0.0
        return float(self._panel_heights.get(key, 0.0) or 0.0)

    def get_track_panel_properties(self, track_num):
        key = self.normalize_track_number(track_num)
        info = self._panel_properties.get(key)
        if not isinstance(info, dict):
            return []
        return info.get("properties", [])

    def get_track_panel_context(self, track_num):
        key = self.normalize_track_number(track_num)
        info = self._panel_properties.get(key)
        if not isinstance(info, dict):
            return {}
        ctx = info.get("context")
        return ctx if isinstance(ctx, dict) else {}

    def get_clip_rect_by_id(self, clip_id):
        clip_id_str = str(clip_id)
        if not clip_id_str:
            return QRectF()
        for rect, clip, _selected in self.geometry.iter_clips():
            if str(getattr(clip, "id", "")) == clip_id_str:
                return rect
        return QRectF()

    def get_transition_rect_by_id(self, transition_id):
        transition_id_str = str(transition_id)
        if not transition_id_str:
            return QRectF()
        for rect, tran, _selected in self.geometry.iter_transitions():
            if str(getattr(tran, "id", "")) == transition_id_str:
                return rect
        return QRectF()

    def is_keyframe_panel_visible(self, track_num):
        key = self.normalize_track_number(track_num)
        if not self._track_panel_enabled.get(key):
            return False
        if self._panel_heights.get(key, 0.0) <= 0.0:
            return False
        return bool(self.get_track_panel_properties(key))

    def _panel_height_for_properties(self, count):
        try:
            total = int(count)
        except (TypeError, ValueError):
            total = 0
        if total <= 0:
            return 0.0
        padding = float(self.keyframe_panel_padding or 0.0)
        row_height = float(self.keyframe_panel_row_height or 0.0)
        spacing = float(self.keyframe_panel_row_spacing or 0.0)
        height = padding * 2.0 + row_height * total
        if total > 1:
            height += spacing * (total - 1)
        return height

    def _panel_property_key(self, prop):
        if not isinstance(prop, dict):
            return None
        key = prop.get("key")
        if key:
            return key
        name = prop.get("display_name")
        if name:
            return name
        return str(id(prop))

    def _panel_property_points_parent_path(self, prop):
        if not isinstance(prop, dict):
            return None
        paths = prop.get("point_paths") or []
        for path in paths:
            try:
                tuple_path = tuple(path)
            except TypeError:
                tuple_path = path
            if tuple_path:
                return tuple_path[:-1]
        for point in prop.get("points") or []:
            path = point.get("path")
            if not path:
                continue
            try:
                tuple_path = tuple(path)
            except TypeError:
                tuple_path = path
            if tuple_path:
                return tuple_path[:-1]
        return None

    def _panel_capture_base_properties(self, properties):
        base = {}
        for prop in properties or []:
            if not isinstance(prop, dict):
                continue
            if prop.get("placeholder"):
                continue
            key = self._panel_property_key(prop)
            if not key:
                continue
            points = []
            for point in prop.get("points") or []:
                if isinstance(point, dict):
                    points.append(dict(point))
            base[key] = points
        return base

    def _panel_capture_base_context(self, context):
        result = {}
        if not isinstance(context, dict):
            return result
        for key in ("position", "range_start_seconds", "range_end_seconds"):
            if key not in context:
                continue
            value = context.get(key)
            if value is None:
                continue
            try:
                result[key] = float(value)
            except (TypeError, ValueError):
                result[key] = value
        return result

    def _panel_current_signature(self):
        enabled = [
            self.normalize_track_number(track)
            for track, state in self._track_panel_enabled.items()
            if state
        ]
        try:
            enabled_sorted = tuple(sorted(enabled))
        except TypeError:
            enabled_sorted = tuple(enabled)

        selection_signature = []
        win = getattr(self, "win", None)
        if win is not None:
            try:
                selection = list(getattr(win, "selected_items", []) or [])
            except Exception:
                selection = []
            for entry in selection:
                sel_type = None
                sel_id = None
                if isinstance(entry, dict):
                    sel_type = entry.get("type")
                    sel_id = entry.get("id")
                else:
                    sel_type = getattr(entry, "type", None)
                    sel_id = getattr(entry, "id", None)
                    if sel_id is None and hasattr(entry, "get"):
                        try:
                            sel_id = entry.get("id")
                        except Exception:
                            sel_id = None
                selection_signature.append(
                    (
                        str(sel_type) if sel_type is not None else "",
                        str(sel_id) if sel_id is not None else str(entry),
                    )
                )

        return (tuple(selection_signature), enabled_sorted)

    def _panel_lane_padding(self):
        row_height = float(self.keyframe_panel_row_height or 0.0)
        if row_height <= 0.0:
            return 6.0
        return min(6.0, row_height * 0.25)

    def _panel_layout_constants(self):
        padding = float(self.keyframe_panel_padding or 0.0)
        row_height = float(self.keyframe_panel_row_height or 0.0)
        spacing = float(self.keyframe_panel_row_spacing or 0.0)
        lane_padding = self._panel_lane_padding()
        return padding, row_height, spacing, lane_padding

    def _panel_seconds_to_x(self, seconds):
        try:
            seconds_val = float(seconds)
        except (TypeError, ValueError):
            seconds_val = 0.0
        ctx = getattr(self.geometry, "_view_context", {}) or {}
        h_offset = ctx.get("h_offset", 0.0)
        origin = self.track_name_width - h_offset
        return origin + seconds_val * float(self.pixels_per_second or 0.0)

    def _panel_x_to_seconds(self, x_value):
        try:
            x_float = float(x_value)
        except (TypeError, ValueError):
            x_float = float(self.track_name_width or 0.0)
        ctx = getattr(self.geometry, "_view_context", {}) or {}
        h_offset = ctx.get("h_offset", 0.0)
        origin = self.track_name_width - h_offset
        pixels = float(self.pixels_per_second or 0.0)
        if pixels <= 0.0:
            return 0.0
        return (x_float - origin) / pixels

    def _panel_bounds_for_track(self, track_num):
        key = self.normalize_track_number(track_num)
        self.geometry.ensure()
        for _track_rect, track, name_rect in self.geometry.track_rects:
            current = self.normalize_track_number(track.data.get("number"))
            if current != key:
                continue
            panel_rect = self.geometry.panel_rects.get(current)
            if not panel_rect or panel_rect.height() <= 0.0:
                return QRectF()
            return QRectF(
                name_rect.x(),
                panel_rect.y(),
                name_rect.width() + panel_rect.width(),
                panel_rect.height(),
            )
        return QRectF()

    def _iter_panel_lanes(self):
        padding, row_height, spacing, lane_padding = self._panel_layout_constants()
        if row_height <= 0.0:
            return
        self.geometry.ensure()
        for _track_rect, track, name_rect in self.geometry.track_rects:
            track_num = self.normalize_track_number(track.data.get("number"))
            panel_rect = self.geometry.panel_rects.get(track_num)
            if not panel_rect or panel_rect.height() <= 0.0:
                continue
            properties = self.get_track_panel_properties(track_num)
            if not properties:
                continue
            context = self.get_track_panel_context(track_num)
            toggle_rect = self._track_toggle_rect(track, name_rect)
            indent = 0.0
            if not toggle_rect.isNull():
                indent = max(0.0, toggle_rect.x() - name_rect.x())
            y = panel_rect.y() + padding
            for prop in properties:
                if y + row_height > panel_rect.bottom() - padding + 1.0:
                    break
                full_lane = QRectF(panel_rect.x(), y, panel_rect.width(), row_height)
                lane_left = max(full_lane.left(), float(self.track_name_width or 0.0))
                right_limit = float(self.width() - self.scroll_bar_thickness)
                lane_right = min(full_lane.right(), right_limit)
                if lane_right < lane_left:
                    lane_right = lane_left
                lane_rect = QRectF(lane_left, y, lane_right - lane_left, row_height)
                label_rect = QRectF(name_rect.x(), y, name_rect.width(), row_height)
                combined_width = label_rect.width() + max(0.0, lane_rect.width())
                combined = QRectF(label_rect.x(), y, combined_width, row_height)
                add_rect = QRectF()
                if isinstance(prop, dict) and not prop.get("placeholder"):
                    add_rect = self._panel_add_icon_rect(label_rect)
                    prop["_panel_add_rect"] = add_rect
                elif isinstance(prop, dict):
                    prop["_panel_add_rect"] = QRectF()
                yield {
                    "track": track_num,
                    "property": prop,
                    "lane_rect": lane_rect,
                    "full_lane_rect": full_lane,
                    "label_rect": label_rect,
                    "combined_rect": combined,
                    "context": context,
                    "lane_padding": lane_padding,
                    "indent": indent,
                    "render_rect": lane_rect,
                    "add_rect": add_rect,
                }
                y += row_height + spacing

    def _panel_lane_at(self, pos, include_label=True):
        for lane in self._iter_panel_lanes() or []:
            rect = lane["combined_rect"] if include_label else lane["lane_rect"]
            if rect.contains(pos):
                return lane
        return None

    def _panel_marker_rect(self, lane_rect, lane_padding, seconds):
        size = max(2.0, float(getattr(self.keyframe_panel_painter, "marker_size", 8.0) or 8.0))
        baseline = lane_rect.center().y()
        if lane_rect.height() > 0.0:
            baseline = max(
                lane_rect.top() + lane_padding,
                min(lane_rect.bottom() - lane_padding, baseline),
            )
        x_pos = self._panel_seconds_to_x(seconds)
        x_pos = max(lane_rect.left(), min(lane_rect.right(), x_pos))
        half = size / 2.0
        return QRectF(x_pos - half, baseline - half, size, size)

    def _panel_add_icon_rect(self, label_rect):
        painter = getattr(self, "keyframe_panel_painter", None)
        if not painter or not getattr(painter, "add_pix", None) or label_rect.isNull():
            return QRectF()
        pix = painter.add_pix
        pix_w, pix_h = painter.logical_size(pix)
        if pix_w <= 0.0 or pix_h <= 0.0:
            return QRectF()
        try:
            margin = float(getattr(painter, "add_margin", painter.label_margin))
        except (TypeError, ValueError):
            margin = float(painter.label_margin)
        if not math.isfinite(margin):
            margin = 0.0
        margin = max(0.0, margin)
        width = float(pix_w)
        height = float(pix_h)
        x = label_rect.right() - margin - width
        if x < label_rect.left():
            x = label_rect.left()
        y = label_rect.center().y() - height / 2.0
        if y < label_rect.top():
            y = label_rect.top()
        if y + height > label_rect.bottom():
            y = label_rect.bottom() - height
        return QRectF(x, y, width, height)

    def _panel_marker_at(self, pos):
        lane = self._panel_lane_at(pos, include_label=False)
        if not lane:
            return None
        prop = lane.get("property")
        lane_rect = lane.get("render_rect", lane.get("lane_rect", QRectF()))
        lane_padding = lane.get("lane_padding", self._panel_lane_padding())
        for point in prop.get("points") or []:
            seconds = point.get("seconds")
            if seconds is None:
                continue
            marker_rect = self._panel_marker_rect(lane_rect, lane_padding, seconds)
            if marker_rect.contains(pos):
                info = dict(lane)
                info["point"] = point
                info["marker_rect"] = marker_rect
                return info
        return None

    def _panel_add_button_at(self, pos):
        for lane in self._iter_panel_lanes() or []:
            add_rect = lane.get("add_rect")
            if isinstance(add_rect, QRectF) and not add_rect.isNull() and add_rect.contains(pos):
                info = dict(lane)
                info["add_rect"] = add_rect
                return info
        return None

    def _panel_compute_snap_targets(self, track_num, property_entry, entries, context):
        targets = []
        seen = set()

        def add_target(value, tolerance=None):
            try:
                seconds_val = float(value)
            except (TypeError, ValueError):
                return
            if seconds_val < 0.0:
                seconds_val = 0.0
            key = round(seconds_val, 6)
            tol_val = None
            if tolerance is not None:
                try:
                    tol_val = float(tolerance)
                except (TypeError, ValueError):
                    tol_val = None
            seen_key = (key, tol_val if tol_val is not None else 0.0)
            if seen_key in seen:
                return
            seen.add(seen_key)
            if tol_val is not None and tol_val > 0.0:
                targets.append({"seconds": seconds_val, "tolerance": tol_val})
            else:
                targets.append(seconds_val)

        for entry in entries or []:
            add_target(entry.get("original_seconds"))

        selected_frames = {
            entry.get("original_frame")
            for entry in entries
            if entry.get("original_frame") is not None
        }
        for point in property_entry.get("points") or []:
            frame_val = point.get("frame")
            try:
                frame_int = int(frame_val)
            except (TypeError, ValueError):
                frame_int = None
            if frame_int is not None and frame_int in selected_frames:
                continue
            seconds = point.get("seconds")
            if seconds is None:
                continue
            add_target(seconds)

        for other_prop in self.get_track_panel_properties(track_num) or []:
            if other_prop is property_entry:
                continue
            for point in other_prop.get("points") or []:
                seconds = point.get("seconds")
                if seconds is None:
                    continue
                add_target(seconds)

        if isinstance(context, dict):
            range_start = context.get("range_start_seconds")
            range_end = context.get("range_end_seconds")
            if range_start is not None:
                add_target(range_start)
            if range_end is not None:
                add_target(range_end)

        self._ensure_keyframe_markers()
        for marker in getattr(self, "_keyframe_markers", []):
            absolute = self._marker_absolute_seconds(marker)
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

    def _panel_snap_seconds(self, drag, seconds):
        if not self.enable_snapping:
            return seconds
        targets = drag.get("snap_targets") or []
        if not targets:
            return seconds
        pps = float(self.pixels_per_second or 0.0)
        if pps <= 0.0:
            return seconds
        tolerance_px = 0.0
        snap_helper = getattr(self, "snap", None)
        if snap_helper and hasattr(snap_helper, "_snap_tolerance_px"):
            try:
                tolerance_px = float(snap_helper._snap_tolerance_px())
            except (TypeError, ValueError):
                tolerance_px = 0.0
        if tolerance_px <= 0.0:
            return seconds
        tolerance_sec = tolerance_px / pps
        best = None
        min_diff = None
        for target in targets:
            tolerance_override = None
            if isinstance(target, dict):
                target_seconds = target.get("seconds")
                tolerance_override = target.get("tolerance")
            else:
                target_seconds = target
            try:
                value = float(target_seconds)
            except (TypeError, ValueError):
                continue
            local_tol = tolerance_sec
            if tolerance_override is not None:
                try:
                    override = float(tolerance_override)
                except (TypeError, ValueError):
                    override = None
                if override and override > 0.0:
                    local_tol = override
            diff = abs(value - seconds)
            if diff > local_tol + 1e-9:
                continue
            if min_diff is None or diff < min_diff:
                min_diff = diff
                best = value
        if best is None:
            return seconds
        return best

    def _panel_write_point_value(
        self,
        data,
        *,
        parent_path,
        frame,
        value,
        existing_path=None,
        interpolation=1,
    ):
        if existing_path:
            target = self._resolve_data_path(data, existing_path)
            if not isinstance(target, dict):
                return False
            co = target.get("co")
            if not isinstance(co, dict):
                return False
            co["Y"] = value
            if frame is not None:
                co["X"] = frame
            if interpolation is not None:
                target["interpolation"] = interpolation
            return True
        target_list = self._resolve_data_path(data, parent_path)
        if not isinstance(target_list, list):
            return False
        new_point = {"co": {"X": frame, "Y": value}}
        if interpolation is not None:
            new_point["interpolation"] = interpolation
        target_list.append(new_point)
        try:
            target_list.sort(key=lambda entry: entry.get("co", {}).get("X", frame))
        except Exception:
            pass
        return True

    def _panel_update_property_points(self, drag, *, resort=True):
        entries = drag.get("entries") or []
        if not entries:
            return
        context = drag.get("context") or {}
        try:
            position = float(context.get("position", 0.0) or 0.0)
        except (TypeError, ValueError):
            position = 0.0
        grouped = {}
        for entry in entries:
            prop = entry.get("property")
            prop_key = entry.get("prop_key")
            if not isinstance(prop, dict) or not prop_key:
                continue
            grouped.setdefault(prop_key, {"property": prop, "entries": []})["entries"].append(entry)
        if not grouped:
            return

        track = drag.get("track")
        track_map = {}
        if track is not None:
            track_map = dict(self._panel_selected_keyframes.get(track, {}) or {})

        for prop_key, bundle in grouped.items():
            prop = bundle.get("property")
            if not isinstance(prop, dict):
                continue
            for entry in bundle.get("entries", []):
                point = entry.get("point")
                if not isinstance(point, dict):
                    continue
                pending_frame = entry.get("pending_frame", entry.get("original_frame"))
                pending_seconds = entry.get("pending_seconds", entry.get("original_seconds"))
                if pending_frame is not None:
                    try:
                        point["frame"] = int(pending_frame)
                    except (TypeError, ValueError):
                        point["frame"] = pending_frame
                if pending_seconds is not None:
                    point["seconds"] = pending_seconds
                    try:
                        point["local_seconds"] = float(pending_seconds) - position
                    except (TypeError, ValueError):
                        pass
            if resort:
                try:
                    prop_points = prop.get("points") or []
                    prop_points.sort(key=lambda pt: pt.get("seconds", 0.0))
                except Exception:
                    pass
            if track is not None:
                new_frames = {
                    int(entry.get("pending_frame"))
                    for entry in bundle.get("entries", [])
                    if entry.get("pending_frame") is not None
                }
                if new_frames:
                    track_map[prop_key] = set(new_frames)
                elif prop_key in track_map:
                    track_map.pop(prop_key, None)

        if track is not None:
            self._panel_selected_keyframes[track] = track_map
            self._apply_panel_selection_flags(track)

    def _panel_begin_transaction(self, drag):
        if drag.get("transaction_started"):
            return
        timeline = getattr(self.win, "timeline", None)
        if not timeline:
            return
        tid = str(uuid.uuid4())
        drag["transaction_started"] = True
        drag["transaction_id"] = tid
        object_type = drag.get("owner_type", "clip") or "clip"
        object_id = drag.get("object_id", "") or ""
        timeline.StartKeyframeDrag(object_type, object_id, tid)

    def _apply_panel_keyframe_delta(self, drag, *, ignore_refresh=False, force=False):
        entries = drag.get("entries") or []
        if not entries:
            return
        timeline = getattr(self.win, "timeline", None)
        if not timeline:
            return
        owner_type = drag.get("owner_type", "clip") or "clip"
        clip_obj = drag.get("clip")
        transition_obj = drag.get("transition")
        transaction_id = drag.get("transaction_id")
        moved = False
        context = drag.get("context") or {}
        base_position = drag.get("base_position")
        if base_position is None and isinstance(context, dict):
            base_position = context.get("position")
        try:
            base_position = float(base_position)
        except (TypeError, ValueError):
            base_position = 0.0
        clip_start = 0.0
        if isinstance(context, dict):
            clip_start = context.get("clip_start")
        try:
            clip_start = float(clip_start)
        except (TypeError, ValueError):
            clip_start = 0.0
        if owner_type == "transition" and transition_obj:
            data_copy = json.loads(json.dumps(transition_obj.data))
            fps = drag.get("fps") or self.fps_float or 1.0
            for entry in entries:
                new_seconds = entry.get("pending_seconds")
                if fps and fps > 0.0 and new_seconds is not None:
                    try:
                        new_seconds = float(new_seconds)
                    except (TypeError, ValueError):
                        new_seconds = None
                if fps and fps > 0.0 and new_seconds is not None:
                    new_local = new_seconds - base_position
                    frame_seconds = new_local + clip_start
                    new_frame = int(round(frame_seconds * fps)) + 1
                else:
                    new_frame = entry.get("pending_frame")
                old_frame = entry.get("original_frame")
                path = entry.get("path")
                if new_frame is None or old_frame is None or not path:
                    continue
                if self._set_keyframe_frame_at_path(data_copy, path, new_frame):
                    moved = moved or (new_frame != old_frame or force)
                    if isinstance(transition_obj.data, (dict, list)) and (new_frame != old_frame or force):
                        self._set_keyframe_frame_at_path(transition_obj.data, path, new_frame)
            if moved or force:
                timeline.update_transition_data(
                    data_copy,
                    only_basic_props=False,
                    ignore_refresh=ignore_refresh,
                    transaction_id=transaction_id,
                )
        elif clip_obj:
            data_copy = json.loads(json.dumps(clip_obj.data))
            fps = drag.get("fps") or self.fps_float or 1.0
            for entry in entries:
                new_seconds = entry.get("pending_seconds")
                if fps and fps > 0.0 and new_seconds is not None:
                    try:
                        new_seconds = float(new_seconds)
                    except (TypeError, ValueError):
                        new_seconds = None
                if fps and fps > 0.0 and new_seconds is not None:
                    new_local = new_seconds - base_position
                    frame_seconds = new_local + clip_start
                    new_frame = int(round(frame_seconds * fps)) + 1
                else:
                    new_frame = entry.get("pending_frame")
                old_frame = entry.get("original_frame")
                path = entry.get("path")
                if new_frame is None or old_frame is None or not path:
                    continue
                if self._set_keyframe_frame_at_path(data_copy, path, new_frame):
                    moved = moved or (new_frame != old_frame or force)
                    if isinstance(clip_obj.data, (dict, list)) and (new_frame != old_frame or force):
                        self._set_keyframe_frame_at_path(clip_obj.data, path, new_frame)
            if moved or force:
                timeline.update_clip_data(
                    data_copy,
                    only_basic_props=False,
                    ignore_reader=True,
                    ignore_refresh=ignore_refresh,
                    transaction_id=transaction_id,
                )

    def _panel_resolve_owner(self, prop, context):
        source_meta = prop.get("source_meta") if isinstance(prop, dict) else {}
        if not isinstance(source_meta, dict):
            source_meta = {}
        owner_hint = source_meta.get("owner")
        clip_obj = source_meta.get("clip")
        transition_obj = source_meta.get("transition")
        effect_obj = source_meta.get("effect")
        if not clip_obj and isinstance(context, dict):
            clip_id_ctx = context.get("clip_id")
            if clip_id_ctx:
                try:
                    clip_obj = Clip.get(id=clip_id_ctx)
                except Exception:
                    pass
        if not transition_obj and isinstance(context, dict) and context.get("item_type") == "transition":
            tran_id_ctx = context.get("transition_id") or context.get("item_id")
            if tran_id_ctx:
                try:
                    transition_obj = Transition.get(id=tran_id_ctx)
                except Exception:
                    pass
        owner_type = "transition" if transition_obj else "clip"
        if owner_hint == "transition" and transition_obj is None and clip_obj is None:
            owner_type = "transition"
        object_id = ""
        if owner_type == "transition" and transition_obj:
            object_id = str(getattr(transition_obj, "id", context.get("item_id") or ""))
        elif clip_obj:
            object_id = str(getattr(clip_obj, "id", context.get("clip_id") or context.get("item_id") or ""))
        elif isinstance(context, dict):
            object_id = str(context.get("item_id") or context.get("clip_id") or "")
        return {
            "source_meta": source_meta,
            "clip": clip_obj,
            "transition": transition_obj,
            "effect": effect_obj,
            "owner_type": owner_type,
            "object_id": object_id,
        }

    def _start_panel_keyframe_drag(self, info):
        if not isinstance(info, dict):
            self._panel_press_info = None
            self._dragging_panel_keyframes = None
            return
        point = info.get("point")
        prop = info.get("property")
        track_num = info.get("track")
        if not isinstance(prop, dict) or track_num is None or not isinstance(point, dict):
            self._panel_press_info = None
            self._dragging_panel_keyframes = None
            return
        frame_val = point.get("frame")
        try:
            frame_int = int(frame_val)
        except (TypeError, ValueError):
            frame_int = None
        if frame_int is None:
            self._panel_press_info = None
            self._dragging_panel_keyframes = None
            return
        track_key = self.normalize_track_number(track_num)
        prop_key = prop.get("key")
        if not prop_key:
            self._panel_press_info = None
            self._dragging_panel_keyframes = None
            return
        lane_rect = info.get("render_rect", info.get("lane_rect", QRectF()))
        if not isinstance(lane_rect, QRectF):
            lane_rect = QRectF(lane_rect)
        if lane_rect.isNull():
            lane_rect = QRectF(info.get("lane_rect", QRectF()))
        lane_padding = info.get("lane_padding", self._panel_lane_padding())
        context = info.get("context") or self.get_track_panel_context(track_key)

        selection_map = self._panel_selected_keyframes.get(track_key, {}) or {}
        selected_frames = set(selection_map.get(prop_key, set()) or set())
        normalized_frames = set()
        for val in selected_frames:
            if val is None:
                continue
            try:
                normalized_frames.add(int(val))
            except (TypeError, ValueError):
                continue
        if normalized_frames:
            selected_frames = normalized_frames
        modifiers = info.get("modifiers", Qt.NoModifier)
        ctrl_down = bool(modifiers & Qt.ControlModifier)
        if frame_int not in selected_frames:
            if ctrl_down:
                self._panel_merge_selection_map(track_key, {prop_key: {frame_int}})
                selected_frames.add(frame_int)
            else:
                selected_frames = {frame_int}
                self._panel_set_selection_map(track_key, {prop_key: {frame_int}})

        lane_lookup = {}
        for lane in self._iter_panel_lanes() or []:
            if lane.get("track") != track_key:
                continue
            lane_prop = lane.get("property")
            key = lane_prop.get("key") if isinstance(lane_prop, dict) else None
            if key:
                lane_lookup[key] = lane

        track_map = self._panel_selected_keyframes.get(track_key, {}) or {}
        move_sets = {}
        for key, frames in (track_map.items() if track_map else []):
            frames_set = set()
            for val in frames or []:
                if val is None:
                    continue
                try:
                    frames_set.add(int(val))
                except (TypeError, ValueError):
                    continue
            if frames_set:
                move_sets[key] = frames_set
        if not move_sets:
            move_sets[prop_key] = selected_frames or {frame_int}

        properties = {}
        entries = []
        anchor_entry = None
        for key, frames in move_sets.items():
            lane = lane_lookup.get(key)
            prop_obj = None
            if lane:
                prop_obj = lane.get("property")
            if prop_obj is None and key == prop_key:
                prop_obj = prop
            if not isinstance(prop_obj, dict):
                continue
            properties[key] = prop_obj
            for candidate in prop_obj.get("points") or []:
                frame_val = candidate.get("frame")
                try:
                    candidate_frame = int(frame_val) if frame_val is not None else None
                except (TypeError, ValueError):
                    candidate_frame = None
                if candidate_frame is None or candidate_frame not in frames:
                    continue
                seconds_val = candidate.get("seconds")
                try:
                    seconds_float = float(seconds_val) if seconds_val is not None else None
                except (TypeError, ValueError):
                    seconds_float = None
                entry = {
                    "point": candidate,
                    "original_frame": candidate_frame,
                    "pending_frame": candidate_frame,
                    "original_seconds": seconds_float,
                    "pending_seconds": seconds_float,
                    "path": tuple(candidate.get("path")) if candidate.get("path") else None,
                    "property": prop_obj,
                    "prop_key": key,
                }
                entries.append(entry)
                if key == prop_key and candidate_frame == frame_int and anchor_entry is None:
                    anchor_entry = entry
        if not entries:
            self._panel_press_info = None
            self._dragging_panel_keyframes = None
            return

        if anchor_entry is None:
            anchor_entry = entries[0]

        owner_info = self._panel_resolve_owner(prop, context)
        source_meta = owner_info.get("source_meta") or {}
        clip_obj = owner_info.get("clip")
        transition_obj = owner_info.get("transition")
        effect_obj = owner_info.get("effect")
        owner_type = owner_info.get("owner_type", "clip")
        object_id = owner_info.get("object_id", "")

        range_start = context.get("range_start_seconds") if isinstance(context, dict) else None
        range_end = context.get("range_end_seconds") if isinstance(context, dict) else None
        base_position = context.get("position") if isinstance(context, dict) else 0.0
        try:
            base_position = float(base_position or 0.0)
        except (TypeError, ValueError):
            base_position = 0.0

        drag_info = {
            "track": track_key,
            "prop_key": prop_key,
            "property": prop,
            "entries": entries,
            "properties": properties,
            "context": context,
            "lane_rect": lane_rect,
            "lane_padding": lane_padding,
            "fps": self.fps_float or 1.0,
            "source_meta": source_meta,
            "clip": clip_obj,
            "transition": transition_obj,
            "effect": effect_obj,
            "owner_type": owner_type,
            "object_id": object_id,
            "transaction_started": False,
            "transaction_id": None,
            "moved": False,
            "range_start": range_start,
            "range_end": range_end,
            "base_position": base_position,
            "snap_targets": tuple(self._panel_compute_snap_targets(track_key, prop, entries, context)),
            "anchor": anchor_entry,
        }

        self._dragging_panel_keyframes = drag_info
        info_copy = dict(info)
        info_copy["dragged"] = False
        info_copy["lane_rect"] = lane_rect
        info_copy["lane_padding"] = lane_padding
        info_copy["context"] = context
        info_copy["modifiers"] = modifiers
        self._panel_press_info = info_copy
        self.mouse_dragging = True
        self._fix_cursor(self.cursors.get("resize_x", Qt.SizeHorCursor))

    def _panel_keyframe_move(self, event):
        drag = self._dragging_panel_keyframes
        if not drag:
            return
        lane_rect = drag.get("lane_rect", QRectF())
        if lane_rect.isNull():
            lane_rect = drag.get("render_rect", QRectF())
        if lane_rect.isNull():
            return
        x_pos = event.pos().x()
        x_pos = max(lane_rect.left(), min(lane_rect.right(), x_pos))
        seconds = self._panel_x_to_seconds(x_pos)
        range_start = drag.get("range_start")
        range_end = drag.get("range_end")
        if range_start is not None and seconds < range_start:
            seconds = range_start
        if range_end is not None and seconds > range_end:
            seconds = range_end
        seconds = self._panel_snap_seconds(drag, seconds)

        entries = drag.get("entries") or []
        if not entries:
            return
        anchor = drag.get("anchor") or entries[0]
        anchor_seconds = anchor.get("original_seconds")
        if anchor_seconds is None:
            anchor_seconds = seconds
        delta = seconds - anchor_seconds

        valid_seconds = [
            entry.get("original_seconds")
            for entry in entries
            if entry.get("original_seconds") is not None
        ]
        if range_start is not None and valid_seconds:
            min_initial = min(valid_seconds)
            min_delta = range_start - min_initial
            if delta < min_delta:
                delta = min_delta
        if range_end is not None and valid_seconds:
            max_initial = max(valid_seconds)
            max_delta = range_end - max_initial
            if delta > max_delta:
                delta = max_delta

        fps = drag.get("fps") or self.fps_float or 1.0
        context = drag.get("context") or {}
        base_position = drag.get("base_position")
        if base_position is None and isinstance(context, dict):
            base_position = context.get("position")
        try:
            base_position = float(base_position)
        except (TypeError, ValueError):
            base_position = 0.0
        clip_start = 0.0
        if isinstance(context, dict):
            clip_start = context.get("clip_start")
        try:
            clip_start = float(clip_start)
        except (TypeError, ValueError):
            clip_start = 0.0
        changed = False
        for entry in entries:
            orig_seconds = entry.get("original_seconds")
            if orig_seconds is None:
                continue
            new_abs = orig_seconds + delta
            prev_seconds = entry.get("pending_seconds")
            prev_frame = entry.get("pending_frame")
            if fps > 0.0:
                new_local = new_abs - base_position
                frame_seconds = new_local + clip_start
                new_frame = int(round(frame_seconds * fps)) + 1
            else:
                new_frame = entry.get("original_frame")
            if new_frame != prev_frame or prev_seconds is None or not math.isclose(new_abs, prev_seconds, rel_tol=1e-6, abs_tol=1e-9):
                changed = True
            entry["pending_seconds"] = new_abs
            entry["pending_frame"] = new_frame
        if not changed and drag.get("moved"):
            return
        if not changed and not drag.get("moved"):
            return

        drag["moved"] = True
        info = dict(self._panel_press_info or {})
        info["dragged"] = True
        self._panel_press_info = info

        anchor_pending = anchor.get("pending_seconds")
        if anchor_pending is None:
            anchor_pending = anchor.get("original_seconds")

        self._panel_update_property_points(drag)
        self._panel_begin_transaction(drag)
        self._apply_panel_keyframe_delta(drag, ignore_refresh=True)

        fps_seek = drag.get("fps") or self.fps_float or 1.0
        if anchor_pending is not None and fps_seek and fps_seek > 0.0 and hasattr(self, "win"):
            frame_seek = int(round(anchor_pending * fps_seek)) + 1
            frame_seek = max(1, frame_seek)
            if hasattr(self.win, "SeekSignal"):
                self.win.SeekSignal.emit(frame_seek)
        self.update()

    def _finish_panel_keyframe_drag(self):
        drag = self._dragging_panel_keyframes
        if not drag:
            return
        timeline = getattr(self.win, "timeline", None)
        started = drag.get("transaction_started")
        moved = drag.get("moved")
        if started:
            self._apply_panel_keyframe_delta(drag, ignore_refresh=False, force=True)
            if timeline:
                timeline.FinalizeKeyframeDrag(
                    drag.get("owner_type", "clip") or "clip",
                    drag.get("object_id", "") or "",
                )
            if moved and hasattr(self.win, "show_property_timeout"):
                QTimer.singleShot(0, self.win.show_property_timeout)
        self._dragging_panel_keyframes = None
        self.mouse_dragging = False
        info = dict(self._panel_press_info or {})
        if moved:
            info["dragged"] = True
        self._panel_press_info = info
        self._release_cursor()
        self._update_track_panel_properties()
        self.geometry.mark_dirty()
        self._keyframes_dirty = True
        self.update()

    def _handle_panel_add_click(self, info):
        if not isinstance(info, dict):
            return False
        prop = info.get("property")
        track_num = info.get("track")
        if not isinstance(prop, dict) or prop.get("placeholder"):
            return False
        context = info.get("context") or self.get_track_panel_context(track_num)
        prop_key = prop.get("key")
        if not prop_key:
            return False
        timeline = getattr(self.win, "timeline", None)
        if not timeline:
            log.info("Keyframe panel add skipped: no timeline backend")
            return False
        owner = self._panel_resolve_owner(prop, context)
        clip_obj = owner.get("clip")
        transition_obj = owner.get("transition")
        parent_path = self._panel_property_points_parent_path(prop)
        if parent_path is None:
            log.info("Keyframe panel add skipped: property %s missing points path", prop_key)
            return False
        try:
            parent_path = tuple(parent_path)
        except TypeError:
            parent_path = parent_path
        data_obj = None
        data_label = None
        for label, candidate in (("clip", clip_obj), ("transition", transition_obj)):
            data = getattr(candidate, "data", None)
            if isinstance(data, (dict, list)):
                target = self._resolve_data_path(data, parent_path)
                if isinstance(target, list):
                    data_obj = candidate
                    data_label = label
                    break
        if not data_obj or not isinstance(getattr(data_obj, "data", None), (dict, list)):
            log.info(
                "Keyframe panel add skipped: property %s has no writable source",
                prop_key,
            )
            return False
        fps_val = context.get("fps") if isinstance(context, dict) else None
        try:
            fps_prop = float(fps_val)
        except (TypeError, ValueError):
            fps_prop = self.fps_float or 1.0
        if not math.isfinite(fps_prop) or fps_prop <= 0.0:
            fps_prop = self.fps_float or 1.0
        if not math.isfinite(fps_prop) or fps_prop <= 0.0:
            fps_prop = 1.0
        timeline_fps = self.fps_float or fps_prop
        if not math.isfinite(timeline_fps) or timeline_fps <= 0.0:
            timeline_fps = fps_prop if math.isfinite(fps_prop) and fps_prop > 0.0 else 1.0
        current_frame = getattr(self, "current_frame", 1)
        try:
            current_frame = int(current_frame)
        except (TypeError, ValueError):
            current_frame = 1
        if current_frame < 1:
            current_frame = 1
        playhead_seconds = (current_frame - 1) / timeline_fps
        position = self._panel_float(context.get("position"), 0.0)
        clip_start = self._panel_float(context.get("clip_start"), 0.0)
        clip_end = self._panel_float(context.get("clip_end"), clip_start)
        if clip_end < clip_start:
            clip_end = clip_start
        local_seconds = playhead_seconds - position
        frame_seconds = local_seconds + clip_start
        if frame_seconds < clip_start:
            frame_seconds = clip_start
        if frame_seconds > clip_end:
            frame_seconds = clip_end
        new_frame = int(round(frame_seconds * fps_prop)) + 1
        if new_frame < 1:
            new_frame = 1
        raw_value = prop.get("value")
        if raw_value is None:
            log.info("Keyframe panel add skipped: property %s missing value", prop_key)
            return False
        prop_type = (prop.get("value_type") or prop.get("type") or "").lower()
        try:
            if prop_key == "time":
                value_num = int(round(float(raw_value)))
            elif prop_type == "int":
                value_num = int(round(float(raw_value)))
            else:
                value_num = float(raw_value)
        except (TypeError, ValueError):
            log.info(
                "Keyframe panel add skipped: invalid value %s for property %s",
                raw_value,
                prop_key,
            )
            return False
        existing_path = None
        interpolation = None
        for point in prop.get("points") or []:
            frame_val = point.get("frame")
            try:
                frame_int = int(frame_val) if frame_val is not None else None
            except (TypeError, ValueError):
                frame_int = None
            if frame_int is None:
                continue
            if interpolation is None:
                interpolation = point.get("interpolation")
            if frame_int == new_frame:
                existing_path = point.get("path")
                interp_val = point.get("interpolation")
                if interp_val is not None:
                    interpolation = interp_val
                break
        if interpolation is None:
            interpolation = 1
        try:
            interpolation_val = int(interpolation)
        except (TypeError, ValueError):
            interpolation_val = interpolation
        if existing_path:
            try:
                existing_path = tuple(existing_path)
            except TypeError:
                existing_path = existing_path
        data_copy = json.loads(json.dumps(data_obj.data))
        if not self._panel_write_point_value(
            data_copy,
            parent_path=parent_path,
            frame=new_frame,
            value=value_num,
            existing_path=existing_path,
            interpolation=interpolation_val,
        ):
            log.info("Keyframe panel add failed: unable to write property %s", prop_key)
            return False
        original_data = getattr(data_obj, "data", None)
        if isinstance(original_data, (dict, list)):
            self._panel_write_point_value(
                original_data,
                parent_path=parent_path,
                frame=new_frame,
                value=value_num,
                existing_path=existing_path,
                interpolation=interpolation_val,
            )
        try:
            if data_label == "transition":
                timeline.update_transition_data(
                    data_copy,
                    only_basic_props=False,
                    ignore_refresh=False,
                )
            else:
                timeline.update_clip_data(
                    data_copy,
                    only_basic_props=False,
                    ignore_reader=True,
                    ignore_refresh=False,
                )
        except Exception:
            log.info(
                "Keyframe panel add failed: timeline update error for property %s",
                prop_key,
            )
            return False
        if track_num is not None:
            self._panel_merge_selection_map(track_num, {prop_key: {new_frame}})
        self._update_track_panel_properties()
        self.geometry.mark_dirty()
        self.update()
        log.info(
            "Keyframe panel add: property %s frame=%s source=%s",
            prop_key,
            new_frame,
            data_label,
        )
        return True

    def _panel_preview_marker(self, marker, old_frame, new_frame, absolute_seconds):
        if not isinstance(marker, dict):
            return
        marker_type = marker.get("type")
        track_num = None
        context_type = None
        context_id = ""
        effect_id = marker.get("owner_id") if marker_type == "effect" else None
        clip = marker.get("clip")
        transition = marker.get("transition")
        if marker_type == "transition" and transition and isinstance(transition.data, dict):
            track_val = transition.data.get("layer")
            track_num = self.normalize_track_number(track_val)
            context_type = "transition"
            context_id = str(getattr(transition, "id", marker.get("object_id") or ""))
        elif clip and isinstance(clip.data, dict):
            track_val = clip.data.get("layer")
            track_num = self.normalize_track_number(track_val)
            context_type = "clip" if marker_type != "effect" else "effect"
            context_id = str(getattr(clip, "id", marker.get("object_id") or ""))
        if track_num is None:
            return
        info = self._panel_properties.get(track_num)
        if not info:
            return
        context = info.get("context", {})
        item_type = context.get("item_type")
        target_id = str(context.get("item_id") or context.get("clip_id") or context.get("transition_id") or "")
        if context_type == "transition":
            if item_type != "transition" or (target_id and target_id != context_id):
                return
        elif context_type == "effect":
            effect_ctx = str(context.get("item_id") or context.get("effect_id") or "")
            if item_type != "effect" or (effect_id and effect_ctx and effect_ctx != effect_id):
                return
        else:
            if item_type not in ("clip", "effect"):
                return
            if target_id and target_id != context_id and context.get("clip_id") not in (None, context_id):
                return

        properties = info.get("properties", [])
        changed = False
        new_frame_int = None
        try:
            new_frame_int = int(new_frame) if new_frame is not None else None
        except (TypeError, ValueError):
            new_frame_int = new_frame
        for prop in properties:
            prop_key = prop.get("key")
            for point in prop.get("points") or []:
                frame_val = point.get("frame")
                try:
                    frame_int = int(frame_val) if frame_val is not None else None
                except (TypeError, ValueError):
                    frame_int = None
                if frame_int is None or frame_int != old_frame:
                    continue
                point["frame"] = new_frame_int
                if absolute_seconds is not None:
                    point["seconds"] = absolute_seconds
                    try:
                        position_val = float(context.get("position", 0.0) or 0.0)
                        point["local_seconds"] = absolute_seconds - position_val
                    except (TypeError, ValueError):
                        pass
                changed = True
                if track_num in self._panel_selected_keyframes and prop_key in self._panel_selected_keyframes[track_num]:
                    selection = self._panel_selected_keyframes[track_num][prop_key]
                    if old_frame in selection:
                        selection.discard(old_frame)
                        if new_frame_int is not None:
                            selection.add(int(new_frame_int))
        if changed:
            self._apply_panel_selection_flags(track_num)
            self.update()

    def _panel_shift_item(self, item, delta_seconds, frame_offset):
        if not isinstance(delta_seconds, (int, float)):
            delta_seconds = 0.0
        try:
            frame_offset = int(frame_offset)
        except (TypeError, ValueError):
            frame_offset = 0
        try:
            layer = item.data.get("layer")
        except Exception:
            layer = None
        track_num = self.normalize_track_number(layer) if layer is not None else None
        if track_num is None:
            return
        info = self._panel_properties.get(track_num)
        if not info:
            return
        context = info.get("context")
        if not isinstance(context, dict) or context.get("placeholder"):
            return
        item_type = context.get("item_type")
        target_id = str(context.get("item_id") or context.get("clip_id") or context.get("transition_id") or "")
        if isinstance(item, Clip):
            item_id = str(getattr(item, "id", ""))
            clip_match = str(context.get("clip_id") or "")
            if item_type == "clip":
                if target_id and target_id != item_id:
                    return
            elif item_type == "effect":
                if clip_match and clip_match != item_id:
                    return
            else:
                return
        elif isinstance(item, Transition):
            item_id = str(getattr(item, "id", ""))
            if item_type != "transition" or (target_id and target_id != item_id):
                return
        else:
            return

        base_props = info.get("base_properties")
        if base_props is None:
            base_props = self._panel_capture_base_properties(info.get("properties"))
            info["base_properties"] = base_props
        base_context = info.get("base_context")
        if base_context is None:
            base_context = self._panel_capture_base_context(context)
            info["base_context"] = base_context

        if delta_seconds:
            for key_name in ("position", "range_start_seconds", "range_end_seconds"):
                base_value = base_context.get(key_name, context.get(key_name))
                if base_value is None:
                    continue
                try:
                    context[key_name] = float(base_value) + delta_seconds
                except (TypeError, ValueError):
                    context[key_name] = base_value

        properties = info.get("properties", [])
        for prop in properties:
            key_name = self._panel_property_key(prop)
            base_points = base_props.get(key_name, []) if key_name else []
            points = prop.get("points") or []
            for index, point in enumerate(points):
                base_point = base_points[index] if index < len(base_points) else {}
                if frame_offset:
                    base_frame = base_point.get("frame")
                    if base_frame is not None:
                        try:
                            point["frame"] = int(base_frame) + frame_offset
                        except (TypeError, ValueError):
                            point["frame"] = base_frame
                if delta_seconds:
                    base_seconds = base_point.get("seconds")
                    if base_seconds is not None:
                        try:
                            new_seconds = float(base_seconds) + delta_seconds
                        except (TypeError, ValueError):
                            new_seconds = base_seconds
                        point["seconds"] = new_seconds
                        try:
                            position_val = float(context.get("position", 0.0) or 0.0)
                            point["local_seconds"] = float(new_seconds) - position_val
                        except (TypeError, ValueError):
                            pass

        if track_num in self._panel_selected_keyframes:
            updated = {}
            for prop in properties:
                prop_key = prop.get("key")
                if not prop_key:
                    continue
                selected_frames = set()
                for point in prop.get("points") or []:
                    if not point.get("selected"):
                        continue
                    frame_val = point.get("frame")
                    if frame_val is None:
                        continue
                    try:
                        selected_frames.add(int(frame_val))
                    except (TypeError, ValueError):
                        continue
                if selected_frames:
                    updated[prop_key] = selected_frames
            if updated:
                self._panel_selected_keyframes[track_num] = updated
            else:
                self._panel_selected_keyframes.pop(track_num, None)
            self._apply_panel_selection_flags(track_num)
        self.update()

    def _clear_panel_selection(self, track_num=None):
        targets = []
        if track_num is None:
            targets = list(self._panel_selected_keyframes.keys())
        else:
            key = self.normalize_track_number(track_num)
            if key in self._panel_selected_keyframes:
                targets = [key]
        if not targets:
            return
        changed = False
        for key in targets:
            if key in self._panel_selected_keyframes:
                self._panel_selected_keyframes.pop(key, None)
                changed = True
            info = self._panel_properties.get(key)
            if not info:
                continue
            for prop in info.get("properties", []):
                for point in prop.get("points") or []:
                    if point.get("selected"):
                        point["selected"] = False
                        changed = True
        if changed:
            self.update()

    def _apply_panel_selection_flags(self, track_num):
        key = self.normalize_track_number(track_num)
        info = self._panel_properties.get(key)
        if not info:
            return
        selection = self._panel_selected_keyframes.get(key, {}) or {}
        for prop in info.get("properties", []):
            frames = selection.get(prop.get("key"), set()) or set()
            for point in prop.get("points") or []:
                frame = point.get("frame")
                point["selected"] = frame in frames if frame is not None else False

    def _sync_panel_selection(self, track_num, properties):
        key = self.normalize_track_number(track_num)
        if key not in self._panel_selected_keyframes:
            return
        current = self._panel_selected_keyframes.get(key) or {}
        if not current:
            self._panel_selected_keyframes.pop(key, None)
            return
        valid = {}
        for prop in properties or []:
            prop_key = prop.get("key")
            frames = {
                int(point.get("frame"))
                for point in prop.get("points") or []
                if point.get("frame") is not None
            }
            if not frames or prop_key not in current:
                continue
            selected = {frame for frame in current.get(prop_key, set()) if frame in frames}
            if selected:
                valid[prop_key] = selected
        if valid:
            self._panel_selected_keyframes[key] = valid
        else:
            self._panel_selected_keyframes.pop(key, None)

    def _panel_set_selection_map(self, track_num, mapping):
        key = self.normalize_track_number(track_num)
        if key is None:
            return
        cleaned = {}
        for prop_key, frames in (mapping or {}).items():
            if not prop_key or not frames:
                continue
            cleaned[prop_key] = {int(frame) for frame in frames if frame is not None}
        if cleaned:
            self._panel_selected_keyframes[key] = cleaned
        else:
            self._panel_selected_keyframes.pop(key, None)
        self._apply_panel_selection_flags(key)
        self.update()

    def _panel_merge_selection_map(self, track_num, mapping):
        key = self.normalize_track_number(track_num)
        if key is None:
            return
        if key not in self._panel_selected_keyframes:
            self._panel_selected_keyframes[key] = {}
        track_map = self._panel_selected_keyframes[key]
        changed = False
        for prop_key, frames in (mapping or {}).items():
            if not prop_key or not frames:
                continue
            if prop_key not in track_map:
                track_map[prop_key] = set()
            dest = set(track_map[prop_key])
            before = set(dest)
            for frame in frames:
                if frame is None:
                    continue
                dest.add(int(frame))
            if dest != before:
                track_map[prop_key] = dest
                changed = True
        if not track_map:
            self._panel_selected_keyframes.pop(key, None)
        if changed:
            self._apply_panel_selection_flags(key)
            self.update()

    def _panel_toggle_frames(self, track_num, prop_key, frames):
        key = self.normalize_track_number(track_num)
        if key is None or not prop_key:
            return
        if key not in self._panel_selected_keyframes:
            self._panel_selected_keyframes[key] = {}
        track_map = self._panel_selected_keyframes[key]
        current = set(track_map.get(prop_key, set()))
        changed = False
        for frame in frames or []:
            if frame is None:
                continue
            frame_int = int(frame)
            if frame_int in current:
                current.remove(frame_int)
            else:
                current.add(frame_int)
            changed = True
        if current:
            track_map[prop_key] = current
        else:
            track_map.pop(prop_key, None)
        if not track_map:
            self._panel_selected_keyframes.pop(key, None)
        if changed:
            self._apply_panel_selection_flags(key)
            self.update()

    def _refresh_panel_selection_state(self, new_props):
        active_tracks = set(new_props.keys())
        for track_num in list(self._panel_selected_keyframes.keys()):
            if track_num not in active_tracks:
                self._panel_selected_keyframes.pop(track_num, None)
                continue
            info = new_props.get(track_num, {})
            properties = info.get("properties", [])
            self._sync_panel_selection(track_num, properties)
            self._apply_panel_selection_flags(track_num)

    def _panel_item_context(self, item_id, item_type):
        context = {
            "item_id": str(item_id),
            "item_type": item_type,
            "fps": self.fps_float or 1.0,
        }

        if item_type == "clip":
            clip = Clip.get(id=item_id)
            data = clip.data if clip and isinstance(clip.data, dict) else {}
            position = self._panel_float(data.get("position"), 0.0)
            clip_start = self._panel_float(data.get("start"), 0.0)
            clip_end = self._panel_float(data.get("end"), clip_start)
            if clip_end < clip_start:
                clip_end = clip_start
            duration = max(0.0, clip_end - clip_start)
            context.update(
                {
                    "position": position,
                    "clip_start": clip_start,
                    "clip_end": clip_end,
                    "range_start_seconds": position,
                    "range_end_seconds": position + duration,
                    "clip_id": str(getattr(clip, "id", "") or data.get("id") or item_id),
                    "track": data.get("layer"),
                    "duration": duration,
                }
            )
            return context

        if item_type == "effect":
            effect = Effect.get(id=item_id)
            data = effect.data if effect and isinstance(effect.data, dict) else {}
            parent = effect.parent if effect and isinstance(effect.parent, dict) else {}
            position = self._panel_float(parent.get("position"), None)
            if position is None:
                position = self._panel_float(data.get("position"), 0.0)
            clip_start = self._panel_float(parent.get("start"), 0.0)
            clip_end = self._panel_float(parent.get("end"), clip_start)
            if clip_end < clip_start:
                clip_end = clip_start
            duration = max(0.0, clip_end - clip_start)
            clip_id = parent.get("id") or data.get("parent_id") or parent.get("clip_id")
            context.update(
                {
                    "position": position,
                    "clip_start": clip_start,
                    "clip_end": clip_end,
                    "range_start_seconds": position,
                    "range_end_seconds": position + duration,
                    "clip_id": str(clip_id) if clip_id is not None else "",
                    "parent": parent,
                    "effect_id": str(getattr(effect, "id", "") or data.get("id") or item_id),
                    "track": parent.get("layer")
                    if isinstance(parent, dict) and parent.get("layer") is not None
                    else data.get("layer"),
                    "duration": duration,
                }
            )
            return context

        if item_type == "transition":
            transition = Transition.get(id=item_id)
            data = transition.data if transition and isinstance(transition.data, dict) else {}
            position = self._panel_float(data.get("position"), 0.0)
            clip_start = self._panel_float(data.get("start"), 0.0)
            clip_end = self._panel_float(data.get("end"), clip_start)
            if clip_end < clip_start:
                clip_end = clip_start
            duration = max(0.0, clip_end - clip_start)
            context.update(
                {
                    "position": position,
                    "clip_start": clip_start,
                    "clip_end": clip_end,
                    "range_start_seconds": position,
                    "range_end_seconds": position + duration,
                    "track": data.get("layer"),
                    "transition_id": str(getattr(transition, "id", "") or data.get("id") or item_id),
                    "duration": duration,
                }
            )
            return context

        position = self._panel_float(context.get("position"), 0.0)
        context.update(
            {
                "position": position,
                "clip_start": 0.0,
                "clip_end": 0.0,
                "range_start_seconds": position,
                "range_end_seconds": position,
                "duration": 0.0,
            }
        )
        return context

    def _track_number_for_selection(self, item_id, item_type):
        try:
            if item_type == "clip":
                clip = Clip.get(id=item_id)
                if clip and isinstance(clip.data, dict):
                    return clip.data.get("layer")
            elif item_type == "transition":
                tran = Transition.get(id=item_id)
                if tran and isinstance(tran.data, dict):
                    return tran.data.get("layer")
            elif item_type == "effect":
                effect = Effect.get(id=item_id)
                if effect:
                    parent = getattr(effect, "parent", None)
                    if isinstance(parent, dict):
                        return parent.get("layer")
                    if isinstance(effect.data, dict):
                        return effect.data.get("layer")
        except Exception:
            return None
        return None

    def _properties_for_item(self, timeline, item_id, item_type, frame, context=None):
        obj = None
        item_id_str = str(item_id)
        try:
            if item_type == "clip":
                obj = timeline.GetClip(item_id_str)
            elif item_type == "transition":
                obj = timeline.GetEffect(item_id_str)
            elif item_type == "effect":
                obj = timeline.GetClipEffect(item_id_str)
        except Exception:
            obj = None
        if not obj:
            return [], {}

        try:
            props = json.loads(obj.PropertiesJSON(int(frame)))
        except Exception:
            return [], {}

        tracked = props.pop("objects", None)
        if isinstance(tracked, dict):
            for track_props in tracked.values():
                if isinstance(track_props, dict):
                    props.update(track_props)
                    break

        context = context or self._panel_item_context(item_id, item_type)
        if not context:
            context = {"item_id": item_id_str, "item_type": item_type}
        fps = context.get("fps") or self.fps_float or 1.0
        if fps <= 0.0:
            fps = 1.0

        clip_start = context.get("clip_start", 0.0)
        position = context.get("position", 0.0)

        _ = get_app()._tr

        track_selection = {}
        if isinstance(context, dict) and context.get("track") is not None:
            track_key = self.normalize_track_number(context.get("track"))
            track_selection = self._panel_selected_keyframes.get(track_key, {}) or {}

        raw_sources = []

        def _add_source(data, owner, **meta):
            if not isinstance(data, (dict, list)):
                return
            entry = {"data": data, "owner": owner}
            for key_name, value in meta.items():
                if value is not None:
                    entry[key_name] = value
            raw_sources.append(entry)

        try:
            if item_type == "clip":
                clip_obj = Clip.get(id=item_id)
                if clip_obj and isinstance(getattr(clip_obj, "data", None), dict):
                    _add_source(
                        clip_obj.data,
                        "clip",
                        clip=clip_obj,
                        clip_id=str(getattr(clip_obj, "id", item_id)),
                    )
            elif item_type == "transition":
                tran_obj = Transition.get(id=item_id)
                if tran_obj and isinstance(getattr(tran_obj, "data", None), dict):
                    _add_source(
                        tran_obj.data,
                        "transition",
                        transition=tran_obj,
                        transition_id=str(getattr(tran_obj, "id", item_id)),
                    )
            elif item_type == "effect":
                eff_obj = Effect.get(id=item_id)
                clip_id = context.get("clip_id") if isinstance(context, dict) else None
                clip_obj = Clip.get(id=clip_id) if clip_id else None
                if clip_obj and isinstance(getattr(clip_obj, "data", None), dict):
                    _add_source(
                        clip_obj.data,
                        "clip",
                        clip=clip_obj,
                        effect=eff_obj,
                        clip_id=str(getattr(clip_obj, "id", clip_id)),
                    )
                parent_ctx = context.get("parent") if isinstance(context, dict) else None
                if isinstance(parent_ctx, (dict, list)):
                    _add_source(
                        parent_ctx,
                        "parent",
                        clip=clip_obj,
                        effect=eff_obj,
                        clip_id=str(clip_id) if clip_id is not None else None,
                    )
                if eff_obj and isinstance(getattr(eff_obj, "data", None), dict):
                    _add_source(
                        eff_obj.data,
                        "effect",
                        effect=eff_obj,
                        clip=clip_obj,
                        clip_id=str(clip_id) if clip_id is not None else None,
                        effect_id=str(getattr(eff_obj, "id", item_id)),
                    )
        except Exception:
            log.info("Keyframe panel refresh: failed to fetch raw data for %s %s", item_type, item_id)

        def _iter_sources():
            visited = set()

            def _visit(source, path, meta):
                if not isinstance(source, (dict, list)):
                    return
                key = (id(source), meta.get("owner"))
                if key in visited:
                    return
                visited.add(key)
                if isinstance(source, dict):
                    yield source, path, meta
                    for key_name, value in source.items():
                        if isinstance(value, dict):
                            yield from _visit(value, path + (("dict", key_name),), meta)
                        elif isinstance(value, list):
                            yield from _visit(value, path + (("dict", key_name),), meta)
                else:
                    for index, item in enumerate(source):
                        yield from _visit(item, path + (("list", index),), meta)

            for entry in raw_sources:
                data = entry.get("data")
                if not isinstance(data, (dict, list)):
                    continue
                meta = dict(entry)
                meta.pop("data", None)
                yield from _visit(data, (), meta)

        def _property_points(prop_key, prop_dict):
            for source, path, meta in _iter_sources():
                if not isinstance(source, dict):
                    continue
                candidate = source.get(prop_key)
                if not isinstance(candidate, dict):
                    continue
                base_path = path + (("dict", prop_key),)
                points = candidate.get("Points")
                if isinstance(points, list) and points:
                    point_paths = [
                        base_path + (("dict", "Points"), ("list", index))
                        for index, _point in enumerate(points)
                    ]
                    return {"points": points, "paths": point_paths, "meta": meta}
                if prop_dict.get("type") == "color":
                    for channel in ("red", "green", "blue", "alpha"):
                        channel_data = candidate.get(channel)
                        if not isinstance(channel_data, dict):
                            continue
                        channel_points = channel_data.get("Points")
                        if isinstance(channel_points, list) and channel_points:
                            channel_path = base_path + (("dict", channel), ("dict", "Points"))
                            point_paths = [
                                channel_path + (("list", index),)
                                for index, _point in enumerate(channel_points)
                            ]
                            return {"points": channel_points, "paths": point_paths, "meta": meta}
            return None

        def convert_points(prop_key, prop_dict):
            points_info = _property_points(prop_key, prop_dict)
            if not isinstance(points_info, dict):
                return [], None, None, {}, []

            points = points_info.get("points") or []
            point_paths = points_info.get("paths") or []
            normalized_paths = []
            for path in point_paths:
                try:
                    normalized_paths.append(tuple(path))
                except TypeError:
                    normalized_paths.append(path)
            metadata = points_info.get("meta") or {}

            converted = []
            min_val = None
            max_val = None
            for index, point in enumerate(points):
                if not isinstance(point, dict):
                    continue
                co = point.get("co") if isinstance(point.get("co"), dict) else {}
                frame_val = co.get("X")
                try:
                    frame_float = float(frame_val)
                except (TypeError, ValueError):
                    continue
                seconds_abs = (frame_float - 1.0) / fps
                local_seconds = seconds_abs - clip_start
                absolute_seconds = position + local_seconds
                value = co.get("Y")
                try:
                    value_float = float(value)
                    if math.isnan(value_float) or math.isinf(value_float):
                        value_float = None
                except (TypeError, ValueError):
                    value_float = None
                if value_float is not None:
                    if min_val is None or value_float < min_val:
                        min_val = value_float
                    if max_val is None or value_float > max_val:
                        max_val = value_float
                entry = {
                    "frame": int(round(frame_float)),
                    "seconds": absolute_seconds,
                    "local_seconds": local_seconds,
                    "value": value_float,
                    "interpolation": point.get("interpolation"),
                }
                if index < len(point_paths):
                    try:
                        entry["path"] = tuple(point_paths[index])
                    except TypeError:
                        entry["path"] = point_paths[index]
                converted.append(entry)
            converted.sort(key=lambda entry: entry.get("seconds", 0.0))
            return converted, min_val, max_val, metadata, normalized_paths

        result = []
        for key, prop in props.items():
            if not isinstance(prop, dict):
                continue
            metadata_keyframe = bool(prop.get("keyframe"))
            point_count_value = prop.get("points")
            declared_points = None
            if point_count_value is not None:
                try:
                    declared_points = int(point_count_value)
                except (TypeError, ValueError):
                    declared_points = None
            points, min_val, max_val, source_meta, normalized_paths = convert_points(key, prop)
            if len(points) <= 1:
                if metadata_keyframe or (declared_points is not None and declared_points > 0):
                    log.info(
                        "Keyframe panel refresh: property %s has insufficient curve data (flag=%s points=%s)",
                        key,
                        metadata_keyframe,
                        point_count_value,
                    )
                continue
            if declared_points is not None and declared_points <= 1:
                log.debug(
                    "Keyframe panel refresh: promoting property %s with reported point count %s (actual=%s)",
                    key,
                    declared_points,
                    len(points),
                )
            if not metadata_keyframe:
                log.debug(
                    "Keyframe panel refresh: treating property %s as keyframe despite flag False", key
                )
            name = prop.get("name") or str(key)
            selected_frames = track_selection.get(key, set())
            if selected_frames:
                selected_frames = {int(frame) for frame in selected_frames}
            for point in points:
                frame_val = point.get("frame")
                try:
                    frame_int = int(frame_val) if frame_val is not None else None
                except (TypeError, ValueError):
                    frame_int = None
                point["frame"] = frame_int
                if selected_frames and frame_int is not None:
                    point["selected"] = frame_int in selected_frames
                else:
                    point["selected"] = False
            result.append(
                {
                    "key": key,
                    "display_name": _(name),
                    "points": points,
                    "min_value": min_val,
                    "max_value": max_val,
                    "source_meta": source_meta,
                    "owner_type": source_meta.get("owner") if isinstance(source_meta, dict) else None,
                    "value": prop.get("value"),
                    "value_type": prop.get("type"),
                    "point_paths": normalized_paths,
                }
            )

        result.sort(key=lambda item: item.get("display_name", "").lower())
        return result, context

    def _update_track_panel_properties(self):
        if not getattr(self, "win", None):
            log.info("Keyframe panel refresh skipped: no window reference")
            return False
        timeline_sync = getattr(self.win, "timeline_sync", None)
        timeline = getattr(timeline_sync, "timeline", None) if timeline_sync else None
        if not timeline:
            self._panel_properties = {}
            self._panel_heights = {}
            log.info("Keyframe panel refresh skipped: no timeline model")
            return False
        enabled_tracks = {
            self.normalize_track_number(track)
            for track, state in self._track_panel_enabled.items()
            if state
        }
        if not enabled_tracks:
            had_data = bool(self._panel_properties or self._panel_heights)
            if had_data:
                log.info("Keyframe panel refresh cleared: no panels enabled")
            self._panel_properties = {}
            self._panel_heights = {}
            return had_data
        selection = list(getattr(self.win, "selected_items", []) or [])
        frame = int(getattr(self, "current_frame", 1) or 1)
        if frame <= 0:
            frame = 1
        priority = {"effect": 0, "clip": 1, "transition": 2}
        new_props = {}
        new_heights = {}
        translate = get_app()._tr

        def _placeholder_info(label_text, reason):
            props = [{"display_name": label_text, "points": [], "placeholder": True}]
            info = {
                "item_id": "",
                "item_type": None,
                "properties": props,
                "context": {"placeholder": reason},
                "base_properties": {},
                "base_context": {},
            }
            return info, self._panel_height_for_properties(len(props))

        for sel in selection:
            item_id = sel.get("id")
            item_type = sel.get("type")
            if not item_id or item_type not in priority:
                continue
            context = self._panel_item_context(item_id, item_type)
            track_value = context.get("track") if isinstance(context, dict) else None
            track_num = self.normalize_track_number(track_value) if track_value is not None else None
            if track_num is None:
                track_num = self._track_number_for_selection(item_id, item_type)
            if track_num is None:
                log.info(
                    "Keyframe panel refresh: unable to determine track for %s %s",
                    item_type,
                    item_id,
                )
                continue
            key = self.normalize_track_number(track_num)
            if key not in enabled_tracks:
                log.info(
                    "Keyframe panel refresh: selection %s %s on track %s not enabled",
                    item_type,
                    item_id,
                    key,
                )
                continue
            existing = new_props.get(key)
            if existing and priority[existing.get("item_type")] <= priority[item_type]:
                continue
            properties, context = self._properties_for_item(
                timeline,
                item_id,
                item_type,
                frame,
                context=context,
            )
            if not properties:
                cached = self._panel_properties.get(key)
                cached_props = cached.get("properties") if isinstance(cached, dict) else None
                if (
                    cached
                    and cached_props
                    and cached.get("item_id") == str(item_id)
                    and cached.get("item_type") == item_type
                ):
                    if "base_properties" not in cached:
                        cached["base_properties"] = self._panel_capture_base_properties(cached_props)
                    if "base_context" not in cached:
                        cached["base_context"] = self._panel_capture_base_context(cached.get("context"))
                    log.info(
                        "Keyframe panel refresh: reusing cached properties for %s %s on track %s",
                        item_type,
                        item_id,
                        key,
                    )
                    new_props[key] = cached
                    cached_height = self._panel_heights.get(key)
                    if cached_height is None:
                        cached_height = self._panel_height_for_properties(len(cached_props))
                    new_heights[key] = cached_height
                    continue
                log.info(
                    "Keyframe panel refresh: no properties found for %s %s on track %s",
                    item_type,
                    item_id,
                    key,
                )
                continue
            info = {
                "item_id": str(item_id),
                "item_type": item_type,
                "properties": properties,
                "context": context,
                "base_properties": self._panel_capture_base_properties(properties),
                "base_context": self._panel_capture_base_context(context),
            }
            new_props[key] = info
            new_heights[key] = self._panel_height_for_properties(len(properties))
        missing_tracks = enabled_tracks - set(new_props.keys())
        if missing_tracks:
            reason = "no-selection" if not selection else "no-keyframes"
            label = translate("No Selection") if not selection else translate("No Keyframes")
            for track_num in sorted(missing_tracks):
                info, height = _placeholder_info(label, reason)
                new_props[track_num] = info
                new_heights[track_num] = height

        changed = new_props != self._panel_properties or new_heights != self._panel_heights
        if changed:
            enabled_tracks = [
                self.normalize_track_number(track)
                for track, state in self._track_panel_enabled.items()
                if state
            ]
            log.info(
                "Keyframe panel refresh: frame=%s selection=%s enabled_tracks=%s",
                frame,
                len(selection),
                enabled_tracks,
            )
            for track_num in sorted(new_props.keys()):
                info = new_props[track_num]
                context = info.get("context") or {}
                props = info.get("properties", [])
                if context.get("placeholder"):
                    log.info(
                        "  track %s placeholder (%s): message=%s",
                        track_num,
                        context.get("placeholder"),
                        props[0].get("display_name") if props else "",
                    )
                    continue
                prop_names = [prop.get("display_name") for prop in props]
                log.info(
                    "  track %s item %s (%s): properties=%s",
                    track_num,
                    info.get("item_id"),
                    info.get("item_type"),
                    prop_names,
                )
        elif not selection and any(self._track_panel_enabled.values()):
            log.info("Keyframe panel refresh: no selection while panels enabled")
        self._panel_properties = new_props
        self._panel_heights = new_heights
        self._panel_refresh_signature = self._panel_current_signature()
        self._refresh_panel_selection_state(new_props)
        return changed

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

        toolbar_button = self._track_toolbar_button_at(pos)
        if toolbar_button:
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

        if event.button() == Qt.LeftButton:
            toolbar_button = self._track_toolbar_button_at(pos)
            if toolbar_button:
                self._last_event = event
                self._toolbar_pressed_key = (toolbar_button.get("track_id"), toolbar_button.get("key"))
                self._toolbar_pressed_inside = True
                self._toolbar_hover_key = self._toolbar_pressed_key
                self.update()
                event.accept()
                return

        if self._handle_menu_icon_clicks(pos):
            return

        self._assign_press_target(event)

        if self._start_scroll_drag_if_needed(pos):
            return

        if self._press_hit == "panel-add":
            event.accept()
            return

        if self._press_hit == "effect-icon":
            event.accept()
            return

        self._last_event = event
        self.events.pressed.emit(event)

    def leaveEvent(self, event):
        if self._toolbar_hover_key is not None or self._toolbar_pressed_inside:
            self._toolbar_hover_key = None
            if self._toolbar_pressed_key:
                self._toolbar_pressed_inside = False
            self.update()
        super().leaveEvent(event)

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
        add_button = self._panel_add_button_at(pos)
        if add_button:
            self._press_hit = "panel-add"
            self._panel_press_info = add_button
            return
        panel_marker = self._panel_marker_at(pos)
        if panel_marker and panel_marker.get("point"):
            self._press_hit = "panel-keyframe"
            panel_marker = dict(panel_marker)
            panel_marker["modifiers"] = modifiers
            self._panel_press_info = panel_marker
            return
        self._panel_press_info = None
        panel_lane = self._panel_lane_at(pos)
        if panel_lane:
            self._press_hit = "panel"
            self._panel_press_info = {"lane": panel_lane}
            return
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

        pos = event.pos()
        if self._toolbar_pressed_key:
            self._update_toolbar_pressed_state(pos)
        self._update_toolbar_hover(pos)

        self._updateCursor(pos)
        self.events.moved.emit(event)

    def mouseReleaseEvent(self, event):
        self._last_event = event

        if event.button() == Qt.LeftButton and self._toolbar_pressed_key:
            button = self._get_toolbar_button(*self._toolbar_pressed_key)
            inside = bool(
                button
                and button.get("rect")
                and button["rect"].contains(event.pos())
                and self._toolbar_pressed_inside
            )
            self._toolbar_pressed_key = None
            self._toolbar_pressed_inside = False
            if inside and button:
                self._activate_track_toolbar_button(button)
            self._update_toolbar_hover(event.pos())
            self.update()
            event.accept()
            return

        if event.button() == Qt.MiddleButton and self._middle_panning:
            self._finishMiddlePan()
            return
        if self.scroll_bar_dragging or self.v_scroll_bar_dragging:
            self.scroll_bar_dragging = False
            self.v_scroll_bar_dragging = False
            self.mouse_dragging = False
            return
        press_hit = self._press_hit
        add_info_initial = self._panel_press_info if press_hit == "panel-add" else None
        effect_info = self._press_effect_icon if press_hit == "effect-icon" else None

        self.events.released.emit(event)

        if press_hit == "panel-add":
            info = self._panel_press_info or add_info_initial or {}
            self._panel_press_info = None
            self._press_hit = None
            self._handle_panel_add_click(info)
            event.accept()
            return

        if press_hit == "panel-keyframe":
            info = self._panel_press_info or {}
            self._panel_press_info = None
            if info.get("dragged"):
                self._press_hit = None
                event.accept()
                return
            point = info.get("point") if isinstance(info, dict) else None
            prop = info.get("property") if isinstance(info, dict) else None
            track_num = info.get("track") if isinstance(info, dict) else None
            frame_val = point.get("frame") if isinstance(point, dict) else None
            prop_key = prop.get("key") if isinstance(prop, dict) else None
            modifiers = event.modifiers() if hasattr(event, "modifiers") else Qt.NoModifier
            additive = bool(modifiers & Qt.ControlModifier)
            if frame_val is not None and prop_key and track_num is not None:
                try:
                    frame_int = int(frame_val)
                except (TypeError, ValueError):
                    frame_int = None
                if frame_int is not None:
                    if additive:
                        self._panel_toggle_frames(track_num, prop_key, {frame_int})
                    else:
                        self._panel_set_selection_map(track_num, {prop_key: {frame_int}})
            self._press_hit = None
            event.accept()
            return

        if press_hit == "effect-icon":
            self._press_hit = None
            self._press_effect_icon = None
            event.accept()
            self._handle_effect_icon_click(effect_info)
            return

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
        if self._press_hit == "panel-keyframe":
            info = self._panel_press_info or {}
            self._start_panel_keyframe_drag(info)
            return
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
        if self._dragging_panel_keyframes:
            self._panel_keyframe_move(event)
            return
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
        absolute_seconds = self._keyframe_base_position(marker) + relative_seconds
        self._panel_preview_marker(marker, drag.get("current_frame"), new_frame, absolute_seconds)
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

        base_position = self._keyframe_base_position(marker)
        pending_seconds = drag.get("pending_seconds")
        if pending_seconds is None and self.fps_float:
            pending_seconds = max(0.0, ((new_frame - 1.0) / self.fps_float) - drag.get("clip_start", 0.0))
        absolute_seconds = base_position + (pending_seconds or 0.0)
        self._panel_preview_marker(marker, old_frame, new_frame, absolute_seconds)

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
        if self._dragging_panel_keyframes:
            self._finish_panel_keyframe_drag()
            return
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
        self._track_index_from_num = {
            self.normalize_track_number(t.data["number"]): idx
            for idx, t in enumerate(self.track_list)
        }
        self._track_num_from_index = {
            idx: self.normalize_track_number(t.data["number"])
            for idx, t in enumerate(self.track_list)
        }

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
            frame_delta = frame_offset if frame_offset is not None else 0
            if delta_sec or frame_delta:
                self._panel_shift_item(itm, delta_sec, frame_delta)

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
        ctrl_down = bool(e.modifiers() & Qt.ControlModifier)
        self.box_start = e.pos()
        panel_lane = self._panel_lane_at(self.box_start)
        if panel_lane:
            self._panel_box_track = panel_lane.get("track")
            self._panel_box_bounds = self._panel_bounds_for_track(self._panel_box_track)
            if not ctrl_down:
                self._clear_panel_selection(self._panel_box_track)
        else:
            self._panel_box_track = None
            self._panel_box_bounds = QRectF()
            if not ctrl_down:
                # Starting a new box selection clears existing selections
                self.win.clearSelections()
        self.selection_rect = QRectF()

    def _boxMove(self):
        rect = QRectF(self.box_start, self._last_event.pos()).normalized()
        if self._panel_box_track is not None:
            bounds = self._panel_box_bounds
            if isinstance(bounds, QRectF) and not bounds.isNull():
                rect = rect.intersected(bounds)
            else:
                rect = QRectF()
        self.selection_rect = rect
        self.update()

    def _finishBoxSelect(self):
        """Finalize box-select: add items intersecting the selection rectangle."""
        self.geometry.ensure()
        if self._panel_box_track is not None:
            ctrl_down = False
            if self._last_event and hasattr(self._last_event, "modifiers"):
                mods = self._last_event.modifiers()
                ctrl_down = bool(mods & Qt.ControlModifier)
            track_num = self._panel_box_track
            selection_rect = QRectF(self.selection_rect)
            frames_by_prop = {}
            if not selection_rect.isNull():
                for lane in self._iter_panel_lanes() or []:
                    if lane.get("track") != track_num:
                        continue
                    combined = lane.get("combined_rect", QRectF())
                    if combined.isNull() or not combined.intersects(selection_rect):
                        continue
                    prop = lane.get("property") or {}
                    points = prop.get("points") or []
                    if not points:
                        continue
                    lane_rect = lane.get("lane_rect", QRectF())
                    lane_padding = lane.get("lane_padding", self._panel_lane_padding())
                    selected_frames = set()
                    for point in points:
                        seconds = point.get("seconds")
                        if seconds is None:
                            continue
                        marker_rect = self._panel_marker_rect(lane_rect, lane_padding, seconds)
                        if marker_rect.intersects(selection_rect):
                            frame_val = point.get("frame")
                            if frame_val is not None:
                                try:
                                    selected_frames.add(int(frame_val))
                                except (TypeError, ValueError):
                                    continue
                    if selected_frames:
                        frames_by_prop[prop.get("key")] = selected_frames
            if ctrl_down:
                if frames_by_prop:
                    self._panel_merge_selection_map(track_num, frames_by_prop)
            else:
                self._panel_set_selection_map(track_num, frames_by_prop)
            self.selection_rect = QRectF()
            self._panel_box_track = None
            self._panel_box_bounds = QRectF()
            self.update()
            return

        # Ensure geometry is up-to-date for clip selections
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
