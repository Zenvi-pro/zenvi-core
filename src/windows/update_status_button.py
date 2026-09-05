"""
 @file
 @brief Toolbar button that reports auto-update state — available, downloading
        (with a live progress bar), ready to install, or failed.
 @author Zenvi Team

 @section LICENSE

 Copyright (c) 2008-2026 Zenvi.
 This file is part of Zenvi Video Editor (https://zenvi.pro).

 Zenvi is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
"""

import os

from PyQt5.QtCore import Qt, QSize, QTimer, QRectF
from PyQt5.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QApplication, QToolButton

from classes import info
from classes.logger import log


# Update states
STATE_HIDDEN = "hidden"
STATE_AVAILABLE = "available"
STATE_DOWNLOADING = "downloading"
STATE_READY = "ready"
STATE_FAILED = "failed"

# Palette — matches the cosmic theme's progress bar and accent colors
COLOR_ACCENT = "#4d9cf6"
COLOR_WARNING = "#f59e0b"
COLOR_SUCCESS = "#22c55e"
COLOR_TRACK = "#1a1a1a"
COLOR_SURFACE = "#252525"

ICON_SIZE = 16
PROGRESS_HEIGHT = 3

# Sliding-chunk animation for downloads of unknown size
INDETERMINATE_INTERVAL = 30  # ms
INDETERMINATE_STEP = 0.012   # fraction of the width per tick
INDETERMINATE_WIDTH = 0.30   # chunk width as a fraction of the track


def svg_icon(relative_path, px=ICON_SIZE):
    """Build a HiDPI QIcon from a theme SVG.

    Mirrors BaseTheme.create_svg_icon(), which needs a theme instance we may not
    have yet when the toolbar button is first constructed."""
    path = relative_path
    if not os.path.exists(path):
        path = os.path.join(info.PATH, relative_path)
    if not os.path.exists(path):
        log.warning("UpdateStatusButton: icon not found: %s", relative_path)
        return QIcon()

    app = QApplication.instance()
    ratio = app.devicePixelRatio() if app else 1.0
    renderer = QSvgRenderer(path)
    pixmap = QPixmap(QSize(px, px) * ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


class UpdateStatusButton(QToolButton):
    """Toolbar pill showing auto-update status.

    Drives the shared `actionUpdate` QAction so the Help menu entry stays in
    sync, and paints its own progress track along the bottom edge during a
    download — a nested QProgressBar would fight the toolbar's layout."""

    def __init__(self, window):
        super().__init__(window)

        self.window = window
        self.state = STATE_HIDDEN
        self.version = ""
        self.percent = 0
        self.downloaded = 0
        self.total = 0

        self._indeterminate_pos = 0.0
        self._indeterminate_timer = QTimer(self)
        self._indeterminate_timer.setInterval(INDETERMINATE_INTERVAL)
        self._indeterminate_timer.timeout.connect(self._advance_indeterminate)

        self._icons = {
            STATE_AVAILABLE: svg_icon("themes/cosmic/images/warning.svg"),
            STATE_DOWNLOADING: svg_icon("themes/cosmic/images/update-download.svg"),
            STATE_READY: svg_icon("themes/cosmic/images/update-ready.svg"),
            STATE_FAILED: svg_icon("themes/cosmic/images/warning.svg"),
        }

        self.setObjectName("updateStatusButton")
        self.setDefaultAction(window.actionUpdate)
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRaise(True)
        self.setVisible(False)

        self._apply_stylesheet(COLOR_WARNING)

        # Reserve enough width for the widest label so the toolbar does not
        # jitter as the percentage counts up
        metrics = self.fontMetrics()
        widest = max(metrics.horizontalAdvance(self._tr(label)) for label in (
            "Update Available", "Downloading update…", "Downloading… 100%",
            "Update Ready",
        ))
        self.setMinimumWidth(widest + ICON_SIZE + 34)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_active(self):
        """True once there is an update worth telling the user about.

        Themes read this when rebuilding the toolbar so a mid-download theme
        switch doesn't hide a pill that should still be showing."""
        return self.state != STATE_HIDDEN

    def set_available(self, version):
        """A newer version exists but has not been downloaded."""
        self.version = version or self.version
        self._set_state(
            STATE_AVAILABLE,
            self._tr("Update Available"),
            self._tr("Version <b>%s</b> is available.") % self.version,
            COLOR_WARNING,
        )

    def set_downloading(self, version, percent, downloaded, total):
        """A download is in flight. *percent* < 0 means the size is unknown."""
        self.version = version or self.version
        self.percent = percent
        self.downloaded = downloaded
        self.total = total

        if percent < 0:
            text = self._tr("Downloading update…")
            tooltip = self._tr(
                "Downloading version <b>%s</b>…") % self.version
        else:
            text = self._tr("Downloading… %d%%") % percent
            tooltip = self._tr(
                "Downloading version <b>%s</b> — %d%% complete.") % (
                    self.version, percent)

        self._set_state(STATE_DOWNLOADING, text, tooltip, COLOR_ACCENT)

    def set_ready(self, version, size=0):
        """The update is downloaded, verified and staged for the next launch.

        *size* comes from the update manifest, so a session that finds an
        already-staged update can still report how large it is."""
        self.version = version or self.version
        self.percent = 100
        if size:
            self.total = size
            self.downloaded = size
        self._set_state(
            STATE_READY,
            self._tr("Update Ready"),
            self._tr("Version <b>%s</b> is ready — restart Zenvi to install it.")
            % self.version,
            COLOR_SUCCESS,
        )

    def set_failed(self, version, reason=""):
        """The download failed; fall back to offering a manual download."""
        self.version = version or self.version
        self._set_state(
            STATE_FAILED,
            self._tr("Update Available"),
            self._tr("Version <b>%s</b> is available, but the download failed. "
                     "Click to download it manually.") % self.version,
            COLOR_WARNING,
        )
        if reason:
            log.warning("UpdateStatusButton: download failed — %s", reason)

    def _set_state(self, state, text, tooltip, color):
        self.state = state

        action = self.defaultAction()
        if action:
            action.setVisible(True)
            action.setText(text)
            action.setToolTip(tooltip)
            action.setIcon(self._icons.get(state, QIcon()))

        self.setVisible(True)
        self._apply_stylesheet(color)

        if state == STATE_DOWNLOADING and self.percent < 0:
            if not self._indeterminate_timer.isActive():
                self._indeterminate_timer.start()
        else:
            self._indeterminate_timer.stop()

        self.update()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def _apply_stylesheet(self, color):
        self.setStyleSheet(
            "QToolButton#updateStatusButton {"
            f"  background-color: {COLOR_SURFACE};"
            f"  color: {color};"
            "   border: none;"
            "   border-radius: 6px;"
            "   padding: 5px 12px 8px 12px;"
            "   font-size: 11px;"
            "}"
            "QToolButton#updateStatusButton:hover {"
            "   background-color: #2f2f2f;"
            "}"
        )

    def paintEvent(self, event):
        super().paintEvent(event)

        if self.state != STATE_DOWNLOADING:
            return

        # Progress track hugs the bottom edge, inset to match the border radius
        inset = 8
        track = QRectF(
            inset,
            self.height() - PROGRESS_HEIGHT - 4,
            max(0, self.width() - inset * 2),
            PROGRESS_HEIGHT,
        )
        if track.width() <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        radius = PROGRESS_HEIGHT / 2.0
        painter.setBrush(QColor(COLOR_TRACK))
        painter.drawRoundedRect(track, radius, radius)

        if self.percent < 0:
            chunk_width = track.width() * INDETERMINATE_WIDTH
            left = track.left() + self._indeterminate_pos * (
                track.width() + chunk_width) - chunk_width
            chunk = QRectF(left, track.top(), chunk_width, track.height())
            # Keep the sliding chunk clipped to the rounded track
            clip = QPainterPath()
            clip.addRoundedRect(track, radius, radius)
            painter.setClipPath(clip)
        else:
            chunk = QRectF(
                track.left(), track.top(),
                track.width() * min(100, max(0, self.percent)) / 100.0,
                track.height(),
            )

        painter.setBrush(QColor(COLOR_ACCENT))
        painter.drawRoundedRect(chunk, radius, radius)
        painter.end()

    def _advance_indeterminate(self):
        # Don't repaint off-screen. Inside a QToolBar this widget is wrapped in
        # a QWidgetAction that owns its visibility, so show/hide events are not
        # a dependable signal — check isVisible() directly instead.
        if not self.isVisible():
            return
        self._indeterminate_pos += INDETERMINATE_STEP
        if self._indeterminate_pos > 1.0:
            self._indeterminate_pos = 0.0
        self.update()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if self.state == STATE_DOWNLOADING and self.percent < 0:
            self._indeterminate_timer.start()

    def hideEvent(self, event):
        self._indeterminate_timer.stop()
        super().hideEvent(event)

    def _tr(self, text):
        try:
            from classes.app import get_app
            return get_app()._tr(text)
        except Exception:
            return text
