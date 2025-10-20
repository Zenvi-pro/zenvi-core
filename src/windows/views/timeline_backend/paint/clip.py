"""
 @file
 @brief Painter for timeline clips.
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

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt5.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene
import math
import os

from classes.app import get_app
from classes.logger import log
from classes.time_parts import secondsToTime
from classes.thumbnail import GetThumbPath

from .base import BasePainter


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
        # Cache of fully rendered clip pixmaps keyed by clip id/size/pen color
        self.clip_cache = {}
        self.menu_margin = self.w.theme.menu_margin

    def clear_cache(self):
        """Clear cached rendered clip pixmaps."""
        self.clip_cache.clear()

    def paint(self, painter: QPainter):
        area = QRectF(
            self.w.track_name_width,
            self.w.ruler_height,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        self.w._effect_icon_rects = []
        painter.save()
        painter.setClipRect(area)
        for rect, clip, selected in self.w.geometry.iter_clips():
            if not rect.intersects(area):
                continue
            pen = self.sel_pen if selected else self.clip_pen
            self._draw_clip(painter, rect, clip, pen)
        painter.restore()

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

    def _clip_pixmap(self, rect, clip, pen):
        """Return cached pixmap of a clip, rendering if needed."""
        w = int(rect.width())
        h = int(rect.height())
        if w <= 0 or h <= 0:
            return None

        ratio = 1.0
        try:
            ratio = float(self.w.devicePixelRatioF())
        except AttributeError:
            try:
                ratio = float(self.w.devicePixelRatio())
            except AttributeError:
                ratio = 1.0
        if not math.isfinite(ratio) or ratio <= 0.0:
            ratio = 1.0

        use_cache = not self.w.clip_has_pending_override(clip)
        waveform_token = self.w.clip_waveform_cache_token(clip) if use_cache else None
        key = (
            clip.id,
            w,
            h,
            pen.color().rgba(),
            waveform_token,
            round(ratio, 4),
        ) if use_cache else None
        if use_cache and key in self.clip_cache:
            return self.clip_cache[key]

        small = w < 20
        tiny = w < 2
        blur = self.w.theme.clip.shadow_blur if not small else 0
        radius = self.w.theme.clip.border_radius if not small else 0
        shadow_col = self.w.theme.clip.shadow_color if not small else QColor()

        img_w = max(1, int(math.ceil((w + (blur * 2.0)) * ratio)))
        img_h = max(1, int(math.ceil((h + (blur * 2.0)) * ratio)))
        img = QImage(img_w, img_h, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)

        if blur and shadow_col.isValid():
            self._draw_clip_shadow(img, w, h, blur, radius, shadow_col, ratio)

        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if ratio != 1.0:
            painter.scale(ratio, ratio)
        inner_rect = QRectF(blur, blur, w, h)

        icon_entries = []
        if not tiny:
            self._fill_clip_background(painter, inner_rect)
        self._draw_clip_border(painter, pen, inner_rect, radius)

        if not tiny:
            icon_entries = self._draw_clip_contents(painter, clip, inner_rect, pen)

        painter.end()

        pix = QPixmap.fromImage(img)
        if ratio != 1.0:
            pix.setDevicePixelRatio(ratio)
        result = (pix, blur, icon_entries)
        if use_cache and key is not None:
            self.clip_cache[key] = result
        return result

    def _draw_clip_shadow(self, img, w, h, blur, radius, shadow_col, ratio):
        if blur <= 0 or not shadow_col.isValid():
            return

        img_w = img.width()
        img_h = img.height()
        shadow = QImage(img_w, img_h, QImage.Format_ARGB32_Premultiplied)
        shadow.fill(0)

        fill_color = QColor(shadow_col)
        fill_color.setAlpha(int(fill_color.alpha() * 0.7))

        shadow_painter = QPainter(shadow)
        shadow_painter.setRenderHint(QPainter.Antialiasing, True)
        if ratio != 1.0:
            shadow_painter.scale(ratio, ratio)
        path = QPainterPath()
        path.addRoundedRect(QRectF(blur, blur, w, h), radius, radius)
        shadow_painter.fillPath(path, fill_color)
        shadow_painter.end()

        shadow_pix = QPixmap.fromImage(shadow)
        blur_effect = QGraphicsBlurEffect()
        blur_effect.setBlurRadius(max(0.1, float(blur) * ratio))

        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(shadow_pix)
        item.setGraphicsEffect(blur_effect)
        scene.addItem(item)

        blurred = QImage(img_w, img_h, QImage.Format_ARGB32_Premultiplied)
        blurred.fill(0)
        blur_painter = QPainter(blurred)
        scene.render(blur_painter, QRectF(), QRectF(0, 0, img_w, img_h))
        blur_painter.end()

        composite = QPainter(img)
        composite.drawImage(0, 0, blurred)
        composite.end()

    def _fill_clip_background(self, painter, inner_rect):
        bg = self.w.theme.clip.background
        bg2 = self.w.theme.clip.background2
        if bg2.isValid() and bg2 != bg:
            grad = QLinearGradient(inner_rect.topLeft(), inner_rect.bottomLeft())
            grad.setColorAt(0, bg)
            grad.setColorAt(1, bg2)
            painter.fillRect(inner_rect, QBrush(grad))
        elif bg.isValid():
            painter.fillRect(inner_rect, bg)

    def _draw_clip_border(self, painter, pen, inner_rect, radius):
        if not pen.color().isValid() or pen.widthF() <= 0:
            return
        painter.setPen(pen)
        if radius:
            painter.drawRoundedRect(inner_rect, radius, radius)
        else:
            painter.drawRect(inner_rect)

    def _draw_clip_contents(self, painter, clip, inner_rect, pen):
        bw = pen.widthF()
        inner = inner_rect.adjusted(bw, bw, -bw, -bw)
        painter.save()
        painter.setClipRect(inner)

        left = inner.x() + self.menu_margin
        right = inner.right() - self.menu_margin
        icon_entries = []

        has_waveform = self._draw_waveform(painter, clip, inner)

        if not has_waveform:
            self._draw_thumbnail(painter, clip, inner, inner.x(), inner.right())

        menu_width = self._draw_menu_icon(painter, inner, left, 0)
        content_x = left
        if menu_width:
            content_x += menu_width + self.menu_margin

        content_x = self._draw_effect_icons(
            painter, clip, inner, content_x, right, icon_entries
        )
        self._draw_clip_text(painter, clip, inner, content_x, right)

        painter.restore()
        return icon_entries

    def _draw_thumbnail(self, painter, clip, inner, x, right):
        thumb = self._thumb(clip)
        thumb_w = self.w.theme.clip.thumb_width or int(inner.height())
        thumb_h = self.w.theme.clip.thumb_height or int(inner.height())
        if not (thumb and not thumb.isNull() and thumb_w and thumb_h):
            return 0
        scaled = thumb.scaled(
            thumb_w,
            thumb_h,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        available = max(0.0, right - x)
        if available <= 0.0:
            return 0

        min_visible = getattr(self.w.theme.clip, "thumb_min_visible", 5.0) or 0.0
        if available < max(1.0, float(min_visible)):
            return 0

        full_width = float(scaled.width())
        draw_width = min(available, full_width)
        target = QRectF(
            x,
            inner.y() + (inner.height() - scaled.height()) / 2.0,
            draw_width,
            float(scaled.height()),
        )
        source = QRectF(0.0, 0.0, draw_width, float(scaled.height()))
        had_hint = bool(painter.renderHints() & QPainter.SmoothPixmapTransform)
        if not had_hint:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(target, scaled, source)
        if not had_hint:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        return float(draw_width)

    def _draw_menu_icon(self, painter, inner, x, used_width):
        if not self.menu_pix:
            return used_width
        painter.drawPixmap(
            QPointF(x, inner.y() + self.menu_margin),
            self.menu_pix,
        )
        return max(used_width, float(self.menu_pix.width()))

    def _draw_effect_icons(self, painter, clip, inner, x, right, entries):
        effects = clip.data.get("effects", []) if isinstance(clip.data, dict) else []
        if not isinstance(effects, list) or not effects:
            return x

        available_height = max(0.0, inner.height() - (self.menu_margin * 2))
        base_height = min(16.0, available_height or 0.0)
        badge_height = max(11.0, base_height if base_height > 0.0 else 11.0)
        top = inner.y() + self.menu_margin

        original_font = painter.font()
        badge_font = QFont(original_font)
        if badge_font.pointSizeF() > 0:
            badge_font.setPointSizeF(max(7.0, badge_font.pointSizeF() * 0.8))
        metrics = QFontMetrics(badge_font)

        selected_ids = set()
        if hasattr(self.w, "_selected_effect_ids"):
            selected_ids = self.w._selected_effect_ids()

        for eff in effects:
            available = right - x
            if available <= 4:
                break

            label = (
                eff.get("type")
                or eff.get("effect")
                or eff.get("name")
                or eff.get("class_name")
                or "?"
            )
            letter = label.strip()[0].upper() if isinstance(label, str) and label.strip() else "?"

            text_width = metrics.horizontalAdvance(letter)
            badge_width = max(text_width + 6.0, badge_height)
            if badge_width > available:
                break

            rect = QRectF(x, top, badge_width, badge_height)
            color = self.w._effect_color(eff)
            if not isinstance(color, QColor) or not color.isValid():
                color = QColor("#4d7bff")

            effect_id = eff.get("id")
            effect_id_str = str(effect_id) if effect_id is not None else ""
            selected = bool(eff.get("selected")) or (
                effect_id_str and effect_id_str in selected_ids
            )

            fill = QColor(color)
            if selected and fill.isValid():
                fill = fill.lighter(120)
            opacity = 1.0 if selected else 0.7

            border = QColor(223, 223, 223) if selected else QColor(0, 0, 0, 200)
            pen = QPen(border, 1.0)
            pen.setCosmetic(True)

            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setOpacity(opacity)
            painter.setBrush(fill)
            painter.setPen(pen)
            radius = min(badge_height / 2.0, 6.0)
            painter.drawRoundedRect(rect, radius, radius)

            painter.setOpacity(1.0)
            painter.setFont(badge_font)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(rect, Qt.AlignCenter, letter)
            painter.restore()

            entries.append(
                {
                    "rect": QRectF(rect),
                    "effect": eff,
                    "selected": selected,
                    "effect_id": effect_id_str,
                }
            )
            x += rect.width() + self.menu_margin

        painter.setFont(original_font)
        return x

    def _draw_clip_text(self, painter, clip, inner, x, right):
        text_width = right - x
        if text_width <= 4:
            return
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

    def _draw_waveform(self, painter, clip, inner):
        data = clip.data if isinstance(clip.data, dict) else {}
        ui_data = data.get("ui", {}) if isinstance(data, dict) else {}
        audio_data = ui_data.get("audio_data") if isinstance(ui_data, dict) else None
        if not (isinstance(audio_data, list) and len(audio_data) > 1):
            return False

        width = int(inner.width())
        height = int(inner.height())
        if width <= 0 or height <= 0:
            return False

        samples = len(audio_data)
        display = self.w.clip_waveform_window(clip)
        scale_waveform = display.get("scale", False)
        if scale_waveform:
            start_ratio = display.get("source_start_ratio", display.get("start_ratio", 0.0))
            end_ratio = display.get("source_end_ratio", display.get("end_ratio", 1.0))
        else:
            start_ratio = display.get("start_ratio", 0.0)
            end_ratio = display.get("end_ratio", 1.0)

        start_float = max(0.0, min(float(samples), float(samples) * start_ratio))
        end_float = max(start_float, min(float(samples), float(samples) * end_ratio))

        span = end_float - start_float
        if span <= 0:
            return False

        samples_per_pixel = span / float(width)
        if samples_per_pixel <= 0:
            return False

        bottom = inner.bottom() - 1
        scale = height * 0.85
        peak_color = self.w.theme.waveform_peak_color
        fill_color = self.w.theme.waveform_color
        if not peak_color.isValid():
            peak_color = QColor(fill_color)
            peak_color.setAlpha(128)
        if not fill_color.isValid():
            fill_color = QColor("#2a82da")

        painter.save()
        painter.setPen(Qt.NoPen)

        block_width = 1.0
        x = 0.0
        while x < width:
            block = min(block_width, width - x)
            if block <= 0.0:
                break
            px_start = start_float + x * samples_per_pixel
            px_end = min(end_float, start_float + (x + block) * samples_per_pixel)
            start_idx = max(0, int(math.floor(px_start)))
            end_idx = min(samples, int(math.ceil(px_end)))
            if end_idx <= start_idx:
                end_idx = min(start_idx + 1, samples)
            if end_idx <= start_idx:
                x += block
                continue
            max_amp = 0.0
            avg_amp = 0.0
            count = 0
            for idx in range(start_idx, end_idx):
                sample = audio_data[idx]
                val = abs(sample) if isinstance(sample, (int, float)) else 0.0
                if val > max_amp:
                    max_amp = val
                avg_amp += val
                count += 1
            if not count:
                x += block
                continue
            avg_amp /= count
            max_height = max_amp * scale
            avg_height = avg_amp * scale
            left = inner.left() + x
            peak_rect = QRectF(left, bottom - max_height, block, max_height)
            fill_rect = QRectF(left, bottom - avg_height, block, avg_height)
            if max_height > 0.0:
                painter.fillRect(peak_rect, peak_color)
            if avg_height > 0.0:
                painter.fillRect(fill_rect, fill_color)
            x += block

        painter.restore()
        return True


    def _draw_clip(self, painter, rect, clip, pen):
        result = self._clip_pixmap(rect, clip, pen)
        if not result:
            return
        pix, shadow_spread, icons = result
        if pix:
            offset = QPointF(rect.x() - shadow_spread, rect.y() - shadow_spread)
            painter.drawPixmap(offset, pix)
            if icons:
                for entry in icons:
                    rect_local = entry.get("rect") if isinstance(entry, dict) else None
                    effect = entry.get("effect") if isinstance(entry, dict) else None
                    if not isinstance(rect_local, QRectF):
                        continue
                    global_rect = QRectF(rect_local)
                    global_rect.translate(offset.x(), offset.y())
                    self.w._effect_icon_rects.append(
                        {
                            "rect": global_rect,
                            "clip": clip,
                            "effect": effect,
                            "effect_id": entry.get("effect_id"),
                        }
                    )
