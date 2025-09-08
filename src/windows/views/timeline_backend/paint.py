"""Painter classes for the QWidget timeline backend."""

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QPainter,
    QColor,
    QPen,
    QBrush,
    QPixmap,
    QFont,
    QFontMetrics,
    QImage,
    QPainterPath,
    QLinearGradient,
)
import os
import re
from PyQt5.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene
from classes.app import get_app
from classes.time_parts import secondsToTime
from classes.thumbnail import GetThumbPath


class BasePainter:
    def __init__(self, widget):
        self.w = widget
        self.update_theme()

    def update_theme(self):
        pass


class BackgroundPainter(BasePainter):
    def paint(self, painter: QPainter, rect: QRectF):
        bg = self.w.theme.background
        bg2 = getattr(self.w.theme, "background2", QColor())
        if bg2.isValid() and bg2 != bg:
            grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            grad.setColorAt(0, bg)
            grad.setColorAt(1, bg2)
            painter.fillRect(rect, QBrush(grad))
        else:
            painter.fillRect(rect, bg)


class ClipPainter(BasePainter):
    def update_theme(self):
        bw = self.w.theme.clip.border_width
        self.clip_pen = QPen(QBrush(self.w.theme.clip.border_color), bw)
        self.clip_pen.setCosmetic(True)
        self.sel_pen = QPen(QBrush(self.w.theme.clip_selected), bw)
        self.sel_pen.setCosmetic(True)
        self.menu_pix = None
        if self.w.theme.menu_icon:
            size = self.w.theme.menu_size or self.w.theme.menu_icon.width()
            self.menu_pix = self.w.theme.menu_icon.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        self.thumb_cache = {}
        self.effect_cache = {}
        self.menu_margin = self.w.theme.menu_margin

    def paint(self, painter: QPainter):
        for rect, clip in self.w.geometry.clip_rects:
            self._draw_clip(painter, rect, clip, self.clip_pen)

        for rect, clip in self.w.geometry.selected_rects:
            self._draw_clip(painter, rect, clip, self.sel_pen)

    def _thumb(self, clip):
        if clip.id in self.thumb_cache:
            return self.thumb_cache[clip.id]
        file_id = clip.data.get("file_id")
        if not file_id:
            return None
        fps = self.w.fps_float
        frame = int(clip.data.get("start", 0) * fps) + 1
        try:
            path = GetThumbPath(file_id, frame)
            pix = QPixmap(path)
        except Exception:
            pix = QPixmap()
        self.thumb_cache[clip.id] = pix
        return pix

    def _effect_icon(self, eff):
        name = (
            eff.get("name")
            or eff.get("effect")
            or eff.get("type")
            or ""
        )
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if not key:
            return QPixmap()
        if key in self.effect_cache:
            return self.effect_cache[key]
        path = os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "..",
                "effects",
                "icons",
                f"{key}.png",
            )
        )
        pix = QPixmap(path) if os.path.exists(path) else QPixmap()
        self.effect_cache[key] = pix
        return pix

    def _draw_clip(self, painter, rect, clip, pen):
        blur = self.w.theme.clip.shadow_blur
        shadow_col = self.w.theme.clip.shadow_color
        if blur and shadow_col.isValid():
            img_rect = rect.adjusted(-blur, -blur, blur, blur)
            img = QImage(int(img_rect.width()), int(img_rect.height()), QImage.Format_ARGB32_Premultiplied)
            img.fill(0)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing, True)
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(blur, blur, rect.width(), rect.height()),
                self.w.theme.clip.border_radius,
                self.w.theme.clip.border_radius,
            )
            p.fillPath(path, shadow_col)
            p.end()

            effect = QGraphicsBlurEffect()
            effect.setBlurRadius(float(blur))
            scene = QGraphicsScene()
            item = QGraphicsPixmapItem(QPixmap.fromImage(img))
            item.setGraphicsEffect(effect)
            scene.addItem(item)
            blurred = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
            blurred.fill(0)
            p = QPainter(blurred)
            scene.render(p, QRectF(), QRectF(0, 0, img.width(), img.height()))
            p.end()
            painter.drawImage(img_rect.topLeft(), blurred)

        bg = self.w.theme.clip.background
        bg2 = self.w.theme.clip.background2
        if bg2.isValid() and bg2 != bg:
            grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            grad.setColorAt(0, bg)
            grad.setColorAt(1, bg2)
            painter.fillRect(rect, QBrush(grad))
        elif bg.isValid():
            painter.fillRect(rect, bg)

        if pen.color().isValid() and pen.widthF() > 0:
            painter.setPen(pen)
            painter.drawRoundedRect(
                rect, self.w.theme.clip.border_radius, self.w.theme.clip.border_radius
            )

        bw = pen.widthF()
        inner = rect.adjusted(bw, bw, -bw, -bw)
        painter.save()
        painter.setClipRect(inner)

        x = inner.x() + self.menu_margin
        right = inner.right() - self.menu_margin

        thumb = self._thumb(clip)
        thumb_w = self.w.theme.clip.thumb_width
        thumb_h = self.w.theme.clip.thumb_height
        scaled = None
        used_w = 0
        if thumb and not thumb.isNull() and thumb_w and thumb_h:
            scaled = thumb.scaled(
                thumb_w, thumb_h, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            if x + scaled.width() <= right:
                painter.drawPixmap(
                    QPointF(x, inner.y() + (inner.height() - scaled.height()) / 2),
                    scaled,
                )
                used_w = scaled.width()

        if self.menu_pix:
            painter.drawPixmap(
                QPointF(x, inner.y() + self.menu_margin),
                self.menu_pix,
            )
            used_w = max(used_w, self.menu_pix.width())

        x += used_w
        if used_w:
            x += self.menu_margin

        icon_size = min(self.w.theme.clip.font_size or 12, int(inner.height()))
        for eff in clip.data.get("effects", []):
            icon = self._effect_icon(eff)
            if icon.isNull():
                continue
            pix = icon
            if icon_size:
                pix = icon.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if x + pix.width() > right:
                break
            painter.drawPixmap(
                QPointF(x, inner.y() + (inner.height() - pix.height()) / 2),
                pix,
            )
            x += pix.width() + self.menu_margin

        text_width = right - x
        if text_width > 4:
            painter.setPen(self.w.theme.clip.font_color)
            text_rect = QRectF(x, inner.y(), text_width, inner.height())
            metrics = QFontMetrics(painter.font())
            title = metrics.elidedText(
                clip.data.get("title", ""), Qt.ElideRight, int(text_width - 4)
            )
            painter.drawText(
                text_rect.adjusted(2, 2, -2, -2),
                self.w._clip_text_flags,
                title,
            )

        painter.restore()


class TransitionPainter(BasePainter):
    def update_theme(self):
        self.col = self.w.theme.transition.background
        self.col2 = self.w.theme.transition.background2
        self.pen = QPen(QBrush(self.w.theme.transition.border_color), 1.5)
        self.pen.setCosmetic(True)
        self.img = self.w.theme.transition.background_image
        self.sel_pen = QPen(QBrush(self.w.theme.clip_selected), 1.5)
        self.sel_pen.setCosmetic(True)
        self.menu_pix = None
        if self.w.theme.menu_icon:
            size = self.w.theme.menu_size or self.w.theme.menu_icon.width()
            self.menu_pix = self.w.theme.menu_icon.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        self.menu_margin = self.w.theme.menu_margin

    def paint(self, painter: QPainter):
        for rect, _ in self.w.geometry.transition_rects:
            if self.col2.isValid() and self.col2 != self.col:
                grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                grad.setColorAt(0, self.col)
                grad.setColorAt(1, self.col2)
                painter.fillRect(rect, QBrush(grad))
            else:
                painter.fillRect(rect, self.col)
            if self.img:
                w = int(rect.width())
                h = int(rect.height())
                scaled = self.img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                painter.drawPixmap(rect.toRect(), scaled)
            painter.setPen(self.pen)
            painter.drawRoundedRect(rect, self.w.theme.transition.border_radius, self.w.theme.transition.border_radius)
            if self.menu_pix:
                bw = self.pen.widthF()
                inner = rect.adjusted(bw, bw, -bw, -bw)
                painter.drawPixmap(
                    QPointF(inner.x() + self.menu_margin, inner.y() + self.menu_margin),
                    self.menu_pix,
                )

        for rect, _ in self.w.geometry.selected_transitions:
            if self.col2.isValid() and self.col2 != self.col:
                grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                grad.setColorAt(0, self.col)
                grad.setColorAt(1, self.col2)
                painter.fillRect(rect, QBrush(grad))
            else:
                painter.fillRect(rect, self.col)
            if self.img:
                w = int(rect.width())
                h = int(rect.height())
                scaled = self.img.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                painter.drawPixmap(rect.toRect(), scaled)
            painter.setPen(self.sel_pen)
            painter.drawRoundedRect(rect, self.w.theme.transition.border_radius, self.w.theme.transition.border_radius)
            if self.menu_pix:
                bw = self.sel_pen.widthF()
                inner = rect.adjusted(bw, bw, -bw, -bw)
                painter.drawPixmap(
                    QPointF(inner.x() + self.menu_margin, inner.y() + self.menu_margin),
                    self.menu_pix,
                )


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
        self.line_brush = QBrush(col)
        self.line_width = float(self.w.theme.playhead_width)
        self.pen = QPen(self.line_brush, self.line_width)
        self.pen.setCosmetic(True)
        self.icon_pix = None
        if self.w.theme.playhead_icon:
            w = self.w.theme.playhead_icon_width or self.w.theme.playhead_icon.width()
            h = self.w.theme.playhead_icon_height or self.w.theme.playhead_icon.height()
            self.icon_pix = self.w.theme.playhead_icon.scaled(
                w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        self.icon_offset_x = self.w.theme.playhead_icon_offset_x
        self.icon_offset_y = self.w.theme.playhead_icon_offset_y

    def paint(self, painter: QPainter):
        x = self.w.track_name_width + (
            self.w.current_frame / self.w.fps_float) * self.w.pixels_per_second
        painter.setRenderHint(QPainter.Antialiasing, False)
        ix = int(round(x))

        top = self.icon_offset_y
        if self.icon_pix:
            top += self.icon_pix.height() / 2

        self.w.geometry.ensure()
        bottom = self.w.height()
        if self.w.geometry.track_rects:
            bottom = self.w.geometry.track_rects[-1][0].bottom()

        painter.fillRect(
            QRectF(ix - self.line_width / 2, top, self.line_width, bottom - top),
            self.line_brush,
        )

        if self.icon_pix:
            painter.drawPixmap(
                QPointF(ix + self.icon_offset_x, self.icon_offset_y),
                self.icon_pix,
            )


class RulerPainter(BasePainter):
    def update_theme(self):
        self.bg = self.w.theme.ruler.background
        self.bg2 = self.w.theme.ruler.background2
        self.name_bg = (
            self.w.theme.ruler_name_background
            if self.w.theme.ruler_name_background.isValid()
            else self.w.theme.track.name_background
        )
        self.name_bg2 = (
            self.w.theme.ruler_name_background2
            if self.w.theme.ruler_name_background2.isValid()
            else self.name_bg
        )
        self.tick_pen = QPen(self.w.theme.ruler.border_color)
        self.tick_pen.setCosmetic(True)
        self.text_pen = QPen(self.w.theme.ruler.font_color)
        self.tick_font = QFont()
        if self.w.theme.ruler.font_size:
            self.tick_font.setPointSize(self.w.theme.ruler.font_size)
        self.play_font = QFont()
        if self.w.theme.ruler_time_font_size:
            self.play_font.setPointSize(self.w.theme.ruler_time_font_size)
        self.label_top = self.w.theme.ruler_label_top
        self.pad_left = self.w.theme.ruler_time_pad_left
        self.pad_top = self.w.theme.ruler_time_pad_top

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
        if self.bg2.isValid() and self.bg != self.bg2:
            grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            grad.setColorAt(0, self.bg)
            grad.setColorAt(1, self.bg2)
            painter.fillRect(rect, QBrush(grad))
        elif self.bg.isValid():
            painter.fillRect(rect, self.bg)
        left_rect = QRectF(0, 0, self.w.track_name_width, self.w.ruler_height)
        if self.name_bg2 != self.name_bg:
            grad = QLinearGradient(left_rect.topLeft(), left_rect.bottomLeft())
            grad.setColorAt(0, self.name_bg)
            grad.setColorAt(1, self.name_bg2)
            painter.fillRect(left_rect, QBrush(grad))
        painter.setPen(self.text_pen)
        painter.setFont(self.play_font)
        tt = secondsToTime(
            self.w.current_frame / fps_float,
            fps_info["num"],
            fps_info["den"],
        )
        play_lbl = f"{tt['hour']}:{tt['min']}:{tt['sec']},{tt['frame']}"
        painter.drawText(
            left_rect.adjusted(self.pad_left, self.pad_top, -2, -2),
            Qt.AlignLeft | Qt.AlignTop,
            play_lbl,
        )
        base_y = self.w.ruler_height
        tick_metrics = QFontMetrics(self.tick_font)
        label_top = max(0, self.label_top - 2)
        long_ht = base_y - (label_top + tick_metrics.height()) - 2
        short_ht = long_ht / 2
        painter.setPen(self.tick_pen)

        fpt = self._frames_per_tick(pps, fps_float)
        frame = 0
        end_frame = int(duration * fps_float)
        while frame <= end_frame:
            t = frame / fps_float
            x = self.w.track_name_width + t * pps
            ht = long_ht if frame % (fpt * 2) == 0 else short_ht

            painter.drawLine(QPointF(x, base_y), QPointF(x, base_y - ht))

            if frame % (fpt * 2) == 0:
                tt = secondsToTime(t, fps_info["num"], fps_info["den"])
                if frame == 0:
                    lbl = f"{int(tt['min'])}:{tt['sec']}"
                    text_w = tick_metrics.width(lbl)
                    text_rect = QRectF(
                        x + 2,
                        label_top,
                        text_w,
                        tick_metrics.height(),
                    )
                    align = Qt.AlignLeft | Qt.AlignTop
                else:
                    lbl = f"{tt['hour']}:{tt['min']}:{tt['sec']}"
                    if fpt < round(fps_float):
                        lbl += f",{tt['frame']}"
                    text_w = tick_metrics.width(lbl)
                    text_rect = QRectF(
                        x - text_w / 2,
                        label_top,
                        text_w,
                        tick_metrics.height(),
                    )
                    align = Qt.AlignCenter | Qt.AlignTop
                painter.setPen(self.text_pen)
                painter.setFont(self.tick_font)
                painter.drawText(text_rect, align, lbl)
                painter.setPen(self.tick_pen)
            frame += fpt


class TrackPainter(BasePainter):
    def update_theme(self):
        self.border_pen = QPen(self.w.theme.track.border_color)
        self.border_pen.setCosmetic(True)
        self.name_border_color = self.w.theme.track.name_border_color
        self.name_border_width = self.w.theme.track.name_border_width
        self.name_border_top_color = self.w.theme.track.name_border_top_color
        self.name_border_top_width = self.w.theme.track.name_border_top_width
        self.name_border_bottom_color = self.w.theme.track.name_border_bottom_color
        self.name_border_bottom_width = self.w.theme.track.name_border_bottom_width
        self.name_radius_tl = self.w.theme.track.name_radius_tl
        self.name_radius_bl = self.w.theme.track.name_radius_bl
        self.menu_pix = None
        if self.w.theme.menu_icon:
            size = self.w.theme.menu_size or self.w.theme.menu_icon.width()
            self.menu_pix = self.w.theme.menu_icon.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        self.menu_margin = self.w.theme.menu_margin

    def paint(self, painter: QPainter):
        for track_rect, track, name_rect in self.w.geometry.track_rects:
            # Fill track and name backgrounds
            bg = self.w.theme.track.background
            bg2 = self.w.theme.track.background2
            if bg2.isValid() and bg2 != bg:
                grad = QLinearGradient(track_rect.topLeft(), track_rect.bottomLeft())
                grad.setColorAt(0, bg)
                grad.setColorAt(1, bg2)
                painter.fillRect(track_rect, QBrush(grad))
            else:
                painter.fillRect(track_rect, bg)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.w.theme.track.name_background)
            if self.name_radius_tl or self.name_radius_bl:
                r = name_rect
                path = QPainterPath()
                path.moveTo(r.x() + self.name_radius_tl, r.y())
                path.lineTo(r.right(), r.y())
                path.lineTo(r.right(), r.bottom())
                path.lineTo(r.x() + self.name_radius_bl, r.bottom())
                if self.name_radius_bl:
                    path.quadTo(r.x(), r.bottom(), r.x(), r.bottom() - self.name_radius_bl)
                else:
                    path.lineTo(r.x(), r.bottom())
                if self.name_radius_tl:
                    path.lineTo(r.x(), r.y() + self.name_radius_tl)
                    path.quadTo(r.x(), r.y(), r.x() + self.name_radius_tl, r.y())
                else:
                    path.lineTo(r.x(), r.y())
                path.closeSubpath()
                painter.drawPath(path)
            else:
                painter.drawRect(name_rect)
            painter.setBrush(Qt.NoBrush)

            # Draw track border lines (top, bottom, right)
            painter.setPen(self.border_pen)
            painter.drawLine(track_rect.topLeft(), track_rect.topRight())
            painter.drawLine(track_rect.bottomLeft(), track_rect.bottomRight())
            painter.drawLine(track_rect.topRight(), track_rect.bottomRight())

            # Draw borders on track name
            if self.name_border_top_width:
                top_rect = QRectF(
                    name_rect.x(),
                    name_rect.y(),
                    name_rect.width(),
                    self.name_border_top_width,
                )
                painter.fillRect(top_rect, self.name_border_top_color)
            if self.name_border_bottom_width:
                bottom_rect = QRectF(
                    name_rect.x(),
                    name_rect.bottom() - self.name_border_bottom_width,
                    name_rect.width(),
                    self.name_border_bottom_width,
                )
                painter.fillRect(bottom_rect, self.name_border_bottom_color)
            if self.name_border_width:
                left_rect = QRectF(
                    name_rect.x(),
                    name_rect.y(),
                    self.name_border_width,
                    name_rect.height(),
                )
                painter.fillRect(left_rect, self.name_border_color)

            # Track menu icon
            if self.menu_pix:
                painter.drawPixmap(
                    QPointF(
                        name_rect.x() + self.name_border_width + self.menu_margin,
                        name_rect.y() + self.menu_margin,
                    ),
                    self.menu_pix,
                )

            text_offset = self.name_border_width + self.menu_margin * 2 + (
                self.menu_pix.width() if self.menu_pix else 0
            )
            painter.setPen(self.w.theme.track.font_color)
            painter.drawText(
                name_rect.adjusted(text_offset, self.menu_margin, -4, 0),
                Qt.AlignLeft | Qt.AlignTop,
                track.data.get("name", f"Track {track.data.get('number')}")
            )

        # Right-side resize handle
        painter.fillRect(self.w.resize_handle_rect, self.w.theme.track.border_color)


class SelectionPainter(BasePainter):
    def update_theme(self):
        bw = self.w.theme.selection_border_width
        col = (
            self.w.theme.selection_border
            if self.w.theme.selection_border.isValid()
            else self.w.theme.selection
        )
        self.pen = QPen(col, bw, Qt.SolidLine)
        self.pen.setCosmetic(True)

    def paint(self, painter: QPainter):
        if not self.w.selection_rect.isNull():
            if self.w.theme.selection.isValid():
                painter.fillRect(self.w.selection_rect, self.w.theme.selection)
            if self.pen.color().isValid() and self.pen.widthF() > 0:
                painter.setPen(self.pen)
                painter.drawRect(self.w.selection_rect)
