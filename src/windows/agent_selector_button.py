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

import math
import os

from PyQt5.QtCore import (
    Qt, QEasingCurve, QPointF, QRectF, QSize, QTimer, QVariantAnimation,
)
from PyQt5.QtGui import (
    QColor, QCursor, QImage, QPainter, QPainterPath, QPen, QPixmap,
)
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QSizePolicy, QToolBar, QToolButton

from classes import info
from classes.logger import log


# ── Palette (cosmic dark theme tokens) ────────────────────────────────────
TEXT = "#d4d4d4"
TEXT_DIM = "#8a8a8a"
TEXT_ON_INK = "#ffffff"
ACCENT = "#4d9cf6"
ACCENT_PRESSED = "#3b8fe8"
BORDER_HI_ALPHA = 0.13     # rgba(255,255,255,0.13)

ROBOT_SVG = "themes/cosmic/images/tool-agent.svg"

# ── Layout (logical px; Qt applies devicePixelRatio to the whole painter) ──
PAD_L = 13
SLOT_W = 20                # shared left slot: robot at rest, arrow on hover
GAP = 10
LABEL_X_REST = PAD_L + SLOT_W + GAP
LABEL_SHIFT = 12           # the CSS -12px → +12px travel
LABEL_X_HOVER = LABEL_X_REST + LABEL_SHIFT
ARROW_W = 16
GAP_R = 10
PAD_R = 16
BORDER_W = 1.5
VPAD = 8
SLOT_CX = PAD_L + SLOT_W / 2.0
HOVER_RADIUS = 12.0        # CSS hover border-radius; rest is a pill (h/2)

# ── Motion ────────────────────────────────────────────────────────────────
HOVER_MS = 800
OUT_MS = 520
PRESS_IN_MS = 110
PRESS_OUT_MS = 170
INK_REST_D = 16.0          # CSS: circle starts at w-4/h-4 = 16px
INK_MIN_D = 220.0          # CSS: grows to 220px
PRESS_SCALE = 0.05         # CSS active:scale(0.95)


def _bezier(x1, y1, x2, y2):
    """A QEasingCurve matching CSS ``cubic-bezier(x1, y1, x2, y2)``.

    Qt does the proper x→t solve, so values match CSS to 4 decimals, and a
    control point with y > 1 is accepted rather than clamped — which is what
    lets CURVE_ARROW overshoot the way the CSS does.
    """
    curve = QEasingCurve(QEasingCurve.BezierSpline)
    curve.addCubicBezierSegment(QPointF(x1, y1), QPointF(x2, y2), QPointF(1.0, 1.0))
    return curve


CURVE_INK = _bezier(0.19, 1.00, 0.22, 1.00)     # ink flood, colours, border
CURVE_RADIUS = _bezier(0.23, 1.00, 0.32, 1.00)  # pill → 12px
CURVE_LABEL = _bezier(0.00, 0.00, 0.58, 1.00)   # CSS `ease-out`
CURVE_ARROW = _bezier(0.34, 1.56, 0.64, 1.00)   # overshoot; peaks ~1.098

# name -> (duration_ms, curve). The master clock spans HOVER_MS; each channel
# finishes early via the min(1, elapsed/dur) clamp in _channels().
CHANNELS = {
    "ink": (800, CURVE_INK),
    "radius": (600, CURVE_RADIUS),
    "label": (800, CURVE_LABEL),
    "arrow": (800, CURVE_ARROW),
    # Not in the CSS: the robot has to clear the left slot well before the
    # incoming arrow lands on it, or the arrow flies through a half-faded
    # robot. CURVE_ARROW is already 82% through its travel by 200ms.
    "robot": (260, CURVE_INK),
}


def _lerp(a, b, t):
    return a + (b - a) * t


def _lerp_color(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return QColor(
        int(round(_lerp(c1.red(), c2.red(), t))),
        int(round(_lerp(c1.green(), c2.green(), t))),
        int(round(_lerp(c1.blue(), c2.blue(), t))),
    )


def _invert(curve, y):
    """Smallest t with ``curve(t) >= y``, by bisection.

    Only ever call this on a MONOTONIC curve. CURVE_ARROW peaks at ~1.098
    around t=0.57 and then falls back, so inverting it would silently return
    a wrong branch — resume logic uses CURVE_INK exclusively.
    """
    lo, hi = 0.0, 1.0
    for _ in range(20):
        mid = (lo + hi) / 2.0
        if curve.valueForProgress(mid) < y:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _build_arrow():
    """A right-pointing arrow centred on the origin, in logical px.

    Stroked rather than filled so recolouring it each frame is a pen swap.
    """
    path = QPainterPath()
    path.moveTo(-7.0, 0.0)
    path.lineTo(6.0, 0.0)
    path.moveTo(1.0, -5.0)
    path.lineTo(6.5, 0.0)
    path.lineTo(1.0, 5.0)
    return path


ARROW_PATH = _build_arrow()


class AgentSelectorButton(QToolButton):
    """Toolbar control for the chat dock's agent backend.

    Custom-painted end to end, reproducing the motion of the "flow button"
    reference: an ink circle floods the pill from its centre while the corner
    radius morphs, the label glides right, and the robot in the left slot is
    replaced by an arrow sliding in as a second arrow exits to the right.

    It owns no backend state — the chat dock does, per tab. This reads through
    ``AIChatWindow.active_backend()`` and writes through ``set_active_backend()``;
    the dock calls :meth:`sync_from_chat` whenever either changes.
    """

    def __init__(self, window):
        super().__init__(window)

        self.window = window

        # Motion state. Everything else is derived in _channels() so paintEvent
        # and the animation slot can never disagree.
        self._t = 0.0                 # master hover clock, 0..1
        self._press_t = 0.0
        self._phase = "in"            # "in" | "out"
        self._snap = {}               # channel values frozen at leave
        self._panel_open = False
        self._robot_cache = None      # (device_pixel_ratio, QPixmap)
        self._hint = QSize(160, 36)

        self.setObjectName("agentSelectorButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.StrongFocus)
        # Belt and braces: overriding paintEvent without calling super() already
        # suppresses the theme's `QToolBar#toolBar QToolButton:hover` background,
        # but an ID selector on the widget's own sheet outranks it either way.
        # NOTE: set_toolbar_buttons() would REPLACE this if a theme ever passed
        # a "stylesheet" key for this widget — neither theme does.
        self.setStyleSheet(
            "QToolButton#agentSelectorButton,"
            "QToolButton#agentSelectorButton:hover,"
            "QToolButton#agentSelectorButton:pressed,"
            "QToolButton#agentSelectorButton:focus {"
            " background: transparent; background-color: transparent;"
            " border: none; padding: 0px; margin: 0px; }"
        )

        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.valueChanged.connect(self._on_hover_value)
        self._press_anim = QVariantAnimation(self)
        self._press_anim.valueChanged.connect(self._on_press_value)

        self.clicked.connect(self._toggle_panel)

        self._recompute_hint()
        self.sync_from_chat()

    # -- chat dock plumbing ------------------------------------------------

    def _chat(self):
        """The chat dock, or None before it has been constructed."""
        return getattr(self.window, "dockAIChat", None)

    def _backends(self):
        from windows.ai_chat_ui import BACKENDS
        return BACKENDS

    def _choose(self, backend_id: str):
        """Switch the active chat tab to *backend_id* (called by the panel)."""
        chat = self._chat()
        if chat is None:
            return
        try:
            chat.set_active_backend(backend_id)
        except Exception:
            log.error("Failed to switch agent backend to %s", backend_id, exc_info=True)
        # set_active_backend early-returns without notifying when the tab is
        # already on this backend, so re-sync to keep the label honest.
        self.sync_from_chat()

    # -- popup -------------------------------------------------------------

    def _panel(self):
        """The shared AgentPanel, created on first use and cached on the window."""
        panel = getattr(self.window, "_agent_panel", None)
        if panel is None:
            from windows.agent_panel import AgentPanel
            panel = AgentPanel(self.window)
            panel.closed.connect(lambda: self._set_panel_open(False))
            self.window._agent_panel = panel
        return panel

    def _toggle_panel(self):
        try:
            panel = self._panel()
        except Exception:
            log.error("Failed to open the agent panel", exc_info=True)
            return
        # A Qt.Popup closes on the outside mouse-press, and the button can then
        # still receive that same click — without this guard it would reopen
        # immediately and the popup would look like it never closed.
        if panel.isVisible() or panel.just_closed():
            panel.close()
            return
        panel.refresh()
        panel.show_under(self)
        self._set_panel_open(True)

    def _set_panel_open(self, is_open: bool):
        """Hold the button in its filled state for as long as the panel shows."""
        if self._panel_open == is_open:
            return
        self._panel_open = is_open
        if is_open:
            self._begin_in()
            self._animate_press(1.0, PRESS_IN_MS)
        else:
            self._animate_press(0.0, PRESS_OUT_MS)
            # underMouse()/WA_UnderMouse are stale while the popup grab is being
            # released, and no enterEvent is synthesised if the pointer never
            # left — so resolve it from the real cursor position, next tick.
            QTimer.singleShot(0, self._resync_hover)

    def _resync_hover(self):
        if not self.isVisible():
            return
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self._begin_in() if inside else self._begin_out()

    def on_connect_result(self, backend_id: str, ok: bool, message: str):
        """Forwarded from the chat dock; hand it to the panel if one is open."""
        panel = getattr(self.window, "_agent_panel", None)
        if panel is not None:
            panel.on_connect_result(backend_id, ok, message)

    # -- motion ------------------------------------------------------------

    def _channels(self):
        """``{name: u}`` where u=0 is rest and u=1 is full hover.

        ``arrow`` may exceed 1.0 — that overshoot is the point.
        """
        out = {}
        if self._phase == "in":
            elapsed = self._t * HOVER_MS
            for name, (dur, curve) in CHANNELS.items():
                out[name] = curve.valueForProgress(min(1.0, elapsed / dur))
        else:
            # Leaving does NOT retrace the clock. CURVE_INK is 0.9983 at t=0.75,
            # so rewinding would move the ink by a fraction of a percent and the
            # button would read as glued. CSS re-runs the transition forward from
            # wherever the property currently sits, which is what this does —
            # and because each channel keeps its own curve, the arrows still
            # overshoot on the way out.
            elapsed = self._t * OUT_MS
            scale = OUT_MS / float(HOVER_MS)
            for name, (dur, curve) in CHANNELS.items():
                k = curve.valueForProgress(min(1.0, elapsed / (dur * scale)))
                out[name] = self._snap.get(name, 0.0) * (1.0 - k)
        return out

    def _start(self, anim, start, end, ms):
        anim.stop()
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setDuration(int(max(1, ms)))
        anim.setEasingCurve(QEasingCurve.Linear)   # the curve lives in _channels
        anim.start()

    def _begin_in(self):
        if self._phase == "in" and (self._hover_anim.state() or self._t >= 1.0):
            return
        resume = 0.0
        if self._phase == "out":
            # Re-entering mid-collapse: restart the clock where it reproduces the
            # ink we are currently showing. Only CURVE_INK is monotonic enough to
            # invert; the other channels jump by a fraction of a pixel.
            resume = _invert(CURVE_INK, min(1.0, self._channels().get("ink", 0.0)))
        self._phase = "in"
        self._snap = {}
        if not self.isVisible():
            self._t = 1.0
            self.update()
            return
        self._t = resume
        self._start(self._hover_anim, resume, 1.0, HOVER_MS * (1.0 - resume))

    def _begin_out(self):
        if self._phase == "out" and not self._hover_anim.state():
            return
        self._snap = self._channels()
        self._phase = "out"
        if not self.isVisible():
            self._t = 1.0
            self.update()
            return
        self._t = 0.0
        # A grazing hover snaps back; a full one collapses at full length.
        self._start(self._hover_anim, 0.0, 1.0,
                    OUT_MS * max(0.25, self._snap.get("ink", 0.0)))

    def _animate_press(self, target, ms):
        if not self.isVisible():
            self._press_t = float(target)
            self.update()
            return
        self._start(self._press_anim, self._press_t, target, ms)

    def _on_hover_value(self, value):
        self._t = float(value)
        if not self.isVisible():
            # A QWidgetAction can hide us without a reliable hideEvent.
            self._hover_anim.stop()
            return
        self.update()

    def _on_press_value(self, value):
        self._press_t = float(value)
        if self.isVisible():
            self.update()

    # -- events ------------------------------------------------------------

    def enterEvent(self, event):
        super().enterEvent(event)
        self._begin_in()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if self._panel_open:
            return   # the popup's mouse grab, not a real departure
        self._begin_out()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._animate_press(1.0, PRESS_IN_MS)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if not self._panel_open:
            self._animate_press(0.0, PRESS_OUT_MS)

    def showEvent(self, event):
        super().showEvent(event)
        self._recompute_hint()
        self.updateGeometry()
        QTimer.singleShot(0, self._resync_hover)

    def hideEvent(self, event):
        self._hover_anim.stop()
        self._press_anim.stop()
        self._t = 0.0
        self._press_t = 0.0
        self._phase = "in"
        self._snap = {}
        self._panel_open = False
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (event.FontChange, event.ApplicationFontChange,
                            event.StyleChange, event.LanguageChange):
            self._recompute_hint()
            self.updateGeometry()

    # -- sizing ------------------------------------------------------------

    def _recompute_hint(self):
        """Reserve width for the WIDEST backend name, not the current one.

        Otherwise selecting "Codex" (50px) after "Zenvi Assistant" (119px)
        reflows every button in the toolbar.
        """
        fm = self.fontMetrics()
        try:
            names = [b["name"] for b in self._backends()]
        except Exception:
            names = [self.text() or "Zenvi Assistant"]
        text_w = max([fm.horizontalAdvance(n) for n in names] or [80])

        icon_px = SLOT_W
        parent = self.parentWidget()
        # None between QToolBar.clear()'s releaseWidget() and the re-addWidget()
        # of a theme switch, and before the toolbar ever adopts us.
        if isinstance(parent, QToolBar):
            icon_px = max(16, min(24, parent.iconSize().height()))

        # Sized for the HOVER layout, so the label's +12px glide has somewhere
        # to go and nothing reflows or clips mid-animation.
        width = LABEL_X_HOVER + text_w + GAP_R + ARROW_W + PAD_R
        height = max(icon_px, fm.height()) + 2 * VPAD
        self._hint = QSize(int(round(width)), int(round(height)))

    def sizeHint(self):
        return QSize(self._hint)

    def minimumSizeHint(self):
        return QSize(self._hint)

    # -- painting ----------------------------------------------------------

    def _robot_pixmap(self):
        """The robot SVG rasterised once per devicePixelRatio, then cached.

        Re-rendering an SVG every frame at 60fps is exactly the cost this
        animation cannot afford.
        """
        ratio = self.devicePixelRatioF() or 1.0
        if self._robot_cache and abs(self._robot_cache[0] - ratio) < 1e-6:
            return self._robot_cache[1]

        path = ROBOT_SVG
        if not os.path.exists(path):
            path = os.path.join(info.PATH, ROBOT_SVG)
        if not os.path.exists(path):
            log.warning("AgentSelectorButton: icon not found: %s", ROBOT_SVG)
            pixmap = QPixmap()
        else:
            px = int(round(SLOT_W * ratio))
            image = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            QSvgRenderer(path).render(painter)
            painter.end()
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(ratio)

        self._robot_cache = (ratio, pixmap)
        return pixmap

    def _draw_arrow(self, painter, cx, cy, color):
        painter.save()
        painter.translate(cx, cy)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(ARROW_PATH)
        painter.restore()

    def paintEvent(self, event):
        w = float(self.width())
        h = float(self.height())
        if w <= 0 or h <= 0:
            return   # mid toolbar-rebuild; a NaN radius would follow

        u = self._channels()
        ink_u = max(0.0, min(1.0, u["ink"]))

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        # 1. Press scale — a painter transform about the centre. Never geometry:
        #    QToolBarLayout would re-run and shuffle every sibling button.
        scale = 1.0 - PRESS_SCALE * self._press_t
        if scale < 1.0:
            painter.translate(w / 2.0, h / 2.0)
            painter.scale(scale, scale)
            painter.translate(-w / 2.0, -h / 2.0)

        # 2. The morphing shape. Two paths, because Qt strokes CENTRED on a
        #    path: the border must inset by half its width to stay inside the
        #    widget, while the ink must reach the true outer edge or a sliver
        #    of toolbar background shows through at the rim.
        radius = _lerp(h / 2.0, HOVER_RADIUS, u["radius"])
        outer = QPainterPath()
        outer.addRoundedRect(QRectF(0.0, 0.0, w, h), radius, radius)

        # 3. Ink flood.
        if ink_u > 0.001:
            d_full = max(INK_MIN_D, math.hypot(w, h) + 8.0)
            r_ink = _lerp(INK_REST_D, d_full, ink_u) / 2.0
            ink = QPainterPath()
            ink.addEllipse(QPointF(w / 2.0, h / 2.0), r_ink, r_ink)
            if r_ink > min(w, h) / 2.0 - 1.0:
                # Boolean rather than setClipPath: a raster clip is ALIASED, and
                # jagged corners would be plainly visible against the dark
                # toolbar exactly when the border has faded to transparent.
                # Skipped while the disc is provably still inside the pill.
                ink = ink.intersected(outer)
            color = _lerp_color(QColor(ACCENT), QColor(ACCENT_PRESSED), self._press_t)
            color.setAlphaF(ink_u)
            painter.fillPath(ink, color)

        # 4. Border, fading out as the ink arrives.
        border = QColor(255, 255, 255)
        border.setAlphaF(BORDER_HI_ALPHA * (1.0 - ink_u))
        half = BORDER_W / 2.0
        stroke = QPainterPath()
        stroke.addRoundedRect(
            QRectF(half, half, w - BORDER_W, h - BORDER_W),
            max(0.0, radius - half), max(0.0, radius - half),
        )
        painter.strokePath(stroke, QPen(border, BORDER_W))

        painter.save()
        painter.setClipPath(outer)

        # 5. Robot leaving the shared left slot.
        robot_u = max(0.0, min(1.0, u["robot"]))
        if robot_u < 0.99:
            pixmap = self._robot_pixmap()
            if not pixmap.isNull():
                painter.setOpacity(1.0 - robot_u)
                painter.drawPixmap(
                    QPointF(SLOT_CX - SLOT_W / 2.0 - 16.0 * robot_u,
                            h / 2.0 - SLOT_W / 2.0),
                    pixmap,
                )
                painter.setOpacity(1.0)

        arrow_u = u["arrow"]

        # 6. Arrow sliding into the slot the robot just vacated.
        if ink_u > 0.001:
            incoming = QColor(TEXT_ON_INK)
            incoming.setAlphaF(ink_u)
            self._draw_arrow(painter, _lerp(-0.25 * w, SLOT_CX, arrow_u), h / 2.0, incoming)

        # 7. Label gliding right.
        label_x = LABEL_X_REST + LABEL_SHIFT * u["label"]
        avail = (w - PAD_R - ARROW_W - GAP_R) - label_x
        if avail > 0:
            painter.setPen(_lerp_color(QColor(TEXT), QColor(TEXT_ON_INK), ink_u))
            painter.setFont(self.font())
            painter.drawText(
                QRectF(label_x, 0.0, avail, h),
                Qt.AlignLeft | Qt.AlignVCenter,
                self.fontMetrics().elidedText(self.text(), Qt.ElideRight, int(avail)),
            )

        # 8. Arrow exiting right.
        self._draw_arrow(
            painter,
            _lerp(w - PAD_R - ARROW_W / 2.0, w * 1.25, arrow_u),
            h / 2.0,
            _lerp_color(QColor(TEXT_DIM), QColor(TEXT_ON_INK), ink_u),
        )

        painter.restore()

        # 9. Focus ring — the widget had no focus affordance at all before.
        if self.hasFocus():
            ring = QPainterPath()
            ring.addRoundedRect(QRectF(2.5, 2.5, w - 5.0, h - 5.0),
                                max(0.0, radius - 2.5), max(0.0, radius - 2.5))
            painter.strokePath(ring, QPen(QColor(ACCENT), 1.0))

        painter.end()

    # -- state -------------------------------------------------------------

    def sync_from_chat(self):
        """Adopt the active chat tab's backend as the displayed one."""
        chat = self._chat()
        backend_id = ""
        if chat is not None:
            try:
                backend_id = chat.active_backend()
            except Exception:
                log.debug("active_backend() failed", exc_info=True)

        name = ""
        for backend in self._backends():
            if backend["id"] == backend_id:
                name = backend["name"]
                break
        if not name:
            backends = self._backends()
            name = backends[0]["name"] if backends else "Zenvi Assistant"
            backend_id = backends[0]["id"] if backends else ""

        if name != self.text():
            self.setText(name)
            self.update()
        self.setToolTip(self._tr("Agent: %s") % name)

        panel = getattr(self.window, "_agent_panel", None)
        if panel is not None and panel.isVisible():
            panel.refresh()

    def _tr(self, text):
        try:
            from classes.app import get_app
            return get_app()._tr(text)
        except Exception:
            return text
