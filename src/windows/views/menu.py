"""
 @file
 @brief This file creates a styled QMenu (which supports border radius and border color)
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2024 OpenShot Studios, LLC
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

import logging
import re
import functools

from qt_api import QMenu, isdeleted
from qt_api import QPainter, QPen, QColor
from qt_api import Qt, QRectF
from classes.app import get_app


logger = logging.getLogger(__name__)


class StyledContextMenu(QMenu):
    def __init__(self, title=None, parent=None):
        super().__init__(title, parent)
        self.app = get_app()
        self.border = self.get_border()
        self.border_radius = self.get_border_radius()

    def show_at(self, event_or_pos):
        """Show the menu at a position or context menu event."""
        pos = event_or_pos
        if hasattr(event_or_pos, "globalPosition"):
            try:
                pos = event_or_pos.globalPosition().toPoint()
            except Exception:
                pos = event_or_pos
        if hasattr(event_or_pos, "globalPos"):
            try:
                pos = event_or_pos.globalPos()
            except Exception:
                pos = event_or_pos
        exec_fn = getattr(self, "exec", None) or getattr(self, "exec_", None)
        if exec_fn:
            exec_fn(pos)
        else:
            self.popup(pos)

    def get_border(self):
        """Parses border width and color from app.styleSheet()"""
        pattern = r'QMenu\s*{\s*[^}]*border:\s*([^;]+);'
        match = re.search(pattern, self.app.styleSheet(), re.IGNORECASE)
        if match:
            border_parts = match.group(1).split()
            # Typically, border is defined as width style color
            if len(border_parts) >= 3:
                width = float(border_parts[0].replace('px', ''))  # Remove 'px' and convert to float
                style = border_parts[1]
                color = QColor(border_parts[2])
                return {'width': width, 'style': style, 'color': color}
        return None

    def get_border_radius(self):
        """Parses border radius from app.styleSheet()"""
        pattern = r'QMenu\s*{\s*[^}]*border-radius:\s*([^;]+);'
        match = re.search(pattern, self.app.styleSheet(), re.IGNORECASE)
        if match:
            # Split the radius values by whitespace and remove 'px' unit
            radius_values = match.group(1).replace('px', '').split()
            if len(radius_values) == 1:
                radius = float(radius_values[0])
                return {'x': radius, 'y': radius}
            if len(radius_values) == 2:
                return {'x': float(radius_values[0]), 'y': float(radius_values[1])}
        return None

    def paintEvent(self, event):
        """Paint optional borders and corner radius on QMenu"""
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self.border:
            pen = QPen(self.border.get('color'), self.border.get('width'))
            painter.setPen(pen)

        if self.border_radius:
            rect = QRectF(0, 0, self.width(), self.height())
            painter.drawRoundedRect(rect, self.border_radius.get('x'), self.border_radius.get('y'), Qt.AbsoluteSize)

        painter.end()


def _resolve_live_owner(owner):
    """Resolve a live QObject owner for menu action metadata/trigger lookup."""
    if owner is None:
        return get_app().window
    try:
        if not isdeleted(owner):
            return owner
    except Exception:
        return get_app().window
    return get_app().window


def add_bound_action(menu, owner, action_name, fallback_text, callback=None, enabled=None):
    """Add a fresh menu-owned action and resolve the current slot at trigger time."""
    owner = _resolve_live_owner(owner)
    source_action = getattr(owner, action_name, None)
    label = fallback_text
    icon = None
    if source_action is not None:
        try:
            if not isdeleted(source_action):
                label = source_action.text() or fallback_text
                icon = source_action.icon()
                if enabled is None:
                    enabled = source_action.isEnabled()
        except Exception as exc:
            logger.debug("menu: failed to read source action metadata for %s: %s", action_name, exc, exc_info=True)
    action = menu.addAction(icon, label) if icon and not icon.isNull() else menu.addAction(label)
    if enabled is not None:
        action.setEnabled(bool(enabled))

    def trigger_slot():
        live_owner = _resolve_live_owner(owner)
        live_callback = callback
        if isinstance(callback, str):
            live_callback = getattr(live_owner, callback, None)
        if live_callback is None:
            live_callback = getattr(live_owner, f"{action_name}_trigger", None)
        if live_callback is None:
            raise AttributeError(f"Unable to resolve callback for {action_name}")
        live_callback()

    action.triggered.connect(lambda checked=False: trigger_slot())
    return action


def keyframe_bezier_presets():
    _ = get_app()._tr
    return [
        (0.250, 0.100, 0.250, 1.000, _("Ease (Default)")),
        (0.420, 0.000, 1.000, 1.000, _("Ease In")),
        (0.000, 0.000, 0.580, 1.000, _("Ease Out")),
        (0.420, 0.000, 0.580, 1.000, _("Ease In/Out")),
        (0.550, 0.085, 0.680, 0.530, _("Ease In (Quad)")),
        (0.550, 0.055, 0.675, 0.190, _("Ease In (Cubic)")),
        (0.895, 0.030, 0.685, 0.220, _("Ease In (Quart)")),
        (0.755, 0.050, 0.855, 0.060, _("Ease In (Quint)")),
        (0.470, 0.000, 0.745, 0.715, _("Ease In (Sine)")),
        (0.950, 0.050, 0.795, 0.035, _("Ease In (Expo)")),
        (0.600, 0.040, 0.980, 0.335, _("Ease In (Circ)")),
        (0.600, -0.280, 0.735, 0.045, _("Ease In (Back)")),
        (0.250, 0.460, 0.450, 0.940, _("Ease Out (Quad)")),
        (0.215, 0.610, 0.355, 1.000, _("Ease Out (Cubic)")),
        (0.165, 0.840, 0.440, 1.000, _("Ease Out (Quart)")),
        (0.230, 1.000, 0.320, 1.000, _("Ease Out (Quint)")),
        (0.390, 0.575, 0.565, 1.000, _("Ease Out (Sine)")),
        (0.190, 1.000, 0.220, 1.000, _("Ease Out (Expo)")),
        (0.075, 0.820, 0.165, 1.000, _("Ease Out (Circ)")),
        (0.175, 0.885, 0.320, 1.275, _("Ease Out (Back)")),
        (0.455, 0.030, 0.515, 0.955, _("Ease In/Out (Quad)")),
        (0.645, 0.045, 0.355, 1.000, _("Ease In/Out (Cubic)")),
        (0.770, 0.000, 0.175, 1.000, _("Ease In/Out (Quart)")),
        (0.860, 0.000, 0.070, 1.000, _("Ease In/Out (Quint)")),
        (0.445, 0.050, 0.550, 0.950, _("Ease In/Out (Sine)")),
        (1.000, 0.000, 0.000, 1.000, _("Ease In/Out (Expo)")),
        (0.785, 0.135, 0.150, 0.860, _("Ease In/Out (Circ)")),
        (0.680, -0.550, 0.265, 1.550, _("Ease In/Out (Back)")),
    ]


def populate_keyframe_context_menu(
    menu,
    *,
    bezier_callback,
    linear_callback,
    constant_callback,
    remove_callback=None,
    remove_enabled=True,
    bezier_icon=None,
    linear_icon=None,
    constant_icon=None,
    remove_text=None,
):
    _ = get_app()._tr

    bez_menu = menu.addMenu(bezier_icon, _("Bezier")) if bezier_icon else menu.addMenu(_("Bezier"))
    for preset in keyframe_bezier_presets():
        action = bez_menu.addAction(preset[4])
        action.triggered.connect(functools.partial(bezier_callback, preset))

    linear_action = menu.addAction(linear_icon, _("Linear")) if linear_icon else menu.addAction(_("Linear"))
    linear_action.triggered.connect(linear_callback)

    constant_action = menu.addAction(constant_icon, _("Constant")) if constant_icon else menu.addAction(_("Constant"))
    constant_action.triggered.connect(constant_callback)

    if remove_callback is not None:
        menu.addSeparator()
        remove_action = menu.addAction(remove_text or _("Remove Keyframe"))
        remove_action.setEnabled(bool(remove_enabled))
        remove_action.triggered.connect(remove_callback)
        return remove_action
    return None
