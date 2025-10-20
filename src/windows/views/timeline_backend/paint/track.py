"""
 @file
 @brief Painter for track backgrounds and labels.
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
from PyQt5.QtGui import QBrush, QLinearGradient, QPainter, QPainterPath, QPen

from .base import BasePainter


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
        self.toggle_off_pix = None
        self.toggle_on_pix = None
        toggle_size = float(self.w.theme.menu_size or 0.0)

        def _scaled_toggle(pixmap):
            if not pixmap or pixmap.isNull():
                return None
            width = float(pixmap.width())
            height = float(pixmap.height())
            if toggle_size > 0.0:
                target = max(toggle_size, width, height)
                width = height = target
            return self.scaled_pixmap(pixmap, width, height)

        if self.w.theme.keyframe_toggle_off_icon:
            self.toggle_off_pix = _scaled_toggle(
                self.w.theme.keyframe_toggle_off_icon
            )
        if self.w.theme.keyframe_toggle_on_icon:
            self.toggle_on_pix = _scaled_toggle(
                self.w.theme.keyframe_toggle_on_icon
            )
        self.toggle_margin = self.w.theme.menu_margin

        self.toolbar_order = (
            "keyframe-panel",
            "insert-above",
            "insert-below",
            "lock-toggle",
            "delete-track",
        )

        toolbar = {}

        keyframe_disabled = _scaled_toggle(
            getattr(self.w.theme, "track_keyframe_panel_disabled_icon", None)
            or self.w.theme.keyframe_toggle_off_icon
        )
        keyframe_enabled = _scaled_toggle(
            getattr(self.w.theme, "track_keyframe_panel_enabled_icon", None)
            or self.w.theme.keyframe_toggle_on_icon
        )
        if keyframe_disabled or keyframe_enabled:
            toolbar["keyframe-panel"] = {
                "disabled": keyframe_disabled,
                "enabled": keyframe_enabled or keyframe_disabled,
            }
            if not self.toggle_off_pix:
                self.toggle_off_pix = keyframe_disabled
            if not self.toggle_on_pix:
                self.toggle_on_pix = keyframe_enabled or keyframe_disabled

        insert_above_disabled = _scaled_toggle(getattr(self.w.theme, "track_add_above_disabled_icon", None))
        insert_above_enabled = _scaled_toggle(getattr(self.w.theme, "track_add_above_enabled_icon", None))
        if insert_above_disabled or insert_above_enabled:
            toolbar["insert-above"] = {
                "disabled": insert_above_disabled,
                "enabled": insert_above_enabled or insert_above_disabled,
            }

        insert_below_disabled = _scaled_toggle(getattr(self.w.theme, "track_add_below_disabled_icon", None))
        insert_below_enabled = _scaled_toggle(getattr(self.w.theme, "track_add_below_enabled_icon", None))
        if insert_below_disabled or insert_below_enabled:
            toolbar["insert-below"] = {
                "disabled": insert_below_disabled,
                "enabled": insert_below_enabled or insert_below_disabled,
            }

        delete_disabled = _scaled_toggle(getattr(self.w.theme, "track_delete_disabled_icon", None))
        delete_enabled = _scaled_toggle(getattr(self.w.theme, "track_delete_enabled_icon", None))
        if delete_disabled or delete_enabled:
            toolbar["delete-track"] = {
                "disabled": delete_disabled,
                "enabled": delete_enabled or delete_disabled,
            }

        lock_locked_disabled = _scaled_toggle(getattr(self.w.theme, "track_locked_disabled_icon", None))
        lock_locked_enabled = _scaled_toggle(getattr(self.w.theme, "track_locked_enabled_icon", None))
        lock_unlocked_disabled = _scaled_toggle(getattr(self.w.theme, "track_unlocked_disabled_icon", None))
        lock_unlocked_enabled = _scaled_toggle(getattr(self.w.theme, "track_unlocked_enabled_icon", None))
        if (
            lock_locked_disabled
            or lock_locked_enabled
            or lock_unlocked_disabled
            or lock_unlocked_enabled
        ):
            toolbar["lock-toggle"] = {
                "locked": {
                    "disabled": lock_locked_disabled,
                    "enabled": lock_locked_enabled or lock_locked_disabled,
                },
                "unlocked": {
                    "disabled": lock_unlocked_disabled,
                    "enabled": lock_unlocked_enabled or lock_unlocked_disabled,
                },
            }

        self.toolbar_pixmaps = toolbar

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

            buttons = self.w._track_toolbar_buttons(track, name_rect)
            toolbar_height = 0.0
            if buttons:
                toolbar_height = max(btn["rect"].height() for btn in buttons)
            text_offset = self.name_border_width + self.menu_margin * 2 + menu_w
            painter.setPen(self.w.theme.track.font_color)
            painter.drawText(
                name_rect.adjusted(text_offset, self.menu_margin, -4, -toolbar_height),
                Qt.AlignLeft | Qt.AlignTop,
                self.w._track_display_label(track)
            )

            hover_key = getattr(self.w, "_toolbar_hover_key", None)
            pressed_key = getattr(self.w, "_toolbar_pressed_key", None)
            pressed_inside = getattr(self.w, "_toolbar_pressed_inside", False)
            for button in buttons:
                button_key = (button.get("track_id"), button.get("key"))
                pix = self.w._toolbar_button_pixmap(
                    track,
                    button,
                    hovered=hover_key == button_key,
                    pressed=pressed_key == button_key and pressed_inside,
                )
                if not pix:
                    continue
                default_margin = float(getattr(self, "toggle_margin", 0.0) or 0.0)
                margin_x = button.get("margin_x", button.get("margin", default_margin))
                margin_y = button.get("margin_y", button.get("margin", default_margin))
                draw_x = button["rect"].x() + margin_x
                draw_y = button["rect"].y() + margin_y
                painter.drawPixmap(QPointF(draw_x, draw_y), pix)
        painter.restore()
