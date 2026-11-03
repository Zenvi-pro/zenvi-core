"""
 @file
 @brief This file contains a theme's colors and UI dimensions
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

import os

from qt_api import Qt, QSize, QTimer
from qt_api import QIcon, QPainter, QPixmap, QTransform
from qt_api import QDockWidget, QMenu, QTabBar, QTabWidget, QWidget

from classes.info import PATH
from ..base import BaseTheme
from .tokens import FONT_FAMILY, FONT_POINT_SIZE, TOKENS, resolve


# objectName → nav SVG filename (stable across translations)
DOCK_NAV_ICONS = [
    ("dockFiles", "nav-files.svg"),
    ("dockTransitions", "nav-transitions.svg"),
    ("dockEffects", "nav-effects.svg"),
    ("dockEmojis", "nav-emojis.svg"),
    ("PexelsDock", "nav-pexels.svg"),
    ("FreesoundDock", "nav-freesound.svg"),
    ("dockVideo", "nav-video.svg"),
    ("dockProperties", "nav-properties.svg"),
    ("dockCaptionEditor", "nav-captions.svg"),
    ("dockTimeline", "nav-timeline.svg"),
    ("AIChatWindow", "nav-ai-chat.svg"),
    ("AIMediaPanel", "nav-scene-desc.svg"),
    ("director_panel_dock", "nav-directors.svg"),
    ("director_plan_review_dock", "nav-plan-review.svg"),
    ("dockPlanGraph", "nav-plan-graph.svg"),
    ("thinkingDock", "nav-thinking.svg"),
]


# ── Design tokens (Cursor-IDE inspired dark palette) ──────────────────────
#
#   BG_DEEP    {surface}   main window / dialog floor  ← the "absolute black"
#   BG_MAIN    {surface}   primary panel surface       ← same as deep black (2-tone palette)
#   BG_RAISED  {raised}   raised surfaces, hover
#   BG_PRESS   {active}   pressed / active toggle
#   BG_INPUT   {surface}   text inputs (same as deep black)
#
#   TEXT       {text}   primary text
#   TEXT_DIM   {text_muted}   secondary / muted text
#   TEXT_OFF   {text_faint}   disabled text
#
#   BORDER     rgba(255,255,255,0.07)   subtle border
#   BORDER_HI  rgba(255,255,255,0.13)  hover border
#   ACCENT     {accent}   blue accent
#   ACCENT_HVR {accent_hover}   accent hover
#   PLAYHEAD   {playhead}   tally-light red playhead
#   DANGER     {danger}   destructive red
#   SEL_BG     rgba(58,114,245,0.18)   selection highlight
# ──────────────────────────────────────────────────────────────────────────


class CosmicTheme(BaseTheme):
    def __init__(self, app):
        super().__init__(app)

        from classes.app import get_app
        _ = get_app()._tr

        # ── Base stylesheet ────────────────────────────────────────────────
        # Assigned first so get_color() / get_int() can parse it.
        # The f-string replacement (PATH, translated strings) happens below.
        with open(os.path.join(os.path.dirname(__file__), "zenvi.qss"), encoding="utf-8") as qss:
            self.style_sheet = qss.read()

        # Prepend translated message-box buttons, and expand {PATH}
        path_unix_slashes = PATH.replace("\\", "/")
        self.style_sheet = f"""
QMessageBox QPushButton[text="&{_('Yes')}"] {{
    background-color: {{primary}};
    color: {{primary_text}};
    border: none;
    padding: 5px 14px;
    border-radius: 6px;
}}

QMessageBox QPushButton[text="&{_('Cancel')}"] {{
    qproperty-icon: none;
}}
        """ + self.style_sheet.replace("{PATH}", f"{path_unix_slashes}/")
        self.style_sheet = resolve(self.style_sheet)

    # ── apply_theme ────────────────────────────────────────────────────────
    def apply_theme(self):
        super().apply_theme()

        from classes.app import get_app
        from classes import ui_util
        from classes.logger import log
        from qt_api import QStyleFactory
        from qt_api import QFont

        _ = get_app()._tr

        log.info("Setting Fusion dark palette (Cursor theme)")
        self.app.setStyle(QStyleFactory.create("Fusion"))
        dark_palette = ui_util.make_dark_palette(self.app.palette())

        # Graphite palette (same tokens as the stylesheet) for unstyled widgets
        from qt_api import QColor as _QColor
        from qt_api import QPalette as _QPalette
        panel, sunken, raised = _QColor(TOKENS["surface"]), _QColor(TOKENS["sunken"]), _QColor(TOKENS["raised"])
        dark_palette.setColor(_QPalette.Window,        panel)
        dark_palette.setColor(_QPalette.Base,          sunken)
        dark_palette.setColor(_QPalette.AlternateBase, panel)
        dark_palette.setColor(_QPalette.Button,        raised)
        dark_palette.setColor(_QPalette.Mid,           _QColor(TOKENS["bg"]))
        dark_palette.setColor(_QPalette.Dark,          _QColor(TOKENS["bg"]))
        dark_palette.setColor(_QPalette.Text,          _QColor(TOKENS["text"]))
        dark_palette.setColor(_QPalette.WindowText,    _QColor(TOKENS["text"]))
        dark_palette.setColor(_QPalette.ButtonText,    _QColor(TOKENS["text"]))
        dark_palette.setColor(_QPalette.Highlight,     _QColor(TOKENS["accent"]))

        self.app.setPalette(dark_palette)

        # Inter, bundled, so the UI reads the same on Windows, macOS and Linux
        from qt_api import QFontDatabase
        for weight in ("Regular", "Medium", "SemiBold"):
            QFontDatabase.addApplicationFont(
                os.path.join(PATH, "images", "fonts", "Inter-%s.ttf" % weight))
        font = QFont(FONT_FAMILY)
        font.setPointSizeF(FONT_POINT_SIZE)
        hinting = getattr(QFont, "PreferNoHinting", None) or getattr(
            getattr(QFont, "HintingPreference", None), "PreferNoHinting", None)  # Qt5 / Qt6
        if hinting is not None:
            font.setHintingPreference(hinting)
        self.app.setFont(font)

        # Tabs on the left side (vertical menu style) for the top area; the other
        # areas get top tabs explicitly, since restoreState() does not persist
        # tab positions (upstream #6016)
        self.app.window.setTabPosition(Qt.TopDockWidgetArea, QTabWidget.West)
        for area in (Qt.BottomDockWidgetArea, Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
            self.app.window.setTabPosition(area, QTabWidget.North)

        # Dock content margins
        self.set_dock_margins([10, 0, 10, 6])
        self.set_dock_margins([0, 0, 0, 0], [0, 8, 0, 0], "dockTimelineContents")

        # Apply new stylesheet
        self.app.setStyleSheet(self.compose_stylesheet())

        # ── Dock nav tab icons (icon-only, text hidden via QSS) ───────
        win = self.app.window

        _img_dir = os.path.join(PATH, "themes/cosmic/images")
        _tab_size = 40   # must match the min/max-width/height in both stylesheets
        _icon_size = QSize(20, 20)
        _icon_offset = (_tab_size - _icon_size.width()) // 2  # centers the 20px icon in the tab
        _icon_cache = {}
        _vertical_tab_shapes = {
            QTabBar.RoundedWest, QTabBar.TriangularWest,
            QTabBar.RoundedEast, QTabBar.TriangularEast,
        }

        def _make_nav_icon(filename, rotate=False):
            """Load SVG, center in tab canvas; optionally rotate for vertical tab bars."""
            cache_key = (filename, rotate)
            if cache_key in _icon_cache:
                return _icon_cache[cache_key]
            raw_icon = QIcon(os.path.join(_img_dir, filename))
            src_pix = raw_icon.pixmap(_icon_size)
            canvas = QPixmap(_tab_size, _tab_size)
            canvas.fill(Qt.transparent)
            painter = QPainter(canvas)
            painter.drawPixmap(_icon_offset, _icon_offset, src_pix)
            painter.end()
            if rotate:
                icon = QIcon(canvas.transformed(QTransform().rotate(90)))
            else:
                icon = QIcon(canvas)
            _icon_cache[cache_key] = icon
            return icon

        _nav_by_title = {}
        _nav_by_object = {}
        _registered_docks = []

        for object_name, filename in DOCK_NAV_ICONS:
            dock = win.findChild(QDockWidget, object_name)
            if dock is None:
                continue
            icon = _make_nav_icon(filename, rotate=False)
            _nav_by_object[object_name] = icon
            _nav_by_title[dock.windowTitle()] = icon
            _registered_docks.append(dock)
            dock.setWindowIcon(icon)

        def _tabbar_is_vertical(tabbar):
            return tabbar.shape() in _vertical_tab_shapes

        def _icon_for_dock_title(title, rotate=False):
            icon = _nav_by_title.get(title)
            if icon is None:
                return None
            if not rotate:
                return icon
            for object_name, filename in DOCK_NAV_ICONS:
                dock = win.findChild(QDockWidget, object_name)
                if dock is not None and dock.windowTitle() == title:
                    return _make_nav_icon(filename, rotate=True)
            return icon

        def _dock_tab_group_titles(anchor_dock):
            """Ordered window titles for docks tabified with anchor_dock."""
            first = anchor_dock
            for dock in win.findChildren(QDockWidget):
                tabified = win.tabifiedDockWidgets(dock)
                if anchor_dock in tabified:
                    first = dock
                    break
            chain = [first]
            current = first
            while win.tabifiedDockWidgets(current):
                nxt = win.tabifiedDockWidgets(current)[0]
                if nxt in chain:
                    break
                chain.append(nxt)
                current = nxt
            return [
                d.windowTitle() for d in chain
                if d.objectName() in _nav_by_object
            ]

        def _apply_tab_icons():
            """Write icons onto every QTabBar that hosts nav docks."""
            for tabbar in win.findChildren(QTabBar):
                if tabbar.parent() and isinstance(tabbar.parent(), QTabWidget):
                    widget_name = tabbar.parent().objectName() or ""
                    if widget_name in ("exportTabs", "tabCategories", "tabCredits"):
                        continue
                tabbar.setIconSize(QSize(_tab_size, _tab_size))
                rotate = _tabbar_is_vertical(tabbar)
                has_nav = False
                for i in range(tabbar.count()):
                    text = tabbar.tabText(i)
                    tooltip = tabbar.tabToolTip(i)
                    key = None
                    if text in _nav_by_title:
                        key = text
                    elif tooltip in _nav_by_title:
                        key = tooltip
                    else:
                        for dock in _registered_docks:
                            titles = _dock_tab_group_titles(dock)
                            if len(titles) == tabbar.count() and i < len(titles):
                                key = titles[i]
                                break
                    if key is None:
                        continue
                    icon = _icon_for_dock_title(key, rotate=rotate)
                    if icon is None:
                        continue
                    has_nav = True
                    tabbar.setTabIcon(i, icon)
                    tabbar.setTabToolTip(i, key)
                    tabbar.setTabText(i, "")
                if has_nav:
                    tabbar.setExpanding(False)
                    tabbar.setStyleSheet(resolve("""
                        QTabBar::tab {
                            min-width:  40px;
                            max-width:  40px;
                            min-height: 40px;
                            max-height: 40px;
                            padding:    0px;
                            margin:     2px 0px;
                            border: none;
                            border-left: 2px solid transparent;
                        }
                        QTabBar::tab:selected {
                            background: transparent;
                            border-left: 2px solid {text};
                        }
                        QTabBar::tab:hover:!selected {
                            background: {hover};
                        }
                    """))

        def _schedule_tab_icons():
            QTimer.singleShot(150, _apply_tab_icons)

        _apply_tab_icons()
        QTimer.singleShot(200, _apply_tab_icons)

        for _dock in _registered_docks:
            _dock.visibilityChanged.connect(_schedule_tab_icons)
            _dock.dockLocationChanged.connect(lambda _area: _schedule_tab_icons())

        # ── Dock close/float via right-click only ─────────────────────

        def _dock_context_menu(dock, global_pos):
            menu = QMenu(dock)
            if dock.isFloating():
                action = menu.addAction("Dock")
                action.triggered.connect(lambda: dock.setFloating(False))
            else:
                action = menu.addAction("Undock / Float")
                action.triggered.connect(lambda: dock.setFloating(True))
            menu.addSeparator()
            close_action = menu.addAction("Close Panel")
            close_action.triggered.connect(dock.close)
            menu.exec_(global_pos)

        for dock in win.findChildren(QDockWidget):
            dock.setContextMenuPolicy(Qt.CustomContextMenu)
            dock.customContextMenuRequested.connect(
                lambda pos, d=dock: _dock_context_menu(d, d.mapToGlobal(pos))
            )

        # Transparent spacer for toolbar end padding
        spacer = QWidget(self.app.window)
        spacer.setFixedSize(12, 1)
        spacer.setStyleSheet("background: transparent;")

        # ── Main toolbar (icon-only; action text shown as tooltip on hover) ──
        toolbar_buttons = [
            {"action": self.app.window.actionNew,         "icon": "themes/cosmic/images/tool-new-project.svg",  "style": Qt.ToolButtonIconOnly},
            {"action": self.app.window.actionOpen,        "icon": "themes/cosmic/images/tool-open-project.svg", "style": Qt.ToolButtonIconOnly},
            {"action": self.app.window.actionImportFiles, "icon": "themes/cosmic/images/tool-import-files.svg", "style": Qt.ToolButtonIconOnly},
            {"action": self.app.window.actionProfile,     "icon": "themes/cosmic/images/tool-profile.svg",      "style": Qt.ToolButtonIconOnly},
            {"expand": True},
            {"widget": self.app.window.agent_selector_button},
            {"action": self.app.window.actionSave,        "icon": "themes/cosmic/images/tool-save-project.svg", "style": Qt.ToolButtonIconOnly},
            {
                "action": self.app.window.actionExportVideo,
                "icon": "themes/cosmic/images/tool-export.svg",
                "style": Qt.ToolButtonIconOnly,
                "stylesheet": (
                    resolve("QToolButton { background-color: {accent}; color: {white}; border: none; border-radius: 6px; padding: 8px 12px; } "
                            "QToolButton:hover, QToolButton:pressed { background-color: {accent_hover}; }")
                ),
            },
            {
                "action": self.app.window.actionLogout,
                "icon": "themes/cosmic/images/tool-logout.svg",
                "style": Qt.ToolButtonIconOnly,
                "stylesheet": (
                    "QToolButton { background: transparent; border: none; border-radius: 6px; padding: 8px 10px; } "
                    "QToolButton:hover { background-color: rgba(239,68,68,0.15); } "
                    "QToolButton:pressed { background-color: rgba(239,68,68,0.25); }"
                ),
            },
            {
                # The update pill styles itself per state and paints its own
                # download progress, so it carries no stylesheet here
                "widget": self.app.window.update_status_button,
                "visible": self.app.window.update_status_button.is_active,
            },
        ]
        # Linked clips pill (classes/handoff): render progress, "N linked clips changed"
        handoff_status = getattr(self.app.window, "handoff_status", None)
        if handoff_status is not None:
            toolbar_buttons.insert(toolbar_buttons.index(next(
                b for b in toolbar_buttons if b.get("widget") is self.app.window.agent_selector_button)),
                {"widget": handoff_status, "visible": handoff_status.is_active})
        self.set_toolbar_buttons(self.app.window.toolBar, icon_size=20, settings=toolbar_buttons)

        self.app.window.actionColor_Grade_View.setIcon(
            QIcon(os.path.join(PATH, "themes/cosmic/images/view-color.svg"))
        )

        # ── Timeline toolbar (all icon-only) ─────────────────────────
        timeline_buttons = [
            {"action": self.app.window.actionAddTrack,        "icon": "themes/cosmic/images/tool-add-track.svg",       "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin-left: 12px; }"},
            {"action": self.app.window.actionUndo,            "icon": "themes/cosmic/images/tool-undo.svg",            "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin-right: 0; border-top-right-radius: 0; border-bottom-right-radius: 0; }"},
            {"action": self.app.window.actionRedo,            "icon": "themes/cosmic/images/tool-redo.svg",            "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin-left: 0; border-top-left-radius: 0; border-bottom-left-radius: 0; }"},
            {"action": self.app.window.actionSnappingTool,    "icon": "themes/cosmic/images/tool-snapping.svg",        "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin-right: 0; border-top-right-radius: 0; border-bottom-right-radius: 0; }"},
            {"action": self.app.window.actionTimingTool,      "icon": "themes/cosmic/images/tool-timing.svg",          "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin: 0; border-radius: 0; }"},
            {"action": self.app.window.actionRazorTool,       "icon": "themes/cosmic/images/tool-razor.svg",           "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin-left: 0; border-top-left-radius: 0; border-bottom-left-radius: 0; }"},
            {"action": self.app.window.actionAddMarker,       "icon": "themes/cosmic/images/tool-add-marker.svg",      "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin-right: 0; border-top-right-radius: 0; border-bottom-right-radius: 0; }"},
            {"action": self.app.window.actionPreviousMarker,  "icon": "themes/cosmic/images/tool-prev-marker.svg",     "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin: 0; border-radius: 0; }"},
            {"action": self.app.window.actionNextMarker,      "icon": "themes/cosmic/images/tool-next-marker.svg",     "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin-left: 0; border-top-left-radius: 0; border-bottom-left-radius: 0; }"},
            {"action": self.app.window.actionCenterOnPlayhead,"icon": "themes/cosmic/images/tool-center-playhead.svg", "style": Qt.ToolButtonIconOnly, "stylesheet": "QToolButton { margin-right: 8px; }"},
            {"widget": self.app.window.sliderZoomWidget},
            {"widget": spacer},
        ]
        self.set_toolbar_buttons(self.app.window.timelineToolbar, icon_size=16, settings=timeline_buttons)

        # ── Video toolbar ─────────────────────────────────────────────
        toolbar_buttons = [
            {"expand": True},
            {"action": self.app.window.actionJumpStart,   "icon": "themes/cosmic/images/tool-media-skip-back.svg",    "style": Qt.ToolButtonIconOnly},
            {"action": self.app.window.actionRewind,      "icon": "themes/cosmic/images/tool-media-rewind.svg",       "style": Qt.ToolButtonIconOnly},
            {"action": self.app.window.actionPlay,        "icon": "themes/cosmic/images/tool-media-play.svg",         "style": Qt.ToolButtonIconOnly},
            {"action": self.app.window.actionFastForward, "icon": "themes/cosmic/images/tool-media-forward.svg",      "style": Qt.ToolButtonIconOnly},
            {"action": self.app.window.actionJumpEnd,     "icon": "themes/cosmic/images/tool-media-skip-forward.svg", "style": Qt.ToolButtonIconOnly},
            {"expand": True},
        ]
        self.set_toolbar_buttons(self.app.window.videoToolbar, icon_size=32, settings=toolbar_buttons)

        # ── Timeline CSS overrides ─────────────────────────────────────
        self.app.window.timeline.apply_theme(resolve("""
            /* ── Timeline body ──────────────────────────────── */
            body {
                background: {surface};
            }

            /* ── Scrollbars ─────────────────────────────────── */
            ::-webkit-scrollbar {
                width: 6px;
                height: 6px;
            }
            ::-webkit-scrollbar-thumb {
                background: rgba(255, 255, 255, 0.14);
                border-radius: 3px;
            }
            ::-webkit-scrollbar-thumb:hover {
                background: rgba(255, 255, 255, 0.26);
            }
            ::-webkit-scrollbar-track {
                background: {surface};
                box-shadow: none;
            }
            ::-webkit-scrollbar-corner { background: {surface}; }

            /* ── Ruler ──────────────────────────────────────── */
            #ruler_label {
                background: {surface};
            }
            #scrolling_ruler {
                background: {surface};
                border-bottom: 1px solid rgba(255, 255, 255, 0.06);
            }
            #ruler_time {
                color: {text};
                font-size: 11pt;
            }
            .ruler_time {
                color: {text_faint};
                font-size: 8pt;
            }
            .tick_mark {
                background-color: rgba(255, 255, 255, 0.2);
            }

            /* ── Track control panel ─────────────────────────── */
            .track_name {
                font-size: 9pt;
                background: {surface};
                border: none;
                border-bottom: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 0;
                box-shadow: none;
                margin-left: 0;
                height: 48px;
                color: {text};
            }
            .track_top {
                padding-top: 4px;
                padding-left: 10px;
                background: none;
            }
            .track_label { text-shadow: none; }

            /* ── Track lanes ─────────────────────────────────── */
            .track {
                background: {bg};
                border: none;
                border-top: 1px solid rgba(255, 255, 255, 0.04);
                border-bottom: 1px solid rgba(255, 255, 255, 0.04);
                border-radius: 0;
                height: 48px;
                box-shadow: none;
                transition: background 0.15s ease;
            }
            .track:hover {
                background: {hover};
            }
            .track_disabled {
                background: {sunken} !important;
            }
            .track-resize-handle {
                background-color: {surface};
                border: 1px solid rgba(255, 255, 255, 0.04);
                border-left: none;
                border-radius: 0;
                transition: background-color 0.15s ease;
            }
            .track-resize-handle:hover { background-color: {raised}; }

            /* ── Clips ───────────────────────────────────────── */
            .clip {
                font-size: 8pt;
                background: {clip};
                border: 1px solid {clip_border};
                border-radius: 4px;
                box-sizing: border-box;
                height: 48px;
                min-height: 48px;
                box-shadow: none;
                display: flex;
                opacity: 0.95;
                transition: filter 0.12s ease, opacity 0.12s ease, border-color 0.12s ease;
                cursor: grab;
            }
            .clip:hover {
                filter: brightness(1.08);
                opacity: 1;
                border-color: rgba(255, 255, 255, 0.32);
            }
            .clip.ui-draggable-dragging {
                cursor: grabbing;
                opacity: 0.85;
                box-shadow: 0 4px 16px rgba(0,0,0,0.5);
                transform-origin: center center;
            }
            .clip_top {
                background: none;
                position: absolute;
                top: 0;
                width: 100%;
                display: flex;
                justify-content: flex-start;
                align-items: center;
                z-index: 2;
            }
            .clip_label {
                color: {text};
                text-shadow: none;
                font-size: 9pt;
            }
            .thumb-container,
            .audio-container {
                position: absolute;
                top: 0; left: 0; right: 0; bottom: 0;
                overflow: hidden;
                margin-top: 0;
                border-radius: 5px;
            }
            .thumb {
                border-radius: 5px;
                margin: 0;
                height: 100%;
                object-fit: cover;
                z-index: 1;
            }
            .audio { border-radius: 5px; }

            /* Selection states */
            .ui-selecting {
                border: 1px solid {accent} !important;
            }
            .ui-selected {
                border: 2px solid {accent} !important;
                filter: brightness(1.15);
            }

            /* Edge stop indicators */
            .left_edge_stop  { border-left: 2px solid {accent}; }
            .right_edge_stop { border-right: 2px solid {accent}; }

            /* ── Transitions ─────────────────────────────────── */
            .transition {
                background: rgba(58, 114, 245, 0.30);
                border: 1px solid rgba(58, 114, 245, 0.55);
                border-radius: 4px;
                height: 48px;
                min-height: 48px;
                opacity: 0.8;
                box-shadow: none;
                transition: opacity 0.12s ease;
            }
            .transition:hover { opacity: 0.95; }
            .transition_top { background: none; }
            .transition.ui-selected { opacity: 1; }
            .highlight_transition { border-color: {accent} !important; }

            /* ── Playhead ────────────────────────────────────── */
            .playhead-top {
                margin-left: -6px;
                margin-top: 20px;
                width: 12px;
                height: 188px;
                background-image: url(../themes/cosmic/images/playhead.svg);
            }
            .playhead-line {
                z-index: 9999;
                position: absolute;
                top: 0;
                width: 2px;
                background-color: {playhead};
                margin: -1px;
                pointer-events: none;
            }

            /* ── Snap line ───────────────────────────────────── */
            .snapping-line {
                background-color: rgba(58, 114, 245, 0.7);
            }

            /* ── Selection box ───────────────────────────────── */
            .ui-selectable-helper {
                border: 1px solid {accent};
                background-color: rgba(58, 114, 245, 0.12);
            }

            /* ── Keyframe icons ──────────────────────────────── */
            .point_bezier   { background-image: url(../themes/cosmic/images/keyframe-bezier.svg); }
            .point_linear   { background-image: url(../themes/cosmic/images/keyframe-linear.svg); }
            .point_constant { background-image: url(../themes/cosmic/images/keyframe-constant.svg); }

            /* ── Track control icons ─────────────────────────── */
            .track-keyframe-panel-disabled { background-image: url(../themes/cosmic/images/track-keyframe-panel-show-disabled.svg); }
            .track-keyframe-panel-enabled  { background-image: url(../themes/cosmic/images/track-keyframe-panel-show-enabled.svg); }
            .track-add-above-disabled      { background-image: url(../themes/cosmic/images/track-add-above-disabled.svg); }
            .track-add-above-enabled       { background-image: url(../themes/cosmic/images/track-add-above-enabled.svg); }
            .track-add-below-disabled      { background-image: url(../themes/cosmic/images/track-add-below-disabled.svg); }
            .track-add-below-enabled       { background-image: url(../themes/cosmic/images/track-add-below-enabled.svg); }
            .track-delete-disabled         { background-image: url(../themes/cosmic/images/track-delete-disabled.svg); }
            .track-delete-enabled          { background-image: url(../themes/cosmic/images/track-delete-enabled.svg); }
            .track-locked-disabled         { background-image: url(../themes/cosmic/images/track-locked-disabled.svg); }
            .track-locked-enabled          { background-image: url(../themes/cosmic/images/track-locked-enabled.svg); }
            .track-unlocked-disabled       { background-image: url(../themes/cosmic/images/track-unlocked-disabled.svg); }
            .track-unlocked-enabled        { background-image: url(../themes/cosmic/images/track-unlocked-enabled.svg); }
            .keyframe-panel-add            { background-image: url(../themes/cosmic/images/keyframe-panel-add.svg); }
            .marker_icon                   { background-image: url(../themes/cosmic/images/marker.svg); }
        """))

        # Emit signal
        self.app.window.ThemeChangedSignal.emit(self)

    def togglePlayIcon(self, isPlay):
        """Toggle the play icon between play and pause."""
        button = self.app.window.videoToolbar.widgetForAction(self.app.window.actionPlay)
        if button:
            icon_name = "tool-media-pause.svg" if isPlay else "tool-media-play.svg"
            icon_path = os.path.join(PATH, "themes/cosmic/images", icon_name)
            icon = self.create_svg_icon(icon_path, self.app.window.videoToolbar.iconSize())
            button.setIcon(icon)
