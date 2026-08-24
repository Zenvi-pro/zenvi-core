"""
 @file
 @brief Popup panel anchored under the toolbar agent button — picks which
        backend drives the editor and explains why each one is or isn't usable.
 @author Zenvi Team

 @section LICENSE

 Copyright (c) 2008-2026 Zenvi.
 This file is part of Zenvi Video Editor (https://zenvi.pro).

 Zenvi is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
"""

import re
import time

from PyQt5.QtCore import Qt, QPoint, QSize, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from classes.logger import log


PANEL_WIDTH = 340        # 320 crowds "Zenvi Assistant" against "● not connected"
ROW_HEIGHT = 62          # FIXED, so the popup never resizes while it is open
RADIO_PX = 14
DOT_PX = 8

# register_claude runs `claude mcp remove` (10s) then `mcp add` (15s), so ~25s
# is a legitimate wait. This only rescues a wedged worker thread.
CONNECT_TIMEOUT_MS = 40_000
# detect_cli shells out to up to two binaries sequentially with 3s/10s timeouts.
DETECT_THROTTLE_S = 2.0
REOPEN_GUARD_S = 0.15

# ── Cosmic dark palette ───────────────────────────────────────────────────
BG_PANEL = "#161616"
BG_ROW_HOVER = "#1f1f1f"
BG_ROW_SELECTED = "#202631"
TEXT = "#e8e8e8"
TEXT_DIM = "#8a8a8a"
BORDER = "rgba(255, 255, 255, 0.07)"
BORDER_HI = "rgba(255, 255, 255, 0.13)"
ACCENT = "#4d9cf6"
DANGER = "#ef4444"

# Status vocabulary, shared with the toolbar button's tooltip.
COLOR_READY = "#22c55e"     # installed and registered with Zenvi's MCP server
COLOR_PARTIAL = "#f59e0b"   # installed, not connected yet
COLOR_MISSING = "#6b7280"   # not installed / not probed yet

CLI_BINARIES = {"claude_code": "claude", "codex": "codex"}

_TOOL_COUNT = None


def _editor_tool_count():
    """How many editor tools an agent gets. Computed, never hard-coded."""
    global _TOOL_COUNT
    if _TOOL_COUNT is None:
        try:
            from classes.tool_handlers import AGENT_TOOL_HANDLERS
            _TOOL_COUNT = len(AGENT_TOOL_HANDLERS)
        except Exception:
            log.debug("tool count unavailable", exc_info=True)
            _TOOL_COUNT = 0
    return _TOOL_COUNT


def _format_version(raw):
    """``2.1.241 (Claude Code)`` / ``codex-cli 0.142.3`` -> ``v2.1.241``."""
    match = re.search(r"\d+(?:\.\d+)+", raw or "")
    return "v" + match.group(0) if match else (raw or "").strip()


def _circle_pixmap(diameter, fill, ring=None, inner=None):
    ratio = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    pixmap = QPixmap(int(diameter * ratio), int(diameter * ratio))
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.scale(ratio, ratio)
    if ring:
        painter.setPen(QColor(ring))
        painter.setBrush(QColor(fill) if fill else Qt.NoBrush)
        painter.drawEllipse(1, 1, diameter - 2, diameter - 2)
    else:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawEllipse(0, 0, diameter, diameter)
    if inner:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(inner))
        pad = diameter / 2.0 - 3.0
        painter.drawEllipse(int(pad), int(pad), 6, 6)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def _radio_pixmap(selected):
    if selected:
        return _circle_pixmap(RADIO_PX, None, ring=ACCENT, inner=ACCENT)
    return _circle_pixmap(RADIO_PX, None, ring="#5a5a5a")


def _dot_pixmap(color):
    return _circle_pixmap(DOT_PX, color)


class ElidedLabel(QLabel):
    """A QLabel that elides instead of wrapping.

    Word-wrap anywhere in this panel would let a long version string or error
    message re-flow the popup's height while it is open.
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setMinimumWidth(0)

    def setText(self, text):
        self._full = text or ""
        super().setText(self._full)
        metrics = self.fontMetrics()
        self.setToolTip(self._full if metrics.horizontalAdvance(self._full) > self.width() else "")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        painter.drawText(
            self.rect(), int(self.alignment()),
            self.fontMetrics().elidedText(self._full, Qt.ElideRight, self.width()),
        )
        painter.end()


class AgentRow(QFrame):
    """One selectable agent.

    QFrame rather than QWidget on purpose: QSS background/border rules do not
    paint on a bare QWidget without a manual PE_Widget paintEvent.
    """

    chosen = pyqtSignal(str)
    connect_requested = pyqtSignal(str)

    def __init__(self, backend_id, name, parent=None):
        super().__init__(parent)
        self.backend_id = backend_id
        self._pressed = False

        self.setObjectName("agentRow")
        self.setFixedHeight(ROW_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setProperty("selected", False)
        self.setAccessibleName(name)

        grid = QGridLayout(self)
        grid.setContentsMargins(12, 9, 12, 9)
        grid.setHorizontalSpacing(9)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(1, 1)

        self.radio = QLabel(self)
        self.radio.setFixedSize(RADIO_PX, RADIO_PX)
        grid.addWidget(self.radio, 0, 0, 2, 1, Qt.AlignTop)

        self.name = QLabel(name, self)
        self.name.setObjectName("agentRowName")
        grid.addWidget(self.name, 0, 1, Qt.AlignLeft | Qt.AlignVCenter)

        status = QWidget(self)
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(5)
        self.dot = QLabel(status)
        self.dot.setFixedSize(DOT_PX, DOT_PX)
        self.word = QLabel(status)
        self.word.setObjectName("agentRowStatus")
        status_layout.addWidget(self.dot)
        status_layout.addWidget(self.word)
        grid.addWidget(status, 0, 2, Qt.AlignRight | Qt.AlignVCenter)

        self.desc = ElidedLabel("", self)
        self.desc.setObjectName("agentRowDesc")
        grid.addWidget(self.desc, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)

        # On the version line, not below it — so a row is exactly ROW_HEIGHT
        # whether or not the button is showing.
        self.action = QPushButton(self)
        self.action.setObjectName("agentRowConnect")
        self.action.setCursor(Qt.PointingHandCursor)
        self.action.setFixedHeight(22)
        self.action.hide()
        self.action.clicked.connect(lambda: self.connect_requested.emit(self.backend_id))
        grid.addWidget(self.action, 1, 2, Qt.AlignRight | Qt.AlignVCenter)

    def set_state(self, selected, color, word, desc, connect_text=None,
                  desc_danger=False, tooltip=""):
        self.radio.setPixmap(_radio_pixmap(selected))
        self.dot.setPixmap(_dot_pixmap(color))
        self.word.setText(word)
        self.desc.setText(desc)
        self.desc.setStyleSheet("color: %s;" % (DANGER if desc_danger else TEXT_DIM))
        self.setToolTip(tooltip)
        self.setAccessibleDescription("%s. %s" % (word, desc))

        if connect_text is None:
            self.action.hide()
        else:
            label, enabled = connect_text
            self.action.setText(label)
            self.action.setEnabled(enabled)
            self.action.show()

        if self.property("selected") != selected:
            self.setProperty("selected", selected)
            # Dynamic properties do not repolish themselves.
            self.style().unpolish(self)
            self.style().polish(self)

    def set_cursor_on(self, on):
        if self.property("cursorOn") != on:
            self.setProperty("cursorOn", on)
            self.style().unpolish(self)
            self.style().polish(self)

    def mousePressEvent(self, event):
        self._pressed = event.button() == Qt.LeftButton
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        was = self._pressed
        self._pressed = False
        super().mouseReleaseEvent(event)
        if not was or not self.rect().contains(event.pos()):
            return
        if self.action.isVisible() and self.action.geometry().contains(event.pos()):
            return   # the Connect button owns that click
        self.chosen.emit(self.backend_id)


class AgentPanel(QFrame):
    """Frameless popup listing the agent backends.

    Qt.Popup gives click-outside-to-close and the keyboard grab for free, the
    same way UpdatePanel works.
    """

    closed = pyqtSignal()

    def __init__(self, window):
        super().__init__(window, Qt.Popup | Qt.FramelessWindowHint)

        self.window = window
        self._rows = {}
        self._connecting = None
        self._connect_error = {}
        self._refreshing = False
        self._last_detect = 0.0
        self._closed_at = 0.0
        self._cursor = 0

        self.setObjectName("agentPanel")
        self.setFixedWidth(PANEL_WIDTH)
        self.setStyleSheet(self._stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 10)
        layout.setSpacing(0)

        header = QVBoxLayout()
        header.setContentsMargins(16, 0, 16, 10)
        header.setSpacing(2)
        title = QLabel(self._tr("Agents"), self)
        title.setObjectName("agentPanelTitle")
        subtitle = QLabel(self._tr("Choose who drives the editor"), self)
        subtitle.setObjectName("agentPanelSubtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        for backend in self._backends():
            row = AgentRow(backend["id"], backend["name"], self)
            row.chosen.connect(self._on_row_chosen)
            row.connect_requested.connect(self._on_connect_requested)
            self._rows[backend["id"]] = row
            layout.addWidget(row)
            layout.addSpacing(2)

        rule = QFrame(self)
        rule.setFrameShape(QFrame.HLine)
        rule.setFixedHeight(1)
        rule_wrap = QHBoxLayout()
        rule_wrap.setContentsMargins(16, 8, 16, 8)
        rule_wrap.addWidget(rule)
        layout.addLayout(rule_wrap)

        self.footer = QPushButton(self)
        self.footer.setObjectName("agentPanelFooter")
        self.footer.setCursor(Qt.PointingHandCursor)
        self.footer.setFlat(True)
        self.footer.clicked.connect(self._on_footer)
        footer_wrap = QHBoxLayout()
        footer_wrap.setContentsMargins(16, 0, 16, 0)
        footer_wrap.addWidget(self.footer)
        layout.addLayout(footer_wrap)

        self._connect_timer = QTimer(self)
        self._connect_timer.setSingleShot(True)
        self._connect_timer.timeout.connect(self._on_connect_timeout)

    # -- data --------------------------------------------------------------

    def _backends(self):
        try:
            from windows.ai_chat_ui import BACKENDS
            return BACKENDS
        except Exception:
            log.debug("BACKENDS unavailable", exc_info=True)
            return []

    def _chat(self):
        return getattr(self.window, "dockAIChat", None)

    def _state_for(self, backend_id, detected):
        """(color, word, desc, connect, danger, tooltip) for one backend."""
        if backend_id not in CLI_BINARIES:
            count = _editor_tool_count()
            desc = (self._tr("Built in · %d editor tools") % count if count
                    else self._tr("Built in · drives the editor directly"))
            return (COLOR_READY, self._tr("ready"), desc, None, False,
                    self._tr("Runs inside Zenvi. Nothing to install."))

        binary = CLI_BINARIES[backend_id]
        name = self._rows[backend_id].name.text()

        if self._connecting == backend_id:
            return (COLOR_PARTIAL, self._tr("connecting…"),
                    self._tr("Registering Zenvi's tools…"),
                    (self._tr("Connecting…"), False), False, "")

        error = self._connect_error.get(backend_id)
        if error:
            return (COLOR_PARTIAL, self._tr("not connected"),
                    error.splitlines()[0], (self._tr("Retry"), True), True, error)

        if detected is None:
            return (COLOR_MISSING, self._tr("checking…"),
                    self._tr("Looking for the ‘%s’ command…") % binary, None, False, "")

        if not detected.get("installed"):
            return (COLOR_MISSING, self._tr("not installed"),
                    self._tr("‘%s’ is not on your PATH") % binary, None, False,
                    self._tr("Zenvi could not find an executable named ‘%s’ on your PATH.")
                    % binary)

        version = _format_version(detected.get("version"))
        if not detected.get("registered"):
            return (COLOR_PARTIAL, self._tr("not connected"), version,
                    (self._tr("Connect"), True), False,
                    self._tr("%s is installed but has not been given Zenvi's tools yet.")
                    % name)

        return (COLOR_READY, self._tr("connected"),
                self._tr("%s · plus its own shell & files") % version,
                None, False,
                self._tr("%s has Zenvi's %d editor tools, and brings its own file, "
                         "shell and web tools too.") % (name, _editor_tool_count()))

    # -- refresh -----------------------------------------------------------

    def refresh(self):
        """Repaint every row from the dock's current state."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            chat = self._chat()
            active = ""
            status = {}
            if chat is not None:
                try:
                    active = chat.active_backend()
                    status = chat.cli_status() or {}
                except Exception:
                    log.debug("agent panel refresh read failed", exc_info=True)
            if not active and self._rows:
                active = next(iter(self._rows))

            for backend_id, row in self._rows.items():
                color, word, desc, connect, danger, tip = self._state_for(
                    backend_id, status.get(backend_id))
                row.set_state(backend_id == active, color, word, desc, connect, danger, tip)

            if active == "codex":
                self.footer.setText(self._tr("Codex uses the model from its own config"))
            else:
                self.footer.setText(self._tr("Model is set in the chat panel  →"))
        finally:
            self._refreshing = False

    def _maybe_detect(self):
        """Re-probe the CLIs, but not more than once every couple of seconds."""
        chat = self._chat()
        if chat is None:
            return
        now = time.monotonic()
        if now - self._last_detect < DETECT_THROTTLE_S:
            return
        self._last_detect = now
        try:
            chat._detect_clis()
        except Exception:
            log.debug("CLI re-detection failed", exc_info=True)

    # -- actions -----------------------------------------------------------

    def _on_row_chosen(self, backend_id):
        # Paint the new selection, close, and only THEN apply it:
        # _set_session_backend blocks the GUI thread for up to 2s tearing the old
        # worker down, and under a Qt.Popup mouse grab that reads as a frozen popup.
        row = self._rows.get(backend_id)
        if row is not None:
            for other_id, other in self._rows.items():
                other.set_cursor_on(False)
                if other.property("selected") != (other_id == backend_id):
                    other.setProperty("selected", other_id == backend_id)
                    other.radio.setPixmap(_radio_pixmap(other_id == backend_id))
                    other.style().unpolish(other)
                    other.style().polish(other)
            self.repaint()

        self.close()
        button = getattr(self.window, "agent_selector_button", None)
        if button is not None:
            QTimer.singleShot(0, lambda: button._choose(backend_id))

    def _on_connect_requested(self, backend_id):
        chat = self._chat()
        if chat is None:
            return
        self._connecting = backend_id
        self._connect_error.pop(backend_id, None)
        self.refresh()
        self._connect_timer.start(CONNECT_TIMEOUT_MS)
        try:
            chat._connect_cli(backend_id)
        except Exception:
            log.error("Failed to start CLI registration for %s", backend_id, exc_info=True)
            self.on_connect_result(backend_id, False, self._tr("Could not start registration."))

    def on_connect_result(self, backend_id, ok, message):
        """Called (via the toolbar button) when register_* finishes."""
        if self._connecting != backend_id:
            return   # stale result for the other CLI
        self._connect_timer.stop()
        self._connecting = None
        if ok:
            self._connect_error.pop(backend_id, None)
            row = self._rows.get(backend_id)
            if row is not None:
                # register_codex succeeds with a multi-line message telling the
                # user to export a token; that cannot fit one line, so point at
                # the chat panel, which shows it in full.
                extra = "\n" in (message or "")
                row.set_state(
                    True, COLOR_READY, self._tr("connected"),
                    self._tr("Connected — one more step, see the chat panel") if extra
                    else self._tr("Connected — verifying…"),
                    None, False, message or "",
                )
        else:
            self._connect_error[backend_id] = message or self._tr("Connect failed.")
            self.refresh()

    def _on_connect_timeout(self):
        backend_id, self._connecting = self._connecting, None
        if backend_id:
            self._connect_error[backend_id] = self._tr("Connect timed out — try again.")
            self.refresh()

    def _on_footer(self):
        self.close()
        dock = self._chat()
        if dock is not None:
            try:
                dock.show()
                dock.raise_()
            except Exception:
                log.debug("could not raise the chat dock", exc_info=True)

    # -- placement ---------------------------------------------------------

    def show_under(self, anchor):
        """Pop up anchored beneath *anchor*, kept inside its screen.

        Left-aligned, unlike UpdatePanel.show_under — that one right-aligns
        because the update pill sits at the far right of the toolbar, whereas
        this anchor sits mid-toolbar.
        """
        self.adjustSize()
        below = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        left, top = below.x(), below.y()

        screen = None
        for source in (anchor, self.window):
            getter = getattr(source, "screen", None)
            if callable(getter):
                found = getter()
                if found is not None:
                    screen = found.availableGeometry()
                    break
        if screen is not None:
            left = max(screen.left() + 8, min(left, screen.right() - self.width() - 8))
            if top + self.height() > screen.bottom() - 8:
                top = anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 6
            top = max(screen.top() + 8, top)

        self.move(QPoint(left, top))
        self.show()
        self.raise_()
        self.setFocus()
        self._cursor = list(self._rows).index(self._selected_id()) if self._rows else 0
        self._maybe_detect()

    def _selected_id(self):
        for backend_id, row in self._rows.items():
            if row.property("selected"):
                return backend_id
        return next(iter(self._rows), "")

    def just_closed(self):
        """True right after a dismissal, so the anchor click can't reopen us."""
        return (time.monotonic() - self._closed_at) < REOPEN_GUARD_S

    def hideEvent(self, event):
        self._closed_at = time.monotonic()
        for row in self._rows.values():
            row.set_cursor_on(False)
        super().hideEvent(event)
        self.closed.emit()

    # -- keyboard ----------------------------------------------------------

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.close()
            return
        ids = list(self._rows)
        if not ids:
            super().keyPressEvent(event)
            return
        if key in (Qt.Key_Down, Qt.Key_Tab):
            self._move_cursor(1)
        elif key in (Qt.Key_Up, Qt.Key_Backtab):
            self._move_cursor(-1)
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._on_row_chosen(ids[self._cursor % len(ids)])
        else:
            super().keyPressEvent(event)

    def _move_cursor(self, delta):
        ids = list(self._rows)
        self._cursor = (self._cursor + delta) % len(ids)
        for index, backend_id in enumerate(ids):
            self._rows[backend_id].set_cursor_on(index == self._cursor)

    # -- style -------------------------------------------------------------

    def _stylesheet(self):
        # Every rule is scoped under #agentPanel: the app-wide cosmic QSS styles
        # bare QPushButton and QLabel, and would otherwise bleed in here.
        return (
            "QFrame#agentPanel {"
            "   background-color: %(bg)s;"
            "   border: 1px solid %(border)s;"
            "   border-radius: 10px;"
            "}"
            "#agentPanel QLabel { background: transparent; }"
            "#agentPanel QLabel#agentPanelTitle {"
            "   color: #f2f2f2; font-size: 13px; font-weight: 600; }"
            "#agentPanel QLabel#agentPanelSubtitle {"
            "   color: %(dim)s; font-size: 11px; }"
            "#agentPanel QFrame#agentRow {"
            "   background: transparent; border: 1px solid transparent;"
            "   border-radius: 6px; }"
            "#agentPanel QFrame#agentRow:hover { background-color: %(hover)s; }"
            "#agentPanel QFrame#agentRow[selected=\"true\"] {"
            "   background-color: %(sel)s; border-left: 2px solid %(accent)s; }"
            "#agentPanel QFrame#agentRow[cursorOn=\"true\"] {"
            "   border: 1px solid %(border_hi)s; }"
            "#agentPanel QLabel#agentRowName {"
            "   color: %(text)s; font-size: 13px; font-weight: 600; }"
            "#agentPanel QLabel#agentRowStatus {"
            "   color: %(dim)s; font-size: 11px; }"
            "#agentPanel QLabel#agentRowDesc { color: %(dim)s; font-size: 11px; }"
            "#agentPanel QPushButton#agentRowConnect {"
            "   color: #0f0f0f; background-color: %(accent)s; border: none;"
            "   border-radius: 5px; padding: 2px 12px; font-size: 11px;"
            "   font-weight: 600; min-height: 0px; }"
            "#agentPanel QPushButton#agentRowConnect:hover { background-color: #67abf8; }"
            "#agentPanel QPushButton#agentRowConnect:disabled {"
            "   background-color: #2a2a2a; color: #6b6b6b; }"
            "#agentPanel QPushButton#agentPanelFooter {"
            "   color: %(dim)s; background: transparent; border: none;"
            "   font-size: 11px; text-align: left; padding: 2px 0px; min-height: 0px; }"
            "#agentPanel QPushButton#agentPanelFooter:hover { color: %(text)s; }"
        ) % {
            "bg": BG_PANEL, "border": BORDER, "border_hi": BORDER_HI,
            "hover": BG_ROW_HOVER, "sel": BG_ROW_SELECTED, "accent": ACCENT,
            "text": TEXT, "dim": TEXT_DIM,
        }

    def sizeHint(self):
        return QSize(PANEL_WIDTH, super().sizeHint().height())

    def _tr(self, text):
        try:
            from classes.app import get_app
            return get_app()._tr(text)
        except Exception:
            return text
