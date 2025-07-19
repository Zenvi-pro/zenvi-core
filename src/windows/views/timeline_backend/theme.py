"""Utilities for parsing and applying timeline CSS themes."""

import re
from typing import Optional
from PyQt5.QtGui import QColor

# Default color palette for the timeline
DEFAULT_THEME = {
    "background": QColor("#191919"),
    "clip_border": QColor("#53a0ed"),
    "clip_bg": QColor("#192332"),
    "clip_selected": QColor("red"),
    "clip_text": QColor("#FFFFFF"),
    "text": QColor("#FFFFFF"),
    "track_bg": QColor("#283241"),
    "track_name_bg": QColor("#192332"),
    "ruler_bg": QColor("#141923"),
    "ruler_tick": QColor("#FABE0A"),
    "ruler_text": QColor("#c8c8c8"),
    "playhead": QColor("#ff0024"),
    "track_border": QColor("#4b92ad"),
    "selection": QColor(0, 120, 215, 80),
}


def _css_prop(css: str, selector: str, prop: str) -> Optional[str]:
    pattern = rf"{re.escape(selector)}\s*\{{[^}}]*{prop}\s*:\s*([^;]+);"
    m = re.search(pattern, css, re.MULTILINE)
    return m.group(1).strip() if m else None


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
    if val and val.endswith("px"):
        try:
            return float(val[:-2])
        except ValueError:
            pass
    return None


def apply_theme(widget, css: str) -> bool:
    """Apply *css* theme values to *widget*.

    Returns True if geometry-affecting values changed."""
    if not css:
        return False

    geom_changed = False

    col = _parse_color(css, "body", "background")
    if col:
        widget.theme["background"] = col

    col = _parse_color(css, ".clip", "border")
    if col:
        widget.theme["clip_border"] = col
    col = _parse_color(css, ".clip", "background")
    if col:
        widget.theme["clip_bg"] = col

    col = _parse_color(css, ".track_label", "color")
    if col:
        widget.theme["text"] = col
    col = _parse_color(css, ".clip_label", "color")
    if col:
        widget.theme["clip_text"] = col

    col = _parse_color(css, ".track", "background")
    if col:
        widget.theme["track_bg"] = col
    col = _parse_color(css, ".track_name", "background")
    if col:
        widget.theme["track_name_bg"] = col

    col = _parse_color(css, ".tick_mark", "background-color")
    if col:
        widget.theme["ruler_tick"] = col
    col = _parse_color(css, ".ruler_time", "color")
    if col:
        widget.theme["ruler_text"] = col
    col = _parse_color(css, ".playhead-line", "background-color")
    if col:
        widget.theme["playhead"] = col
    col = _parse_color(css, ".track", "border-top")
    if col:
        widget.theme["track_border"] = col

    val = _parse_float(css, ".track", "height")
    if val and val != widget.track_height:
        widget.track_height = val
        geom_changed = True
    val = _parse_float(css, ".clip", "height")
    if val and val != widget.track_height:
        widget.track_height = val
        geom_changed = True
    val = _parse_float(css, ".track_name", "width")
    if val and val != widget.track_name_width:
        widget.track_name_width = val
        geom_changed = True
    val = _parse_float(css, "#ruler_label", "height")
    if val and val != widget.ruler_height:
        widget.ruler_height = val
        geom_changed = True

    return geom_changed
