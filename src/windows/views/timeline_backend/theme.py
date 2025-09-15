"""Utilities for parsing and applying timeline CSS themes."""

from dataclasses import dataclass, field
import os
import re
from typing import Optional, Sequence, Union

from PyQt5.QtGui import QColor, QPixmap
from classes.logger import log


@dataclass
class BasicTheme:
    """Common style options for timeline elements."""

    background: QColor = field(default_factory=QColor)
    background2: QColor = field(default_factory=QColor)
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
    name_border_top_color: QColor = field(default_factory=QColor)
    name_border_top_width: int = 0
    name_border_bottom_color: QColor = field(default_factory=QColor)
    name_border_bottom_width: int = 0
    name_radius_tl: int = 0
    name_radius_bl: int = 0


@dataclass
class TimelineTheme:
    """Container for all timeline related themes."""

    background: QColor = field(default_factory=lambda: QColor("#000"))
    background2: QColor = field(default_factory=QColor)
    playhead_color: QColor = field(default_factory=lambda: QColor("#FFF"))
    playhead_width: float = 0.0
    clip_selected: QColor = field(default_factory=lambda: QColor("#FFF"))
    selection: QColor = field(default_factory=lambda: QColor(255, 255, 255, 80))
    selection_border: QColor = field(default_factory=QColor)
    selection_border_width: float = 0.0

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
    scrollbar_handle: QColor = field(default_factory=QColor)
    scrollbar_track: QColor = field(default_factory=QColor)
    scrollbar_width: int = 0


DEFAULT_THEME = TimelineTheme()

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


def _css_prop(
    css: str,
    selector: str,
    prop: str,
    source: str,
    *,
    log_selector: bool = True,
    log_property: bool = True,
) -> Optional[str]:
    """Return property *prop* from the CSS *selector* block.

    Logging can be disabled for selector or property misses using the optional
    flags. This is useful when calling code plans to fall back to alternate
    property names and does not want intermediate MISS messages.
    """
    block_pat = rf"{re.escape(selector)}\s*\{{([^}}]*)\}}"
    m = re.search(block_pat, css, re.MULTILINE)
    if not m:
        if log_selector:
            log.info("Theme MISS [%s] selector '%s'", source, selector)
        return None
    block = m.group(1)
    m2 = re.search(rf"(?:^|;)\s*{re.escape(prop)}\s*:\s*([^;]+)", block)
    if not m2:
        if log_property:
            log.info(
                "Theme MISS [%s] selector '%s' property '%s'",
                source,
                selector,
                prop,
            )
        return None
    return m2.group(1).strip()


def _color_from_str(val: str) -> Optional[QColor]:
    """Parse a CSS color value into a ``QColor``.

    Supports hex colors and ``rgb/rgba`` declarations with either integer or
    float components. Returns ``None`` if the string cannot be parsed.
    """
    val = val.strip()
    if not val:
        return None
    if val.startswith("#"):
        col = QColor(val)
        return col if col.isValid() else None
    m = re.match(r"rgba?\(([^)]+)\)", val)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) >= 3:
            try:
                r = int(float(parts[0]))
                g = int(float(parts[1]))
                b = int(float(parts[2]))
                a = 255
                if len(parts) >= 4:
                    a_part = parts[3]
                    if a_part.endswith("%"):
                        a = int(float(a_part[:-1]) * 2.55)
                    else:
                        fa = float(a_part)
                        a = int(fa * 255) if fa <= 1 else int(fa)
                return QColor(r, g, b, a)
            except ValueError:
                return None
    col = QColor(val)
    return col if col.isValid() else None


def _parse_color(
    css: str,
    selector: str,
    prop: Union[str, Sequence[str]],
    source: str,
    *,
    log_miss: bool = True,
    log_selector: bool = True,
) -> Optional[QColor]:
    props = (prop,) if isinstance(prop, str) else tuple(prop)
    val = None
    for i, p in enumerate(props):
        val = _css_prop(
            css,
            selector,
            p,
            source,
            log_selector=i == 0 and log_selector,
            log_property=False,
        )
        if val is not None:
            break
    if val is None:
        if log_miss:
            log.info(
                "Theme MISS [%s] selector '%s' property '%s'",
                source,
                selector,
                props[0],
            )
        return None
    m = re.search(r"#([0-9a-fA-F]{3,8})", val)
    if m:
        col = _color_from_str("#" + m.group(1))
        if col:
            return col
    m = re.search(r"rgba?\([^\)]+\)", val)
    if m:
        col = _color_from_str(m.group(0))
        if col:
            return col
    # Handle shorthand declarations like "1px solid red !important" by
    # scanning tokens from right to left and returning the first valid color.
    parts = re.split(r"\s+", val.strip())
    for token in reversed(parts):
        if token.lower() == "!important":
            continue
        col = _color_from_str(token)
        if col and col.isValid():
            return col
    if log_miss:
        log.info(
            "Theme MISS [%s] selector '%s' property '%s' invalid color '%s'",
            source,
            selector,
            prop,
            val,
        )
    return None


def _parse_gradient(
    css: str, selector: str, prop: str, source: str, *, log_miss: bool = True
):
    """Return up to two colors from a CSS gradient.

    The returned colors are ordered for a top-to-bottom gradient. If the CSS
    gradient specifies the opposite direction (bottom to top), the order of the
    colors is swapped so callers can simply paint from top to bottom.
    """
    val = _css_prop(css, selector, prop, source)
    if not val:
        if log_miss:
            log.info("Theme MISS [%s] selector '%s' property '%s'", source, selector, prop)
        return None, None
    cols = re.findall(r"#(?:[0-9a-fA-F]{3,8})|rgba?\([^\)]+\)", val)
    qcols = [_color_from_str(c) for c in cols]
    qcols = [c for c in qcols if c and c.isValid()]
    if not qcols:
        if log_miss:
            log.info(
                "Theme MISS [%s] selector '%s' property '%s' invalid gradient '%s'",
                source,
                selector,
                prop,
                val,
            )
        return None, None

    first = qcols[0]
    second = qcols[1] if len(qcols) > 1 else None

    # Detect bottom-to-top gradients and reverse the color order so callers can
    # always assume the first color is at the top.
    val_lower = val.lower()
    idx_bottom = val_lower.find("bottom")
    idx_top = val_lower.find("top")
    reverse = False
    if idx_bottom != -1 and idx_top != -1:
        reverse = idx_bottom < idx_top
    else:
        m = re.search(r"linear-gradient\((?:to\s+)?(top|bottom)", val_lower)
        if m:
            reverse = m.group(1) == "bottom"
        else:
            m = re.search(r"-webkit-linear-gradient\((top|bottom)", val_lower)
            if m:
                reverse = m.group(1) == "bottom"
    if reverse and second is not None:
        first, second = second, first

    return first, second


def _parse_float(
    css: str,
    selector: str,
    prop: Union[str, Sequence[str]],
    source: str,
    *,
    log_miss: bool = True,
    log_selector: bool = True,
) -> Optional[float]:
    props = (prop,) if isinstance(prop, str) else tuple(prop)
    val = None
    for i, p in enumerate(props):
        val = _css_prop(
            css,
            selector,
            p,
            source,
            log_selector=i == 0 and log_selector,
            log_property=False,
        )
        if val is not None:
            break
    if val is None:
        if log_miss:
            log.info(
                "Theme MISS [%s] selector '%s' property '%s'",
                source,
                selector,
                props[0],
            )
        return None
    m = re.search(r"(-?[0-9.]+)", val)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    if log_miss:
        log.info(
            "Theme MISS [%s] selector '%s' property '%s' invalid number '%s'",
            source,
            selector,
            props[0],
            val,
        )
    return None


def _parse_pixmap(
    css: str,
    selector: str,
    prop: str,
    source: str,
    *,
    log_miss: bool = True,
) -> Optional[QPixmap]:
    val = _css_prop(css, selector, prop, source)
    if not val:
        if log_miss:
            log.info("Theme MISS [%s] selector '%s' property '%s'", source, selector, prop)
        return None
    m = re.search(r"url\(([^)]+)\)", val)
    if m:
        path = m.group(1).strip('"\'')
        if path.startswith(":"):
            img = QPixmap(path)
            if not img.isNull():
                return img
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
            img = QPixmap(path)
            if not img.isNull():
                if selector == ".playhead-top" and prop == "background-image":
                    log.info(
                        "Theme [%s] %s %s loaded '%s'",
                        source,
                        selector,
                        prop,
                        path,
                    )
                return img
    if log_miss:
        log.info(
            "Theme MISS [%s] selector '%s' property '%s' invalid pixmap '%s'",
            source,
            selector,
            prop,
            val,
        )
    return None


def _parse_box_shadow(
    css: str, selector: str, source: str, *, log_miss: bool = True
):
    """Return (color, blur) from a box-shadow property."""
    val = _css_prop(css, selector, "box-shadow", source)
    if not val:
        if log_miss:
            log.info(
                "Theme MISS [%s] selector '%s' property '%s'",
                source,
                selector,
                "box-shadow",
            )
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
    if col is None and blur is None and log_miss:
        log.info(
            "Theme MISS [%s] selector '%s' property '%s' invalid value '%s'",
            source,
            selector,
            "box-shadow",
            val,
        )
    return col, blur


def _theme_pixmap(
    qt_theme, selector: str, prop: str, *, log_miss: bool = True
) -> Optional[QPixmap]:
    if not qt_theme or not hasattr(qt_theme, "style_sheet"):
        return None
    val = _css_prop(qt_theme.style_sheet, selector, prop, "theme", log_selector=log_miss, log_property=log_miss)
    if not val:
        return None
    m = re.search(r"url\(([^)]+)\)", val)
    if m:
        path = m.group(1).strip('"\'')
        if path.startswith(":"):
            img = QPixmap(path)
            if not img.isNull():
                return img
        module_path = os.path.dirname(__import__(qt_theme.__module__).__file__)
        if not os.path.isabs(path):
            candidate = os.path.normpath(os.path.join(module_path, path))
            if not os.path.exists(candidate):
                candidate = os.path.normpath(os.path.join(os.path.dirname(module_path), path))
            path = candidate
        if os.path.exists(path):
            img = QPixmap(path)
            if not img.isNull():
                if selector == ".playhead-top" and prop == "background-image":
                    log.info(
                        "Theme [theme] %s %s loaded '%s'",
                        selector,
                        prop,
                        path,
                    )
                return img
    if log_miss:
        log.info("Theme MISS [theme] %s %s invalid pixmap '%s'", selector, prop, val)
    return None


def _theme_get_color(
    qt_theme,
    selector: str,
    prop: Union[str, Sequence[str]],
    *,
    log_miss: bool = True,
):
    if not qt_theme:
        return None
    props = (prop,) if isinstance(prop, str) else tuple(prop)
    if hasattr(qt_theme, "get_color"):
        for p in props:
            col = qt_theme.get_color(selector, p)
            if col:
                return col
    if hasattr(qt_theme, "style_sheet"):
        return _parse_color(
            qt_theme.style_sheet,
            selector,
            props,
            "theme",
            log_miss=log_miss,
            log_selector=log_miss,
        )
    return None


def _theme_get_int(
    qt_theme,
    selector: str,
    prop: Union[str, Sequence[str]],
    *,
    log_miss: bool = True,
):
    if not qt_theme:
        return None
    props = (prop,) if isinstance(prop, str) else tuple(prop)
    if hasattr(qt_theme, "get_int"):
        for p in props:
            val = qt_theme.get_int(selector, p)
            if val is not None:
                return val
    if hasattr(qt_theme, "style_sheet"):
        val = _parse_float(
            qt_theme.style_sheet,
            selector,
            props,
            "theme",
            log_miss=log_miss,
            log_selector=log_miss,
        )
        if val is not None:
            return int(val)
    return None


def _theme_apply_background(theme: TimelineTheme, qt_theme, css_sheet: str) -> None:
    col1, col2 = _parse_gradient(css_sheet, "body", "background", "theme", log_miss=False)
    if col1:
        theme.background = col1
    if col2:
        theme.background2 = col2
    elif col1:
        theme.background2 = QColor()
    if col1 is None and col2 is None:
        col = _theme_get_color(qt_theme, "body", ("background", "background-color"))
        if col:
            theme.background = col
            theme.background2 = QColor()


def _theme_apply_clip(theme: TimelineTheme, qt_theme, css_sheet: str) -> None:
    col1, col2 = _parse_gradient(css_sheet, ".clip", "background", "theme", log_miss=False)
    if col1:
        theme.clip.background = col1
    if col2:
        theme.clip.background2 = col2
    elif col1:
        theme.clip.background2 = QColor()
    if col1 is None and col2 is None:
        col = _theme_get_color(qt_theme, ".clip", ("background", "background-color"))
        if col:
            theme.clip.background = col
            theme.clip.background2 = QColor()
    col = _theme_get_color(qt_theme, ".clip", ("border-top", "border"))
    if col:
        theme.clip.border_color = col
    val = _theme_get_int(qt_theme, ".clip", ("border-top", "border"))
    if val is not None:
        theme.clip.border_width = float(val)
    val = _theme_get_int(qt_theme, ".clip", "border-radius")
    if val is not None:
        theme.clip.border_radius = val
    val = _theme_get_int(qt_theme, ".clip", "font-size")
    if val is not None:
        theme.clip.font_size = val
    col = _theme_get_color(qt_theme, ".clip_label", "color")
    if col:
        theme.clip.font_color = col
    val = _theme_get_int(qt_theme, ".clip", "height")
    if val is not None:
        theme.clip.height = val
    val = _css_prop(css_sheet, ".clip", "box-shadow", "theme", log_selector=False, log_property=False)
    if val:
        col, blur = _parse_box_shadow(css_sheet, ".clip", "theme", log_miss=False)
        if col:
            theme.clip.shadow_color = col
        if blur is not None:
            theme.clip.shadow_blur = blur
    val = _theme_get_int(qt_theme, ".thumb", "width")
    if val is not None:
        theme.clip.thumb_width = val
    val = _theme_get_int(qt_theme, ".thumb", "height")
    if val is not None:
        theme.clip.thumb_height = val


def _theme_apply_selection(theme: TimelineTheme, qt_theme, css_sheet: str) -> None:
    col = _theme_get_color(qt_theme, ".ui-selected", ("border-top", "border"))
    if col:
        theme.clip_selected = col
    op = None
    if css_sheet:
        op = _parse_float(
            css_sheet,
            ".ui-selectable-helper",
            "opacity",
            "theme",
            log_miss=False,
            log_selector=False,
        )
    col = _theme_get_color(qt_theme, ".ui-selectable-helper", ("background", "background-color"))
    if col:
        if op is not None and col.alpha() == 255:
            col.setAlpha(int(255 * op))
        theme.selection = col
    col = _theme_get_color(qt_theme, ".ui-selectable-helper", ("border", "border-color"))
    if col:
        if op is not None and col.alpha() == 255:
            col.setAlpha(int(255 * op))
        theme.selection_border = col
    val = _theme_get_int(qt_theme, ".ui-selectable-helper", ("border", "border-width"))
    if val is not None:
        theme.selection_border_width = float(val)


def _theme_apply_transition(theme: TimelineTheme, qt_theme, css_sheet: str) -> None:
    col1, col2 = _parse_gradient(css_sheet, ".transition", "background", "theme", log_miss=False)
    if col1:
        theme.transition.background = col1
    if col2:
        theme.transition.background2 = col2
    elif col1:
        theme.transition.background2 = QColor()
    if col1 is None and col2 is None:
        col = _theme_get_color(qt_theme, ".transition", ("background", "background-color"))
        if col:
            theme.transition.background = col
            theme.transition.background2 = QColor()
    col = _theme_get_color(qt_theme, ".transition", ("border-top", "border"))
    if col:
        theme.transition.border_color = col
    val = _theme_get_int(qt_theme, ".transition", "border-radius")
    if val is not None:
        theme.transition.border_radius = val
    img = _theme_pixmap(qt_theme, ".transition", "background-image")
    if img:
        theme.transition.background_image = img
    col = _theme_get_color(qt_theme, ".transition_label", "color")
    if col:
        theme.transition.font_color = col
    val = _theme_get_int(qt_theme, ".transition", "font-size")
    if val is not None:
        theme.transition.font_size = val
    val = _theme_get_int(qt_theme, ".transition", "height")
    if val is not None:
        theme.transition.height = val


def _theme_apply_track(theme: TimelineTheme, qt_theme, css_sheet: str) -> None:
    col1, col2 = _parse_gradient(css_sheet, ".track", "background", "theme", log_miss=False)
    if col1:
        theme.track.background = col1
    if col2:
        theme.track.background2 = col2
    elif col1:
        theme.track.background2 = QColor()
    if col1 is None and col2 is None:
        col = _theme_get_color(qt_theme, ".track", ("background", "background-color"))
        if col:
            theme.track.background = col
            theme.track.background2 = QColor()
    col = _theme_get_color(qt_theme, ".track", ("border-top", "border"))
    if col:
        theme.track.border_color = col
    val = _theme_get_int(qt_theme, ".track", "border-radius")
    if val is not None:
        theme.track.border_radius = val
    col = _theme_get_color(qt_theme, ".track_name", "color")
    if not col:
        col = _theme_get_color(qt_theme, ".track_label", "color")
    if col:
        theme.track.font_color = col
    val = _theme_get_int(qt_theme, ".track", "height")
    if val is not None:
        theme.track.height = val
    val = _theme_get_int(qt_theme, ".track_name", "font-size")
    if val is not None:
        theme.track.font_size = val
    col = _theme_get_color(qt_theme, ".track_name", ("background", "background-color"))
    if col:
        theme.track.name_background = col
    val = _theme_get_int(qt_theme, ".track_name", "width")
    if val is not None:
        theme.track.name_width = val
    val = _theme_get_int(qt_theme, ".track", "margin-bottom")
    if val is not None:
        theme.track.gap = val
    col = _theme_get_color(qt_theme, ".track_name", "border-left")
    if col:
        theme.track.name_border_color = col
    val = _theme_get_int(qt_theme, ".track_name", "border-left")
    if val is not None:
        theme.track.name_border_width = val
    col = _theme_get_color(qt_theme, ".track_name", ("border-top", "border"))
    if col:
        theme.track.name_border_top_color = col
    val = _theme_get_int(qt_theme, ".track_name", ("border-top", "border"))
    if val is not None:
        theme.track.name_border_top_width = val
    col = _theme_get_color(qt_theme, ".track_name", ("border-bottom", "border"))
    if col:
        theme.track.name_border_bottom_color = col
    val = _theme_get_int(qt_theme, ".track_name", ("border-bottom", "border"))
    if val is not None:
        theme.track.name_border_bottom_width = val
    val = _theme_get_int(qt_theme, ".track_name", "border-top-left-radius")
    if val is not None:
        theme.track.name_radius_tl = val
    val = _theme_get_int(qt_theme, ".track_name", "border-bottom-left-radius")
    if val is not None:
        theme.track.name_radius_bl = val
    val = _theme_get_int(qt_theme, ".track_name", "border-radius")
    if val is not None:
        if not theme.track.name_radius_tl:
            theme.track.name_radius_tl = val
        if not theme.track.name_radius_bl:
            theme.track.name_radius_bl = val


def _theme_apply_ruler(theme: TimelineTheme, qt_theme, css_sheet: str) -> None:
    col1, col2 = _parse_gradient(css_sheet, "#scrolling_ruler", "background", "theme", log_miss=False)
    if not col1 and not col2:
        col1, col2 = _parse_gradient(css_sheet, "#ruler", "background", "theme", log_miss=False)
    if col1:
        theme.ruler.background = col1
    if col2:
        theme.ruler.background2 = col2
    elif col1:
        theme.ruler.background2 = QColor()
    if col1 is None and col2 is None:
        col = _theme_get_color(
            qt_theme,
            "#scrolling_ruler",
            ("background", "background-color"),
            log_miss=False,
        )
        if not col:
            col = _theme_get_color(
                qt_theme,
                "#ruler",
                ("background", "background-color"),
                log_miss=False,
            )
        if col:
            theme.ruler.background = col
            theme.ruler.background2 = QColor()
        else:
            log.info("Theme MISS [theme] selector '#scrolling_ruler' property 'background'")
    col1, col2 = _parse_gradient(
        css_sheet,
        "#ruler_label",
        "background",
        "theme",
        log_miss=False,
    )
    if col1:
        theme.ruler_name_background = col1
    if col2:
        theme.ruler_name_background2 = col2
    elif col1:
        theme.ruler_name_background2 = QColor()
    if col1 is None and col2 is None:
        col = _theme_get_color(qt_theme, "#ruler_label", "background")
        if col:
            theme.ruler_name_background = col
            theme.ruler_name_background2 = QColor()
    col = _theme_get_color(qt_theme, ".tick_mark", "background-color")
    if col:
        theme.ruler.border_color = col
    col = _theme_get_color(qt_theme, "#ruler_time", "color")
    if col:
        theme.ruler.font_color = col
    val = _theme_get_int(qt_theme, "#ruler_time", "font-size")
    if val is not None:
        theme.ruler_time_font_size = val
    fs = _parse_float(
        css_sheet,
        ".ruler_time",
        "font-size",
        "theme",
        log_miss=False,
    )
    if fs is not None:
        base = theme.ruler_time_font_size or 12
        theme.ruler.font_size = int(fs * base) if fs < 5 else int(fs)
    val = _theme_get_int(qt_theme, ".ruler_time", "top")
    if val is not None:
        theme.ruler_label_top = val
    val = _theme_get_int(qt_theme, "#ruler", "height")
    if val is not None:
        theme.ruler.height = val
    val = _theme_get_int(qt_theme, "#ruler_time", "padding-left")
    if val is not None:
        theme.ruler_time_pad_left = val
    val = _theme_get_int(qt_theme, "#ruler_time", "padding-top")
    if val is not None:
        theme.ruler_time_pad_top = val


def _theme_apply_playhead(theme: TimelineTheme, qt_theme) -> None:
    col = _theme_get_color(qt_theme, ".playhead-line", "background-color")
    if col:
        theme.playhead_color = col
    val = _theme_get_int(qt_theme, ".playhead-line", "width")
    if val is not None:
        theme.playhead_width = float(val)
    img = _theme_pixmap(qt_theme, ".playhead-top", "background-image")
    if img:
        theme.playhead_icon = img
    val = _theme_get_int(qt_theme, ".playhead-top", "width")
    if val is not None:
        theme.playhead_icon_width = val
    val = _theme_get_int(qt_theme, ".playhead-top", "height")
    if val is not None:
        theme.playhead_icon_height = val
    val = _theme_get_int(qt_theme, ".playhead-top", "margin-left")
    if val is not None:
        theme.playhead_icon_offset_x = val
    val = _theme_get_int(qt_theme, ".playhead-top", "margin-top")
    if val is not None:
        theme.playhead_icon_offset_y = val


def _theme_apply_menu(theme: TimelineTheme, qt_theme) -> None:
    img = _theme_pixmap(qt_theme, ".menu", "background-image")
    if img:
        theme.menu_icon = img
    val = _theme_get_int(qt_theme, ".menu", "width")
    if val is not None:
        theme.menu_size = val
    val = _theme_get_int(qt_theme, ".menu", "margin")
    if val is not None:
        theme.menu_margin = val


def _apply_theme_obj(theme: TimelineTheme, qt_theme) -> TimelineTheme:
    """Update *theme* from a Qt theme instance using BaseTheme helpers."""

    if not qt_theme:
        return theme

    css_sheet = getattr(qt_theme, "style_sheet", "")
    _theme_apply_background(theme, qt_theme, css_sheet)
    _theme_apply_clip(theme, qt_theme, css_sheet)
    _theme_apply_selection(theme, qt_theme, css_sheet)
    _theme_apply_transition(theme, qt_theme, css_sheet)
    _theme_apply_track(theme, qt_theme, css_sheet)
    _theme_apply_ruler(theme, qt_theme, css_sheet)
    _theme_apply_playhead(theme, qt_theme)
    _theme_apply_menu(theme, qt_theme)

    return theme


def _css_apply_background(theme: TimelineTheme, css: str, source: str, log_miss: bool) -> None:
    col1, col2 = _parse_gradient(css, "body", "background", source, log_miss=False)
    if col1:
        theme.background = col1
    if col2:
        theme.background2 = col2
    elif col1:
        theme.background2 = QColor()
    if col1 is None and col2 is None:
        col = _parse_color(css, "body", ("background", "background-color"), source, log_miss=log_miss)
        if col:
            theme.background = col
            theme.background2 = QColor()


def _css_apply_clip(theme: TimelineTheme, css: str, source: str, log_miss: bool) -> None:
    col1, col2 = _parse_gradient(css, ".clip", "background", source, log_miss=False)
    if col1:
        theme.clip.background = col1
    if col2:
        theme.clip.background2 = col2
    elif col1:
        theme.clip.background2 = QColor()
    if col1 is None and col2 is None:
        col = _parse_color(css, ".clip", ("background", "background-color"), source, log_miss=log_miss)
        if col:
            theme.clip.background = col
            theme.clip.background2 = QColor()
    col = _parse_color(css, ".clip", ("border-top", "border"), source, log_miss=False)
    if col:
        theme.clip.border_color = col
    val = _parse_float(css, ".clip", ("border-top", "border"), source, log_miss=False)
    if val is not None:
        theme.clip.border_width = float(val)
    val = _parse_float(css, ".clip", "border-radius", source, log_miss=False)
    if val is not None:
        theme.clip.border_radius = int(val)
    col = _parse_color(css, ".clip_label", "color", source, log_miss=log_miss)
    if col:
        theme.clip.font_color = col
    val = _parse_float(css, ".clip", "height", source, log_miss=log_miss)
    if val is not None:
        theme.clip.height = int(val)
    col2, blur = _parse_box_shadow(css, ".clip", source, log_miss=log_miss)
    if col2:
        theme.clip.shadow_color = col2
    if blur is not None:
        theme.clip.shadow_blur = blur
    val = _parse_float(css, ".thumb", "width", source, log_miss=log_miss)
    if val is not None:
        theme.clip.thumb_width = int(val)
    val = _parse_float(css, ".thumb", "height", source, log_miss=log_miss)
    if val is not None:
        theme.clip.thumb_height = int(val)
    val = _parse_float(css, ".clip", "font-size", source, log_miss=log_miss)
    if val is not None:
        theme.clip.font_size = int(val)


def _css_apply_selection(theme: TimelineTheme, css: str, source: str, log_miss: bool) -> None:
    col = _parse_color(css, ".ui-selected", ("border-top", "border"), source, log_miss=log_miss)
    if col:
        theme.clip_selected = col
    op = _parse_float(css, ".ui-selectable-helper", "opacity", source, log_miss=log_miss)
    col = _parse_color(css, ".ui-selectable-helper", ("background", "background-color"), source, log_miss=log_miss)
    if col:
        if op is not None and col.alpha() == 255:
            col.setAlpha(int(255 * op))
        theme.selection = col
    col = _parse_color(css, ".ui-selectable-helper", ("border", "border-color"), source, log_miss=log_miss)
    if col:
        if op is not None and col.alpha() == 255:
            col.setAlpha(int(255 * op))
        theme.selection_border = col
    val = _parse_float(css, ".ui-selectable-helper", ("border", "border-width"), source, log_miss=log_miss)
    if val is not None:
        theme.selection_border_width = float(val)


def _css_apply_transition(theme: TimelineTheme, css: str, source: str, log_miss: bool) -> None:
    col1, col2 = _parse_gradient(css, ".transition", "background", source, log_miss=False)
    if col1:
        theme.transition.background = col1
    if col2:
        theme.transition.background2 = col2
    elif col1:
        theme.transition.background2 = QColor()
    if col1 is None and col2 is None:
        col = _parse_color(css, ".transition", ("background", "background-color"), source, log_miss=log_miss)
        if col:
            theme.transition.background = col
            theme.transition.background2 = QColor()
    col = _parse_color(css, ".transition", ("border-top", "border"), source, log_miss=False)
    if col:
        theme.transition.border_color = col
    val = _parse_float(css, ".transition", "border-radius", source, log_miss=False)
    if val is not None:
        theme.transition.border_radius = int(val)
    img = _parse_pixmap(css, ".transition", "background-image", source, log_miss=log_miss)
    if img:
        theme.transition.background_image = img
    col = _parse_color(css, ".transition_label", "color", source, log_miss=log_miss)
    if col:
        theme.transition.font_color = col
    val = _parse_float(css, ".transition", "font-size", source, log_miss=log_miss)
    if val is not None:
        theme.transition.font_size = int(val)
    val = _parse_float(css, ".transition", "height", source, log_miss=log_miss)
    if val is not None:
        theme.transition.height = int(val)


def _css_apply_track(theme: TimelineTheme, css: str, source: str, log_miss: bool) -> None:
    col1, col2 = _parse_gradient(css, ".track", "background", source, log_miss=False)
    if col1:
        theme.track.background = col1
    if col2:
        theme.track.background2 = col2
    elif col1:
        theme.track.background2 = QColor()
    if col1 is None and col2 is None:
        col = _parse_color(css, ".track", ("background", "background-color"), source, log_miss=log_miss)
        if col:
            theme.track.background = col
            theme.track.background2 = QColor()
    col = _parse_color(css, ".track", ("border-top", "border"), source, log_miss=False)
    if col:
        theme.track.border_color = col
    val = _parse_float(css, ".track", "border-radius", source, log_miss=False)
    if val is not None:
        theme.track.border_radius = int(val)
    col = _parse_color(css, ".track_name", "color", source, log_miss=log_miss)
    if not col:
        col = _parse_color(css, ".track_label", "color", source, log_miss=log_miss)
    if col:
        theme.track.font_color = col
    val = _parse_float(css, ".track", "height", source, log_miss=log_miss)
    if val is not None:
        theme.track.height = int(val)
    col = _parse_color(css, ".track_name", ("background", "background-color"), source, log_miss=log_miss)
    if col:
        theme.track.name_background = col
    val = _parse_float(css, ".track_name", "width", source, log_miss=log_miss)
    if val is not None:
        theme.track.name_width = int(val)
    val = _parse_float(css, ".track", "margin-bottom", source, log_miss=log_miss)
    if val is not None:
        theme.track.gap = int(val)
    col = _parse_color(css, ".track_name", "border-left", source, log_miss=log_miss)
    if col:
        theme.track.name_border_color = col
    val = _parse_float(css, ".track_name", "border-left", source, log_miss=log_miss)
    if val is not None:
        theme.track.name_border_width = int(val)
    col = _parse_color(css, ".track_name", ("border-top", "border"), source, log_miss=log_miss)
    if col:
        theme.track.name_border_top_color = col
    val = _parse_float(css, ".track_name", ("border-top", "border"), source, log_miss=log_miss)
    if val is not None:
        theme.track.name_border_top_width = int(val)
    col = _parse_color(css, ".track_name", ("border-bottom", "border"), source, log_miss=log_miss)
    if col:
        theme.track.name_border_bottom_color = col
    val = _parse_float(css, ".track_name", ("border-bottom", "border"), source, log_miss=log_miss)
    if val is not None:
        theme.track.name_border_bottom_width = int(val)
    val = _parse_float(css, ".track_name", ("border-top-left-radius", "border-radius"), source, log_miss=log_miss)
    if val is not None:
        theme.track.name_radius_tl = int(val)
    val = _parse_float(css, ".track_name", ("border-bottom-left-radius", "border-radius"), source, log_miss=log_miss)
    if val is not None:
        theme.track.name_radius_bl = int(val)
    val = _parse_float(css, ".track_name", "font-size", source, log_miss=log_miss)
    if val is not None:
        theme.track.font_size = int(val)


def _css_apply_ruler(theme: TimelineTheme, css: str, source: str, log_miss: bool) -> None:
    col1, col2 = _parse_gradient(css, "#scrolling_ruler", "background", source, log_miss=False)
    if not col1 and not col2:
        col1, col2 = _parse_gradient(css, "#ruler", "background", source, log_miss=False)
    if col1:
        theme.ruler.background = col1
    if col2:
        theme.ruler.background2 = col2
    elif col1:
        theme.ruler.background2 = QColor()
    if col1 is None and col2 is None:
        col = _parse_color(css, "#scrolling_ruler", ("background", "background-color"), source, log_miss=False)
        if not col:
            col = _parse_color(css, "#ruler", ("background", "background-color"), source, log_miss=False)
        if col:
            theme.ruler.background = col
            theme.ruler.background2 = QColor()
        else:
            log.info("Theme MISS [%s] selector '#scrolling_ruler' property 'background'", source)
    col1, col2 = _parse_gradient(css, "#ruler_label", "background", source, log_miss=log_miss)
    if col1:
        theme.ruler_name_background = col1
    if col2:
        theme.ruler_name_background2 = col2
    elif col1:
        theme.ruler_name_background2 = QColor()
    if col1 is None and col2 is None:
        col = _parse_color(css, "#ruler_label", ("background", "background-color"), source, log_miss=log_miss)
        if col:
            theme.ruler_name_background = col
            theme.ruler_name_background2 = QColor()
    col = _parse_color(css, ".tick_mark", "background-color", source, log_miss=log_miss)
    if col:
        theme.ruler.border_color = col
    col = _parse_color(css, "#ruler_time", "color", source, log_miss=log_miss)
    if col:
        theme.ruler.font_color = col
    val = _parse_float(css, "#ruler_time", "font-size", source, log_miss=log_miss)
    if val is not None:
        theme.ruler_time_font_size = int(val)
    fs = _parse_float(css, ".ruler_time", "font-size", source, log_miss=log_miss)
    if fs is not None:
        base = theme.ruler_time_font_size or 12
        theme.ruler.font_size = int(fs * base) if fs < 5 else int(fs)
    val = _parse_float(css, ".ruler_time", "top", source, log_miss=log_miss)
    if val is not None:
        theme.ruler_label_top = int(val)
    val = _parse_float(css, "#ruler", "height", source, log_miss=log_miss)
    if val is not None:
        theme.ruler.height = int(val)
    val = _parse_float(css, "#ruler_time", "padding-left", source, log_miss=log_miss)
    if val is not None:
        theme.ruler_time_pad_left = int(val)
    val = _parse_float(css, "#ruler_time", "padding-top", source, log_miss=log_miss)
    if val is not None:
        theme.ruler_time_pad_top = int(val)


def _css_apply_playhead(theme: TimelineTheme, css: str, source: str, log_miss: bool) -> None:
    col = _parse_color(css, ".playhead-line", "background-color", source, log_miss=log_miss)
    if col:
        theme.playhead_color = col
    val = _parse_float(css, ".playhead-line", "width", source, log_miss=log_miss)
    if val is not None:
        theme.playhead_width = val
    img = _parse_pixmap(css, ".playhead-top", "background-image", source, log_miss=log_miss)
    if img:
        theme.playhead_icon = img
    val = _parse_float(css, ".playhead-top", "width", source, log_miss=log_miss)
    if val is not None:
        theme.playhead_icon_width = int(val)
    val = _parse_float(css, ".playhead-top", "height", source, log_miss=log_miss)
    if val is not None:
        theme.playhead_icon_height = int(val)
    val = _parse_float(css, ".playhead-top", "margin-left", source, log_miss=log_miss)
    if val is not None:
        theme.playhead_icon_offset_x = int(val)
    val = _parse_float(css, ".playhead-top", "margin-top", source, log_miss=log_miss)
    if val is not None:
        theme.playhead_icon_offset_y = int(val)


def _css_apply_menu(theme: TimelineTheme, css: str, source: str, log_miss: bool) -> None:
    img = _parse_pixmap(css, ".menu", "background-image", source, log_miss=log_miss)
    if img:
        theme.menu_icon = img
    val = _parse_float(css, ".menu", "width", source, log_miss=log_miss)
    if val is not None:
        theme.menu_size = int(val)
    margin = _css_prop(css, ".menu", "margin", source, log_selector=log_miss, log_property=log_miss)
    if margin:
        match = re.search(r"(-?[0-9.]+)", margin)
        if match:
            theme.menu_margin = int(float(match.group(1)))


def _css_apply_scrollbars(theme: TimelineTheme, css: str, source: str) -> None:
    col = _parse_color(css, "::-webkit-scrollbar-thumb", "background-color", source, log_miss=False)
    if col:
        theme.scrollbar_handle = col
    col = _parse_color(
        css,
        "::-webkit-scrollbar-track",
        ("background", "background-color"),
        source,
        log_miss=False,
    )
    if col:
        theme.scrollbar_track = col
    val = _parse_float(css, "::-webkit-scrollbar", ("width", "height"), source, log_miss=False)
    if val is not None:
        theme.scrollbar_width = int(val)


def _apply_css(theme: TimelineTheme, css: str, source: str = "css") -> TimelineTheme:
    """Update *theme* with values parsed from *css*."""

    if not css:
        return theme

    log_miss = True

    _css_apply_background(theme, css, source, log_miss)
    _css_apply_clip(theme, css, source, log_miss)
    _css_apply_selection(theme, css, source, log_miss)
    _css_apply_transition(theme, css, source, log_miss)
    _css_apply_track(theme, css, source, log_miss)
    _css_apply_ruler(theme, css, source, log_miss)
    _css_apply_playhead(theme, css, source, log_miss)
    _css_apply_menu(theme, css, source, log_miss)
    _css_apply_scrollbars(theme, css, source)

    return theme

def apply_theme(widget, css: str = "") -> bool:
    """Load theme values for *widget* and return True if geometry changed."""

    from classes.app import get_app

    app_theme = get_app().theme_manager.get_current_theme() if get_app() else None

    t = TimelineTheme()

    # Start with defaults from the main CSS file
    t = _apply_css(t, MAIN_CSS, source="main.css")

    # Override with values from the active Qt theme instance
    if app_theme:
        t = _apply_theme_obj(t, app_theme)

    # Optional additional CSS overrides
    if isinstance(css, str) and css.strip():
        t = _apply_css(t, css, source="override")

    if not t.playhead_icon:
        base = os.path.dirname(_CSS_PATH)
        default_path = os.path.normpath(os.path.join(base, "../images/playhead.svg"))
        if os.path.exists(default_path):
            t.playhead_icon = QPixmap(default_path)
            log.info(
                "Theme [default] .playhead-top background-image loaded '%s'",
                default_path,
            )

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
    if t.scrollbar_width:
        widget.scroll_bar_thickness = t.scrollbar_width

    return (
        old_track_h != widget.track_height
        or old_name_w != widget.track_name_width
        or old_ruler_h != widget.ruler_height
        or old_gap != widget.track_gap
    )
