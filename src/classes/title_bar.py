"""
 @file
 @brief This file contains a custom title bar used by dock widgets
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

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton, QDockWidget


class HiddenTitleBar(QWidget):
    """A Qt-drawn title bar for a QDockWidget.

    Besides theming, this is also what keeps *floating* docks re-dockable on
    Windows and Linux. A floating QDockWidget with no title bar widget gets
    native window-manager decorations, and drags on a native title bar are
    never delivered to Qt on those platforms — so the panel can no longer be
    dragged back into the main window and behaves like a separate app window.
    With a Qt-drawn title bar the floating dock stays frameless, and Qt handles
    the drag (and double-click) itself, matching how macOS already behaves.
    """

    def __init__(self, dock_widget, title_text="", show_buttons=False, floating=False):
        super().__init__()
        self.dock_widget = dock_widget
        self.floating = floating
        self.setObjectName("dock-title-bar")

        # Set up a horizontal layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Add a QLabel for the title (optional, based on title_text)
        self.title_label = QLabel(title_text)
        if title_text:
            self.title_label.setObjectName("dock-title-label")
        else:
            self.title_label.setObjectName("dock-title-handle")
        layout.addWidget(self.title_label)

        self.float_btn = None
        if show_buttons:
            layout.addStretch()

            # Float / dock button (re-docks the panel when it's floating)
            self.float_btn = QPushButton("⤓" if floating else "⧉")
            self.float_btn.setObjectName("dock-float-button")
            self.float_btn.setFixedSize(18, 18)
            self.float_btn.setFlat(True)
            self.float_btn.setToolTip("Dock" if floating else "Float")
            self.float_btn.clicked.connect(self.toggle_floating)
            layout.addWidget(self.float_btn)

            # Close button (only for docks the user is allowed to close)
            if dock_widget.features() & QDockWidget.DockWidgetClosable:
                close_btn = QPushButton("✕")
                close_btn.setObjectName("dock-close-button")
                close_btn.setFixedSize(18, 18)
                close_btn.setFlat(True)
                close_btn.setToolTip("Close")
                close_btn.clicked.connect(dock_widget.hide)
                layout.addWidget(close_btn)

        if floating:
            self.setToolTip("Drag or double-click this bar to dock the panel")

        # Keep title in sync with dock widget
        self.dock_widget.windowTitleChanged.connect(self.update_title)

        # Collapse to zero height when no title text — avoids blank whitespace above content
        if floating:
            self.setFixedHeight(24)
        else:
            self.setFixedHeight(0 if (not title_text and not show_buttons) else 20)

    def update_title(self, text):
        """Update label text when dock title changes."""
        self.title_label.setText(text)

    def toggle_floating(self):
        """Float the panel, or dock it back if it's already floating."""
        if self.dock_widget.isFloating():
            self.redock()
        else:
            self.dock_widget.setFloating(True)

    def redock(self):
        """Dock the panel back into the main window."""
        window = self.dock_widget.parent()
        redock_widget = getattr(window, "redock_widget", None)
        if callable(redock_widget):
            # Main window knows where this dock came from
            redock_widget(self.dock_widget)
        else:
            self.dock_widget.setFloating(False)

    def mouseDoubleClickEvent(self, event):
        """Toggle floating, like Qt's own dock title bar does."""
        if event.button() == Qt.LeftButton:
            self.toggle_floating()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        """Ignore presses, so the QDockWidget starts its own (dockable) drag."""
        event.ignore()

    def mouseMoveEvent(self, event):
        """Ignore moves, so the QDockWidget drives the drag."""
        event.ignore()

    def mouseReleaseEvent(self, event):
        """Ignore releases, so the QDockWidget can finish the drag."""
        event.ignore()
