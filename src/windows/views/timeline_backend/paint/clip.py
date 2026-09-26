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

from qt_api import QPointF, QRectF, Qt, QTimer
from qt_api import (
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
from qt_api import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene
import copy
import json
import math
import os
import time

import openshot

from classes.app import get_app
from classes.keyframe_scaler import KeyframeScaler
from classes.logger import log
from classes.time_parts import secondsToTime
from classes import info

from .base import BasePainter


def _frame_for_seconds(seconds, fps):
    fps = float(fps or 0.0)
    if fps <= 0.0:
        fps = 24.0
    seconds = max(0.0, float(seconds or 0.0))
    frame_float = seconds * fps
    frame = int(math.floor(frame_float + 0.5)) + 1
    return max(1, frame)


def _clip_media_frame_count(clip, clip_fps):
    data = clip.data if isinstance(getattr(clip, "data", None), dict) else {}
    reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}

    try:
        video_length = int(round(float(reader.get("video_length", 0) or 0)))
    except (TypeError, ValueError):
        video_length = 0
    if video_length > 0:
        return video_length

    try:
        duration = float(reader.get("duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration > 0.0 and clip_fps > 0.0:
        return max(1, int(math.floor(duration * clip_fps)))

    return 0


def _scaled_time_keyframe_points(clip, clip_fps, project_fps):
    """Return time keyframe points scaled into clip-fps space."""
    data = clip.data if isinstance(getattr(clip, "data", None), dict) else {}
    time_data = data.get("time") if isinstance(data.get("time"), dict) else {}
    points = time_data.get("Points") if isinstance(time_data.get("Points"), list) else []
    if len(points) < 2:
        return []

    clip_fps = float(clip_fps or 0.0)
    project_fps = float(project_fps or 0.0)
    if clip_fps <= 0.0:
        clip_fps = 24.0
    if project_fps <= 0.0:
        project_fps = clip_fps

    scaled_time = copy.deepcopy(time_data)
    if project_fps > 0.0 and abs(project_fps - clip_fps) > 1e-6:
        payload = {"clips": [{"time": scaled_time}]}
        KeyframeScaler(clip_fps / project_fps)(payload)
        scaled_time = payload["clips"][0]["time"]
    return scaled_time.get("Points", []) if isinstance(scaled_time, dict) else []


def resolve_source_frame(clip, clip_time_seconds, clip_fps, project_fps=None, fallback_frame=None):
    """Resolve a source-media frame for a clip-local timestamp.

    Clips without a multi-point time curve fall back to linear trim mapping.
    Clips with time keyframes scale that curve into clip-fps space, then
    query the scaled curve directly to find the source reader frame.
    """
    frame = int(fallback_frame or _frame_for_seconds(clip_time_seconds, clip_fps))
    points = _scaled_time_keyframe_points(clip, clip_fps, project_fps)
    if len(points) < 2:
        return max(1, frame)

    clip_fps = float(clip_fps or 0.0)
    if clip_fps <= 0.0:
        clip_fps = 24.0

    project_fps = float(project_fps or 0.0)
    if project_fps <= 0.0:
        project_fps = clip_fps

    keyframe = openshot.Keyframe()
    point_count = 0
    for point in sorted(points, key=lambda value: float(value.get("co", {}).get("X", 0.0))):
        co = point.get("co") if isinstance(point, dict) else {}
        if not isinstance(co, dict):
            continue
        x_val = co.get("X")
        y_val = co.get("Y")
        if x_val is None or y_val is None:
            continue
        try:
            interpolation = int(point.get("interpolation", openshot.LINEAR))
        except (TypeError, ValueError):
            interpolation = openshot.LINEAR
        keyframe.AddPoint(float(x_val), float(y_val), interpolation)
        point_count += 1

    if point_count < 2:
        return max(1, frame)

    try:
        mapped_frame = int(keyframe.GetLong(frame))
    except Exception:
        return max(1, frame)

    max_frame = _clip_media_frame_count(clip, clip_fps)
    if max_frame > 0:
        mapped_frame = min(mapped_frame, max_frame)

    return max(1, mapped_frame or frame)


class ClipPainter(BasePainter):
    def __init__(self, widget):
        super().__init__(widget)
        self._thumbnail_repaint_timer = QTimer(self.w)
        self._thumbnail_repaint_timer.setSingleShot(True)
        self._thumbnail_repaint_timer.setInterval(250)
        self._thumbnail_repaint_timer.timeout.connect(self._flush_thumbnail_repaint)
        self._thumb_repaint_pending = False
        self._last_thumb_request_time = {}
        self._slot_fallback_cache = {}
        self._trim_request_cooldown = 0.12
        self._retime_preview_cache = {}

    MAX_THUMB_SLOTS = 150

    def _clip_timeline_position(self, clip):
        """Return the clip's left-edge position on the timeline in seconds.

        This reads the clip's data["position"] (if present), or falls back
        to a clip.position attribute. It is expected to be in *seconds*.
        """
        data = clip.data if isinstance(clip.data, dict) else {}
        pos = data.get("position", None)

        if self.w.clip_has_pending_override(clip):
            overrides = self.w._pending_clip_overrides.get(clip.id, {})
            pending_pos = overrides.get("position")
            if pending_pos is not None:
                pos = pending_pos

        if pos is None and hasattr(clip, "position"):
            try:
                pos = getattr(clip, "position")
            except Exception:
                pos = None

        return self._to_float(pos, 0.0)

    def update_theme(self):
        bw = float(self.w.theme.clip.border_width or 0.0)
        self.border_width = bw
        self.border_radius = float(self.w.theme.clip.border_radius or 0.0)
        self.clip_pen = QPen(QBrush(self.w.theme.clip.border_color), bw)
        self.clip_pen.setCosmetic(True)
        self.sel_pen = QPen(QBrush(self.w.theme.clip_selected), bw)
        self.sel_pen.setCosmetic(True)
        self.top_overlay = QColor(self.w.theme.clip.top_overlay)
        self.top_overlay2 = QColor(self.w.theme.clip.top_overlay2)
        self.menu_pix = None
        if self.w.theme.menu_icon:
            size = self.w.theme.menu_size or self.w.theme.menu_icon.width()
            self.menu_pix = self.w.theme.menu_icon.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        self.thumb_cache = {}
        self._thumb_pending = {}
        self._thumb_regions = {}
        self._thumb_missing_logged = set()
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
        self._thumb_missing_logged.clear()
        self._last_thumb_request_time.clear()
        self._slot_fallback_cache.clear()
        self._retime_preview_cache.clear()

    def invalidate_clip_thumbnails(
        self,
        clip_token,
        *,
        drop_cache=True,
        drop_pending=True,
        drop_fallback=True,
        drop_preview=True,
        invalidate_render_cache=True,
    ):
        """Invalidate thumbnail caches/requests for a single clip."""
        if not clip_token:
            return
        clip_id = str(clip_token).split(":", 1)[0]
        if drop_cache:
            cache_keys = [
                key for key in list(self.thumb_cache.keys())
                if isinstance(key, tuple) and key and str(key[0]).split(":", 1)[0] == clip_id
            ]
            for key in cache_keys:
                self.thumb_cache.pop(key, None)

        if drop_pending:
            pending_keys = [
                key for key in list(self._thumb_pending.keys())
                if isinstance(key, tuple) and key and str(key[0]).split(":", 1)[0] == clip_id
            ]
            for key in pending_keys:
                self._thumb_pending.pop(key, None)
                self._thumb_regions.pop(key, None)
                self._thumb_missing_logged.discard(key)

        if drop_fallback:
            fallback_keys = [
                key for key in list(self._slot_fallback_cache.keys())
                if isinstance(key, tuple) and key and str(key[0]).split(":", 1)[0] == clip_id
            ]
            for key in fallback_keys:
                self._slot_fallback_cache.pop(key, None)

        self._last_thumb_request_time.pop(clip_id, None)
        if drop_preview:
            self._retime_preview_cache.pop(clip_id, None)
        if invalidate_render_cache:
            self._invalidate_clip_cache_for_clip(clip_id)

    def clear_retime_preview(self, clip_token):
        if not clip_token:
            return
        clip_id = str(clip_token).split(":", 1)[0]
        self._retime_preview_cache.pop(clip_id, None)

    def _is_timing_preview_active(self, clip):
        if not clip or not isinstance(getattr(clip, "id", None), str):
            return False
        if not getattr(self.w, "clip_has_pending_override", None):
            return False
        if not self.w.clip_has_pending_override(clip):
            return False
        overrides = getattr(self.w, "_pending_clip_overrides", {}).get(clip.id, {})
        if not isinstance(overrides, dict) or not overrides.get("scale"):
            return False
        return (
            getattr(self.w, "_resizing_item", None) is clip
            and getattr(self.w, "_press_hit", "") == "clip-edge"
        )

    def _is_trim_preview_active(self, clip):
        if not clip or not isinstance(getattr(clip, "id", None), str):
            return False
        if not getattr(self.w, "clip_has_pending_override", None):
            return False
        if not self.w.clip_has_pending_override(clip):
            return False
        overrides = getattr(self.w, "_pending_clip_overrides", {}).get(clip.id, {})
        if not isinstance(overrides, dict) or overrides.get("scale"):
            return False
        return (
            getattr(self.w, "_resizing_item", None) is clip
            and getattr(self.w, "_press_hit", "") == "clip-edge"
        )

    def _trim_preview_offset_px(self, clip):
        overrides = getattr(self.w, "_pending_clip_overrides", {}).get(getattr(clip, "id", ""), {})
        if not isinstance(overrides, dict):
            return 0.0
        current_position = self._to_float(overrides.get("position"), self._clip_timeline_position(clip))
        initial_position = self._to_float(overrides.get("initial_position"), current_position)
        return (current_position - initial_position) * float(self.w.pixels_per_second or 0.0)

    def _retime_preview_result(self, clip, segment_rect):
        entry = self._retime_preview_cache.get(getattr(clip, "id", ""))
        if not entry:
            return None

        pix = entry.get("pix")
        blur = float(entry.get("blur", 0.0) or 0.0)
        if not isinstance(pix, QPixmap) or pix.isNull():
            return None

        ratio = 1.0
        try:
            ratio = float(pix.devicePixelRatioF())
        except AttributeError:
            try:
                ratio = float(pix.devicePixelRatio())
            except AttributeError:
                ratio = 1.0
        if not math.isfinite(ratio) or ratio <= 0.0:
            ratio = 1.0

        logical_w = max(1, int(round(float(segment_rect.width()) + (blur * 2.0))))
        logical_h = max(1, int(round(float(segment_rect.height()) + (blur * 2.0))))
        target_w = max(1, int(round(logical_w * ratio)))
        target_h = max(1, int(round(logical_h * ratio)))

        scaled = pix.scaled(target_w, target_h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        if not scaled or scaled.isNull():
            return None
        if ratio != 1.0:
            scaled.setDevicePixelRatio(ratio)
        return (scaled, blur, [], False, None)

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
            locked = self.w._is_track_locked((clip.data if isinstance(clip.data, dict) else {}).get("layer"))
            if locked:
                pen = self.dimmed_pen(pen)
                painter.save()
                painter.setOpacity(0.8)
            self._draw_clip(painter, rect, segment_rect, clip, pen, selected)
            if locked:
                painter.restore()
        painter.restore()

    @staticmethod
    def _to_float(value, fallback=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _clip_key(self, clip):
        clip_id = getattr(clip, "id", None)
        return str(clip_id) if clip_id is not None else ""

    def _has_static_image(self, clip):
        """Return True if the clip reports a static image so all frames are identical."""
        if not clip:
            return False
        data = clip.data if isinstance(clip.data, dict) else {}
        reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
        flags = (
            data.get("has_static_image"),
            reader.get("has_static_image"),
            data.get("has_single_image"),
            reader.get("has_single_image"),
        )
        for value in flags:
            if isinstance(value, bool) and value:
                return True
            if isinstance(value, (int, float)) and value:
                return True
        return False

    def _clip_file_id(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        file_id = data.get("file_id")
        return str(file_id) if file_id else None

    def _clip_time_bounds(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        start = self._to_float(data.get("start"), 0.0)
        end = self._to_float(data.get("end"), start)
        if self.w.clip_has_pending_override(clip):
            overrides = self.w._pending_clip_overrides.get(clip.id, {})
            pending_start = overrides.get("start")
            pending_end = overrides.get("end")
            if pending_start is not None:
                start = self._to_float(pending_start, start)
            if pending_end is not None:
                end = self._to_float(pending_end, end)
        if end < start:
            end = start
        return start, max(0.0, end - start)

    def _existing_thumb_path(self, file_id, frame):
        from classes.thumbnail import resolve_thumbnail_path
        fingerprint = None
        try:
            from classes.query import File
            f = File.get(id=file_id)
            if f and isinstance(getattr(f, "data", None), dict):
                fingerprint = f.data.get("fingerprint")
        except Exception:
            fingerprint = None
        return resolve_thumbnail_path(file_id, frame, fingerprint=fingerprint) or ""

    def _frame_for_offset(self, offset, fps):
        return _frame_for_seconds(offset, fps)

    def _frame_rounding_increment(self, fps, interval_seconds):
        """Return frame rounding increment based on frames-per-slot at current zoom.

        Keep rounding local enough to reuse nearby thumbnails while avoiding
        visible multi-second jumps as the zoom level changes.
        """
        fps = float(fps or 0.0)
        if fps <= 0.0 or not interval_seconds or interval_seconds <= 0.0:
            return 1
        frames_per_slot = fps * float(interval_seconds)
        if frames_per_slot <= 1.25:
            return 1
        # Cap rounding at roughly a quarter-second so cache reuse stays local.
        max_increment = max(1, int(round(fps / 4.0)))
        return max(1, min(int(round(frames_per_slot)), max_increment))

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

    def _clip_media_duration(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
        candidate_durations = []

        duration = self._to_float(reader.get("duration"))
        if duration > 0.0:
            candidate_durations.append(duration)
        video_length = self._to_float(reader.get("video_length"))
        if video_length > 0.0:
            fps_meta = reader.get("fps") if isinstance(reader.get("fps"), dict) else {}
            fps_num = self._to_float(fps_meta.get("num"))
            fps_den = self._to_float(fps_meta.get("den"))
            if fps_num > 0.0 and fps_den > 0.0:
                fps_value = fps_num / fps_den
                if fps_value > 0.0:
                    candidate_durations.append(video_length / fps_value)
        project_fps = self._to_float(getattr(self.w, "fps_float", None))
        time_data = data.get("time") if isinstance(data.get("time"), dict) else {}
        time_points = time_data.get("Points") if isinstance(time_data.get("Points"), list) else []
        if project_fps > 0.0 and len(time_points) >= 2:
            x_values = []
            for point in time_points:
                if not isinstance(point, dict):
                    continue
                co = point.get("co")
                if not isinstance(co, dict):
                    continue
                x_val = self._to_float(co.get("X"))
                if x_val > 0.0:
                    x_values.append(x_val)
            if len(x_values) >= 2:
                time_duration = (max(x_values) - min(x_values)) / project_fps
                if time_duration > 0.0:
                    candidate_durations.append(time_duration)
        clip_duration = self._to_float(data.get("duration"))
        if clip_duration > 0.0:
            candidate_durations.append(clip_duration)
        start = self._to_float(data.get("start"))
        end = self._to_float(data.get("end"), start)
        span = end - start
        if span > 0.0:
            candidate_durations.append(span)
        return max(candidate_durations) if candidate_durations else 0.0

    def _clip_trim_start(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        start = self._to_float(data.get("start"), 0.0)

        if self.w.clip_has_pending_override(clip):
            overrides = self.w._pending_clip_overrides.get(clip.id, {})
            pending_start = overrides.get("start")
            if pending_start is not None:
                start = self._to_float(pending_start, start)

        return start

    def _clip_media_fps(self, clip):
        data = clip.data if isinstance(clip.data, dict) else {}
        reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
        fps_meta = reader.get("fps") if isinstance(reader.get("fps"), dict) else {}
        fps_num = self._to_float(fps_meta.get("num"))
        fps_den = self._to_float(fps_meta.get("den"), 1.0)
        if fps_num > 0.0 and fps_den > 0.0:
            return fps_num / fps_den
        fps_value = self._to_float(data.get("fps"))
        if fps_value > 0.0:
            return fps_value
        fps = self._to_float(reader.get("frame_rate"))
        if fps > 0.0:
            return fps
        return float(getattr(self.w, "fps_float", 24.0) or 24.0)

    def _clip_pixmap(self, full_rect, segment_rect, clip):
        """Return cached pixmap for the visible portion of a clip."""

        w = int(segment_rect.width())
        h = int(segment_rect.height())
        if w <= 0 or h <= 0:
            return None

        if self._is_timing_preview_active(clip):
            preview = self._retime_preview_result(clip, segment_rect)
            if preview:
                return preview

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
            self._fill_clip_background(painter, inner_rect, segment_info)
            icon_entries, pending_thumbs = self._draw_clip_contents(
                painter, clip, inner_rect, segment_info
            )

        painter.end()

        pix = QPixmap.fromImage(img)
        if ratio != 1.0:
            pix.setDevicePixelRatio(ratio)
        result = (pix, blur, icon_entries, pending_thumbs, text_entry)
        if getattr(clip, "id", None):
            self._retime_preview_cache[str(clip.id)] = {
                "pix": pix,
                "blur": blur,
                "icons": icon_entries,
                "text_entry": text_entry,
            }
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

    def _clip_fill_path(self, rect, includes_start=True, includes_end=True):
        if rect.width() <= 0.0 or rect.height() <= 0.0:
            return None
        radius = 0.0
        if rect.width() >= 20.0 and rect.height() > 0.0:
            radius = min(float(self.border_radius or 0.0), min(rect.width(), rect.height()) / 2.0)
        if radius <= 0.0:
            return None

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
        return path

    def _fill_clip_background(self, painter, inner_rect, segment=None):
        includes_start = True
        includes_end = True
        if isinstance(segment, dict):
            includes_start = bool(segment.get("includes_start", True))
            includes_end = bool(segment.get("includes_end", True))
        shape_path = self._clip_fill_path(inner_rect, includes_start, includes_end)

        bg = self.w.theme.clip.background
        bg2 = self.w.theme.clip.background2
        if bg2.isValid() and bg2 != bg:
            grad = QLinearGradient(QPointF(inner_rect.topLeft()), QPointF(inner_rect.bottomLeft()))
            grad.setColorAt(0, bg)
            grad.setColorAt(1, bg2)
            if shape_path:
                painter.fillPath(shape_path, QBrush(grad))
            else:
                painter.fillRect(inner_rect, QBrush(grad))
        elif bg.isValid():
            if shape_path:
                painter.fillPath(shape_path, bg)
            else:
                painter.fillRect(inner_rect, bg)

        # Match JS .clip_top overlay (light-to-transparent).
        top_overlay = QColor(self.top_overlay)
        bottom_overlay = QColor(self.top_overlay2)
        if top_overlay.isValid() or bottom_overlay.isValid():
            if not top_overlay.isValid() and bottom_overlay.isValid():
                top_overlay = QColor(bottom_overlay)
            if not bottom_overlay.isValid() and top_overlay.isValid():
                bottom_overlay = QColor(top_overlay)
                bottom_overlay.setAlpha(0)
            overlay = QLinearGradient(inner_rect.topLeft(), inner_rect.bottomLeft())
            overlay.setColorAt(0.0, top_overlay)
            overlay.setColorAt(1.0, bottom_overlay)
            if shape_path:
                painter.fillPath(shape_path, QBrush(overlay))
            else:
                painter.fillRect(inner_rect, QBrush(overlay))

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

    def _add_slot_if_valid(self, slots, seen, center_clip_time, half_interval, segment_start,
                           segment_end, trim_start, media_duration, inner_x, top,
                           thumb_w, thumb_h, pixels_per_second, view_left, view_right):
        """Helper to add a slot if it meets all criteria."""
        start_clip_time = center_clip_time - half_interval
        end_clip_time = center_clip_time + half_interval

        # Require overlap with visible segment
        if end_clip_time <= segment_start + 1e-6 or start_clip_time >= segment_end - 1e-6:
            return

        # X coordinate within the visible segment
        local_x = (start_clip_time - segment_start) * pixels_per_second
        if local_x >= view_right or (local_x + thumb_w) <= view_left:
            return

        # Media time check
        center_media_time = trim_start + center_clip_time
        if center_media_time < -1e-6 or center_media_time > media_duration + 1e-6:
            return
        center_media_time = max(0.0, min(center_media_time, media_duration))

        # Deduplicate by clip-local time
        key = round(center_clip_time, 4)
        if key in seen:
            return
        seen.add(key)

        rect = QRectF(inner_x + local_x, top, thumb_w, thumb_h)
        slots.append((center_clip_time, rect))

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
            # Clear pending override after update to ensure consistency
            self._pending_clip_overrides.pop(item.id, None)
        else:
            item.data["position"] = self._snap_time(position)
            item.data["start"] = 0.0
            item.data["end"] = self._snap_time(end)
            item.data["duration"] = self._snap_time(end)
            self.update_transition_data(item.data, only_basic_props=True)

        self._resizing_item = None
        self._snap_keyframe_seconds = []
        self.snap.reset()
        if hasattr(self, "_resize_snap_ignore_backup"):
            self._snap_ignore_ids = self._resize_snap_ignore_backup
            del self._resize_snap_ignore_backup
        self._update_project_duration()
        self.changed(None)
        self.geometry.mark_dirty()  # Ensure geometry rebuild
        self.update()
        self._release_cursor()
        if self._last_event:
            self._updateCursor(self._last_event.pos())
        if hasattr(self, "_resize_initial_world_rect"):
            del self._resize_initial_world_rect
        self._resize_clip_max_duration = None
        self._resize_allow_left_overflow = False
        self._resize_clip_is_single_image = False

    def _build_thumbnail_slots(self, clip, inner, segment, style, timing):
        """ Build thumbnail slots for a clip. """
        if style == "none":
            return [], None

        segment = segment or {}
        timing = timing or {}

        # Visible width of this segment (in pixels)
        visible_width = max(0.0, float(segment.get("segment_width") or inner.width()))
        if visible_width < self._min_clip_thumb_width:
            return [], None

        # Full clip width in pixels at the current zoom
        clip_width = float(segment.get("clip_width") or visible_width)
        if clip_width <= 0.0:
            return [], None

        # Slot dimensions
        theme_thumb_w = float(self.w.theme.clip.thumb_width or inner.height())
        thumb_w = theme_thumb_w
        thumb_h = float(self.w.theme.clip.thumb_height or inner.height())
        # Keep slot width at nominal thumbnail width even for small clips.
        # This allows the clip bounds to crop thumbnails instead of shrinking them.
        thumb_w = max(self._min_thumb_slot_width, thumb_w)
        thumb_h = max(self._min_thumb_slot_width, min(thumb_h, inner.height()))
        top = inner.y() + (inner.height() - thumb_h) / 2.0
        # Keep legacy/default theme behavior while nudging thumbnails downward
        # on taller tracks so they do not appear vertically centered too high.
        baseline_clip_height = 48.0
        if inner.height() > baseline_clip_height:
            top += (inner.height() - baseline_clip_height) / 2.0
            top += 3.0
        max_top = inner.bottom() - thumb_h
        if max_top < inner.y():
            max_top = inner.y()
        top = min(max(top, inner.y()), max_top)

        pixels_per_second = float(self.w.pixels_per_second or 0.0)
        if pixels_per_second <= 0.0:
            return [], None

        # Clip duration on the timeline (seconds)
        clip_duration = self._to_float(
            segment.get("clip_duration"),
            clip_width / pixels_per_second,
        )
        if clip_duration <= 0.0:
            return [], None

        # Segment window in clip-local seconds
        segment_offset = self._to_float(segment.get("offset_seconds"), 0.0)
        segment_duration = self._to_float(
            segment.get("duration_seconds"),
            clip_duration,
        )
        segment_duration = max(0.0, min(segment_duration, clip_duration))
        if segment_duration <= 0.0:
            return [], None

        segment_start = segment_offset
        segment_end = segment_start + segment_duration
        if segment_end <= segment_start:
            return [], None

        # Source media duration (seconds)
        media_duration = self._clip_media_duration(clip)
        if media_duration <= 0.0:
            media_duration = clip_duration
        media_duration = max(media_duration, clip_duration)
        time_data = clip.data.get("time") if isinstance(getattr(clip, "data", None), dict) and isinstance(clip.data.get("time"), dict) else {}
        time_points = time_data.get("Points") if isinstance(time_data.get("Points"), list) else []
        has_time_curve = len(time_points) >= 2

        # Slot spacing in time
        # Entire style keeps spacing tied to nominal thumb width (not the
        # shrinking clip width), which prevents end-of-trim slot oscillation.
        if style == "entire":
            interval_pixels = max(theme_thumb_w, self._min_thumb_slot_width)
        else:
            interval_pixels = max(thumb_w, self._min_thumb_slot_width)
        interval_seconds = interval_pixels / pixels_per_second
        if interval_seconds <= 0.0:
            interval_seconds = 0.01
        if style == "entire":
            clip_fps = self._clip_media_fps(clip)
            if clip_fps > 0.0:
                interval_frames = max(1, int(round(interval_seconds * clip_fps)))
                interval_seconds = interval_frames / clip_fps
        half_interval = interval_seconds * 0.5
        slot_duration_seconds = interval_seconds

        includes_start = bool(timing.get("includes_start", True))
        includes_end = bool(timing.get("includes_end", True))

        # --- World-anchor via (position - start) --------------------------

        trim_start = self._clip_trim_start(clip)  # media in-point
        clip_pos = self._clip_timeline_position(clip)  # world time of clip left
        anchor_world = clip_pos - trim_start  # world time of media 0.0

        # World-time range covered by this segment of the clip
        segment_start_world = clip_pos + segment_start
        segment_end_world = segment_start_world + segment_duration

        view_left = 0.0
        view_right = visible_width

        slots = []
        seen = set()

        epsilon = 1e-6

        def add_center_world(center_world):
            """
            Add a slot whose left edge begins at `center_world` (timeline seconds),
            if it overlaps the visible segment and lies within clip & media.
            """

            # Media time (0 at media start)
            slot_start_media_time = center_world - anchor_world

            # Clip-local time (0 at clip's left edge)
            slot_start_clip_time = center_world - clip_pos
            slot_end_clip_time = slot_start_clip_time + slot_duration_seconds

            # Require overlap with visible segment (lenient for boundary cases)
            if (
                slot_end_clip_time < segment_start - epsilon
                or slot_start_clip_time > segment_end + epsilon
            ):
                return

            # Slot coverage in media time
            slot_end_media_time = slot_start_media_time + slot_duration_seconds

            # Require overlap with media bounds [0, media_duration] (lenient)
            if not has_time_curve:
                if (
                    slot_end_media_time < -epsilon
                    or slot_start_media_time > media_duration + epsilon
                ):
                    return

            # X coordinate within the visible segment (lenient for boundary cases)
            local_x = (slot_start_clip_time - segment_start) * pixels_per_second
            if local_x > view_right + epsilon or (local_x + thumb_w) < view_left - epsilon:
                return

            # Deduplicate by clip-local time to avoid overlapping slots
            key = round(slot_start_clip_time, 4)
            if key in seen:
                return
            seen.add(key)

            rect = QRectF(inner.x() + local_x, top, thumb_w, thumb_h)
            # Store slot start time; _draw_thumbnails samples near the center.
            slots.append((slot_start_clip_time, rect))

        # --- Style handling -----------------------------------------------

        if style == "start":
            if includes_start:
                add_center_world(segment_start_world)
        elif style == "start-end":
            if includes_start:
                add_center_world(segment_start_world)
            # If the visible segment cannot fit two full slots, prioritize the
            # start slot to avoid the end slot covering it on very short clips.
            allow_end_slot = True
            if includes_start and includes_end and segment_duration < (slot_duration_seconds * 2.0):
                allow_end_slot = False
            if includes_end and allow_end_slot:
                # Start slot so its right edge aligns with the clip end
                clip_end_world = clip_pos + max(0.0, clip_duration - slot_duration_seconds)
                add_center_world(clip_end_world)
        else:
            # Full-grid style ("entire", etc.)
            # Slot starts should cover any thumbnail overlapping the visible
            # segment, including partials at either edge.
            n_min = int(
                math.floor(
                    (segment_start_world - slot_duration_seconds - anchor_world) / interval_seconds
                )
            ) - 2
            n_max = int(
                math.ceil(
                    (segment_end_world - anchor_world) / interval_seconds
                )
            ) + 2

            for n in range(n_min, n_max + 1):
                center_world = anchor_world + n * interval_seconds
                add_center_world(center_world)

        if not slots:
            return [], interval_seconds

        return slots, interval_seconds

    def _draw_thumbnails(self, painter, clip, inner, segment):
        style = str(getattr(self.w, "thumbnail_style", "entire") or "").strip().lower()
        if style == "none":
            return False
        clip_key = self._clip_key(clip)
        file_id = self._clip_file_id(clip)
        if not (clip_key and file_id):
            return False

        _, clip_duration = self._clip_time_bounds(clip)
        timing = self._segment_timing(segment, clip_duration)
        slots, interval_seconds = self._build_thumbnail_slots(clip, inner, segment, style, timing)
        if not slots:
            return False

        clip_fps = self._clip_media_fps(clip)
        project_fps = float(getattr(self.w, "fps_float", clip_fps) or clip_fps or 24.0)
        trim_start = self._clip_trim_start(clip)
        slot_duration_seconds = float(interval_seconds or 0.0)
        half_slot_duration = slot_duration_seconds * 0.5
        segment_offset = float(timing.get("offset", 0.0) or 0.0)
        segment_duration = float(timing.get("duration", 0.0) or 0.0)
        segment_end = segment_offset + segment_duration
        frame_duration = (1.0 / project_fps) if project_fps and project_fps > 0.0 else 0.0
        clip_start_frame = _frame_for_seconds(trim_start + segment_offset, clip_fps)
        if frame_duration > 0.0:
            clip_end_frame = _frame_for_seconds(max(trim_start + segment_offset, trim_start + segment_end - frame_duration), clip_fps)
        else:
            clip_end_frame = _frame_for_seconds(trim_start + segment_end, clip_fps)
        edge_epsilon = 1e-6
        is_resizing_clip = (
            getattr(self.w, "_resizing_item", None) is clip
            and getattr(self.w, "_press_hit", "") == "clip-edge"
        )
        throttle_requests = is_resizing_clip and style in ("start", "start-end")

        pending = False
        generation = getattr(self.w, "thumbnail_generation", 0)
        rounding = self._frame_rounding_increment(clip_fps, interval_seconds)
        static_image = self._has_static_image(clip)
        static_frame = 1 if static_image else None

        for slot_index, (time_offset, rect) in enumerate(slots):
            if self._clip_is_audio_only(clip):
                pix = self._audio_thumbnail_pixmap()
                if pix:
                    self._paint_thumbnail_pixmap(painter, pix, rect, inner)
                else:
                    pending = True
                continue

            slot_start_time = float(time_offset)
            slot_end_time = slot_start_time + slot_duration_seconds
            slot_center_time = slot_start_time + half_slot_duration
            clamped_center_time = slot_center_time
            if clamped_center_time < segment_offset:
                clamped_center_time = segment_offset
            elif clamped_center_time > segment_end:
                clamped_center_time = segment_end

            touches_start = slot_start_time <= segment_offset + edge_epsilon
            touches_end = slot_end_time >= segment_end - edge_epsilon
            is_first_slot = slot_index == 0
            is_last_slot = slot_index == (len(slots) - 1)
            is_edge = (touches_start and is_first_slot) or (touches_end and is_last_slot)
            slot_role = "grid"
            if touches_start and is_first_slot:
                slot_role = "edge-start"
            elif touches_end and is_last_slot:
                slot_role = "edge-end"

            # For "start" and "start-end", anchor edge thumbnails to exact trim edges.
            # Keep strip/entire behavior unchanged (centered sampling).
            if style in ("start", "start-end") and slot_role == "edge-start":
                sample_time = segment_offset
            elif style in ("start", "start-end") and slot_role == "edge-end":
                if frame_duration > 0.0:
                    sample_time = max(segment_offset, segment_end - frame_duration)
                else:
                    sample_time = segment_end
            elif style == "entire":
                # Sample the visual center of each strip thumbnail, but clamp the
                # first/last visible slots to the actual clip edges.
                if slot_role == "edge-start":
                    sample_time = segment_offset
                elif slot_role == "edge-end":
                    if frame_duration > 0.0:
                        sample_time = max(segment_offset, segment_end - frame_duration)
                    else:
                        sample_time = segment_end
                else:
                    sample_time = clamped_center_time
            else:
                sample_time = clamped_center_time

            clip_time = trim_start + sample_time
            frame = self._frame_for_offset(clip_time, clip_fps)
            pre_round_frame = frame
            if rounding > 1 and (style == "entire" or not is_edge):
                frame = max(1, int(round((frame - 1) / rounding) * rounding) + 1)
            frame = min(max(frame, clip_start_frame), clip_end_frame)
            rounded_frame = frame
            mapped_frame = resolve_source_frame(
                clip,
                clip_time,
                clip_fps,
                project_fps,
                fallback_frame=frame,
            )
            frame = mapped_frame
            if static_frame:
                frame = static_frame

            key = (clip_key, frame)
            cached = self.thumb_cache.get(key)
            if cached and not cached.isNull():
                pix = cached
            else:
                allow_request = True
                if throttle_requests:
                    allow_request = self._can_request_thumbnail(clip_key, throttle_requests)

                # Always queue/load for all slots in the clip (since clip is visible during paint)
                pix = self._get_thumbnail_pixmap(
                    clip_key,
                    file_id,
                    frame,
                    rect,
                    generation,
                    allow_request=allow_request,
                )
                if pix is None and slot_role != "grid":
                    fallback = self._slot_fallback_cache.get((clip_key, slot_role))
                    if fallback and not fallback.isNull():
                        pix = fallback

            if pix:
                self._paint_thumbnail_pixmap(painter, pix, rect, inner)
                if slot_role != "grid":
                    self._slot_fallback_cache[(clip_key, slot_role)] = pix
            else:
                pending = True

        return pending

    def _get_thumbnail_pixmap(self, clip_key, file_id, frame, rect, generation, *, allow_request=True):
        key = (clip_key, frame)

        # 1. If we already have it cached → return it immediately
        if key in self.thumb_cache:
            cached = self.thumb_cache[key]
            if not cached.isNull():
                return cached
            # Null pixmap means "we tried and failed" — don't request again this generation
            if self._thumb_pending.get(key) == generation:
                return None

        # 2. If already requested this generation → don't request again
        if self._thumb_pending.get(key) == generation:
            return None

        if getattr(self.w, "_suspend_thumbnail_requests", False):
            return None

        if not allow_request:
            return None

        # Queue the request exactly once per generation (only for visible slots)
        self._thumb_pending[key] = generation
        self._thumb_regions[key] = QRectF(rect)
        if self.w.thumbnail_manager:
            self.w.thumbnail_manager.request_thumbnail(clip_key, file_id, frame, generation)
            if key not in self._thumb_missing_logged:
                self._thumb_missing_logged.add(key)
                log.debug("Thumbnail miss queued %s gen=%s", key, generation)

        return None

    def _paint_thumbnail_pixmap(self, painter, pixmap, rect, clip_bounds):
        if not pixmap or pixmap.isNull() or not isinstance(rect, QRectF) or not isinstance(clip_bounds, QRectF):
            return

        visible_rect = rect.intersected(clip_bounds)
        if visible_rect.isEmpty():
            return

        rect_width = rect.width()
        if rect_width <= 0.0:
            return

        width = max(1, int(round(rect_width)))
        height = max(1, int(round(rect.height())))
        scaled = pixmap.scaled(
            width,
            height,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        full_width = float(scaled.width())
        scaled_height = float(scaled.height())

        # Compute fractions for source clipping
        frac_left = max(0.0, (visible_rect.left() - rect.left()) / rect_width)
        frac_width = visible_rect.width() / rect_width

        source_x = frac_left * full_width
        draw_width = frac_width * full_width

        # Target rect within visible area
        target_x = visible_rect.x()
        target_y = visible_rect.y() + (visible_rect.height() - scaled_height) / 2.0
        target = QRectF(target_x, target_y, visible_rect.width(), scaled_height)

        # Source rect from scaled pixmap
        source = QRectF(source_x, 0.0, draw_width, scaled_height)

        had_hint = bool(painter.renderHints() & QPainter.SmoothPixmapTransform)
        if not had_hint:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(target, scaled, source)
        if not had_hint:
            painter.setRenderHint(QPainter.SmoothPixmapTransform, False)

    def _slot_is_visible(self, rect):
        """Return True if a thumbnail slot rect intersects the current viewport."""
        if not isinstance(rect, QRectF):
            return False
        view = QRectF(
            self.w.track_name_width,
            self.w.ruler_height,
            self.w.width() - self.w.track_name_width - self.w.scroll_bar_thickness,
            self.w.height() - self.w.ruler_height - self.w.scroll_bar_thickness,
        )
        return view.isValid() and view.intersects(rect)

    def _can_request_thumbnail(self, clip_key, throttle_requests):
        """Throttle requests while trimming to avoid flooding the manager."""
        if not throttle_requests:
            return True
        now = time.monotonic()
        cooldown = max(0.01, float(self._trim_request_cooldown or 0.0))
        last = self._last_thumb_request_time.get(clip_key)
        if last is not None and (now - last) < cooldown:
            return False
        self._last_thumb_request_time[clip_key] = now
        return True

    def _flush_thumbnail_repaint(self):
        if not self._thumb_repaint_pending:
            return
        self._thumb_repaint_pending = False
        self.w.update()

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
        style = str(getattr(self.w, "thumbnail_style", "entire") or "").strip().lower()
        pix = None
        shadow_spread = 0.0
        icons = []
        text_entry = None
        preview_drawn = False
        if style in ("entire", "start", "start-end") and self._is_trim_preview_active(clip):
            preview = self._retime_preview_cache.get(getattr(clip, "id", ""))
            pix = preview.get("pix") if isinstance(preview, dict) else None
            blur = float(preview.get("blur", 0.0) or 0.0) if isinstance(preview, dict) else 0.0
            if isinstance(pix, QPixmap) and not pix.isNull():
                shadow_spread = blur
                icons = preview.get("icons") if isinstance(preview.get("icons"), list) else []
                text_entry = preview.get("text_entry") if isinstance(preview.get("text_entry"), dict) else None
                offset_x = segment_rect.x() - blur - self._trim_preview_offset_px(clip)
                offset = QPointF(offset_x, segment_rect.y() - blur)
                painter.save()
                painter.setClipRect(segment_rect, Qt.IntersectClip)
                painter.drawPixmap(offset, pix)
                painter.restore()
                preview_drawn = True
        else:
            result = self._clip_pixmap(full_rect, segment_rect, clip)
            if not result:
                return
            pix, shadow_spread, icons, _, text_entry = result
        includes_start = (segment_rect.left() - full_rect.left()) <= 0.5
        if pix:
            offset = QPointF(segment_rect.x() - shadow_spread, segment_rect.y() - shadow_spread)
            if not preview_drawn:
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

        border_pen = pen if isinstance(pen, QPen) else (self.sel_pen if selected else self.clip_pen)
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
            self._thumb_missing_logged.discard(key)

    def handle_thumbnail_ready(self, clip_id, frame, thumb_path, generation):
        clip_key = str(clip_id or "")
        key = (clip_key, int(frame or 0))
        pending_generation = self._thumb_pending.get(key)

        # Ignore if not from current generation
        if pending_generation != generation:
            return

        self._thumb_pending.pop(key, None)
        rect = self._thumb_regions.pop(key, None)

        pix = QPixmap()
        if thumb_path and os.path.exists(thumb_path):
            pix = QPixmap(thumb_path)

        # Store even empty pixmaps so we don't re-request failed ones
        self.thumb_cache[key] = pix
        self._invalidate_clip_cache_for_clip(clip_key)

        # Safe repaint — defer to avoid active painter issues
        QTimer.singleShot(0, self.w.update)

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
