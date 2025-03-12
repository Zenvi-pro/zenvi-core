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

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QColorDialog, QPushButton, QDialog, QWidget, QFrame
from PyQt5.QtGui import QColor, QPainter, QPen, QCursor, QBrush
from PyQt5.QtCore import Qt, QRect, QPoint
from classes.logger import log
from classes.app import get_app


def draw_checkerboard(painter, rect):
    """Draw a checkerboard pattern for transparent backgrounds."""
    # Use logical pixels for the checker size that scales with different DPIs
    scale = get_app().devicePixelRatio()
    effective_checker_size = 16 / scale
    light_color = QColor(220, 220, 220)
    dark_color = QColor(170, 170, 170)

    # Save current brush
    old_brush = painter.brush()

    # Draw checkerboard pattern in logical coordinates
    checker_size = int(effective_checker_size)
    for x in range(0, rect.width(), checker_size):
        for y in range(0, rect.height(), checker_size):
            if ((x // checker_size) + (y // checker_size)) % 2 == 0:
                color = light_color
            else:
                color = dark_color
            painter.fillRect(
                rect.x() + x,
                rect.y() + y,
                min(checker_size, rect.width() - x),
                min(checker_size, rect.height() - y),
                color
            )

    # Restore brush
    painter.setBrush(old_brush)

class BlockPaintFilter(QtCore.QObject):
    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.Paint:
            # Block the paint event so the widget doesn’t draw its own contents.
            return True
        return super().eventFilter(obj, event)


class ColorPicker(QColorDialog):
    def __init__(self, initial_color, parent=None, title=None, callback=None, *args, **kwargs):
        # Initialize with parent only first
        super().__init__(parent=parent, *args, **kwargs)
        self.parent_window = parent
        self.callback = callback
        self.picked_pixmap = None
        self.hover_color = initial_color  # Track the color being hovered

        # Keep track of the dialog dimensions to handle resizing
        self.dialog_width = self.width()
        self.dialog_height = self.height()

        if title:
            self.setWindowTitle(title)

        # Enable alpha channel and disable native dialog
        self.setOption(QColorDialog.DontUseNativeDialog)
        self.setOption(QColorDialog.ShowAlphaChannel)

        # Set the current color after options are set
        self.setCurrentColor(initial_color)
        self.colorSelected.connect(self.on_color_selected)
        self.currentColorChanged.connect(self.on_current_color_changed)

        # Override the "Pick Screen Color" button signal
        self._override_pick_screen_color()

        # Automatically open the dialog
        self.open()

        # Calculate preview rectangle positions based on dialog size
        self.update_preview_rectangles()

    def update_preview_rectangles(self):
        """Update the positions of preview rectangles based on current dialog size"""
        all_frames = self.findChildren(QFrame)
        # Filter to exact QFrame instances (not subclasses)
        exact_frames = [w for w in all_frames if type(w) is QFrame]
        preview_frame = exact_frames[-1]
        if preview_frame:
            # Map the top-left (0,0) of preview_frame to the dialog's coordinate system
            topLeft = preview_frame.mapTo(self, QPoint(0, 0))
            self.current_color_rect = QRect(topLeft, preview_frame.size())

            # Install our paint blocking filter on the preview frame
            filter = BlockPaintFilter(preview_frame)
            preview_frame.installEventFilter(filter)
            self._preview_paint_filter = filter

    def resizeEvent(self, event):
        """Handle window resize events to update preview rectangle positions"""
        super().resizeEvent(event)
        #self.update_preview_rectangles()
        self.update()  # Force repaint

    def on_current_color_changed(self, color):
        """Track when the current color changes (hover or selection)"""
        self.hover_color = color
        self.update()  # Trigger a repaint

    def paintEvent(self, event):
        """Override paintEvent to add checkerboard pattern to color preview areas"""
        # Call the parent class's paintEvent first
        super().paintEvent(event)

        # Create painter for our custom drawing
        painter = QPainter(self)

        # Draw checkerboard pattern under the current color
        draw_checkerboard(painter, self.current_color_rect)

        # Draw the current color with its alpha over the checkerboard
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.fillRect(self.current_color_rect, self.currentColor())

        painter.end()

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

            # Create preview rectangle
            preview_rect = QRect(preview_x, preview_y, preview_size, preview_size)

            # Draw checkerboard pattern for transparency
            draw_checkerboard(painter, preview_rect)

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
