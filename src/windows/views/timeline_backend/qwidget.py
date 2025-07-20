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
import re

from PyQt5.QtCore import (
    Qt, QRectF, QTimer, QPointF,
    QSignalTransition, pyqtSignal, QObject
)
from PyQt5.QtGui import (
    QPainter, QColor, QPen, QBrush, QCursor, QPainterPath, QIcon
)
from PyQt5.QtWidgets import QSizePolicy, QWidget

from .geometry import Geometry
from .paint import (
    BackgroundPainter, ClipPainter, TransitionPainter,
    MarkerPainter, PlayheadPainter, RulerPainter, TrackPainter,
    SelectionPainter,
)
from .snap import SnapHelper
from .theme import DEFAULT_THEME, apply_theme as parse_theme
from .state import TimelineStateMachine
from classes.time_parts import secondsToTime

from classes.app import get_app
from classes.query import Clip, Track, Transition, Marker, File


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
        self.left_handle_rect = QRectF()
        self.left_handle_dragging = False
        self.right_handle_rect = QRectF()
        self.right_handle_dragging = False
        self.scroll_bar_rect = QRectF()
        self.scroll_bar_dragging = False
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

        # Geometry constants
        self.ruler_height = 40
        self.track_name_width = 140
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
        self.selection_painter = SelectionPainter(self)

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

    def _buildStateMachine(self):
        sm = TimelineStateMachine(self)

        idle = sm.idle
        drag = sm.drag
        resize = sm.resize
        playhead = sm.playhead
        boxsel = sm.box

        drag.entered.connect(self._startClipDrag)
        drag.exited.connect(self._finishClipDrag)
        resize.entered.connect(self._startResize)
        resize.exited.connect(self._finishResize)
        playhead.entered.connect(self._startPlayhead)
        playhead.exited.connect(self._finishPlayhead)
        boxsel.entered.connect(self._startBoxSelect)
        boxsel.exited.connect(self._finishBoxSelect)

        idle.addTransition(_ConditionalTransition(
            self.events.pressed, drag,
            lambda: self._press_hit == "clip"
        ))
        idle.addTransition(_ConditionalTransition(
            self.events.pressed, resize,
            lambda: self._press_hit == "handle"
        ))
        idle.addTransition(_ConditionalTransition(
            self.events.pressed, playhead,
            lambda: self._press_hit == "ruler"
        ))
        idle.addTransition(_ConditionalTransition(
            self.events.pressed, boxsel,
            lambda: self._press_hit == "background"
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

        # repaint exactly once when any interactive state exits
        for s in (drag, resize, playhead, boxsel):
            s.exited.connect(self.update)

        sm.setInitialState(idle)
        sm.start()
        self._sm = sm

    def _safe_disconnect(self, signal, slot):
        try:
            signal.disconnect(slot)
        except TypeError:
            pass

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
            self.selection_painter,
        ):
            p.update_theme()
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

        # Invalidate and rebuild geometry
        self.geometry.mark_dirty()
        self.geometry.ensure()

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

        self.bg_painter.paint(painter, event.rect())
        self.ruler_painter.paint(painter)
        self.track_painter.paint(painter)
        self.clip_painter.paint(painter)
        self.transition_painter.paint(painter)
        self.marker_painter.paint(painter)
        self.playhead_painter.paint(painter)
        self.selection_painter.paint(painter)

        painter.end()

    def dragEnterEvent(self, event):
        # Check if the drag event contains the data type you can handle
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

        # If a plain text drag accept
        if not self.new_item and not event.mimeData().hasUrls() and event.mimeData().html():
            # get type of dropped data
            self.item_type = event.mimeData().html()

            # Track that a new item is being 'added'
            self.new_item = True

            # TODO: Implement drag n drop
            # Get the mime data (i.e. list of files, list of transitions, etc...)
            # data = json.loads(event.mimeData().text())
            # pos = event.posF()

            # create the item
            # if self.item_type == "clip":
            #     self.addClip(data, pos)
            # elif self.item_type == "transition":
            #     self.addTransition(data, pos)

            # accept all events, even if a new clip is not being added
            event.accept()

        # Accept a plain file URL (from the OS)
        elif not self.new_item and event.mimeData().hasUrls():
            # Track that a new item is being 'added'
            self.new_item = True
            self.item_type = "os_drop"

            # accept event
            event.accept()

        # DEBUG
        self.new_item = False

    def dragMoveEvent(self, event):
        # Optional: Provide feedback to the user about the drag operation
        event.accept()

    def dropEvent(self, event):
        event.accept()
        file_ids = []
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            self.win.files_model.process_urls(urls, import_quietly=True, prevent_image_seq=True)
            for uri in urls:
                for f in File.filter(path=uri.toLocalFile()):
                    file_ids.append(f.id)
        elif event.mimeData().html() == "clip":
            ids = json.loads(event.mimeData().text())
            if not isinstance(ids, list):
                ids = [ids]
            file_ids.extend(ids)
        elif event.mimeData().html() == "transition":
            ids = json.loads(event.mimeData().text())
            if not isinstance(ids, list):
                ids = [ids]
            file_ids.extend(ids)

        if not file_ids:
            return

        pos_seconds = max(0.0, (event.pos().x() - self.track_name_width) / self.pixels_per_second)
        track_idx = int((event.pos().y() - self.ruler_height) / self.vertical_factor)
        track_idx = min(max(track_idx, 0), len(self.track_list)-1)
        track_num = self.track_list[track_idx].data.get("number")
        pos = QPointF(pos_seconds, 0)
        for fid in file_ids:
            if event.mimeData().html() == "transition":
                item = self.addTransition(fid, pos, track_num, ignore_refresh=False, call_manual_move=False)
                if item:
                    pos.setX(pos.x() + (item["end"] - item["start"]))
            else:
                clip = self.addClip(fid, pos, track_num, ignore_refresh=False, call_manual_move=False)
                if clip:
                    pos.setX(pos.x() + (clip["end"] - clip["start"]))


    def resizeEvent(self, event):
        """Widget resize event"""
        event.accept()
        self.delayed_size = self.size()
        self.delayed_resize_timer.start()

    def delayed_resize_callback(self):
        """Callback for resize event timer (to delay the resize event, and prevent lots of similar resize events)"""
        # Get max width of timeline
        project_duration = get_app().project.get("duration")
        normalized_scroll_width = self.scrollbar_position[1] - self.scrollbar_position[0]
        scroll_width_seconds = normalized_scroll_width * project_duration
        tick_pixels = 100
        if self.scrollbar_position[3] > 0.0:
            # Calculate the new zoom factor, based on pixels per tick
            zoom_factor = scroll_width_seconds / (self.scrollbar_position[3] / tick_pixels)

            # Set scroll width (and send signal)
            if zoom_factor > 0.0:
                self.setZoomFactor(zoom_factor)

                # Emit signal to scroll Timeline
                get_app().window.TimelineScroll.emit(self.scrollbar_position[0])

    # Capture wheel event to alter zoom/scale of widget
    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoomIn()
            else:
                self.zoomOut()
            event.accept()
        else:
            event.ignore()

    def setZoomFactor(self, zoom_factor, emit=True):
        """Set the current zoom factor"""
        # Force recalculation of clips
        self.zoom_factor = zoom_factor
        TimelineWidget.changed(self, None)

        if emit:
            # Emit zoom signal
            get_app().window.TimelineZoom.emit(self.zoom_factor)
            get_app().window.TimelineCenter.emit()

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

        self.scrollbar_position = new_positions

        # Check for empty clip rectangles
        if not self.geometry.clip_rects:
            TimelineWidget.changed(self, None)

        # Disable auto center
        self.is_auto_center = False

        # Schedule repaint
        self.update()

    def handle_selection(self):
        # Force recalculation of clips and repaint
        TimelineWidget.changed(self, None)
        self.update()

    def _move_playhead(self, x_pos):
        fps = get_app().project.get("fps")
        fps_float = float(fps.get("num", 24)) / float(fps.get("den", 1) or 1)
        seconds = max(0.0, (x_pos - self.track_name_width) / self.pixels_per_second)
        frame = int(seconds * fps_float) + 1
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

    def mousePressEvent(self, event):
        self.geometry.ensure()
        self._press_hit = self._hitTest(event.pos())
        self._last_event = event
        self.events.pressed.emit(event)

    def mouseMoveEvent(self, event):
        self._last_event = event
        self.events.moved.emit(event)

    def mouseReleaseEvent(self, event):
        self._last_event = event
        self.events.released.emit(event)
        self._press_hit = None

    # ---- Clip drag ----
    def _startClipDrag(self):
        """Begin a drag operation on one or many selected clips/transitions."""
        e = self._last_event

        # Identify the item under the cursor (include clips and transitions)
        clicked_item = None
        for rect, item in (
            self.geometry.selected_rects +
            self.geometry.clip_rects +
            self.geometry.selected_transitions +
            self.geometry.transition_rects
        ):
            if rect.contains(e.pos()):
                clicked_item = item
                break
        if clicked_item is None:
            return

        ctrl = bool(e.modifiers() & Qt.ControlModifier)
        already = (
            clicked_item.id in self.win.selected_clips or
            clicked_item.id in self.win.selected_transitions
        )

        if not already:
            if not ctrl:
                for cid in list(self.win.selected_clips):
                    self.win.removeSelection(cid, "clip")
                for tid in list(self.win.selected_transitions):
                    self.win.removeSelection(tid, "transition")
            sel_type = "transition" if isinstance(clicked_item, Transition) else "clip"
            self.win.addSelection(clicked_item.id, sel_type, False)
            TimelineWidget.changed(self, None)

        # All selected clips and transitions participate in the drag
        self.dragging_items = [itm for _, itm in self.geometry.selected_rects] + [itm for _, itm in self.geometry.selected_transitions]
        if not self.dragging_items:
            self.dragging_items = [clicked_item]

        # Map track number → index
        self._track_index_from_num = { t.data["number"]: idx for idx, t in enumerate(self.track_list) }
        self._track_num_from_index = { idx: t.data["number"] for idx, t in enumerate(self.track_list) }

        # Record each item’s starting position and layer index
        self._drag_initial = {
            itm.id: (
                itm.data.get("position", 0.0),
                self._track_index_from_num.get(itm.data.get("layer", 0), 0)
            )
            for itm in self.dragging_items
        }

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
        new_bbox_x = e.pos().x() - self.drag_clip_offset
        delta_sec = (new_bbox_x - self.drag_bbox.x()) / self.pixels_per_second

        # Snap horizontally ±1.5 s (pure x-axis)
        delta_sec = self._snap_delta(delta_sec)

        # -------- Vertical delta (track indexes) ----
        new_idx_under_cursor = int(
            (e.pos().y() - self.ruler_height) / self.vertical_factor
        )
        delta_idx = new_idx_under_cursor - self._drag_layer_idx_start

        # Clamp delta_idx so *all* items stay within valid index range
        orig_indices = [info[1] for info in self._drag_initial.values()]
        if orig_indices:
            if min(orig_indices) + delta_idx < 0:
                delta_idx = -min(orig_indices)
            if max(orig_indices) + delta_idx >= len(self.track_list):
                delta_idx = (len(self.track_list) - 1) - max(orig_indices)

        # -------- Apply identical deltas ------------
        for itm in self.dragging_items:
            start_pos_sec, start_idx = self._drag_initial[itm.id]

            # New values
            new_pos_sec = max(0.0, start_pos_sec + delta_sec)
            new_idx = start_idx + delta_idx
            new_idx = max(0, min(new_idx, len(self.track_list) - 1))
            new_layer_num = self._track_num_from_index[new_idx]

            itm.data["position"] = new_pos_sec
            itm.data["layer"] = new_layer_num

            # Update cached rect
            rect = self.geometry.calc_item_rect(itm)
            self.geometry.update_item_rect(itm, rect)

        # Immediate visual feedback
        self.update()

    def _finishClipDrag(self):
        """Persist all moved clips/transitions and refresh geometry."""
        if getattr(self, "dragging_items", None):
            total = len(self.dragging_items)
            for idx, itm in enumerate(self.dragging_items):
                ignore_refresh = idx < total - 1
                if isinstance(itm, Transition):
                    self.update_transition_data(
                        itm.data,
                        only_basic_props=True,
                        ignore_refresh=ignore_refresh,
                    )
                else:
                    self.update_clip_data(
                        itm.data,
                        only_basic_props=True,
                        ignore_reader=True,
                        ignore_refresh=ignore_refresh,
                    )

        self.dragging_items = []
        # Recompute geometry (snap may have shifted) and repaint
        TimelineWidget.changed(self, None)
        self.update()

    def _compute_selected_bounding(self):
        """Return a QRectF encompassing all currently-selected clips and transitions."""
        items = self.geometry.selected_rects + self.geometry.selected_transitions
        if not items:
            return QRectF()
        bbox = QRectF(items[0][0])
        for rect, _ in items[1:]:
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
        return self.snap.snap_dx(delta_seconds)

    # ---- Resize track names ----
    def _startResize(self):
        self._resize_start = self.track_name_width

    def _resizeMove(self):
        new_width = max(40, self._last_event.pos().x())
        if new_width != self.track_name_width:
            self.track_name_width = new_width
            TimelineWidget.changed(self, None)

    def _finishResize(self):
        pass

    # ---- Playhead move ----
    def _startPlayhead(self):
        self.dragging_playhead = True
        self._move_playhead(self._last_event.pos().x())

    def _playheadMove(self):
        if self.dragging_playhead:
            self._move_playhead(self._last_event.pos().x())

    def _finishPlayhead(self):
        self.dragging_playhead = False

    # ---- Box selection ----
    def _startBoxSelect(self):
        e = self._last_event
        if not (e.modifiers() & Qt.ControlModifier):
            for cid in list(self.win.selected_clips):
                self.win.removeSelection(cid, "clip")
            for tid in list(self.win.selected_transitions):
                self.win.removeSelection(tid, "transition")
        self.box_start = e.pos()
        self.selection_rect = QRectF()

    def _boxMove(self):
        self.selection_rect = QRectF(self.box_start, self._last_event.pos()).normalized()
        self.update()

    def _finishBoxSelect(self):
        """Finalize box-select: add items intersecting the selection rectangle."""
        add = bool(self._last_event.modifiers() & Qt.ControlModifier)

        # Ensure geometry is up-to-date
        self.geometry.mark_dirty()
        self.geometry.ensure()

        # Add any item whose rect intersects selection_rect
        for rect, item in (
            self.geometry.clip_rects +
            self.geometry.selected_rects +
            self.geometry.transition_rects +
            self.geometry.selected_transitions
        ):
            if rect.intersects(self.selection_rect):
                sel_type = "transition" if isinstance(item, Transition) else "clip"
                # False = don’t emit SelectionChanged (we’ll handle it ourselves)
                self.win.addSelection(item.id, sel_type, False)

        # Clear the box
        self.selection_rect = QRectF()

        # Recompute all clip/track geometry and repaint immediately
        TimelineWidget.changed(self, None)
        self.update()

