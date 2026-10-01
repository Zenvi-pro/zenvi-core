"""SVG titles without the Title Editor dialog (no Qt, no libopenshot).

The Title Editor (``windows/title_editor.py``), the title template list
(``windows/models/titles_model.py``) and the agent tools
(``classes/editor_tools/titles_text.py``) share these rules:

* which nodes are the editable text lines (``<tspan>`` elements holding text,
  in document order -- the dialog's "Line N" boxes),
* how a font, a text colour and a background colour are written into a
  template (text/tspan ``style`` and the first ``<rect>``),
* how a new title file is named ("Name (2).svg") and written (staged to a
  temporary file, then renamed into place).

The agent tools also group lines that carry the same text (a template's
reflection or shadow copy of its title) into one *field*, so "the title" is one
value to fill, and give each field a role (title / subtitle / line).
"""

from __future__ import annotations

import fnmatch
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple
from xml.dom import minidom

from classes import info

# A blanked line keeps one text node, so the Title Editor still lists it and the
# placeholder ("Sub-Title", "Line 2") never reaches the video.
BLANK = " "

# Fonts the Title Editor falls back to when a template font is not installed.
FALLBACK_FONTS = ('DejaVu Sans', 'Liberation Sans', 'Noto Sans', 'FreeSans',
                  'Ubuntu', 'Cantarell', 'Open Sans', 'Sans-serif', 'Arial')


# ---------------------------------------------------------------------------
# style="" helpers (tolerant of stray ';' and missing ':')
# ---------------------------------------------------------------------------

def parse_style(style: Optional[str]) -> dict:
    out = {}
    for part in (style or "").split(";"):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def format_style(styledict: dict) -> str:
    return ";".join("%s:%s" % (k, v) for k, v in styledict.items()) + ";"


def node_style(node) -> dict:
    return parse_style(node.getAttribute("style"))


def set_node_style(node, styledict: dict) -> str:
    style = format_style(styledict)
    node.setAttribute("style", style)
    return style


def node_property(node, name: str, default=None, inherit=True):
    """A presentation property from style="", then the attribute, then (optionally) the parent text."""
    current = node
    while current is not None and getattr(current, "nodeType", None) == current.ELEMENT_NODE:
        value = node_style(current).get(name)
        if value in (None, "") and current.hasAttribute(name):
            value = current.getAttribute(name)
        if value not in (None, ""):
            return value
        if not inherit:
            break
        current = current.parentNode
    return default


def _px(value) -> Optional[float]:
    """'131.25px' / '120' / '12pt' -> pixels (pt converted at 96 dpi)."""
    if value in (None, ""):
        return None
    s = str(value).strip().lower()
    m = re.match(r"^(-?[0-9]*\.?[0-9]+)\s*(px|pt)?$", s)
    if not m:
        return None
    number = float(m.group(1))
    return number * 96.0 / 72.0 if m.group(2) == "pt" else number


# ---------------------------------------------------------------------------
# Template catalog (TitlesModel's list, without Qt)
# ---------------------------------------------------------------------------

def bundled_titles_dir() -> str:
    return os.path.join(info.PATH, "titles")


def template_paths() -> List[str]:
    """Title templates shown in Title > Title: bundled ``src/titles`` + the user's templates."""
    titles_dir = bundled_titles_dir()
    paths = [os.path.join(titles_dir, name) for name in sorted(os.listdir(titles_dir))]
    user_dir = getattr(info, "USER_TITLES_PATH", "")
    if user_dir and os.path.isdir(user_dir):
        paths.extend(
            os.path.join(user_dir, name)
            for name in sorted(os.listdir(user_dir))
            if fnmatch.fnmatch(name, "*.svg")
        )
    out = []
    for path in sorted(paths):
        filename = os.path.basename(path)
        if filename.startswith(".") or "thumbs.db" in filename.lower() or filename.lower() == "temp.svg":
            continue
        out.append(path)
    return out


def template_display_name(path: str, tr=None) -> str:
    """'Gray_Box_4.svg' -> 'Gray box 4' (the list's label; *tr* translates it)."""
    tr = tr or (lambda s: s)
    base = os.path.splitext(os.path.basename(path))[0]
    suffix_number = None
    parts = base.split("_")
    if parts[-1].isdigit():
        suffix_number = parts[-1]
    title_name = base.replace("_", " ").capitalize()
    if suffix_number:
        title_name = title_name.replace(suffix_number, "%s")
        return tr(title_name) % suffix_number
    return tr(title_name)


def is_bundled(path: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), bundled_titles_dir()]) == bundled_titles_dir()
    except ValueError:
        return False


def find_template(name: str) -> Optional[str]:
    """A template path by name ('Gray_Box_4', 'gray box 4', 'Gray_Box_4.svg') or an existing .svg path."""
    raw = str(name or "").strip()
    if not raw:
        return None
    if os.path.isabs(os.path.expanduser(raw)):
        path = os.path.expanduser(raw)
        return path if os.path.isfile(path) and path.lower().endswith(".svg") else None

    def norm(s):
        return re.sub(r"[\s_\-]+", "_", os.path.splitext(s)[0].strip().lower())

    wanted = norm(raw)
    for path in template_paths():
        base = os.path.basename(path)
        if norm(base) == wanted or norm(template_display_name(path)) == wanted:
            return path
    return None


def suggest_templates(name: str, limit: int = 6) -> List[str]:
    """Template names that look like *name* (for 'did you mean' refusals)."""
    import difflib
    names = [os.path.splitext(os.path.basename(p))[0] for p in template_paths()]
    raw = str(name or "").strip()
    close = difflib.get_close_matches(raw, names, n=limit, cutoff=0.4)
    if not close:
        low = raw.lower().split("_")[0]
        close = [n for n in names if low and low in n.lower()][:limit]
    return close


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def load(path: str):
    return minidom.parse(path)


def parse_string(text: str):
    return minidom.parseString(text)


def text_nodes(xmldoc) -> list:
    return list(xmldoc.getElementsByTagName("text"))


def tspan_nodes(xmldoc) -> list:
    return list(xmldoc.getElementsByTagName("tspan"))


def rect_nodes(xmldoc) -> list:
    return list(xmldoc.getElementsByTagName("rect"))


def _first_text_child(node):
    for child in node.childNodes:
        if child.nodeType == child.TEXT_NODE:
            return child
        break
    return None


def line_nodes(xmldoc) -> list:
    """The Title Editor's "Line N" nodes: tspans whose first child is text, in document order."""
    return [n for n in tspan_nodes(xmldoc) if _first_text_child(n) is not None]


def _line_text(node) -> str:
    child = _first_text_child(node)
    return child.data if child is not None else ""


def line_texts(xmldoc) -> List[str]:
    return [_line_text(n) for n in line_nodes(xmldoc)]


def set_line_texts(xmldoc, texts: Iterable[str], nodes=None) -> None:
    """Replace the text of line i with texts[i] (the dialog's txtLine_changed)."""
    nodes = list(nodes) if nodes is not None else line_nodes(xmldoc)
    for node, text in zip(nodes, texts):
        _replace_text(xmldoc, node, text)


def _replace_text(xmldoc, node, text: str) -> None:
    old = _first_text_child(node)
    new = xmldoc.createTextNode(str(text))
    if old is not None:
        node.replaceChild(new, old)
    else:
        node.insertBefore(new, node.firstChild)


def artboard_size(xmldoc) -> Tuple[float, float]:
    svg = xmldoc.documentElement
    width = _px(svg.getAttribute("width"))
    height = _px(svg.getAttribute("height"))
    if not (width and height):
        box = [p for p in re.split(r"[\s,]+", svg.getAttribute("viewBox").strip()) if p]
        if len(box) == 4:
            try:
                width, height = float(box[2]), float(box[3])
            except ValueError:
                width = height = None
    return float(width or 1920.0), float(height or 1080.0)


def to_xml(xmldoc) -> str:
    return xmldoc.toxml()


def write(xmldoc, path: str) -> str:
    """Write *xmldoc* to *path*: staged in the same folder, then renamed into place."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".title-", suffix=".svg.partial", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:  # binary, like the dialog (Windows line endings)
            fh.write(bytes(to_xml(xmldoc), "UTF-8"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------------------
# Font / colours (the dialog's Change Font / Text Color / Background Color)
# ---------------------------------------------------------------------------

def apply_font(nodes, family: Optional[str] = None, italic: Optional[bool] = None,
               bold: Optional[bool] = None, font_size_ratio: float = 1.0) -> str:
    """Set family, style and weight on every node; scale font-size by *font_size_ratio*.

    ``None`` keeps a node's current value. Font size is read from style="" or
    the font-size attribute (the dialog only understood style="...px").
    Returns the last style string written (the dialog keeps it).
    """
    last = ""
    for node in nodes:
        style = node_style(node)
        if family:
            style["font-family"] = "'%s'" % str(family).strip("'\"")
        if italic is not None:
            style["font-style"] = "italic" if italic else "normal"
        if bold is not None:
            style["font-weight"] = "bold" if bold else "normal"
        if font_size_ratio and abs(float(font_size_ratio) - 1.0) > 1e-9:
            size = _px(style.get("font-size"))
            if size is None and node.hasAttribute("font-size"):
                size = _px(node.getAttribute("font-size"))
            if size is not None:
                style["font-size"] = "%spx" % _fmt(size * float(font_size_ratio))
        last = set_node_style(node, style)
    return last


def _fmt(value: float) -> str:
    return ("%.4f" % float(value)).rstrip("0").rstrip(".")


def _opacity_str(alpha: float) -> str:
    return _fmt(max(0.0, min(1.0, float(alpha))))


def set_text_color(text_nodes_, tspan_nodes_, color: str, alpha: float = 1.0) -> None:
    """Fill every text line with *color* at *alpha* opacity.

    Opacity is written once per line (on the tspan, or on a <text> that holds
    its text directly): SVG multiplies nested opacities, so writing it on both
    the <text> and its <tspan> (as the dialog used to) showed 50% as 25%.
    """
    opacity = _opacity_str(alpha)
    tspans = list(tspan_nodes_)
    for node in list(text_nodes_) + tspans:
        style = node_style(node)
        style["fill"] = color
        is_text_with_tspans = node.tagName == "text" and node.getElementsByTagName("tspan").length > 0
        style["opacity"] = "1" if is_text_with_tspans else opacity
        set_node_style(node, style)


def set_background(rects, color: str, alpha: float = 1.0) -> str:
    """Colour the first <rect> (every template's full-frame background) at *alpha*."""
    rects = list(rects)
    if not rects:
        return ""
    style = node_style(rects[0])
    style.update({"fill": color, "opacity": _opacity_str(alpha)})
    return set_node_style(rects[0], style)


def ref_color(xmldoc, ref_id: str) -> str:
    """First stop colour of a gradient referenced as url(#ref_id) (following xlink:href)."""
    defs = xmldoc.getElementsByTagName("defs")
    if not defs:
        return ""
    seen = set()
    while ref_id and ref_id not in seen:
        seen.add(ref_id)
        target = None
        for node in defs[0].childNodes:
            if getattr(node, "attributes", None) and node.getAttribute("id") == ref_id:
                target = node
                break
        if target is None:
            return ""
        href = target.getAttribute("xlink:href") or target.getAttribute("href")
        if href:
            ref_id = href.lstrip("#")
            continue
        for stop in target.childNodes:
            if getattr(stop, "nodeName", "") == "stop":
                color = parse_style(stop.getAttribute("style")).get("stop-color") or stop.getAttribute("stop-color")
                if color:
                    return color
        return ""
    return ""


def resolve_paint(xmldoc, value: Optional[str], default: str) -> str:
    value = (value or "").strip()
    if value.startswith("url(#"):
        return ref_color(xmldoc, value[5:].rstrip(")")) or default
    if not value or value == "none":
        return default
    return value


def text_color(xmldoc, node=None) -> Tuple[str, float]:
    """(fill, opacity) of a text line (default: the last line, like the dialog's colour button)."""
    nodes = line_nodes(xmldoc)
    node = node if node is not None else (nodes[-1] if nodes else None)
    if node is None:
        return "#ffffff", 1.0
    fill = resolve_paint(xmldoc, node_property(node, "fill"), "#ffffff")
    opacity = 1.0
    current = node
    while current is not None and getattr(current, "tagName", "") in ("tspan", "text"):
        try:
            opacity *= float(node_property(current, "opacity", 1.0, inherit=False))
        except (TypeError, ValueError):
            pass
        current = current.parentNode
    return fill, opacity


def background_color(xmldoc) -> Tuple[str, float]:
    rects = rect_nodes(xmldoc)
    if not rects:
        return "", 0.0
    fill = resolve_paint(xmldoc, node_property(rects[0], "fill", inherit=False), "#000000")
    try:
        opacity = float(node_property(rects[0], "opacity", 1.0, inherit=False))
    except (TypeError, ValueError):
        opacity = 1.0
    if (node_property(rects[0], "fill", inherit=False) or "") == "none":
        opacity = 0.0
    return fill, opacity


def editor_font_family(requested: str, families) -> str:
    """The installed family the Title Editor uses for *requested* ('' = Qt's default font).

    The requested family (exact, else the first installed family containing its name,
    as TitleEditor.get_font always did), else the first FALLBACK_FONTS entry installed
    under exactly that name, and only then one merely containing it -- so on macOS
    "Arial" wins over "Noto Sans Armenian" for the DejaVu Sans templates.
    """
    families = list(families or [])
    if requested:
        if requested in families:
            return requested
        for font in families:
            if requested in font:
                return font
    for fallback in FALLBACK_FONTS:
        if fallback in families:
            return fallback
    for fallback in FALLBACK_FONTS:
        for font in families:
            if fallback in font:
                return font
    return ""


def installed_font_for_title(xmldoc, families) -> Tuple[str, str]:
    """(template font, installed replacement) when the template's font is missing, else ('', '').

    Opening a title in the Title Editor rewrites a missing template font (e.g. DejaVu
    Sans on macOS) to an installed one; the tools do the same so titles look alike and
    Qt does not spend seconds searching font aliases on the GUI thread.
    """
    if families is None:
        return "", ""
    current = font_family(xmldoc)
    if not current or any(current == f or current in f for f in families):
        return "", ""
    replacement = editor_font_family(current, families)
    return (current, replacement) if replacement else ("", "")


def font_family(xmldoc) -> str:
    for node in line_nodes(xmldoc):
        family = node_property(node, "font-family")
        if family:
            return str(family).strip("'\"")
    return ""


# ---------------------------------------------------------------------------
# Fields: the distinct text slots of a title (agent view of the lines)
# ---------------------------------------------------------------------------

@dataclass
class TextField:
    index: int                       # 1-based, document order of the slot's first line
    text: str
    nodes: list = field(default_factory=list)
    font_size: Optional[float] = None
    role: str = "line"               # title | subtitle | line
    x: Optional[float] = None
    y: Optional[float] = None
    anchor: str = "start"

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


def _float_attr(node, name):
    current = node
    while current is not None and getattr(current, "tagName", "") in ("tspan", "text"):
        raw = current.getAttribute(name)
        if raw:
            try:
                return float(re.split(r"[\s,]+", raw.strip())[0])
            except ValueError:
                return None
        current = current.parentNode
    return None


def _is_mirrored(node) -> bool:
    current = node
    while current is not None and getattr(current, "tagName", "") in ("tspan", "text"):
        if "scale(1,-1)" in current.getAttribute("transform").replace(" ", ""):
            return True
        current = current.parentNode
    return False


def fields(xmldoc) -> List[TextField]:
    """Distinct text slots; lines repeating a slot's text (reflections, shadows) join it."""
    slots: List[TextField] = []
    by_text = {}
    for node in line_nodes(xmldoc):
        text = _line_text(node)
        key = text.strip()
        slot = by_text.get(key) if key else None
        if slot is None:
            slot = TextField(index=len(slots) + 1, text=text)
            slots.append(slot)
            if key:
                by_text[key] = slot
        slot.nodes.append(node)
    for slot in slots:
        primary = next((n for n in slot.nodes if not _is_mirrored(n)), slot.nodes[0])
        slot.font_size = _px(node_property(primary, "font-size"))
        slot.x = _float_attr(primary, "x")
        slot.y = _float_attr(primary, "y")
        slot.anchor = str(node_property(primary, "text-anchor", "start") or "start")
    _assign_roles(slots)
    return slots


def _assign_roles(slots: List[TextField]) -> None:
    if not slots:
        return
    ranked = sorted(slots, key=lambda s: (-(s.font_size or 0.0), s.index))
    main = next((s for s in ranked if "sub" not in s.text.lower()), ranked[0])
    main.role = "title"
    rest = [s for s in ranked if s is not main]
    sub = next((s for s in rest if "sub" in s.text.lower()), None)
    if sub is None and rest:
        sub = rest[0]
    if sub is not None:
        sub.role = "subtitle"


def field_by_role(slots: List[TextField], role: str) -> Optional[TextField]:
    return next((s for s in slots if s.role == role), None)


def set_field_text(xmldoc, slot: TextField, text: str) -> None:
    value = " ".join(str(text or "").split("\n")).strip() or BLANK
    for node in slot.nodes:
        _replace_text(xmldoc, node, value)
    slot.text = value


def text_zone(slots: List[TextField], artboard_h: float) -> str:
    """Where a title's main text sits on its artboard: top / center / bottom."""
    main = field_by_role(slots, "title") or (slots[0] if slots else None)
    if main is None or main.y is None or not artboard_h:
        return "center"
    y = abs(main.y) / float(artboard_h)
    if y < 0.3:
        return "top"
    if y > 0.7:
        return "bottom"
    return "center"


# ---------------------------------------------------------------------------
# Fitting text to the artboard (templates do not wrap)
# ---------------------------------------------------------------------------

def estimated_text_width(text: str, font_px: float, bold: bool = True) -> float:
    """Rough rendered width of *text* in a sans font (no font metrics without Qt)."""
    wide = sum(1 for ch in text if ch.isupper() or ch in "MWmw@%")
    narrow = sum(1 for ch in text if ch in " .,:;'!|il1")
    normal = max(0, len(text) - wide - narrow)
    per = 0.62 if bold else 0.56
    return font_px * (normal * per + wide * (per + 0.14) + narrow * 0.3)


def available_width(slot: TextField, artboard_w: float) -> float:
    x = slot.x if slot.x is not None else artboard_w / 2.0
    x = max(0.0, min(artboard_w, x))
    if slot.anchor == "middle":
        return 2.0 * min(x, artboard_w - x) * 0.94
    if slot.anchor == "end":
        return x * 0.96
    return (artboard_w - x) * 0.96


def fit_field(slot: TextField, artboard_w: float, min_ratio: float = 0.35) -> float:
    """Shrink a slot's font when its text would run off the artboard; returns the ratio applied."""
    if slot.is_blank or not slot.font_size:
        return 1.0
    bold = any(str(node_property(n, "font-weight", "normal")).lower() in ("bold", "700", "800", "900")
               for n in slot.nodes)
    width = estimated_text_width(slot.text.strip(), slot.font_size, bold)
    room = available_width(slot, artboard_w)
    if width <= room or width <= 0:
        return 1.0
    ratio = max(min_ratio, room / width)
    apply_font(slot.nodes, font_size_ratio=ratio)
    slot.font_size = slot.font_size * ratio
    return ratio


# ---------------------------------------------------------------------------
# Names ("Name (2).svg"), the dialog's rule
# ---------------------------------------------------------------------------

_NUMBERED = re.compile(r"^(.+?)(\s*)(\(([0-9]*)\))?\.svg$", re.IGNORECASE)
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_base_name(text: str, fallback: str = "Title", limit: int = 60) -> str:
    """A file-system safe title name from free text ("Tokyo: Day 1" -> "Tokyo Day 1")."""
    base = _BAD_CHARS.sub(" ", str(text or ""))
    base = " ".join(base.split()).strip(" .")
    if base.lower().endswith(".svg"):
        base = base[:-4].strip(" .")
    if len(base) > limit:
        base = base[:limit].rstrip(" .")
    return base or fallback


def duplicate_pattern(file_name: str) -> Tuple[str, int]:
    """'Title.svg' -> ('Title (%d)', 0); 'Title (3).svg' -> ('Title (%d)', 3) (Duplicate naming)."""
    name = os.path.basename(file_name)
    if not name.lower().endswith(".svg"):
        name += ".svg"
    match = _NUMBERED.match(name)
    if not match:
        return os.path.splitext(name)[0].replace("%", "%%") + " (%d)", 0
    base = match.group(1).replace("%", "%%")
    if match.group(4):
        return base + match.group(2) + "(%d)", int(match.group(4))
    return base + " (%d)", 0


def free_name(pattern: str, offset: int, title_dir: str, taken=(), limit: int = 1000) -> Optional[str]:
    """First 'pattern % (offset + i)' whose .svg does not exist in *title_dir* (i = 1..limit)."""
    taken_l = {os.path.normcase(os.path.abspath(p)) for p in (taken or ())}
    for i in range(1, limit):
        name = pattern % (offset + i)
        path = os.path.join(title_dir, "%s.svg" % name)
        if not os.path.exists(path) and os.path.normcase(os.path.abspath(path)) not in taken_l:
            return name
    return None


def unique_title_path(title_dir: str, base: str, taken=()) -> str:
    """'<dir>/<base>.svg' when free, else the Duplicate rule: '<base> (1).svg', '(2)', ..."""
    base = safe_base_name(base)
    path = os.path.join(title_dir, base + ".svg")
    taken_l = {os.path.normcase(os.path.abspath(p)) for p in (taken or ())}
    if not os.path.exists(path) and os.path.normcase(os.path.abspath(path)) not in taken_l:
        return path
    pattern, offset = duplicate_pattern(base + ".svg")
    name = free_name(pattern, offset, title_dir, taken)
    if not name:
        raise FileExistsError("no free title name for %r in %s" % (base, title_dir))
    return os.path.join(title_dir, name + ".svg")
