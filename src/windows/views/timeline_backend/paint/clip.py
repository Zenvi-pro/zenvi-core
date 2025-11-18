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
from classes import info

from .base import BasePainter


class ClipPainter(BasePainter):
    MAX_THUMB_SLOTS = 150

    def update_theme(self):
        bw = float(self.w.theme.clip.border_width or 0.0)
        self.border_width = bw
        self.border_radius = float(self.w.theme.clip.border_radius or 0.0)
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
        self._thumb_pending = {}
        self._thumb_regions = {}
        min_visible = float(getattr(self.w.theme.clip, "thumb_min_visible", 5.0) or 5.0)
        clip_min = float(getattr(self.w.theme.clip, "thumb_clip_min_width", 24.0) or 24.0)
        self._min_thumb_slot_width = max(6.0, min_visible)
        self._min_clip_thumb_width = max(min_visible * 2.0, clip_min)
        # Cache of fully rendered clip pixmaps keyed by clip id/size/pen color
        self.clip_cache = {}
        self.menu_margin = self.w.theme.menu_margin

    def clear_cache(self):
        """Clear cached rendered clip pixmaps."""
        self.clip_cache.clear()
        self.thumb_cache.clear()
        self._thumb_pending.clear()
        self._thumb_regions.clear()

    def _segment_overdraw(self, view_width):
        """Return the horizontal overdraw (extra pixels) to render beyond the view."""

        blur = max(0.0, float(self.w.theme.clip.shadow_blur or 0.0))
        base = max(64.0, view_width * 0.25)
        return max(base, blur * 3.0)

    def paint(self, painter: QPainter):
        area = QRectF(
            self.w.track_name_width,
            self.w.ruler_height,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        overdraw = self._segment_overdraw(area.width())
        expanded = QRectF(
            area.left() - overdraw,
            area.top(),
            area.width() + (overdraw * 2.0),
            area.height(),
        )

        self.w._effect_icon_rects = []
        painter.save()
        painter.setClipRect(area)
        for rect, clip, selected in self.w.geometry.iter_clips():
            if not rect.intersects(expanded):
                continue

            segment_left = max(rect.left(), expanded.left())
            segment_right = min(rect.right(), expanded.right())
            if segment_right <= segment_left:
                continue

            segment_rect = QRectF(
                segment_left,
                rect.top(),
                segment_right - segment_left,
                rect.height(),
            )

            pen = self.sel_pen if selected else self.clip_pen
            self._draw_clip(painter, rect, segment_rect, clip, pen, selected)
        painter.restore()

    @staticmethod
    def _to_float(value, fallback=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _clip_key(self, clip):
        clip_id = getattr(clip, "id", None)
        data = clip.data if isinstance(clip.data, dict) else {}
        start = self._to_float(data.get("start"), 0.0)
        end = self._to_float(data.get("end"), start)
        trim_token = f"{start:.4f}:{end:.4f}"
        return f"{clip_id}:{trim_token}"

    def _clip_file_id(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        file_id = data.get("file_id")
        return str(file_id) if file_id else None

    def _clip_time_bounds(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        start = self._to_float(data.get("start"), 0.0)
        end = self._to_float(data.get("end"), start)
        if end < start:
            end = start
        return start, max(0.0, end - start)

    def _existing_thumb_path(self, file_id, frame):
        subdir = os.path.join(info.THUMBNAIL_PATH, file_id)
        candidates = [
            os.path.join(subdir, f"{frame}.png"),
        ]
        if frame == 1:
            candidates.append(os.path.join(info.THUMBNAIL_PATH, f"{file_id}.png"))
        else:
            candidates.append(os.path.join(info.THUMBNAIL_PATH, f"{file_id}-{frame}.png"))

        for path in candidates:
            if path and os.path.exists(path):
                return path
        return ""

    def _frame_for_offset(self, clip_start, offset, fps):
        fps = float(fps or 0.0)
        if fps <= 0.0:
            fps = 24.0
        seconds = max(0.0, clip_start + max(0.0, offset))
        frame = int(seconds * fps) + 1
        return max(1, frame)

    def _segment_timing(self, segment, clip_duration):
        segment = segment or {}
        offset = self._to_float(segment.get("offset_seconds"), 0.0)
        duration = self._to_float(segment.get("duration_seconds"), clip_duration)
        includes_start = segment.get("includes_start", True)
        includes_end = segment.get("includes_end", True)
        return {
            "offset": max(0.0, offset),
            "duration": max(0.0, duration),
            "includes_start": bool(includes_start),
            "includes_end": bool(includes_end),
        }

    def _clip_pixmap(self, full_rect, segment_rect, clip):
        """Return cached pixmap for the visible portion of a clip."""

        w = int(segment_rect.width())
        h = int(segment_rect.height())
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

        clip_width = max(float(full_rect.width()), 0.0)
        offset_px = max(0.0, float(segment_rect.left() - full_rect.left()))
        offset_seconds = 0.0
        duration_seconds = 0.0
        clip_duration_seconds = 0.0
        if self.w.pixels_per_second > 0.0:
            offset_seconds = offset_px / float(self.w.pixels_per_second)
            duration_seconds = segment_rect.width() / float(self.w.pixels_per_second)
            clip_duration_seconds = clip_width / float(self.w.pixels_per_second)

        includes_start = offset_px <= 0.5
        includes_end = (segment_rect.right() + 0.5) >= full_rect.right()

        segment_info = {
            "offset_px": offset_px,
            "segment_width": float(segment_rect.width()),
            "clip_width": clip_width,
            "includes_start": includes_start,
            "includes_end": includes_end,
            "offset_seconds": offset_seconds,
            "duration_seconds": duration_seconds,
            "clip_duration": clip_duration_seconds,
        }

        use_cache = not self.w.clip_has_pending_override(clip)
        waveform_token = self.w.clip_waveform_cache_token(clip) if use_cache else None
        key = (
            clip.id,
            w,
            h,
            waveform_token,
            round(ratio, 4),
            round(offset_seconds, 4),
            round(duration_seconds, 4),
            includes_start,
            includes_end,
        ) if use_cache else None
        if use_cache and key in self.clip_cache:
            cached = self.clip_cache[key]
            if isinstance(cached, tuple) and len(cached) == 3:
                pix, blur, icons = cached
                cached = (pix, blur, icons, False)
                self.clip_cache[key] = cached
            return cached

        small = w < 20
        tiny = w < 2
        blur = self.w.theme.clip.shadow_blur if not small else 0
        if not includes_start or not includes_end:
            blur = 0
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
        pending_thumbs = False
        if not tiny:
            self._fill_clip_background(painter, inner_rect)
            icon_entries, pending_thumbs = self._draw_clip_contents(
                painter, clip, inner_rect, segment_info
            )

        painter.end()

        pix = QPixmap.fromImage(img)
        if ratio != 1.0:
            pix.setDevicePixelRatio(ratio)
        result = (pix, blur, icon_entries, pending_thumbs)
        if use_cache and key is not None and not pending_thumbs:
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

    def _draw_clip_contents(self, painter, clip, inner_rect, segment):
        bw = float(self.border_width or 0.0)
        inner = inner_rect.adjusted(bw, bw, -bw, -bw)
        painter.save()
        painter.setClipRect(inner)

        left = inner.x() + self.menu_margin
        right = inner.right() - self.menu_margin
        icon_entries = []
        pending_thumbs = False

        has_waveform = self._draw_waveform(painter, clip, inner, segment)

        includes_start = segment.get("includes_start", True) if isinstance(segment, dict) else True

        if not has_waveform:
            pending_thumbs = self._draw_thumbnails(painter, clip, inner, segment)

        content_x = left
        if includes_start:
            menu_width = self._draw_menu_icon(painter, inner, left, 0)
            if menu_width:
                content_x += menu_width + self.menu_margin

            content_x = self._draw_effect_icons(
                painter, clip, inner, content_x, right, icon_entries
            )
            self._draw_clip_text(painter, clip, inner, content_x, right)

        painter.restore()
        return icon_entries, pending_thumbs

    def _build_thumbnail_slots(self, clip, inner, segment, style, timing):
        if style == "none":
            return []

        segment = segment or {}
        visible_width = max(0.0, float(segment.get("segment_width") or inner.width()))
        if visible_width < self._min_clip_thumb_width:
            return []

        clip_width = max(visible_width, float(segment.get("clip_width") or visible_width))
        if clip_width <= 0.0:
            return []

        thumb_w = float(self.w.theme.clip.thumb_width or inner.height())
        thumb_h = float(self.w.theme.clip.thumb_height or inner.height())
        thumb_w = max(self._min_thumb_slot_width, min(thumb_w, clip_width))
        thumb_h = max(self._min_thumb_slot_width, min(thumb_h, inner.height()))
        top = inner.y() + (inner.height() - thumb_h) / 2.0

        offset_px = max(0.0, float(segment.get("offset_px") or 0.0))
        visible_left = offset_px
        visible_right = offset_px + visible_width

        includes_start = timing.get("includes_start", True)
        includes_end = timing.get("includes_end", True)

        positions = []
        added = set()

        def add_position(pos):
            pos = max(0.0, min(pos, clip_width))
            key = round(pos, 2)
            if key in added:
                return
            added.add(key)
            positions.append(pos)

        if style == "start":
            if includes_start:
                add_position(0.0)
        elif style == "start-end":
            if includes_start:
                add_position(0.0)
            if includes_end:
                add_position(max(clip_width - thumb_w, 0.0))
        else:
            step = max(thumb_w, self._min_thumb_slot_width)
            if step <= 0.0:
                step = self._min_thumb_slot_width
            start_clip = max(0.0, visible_left - step)
            pos = (math.floor(start_clip / step) * step) if step else start_clip
            count = 0
            max_visible = visible_right + step
            while pos < clip_width and count < self.MAX_THUMB_SLOTS:
                if pos > max_visible:
                    break
                if pos + thumb_w >= visible_left - step:
                    add_position(pos)
                pos += step
                count += 1
            if not positions and includes_start:
                add_position(0.0)
            elif positions and positions[-1] < max(clip_width - thumb_w, 0.0):
                if count < self.MAX_THUMB_SLOTS:
                    add_position(max(clip_width - thumb_w, 0.0))

        if not positions:
            return []

        slots = []
        view_left = 0.0
        view_right = visible_width
        for clip_pos in positions:
            local_x = clip_pos - offset_px
            if local_x >= view_right or (local_x + thumb_w) <= view_left:
                continue
            rect = QRectF(inner.x() + local_x, top, thumb_w, thumb_h)
            clip_left = max(0.0, min(clip_pos, clip_width))
            slots.append((clip_left, rect))
        return slots

    def _draw_thumbnails(self, painter, clip, inner, segment):
        style = str(getattr(self.w, "thumbnail_style", "entire") or "").strip().lower()
        if style == "none":
            return False
        clip_key = self._clip_key(clip)
        file_id = self._clip_file_id(clip)
        if not (clip_key and file_id):
            return False

        clip_start, clip_duration = self._clip_time_bounds(clip)
        timing = self._segment_timing(segment, clip_duration)
        clip_width = max(float(segment.get("clip_width") or inner.width()), inner.width()) if isinstance(segment, dict) else inner.width()
        slots = self._build_thumbnail_slots(clip, inner, segment, style, timing)
        if not slots:
            return False

        fps = getattr(self.w, "fps_float", 24.0) or 24.0
        clip_duration_seconds = clip_duration if clip_duration > 0.0 else timing.get("duration", 0.0)
        if clip_duration_seconds <= 0.0:
            extra = self._to_float(segment.get("duration_seconds"), 0.0) if isinstance(segment, dict) else 0.0
            clip_duration_seconds = extra if extra > 0.0 else 0.0
        if clip_duration_seconds <= 0.0:
            return False

        seen = set()
        pending = False
        for clip_left, rect in slots:
            if not isinstance(rect, QRectF):
                continue
            clip_left = max(0.0, min(float(clip_left), clip_width))
            offset_seconds = 0.0
            if clip_width > 0.0 and clip_duration_seconds > 0.0:
                offset_seconds = (clip_left / clip_width) * clip_duration_seconds
            frame = self._frame_for_offset(clip_start, offset_seconds, fps)
            if frame <= 0:
                continue
            key = (clip_key, frame)
            if key in seen:
                continue
            seen.add(key)
            pix = self._get_thumbnail_pixmap(clip_key, file_id, frame, rect)
            if pix:
                self._paint_thumbnail_pixmap(painter, pix, rect)
            else:
                pending = True
        return pending

    def _get_thumbnail_pixmap(self, clip_key, file_id, frame, rect):
        key = (clip_key, frame)
        cached = self.thumb_cache.get(key)
        if cached is not None:
            return None if cached.isNull() else cached

        path = self._existing_thumb_path(file_id, frame)
        if path:
            pix = QPixmap(path)
            if not pix.isNull():
                self.thumb_cache[key] = pix
                return pix

        generation = getattr(self.w, "thumbnail_generation", 0)
        if key not in self._thumb_pending or self._thumb_pending[key] != generation:
            self._thumb_pending[key] = generation
            if self.w.thumbnail_manager:
                self.w.thumbnail_manager.request_thumbnail(
                    clip_key, file_id, frame, generation
                )
        self._thumb_regions[key] = QRectF(rect)
        return None

    def _paint_thumbnail_pixmap(self, painter, pixmap, rect):
        if not pixmap or pixmap.isNull() or not isinstance(rect, QRectF):
            return
        width = max(1, int(round(rect.width())))
        height = max(1, int(round(rect.height())))
        scaled = pixmap.scaled(
            width,
            height,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        full_width = float(scaled.width())
        draw_width = min(float(rect.width()), full_width)
        target = QRectF(
            rect.x(),
            rect.y() + (rect.height() - scaled.height()) / 2.0,
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

    def _draw_waveform(self, painter, clip, inner, segment=None):
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

        source_start_ratio = display.get("source_start_ratio", start_ratio)
        source_end_ratio = display.get("source_end_ratio", end_ratio)

        start_float = max(0.0, min(float(samples), float(samples) * start_ratio))
        end_float = max(start_float, min(float(samples), float(samples) * end_ratio))

        span = end_float - start_float
        if span <= 0:
            return False

        if segment and isinstance(segment, dict):
            clip_duration = float(segment.get("clip_duration") or 0.0)
            offset_seconds = float(segment.get("offset_seconds") or 0.0)
            duration_seconds = float(segment.get("duration_seconds") or 0.0)
            total_span = max(float(end_ratio - start_ratio), 0.0)
            source_span = max(float(source_end_ratio - source_start_ratio), 0.0)
            if clip_duration > 0.0 and total_span > 0.0:
                start_frac = max(0.0, min(1.0, offset_seconds / clip_duration))
                end_frac = max(start_frac, min(1.0, (offset_seconds + duration_seconds) / clip_duration))

                adj_start_ratio = start_ratio + total_span * start_frac
                adj_end_ratio = start_ratio + total_span * end_frac
                start_ratio = max(0.0, min(1.0, adj_start_ratio))
                end_ratio = max(start_ratio, min(1.0, adj_end_ratio))

                if source_span > 0.0:
                    adj_source_start = source_start_ratio + source_span * start_frac
                    adj_source_end = source_start_ratio + source_span * end_frac
                    source_start_ratio = max(0.0, min(1.0, adj_source_start))
                    source_end_ratio = max(source_start_ratio, min(1.0, adj_source_end))

                start_float = max(0.0, min(float(samples), float(samples) * start_ratio))
                end_float = max(start_float, min(float(samples), float(samples) * end_ratio))
                span = end_float - start_float
                if span <= 0:
                    return False

        samples_per_pixel = span / float(width)
        if samples_per_pixel <= 0:
            return False

        clip_rect = painter.clipBoundingRect()
        visible_left = 0
        visible_right = width
        if clip_rect.isValid():
            left_offset = int(math.floor(clip_rect.left() - inner.left()))
            right_offset = int(math.ceil(clip_rect.right() - inner.left()))
            visible_left = min(width, max(0, left_offset))
            visible_right = min(width, max(visible_left, right_offset))
        if visible_right <= visible_left:
            return False

        center_y = inner.center().y()
        amplitude_scale = (height * 0.5) * 0.95
        peak_color = self.w.theme.waveform_peak_color
        fill_color = self.w.theme.waveform_color
        if not peak_color.isValid():
            peak_color = QColor(fill_color)
            peak_color.setAlpha(128)
        if not fill_color.isValid():
            fill_color = QColor("#2a82da")

        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setClipRect(inner, Qt.IntersectClip)

        peak_heights = []
        avg_heights = []
        x_positions = []

        for column in range(visible_left, visible_right):
            px_start = start_float + column * samples_per_pixel
            px_end = min(end_float, px_start + samples_per_pixel)
            start_idx = max(0, int(math.floor(px_start)))
            end_idx = min(samples, int(math.ceil(px_end)))
            values = []

            if end_idx <= start_idx:
                idx = min(samples - 1, max(0, int(round(px_start)))) if samples else 0
                if samples:
                    sample = audio_data[idx]
                    values.append(abs(sample) if isinstance(sample, (int, float)) else 0.0)
            else:
                step = max(1, int(math.ceil((end_idx - start_idx) / 20.0)))
                idx = start_idx
                while idx < end_idx:
                    sample = audio_data[idx]
                    values.append(abs(sample) if isinstance(sample, (int, float)) else 0.0)
                    idx += step
                last_idx = end_idx - 1
                if values and (last_idx - start_idx) % step != 0:
                    sample = audio_data[last_idx]
                    values.append(abs(sample) if isinstance(sample, (int, float)) else 0.0)

            if not values:
                peak_heights.append(0.0)
                avg_heights.append(0.0)
                x_positions.append(inner.left() + column + 0.5)
                continue

            max_amp = max(values)
            avg_amp = sum(values) / len(values)
            peak_heights.append(max_amp * amplitude_scale)
            avg_heights.append(avg_amp * amplitude_scale)
            x_positions.append(inner.left() + column + 0.5)

        if x_positions:
            peak_path = QPainterPath()
            peak_path.moveTo(x_positions[0], center_y)
            for x_pos, height_px in zip(x_positions, peak_heights):
                peak_path.lineTo(x_pos, center_y - height_px)
            peak_path.lineTo(x_positions[-1], center_y)
            for x_pos, height_px in zip(reversed(x_positions), reversed(peak_heights)):
                peak_path.lineTo(x_pos, center_y + height_px)
            peak_path.closeSubpath()

            fill_path = QPainterPath()
            fill_path.moveTo(x_positions[0], center_y)
            for x_pos, height_px in zip(x_positions, avg_heights):
                fill_path.lineTo(x_pos, center_y - height_px)
            fill_path.lineTo(x_positions[-1], center_y)
            for x_pos, height_px in zip(reversed(x_positions), reversed(avg_heights)):
                fill_path.lineTo(x_pos, center_y + height_px)
            fill_path.closeSubpath()

            if any(height > 0.0 for height in peak_heights):
                painter.fillPath(peak_path, peak_color)
            if any(height > 0.0 for height in avg_heights):
                painter.fillPath(fill_path, fill_color)

        painter.restore()
        return True


    def _draw_clip(self, painter, full_rect, segment_rect, clip, pen, selected):
        result = self._clip_pixmap(full_rect, segment_rect, clip)
        if not result:
            return
        pix, shadow_spread, icons, _ = result
        if pix:
            offset = QPointF(segment_rect.x() - shadow_spread, segment_rect.y() - shadow_spread)
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
        includes_start = (segment_rect.left() - full_rect.left()) <= 0.5
        includes_end = (full_rect.right() - segment_rect.right()) <= 0.5

        border_pen = self.sel_pen if selected else self.clip_pen
        self._stroke_visible_border(
            painter,
            segment_rect,
            border_pen,
            includes_start=includes_start,
            includes_end=includes_end,
        )

    def _stroke_visible_border(
        self,
        painter,
        segment_rect,
        pen,
        *,
        includes_start=True,
        includes_end=True,
    ):
        if not isinstance(pen, QPen) or not pen.color().isValid():
            return
        if segment_rect.width() <= 0.0 or segment_rect.height() <= 0.0:
            return

        painter.save()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(pen)

        rect = QRectF(segment_rect)
        width_offset = max(pen.widthF(), 1.0) / 2.0
        max_x = max(rect.width() / 2.0 - 0.1, 0.0)
        max_y = max(rect.height() / 2.0 - 0.1, 0.0)
        offset_x = min(width_offset, max_x)
        offset_y = min(width_offset, max_y)
        rect.adjust(offset_x, offset_y, -offset_x, -offset_y)
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            painter.restore()
            return

        radius = 0.0
        if rect.width() >= 20.0 and rect.height() > 0.0:
            radius = min(self.border_radius, min(rect.width(), rect.height()) / 2.0)

        painter.setRenderHint(QPainter.Antialiasing, True)

        if radius > 0.0 and (includes_start or includes_end):
            left = rect.left()
            right = rect.right()
            top = rect.top()
            bottom = rect.bottom()
            path = QPainterPath()

            if includes_start:
                path.moveTo(left, top + radius)
                path.quadTo(left, top, left + radius, top)
            else:
                path.moveTo(left, top)

            if includes_end:
                path.lineTo(right - radius, top)
                path.quadTo(right, top, right, top + radius)
                path.lineTo(right, bottom - radius)
                path.quadTo(right, bottom, right - radius, bottom)
            else:
                path.lineTo(right, top)
                path.lineTo(right, bottom)

            if includes_start:
                path.lineTo(left + radius, bottom)
                path.quadTo(left, bottom, left, bottom - radius)
                path.lineTo(left, top + radius)
            else:
                path.lineTo(left, bottom)
                path.lineTo(left, top)

            path.closeSubpath()
            painter.drawPath(path)
        elif radius > 0.0:
            painter.drawRoundedRect(rect, radius, radius)
        else:
            painter.drawRect(rect)
        painter.restore()

    def expire_thumbnail_requests(self, generation):
        """Remove pending thumbnail entries when the viewport changes."""
        stale_keys = [
            key
            for key, pending_generation in self._thumb_pending.items()
            if pending_generation != generation
        ]
        for key in stale_keys:
            self._thumb_pending.pop(key, None)
            self._thumb_regions.pop(key, None)

    def handle_thumbnail_ready(self, clip_id, frame, thumb_path, generation):
        """Handle a thumbnail becoming available on the background thread."""
        clip_key = str(clip_id or "")
        key = (clip_key, int(frame or 0))
        pending_generation = self._thumb_pending.get(key)
        if pending_generation is None or pending_generation != generation:
            return

        self._thumb_pending.pop(key, None)
        rect = self._thumb_regions.pop(key, None)
        pix = QPixmap()
        if thumb_path and os.path.exists(thumb_path):
            pix = QPixmap(thumb_path)
        self.thumb_cache[key] = pix
        self._invalidate_clip_cache_for_clip(clip_key)

        if isinstance(rect, QRectF) and rect.isValid():
            inflated = QRectF(rect).adjusted(-2.0, -2.0, 2.0, 2.0)
            self.w.update(inflated.toRect())
        else:
            self.w.update()

    def _invalidate_clip_cache_for_clip(self, clip_token):
        """Drop cached clip pixmaps when a thumbnail changes."""
        if not clip_token:
            return
        clip_id = str(clip_token).split(":", 1)[0]
        stale_keys = [
            key
            for key in self.clip_cache.keys()
            if isinstance(key, tuple) and key and str(key[0]) == clip_id
        ]
        for key in stale_keys:
            self.clip_cache.pop(key, None)
