"""
 @file
 @brief Dock/undock behaviour shared by the main window
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

import functools
import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QApplication, QDockWidget, QWidget

from classes.logger import log
from classes.title_bar import HiddenTitleBar


class DockingMixin:
    """Undock/re-dock behaviour for a QMainWindow.

    Kept separate from MainWindow so it can be exercised on its own (see
    tests/test_dock_redock.py). Mix into a QMainWindow subclass that also
    provides style_dock_widgets().
    """

    # Where a panel goes when it's docked back and we have no better idea
    # (i.e. it was floating before we ever saw it docked).
    FALLBACK_DOCK_AREAS = {
        "dockTimeline": Qt.BottomDockWidgetArea,
        "dockFiles": Qt.TopDockWidgetArea,
        "dockVideo": Qt.TopDockWidgetArea,
        "dockTransitions": Qt.TopDockWidgetArea,
        "dockEffects": Qt.TopDockWidgetArea,
        "dockEmojis": Qt.TopDockWidgetArea,
        "dockProperties": Qt.LeftDockWidgetArea,
        "dockCaptionEditor": Qt.BottomDockWidgetArea,
    }

    # Last dock area of each dock widget, by objectName
    last_dock_areas = None

    def getDocks(self):
        """ Get a list of all dockable widgets """
        return self.findChildren(QDockWidget)

    def connect_dock_signals(self):
        """Track and restyle dock widgets as the user moves them around."""
        if self.last_dock_areas is None:
            self.last_dock_areas = {}
        for dock_widget in self.getDocks():
            dock_widget.dockLocationChanged.connect(
                functools.partial(self._on_dock_location_changed, dock_widget))
            # Floating/docking changes which title bar a dock needs, and is
            # not covered by dockLocationChanged
            dock_widget.topLevelChanged.connect(
                functools.partial(self._on_dock_top_level_changed, dock_widget))

    # ── Remembering where panels live ─────────────────────────────────────

    def dock_area_of(self, dock):
        """Current dock area of a dock widget (NoDockWidgetArea when floating)."""
        if dock.isFloating():
            return Qt.NoDockWidgetArea
        return self.dockWidgetArea(dock)

    def remember_dock_area(self, dock, area=None):
        """Record where a dock is docked, so it can be sent back there later."""
        if self.last_dock_areas is None:
            self.last_dock_areas = {}
        if area is None:
            area = self.dock_area_of(dock)
        if area and area != Qt.NoDockWidgetArea:
            self.last_dock_areas[dock.objectName()] = area

    def snapshot_dock_areas(self):
        """Refresh the remembered dock area of every currently docked panel."""
        for dock in self.getDocks():
            self.remember_dock_area(dock)

    def fallback_dock_area(self, dock):
        """Pick an allowed dock area for a panel with no remembered position."""
        allowed = dock.allowedAreas()
        area = self.FALLBACK_DOCK_AREAS.get(dock.objectName())
        if area and allowed & area:
            return area
        for area in (
            Qt.RightDockWidgetArea,
            Qt.LeftDockWidgetArea,
            Qt.TopDockWidgetArea,
            Qt.BottomDockWidgetArea,
        ):
            if allowed & area:
                return area
        return Qt.NoDockWidgetArea

    # ── Docking panels back in ────────────────────────────────────────────

    def redock_widget(self, dock):
        """Dock a floating panel back into the main window.

        Qt's own setFloating(False) silently does nothing when it has no
        remembered position to restore (for example after removeDockWidget, or
        for a panel that has only ever been floating), which leaves the panel
        stranded as a separate window. Fall back to an explicit dock area in
        that case.
        """
        if not dock or dock.allowedAreas() == Qt.NoDockWidgetArea:
            # Float-only panels (e.g. the tutorial dock) can't be docked
            return
        area = Qt.NoDockWidgetArea
        if self.last_dock_areas:
            area = self.last_dock_areas.get(dock.objectName(), Qt.NoDockWidgetArea)
        if area == Qt.NoDockWidgetArea or not (dock.allowedAreas() & area):
            area = self.fallback_dock_area(dock)

        dock.setFloating(False)
        if dock.isFloating() and area != Qt.NoDockWidgetArea:
            # No position for Qt to restore — assign one, then dock into it
            # (addDockWidget() on its own leaves the panel floating)
            log.debug("Re-docking %s into area %s", dock.objectName(), int(area))
            self.addDockWidget(area, dock)
            dock.setFloating(False)
        dock.setVisible(True)
        dock.raise_()
        self.style_dock_widgets()

    def redock_all_widgets(self):
        """Dock every floating panel back into the main window."""
        for dock in self.getDocks():
            if dock.isFloating():
                self.redock_widget(dock)

    # ── Title bars ────────────────────────────────────────────────────────

    @staticmethod
    def needs_qt_titlebar_when_floating():
        """True on platforms where native window frames break re-docking.

        macOS forwards non-client-area (window frame) mouse events to Qt, so a
        floating dock with native decorations can still be dragged back into the
        main window. Windows and Linux window managers keep those events to
        themselves, so on those platforms floating docks need a Qt-drawn title
        bar to stay dockable.
        """
        return sys.platform != "darwin"

    def dock_titlebar_kind(self, dock_widget, is_cosmic=False):
        """Which title bar a dock widget should have in its current state."""
        if dock_widget.isFloating():
            if (dock_widget.allowedAreas() == Qt.NoDockWidgetArea
                    or not self.needs_qt_titlebar_when_floating()):
                # Float-only panels (tutorial), and macOS where the native
                # title bar can already be dragged back into the window
                return "native"
            # Keep floating panels frameless so Qt (not the window manager)
            # owns the title bar and they can be dragged back in
            return "floating"
        if dock_widget.objectName() == "dockTimeline":
            # Hide title bar for timeline widget (ALL themes)
            return "hidden"
        if is_cosmic:
            # Keep mandatory float/close actions visible for docked widgets
            return "cosmic"
        # Standard Qt dock title bar (float, close)
        return "native"

    def set_dock_titlebar(self, dock_widget, kind):
        """Give a dock the requested title bar, replacing it only when it changes.

        Re-creating title bars on every call would churn widgets (and can
        disturb an in-progress drag), so each dock remembers which kind it has.
        """
        if dock_widget.property("zenvi_titlebar_kind") == kind:
            return

        if kind == "native":
            titlebar = None
        elif kind == "hidden":
            titlebar = QWidget()
        elif kind == "floating":
            titlebar = HiddenTitleBar(
                dock_widget, title_text=dock_widget.windowTitle(),
                show_buttons=True, floating=True)
        else:
            titlebar = HiddenTitleBar(dock_widget, show_buttons=True)

        previous = dock_widget.titleBarWidget()
        dock_widget.setTitleBarWidget(titlebar)
        dock_widget.setProperty("zenvi_titlebar_kind", kind)
        if previous is not None and previous is not titlebar:
            # setTitleBarWidget() doesn't delete the old bar, it only drops it
            # from the layout — clean it up ourselves
            previous.deleteLater()

    def apply_dock_titlebars(self, is_cosmic=False):
        """Apply the right title bar to every dock widget."""
        # Keep track of where docked panels live, for docking floating ones back
        self.snapshot_dock_areas()
        for dock_widget in self.getDocks():
            self.set_dock_titlebar(
                dock_widget, self.dock_titlebar_kind(dock_widget, is_cosmic))

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_dock_location_changed(self, dock_widget, area):
        """Remember the new dock area, then restyle title bars."""
        self.remember_dock_area(dock_widget, area)
        self.style_dock_widgets()

    def _on_dock_top_level_changed(self, dock_widget, floating):
        """Restyle title bars when a dock is floated or docked."""
        self._restyle_docks_when_idle()

    def _restyle_docks_when_idle(self, attempts=0):
        """Restyle docks once any in-progress drag has finished.

        Qt floats a dock as soon as the user starts dragging it out, and
        swapping its title bar mid-drag would interrupt the drag — so wait for
        the mouse button to be released first (giving up after ~3s in case no
        release ever reaches us).
        """
        if QApplication.mouseButtons() & Qt.LeftButton and attempts < 60:
            QTimer.singleShot(
                50, functools.partial(self._restyle_docks_when_idle, attempts + 1))
            return
        self.style_dock_widgets()
