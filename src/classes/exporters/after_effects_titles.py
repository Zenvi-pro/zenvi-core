"""Zenvi SVG titles as native After Effects layers, when that is exact enough.

A Zenvi title is an SVG file (a Title Editor template, ``classes.title_svg``).
libopenshot rasterizes it with QSvgRenderer; the export does the same for
any title it cannot rebuild faithfully (``mode = "png"``). A title whose
visible content is only solid-colour text and rectangles -- the "simple"
templates: Box, Footer 1-3, Gray Box 1-4, Header 1-3, Solid Color,
Standard 1, 3 and 4 -- becomes editable layers instead: one point-text layer
per text line and one shape layer per rectangle, positioned in the title's
own pixel space (the export puts them in a precomp of the artboard size, so
the clip's transform applies to the whole title exactly as in Zenvi).

Anything else keeps the PNG: gradients or patterns (``url(#...)`` paints),
filters, masks, clip paths, paths and other shapes, images, rotated, skewed
or mirrored transforms, inline-styled text runs, ``dx``/``dy`` offsets, and
semi-transparent outlines wider than a hairline (AE text strokes have no
opacity of their own). ``parse_title_svg`` says which and why.

Pure Python (minidom): no Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from xml.dom import minidom

from classes import title_svg

SKIP_TAGS = frozenset({"defs", "metadata", "title", "desc", "sodipodi:namedview", "style", "script"})
CONTAINER_TAGS = frozenset({"svg", "g", "a", "switch"})
NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "lime": "#00ff00", "green": "#008000",
    "blue": "#0000ff", "yellow": "#ffff00", "cyan": "#00ffff", "aqua": "#00ffff", "magenta": "#ff00ff",
    "fuchsia": "#ff00ff", "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0", "maroon": "#800000",
    "olive": "#808000", "navy": "#000080", "purple": "#800080", "teal": "#008080", "orange": "#ffa500",
}
# An outline this thin and faint is dropped (with a note) rather than forcing a PNG.
FAINT_STROKE_WIDTH = 2.0
FAINT_STROKE_OPACITY = 0.5


class NotNative(Exception):
    """The title needs features native AE layers would not reproduce; the message says which."""


@dataclass(frozen=True)
class RectItem:
    x: float
    y: float
    width: float
    height: float
    rx: float
    fill: Optional[Tuple[float, float, float]]
    fill_opacity: float
    stroke: Optional[Tuple[float, float, float]]
    stroke_width: float
    stroke_opacity: float
    opacity: float
    name: str = ""

    kind = "rect"


@dataclass(frozen=True)
class TextItem:
    text: str
    x: float
    y: float                     # baseline
    size: float                  # px
    family: str
    bold: bool
    italic: bool
    anchor: str                  # start | middle | end
    fill: Tuple[float, float, float]
    opacity: float               # opacity * fill-opacity
    stroke: Optional[Tuple[float, float, float]]
    stroke_width: float
    tracking: float              # AE tracking (1/1000 em)
    name: str = ""

    kind = "text"


@dataclass
class TitleLayout:
    width: float
    height: float
    items: List[object] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def text_count(self) -> int:
        return sum(1 for i in self.items if isinstance(i, TextItem))

    @property
    def rect_count(self) -> int:
        return sum(1 for i in self.items if isinstance(i, RectItem))


def parse_color(value: Optional[str]) -> Optional[Tuple[float, float, float]]:
    """'#rgb', '#rrggbb', 'rgb(r, g, b)' or a basic name -> (r, g, b) in 0..1; None for 'none'."""
    text = (value or "").strip().lower()
    if not text or text in ("none", "transparent"):
        return None
    if text.startswith("url("):
        raise NotNative("gradient or pattern paint")
    text = NAMED_COLORS.get(text, text)
    if text.startswith("#"):
        h = text[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            try:
                return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
            except ValueError:
                pass
    m = re.match(r"rgb\(\s*([\d.]+)%?\s*,\s*([\d.]+)%?\s*,\s*([\d.]+)%?\s*\)", text)
    if m:
        scale = 100.0 if "%" in text else 255.0
        return tuple(max(0.0, min(1.0, float(g) / scale)) for g in m.groups())  # type: ignore[return-value]
    raise NotNative(f"unsupported colour {value!r}")


def _num(value: Optional[str], default: float = 0.0) -> float:
    px = title_svg._px(value) if value not in (None, "") else None
    if px is not None:
        return px
    try:
        return float(str(value).strip()) if value not in (None, "") else default
    except ValueError:
        return default


def _first_number(value: str) -> Optional[float]:
    parts = [p for p in re.split(r"[\s,]+", (value or "").strip()) if p]
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


@dataclass(frozen=True)
class _Affine:
    """Uniform scale + translation (the only transforms native layers reproduce exactly)."""

    s: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    def then(self, other: "_Affine") -> "_Affine":
        """Apply *other* inside this one (a child transform)."""
        return _Affine(self.s * other.s, self.tx + self.s * other.tx, self.ty + self.s * other.ty)

    def point(self, x: float, y: float) -> Tuple[float, float]:
        return self.tx + self.s * x, self.ty + self.s * y


def _parse_transform(text: str) -> _Affine:
    out = _Affine()
    for name, args in re.findall(r"([a-zA-Z]+)\s*\(([^)]*)\)", text or ""):
        nums = [float(a) for a in re.split(r"[\s,]+", args.strip()) if a]
        name = name.lower()
        if name == "translate":
            out = out.then(_Affine(1.0, nums[0] if nums else 0.0, nums[1] if len(nums) > 1 else 0.0))
        elif name == "scale":
            sx = nums[0] if nums else 1.0
            sy = nums[1] if len(nums) > 1 else sx
            if sx <= 0 or abs(sx - sy) > 1e-9:
                raise NotNative("mirrored or non-uniform scale")
            out = out.then(_Affine(sx))
        elif name == "matrix" and len(nums) == 6:
            a, b, c, d, e, f = nums
            if abs(b) > 1e-9 or abs(c) > 1e-9 or a <= 0 or abs(a - d) > 1e-9:
                raise NotNative("rotated, skewed or mirrored transform")
            out = out.then(_Affine(a, e, f))
        else:
            raise NotNative(f"{name} transform")
    return out


def _style(node) -> dict:
    return title_svg.node_style(node)


def _prop(node, name: str, inherit: bool = True, default: Optional[str] = None) -> Optional[str]:
    return title_svg.node_property(node, name, default, inherit=inherit)


def _opacity(node) -> float:
    try:
        return max(0.0, min(1.0, float(_prop(node, "opacity", inherit=False, default="1") or 1)))
    except ValueError:
        return 1.0


def _float_prop(node, name: str, default: float) -> float:
    try:
        return float(_prop(node, name, default=str(default)) or default)
    except ValueError:
        return default


_EFFECTS = {"filter": "an SVG filter (glow, shadow or blur)", "mask": "an SVG mask", "clip-path": "an SVG clip path"}


def _check_effects(node) -> None:
    style = _style(node)
    for key, reason in _EFFECTS.items():
        value = style.get(key) or node.getAttribute(key)
        if value and value.strip().lower() != "none":
            raise NotNative(reason)


def _visible(node) -> bool:
    style = _style(node)
    if (style.get("display") or node.getAttribute("display") or "").strip() == "none":
        return False
    vis = (style.get("visibility") or node.getAttribute("visibility") or "").strip()
    return vis not in ("hidden", "collapse")


def _text_content(node) -> str:
    return "".join(c.data for c in node.childNodes if c.nodeType == c.TEXT_NODE)


def _line_nodes(text_node) -> list:
    tspans = [t for t in text_node.getElementsByTagName("tspan")]
    if not tspans:
        return [text_node]
    lines = []
    for t in tspans:
        if t.getElementsByTagName("tspan"):
            raise NotNative("nested text runs")
        lines.append(t)
    return lines


def _text_items(text_node, transform: _Affine, opacity: float, layout: TitleLayout) -> List[TextItem]:
    items = []
    lines = _line_nodes(text_node)
    if len(lines) > 1 or lines[0] is not text_node:
        stray = _text_content(text_node).strip()
        if stray:
            raise NotNative("text outside its lines")
    for i, node in enumerate(lines):
        if node is not text_node and not _visible(node):
            continue
        _check_effects(node)
        if node is not text_node and node.getAttribute("transform"):
            raise NotNative("transformed text run")
        raw = _text_content(node)
        preserve = (_prop(node, "xml:space") == "preserve" or text_node.getAttribute("xml:space") == "preserve")
        content = raw if preserve else " ".join(raw.split())
        if not content.strip():
            continue  # a blanked template line (title_svg.BLANK)
        for attr in ("dx", "dy", "rotate", "textLength"):
            if node.getAttribute(attr) or text_node.getAttribute(attr):
                raise NotNative(f"{attr} on text")
        x_attr = node.getAttribute("x") or text_node.getAttribute("x")
        y_attr = node.getAttribute("y") or text_node.getAttribute("y")
        if node is not text_node and len(lines) > 1 and not node.getAttribute("y"):
            raise NotNative("text runs flowing on one line")
        x = _first_number(x_attr) or 0.0
        y = _first_number(y_attr) or 0.0
        px, py = transform.point(x, y)
        size = title_svg._px(_prop(node, "font-size")) or 16.0
        family = str(_prop(node, "font-family") or "").strip().strip("'\"").split(",")[0].strip().strip("'\"")
        weight = str(_prop(node, "font-weight") or "normal").strip().lower()
        bold = weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600)
        italic = str(_prop(node, "font-style") or "normal").strip().lower() in ("italic", "oblique")
        anchor = str(_prop(node, "text-anchor") or "start").strip().lower()
        if anchor not in ("start", "middle", "end"):
            anchor = "start"
        fill_value = _prop(node, "fill", default="#000000")
        fill = parse_color(fill_value)
        fill_opacity = _float_prop(node, "fill-opacity", 1.0)
        node_opacity = opacity
        current = node
        while current is not None and getattr(current, "tagName", "") in ("tspan", "text"):
            node_opacity *= _opacity(current)
            current = current.parentNode
        stroke = parse_color(_prop(node, "stroke", default="none"))
        stroke_width = _num(_prop(node, "stroke-width", default="1"), 1.0) * transform.s
        stroke_opacity = _float_prop(node, "stroke-opacity", 1.0)
        if stroke is not None:
            if stroke_opacity < 0.999:
                if stroke_width <= FAINT_STROKE_WIDTH and stroke_opacity <= FAINT_STROKE_OPACITY:
                    layout.notes.append(f"faint outline on {content[:24]!r} omitted")
                    stroke = None
                else:
                    raise NotNative("semi-transparent text outline")
            elif stroke_width <= 0:
                stroke = None
        if fill is None:
            if stroke is None:
                continue
            raise NotNative("outline-only text")
        if fill_opacity < 0.999 and stroke is not None:
            raise NotNative("translucent fill with an outline")
        spacing = _prop(node, "letter-spacing")
        tracking = 0.0
        if spacing not in (None, "", "normal"):
            sp = title_svg._px(spacing)
            if sp is None:
                raise NotNative(f"letter-spacing {spacing!r}")
            tracking = sp / size * 1000.0 if size else 0.0
        items.append(TextItem(text=content, x=px, y=py, size=size * transform.s, family=family, bold=bold,
                              italic=italic, anchor=anchor, fill=fill,
                              opacity=max(0.0, min(1.0, node_opacity * fill_opacity)),
                              stroke=stroke, stroke_width=stroke_width, tracking=tracking,
                              name=content.strip()[:40] or f"Line {i + 1}"))
    return items


def _rect_item(node, transform: _Affine, opacity: float) -> Optional[RectItem]:
    _check_effects(node)
    local = _parse_transform(node.getAttribute("transform")) if node.getAttribute("transform") else _Affine()
    t = transform.then(local)
    w = _num(node.getAttribute("width"))
    h = _num(node.getAttribute("height"))
    if w <= 0 or h <= 0:
        return None
    x, y = t.point(_num(node.getAttribute("x")), _num(node.getAttribute("y")))
    rx_attr, ry_attr = node.getAttribute("rx"), node.getAttribute("ry")
    rx = _num(rx_attr) if rx_attr else (_num(ry_attr) if ry_attr else 0.0)
    ry = _num(ry_attr) if ry_attr else rx
    radius = min((rx + ry) / 2.0, w / 2.0, h / 2.0) * t.s
    node_opacity = opacity * _opacity(node)
    fill = parse_color(_prop(node, "fill", inherit=True, default="#000000"))
    fill_opacity = _float_prop(node, "fill-opacity", 1.0)
    stroke = parse_color(_prop(node, "stroke", default="none"))
    stroke_width = _num(_prop(node, "stroke-width", default="1"), 1.0) * t.s
    stroke_opacity = _float_prop(node, "stroke-opacity", 1.0)
    if stroke is not None and stroke_width <= 0:
        stroke = None
    visible = node_opacity > 1e-6 and ((fill is not None and fill_opacity > 1e-6)
                                       or (stroke is not None and stroke_opacity > 1e-6))
    if not visible:
        return None
    return RectItem(x=x, y=y, width=w * t.s, height=h * t.s, rx=radius, fill=fill,
                    fill_opacity=max(0.0, min(1.0, fill_opacity)), stroke=stroke, stroke_width=stroke_width,
                    stroke_opacity=max(0.0, min(1.0, stroke_opacity)), opacity=max(0.0, min(1.0, node_opacity)),
                    name=node.getAttribute("id") or "Rectangle")


def parse_title_svg(svg_text: str) -> TitleLayout:
    """The native layout of a title SVG, or NotNative(reason) when it needs the PNG route."""
    try:
        doc = minidom.parseString(svg_text)
    except Exception as exc:  # expat errors are not one exception type
        raise NotNative(f"unreadable SVG ({exc})") from None
    svg = doc.documentElement
    if svg is None:
        raise NotNative("empty SVG")
    width, height = title_svg.artboard_size(doc)
    root = _Affine()
    box = [p for p in re.split(r"[\s,]+", svg.getAttribute("viewBox").strip()) if p]
    if len(box) == 4:
        try:
            vx, vy, vw, vh = (float(v) for v in box)
        except ValueError:
            raise NotNative("unreadable viewBox") from None
        if vw <= 0 or vh <= 0:
            raise NotNative("empty viewBox")
        sx, sy = width / vw, height / vh
        if abs(sx - sy) > 1e-6 * max(sx, sy):
            raise NotNative("viewBox with another aspect ratio")
        root = _Affine(sx, -vx * sx, -vy * sy)
    layout = TitleLayout(width=width, height=height)

    def walk(node, transform: _Affine, opacity: float) -> None:
        for child in node.childNodes:
            if child.nodeType != child.ELEMENT_NODE:
                continue
            tag = child.tagName
            local = tag.split(":")[-1]
            if tag in SKIP_TAGS or local in SKIP_TAGS:
                continue
            if not _visible(child):
                continue
            if local in CONTAINER_TAGS:
                _check_effects(child)
                t = transform.then(_parse_transform(child.getAttribute("transform"))) \
                    if child.getAttribute("transform") else transform
                walk(child, t, opacity * _opacity(child))
            elif local == "rect":
                rect = _rect_item(child, transform, opacity)
                if rect is not None:
                    layout.items.append(rect)
            elif local == "text":
                _check_effects(child)
                t = transform.then(_parse_transform(child.getAttribute("transform"))) \
                    if child.getAttribute("transform") else transform
                layout.items.extend(_text_items(child, t, opacity, layout))
            else:
                raise NotNative(f"<{local}> element")

    walk(svg, root, _opacity(svg))
    if not layout.items:
        raise NotNative("nothing visible")
    return layout


def describe(layout: TitleLayout) -> str:
    parts = []
    if layout.text_count:
        parts.append(f"{layout.text_count} text layer{'s' if layout.text_count != 1 else ''}")
    if layout.rect_count:
        parts.append(f"{layout.rect_count} shape layer{'s' if layout.rect_count != 1 else ''}")
    return ", ".join(parts) or "empty"


__all__ = ["NotNative", "RectItem", "TextItem", "TitleLayout", "parse_title_svg", "parse_color", "describe"]
