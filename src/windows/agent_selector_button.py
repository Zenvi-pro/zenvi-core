"""
 @file
 @brief Main-toolbar button that picks which agent backend the active chat tab
        talks to — Zenvi Assistant, Claude Code or Codex.
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

from PyQt5.QtCore import Qt, QRectF, QSize
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QAction, QActionGroup, QApplication, QMenu, QToolButton

from classes import info
from classes.logger import log
from windows.update_status_button import svg_icon


ICON_SIZE = 16
ICON_GAP = 6   # transparent padding baked into the icon (see _agent_icon)
DOT_SIZE = 8
CHEVRON = os.path.join(info.PATH, "themes/cosmic/images/dropdown-arrow.svg").replace("\\", "/")

# Status-dot palette, matching the chat panel's own CLI status colors
COLOR_READY = "#22c55e"      # CLI installed and registered with Zenvi's MCP server
COLOR_PARTIAL = "#f59e0b"    # installed, but not registered yet
COLOR_MISSING = "#6b7280"    # not installed

# Padding on the right leaves room for the chevron the menu-indicator draws
# there — without it Qt overlaps the arrow onto the label's last character.
STYLESHEET = (
    "QToolButton { background: transparent; border: 1px solid rgba(145,195,255,0.22);"
    " border-radius: 6px; padding: 5px 22px 5px 8px; }"
    "QToolButton:hover { background-color: rgba(145,195,255,0.12); }"
    "QToolButton:pressed { background-color: rgba(145,195,255,0.20); }"
    "QToolButton::menu-indicator { image: url(%s); width: 12px; height: 12px;"
    " subcontrol-origin: padding; subcontrol-position: center right; right: 6px; }"
) % CHEVRON


def _agent_icon() -> QIcon:
    """The robot glyph with trailing transparent padding.

    QToolButton hard-codes a very tight icon/text gap in
    ToolButtonTextBesideIcon mode and exposes no way to widen it, so the
    breathing room is baked into the pixmap instead.
    """
    path = os.path.join(info.PATH, "themes/cosmic/images/tool-agent.svg")
    if not os.path.exists(path):
        log.warning("AgentSelectorButton: icon not found: %s", path)
        return svg_icon("themes/cosmic/images/tool-agent.svg", ICON_SIZE)

    app = QApplication.instance()
    ratio = app.devicePixelRatio() if app else 1.0
    pixmap = QPixmap(QSize(ICON_SIZE + ICON_GAP, ICON_SIZE) * ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(path).render(painter, QRectF(0, 0, ICON_SIZE * ratio, ICON_SIZE * ratio))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return QIcon(pixmap)


def _dot_icon(color: str) -> QIcon:
    """A small filled circle used as the per-agent status indicator."""
    pixmap = QPixmap(DOT_SIZE, DOT_SIZE)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(0, 0, DOT_SIZE, DOT_SIZE)
    painter.end()
    return QIcon(pixmap)


class AgentSelectorButton(QToolButton):
    """Toolbar control for the chat dock's agent backend.

    It owns no state of its own: the chat dock is the single source of truth
    (each tab remembers its own backend), so this reads through
    ``AIChatWindow.active_backend()`` and writes through
    ``set_active_backend()``. The dock calls :meth:`sync_from_chat` whenever
    that changes — including on tab switches, where the label must follow the
    newly active tab.
    """

    def __init__(self, window):
        super().__init__(window)

        self.window = window
        self._backend_actions = {}

        self.setObjectName("agentSelectorButton")
        self.setIcon(_agent_icon())
        self.setIconSize(QSize(ICON_SIZE + ICON_GAP, ICON_SIZE))
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.InstantPopup)
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRaise(True)
        self.setStyleSheet(STYLESHEET)

        self._menu = QMenu(self)
        self._group = QActionGroup(self)
        self._group.setExclusive(True)
        self.setMenu(self._menu)
        self._menu.aboutToShow.connect(self._on_menu_about_to_show)

        self._build_menu()
        self.sync_from_chat()

    # -- construction ------------------------------------------------------

    def _build_menu(self):
        from windows.ai_chat_ui import BACKENDS

        for backend in BACKENDS:
            action = QAction(backend["name"], self)
            action.setCheckable(True)
            action.setData(backend["id"])
            action.triggered.connect(
                lambda _checked, bid=backend["id"]: self._choose(bid)
            )
            self._group.addAction(action)
            self._menu.addAction(action)
            self._backend_actions[backend["id"]] = action

    # -- chat dock plumbing ------------------------------------------------

    def _chat(self):
        """The chat dock, or None before it has been constructed."""
        return getattr(self.window, "dockAIChat", None)

    def _choose(self, backend_id: str):
        chat = self._chat()
        if chat is None:
            return
        try:
            chat.set_active_backend(backend_id)
        except Exception:
            log.error("Failed to switch agent backend to %s", backend_id, exc_info=True)
        # set_active_backend calls back into sync_from_chat, but it early-returns
        # when the tab already runs this backend — resync so a no-op click still
        # leaves the radio marks consistent.
        self.sync_from_chat()

    def _on_menu_about_to_show(self):
        """Re-check CLI availability so the dots are current, not up-to-60s old."""
        chat = self._chat()
        if chat is not None:
            try:
                chat._detect_clis()
            except Exception:
                log.debug("CLI re-detection on menu open failed", exc_info=True)
        self._refresh_menu_items()

    # -- rendering ---------------------------------------------------------

    def sync_from_chat(self):
        """Adopt the active chat tab's backend as the displayed one."""
        chat = self._chat()
        backend_id = chat.active_backend() if chat is not None else ""
        action = self._backend_actions.get(backend_id)
        if action is None:
            action = next(iter(self._backend_actions.values()), None)
        if action is None:
            return
        action.setChecked(True)
        self.setText(action.text())
        self.setToolTip(self._tr("Agent: %s") % action.text())
        self._refresh_menu_items()

    def _refresh_menu_items(self):
        """Annotate each CLI backend with a status dot and availability hint."""
        chat = self._chat()
        status = chat.cli_status() if chat is not None else {}
        for backend_id, action in self._backend_actions.items():
            detected = status.get(backend_id)
            if detected is None:
                # The built-in assistant has no CLI to detect — it is always ready.
                action.setIcon(_dot_icon(COLOR_READY))
                action.setToolTip("")
                continue
            if not detected.get("installed"):
                action.setIcon(_dot_icon(COLOR_MISSING))
                action.setToolTip(
                    self._tr("%s is not installed or not on your PATH") % action.text()
                )
                continue
            registered = detected.get("registered")
            action.setIcon(_dot_icon(COLOR_READY if registered else COLOR_PARTIAL))
            version = detected.get("version") or ""
            action.setToolTip(
                version if registered
                else self._tr("%s — not connected to Zenvi yet") % (version or action.text())
            )

    def _tr(self, text):
        try:
            from classes.app import get_app
            return get_app()._tr(text)
        except Exception:
            return text
