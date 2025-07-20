"""Utilities for parsing and applying timeline CSS themes."""

from dataclasses import dataclass, field
import os
import re
from typing import Optional

from PyQt5.QtGui import QColor, QPixmap


@dataclass
class BasicTheme:
    """Common style options for timeline elements."""

    background: QColor = field(default_factory=QColor)
    border_color: QColor = field(default_factory=QColor)
    border_radius: int = 0
    font_color: QColor = field(default_factory=QColor)
    font_size: int = 0
    height: int = 0
    background_image: QPixmap | None = None


@dataclass
class TrackTheme(BasicTheme):
    """Theme for tracks."""

    name_background: QColor = field(default_factory=QColor)
    name_width: int = 0
    gap: int = 0


@dataclass
class TimelineTheme:
    """Container for all timeline related themes."""

    background: QColor = field(default_factory=lambda: QColor("#191919"))
    playhead_color: QColor = field(default_factory=lambda: QColor("#ff0024"))
    playhead_width: float = 2.0
    clip_selected: QColor = field(default_factory=lambda: QColor("red"))
    selection: QColor = field(default_factory=lambda: QColor(0, 120, 215, 80))

    clip: BasicTheme = field(default_factory=BasicTheme)
    transition: BasicTheme = field(default_factory=BasicTheme)
    track: TrackTheme = field(default_factory=TrackTheme)
    ruler: BasicTheme = field(default_factory=BasicTheme)


def default_theme() -> TimelineTheme:
    """Return a TimelineTheme with sensible defaults."""

    t = TimelineTheme()
    t.clip.background = QColor("#192332")
    t.clip.border_color = QColor("#53a0ed")
    t.clip.font_color = QColor("#FFFFFF")
    t.clip.font_size = 9
    t.clip.border_radius = 8
    t.clip.height = 64

    t.transition.background = QColor("#0192c1")
    t.transition.border_color = QColor("#0192c1")
    t.transition.font_color = QColor("#FFFFFF")
    t.transition.font_size = 9
    t.transition.border_radius = 8
    t.transition.height = 64
    t.transition.background_image = QPixmap(
        os.path.normpath(os.path.join(
            os.path.dirname(__file__),
            "../../..",
            "timeline/media/images/transition.svg",
        ))
    )

    t.track.background = QColor("#283241")
    t.track.border_color = QColor("#4b92ad")
    t.track.name_background = QColor("#192332")
    t.track.font_color = QColor("#FFFFFF")
    t.track.font_size = 9
    t.track.border_radius = 8
    t.track.height = 62
    t.track.name_width = 140
    t.track.gap = 8

    t.ruler.background = QColor("#141923")
    t.ruler.border_color = QColor("#FABE0A")
    t.ruler.font_color = QColor("#c8c8c8")
    t.ruler.font_size = 13
    t.ruler.height = 39

    return t


DEFAULT_THEME = default_theme()

# Load the main timeline CSS used by the web backends. Many timeline style
# values are defined here and are reused by the QWidget backend.
_CSS_PATH = os.path.normpath(os.path.join(
    os.path.dirname(__file__),
    "../../..",
    "timeline/media/css/main.css",
))
try:
    with open(_CSS_PATH, "r", encoding="utf-8") as _f:
        MAIN_CSS = _f.read()
except OSError:
    MAIN_CSS = ""


def _css_prop(css: str, selector: str, prop: str) -> Optional[str]:
    """Return property *prop* from the CSS *selector* block."""
    block_pat = rf"{re.escape(selector)}\s*\{{([^}}]*)\}}"
    m = re.search(block_pat, css, re.MULTILINE)
    if not m:
        return None
    block = m.group(1)
    m2 = re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", block)
    return m2.group(1).strip() if m2 else None


def _parse_color(css: str, selector: str, prop: str) -> Optional[QColor]:
    val = _css_prop(css, selector, prop)
    if not val:
        return None
    m = re.search(r"#([0-9a-fA-F]{3,8})", val)
    if m:
        return QColor("#" + m.group(1))
    m = re.search(r"rgba?\([^\)]+\)", val)
    if m:
        return QColor(m.group(0))
    parts = val.split()
    if parts and QColor(parts[-1]).isValid():
        return QColor(parts[-1])
    return None


def _parse_float(css: str, selector: str, prop: str) -> Optional[float]:
    val = _css_prop(css, selector, prop)
    if val:
        m = re.search(r"([0-9.]+)", val)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _parse_pixmap(css: str, selector: str, prop: str) -> Optional[QPixmap]:
    val = _css_prop(css, selector, prop)
    if not val:
        return None
    m = re.search(r"url\(([^)]+)\)", val)
    if m:
        path = m.group(1).strip('"\'')
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(os.path.dirname(_CSS_PATH), path))
        if os.path.exists(path):
            return QPixmap(path)
    return None


def _theme_pixmap(qt_theme, selector: str, prop: str) -> Optional[QPixmap]:
    if not qt_theme or not hasattr(qt_theme, "style_sheet"):
        return None
    val = _css_prop(qt_theme.style_sheet, selector, prop)
    if not val:
        return None
    m = re.search(r"url\(([^)]+)\)", val)
    if m:
        path = m.group(1).strip('"\'')
        module_path = os.path.dirname(__import__(qt_theme.__module__).__file__)
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(module_path, path))
        if os.path.exists(path):
            return QPixmap(path)
    return None


def _apply_theme_obj(theme: TimelineTheme, qt_theme) -> TimelineTheme:
    """Update *theme* from a Qt theme instance using BaseTheme helpers."""

    if not qt_theme:
        return theme

    # Backgrounds
    col = qt_theme.get_color("body", "background")
    if col:
        theme.background = col

    # Clip settings
    col = qt_theme.get_color(".clip", "background")
    if col:
        theme.clip.background = col
    col = qt_theme.get_color(".clip", "border-top")
    if col:
        theme.clip.border_color = col
    val = qt_theme.get_int(".clip", "border-radius")
    if val is not None:
        theme.clip.border_radius = val
    col = qt_theme.get_color(".clip_label", "color")
    if col:
        theme.clip.font_color = col
    val = qt_theme.get_int(".clip", "font-size")
    if val is not None:
        theme.clip.font_size = val
    val = qt_theme.get_int(".clip", "height")
    if val is not None:
        theme.clip.height = val

    # Transition settings
    col = qt_theme.get_color(".transition", "background")
    if col:
        theme.transition.background = col
    col = qt_theme.get_color(".transition", "border-top")
    if col:
        theme.transition.border_color = col
    val = qt_theme.get_int(".transition", "border-radius")
    if val is not None:
        theme.transition.border_radius = val
    img = _theme_pixmap(qt_theme, ".transition", "background-image")
    if img:
        theme.transition.background_image = img
    col = qt_theme.get_color(".transition_label", "color")
    if col:
        theme.transition.font_color = col
    val = qt_theme.get_int(".transition", "font-size")
    if val is not None:
        theme.transition.font_size = val
    val = qt_theme.get_int(".transition", "height")
    if val is not None:
        theme.transition.height = val

    # Track settings
    col = qt_theme.get_color(".track", "background")
    if col:
        theme.track.background = col
    col = qt_theme.get_color(".track", "border-top")
    if col:
        theme.track.border_color = col
    val = qt_theme.get_int(".track", "border-radius")
    if val is not None:
        theme.track.border_radius = val
    col = qt_theme.get_color(".track_name", "color")
    if not col:
        col = qt_theme.get_color(".track_label", "color")
    if col:
        theme.track.font_color = col
    val = qt_theme.get_int(".track", "font-size")
    if val is not None:
        theme.track.font_size = val
    val = qt_theme.get_int(".track", "height")
    if val is not None:
        theme.track.height = val
    col = qt_theme.get_color(".track_name", "background")
    if col:
        theme.track.name_background = col
    val = qt_theme.get_int(".track_name", "width")
    if val is not None:
        theme.track.name_width = val
    val = qt_theme.get_int(".track", "margin-bottom")
    if val is not None:
        theme.track.gap = val

    # Ruler settings
    col = qt_theme.get_color("#ruler", "background")
    if col:
        theme.ruler.background = col
    col = qt_theme.get_color(".tick_mark", "background-color")
    if col:
        theme.ruler.border_color = col
    col = qt_theme.get_color(".ruler_time", "color")
    if col:
        theme.ruler.font_color = col
    val = qt_theme.get_int(".ruler_time", "font-size")
    if val is not None:
        theme.ruler.font_size = val
    val = qt_theme.get_int("#ruler", "height")
    if val is not None:
        theme.ruler.height = val

    # Playhead
    col = qt_theme.get_color(".playhead-line", "background-color")
    if col:
        theme.playhead_color = col
    val = qt_theme.get_int(".playhead-line", "width")
    if val is not None:
        theme.playhead_width = float(val)

    return theme


def _apply_css(theme: TimelineTheme, css: str) -> TimelineTheme:
    """Update *theme* with values parsed from *css*."""

    if not css:
        return theme

    col = _parse_color(css, "body", "background")
    if col:
        theme.background = col

    # Clip
    col = _parse_color(css, ".clip", "background")
    if col:
        theme.clip.background = col
    col = _parse_color(css, ".clip", "border-top")
    if col:
        theme.clip.border_color = col
    val = _parse_float(css, ".clip", "border-radius")
    if val is not None:
        theme.clip.border_radius = int(val)
    col = _parse_color(css, ".clip_label", "color")
    if col:
        theme.clip.font_color = col
    val = _parse_float(css, ".clip", "font-size")
    if val is not None:
        theme.clip.font_size = int(val)
    val = _parse_float(css, ".clip", "height")
    if val is not None:
        theme.clip.height = int(val)

    # Transition
    col = _parse_color(css, ".transition", "background")
    if col:
        theme.transition.background = col
    col = _parse_color(css, ".transition", "border-top")
    if col:
        theme.transition.border_color = col
    val = _parse_float(css, ".transition", "border-radius")
    if val is not None:
        theme.transition.border_radius = int(val)
    img = _parse_pixmap(css, ".transition", "background-image")
    if img:
        theme.transition.background_image = img
    col = _parse_color(css, ".transition_label", "color")
    if col:
        theme.transition.font_color = col
    val = _parse_float(css, ".transition", "font-size")
    if val is not None:
        theme.transition.font_size = int(val)
    val = _parse_float(css, ".transition", "height")
    if val is not None:
        theme.transition.height = int(val)

    # Track
    col = _parse_color(css, ".track", "background")
    if col:
        theme.track.background = col
    col = _parse_color(css, ".track", "border-top")
    if col:
        theme.track.border_color = col
    val = _parse_float(css, ".track", "border-radius")
    if val is not None:
        theme.track.border_radius = int(val)
    col = _parse_color(css, ".track_name", "color")
    if not col:
        col = _parse_color(css, ".track_label", "color")
    if col:
        theme.track.font_color = col
    val = _parse_float(css, ".track", "font-size")
    if val is not None:
        theme.track.font_size = int(val)
    val = _parse_float(css, ".track", "height")
    if val is not None:
        theme.track.height = int(val)
    col = _parse_color(css, ".track_name", "background")
    if col:
        theme.track.name_background = col
    val = _parse_float(css, ".track_name", "width")
    if val is not None:
        theme.track.name_width = int(val)
    val = _parse_float(css, ".track", "margin-bottom")
    if val is not None:
        theme.track.gap = int(val)

    # Ruler
    col = _parse_color(css, "#ruler", "background")
    if col:
        theme.ruler.background = col
    col = _parse_color(css, ".tick_mark", "background-color")
    if col:
        theme.ruler.border_color = col
    col = _parse_color(css, ".ruler_time", "color")
    if col:
        theme.ruler.font_color = col
    val = _parse_float(css, ".ruler_time", "font-size")
    if val is not None:
        theme.ruler.font_size = int(val)
    val = _parse_float(css, "#ruler", "height")
    if val is not None:
        theme.ruler.height = int(val)

    # Playhead
    col = _parse_color(css, ".playhead-line", "background-color")
    if col:
        theme.playhead_color = col
    val = _parse_float(css, ".playhead-line", "width")
    if val is not None:
        theme.playhead_width = val

    return theme


def apply_theme(widget, css: str = "") -> bool:
    """Load theme values for *widget* and return True if geometry changed."""

    from classes.app import get_app

    app_theme = get_app().theme_manager.get_current_theme() if get_app() else None

    t = default_theme()

    # Start with defaults from the main CSS file
    t = _apply_css(t, MAIN_CSS)

    # Override with values from the active Qt theme instance
    if app_theme:
        t = _apply_theme_obj(t, app_theme)

    # Optional additional CSS overrides
    if isinstance(css, str):
        t = _apply_css(t, css)

    old_track_h = widget.track_height
    old_name_w = widget.track_name_width
    old_ruler_h = widget.ruler_height
    old_gap = getattr(widget, 'track_gap', 0)

    widget.theme = t

    if t.track.height:
        widget.track_height = t.track.height
    if t.track.name_width:
        widget.track_name_width = t.track.name_width
    if t.track.gap:
        widget.track_gap = t.track.gap
    if t.ruler.height:
        widget.ruler_height = t.ruler.height

    return (
        old_track_h != widget.track_height
        or old_name_w != widget.track_name_width
        or old_ruler_h != widget.ruler_height
        or old_gap != widget.track_gap
    )
