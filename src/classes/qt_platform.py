"""
 @file
 @brief Qt platform-plugin selection, decided before QApplication exists
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

import sys

# Deliberately free of PyQt5 imports: this module runs before PyQt5 is
# imported, because QT_QPA_PLATFORM is read when QApplication is constructed.


def should_force_xcb(env, platform=None):
    """True when we should ask Qt for the xcb plugin instead of wayland.

    Qt 5's Wayland plugin refuses to grab the pointer for ordinary (non-popup)
    windows, cannot position a top-level window, and synthesises global mouse
    coordinates from a position the compositor never honoured. QDockWidget's
    drag-to-dock needs all three, so on a Wayland session a panel dragged out
    of the main window cannot be dragged back in — see classes/docking.py.
    XWayland has none of those limits.

    Only applies when the user hasn't picked a platform themselves
    (QT_QPA_PLATFORM), and only when XWayland is actually reachable — forcing
    xcb with no X server would leave the app unable to start at all.
    """
    if platform is None:
        platform = sys.platform
    if not platform.startswith("linux"):
        return False
    if env.get("QT_QPA_PLATFORM"):
        # User (or packaging) already chose — don't second-guess it
        return False
    if env.get("XDG_SESSION_TYPE", "").lower() != "wayland" and not env.get("WAYLAND_DISPLAY"):
        return False
    # No X server to fall back to (Wayland without XWayland)
    return bool(env.get("DISPLAY"))


def select_qt_platform(env, platform=None):
    """Apply should_force_xcb() to the given environment mapping.

    Returns the platform plugin we forced, or None if we left the choice to Qt.
    """
    if not should_force_xcb(env, platform):
        return None
    env["QT_QPA_PLATFORM"] = "xcb"
    return "xcb"
