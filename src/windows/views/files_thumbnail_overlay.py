"""
 @file
 @brief Thumbnail overlays for the project files views (Optimize Preview badge).
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
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

import os

from qt_api import QRectF
from qt_api import QPainter
from qt_api import QSvgRenderer

from classes import info


_OPTIMIZE_PREVIEW_READY_ICON = "tool-optimize-preview.svg"
_OPTIMIZE_PREVIEW_MISSING_ICON = "tool-optimize-preview-missing.svg"


def paint_proxy_badge(painter, deco_rect, proxy_state):
    """Paint a bottom-right lightning badge for proxy-ready/missing files."""
    proxy_state = str(proxy_state or "").strip().lower()
    if proxy_state not in ("ready", "missing"):
        return
    if not deco_rect or not deco_rect.isValid():
        return

    icon_name = _OPTIMIZE_PREVIEW_MISSING_ICON if proxy_state == "missing" else _OPTIMIZE_PREVIEW_READY_ICON
    icon_path = os.path.join(info.PATH, "themes", "cosmic", "images", icon_name)
    if not os.path.exists(icon_path):
        return

    badge_size = max(14.0, min(deco_rect.width(), deco_rect.height()) * 0.24)
    margin_x = 1.5
    margin_y = 4.0
    glyph_rect = QRectF(
        deco_rect.right() - badge_size - margin_x,
        deco_rect.bottom() - badge_size - margin_y,
        badge_size,
        badge_size,
    )

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setOpacity(0.95)
    renderer = QSvgRenderer(icon_path)
    renderer.render(painter, glyph_rect)
    painter.restore()
