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
    QRadialGradient,
)
from PyQt5.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene
import math
from classes.app import get_app
from classes.time_parts import secondsToTime
from classes.thumbnail import GetThumbPath


class BasePainter:
    def __init__(self, widget):
        self.w = widget
        self.update_theme()

    def update_theme(self):
        pass

    def scaled_pixmap(self, pixmap, width, height):
        """Return *pixmap* scaled to the requested logical size."""
        if pixmap is None or pixmap.isNull():
            return pixmap
        try:
            w = int(round(width)) if width else pixmap.width()
            h = int(round(height)) if height else pixmap.height()
        except TypeError:
            w = pixmap.width()
            h = pixmap.height()
        w = max(1, w)
        h = max(1, h)
        return pixmap.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def logical_size(self, pixmap):
        """Return (width, height) of *pixmap* in logical units."""
        if pixmap is None or pixmap.isNull():
            return 0.0, 0.0
        return float(pixmap.width()), float(pixmap.height())


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
        for rect, clip in self.w.geometry.clip_rects:
            if not rect.intersects(area):
                continue
            self._draw_clip(painter, rect, clip, self.clip_pen)

        for rect, clip in self.w.geometry.selected_rects:
            if not rect.intersects(area):
                continue
            self._draw_clip(painter, rect, clip, self.sel_pen)
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
            self.menu_pix = self.scaled_pixmap(self.w.theme.menu_icon, size, size)
        self.menu_margin = self.w.theme.menu_margin
        # Cache of fully rendered transition pixmaps
        self.transition_cache = {}

    def clear_cache(self):
        """Clear cached rendered transition pixmaps."""
        self.transition_cache.clear()

    def paint(self, painter: QPainter):
        area = QRectF(
            self.w.track_name_width,
            self.w.ruler_height,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        painter.save()
        painter.setClipRect(area)
        for rect, _ in self.w.geometry.transition_rects:
            if not rect.intersects(area):
                continue
            pix = self._transition_pixmap(rect, self.pen)
            if pix:
                painter.drawPixmap(rect.topLeft(), pix)

        for rect, _ in self.w.geometry.selected_transitions:
            if not rect.intersects(area):
                continue
            pix = self._transition_pixmap(rect, self.sel_pen)
            if pix:
                painter.drawPixmap(rect.topLeft(), pix)
        painter.restore()

    def _transition_pixmap(self, rect, pen):
        """Return cached pixmap of a transition, rendering if needed."""
        w = int(rect.width())
        h = int(rect.height())
        if w <= 0 or h <= 0:
            return None

        key = (w, h, pen.color().rgba())
        if key in self.transition_cache:
            return self.transition_cache[key]

        small = w < 20
        tiny = w < 2
        radius = self.w.theme.transition.border_radius if not small else 0

        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)

        if not tiny:
            if self.col2.isValid() and self.col2 != self.col:
                grad = QLinearGradient(QPointF(0, 0), QPointF(0, h))
                grad.setColorAt(0, self.col)
                grad.setColorAt(1, self.col2)
                p.fillRect(QRectF(0, 0, w, h), QBrush(grad))
            else:
                p.fillRect(QRectF(0, 0, w, h), self.col)
            if self.img and not small:
                scaled = self.img.scaled(
                    w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
                )
                p.drawPixmap(0, 0, scaled)

        if pen.color().isValid():
            p.setPen(pen)
            if radius:
                p.drawRoundedRect(QRectF(0, 0, w, h), radius, radius)
            else:
                p.drawRect(QRectF(0, 0, w, h))

        if self.menu_pix and not small:
            p.drawPixmap(
                QPointF(self.menu_margin, self.menu_margin),
                self.menu_pix,
            )

        p.end()

        pix = QPixmap.fromImage(img)
        self.transition_cache[key] = pix
        return pix


class MarkerPainter(BasePainter):
    def update_theme(self):
        self.pen = QPen(QBrush(self.w.theme.ruler.border_color), 1.0)
        self.pen.setCosmetic(True)

    def paint(self, painter: QPainter):
        area = QRectF(
            self.w.track_name_width,
            self.w.ruler_height,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        painter.save()
        painter.setPen(self.pen)
        for mr in self.w.geometry.marker_rects:
            vis = mr.intersected(area)
            if vis.isNull():
                continue
            painter.drawRect(vis)
        painter.restore()


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
        offset_px = getattr(self.w, "h_scroll_offset", 0.0)
        frame_seconds = 0.0
        if self.w.fps_float:
            frame_seconds = max(
                0.0, (max(1, self.w.current_frame) - 1) / self.w.fps_float
            )
        x = (
            self.w.track_name_width
            + frame_seconds * self.w.pixels_per_second
            - offset_px
        )
        painter.setRenderHint(QPainter.Antialiasing, False)
        ix = int(round(x))

        if self.icon_pix:
            icon_top = math.floor(self.icon_offset_y)
            icon_w = float(self.icon_pix.width())
            icon_h = float(self.icon_pix.height())
            icon_bottom = icon_top + icon_h
        else:
            icon_top = self.icon_offset_y
            icon_bottom = self.w.ruler_height

        margin_top = float(getattr(self.w, "track_margin_top", 0.0) or 0.0)
        line_top = float(self.w.ruler_height + margin_top)
        top = float(icon_bottom if self.icon_pix else self.w.ruler_height)
        if line_top > top:
            top = line_top

        self.w.geometry.ensure()
        bottom = self.w.height()
        if self.w.geometry.track_rects:
            bottom = self.w.geometry.track_rects[-1][0].bottom()

        timeline_left = self.w.track_name_width
        timeline_width = (
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness
        )
        visible = QRectF(
            timeline_left,
            top,
            max(0.0, timeline_width),
            bottom - top,
        )
        line_rect = QRectF(ix - self.line_width / 2, top, self.line_width, bottom - top)
        intersected = line_rect.intersected(visible)
        if not intersected.isNull():
            painter.fillRect(intersected, self.line_brush)

        if self.icon_pix:
            icon_pos = QPointF(ix + self.icon_offset_x, icon_top)
            icon_w = float(self.icon_pix.width())
            icon_h = float(self.icon_pix.height())
            icon_rect = QRectF(icon_pos.x(), icon_pos.y(), icon_w, icon_h)
            icon_visible = icon_rect.intersected(
                QRectF(
                    timeline_left,
                    icon_rect.y(),
                    max(0.0, timeline_width),
                    icon_rect.height(),
                )
            )
            if not icon_visible.isNull():
                # Adjust source rect when partially clipped by the track labels.
                dx = icon_visible.x() - icon_rect.x()
                source_rect = QRectF(
                    max(0.0, dx),
                    0.0,
                    icon_visible.width(),
                    icon_visible.height(),
                )
                painter.drawPixmap(icon_visible.topLeft(), self.icon_pix, source_rect)


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
        self._last_playhead_label = ""

    def _current_playhead_label(self):
        proj = get_app().project
        fps_info = proj.get("fps")
        fps_float = float(fps_info.get("num", 24)) / float(fps_info.get("den", 1) or 1)
        frame_seconds = 0.0
        if fps_float:
            frame_seconds = max(
                0.0, (max(1, self.w.current_frame) - 1) / fps_float
            )
        tt = secondsToTime(
            frame_seconds,
            fps_info["num"],
            fps_info["den"],
        )
        return f"{tt['hour']}:{tt['min']}:{tt['sec']},{tt['frame']}"

    def _draw_time_panel(self, painter: QPainter, label: str = None):
        left_rect = QRectF(0, 0, self.w.track_name_width, self.w.ruler_height)
        if left_rect.width() <= 0 or left_rect.height() <= 0:
            return left_rect
        if self.name_bg2 != self.name_bg:
            grad = QLinearGradient(left_rect.topLeft(), left_rect.bottomLeft())
            grad.setColorAt(0, self.name_bg)
            grad.setColorAt(1, self.name_bg2)
            painter.fillRect(left_rect, QBrush(grad))
        else:
            painter.fillRect(left_rect, self.name_bg)

        if label is None:
            label = self._current_playhead_label()
        if label:
            painter.setPen(self.text_pen)
            painter.setFont(self.play_font)
            painter.drawText(
                left_rect.adjusted(self.pad_left, self.pad_top, -2, -2),
                Qt.AlignLeft | Qt.AlignTop,
                label,
            )
            painter.setPen(self.tick_pen)
        return left_rect

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
        play_lbl = self._current_playhead_label()
        self._last_playhead_label = play_lbl
        self._draw_time_panel(painter, play_lbl)
        base_y = self.w.ruler_height
        tick_metrics = QFontMetrics(self.tick_font)
        label_top = max(0, self.label_top - 2)
        long_ht = base_y - (label_top + tick_metrics.height()) - 2
        short_ht = long_ht / 2
        painter.setPen(self.tick_pen)

        offset_px = getattr(self.w, "h_scroll_offset", 0.0)
        if pps <= 0:
            return
        visible_px = width
        start_seconds = offset_px / pps
        end_seconds = (offset_px + visible_px) / pps
        fpt = self._frames_per_tick(pps, fps_float)
        if fpt <= 0:
            return
        total_frames = int(duration * fps_float)
        start_frame = int(math.floor((start_seconds * fps_float) / fpt) * fpt)
        start_frame = max(0, start_frame)
        end_frame = int(math.ceil((end_seconds * fps_float) / fpt) * fpt)
        end_frame = min(total_frames, end_frame + fpt)
        frame = start_frame
        while frame <= end_frame:
            t = frame / fps_float
            x = self.w.track_name_width + t * pps - offset_px
            ht = long_ht if frame % (fpt * 2) == 0 else short_ht

            if x >= self.w.track_name_width - 2 and x <= self.w.track_name_width + visible_px + 2:
                painter.drawLine(QPointF(x, base_y), QPointF(x, base_y - ht))

            if frame % (fpt * 2) == 0 and (
                x + 1.0 >= self.w.track_name_width and x <= self.w.track_name_width + visible_px
            ):
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

    def paint_overlay(self, painter: QPainter):
        self._draw_time_panel(painter, getattr(self, "_last_playhead_label", None))


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
            self.menu_pix = self.scaled_pixmap(self.w.theme.menu_icon, size, size)
        self.menu_margin = self.w.theme.menu_margin

    def paint_background(self, painter: QPainter):
        area = QRectF(
            self.w.track_name_width,
            self.w.ruler_height,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        painter.save()
        painter.setClipRect(area)
        for track_rect, _track, _name_rect in self.w.geometry.track_rects:
            vis = track_rect.intersected(area)
            if vis.isNull():
                continue
            bg = self.w.theme.track.background
            bg2 = self.w.theme.track.background2
            if bg2.isValid() and bg2 != bg:
                grad = QLinearGradient(vis.topLeft(), vis.bottomLeft())
                grad.setColorAt(0, bg)
                grad.setColorAt(1, bg2)
                painter.fillRect(vis, QBrush(grad))
            else:
                painter.fillRect(vis, bg)
            painter.setPen(self.border_pen)
            painter.drawLine(vis.topLeft(), vis.topRight())
            painter.drawLine(vis.bottomLeft(), vis.bottomRight())
            painter.drawLine(vis.topRight(), vis.bottomRight())

        painter.fillRect(self.w.resize_handle_rect.intersected(area), self.w.theme.track.border_color)
        painter.restore()

    def paint_names(self, painter: QPainter):
        area = QRectF(
            0,
            self.w.ruler_height,
            self.w.track_name_width,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        painter.save()
        painter.setClipRect(area)
        for _track_rect, track, name_rect in self.w.geometry.track_rects:
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

            menu_w = 0.0
            if self.menu_pix:
                painter.drawPixmap(
                    QPointF(
                        name_rect.x() + self.name_border_width + self.menu_margin,
                        name_rect.y() + self.menu_margin,
                    ),
                    self.menu_pix,
                )
                menu_w, _ = self.logical_size(self.menu_pix)

            text_offset = self.name_border_width + self.menu_margin * 2 + menu_w
            painter.setPen(self.w.theme.track.font_color)
            painter.drawText(
                name_rect.adjusted(text_offset, self.menu_margin, -4, 0),
                Qt.AlignLeft | Qt.AlignTop,
                track.data.get("name", f"Track {track.data.get('number')}")
            )
        painter.restore()


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
            area = QRectF(
                self.w.track_name_width,
                self.w.ruler_height,
                self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
                self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
            )
            painter.save()
            vis = self.w.selection_rect.intersected(area)
            if not vis.isNull():
                if self.w.theme.selection.isValid():
                    painter.fillRect(vis, self.w.theme.selection)
                if self.pen.color().isValid() and self.pen.widthF() > 0:
                    painter.setPen(self.pen)
                    painter.drawRect(vis)
            painter.restore()


class ScrollbarPainter(BasePainter):
    """Draw horizontal and vertical scrollbars."""

    def update_theme(self):
        handle = getattr(self.w.theme, "scrollbar_handle", QColor())
        track = getattr(self.w.theme, "scrollbar_track", QColor())
        if not handle.isValid():
            handle = QColor("#4b92ad")
        if not track.isValid():
            track = QColor("#000")
        self.handle_brush = QBrush(handle)
        self.track_brush = QBrush(track)

    def paint(self, painter: QPainter):
        # Horizontal scrollbar
        sb = self.w.scroll_bar_rect
        if not sb.isNull():
            track = QRectF(
                self.w.track_name_width,
                self.w.height() - self.w.scroll_bar_thickness,
                self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
                self.w.scroll_bar_thickness,
            )
            painter.fillRect(track, self.track_brush)
            painter.fillRect(sb, self.handle_brush)

        # Vertical scrollbar
        sbv = getattr(self.w, "v_scroll_bar_rect", QRectF())
        if not sbv.isNull():
            track = QRectF(
                self.w.width() - self.w.scroll_bar_thickness,
                self.w.ruler_height,
                self.w.scroll_bar_thickness,
                self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
            )
            painter.fillRect(track, self.track_brush)
            painter.fillRect(sbv, self.handle_brush)


class KeyframePainter(BasePainter):
    def update_theme(self):
        fill = self.w.theme.keyframe_fill
        border = self.w.theme.keyframe_border
        base_color = QColor("#4d7bff")
        if fill.isValid():
            base_color = QColor(fill)
        self.fill = base_color
        border_color = QColor("#ffffff")
        if border.isValid():
            border_color = QColor(border)
        self.border = border_color
        self.pen = QPen(self.border, 1.2)
        self.pen.setCosmetic(True)
        self.inactive_opacity = getattr(self.w.theme, "keyframe_inactive_opacity", 0.5)
        self.size = max(1, int(getattr(self.w.theme, "keyframe_size", 10) or 10))

    def paint(self, painter: QPainter):
        markers = getattr(self.w, "_keyframe_markers", [])
        if not markers:
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(self.pen)
        for marker in markers:
            rect = marker.get("rect")
            if not isinstance(rect, QRectF) or rect.isNull():
                continue
            opacity = 1.0 if marker.get("selected") else self.inactive_opacity
            if marker.get("dimmed"):
                opacity *= 0.5
            painter.setOpacity(opacity)
            color = marker.get("color")
            if not isinstance(color, QColor) or not color.isValid():
                color = self.fill
            painter.setBrush(color)
            interpolation = marker.get("interpolation", "bezier")
            if interpolation == "linear":
                painter.drawRect(rect)
            elif interpolation == "constant":
                path = QPainterPath()
                center = rect.center()
                path.moveTo(center.x(), rect.top())
                path.lineTo(rect.right(), center.y())
                path.lineTo(center.x(), rect.bottom())
                path.lineTo(rect.left(), center.y())
                path.closeSubpath()
                painter.drawPath(path)
            else:
                painter.drawEllipse(rect)
        painter.restore()
