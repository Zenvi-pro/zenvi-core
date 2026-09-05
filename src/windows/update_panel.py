"""
 @file
 @brief Popup panel showing auto-update details — version, download progress,
        and the action to restart and install.
 @author Zenvi Team

 @section LICENSE

 Copyright (c) 2008-2026 Zenvi.
 This file is part of Zenvi Video Editor (https://zenvi.pro).

 Zenvi is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
"""

import webbrowser

from PyQt5.QtCore import Qt, QSize, QPoint
from PyQt5.QtWidgets import (
    QFrame, QLabel, QPushButton, QProgressBar, QHBoxLayout, QVBoxLayout,
)

from classes.logger import log
from windows.update_status_button import (
    STATE_AVAILABLE, STATE_DOWNLOADING, STATE_READY, STATE_FAILED,
    COLOR_ACCENT, COLOR_SUCCESS, COLOR_WARNING, svg_icon,
)


DOWNLOAD_URL = "https://zenvi.pro/download"

PANEL_WIDTH = 320
GLYPH_SIZE = 20


def human_bytes(count):
    """Format a byte count for display (e.g. 118.4 MB)."""
    try:
        count = float(count)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            precision = 0 if unit in ("B", "KB") else 1
            return f"{count:.{precision}f} {unit}"
        count /= 1024
    return ""


class UpdatePanel(QFrame):
    """Frameless popup anchored under the update toolbar button.

    Closes when the user clicks elsewhere (Qt.Popup) and mirrors whatever state
    the toolbar button is in, so progress stays live while it is open."""

    def __init__(self, window):
        super().__init__(window, Qt.Popup | Qt.FramelessWindowHint)

        self.window = window
        self.state = STATE_AVAILABLE
        self.version = ""

        self.setObjectName("updatePanel")
        self.setFixedWidth(PANEL_WIDTH)
        self.setStyleSheet(self._stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(4)

        # Headline row — state glyph + title
        header = QHBoxLayout()
        header.setSpacing(10)
        self.glyph = QLabel(self)
        self.glyph.setFixedSize(QSize(GLYPH_SIZE, GLYPH_SIZE))
        self.title = QLabel(self)
        self.title.setObjectName("updatePanelTitle")
        self.title.setWordWrap(True)
        header.addWidget(self.glyph, 0, Qt.AlignTop)
        header.addWidget(self.title, 1)
        layout.addLayout(header)

        # Muted detail line (size / progress)
        self.detail = QLabel(self)
        self.detail.setObjectName("updatePanelDetail")
        self.detail.setContentsMargins(GLYPH_SIZE + 10, 0, 0, 0)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        layout.addSpacing(10)

        self.progress = QProgressBar(self)
        self.progress.setFixedHeight(4)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        layout.addSpacing(12)

        self.primary = QPushButton(self)
        self.primary.setObjectName("updatePanelPrimary")
        self.primary.setCursor(Qt.PointingHandCursor)
        self.primary.setFixedHeight(32)
        self.primary.clicked.connect(self._on_primary)
        layout.addWidget(self.primary)

        self.later = QPushButton(self._tr("Later"), self)
        self.later.setObjectName("updatePanelLater")
        self.later.setCursor(Qt.PointingHandCursor)
        self.later.setFlat(True)
        self.later.setFixedHeight(24)
        self.later.clicked.connect(self.close)
        layout.addWidget(self.later)

        self._glyphs = {
            STATE_AVAILABLE: svg_icon("themes/cosmic/images/warning.svg", GLYPH_SIZE),
            STATE_DOWNLOADING: svg_icon("themes/cosmic/images/update-download.svg", GLYPH_SIZE),
            STATE_READY: svg_icon("themes/cosmic/images/update-ready.svg", GLYPH_SIZE),
            STATE_FAILED: svg_icon("themes/cosmic/images/warning.svg", GLYPH_SIZE),
        }

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------

    def sync_from_button(self, button):
        """Mirror the toolbar button's current state into the panel."""
        self.state = button.state
        self.version = button.version

        self.glyph.setPixmap(
            self._glyphs.get(self.state).pixmap(GLYPH_SIZE, GLYPH_SIZE))

        if self.state == STATE_DOWNLOADING:
            self._show_downloading(button.percent, button.downloaded, button.total)
        elif self.state == STATE_READY:
            self._show_ready(button.total or button.downloaded)
        elif self.state == STATE_FAILED:
            self._show_failed()
        else:
            self._show_available()

    def _show_available(self):
        self.title.setText(
            self._tr("Zenvi %s is available") % self.version)
        self.detail.setText(self._tr("Not downloaded yet"))
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.primary.setText(self._tr("Download from zenvi.pro"))
        self.primary.setEnabled(True)
        self._accent(COLOR_WARNING)

    def _show_downloading(self, percent, downloaded, total):
        self.title.setText(
            self._tr("Downloading Zenvi %s") % self.version)

        if percent < 0:
            # Size unknown — indeterminate bar, matching the toolbar pill
            self.progress.setRange(0, 0)
            got = human_bytes(downloaded)
            self.detail.setText(
                self._tr("Downloaded %s so far") % got if got
                else self._tr("Starting download…"))
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
            self.detail.setText(self._tr("%s of %s · %d%%") % (
                human_bytes(downloaded), human_bytes(total), percent))

        self.progress.setVisible(True)
        self.primary.setText(self._tr("Downloading…"))
        self.primary.setEnabled(False)
        self._accent(COLOR_ACCENT)

    def _show_ready(self, size):
        self.title.setText(
            self._tr("Zenvi %s is ready to install") % self.version)
        readable = human_bytes(size)
        self.detail.setText(
            self._tr("Downloaded · %s") % readable if readable
            else self._tr("Downloaded and verified"))
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.progress.setVisible(True)
        self.primary.setText(self._tr("Restart to Update"))
        self.primary.setEnabled(True)
        self._accent(COLOR_SUCCESS)

    def _show_failed(self):
        self.title.setText(
            self._tr("Zenvi %s is available") % self.version)
        self.detail.setText(self._tr("The download failed — try downloading it manually"))
        self.progress.setVisible(False)
        self.primary.setText(self._tr("Download from zenvi.pro"))
        self.primary.setEnabled(True)
        self._accent(COLOR_WARNING)

    def _accent(self, color):
        """Recolor the progress chunk and primary button for the current state."""
        self.progress.setStyleSheet(
            "QProgressBar { background: #1a1a1a; border: none; border-radius: 2px; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}"
        )
        self.primary.setStyleSheet(
            "QPushButton#updatePanelPrimary {"
            f"  background-color: {color};"
            "   color: #0f0f0f;"
            "   border: none;"
            "   border-radius: 6px;"
            "   font-weight: 600;"
            "   font-size: 12px;"
            "}"
            "QPushButton#updatePanelPrimary:hover {"
            f"  background-color: {self._lighten(color)};"
            "}"
            "QPushButton#updatePanelPrimary:disabled {"
            "   background-color: #2a2a2a;"
            "   color: #6b6b6b;"
            "}"
        )

    @staticmethod
    def _lighten(hex_color, amount=24):
        """Nudge a #rrggbb color brighter for the hover state."""
        try:
            raw = hex_color.lstrip("#")
            parts = [min(255, int(raw[i:i + 2], 16) + amount) for i in (0, 2, 4)]
            return "#%02x%02x%02x" % tuple(parts)
        except (ValueError, IndexError):
            return hex_color

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_primary(self):
        if self.state == STATE_READY:
            self.close()
            # Reuse the existing restart path: closeEvent relaunches the app and
            # launch.py applies the staged update before Qt loads
            self.window._restart_for_update = True
            self.window.close()
            return

        self.close()
        try:
            webbrowser.open(DOWNLOAD_URL, new=1)
        except Exception:
            log.error("Unable to open the Download url: %s", DOWNLOAD_URL, exc_info=True)

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    def show_under(self, widget):
        """Pop up anchored beneath *widget*, kept inside the screen."""
        self.adjustSize()
        below = widget.mapToGlobal(QPoint(0, widget.height() + 6))
        # Right-align with the button, which sits at the far right of the toolbar
        left = below.x() + widget.width() - self.width()

        screen = self.window.screen().availableGeometry() \
            if hasattr(self.window, "screen") else None
        if screen:
            left = max(screen.left() + 8,
                       min(left, screen.right() - self.width() - 8))

        self.move(QPoint(left, below.y()))
        self.show()
        self.raise_()

    def _stylesheet(self):
        return (
            "QFrame#updatePanel {"
            "   background-color: #161616;"
            "   border: 1px solid rgba(255, 255, 255, 0.08);"
            "   border-radius: 10px;"
            "}"
            "QLabel#updatePanelTitle {"
            "   color: #f2f2f2;"
            "   font-size: 13px;"
            "   font-weight: 600;"
            "   background: transparent;"
            "}"
            "QLabel#updatePanelDetail {"
            "   color: #8b8b8b;"
            "   font-size: 11px;"
            "   background: transparent;"
            "}"
            "QPushButton#updatePanelLater {"
            "   color: #8b8b8b;"
            "   background: transparent;"
            "   border: none;"
            "   font-size: 11px;"
            "}"
            "QPushButton#updatePanelLater:hover { color: #cfcfcf; }"
        )

    def _tr(self, text):
        try:
            from classes.app import get_app
            return get_app()._tr(text)
        except Exception:
            return text
