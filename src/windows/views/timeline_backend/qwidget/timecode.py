"""
 @file
 @brief Editable timecode field for the QWidget timeline ruler.
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

from qt_api import QColor, QLineEdit, Qt, pyqtSignal

from classes.time_parts import secondsToTime


def _color_css(color, fallback):
    col = QColor(color)
    if not col.isValid():
        col = QColor(fallback)
    return "rgba(%d, %d, %d, %.3f)" % (
        col.red(),
        col.green(),
        col.blue(),
        col.alpha() / 255.0,
    )


class TimecodeLineEdit(QLineEdit):
    """Small in-place timecode editor for the ruler playhead label."""

    frameCommitted = pyqtSignal(int, bool, bool)
    editCanceled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fps_num = 30
        self._fps_den = 1
        self._current_frame = 1
        self._committing_focus_out = False
        self.setFrame(False)
        self.setObjectName("timelinePlayheadTimeEdit")
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setMaxLength(11)
        self.hide()

    def set_context(self, fps_num, fps_den, current_frame):
        self._fps_num = int(fps_num or 30)
        self._fps_den = int(fps_den or 1) or 1
        self._current_frame = max(1, int(current_frame or 1))

    def set_current_frame_text(self, frame):
        self._current_frame = max(1, int(frame or 1))
        self.setText(self.format_frame(self._current_frame))

    def apply_timeline_theme(self, theme, font, pad_left, pad_top):
        self.setFont(font)
        bg = theme.ruler_name_background
        if not bg.isValid():
            bg = theme.track.name_background
        fg = theme.ruler.font_color
        if not fg.isValid():
            fg = QColor("#ffffff")
        self.setTextMargins(max(0, int(pad_left)), 0, 2, 0)
        self.setStyleSheet(
            "QLineEdit#timelinePlayheadTimeEdit {"
            "background: %s;"
            "color: %s;"
            "border: 0px;"
            "border-radius: 0px;"
            "padding: 0px;"
            "}"
            % (
                _color_css(bg, "#000000"),
                _color_css(fg, "#ffffff"),
            )
        )

    def fps_float(self):
        if not self._fps_den:
            return 0.0
        return float(self._fps_num) / float(self._fps_den)

    def fps_frames(self):
        return max(1, int(self.fps_float() or 1.0))

    def format_frame(self, frame):
        fps = self.fps_float()
        seconds = 0.0
        if fps:
            seconds = max(0.0, (max(1, int(frame)) - 1) / fps)
        tt = secondsToTime(seconds, self._fps_num, self._fps_den)
        return "%s:%s:%s,%s" % (tt["hour"], tt["min"], tt["sec"], tt["frame"])

    def parse_text(self, text=None):
        raw = (self.text() if text is None else text).strip()
        if raw in ("", "0"):
            return 1
        raw = raw.replace(",", ":")
        parts = raw.split(":")
        if len(parts) != 4 or not all(part.strip().isdigit() for part in parts):
            return None
        hours, minutes, seconds, frames = [int(part.strip()) for part in parts]
        if minutes > 59 or seconds > 59 or frames >= self.fps_frames():
            return None
        fps = self.fps_float()
        if fps <= 0.0:
            return 1
        total_seconds = (hours * 3600) + (minutes * 60) + seconds
        return max(1, int(round(total_seconds * fps)) + frames + 1)

    def commit(self, start_preroll=True, force=True):
        frame = self.parse_text()
        if frame is None:
            self.editCanceled.emit()
            return False
        self.set_current_frame_text(frame)
        self.frameCommitted.emit(int(frame), bool(start_preroll), bool(force))
        return True

    def _segment_for_pos(self, pos):
        if pos <= 2:
            return "hour"
        if pos <= 5:
            return "minute"
        if pos <= 8:
            return "second"
        return "frame"

    def _segment_span(self, segment):
        return {
            "hour": (0, 2),
            "minute": (3, 2),
            "second": (6, 2),
            "frame": (9, 2),
        }.get(segment, (9, 2))

    def _step_segment(self, direction, modifiers):
        frame = self.parse_text()
        if frame is None:
            frame = self._current_frame

        segment = self._segment_for_pos(self.cursorPosition())
        fps = self.fps_float()
        fps_frames = self.fps_frames()
        if segment == "hour":
            delta = int(round(fps * 3600.0))
        elif segment == "minute":
            delta = int(round(fps * 60.0))
        elif segment == "second":
            delta = int(round(fps))
        else:
            delta = 1

        if modifiers & Qt.ShiftModifier:
            delta *= 10
        elif modifiers & Qt.ControlModifier and segment == "frame":
            delta = fps_frames

        frame = max(1, frame + (int(direction) * max(1, delta)))
        self.set_current_frame_text(frame)
        start, length = self._segment_span(segment)
        self.setSelection(start, length)
        self.frameCommitted.emit(int(frame), False, False)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if self.commit(start_preroll=True, force=True):
                self._committing_focus_out = True
                try:
                    self.clearFocus()
                    self.hide()
                finally:
                    self._committing_focus_out = False
            event.accept()
            return
        if key == Qt.Key_Escape:
            self.editCanceled.emit()
            event.accept()
            return
        if key in (Qt.Key_Up, Qt.Key_Down):
            self._step_segment(1 if key == Qt.Key_Up else -1, event.modifiers())
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        if self.isVisible() and not self._committing_focus_out:
            self._committing_focus_out = True
            try:
                self.commit(start_preroll=True, force=True)
            finally:
                self._committing_focus_out = False
        super().focusOutEvent(event)
