"""Painter classes for the QWidget timeline backend."""

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush
from classes.app import get_app
from classes.time_parts import secondsToTime


class BasePainter:
    def __init__(self, widget):
        self.w = widget
        self.update_theme()

    def update_theme(self):
        pass


class BackgroundPainter(BasePainter):
    def paint(self, painter: QPainter, rect: QRectF):
        painter.fillRect(rect, self.w.theme.background)


class ClipPainter(BasePainter):
    def update_theme(self):
        self.clip_pen = QPen(QBrush(self.w.theme.clip.border_color), 1.5)
        self.clip_pen.setCosmetic(True)
        self.sel_pen = QPen(QBrush(self.w.theme.clip_selected), 1.5)
        self.sel_pen.setCosmetic(True)

    def paint(self, painter: QPainter):
        for rect, clip in self.w.geometry.clip_rects:
            painter.fillRect(rect, self.w.theme.clip.background)
            painter.setPen(self.clip_pen)
            painter.drawRoundedRect(rect, self.w.theme.clip.border_radius,
                                     self.w.theme.clip.border_radius)
            painter.setPen(self.w.theme.clip.font_color)
            painter.drawText(rect.adjusted(2, 2, -2, -2),
                             self.w._clip_text_flags,
                             clip.data.get("title", ""))

        for rect, clip in self.w.geometry.selected_rects:
            painter.fillRect(rect, self.w.theme.clip.background)
            painter.setPen(self.sel_pen)
            painter.drawRoundedRect(rect, self.w.theme.clip.border_radius,
                                     self.w.theme.clip.border_radius)
            painter.setPen(self.w.theme.clip.font_color)
            painter.drawText(rect.adjusted(2, 2, -2, -2),
                             self.w._clip_text_flags,
                             clip.data.get("title", ""))


class TransitionPainter(BasePainter):
    def update_theme(self):
        self.col = self.w.theme.transition.background
        self.pen = QPen(QBrush(self.w.theme.transition.border_color), 1.5)
        self.pen.setCosmetic(True)
        self.img = self.w.theme.transition.background_image
        self.sel_pen = QPen(QBrush(self.w.theme.clip_selected), 1.5)
        self.sel_pen.setCosmetic(True)

    def paint(self, painter: QPainter):
        for rect, _ in self.w.geometry.transition_rects:
            painter.fillRect(rect, self.col)
            if self.img:
                w = int(rect.width())
                h = int(rect.height())
                scaled = self.img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                painter.drawPixmap(rect.toRect(), scaled)
            painter.setPen(self.pen)
            painter.drawRoundedRect(rect, self.w.theme.transition.border_radius, self.w.theme.transition.border_radius)

        for rect, _ in self.w.geometry.selected_transitions:
            painter.fillRect(rect, self.col)
            if self.img:
                w = int(rect.width())
                h = int(rect.height())
                scaled = self.img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                painter.drawPixmap(rect.toRect(), scaled)
            painter.setPen(self.sel_pen)
            painter.drawRoundedRect(rect, self.w.theme.transition.border_radius, self.w.theme.transition.border_radius)


class MarkerPainter(BasePainter):
    def update_theme(self):
        self.pen = QPen(QBrush(self.w.theme.ruler.border_color), 1.0)
        self.pen.setCosmetic(True)

    def paint(self, painter: QPainter):
        painter.setPen(self.pen)
        for mr in self.w.geometry.marker_rects:
            painter.drawRect(mr)


class PlayheadPainter(BasePainter):
    def update_theme(self):
        col = QColor(self.w.theme.playhead_color)
        col.setAlphaF(0.5)
        width = self.w.theme.playhead_width
        self.pen = QPen(QBrush(col), float(width))
        self.pen.setCosmetic(True)

    def paint(self, painter: QPainter):
        x = self.w.track_name_width + (
            self.w.current_frame / self.w.fps_float) * self.w.pixels_per_second
        painter.setPen(self.pen)
        painter.drawLine(QPointF(x, 0), QPointF(x, self.w.height()))


class RulerPainter(BasePainter):
    def update_theme(self):
        self.bg = self.w.theme.ruler.background
        self.tick_pen = QPen(self.w.theme.ruler.border_color)
        self.tick_pen.setCosmetic(True)
        self.text_pen = QPen(self.w.theme.ruler.font_color)

    def _prime_factors(self, n: int):
        factors = []
        d = 2
        while d * d <= n:
            while n % d == 0:
                factors.append(d)
                n //= d
            d += 1
        if n > 1:
            factors.append(n)
        return factors

    def _frames_per_tick(self, pps, fps):
        frames = 1
        factors = self._prime_factors(round(fps))
        while (frames / fps) * pps < 40:
            frames *= factors.pop(0) if factors else 2
        return frames

    def paint(self, painter: QPainter):
        proj = get_app().project
        duration = proj.get("duration")
        fps_info = proj.get("fps")
        fps_float = float(fps_info.get("num", 24)) / float(fps_info.get("den", 1) or 1)
        pps = self.w.pixels_per_second
        width = max(1, self.w.width() - self.w.track_name_width)

        rect = QRectF(self.w.track_name_width, 0, width, self.w.ruler_height)
        painter.fillRect(rect, self.bg)

        fpt = self._frames_per_tick(pps, fps_float)
        frame = 0
        end_frame = int(duration * fps_float)
        while frame <= end_frame:
            t = frame / fps_float
            x = self.w.track_name_width + t * pps
            ht = self.w.ruler_height if frame % (fpt * 2) == 0 else self.w.ruler_height / 2

            painter.setPen(self.tick_pen)
            painter.drawLine(QPointF(x, self.w.ruler_height - ht), QPointF(x, self.w.ruler_height))

            if frame % (fpt * 2) == 0:
                tt = secondsToTime(t, fps_info["num"], fps_info["den"])
                lbl = f"{tt['hour']}:{tt['min']}:{tt['sec']}"
                if fpt < round(fps_float):
                    lbl += f",{tt['frame']}"
                painter.setPen(self.text_pen)
                painter.drawText(QRectF(x + 2, 0, 50, self.w.ruler_height - 2),
                                 Qt.AlignLeft | Qt.AlignBottom, lbl)
            frame += fpt


class TrackPainter(BasePainter):
    def update_theme(self):
        self.border_pen = QPen(self.w.theme.track.border_color)
        self.border_pen.setCosmetic(True)

    def paint(self, painter: QPainter):
        for track_rect, track, name_rect in self.w.geometry.track_rects:
            painter.fillRect(track_rect, self.w.theme.track.background)
            painter.setPen(self.border_pen)
            painter.drawRoundedRect(track_rect, self.w.theme.track.border_radius,
                                   self.w.theme.track.border_radius)

            painter.fillRect(name_rect, self.w.theme.track.name_background)
            painter.drawRoundedRect(name_rect, self.w.theme.track.border_radius,
                                   self.w.theme.track.border_radius)
            painter.setPen(self.w.theme.track.font_color)
            painter.drawText(name_rect.adjusted(4, 0, -4, 0),
                             Qt.AlignVCenter | Qt.AlignLeft,
                             track.data.get("name", f"Track {track.data.get('number')}") )
        painter.fillRect(self.w.resize_handle_rect, self.w.theme.track.border_color)


class SelectionPainter(BasePainter):
    def update_theme(self):
        self.pen = QPen(self.w.theme.selection, 1, Qt.DashLine)

    def paint(self, painter: QPainter):
        if not self.w.selection_rect.isNull():
            painter.setPen(self.pen)
            painter.fillRect(self.w.selection_rect, self.w.theme.selection)
            painter.drawRect(self.w.selection_rect)
