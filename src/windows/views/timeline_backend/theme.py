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
    border_width: float = 0
    font_color: QColor = field(default_factory=QColor)
    font_size: int = 0
    height: int = 0
    background_image: Optional[QPixmap] = None
    shadow_color: QColor = field(default_factory=QColor)
    shadow_blur: int = 0
    thumb_width: int = 0
    thumb_height: int = 0


@dataclass
class TrackTheme(BasicTheme):
    """Theme for tracks."""

    name_background: QColor = field(default_factory=QColor)
    name_width: int = 0
    gap: int = 0
    name_border_color: QColor = field(default_factory=QColor)
    name_border_width: int = 0


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
    ruler_name_background: QColor = field(default_factory=QColor)
    ruler_name_background2: QColor = field(default_factory=QColor)
    ruler_time_font_size: int = 0
    menu_icon: Optional[QPixmap] = None
    menu_size: int = 0
    menu_margin: int = 0
    playhead_icon: Optional[QPixmap] = None
    playhead_icon_width: int = 0
    playhead_icon_height: int = 0
    playhead_icon_offset_x: int = 0
    playhead_icon_offset_y: int = 0
    ruler_time_pad_left: int = 0
    ruler_time_pad_top: int = 0
    ruler_label_top: int = 0


def default_theme() -> TimelineTheme:
    """Return a TimelineTheme with sensible defaults."""

    t = TimelineTheme()
    t.clip.background = QColor("#192332")
    t.clip.border_color = QColor("#53a0ed")
    t.clip.border_width = 1
    t.clip.font_color = QColor("#FFFFFF")
    t.clip.font_size = 9
    t.clip.border_radius = 8
    t.clip.height = 64
    t.clip.shadow_color = QColor(0, 0, 0, 255)
    t.clip.shadow_blur = 10
    t.clip.thumb_width = 66
    t.clip.thumb_height = 38

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
    t.track.name_border_color = t.track.border_color
    t.track.name_border_width = 1

    t.ruler.background = QColor("#141923")
    t.ruler.border_color = QColor("#FABE0A")
    t.ruler.font_color = QColor("#c8c8c8")
    t.ruler.font_size = 10
    t.ruler.height = 39
    t.ruler_name_background = t.track.name_background
    t.ruler_name_background2 = t.track.name_background
    t.ruler_time_font_size = 13

    t.menu_icon = QPixmap(
        os.path.normpath(os.path.join(
            os.path.dirname(__file__),
            "../../..",
            "timeline/media/images/menu.svg",
        ))
    )
    t.menu_size = 12
    t.menu_margin = 4

    t.playhead_icon = QPixmap(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "../../..",
                "timeline/media/images/playhead.svg",
            )
        )
    )
    t.playhead_icon_width = 12
    t.playhead_icon_height = 188
    t.playhead_icon_offset_x = -6
    t.playhead_icon_offset_y = 20
    t.ruler_time_pad_left = 17
    t.ruler_time_pad_top = 12
    t.ruler_label_top = 6

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


def _parse_gradient(css: str, selector: str, prop: str):
    """Return up to two colors from a CSS gradient."""
    val = _css_prop(css, selector, prop)
    if not val:
        return None, None
    cols = re.findall(r"#(?:[0-9a-fA-F]{3,8})|rgba?\([^\)]+\)", val)
    qcols = [QColor(c) for c in cols if QColor(c).isValid()]
    if not qcols:
        return None, None
    first = qcols[0]
    second = qcols[1] if len(qcols) > 1 else None
    return first, second


def _parse_float(css: str, selector: str, prop: str) -> Optional[float]:
    val = _css_prop(css, selector, prop)
    if val:
        m = re.search(r"(-?[0-9.]+)", val)
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
            base = os.path.dirname(_CSS_PATH)
            found = None
            for i in range(3):
                candidate = os.path.normpath(
                    os.path.join(base, *([".."] * i), path)
                )
                if os.path.exists(candidate):
                    found = candidate
                    break
            path = found or os.path.normpath(os.path.join(base, path))
        if os.path.exists(path):
            return QPixmap(path)
    return None


def _parse_box_shadow(css: str, selector: str):
    """Return (color, blur) from a box-shadow property."""
    val = _css_prop(css, selector, "box-shadow")
    if not val:
        return None, None
    col = None
    m = re.search(r"#([0-9a-fA-F]{3,8})", val)
    if m:
        col = QColor("#" + m.group(1))
    else:
        m = re.search(r"rgba?\([^\)]+\)", val)
        if m:
            col = QColor(m.group(0))
    nums = re.findall(r"(-?[0-9.]+)", val)
    blur = int(float(nums[2])) if len(nums) >= 3 else None
    return col, blur


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
            candidate = os.path.normpath(os.path.join(module_path, path))
            if not os.path.exists(candidate):
                candidate = os.path.normpath(os.path.join(os.path.dirname(module_path), path))
            path = candidate
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
    if not col:
        col = qt_theme.get_color(".clip", "border")
    if col:
        theme.clip.border_color = col
    val = qt_theme.get_int(".clip", "border-top")
    if val is None:
        val = qt_theme.get_int(".clip", "border")
    if val is not None:
        theme.clip.border_width = float(val)
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
    val = _css_prop(getattr(qt_theme, "style_sheet", ""), ".clip", "box-shadow")
    if val:
        col, blur = _parse_box_shadow(qt_theme.style_sheet, ".clip")
        if col:
            theme.clip.shadow_color = col
        if blur is not None:
            theme.clip.shadow_blur = blur
    val = qt_theme.get_int(".thumb", "width")
    if val is not None:
        theme.clip.thumb_width = val
    val = qt_theme.get_int(".thumb", "height")
    if val is not None:
        theme.clip.thumb_height = val

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
    if not col:
        col = qt_theme.get_color(".track", "border")
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
    col = qt_theme.get_color(".track_name", "border-left")
    if col:
        theme.track.name_border_color = col
    val = qt_theme.get_int(".track_name", "border-left")
    if val is not None:
        theme.track.name_border_width = val

    # Ruler settings
    col = qt_theme.get_color("#ruler", "background")
    if col:
        theme.ruler.background = col
    col1, col2 = _parse_gradient(getattr(qt_theme, "style_sheet", ""), "#ruler_label", "background")
    if col1:
        theme.ruler_name_background = col1
    if col2:
        theme.ruler_name_background2 = col2
    else:
        col = qt_theme.get_color("#ruler_label", "background")
        if col:
            theme.ruler_name_background = col
    col = qt_theme.get_color(".tick_mark", "background-color")
    if col:
        theme.ruler.border_color = col
    col = qt_theme.get_color(".ruler_time", "color")
    if col:
        theme.ruler.font_color = col
    val = qt_theme.get_int(".ruler_time", "font-size")
    if val is not None:
        theme.ruler.font_size = val
    val = qt_theme.get_int("#ruler_time", "font-size")
    if val is not None:
        theme.ruler_time_font_size = val
    val = qt_theme.get_int(".ruler_time", "top")
    if val is not None:
        theme.ruler_label_top = val
    val = qt_theme.get_int("#ruler", "height")
    if val is not None:
        theme.ruler.height = val
    val = qt_theme.get_int("#ruler_time", "padding-left")
    if val is not None:
        theme.ruler_time_pad_left = val
    val = qt_theme.get_int("#ruler_time", "padding-top")
    if val is not None:
        theme.ruler_time_pad_top = val

    # Playhead
    col = qt_theme.get_color(".playhead-line", "background-color")
    if col:
        theme.playhead_color = col
    val = qt_theme.get_int(".playhead-line", "width")
    if val is not None:
        theme.playhead_width = float(val)
    img = _theme_pixmap(qt_theme, ".playhead-top", "background-image")
    if img:
        theme.playhead_icon = img
    val = qt_theme.get_int(".playhead-top", "width")
    if val is not None:
        theme.playhead_icon_width = val
    val = qt_theme.get_int(".playhead-top", "height")
    if val is not None:
        theme.playhead_icon_height = val
    val = qt_theme.get_int(".playhead-top", "margin-left")
    if val is not None:
        theme.playhead_icon_offset_x = val
    val = qt_theme.get_int(".playhead-top", "margin-top")
    if val is not None:
        theme.playhead_icon_offset_y = val

    img = _theme_pixmap(qt_theme, ".menu", "background-image")
    if img:
        theme.menu_icon = img
    val = qt_theme.get_int(".menu", "width")
    if val is not None:
        theme.menu_size = val
    val = qt_theme.get_int(".menu", "margin")
    if val is not None:
        theme.menu_margin = val

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
    if not col:
        col = _parse_color(css, ".clip", "border")
    if col:
        theme.clip.border_color = col
    val = _parse_float(css, ".clip", "border-top")
    if val is None:
        val = _parse_float(css, ".clip", "border")
    if val is not None:
        theme.clip.border_width = float(val)
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
    col2, blur = _parse_box_shadow(css, ".clip")
    if col2:
        theme.clip.shadow_color = col2
    if blur is not None:
        theme.clip.shadow_blur = blur
    val = _parse_float(css, ".thumb", "width")
    if val is not None:
        theme.clip.thumb_width = int(val)
    val = _parse_float(css, ".thumb", "height")
    if val is not None:
        theme.clip.thumb_height = int(val)

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
    if not col:
        col = _parse_color(css, ".track", "border")
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
    col = _parse_color(css, ".track_name", "border-left")
    if col:
        theme.track.name_border_color = col
    val = _parse_float(css, ".track_name", "border-left")
    if val is not None:
        theme.track.name_border_width = int(val)

    # Ruler
    col = _parse_color(css, "#ruler", "background")
    if col:
        theme.ruler.background = col
    col1, col2 = _parse_gradient(css, "#ruler_label", "background")
    if col1:
        theme.ruler_name_background = col1
    if col2:
        theme.ruler_name_background2 = col2
    col = _parse_color(css, ".tick_mark", "background-color")
    if col:
        theme.ruler.border_color = col
    col = _parse_color(css, ".ruler_time", "color")
    if col:
        theme.ruler.font_color = col
    val = _parse_float(css, ".ruler_time", "font-size")
    if val is not None:
        theme.ruler.font_size = int(val)
    val = _parse_float(css, "#ruler_time", "font-size")
    if val is not None:
        theme.ruler_time_font_size = int(val)
    val = _parse_float(css, ".ruler_time", "top")
    if val is not None:
        theme.ruler_label_top = int(val)
    val = _parse_float(css, "#ruler", "height")
    if val is not None:
        theme.ruler.height = int(val)
    val = _parse_float(css, "#ruler_time", "padding-left")
    if val is not None:
        theme.ruler_time_pad_left = int(val)
    val = _parse_float(css, "#ruler_time", "padding-top")
    if val is not None:
        theme.ruler_time_pad_top = int(val)

    # Playhead
    col = _parse_color(css, ".playhead-line", "background-color")
    if col:
        theme.playhead_color = col
    val = _parse_float(css, ".playhead-line", "width")
    if val is not None:
        theme.playhead_width = val
    img = _parse_pixmap(css, ".playhead-top", "background-image")
    if img:
        theme.playhead_icon = img
    val = _parse_float(css, ".playhead-top", "width")
    if val is not None:
        theme.playhead_icon_width = int(val)
    val = _parse_float(css, ".playhead-top", "height")
    if val is not None:
        theme.playhead_icon_height = int(val)
    val = _parse_float(css, ".playhead-top", "margin-left")
    if val is not None:
        theme.playhead_icon_offset_x = int(val)
    val = _parse_float(css, ".playhead-top", "margin-top")
    if val is not None:
        theme.playhead_icon_offset_y = int(val)

    img = _parse_pixmap(css, ".menu", "background-image")
    if img:
        theme.menu_icon = img
    val = _parse_float(css, ".menu", "width")
    if val is not None:
        theme.menu_size = int(val)
    m = _css_prop(css, ".menu", "margin")
    if m:
        m_val = re.search(r"(-?[0-9.]+)", m)
        if m_val:
            theme.menu_margin = int(float(m_val.group(1)))

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
