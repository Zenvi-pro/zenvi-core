"""
 @file
 @brief This file contains the zoom slider QWidget (for interactive zooming/panning on the timeline)
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2018 OpenShot Studios, LLC
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
import copy
import math
import threading

from PyQt5.QtCore import (
    Qt, QCoreApplication, QRectF, QTimer, QSize, QThread, pyqtSignal
)
from PyQt5.QtGui import (
    QPainter, QColor, QPen, QBrush, QCursor, QPainterPath, QIcon
)
from PyQt5.QtWidgets import QSizePolicy, QWidget

import openshot  # Python module for libopenshot (required video editing module installed separately)

from classes import updates
from classes.app import get_app
from classes.query import Clip, Track, Transition, Marker
from classes.logger import log
from classes.recompute_queue import CoalescingRecomputeQueue
from windows.views.minimap_geometry import compute_minimap_rects


class _MinimapGeometryWorker(QThread):
    """Background worker that recomputes minimap geometry off the GUI thread.

    Pulls the newest queued snapshot, computes plain rect tuples, and commits
    them latest-wins so superseded results are dropped. Emits result_ready (no
    args) so the widget can pick up the committed result on the GUI thread.
    """

    result_ready = pyqtSignal()

    def __init__(self, recompute_queue, parent=None):
        super().__init__(parent)
        self._queue = recompute_queue
        self._wake = threading.Event()
        self._running = True

    def wake(self):
        self._wake.set()

    def stop(self):
        self._running = False
        self._wake.set()

    def run(self):
        while self._running:
            self._wake.wait()
            self._wake.clear()
            if not self._running:
                break
            while True:
                pending = self._queue.take_pending()
                if pending is None:
                    break
                generation, payload = pending
                if self._queue.is_stale(generation):
                    # A newer recompute was already requested; skip this one.
                    continue
                try:
                    rects = compute_minimap_rects(**payload)
                except Exception as ex:
                    log.warning("Minimap geometry recompute failed: %s", ex)
                    continue
                if self._queue.commit(rects, generation):
                    self.result_ready.emit()


class ZoomSlider(QWidget, updates.UpdateInterface):
    """ A QWidget used to zoom and pan around a Timeline"""

    def sizeHint(self):
        """Preferred size for layouts that host the slider."""
        return QSize(200, 20)

    def minimumSizeHint(self):
        """Allow the slider to shrink horizontally when space is limited."""
        return QSize(0, 20)

    # This method is invoked by the UpdateManager each time a change happens (i.e UpdateInterface)
    def changed(self, action):
        # Ignore changes that don't affect this
        if (action and len(action.key) >= 1 and action.key[0].lower() in ["files", "history", "profile"]) or self.ignore_updates:
            return

        # A committed change ends any live drag mirroring; recompute from persisted data.
        self._drag_overrides = None
        self._schedule_recompute()

    def apply_drag_overrides(self, overrides):
        """Mirror the timeline's live drag overrides on the minimap.

        Called while a clip/transition is being dragged on the timeline so the
        overview tracks the cursor immediately (instead of jumping a drag behind
        when the move is finally persisted). Passing a falsy value clears the
        drag state and recomputes from persisted project data.
        """
        if overrides:
            self._drag_overrides = dict(overrides)
        else:
            self._drag_overrides = None
        self._schedule_recompute()

    def _build_snapshot(self):
        """Capture an immutable, plain-data snapshot for off-thread recompute.

        Reads project data on the GUI thread (where the query cache lives) and
        returns only plain dicts/sets so the worker never touches Qt or the
        shared query cache.
        """
        app = get_app()
        if not app or not getattr(app, "window", None) or not getattr(app.window, "timeline", None):
            return None

        project_duration = app.project.get("duration")
        if not project_duration:
            return None

        # Layer lookup: layer number -> row index (0 at top), matching paint order.
        layer_index = {}
        for count, layer in enumerate(reversed(sorted(Track.filter()))):
            layer_index[layer.data.get('number')] = count

        # clip.data is an already-detached cached copy; the worker only reads it.
        clips = [clip.data for clip in Clip.filter()]
        transitions = [t.data for t in Transition.filter()]
        markers = [m.data for m in Marker.filter()]
        selected = set(app.window.selected_clips) | set(app.window.selected_transitions)

        return {
            "clips": clips,
            "transitions": transitions,
            "markers": markers,
            "selected_ids": selected,
            "layer_index": layer_index,
            "width": float(self.width()),
            "height": float(self.height()),
            "duration": float(project_duration),
            "overrides": dict(self._drag_overrides) if self._drag_overrides else None,
        }

    def _schedule_recompute(self):
        """Queue a latest-wins minimap geometry recompute on the worker."""
        snapshot = self._build_snapshot()
        if snapshot is None:
            return
        generation = self._recompute_queue.next_generation()
        self._recompute_queue.submit(generation, snapshot)
        self._minimap_worker.wake()

    def _on_rects_ready(self):
        """GUI-thread slot: adopt the latest committed geometry and repaint."""
        result = self._recompute_queue.result
        if not result:
            return
        self.clip_rects = [QRectF(*r) for r in result.get("clip_rects", [])]
        self.clip_rects_selected = [QRectF(*r) for r in result.get("clip_rects_selected", [])]
        self.marker_rects = [QRectF(*r) for r in result.get("marker_rects", [])]
        self.update()

    def paintEvent(self, event, *args):
        """ Custom paint event """
        event.accept()

        # Get theme colors
        if get_app().theme_manager:
            theme = get_app().theme_manager.get_current_theme()
            if not theme:
                log.warning("No theme loaded yet. Skip rendering zoom slider widget.")
                return
            playhead_color = theme.get_color(".zoom_slider_playhead", "background-color")
        else:
            log.warning("No ThemeManager loaded yet. Skip rendering zoom slider widget.")

        # Paint timeline preview on QWidget
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing, True)

        # Fill the whole widget with the solid color (background solid color)
        background_color = self.palette().color(self.palette().Base)
        painter.fillRect(event.rect(), background_color)

        # Create pens / colors
        clip_pen = QPen(QBrush(QColor("#53a0ed")), 1.5)
        clip_pen.setCosmetic(True)
        painter.setPen(clip_pen)

        selected_clip_pen = QPen(QBrush(QColor("Red")), 1.5)
        selected_clip_pen.setCosmetic(True)

        scroll_color = QColor("#4053a0ed")
        scroll_pen = QPen(QBrush(scroll_color), 2.0)
        scroll_pen.setCosmetic(True)

        marker_color = QColor("#4053a0ed")
        marker_pen = QPen(QBrush(marker_color), 1.0)
        marker_pen.setCosmetic(True)

        playhead_color = playhead_color
        playhead_pen = QPen(QBrush(playhead_color), 1.0)
        playhead_pen.setCosmetic(True)

        handle_color = QColor("#a653a0ed")
        handle_pen = QPen(QBrush(handle_color), 1.5)
        handle_pen.setCosmetic(True)

        # Get layer lookup
        layers = Track.filter()

        # Wait for timeline object and valid scrollbar positions
        if get_app().window.timeline:
            # Get max width of timeline
            project_duration = get_app().project.get("duration")
            pixels_per_second = event.rect().width() / project_duration
            scroll_width = (self.scrollbar_position[1] - self.scrollbar_position[0]) * event.rect().width()

            # Get FPS info
            fps_num = get_app().project.get("fps").get("num", 24)
            fps_den = get_app().project.get("fps").get("den", 1) or 1
            fps_float = float(fps_num / fps_den)

            # Determine scale factor
            vertical_factor = event.rect().height() / len(layers)

            # Loop through each clip
            painter.setPen(clip_pen)
            for clip_rect in self.clip_rects:
                painter.drawRect(clip_rect)

            painter.setPen(selected_clip_pen)
            for clip_rect in self.clip_rects_selected:
                painter.drawRect(clip_rect)

            painter.setPen(marker_pen)
            for marker_rect in self.marker_rects:
                painter.drawRect(marker_rect)

            painter.setPen(playhead_pen)
            playhead_x = ((self.current_frame / fps_float) * pixels_per_second)
            playhead_rect = QRectF(playhead_x, 0, 0.5, len(layers) * vertical_factor)
            painter.drawRect(playhead_rect)

            # Draw scroll bars (if available)
            if self.scrollbar_position:
                painter.setPen(scroll_pen)

                # scroll bar path
                scroll_x = self.scrollbar_position[0] * event.rect().width()
                self.scroll_bar_rect = QRectF(scroll_x, 0.0, scroll_width, event.rect().height())
                scroll_path = QPainterPath()
                scroll_path.addRoundedRect(self.scroll_bar_rect, 6, 6)

                # draw scroll bar rect
                painter.fillPath(scroll_path, scroll_color)
                painter.drawPath(scroll_path)

                # draw handles
                painter.setPen(handle_pen)
                handle_width = 12.0

                # left handle
                left_handle_x = (self.scrollbar_position[0] * event.rect().width()) - (handle_width/2.0)
                self.left_handle_rect = QRectF(left_handle_x, event.rect().height() / 4.0, handle_width, event.rect().height() / 2.0)
                left_handle_path = QPainterPath()
                left_handle_path.addRoundedRect(self.left_handle_rect, handle_width, handle_width)
                painter.fillPath(left_handle_path, handle_color)

                # right handle
                right_handle_x = self.scroll_bar_rect.right() - (handle_width/2.0)
                right_handle_x = min(right_handle_x, event.rect().width() - (handle_width/2.0))
                self.right_handle_rect = QRectF(right_handle_x, event.rect().height() / 4.0, handle_width, event.rect().height() / 2.0)
                right_handle_path = QPainterPath()
                right_handle_path.addRoundedRect(self.right_handle_rect, handle_width, handle_width)
                painter.fillPath(right_handle_path, handle_color)

            # Determine if play-head is inside scroll area
            if get_app().window.preview_thread.player.Mode() == openshot.PLAYBACK_PLAY and self.is_auto_center:
                if not self.scroll_bar_rect.contains(playhead_rect):
                    get_app().window.TimelineCenter.emit()

        # End painter
        painter.end()

    def zoomToTimeline(self):
        """Toggle between zooming to the entire timeline and the previous zoom"""
        # Are we already zoomed complete out?
        if math.isclose(self.scrollbar_position[0], 0.0, abs_tol=1e-9) and math.isclose(self.scrollbar_position[1], 1.0, abs_tol=1e-9):
            # Restore previous zoom
            self.scrollbar_position[0] = self.scrollbar_zoom_previous[0]
            self.scrollbar_position[1] = self.scrollbar_zoom_previous[1]
        else:
            # Zoom out to reveal the entire timeline
            self.scrollbar_zoom_previous = copy.deepcopy(self.scrollbar_position)
            self.scrollbar_position[0] = 0.0
            self.scrollbar_position[1] = 1.0
        self.delayed_resize_callback()

    def mouseDoubleClickEvent(self, event):
        self.zoomToTimeline()
        self.mouse_dragging = True  # Prevent mouseReleaseEvent from moving selection
        event.accept()

    def mousePressEvent(self, event):
        """Capture mouse press event"""
        event.accept()
        self.mouse_pressed = True
        self.mouse_dragging = False
        self.mouse_position = event.pos().x()
        self.scrollbar_position_previous = list(self.scrollbar_position)  # copy, don't alias

    def mouseReleaseEvent(self, event):
        """Capture mouse release event"""
        event.accept()

        # Handle the case where no dragging occurred (single click)
        if not self.mouse_dragging and not self.scroll_bar_rect.contains(event.pos()):
            # Center the scroll region at the click position (if outside the selection)
            click_pos = event.pos().x() / self.width()
            selection_width = self.scrollbar_position[1] - self.scrollbar_position[0]
            half_width = selection_width / 2

            # Calculate new left / right handles
            new_left_pos = click_pos - half_width
            new_right_pos = click_pos + half_width

            # If the new left position is less than 0, adjust both sides to fit within bounds
            if new_left_pos < 0.0:
                diff = -new_left_pos
                new_left_pos = 0.0
                new_right_pos = min(1.0, new_right_pos + diff)

            # If the new right position is greater than 1, adjust both sides to fit within bounds
            if new_right_pos > 1.0:
                diff = new_right_pos - 1.0
                new_right_pos = 1.0
                new_left_pos = max(0.0, new_left_pos - diff)

            # Update the scrollbar position to the newly calculated values
            self.scrollbar_position = [new_left_pos, new_right_pos, self.scrollbar_position[2], self.scrollbar_position[3]]

            # Trigger the resize and update
            self.delayed_resize_timer.start()
            self.update()

        # Finalize drag selection
        self.mouse_pressed = False
        self.mouse_dragging = False
        self.left_handle_dragging = False
        self.right_handle_dragging = False
        self.scroll_bar_dragging = False
        self.create_bar_dragging = False
        self.update()

    def set_handle_limits(self, left_handle, right_handle, is_left=False):
        """Set min/max limits on the bounds of the handles (to prevent invalid values)"""
        if left_handle < 0.0:
            left_handle = 0.0
            right_handle = self.scroll_bar_rect.width() / self.width()
        if right_handle > 1.0:
            left_handle = 1.0 - (self.scroll_bar_rect.width() / self.width())
            right_handle = 1.0

        # Don't allow handles to extend past each other
        diff = right_handle - left_handle

        # Adjust currently dragged handle (if exceeding min distance)
        if is_left and diff < self.min_distance:
            left_handle = right_handle - self.min_distance
        elif not is_left and diff < self.min_distance:
            right_handle = left_handle + self.min_distance

        return left_handle, right_handle

    def mouseMoveEvent(self, event):
        """Capture mouse events"""
        event.accept()

        # Get current mouse position
        mouse_pos = event.pos().x()
        if mouse_pos < 0:
            mouse_pos = 0
        elif mouse_pos > self.width():
            mouse_pos = self.width()

        # Set cursor (based on current mouse position)
        drag_threshold = 5
        if not self.mouse_dragging:
            if self.left_handle_rect.contains(event.pos()):
                self.setCursor(self.cursors.get('resize_x'))
            elif self.right_handle_rect.contains(event.pos()):
                self.setCursor(self.cursors.get('resize_x'))
            elif self.scroll_bar_rect.contains(event.pos()):
                self.setCursor(self.cursors.get('move'))
            else:
                self.setCursor(Qt.ArrowCursor)

        # Detect dragging (only if the user clicked and started dragging beyond the threshold)
        if self.mouse_pressed and not self.mouse_dragging:
            self.mouse_dragging = True
            if self.left_handle_rect.contains(event.pos()):
                self.left_handle_dragging = True
            elif self.right_handle_rect.contains(event.pos()):
                self.right_handle_dragging = True
            elif self.scroll_bar_rect.contains(event.pos()):
                self.scroll_bar_dragging = True
            elif abs(self.mouse_position - mouse_pos) > drag_threshold:
                # If clicking outside the current selection, initiate drag to create a new selection
                self.create_bar_dragging = True
            else:
                self.mouse_dragging = False

        # Handle dragging the selection (scroll bar dragging)
        if self.mouse_dragging:
            if self.left_handle_dragging:
                # Dragging the left handle to resize the selection
                delta = (self.mouse_position - mouse_pos) / self.width()
                new_left_pos = self.scrollbar_position_previous[0] - delta
                is_left = True

                if int(QCoreApplication.instance().keyboardModifiers() & Qt.ShiftModifier) > 0:
                    # SHIFT key pressed, move both handles
                    if (self.scrollbar_position_previous[1] + delta) - new_left_pos > self.min_distance:
                        new_right_pos = self.scrollbar_position_previous[1] + delta
                    else:
                        midpoint = (self.scrollbar_position_previous[1] + self.scrollbar_position_previous[0]) / 2
                        new_right_pos = midpoint + (self.min_distance / 2)
                        new_left_pos = midpoint - (self.min_distance / 2)
                else:
                    new_right_pos = self.scrollbar_position_previous[1]

                # Enforce limits (don't allow handles to go past each other, or out of bounds)
                new_left_pos, new_right_pos = self.set_handle_limits(new_left_pos, new_right_pos, is_left)

                self.scrollbar_position = [new_left_pos, new_right_pos, self.scrollbar_position[2], self.scrollbar_position[3]]
                self.delayed_resize_timer.start()

            elif self.right_handle_dragging:
                # Dragging the right handle to resize the selection
                delta = (self.mouse_position - mouse_pos) / self.width()
                new_right_pos = self.scrollbar_position_previous[1] - delta
                is_left = False

                if int(QCoreApplication.instance().keyboardModifiers() & Qt.ShiftModifier) > 0:
                    # SHIFT key pressed, move both handles
                    if new_right_pos - (self.scrollbar_position_previous[0] + delta) > self.min_distance:
                        new_left_pos = self.scrollbar_position_previous[0] + delta
                    else:
                        midpoint = (self.scrollbar_position_previous[1] + self.scrollbar_position_previous[0]) / 2
                        new_right_pos = midpoint + (self.min_distance / 2)
                        new_left_pos = midpoint - (self.min_distance / 2)
                else:
                    new_left_pos = self.scrollbar_position_previous[0]

                # Enforce limits
                new_left_pos, new_right_pos = self.set_handle_limits(new_left_pos, new_right_pos, is_left)

                self.scrollbar_position = [new_left_pos, new_right_pos, self.scrollbar_position[2], self.scrollbar_position[3]]
                self.delayed_resize_timer.start()

            elif self.scroll_bar_dragging:
                # Dragging the entire selection (scrolling the timeline)
                delta = (self.mouse_position - mouse_pos) / self.width()
                new_left_pos = self.scrollbar_position_previous[0] - delta
                new_right_pos = self.scrollbar_position_previous[1] - delta

                # Enforce limits
                new_left_pos, new_right_pos = self.set_handle_limits(new_left_pos, new_right_pos)

                self.scrollbar_position = [new_left_pos, new_right_pos, self.scrollbar_position[2], self.scrollbar_position[3]]
                get_app().window.TimelineScroll.emit(new_left_pos)

            elif self.create_bar_dragging:
                # Handle creating a new selection region
                new_pos = mouse_pos / self.width()

                if self.mouse_position < mouse_pos:
                    # Dragging to the right: set both handles to the starting position,
                    # then move the right handle (left handle stays where the drag started)
                    new_left_pos = self.mouse_position / self.width()
                    new_right_pos = new_pos
                else:
                    # Dragging to the left: set both handles to the starting position,
                    # then move the left handle (right handle stays where the drag started)
                    new_right_pos = self.mouse_position / self.width()
                    new_left_pos = new_pos

                # Enforce limits for the new selection
                new_left_pos, new_right_pos = self.set_handle_limits(new_left_pos, new_right_pos)
                self.scrollbar_position = [new_left_pos, new_right_pos, self.scrollbar_position[2], self.scrollbar_position[3]]
                self.delayed_resize_timer.start()

            # Force re-paint after any dragging
            self.update()

    def resizeEvent(self, event):
        """Widget resize event"""
        event.accept()
        self.delayed_size = self.size()
        self.delayed_resize_timer.start()
        # Clip geometry depends on widget width, so recompute when the size changes.
        self._schedule_recompute()

    def closeEvent(self, event):
        """Stop the background recompute worker when the widget closes."""
        try:
            worker = getattr(self, "_minimap_worker", None)
            if worker is not None:
                worker.stop()
                worker.wait(1000)
        except Exception:
            pass
        super().closeEvent(event)

    def get_scroll_width(self):
        """Calculate the width of the scrollbar handle (i.e. selection width)"""
        # Get max width of timeline
        project_duration = get_app().project.get("duration")

        # Calculate scroll bar / selection width
        timeline_pixels_per_second = 100.0 / get_app().project.get("scale")
        timeline_project_width = project_duration * timeline_pixels_per_second
        scroll_ratio = self.scrollbar_position[3] / timeline_project_width
        scroll_width = scroll_ratio * self.width()
        scroll_width = min(scroll_width, self.width())
        return scroll_width, scroll_ratio

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
                self._syncing_backend = True
                try:
                    get_app().window.TimelineScroll.emit(self.scrollbar_position[0])
                    self.setZoomFactor(zoom_factor)
                finally:
                    self._syncing_backend = False

    # Capture wheel event to alter zoom/scale of widget
    def wheelEvent(self, event):
        event.accept()

        # Repaint widget on zoom
        self.repaint()

    def setZoomFactor(self, zoom_factor, center=False, emit=True):
        """Set the current zoom factor (do not clamp width here — backend owns authoritative geometry)."""
        self.zoom_factor = zoom_factor
        if emit:
            get_app().window.TimelineZoom.emit(self.zoom_factor)
        if center:
            get_app().window.TimelineCenter.emit()

        # Force re-paint
        self.repaint()

    def zoomIn(self):
        """Zoom into timeline"""
        if self.zoom_factor >= 10.0:
            new_factor = self.zoom_factor - 5.0
        elif self.zoom_factor >= 4.0:
            new_factor = self.zoom_factor - 2.0
        else:
            new_factor = self.zoom_factor * 0.8

        # Emit zoom signal
        self.setZoomFactor(new_factor, center=True)

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
        self.setZoomFactor(new_factor, center=True)

    def update_scrollbars(self, new_positions):
        """Consume the current scroll bar positions from the webview timeline"""
        if self.mouse_dragging:
            return

        self.scrollbar_position = new_positions

        # Check for empty clips rects
        if not self.clip_rects:
            self.changed(None)

        # Disable auto center
        self.is_auto_center = False

        # Force re-paint
        self.repaint()

    def handle_selection(self):
        # Force recalculation of clips and repaint
        self.changed(None)
        self.repaint()

    def timeline_resized(self):
        # Force recalculation of clips and repaint
        self.repaint()
        self.delayed_resize_timer.start()

    def update_playhead_pos(self, currentFrame):
        """Callback when position is changed"""
        self.current_frame = currentFrame

        # Force re-paint
        self.repaint()

    def handle_play(self):
        """Callback when play button is clicked"""
        self.is_auto_center = True

    def connect_playback(self):
        """Connect playback signals"""
        self.win.preview_thread.position_changed.connect(self.update_playhead_pos)
        self.win.PlaySignal.connect(self.handle_play)

    def ignore_updates_callback(self, ignore, show_wait=True):
        """Ignore updates callback - used to stop updating this widget during batch updates"""
        if not ignore and self.ignore_updates:
            # Force recalculation and repaint
            self.ignore_updates = ignore
            self.changed(None)
            self.repaint()
        self.ignore_updates = ignore

    def __init__(self, *args):
        # Invoke parent init
        QWidget.__init__(self, *args)

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
        self.scrollbar_zoom_previous = [0.0, 0.2, 0.0, 0.0]
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
        self.min_distance = 0.002
        self.ignore_updates = False
        self._syncing_backend = False

        # Live drag overrides mirrored from the timeline track (or None when idle)
        self._drag_overrides = None

        # Coalescing latest-wins recompute pipeline (keeps geometry off the GUI thread)
        self._recompute_queue = CoalescingRecomputeQueue()
        self._minimap_worker = _MinimapGeometryWorker(self._recompute_queue, self)
        self._minimap_worker.result_ready.connect(self._on_rects_ready)
        self._minimap_worker.start()

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

        # Connect zoom functionality
        self.win.TimelineScrolled.connect(self.update_scrollbars)
        self.win.TimelineResize.connect(self.timeline_resized)
        self.win.IgnoreUpdates.connect(self.ignore_updates_callback)
        self.win.TimelineZoom.connect(lambda z: self.setZoomFactor(z, emit=False))

        # Live drag mirroring from the timeline track
        if hasattr(self.win, "TimelineDragPreview"):
            self.win.TimelineDragPreview.connect(self.apply_drag_overrides)

        # Connect Selection signals
        self.win.SelectionChanged.connect(self.handle_selection)

        # Show Property timer
        # Timer to use a delay before sending MaxSizeChanged signals (so we don't spam libopenshot)
        self.delayed_size = None
        self.delayed_resize_timer = QTimer(self)
        self.delayed_resize_timer.setInterval(100)
        self.delayed_resize_timer.setSingleShot(True)
        self.delayed_resize_timer.timeout.connect(self.delayed_resize_callback)
