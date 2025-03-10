"""
 @file
 @brief A modal Qt color picker dialog launcher, which works in Wayland
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

from PyQt5.QtWidgets import QColorDialog, QPushButton, QDialog
from PyQt5.QtGui import QColor, QPainter, QPen, QCursor
from PyQt5.QtCore import Qt, QRect
from classes.logger import log
from classes.app import get_app


class ColorPicker(QColorDialog):
    def __init__(self, initial_color, parent=None, title=None, callback=None, *args, **kwargs):
        # Initialize with parent only first
        super().__init__(parent=parent, *args, **kwargs)
        self.parent_window = parent
        self.callback = callback
        self.picked_pixmap = None

        if title:
            self.setWindowTitle(title)

        # Enable alpha channel and disable native dialog
        self.setOption(QColorDialog.DontUseNativeDialog)
        self.setOption(QColorDialog.ShowAlphaChannel)

        # Set the current color after options are set
        self.setCurrentColor(initial_color)
        self.colorSelected.connect(self.on_color_selected)

        # Override the "Pick Screen Color" button signal
        self._override_pick_screen_color()

        # Automatically open the dialog
        self.open()

    def _override_pick_screen_color(self):
        # Get first pushbutton (color picker)
        color_picker_button = self.findChildren(QPushButton)[0]

        # Connect to button signals
        color_picker_button.clicked.disconnect()
        color_picker_button.clicked.connect(self.start_color_picking)

    def start_color_picking(self):
        if self.parent_window:
            self.picked_pixmap = get_app().window.grab()
            self._show_picking_dialog()
        else:
            log.error("No parent window available for color picking")

    def _show_picking_dialog(self):
        dialog = PickingDialog(self.picked_pixmap, self)
        dialog.exec_()  # Show modal dialog
        self.raise_()

    def on_color_selected(self, color):
        if self.callback:
            self.callback(color)

class PickingDialog(QDialog):
    def __init__(self, pixmap, color_picker, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pixmap = pixmap
        self.device_pixel_ratio = pixmap.devicePixelRatio()
        self.color_picker = color_picker
        self.setWindowModality(Qt.WindowModal)
        self.setGeometry(get_app().window.geometry())
        self.setFixedSize(self.size())
        self.setCursor(Qt.CrossCursor)
        self.color_preview = QColor("#FFFFFF")
        self.setMouseTracking(True)

        # Get first pushbutton (color picker)
        color_picker_button = self.color_picker.findChildren(QPushButton)[0]
        self.setWindowTitle(f"OpenShot: {color_picker_button.text().replace('&', '')}")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawPixmap(self.rect(), self.pixmap)

        # Draw color preview rectangle near the cursor
        if self.color_preview:
            cursor_pos = self.mapFromGlobal(QCursor.pos())
            preview_x = cursor_pos.x() + 15
            preview_y = cursor_pos.y() + 15
            preview_size = 50

            # Draw checkerboard pattern for transparency
            preview_rect = QRect(preview_x, preview_y, preview_size, preview_size)

            # Draw the color with proper alpha over the checkerboard
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.fillRect(preview_rect, self.color_preview)

            # Draw border around preview
            pen = QPen(Qt.black, 2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(preview_x, preview_y, preview_size, preview_size)

        painter.end()

    def mouseMoveEvent(self, event):
        if self.pixmap:
            image = self.pixmap.toImage()
            # Scale the coordinates for High DPI displays
            scaled_x = int(event.x() * self.device_pixel_ratio)
            scaled_y = int(event.y() * self.device_pixel_ratio)
            if 0 <= scaled_x < image.width() and 0 <= scaled_y < image.height():
                pixel = image.pixel(scaled_x, scaled_y)
                color = QColor(pixel)
                # Ensure alpha channel is preserved in preview
                # Get alpha value from pixel (first 8 bits are alpha in ARGB format)
                alpha = (pixel >> 24) & 0xff
                color.setAlpha(alpha)
                self.color_preview = color

                # Force update display
                self.update()

    def mousePressEvent(self, event):
        if self.pixmap:
            image = self.pixmap.toImage()
            # Scale the coordinates for High DPI displays
            scaled_x = int(event.x() * self.device_pixel_ratio)
            scaled_y = int(event.y() * self.device_pixel_ratio)
            if 0 <= scaled_x < image.width() and 0 <= scaled_y < image.height():
                pixel = image.pixel(scaled_x, scaled_y)
                color = QColor(pixel)
                # Ensure alpha channel is preserved
                # Get alpha value from pixel (first 8 bits are alpha in ARGB format)
                alpha = (pixel >> 24) & 0xff
                color.setAlpha(alpha)

                # Set the selected color in the dialog
                self.color_picker.setCurrentColor(color)
        self.accept()  # Close the dialog
