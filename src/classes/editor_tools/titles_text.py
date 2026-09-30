"""Text on screen: title templates, animated (Blender) titles, captions, timer overlays, emoji.

Workstream: titles-text. Tools register themselves with ``@editor_tool`` from
``classes.editor_tools._registry``; helpers live in ``classes.editor_tools._base``
and ``titles_text_common``. The title rules (which SVG nodes are the lines, how
font and colours are written, naming) are shared with the Title Editor dialog
through ``classes.title_svg``.

This module holds the SVG title tools; the sibling modules register the rest:
``titles_text_captions`` (Caption effect), ``titles_text_overlays`` (Timer,
emoji) and ``titles_text_animated`` (Blender).
"""

from __future__ import annotations

import os
import re
from typing import Dict, List

from classes import title_svg
from classes.editor_tools._base import (
    ToolError, array, boolean, enum, is_locked, number, obj, ok, project_fps, selected_clip_ids,
    snap_seconds, string, ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.titles_text_common import (
    GRAVITY, CommitTimeout, color_hex_alpha, commit_on_main, create_track, fade_alpha, import_media_file,
    installed_font_families, place_clip, plan_overlay_track, precheck_on_main, refresh_file_and_clips,
    resolve_font, title_dir, track_info, OVERLAY_ROLE,
)

# #183's add_title_tool tried these in order when no template was named.
DEFAULT_TEMPLATES = ("Bar_1", "Standard_1", "Footer_1")

# What each bundled template looks like (from rendering all 50), for the model.
TEMPLATE_PURPOSE = {
    "Bar_1": "centre title on a glossy blue bar, with a reflection",
    "Bar_2": "centre title + subtitle on a glossy blue bar",
    "Bar_3": "lower third: gold title on a dark bar across the bottom",
    "Box": "full-screen card: purple background, cyan frame, title + subtitle",
    "Bubbles_1": "title + subtitle under floating bubbles",
    "Bubbles_2": "title + subtitle with bubbles around the frame",
    "Camera_Border": "camera viewfinder corners around a small title",
    "Cloud_1": "title + subtitle inside a cartoon cloud",
    "Cloud_2": "three lines in three cartoon clouds",
    "Creative_Commons_1": "music credit card (song, author, CC licence) on black",
    "Creative_Commons_2": "music credit card with a URL line, on black",
    "Film_Rating_1": "green 'approved for all audiences' trailer card",
    "Film_Rating_2": "green trailer rating card: R",
    "Film_Rating_3": "green trailer rating card: G",
    "Film_Rating_4": "green trailer rating card: PG-13",
    "Flames": "title + subtitle over flames along the bottom",
    "Flare": "full-screen card: colourful light flare, big title + subtitle",
    "Footer_1": "small footer text, bottom left",
    "Footer_2": "small footer text, bottom centre",
    "Footer_3": "small footer text, bottom right",
    "Gold_1": "gold title, centre",
    "Gold_2": "gold title + subtitle, centre",
    "Gold_Bottom": "gold title, bottom centre",
    "Gold_Top": "gold title, top centre",
    "Gray_Box_1": "title in a grey box, top left",
    "Gray_Box_2": "title + subtitle in a grey box, top left",
    "Gray_Box_3": "lower third: title in a grey box, bottom right",
    "Gray_Box_4": "lower third: name + role in a grey box, bottom right",
    "Header_1": "small header text, top left",
    "Header_2": "small header text, top centre",
    "Header_3": "small header text, top right",
    "OpenShot": "full-screen card: pink-to-blue gradient, white title + subtitle (end card)",
    "Oval_1": "title + subtitle inside a blue oval vignette",
    "Oval_2": "title + subtitle inside a blue oval frame",
    "Oval_3": "title at the top and subtitle at the bottom of a blue oval",
    "Oval_4": "title with reflection inside a blue oval",
    "Post_it": "yellow sticky note with four lines",
    "Ribbon_1": "subtitle + title on a blue ribbon across the top",
    "Ribbon_2": "title + subtitle on a blue ribbon across the top",
    "Ribbon_3": "title ribbon at the top, subtitle ribbon at the bottom",
    "Smoke_1": "title + subtitle between dark smoke at top and bottom",
    "Smoke_2": "title + subtitle with dark smoke",
    "Smoke_3": "title with reflection between dark smoke",
    "Solid_Color": "solid full-screen colour card with no text (set background_color)",
    "Standard_1": "plain title + subtitle, centre",
    "Standard_2": "plain title with a reflection, centre",
    "Standard_3": "three centred lines",
    "Standard_4": "four centred lines (credits)",
    "Sunset": "glowing gradient title, centre",
    "TV_Rating": "small TV rating badge (TV / G), top left",
}

WEIGHTS = {"": None, "bold": True, "normal": False}
STYLES = {"": None, "italic": True, "normal": False}
ZONE_POSITION = {"top": "top_center", "center": "center", "bottom": "bottom_center"}


# ---------------------------------------------------------------------------
# Title documents
# ---------------------------------------------------------------------------

def _template_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _resolve_template(template: str) -> str:
    raw = str(template or "").strip()
    if not raw:
        for name in DEFAULT_TEMPLATES:
            path = title_svg.find_template(name)
            if path:
                return path
        paths = [p for p in title_svg.template_paths() if p.lower().endswith(".svg")]
        if paths:
            return paths[0]
        raise ToolError("no title templates are installed")
    path = title_svg.find_template(raw)
    if not path:
        hint = ", ".join(title_svg.suggest_templates(raw)) or ", ".join(DEFAULT_TEMPLATES)
        raise ToolError(f"unknown title template {raw!r}; close matches: {hint} "
                        "(list_title_templates_tool lists them all)")
    return path


def _load_doc(path: str):
    try:
        return title_svg.load(path)
    except Exception as exc:
        raise ToolError(f"cannot read title SVG {os.path.basename(path)}: {exc}") from None


def _field_rows(slots) -> list:
    return [{"index": s.index, "role": s.role, "text": s.text.strip()} for s in slots]


def _assign_texts(slots, text: str, subtitle: str, lines: List[str], name: str,
                  blank_rest: bool) -> Dict[int, str]:
    """Map text / subtitle / lines onto the title's fields (index -> new text)."""
    values: Dict[int, str] = {}
    by_index = {s.index: s for s in slots}
    count = len(slots)
    if lines:
        if len(lines) > count:
            raise ToolError(
                f"{name} has {count} text field(s) {[s.role for s in slots]} but {len(lines)} lines were "
                "given; pick a template with more lines (Standard_3 has 3, Standard_4 and Post_it have 4)")
        for i, value in enumerate(lines):
            values[slots[i].index] = str(value)
    else:
        parts = [p.strip() for p in str(text or "").split("\n")]
        while parts and not parts[-1]:
            parts.pop()
        sub_slot = title_svg.field_by_role(slots, "subtitle")
        if subtitle.strip() and sub_slot is None:
            raise ToolError(f"{name} has a single text line; drop subtitle or use a two-line template "
                            "such as Standard_1, Bar_2 or Gray_Box_4")
        main = title_svg.field_by_role(slots, "title")
        order = [s for s in [main, sub_slot] if s is not None]
        order += [s for s in slots if s not in order]
        if subtitle.strip():
            order = [s for s in order if s is not sub_slot]
            values[sub_slot.index] = subtitle.strip()
        if len(parts) > len(order):
            raise ToolError(
                f"{name} has {count} text field(s) but the text has {len(parts)} lines; use a template "
                "with more lines (Standard_3, Standard_4, Post_it) or pass lines=[...]")
        for slot, value in zip(order, parts):
            values[slot.index] = value
    if blank_rest:
        for index in by_index:
            values.setdefault(index, "")
    return values


class _Style:
    """Validated font / colour / size changes for a title document."""

    def __init__(self, font="", font_weight="", font_style="", font_scale=1.0,
                 text_color="", background_color="", fit_text=True):
        if font_weight not in WEIGHTS:
            raise ToolError("font_weight must be '', 'bold' or 'normal'")
        if font_style not in STYLES:
            raise ToolError("font_style must be '', 'italic' or 'normal'")
        self.font = str(font or "").strip()
        self.bold = WEIGHTS[font_weight]
        self.italic = STYLES[font_style]
        self.font_scale = float(font_scale or 1.0)
        self.text_color = color_hex_alpha(text_color, "text_color") if str(text_color or "").strip() else None
        self.background = (color_hex_alpha(background_color, "background_color")
                           if str(background_color or "").strip() else None)
        self.fit_text = bool(fit_text)

    @property
    def changes_anything(self) -> bool:
        return bool(self.font or self.bold is not None or self.italic is not None
                    or abs(self.font_scale - 1.0) > 1e-9 or self.text_color or self.background)

    def resolve_font(self, families):
        """Match the requested family against the installed fonts (None = unknown, accept as given)."""
        if self.font:
            self.font = resolve_font(self.font, families)

    def apply(self, doc, families=None) -> dict:
        info = {}
        nodes = title_svg.text_nodes(doc) + title_svg.tspan_nodes(doc)
        if not self.font:
            missing, installed = title_svg.installed_font_for_title(doc, families)
            if installed:
                # what the Title Editor does when it opens a title whose font is not installed
                title_svg.apply_font(nodes, family=installed)
                info["font"] = installed
                info["font_replaced"] = missing
        if self.font or self.bold is not None or self.italic is not None or abs(self.font_scale - 1.0) > 1e-9:
            title_svg.apply_font(nodes, family=self.font or None, italic=self.italic, bold=self.bold,
                                 font_size_ratio=self.font_scale)
        if self.text_color:
            title_svg.set_text_color(title_svg.text_nodes(doc), title_svg.tspan_nodes(doc), *self.text_color)
            info["text_color"] = self.text_color[0]
        if self.background:
            rects = title_svg.rect_nodes(doc)
            if not rects:
                raise ToolError("this title has no background shape to colour")
            title_svg.set_background(rects, *self.background)
            info["background_color"] = self.background[0]
        if self.font:
            info["font"] = self.font
        return info


def _fit(doc, slots, style: _Style) -> dict:
    fitted = {}
    if not style.fit_text:
        return fitted
    width, _h = title_svg.artboard_size(doc)
    for slot in slots:
        ratio = title_svg.fit_field(slot, width)
        if ratio < 0.999:
            fitted[str(slot.index)] = round(ratio, 3)
    return fitted


def _project_title_paths() -> list:
    from classes.query import File
    return [f.absolute_path() for f in File.filter() if str(f.data.get("path") or "").lower().endswith(".svg")]


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _frames(seconds: float) -> float:
    fps = project_fps()
    return max(1, int(round(float(seconds) * fps))) / fps


# ---------------------------------------------------------------------------
# list_title_templates_tool
# ---------------------------------------------------------------------------

def _template_entry(path: str) -> dict:
    name = _template_name(path)
    entry = {"name": name, "label": title_svg.template_display_name(path),
             "source": "bundled" if title_svg.is_bundled(path) else "user"}
    try:
        doc = title_svg.load(path)
    except Exception as exc:
        entry["error"] = f"unreadable: {exc}"
        return entry
    slots = title_svg.fields(doc)
    _w, h = title_svg.artboard_size(doc)
    color, alpha = title_svg.text_color(doc, (title_svg.field_by_role(slots, "title") or slots[0]).nodes[0]) \
        if slots else ("", 0.0)
    bg, bg_alpha = title_svg.background_color(doc)
    entry.update({
        "purpose": TEMPLATE_PURPOSE.get(name, "user title template" if entry["source"] == "user" else ""),
        "zone": title_svg.text_zone(slots, h),
        "fields": _field_rows(slots),
        "text_color": color,
        "background": {"color": bg, "opacity": round(bg_alpha, 3)},
        "font": title_svg.font_family(doc),
    })
    if alpha < 0.999:
        entry["text_opacity"] = round(alpha, 3)
    return entry


def _matches(entry: dict, query: str) -> bool:
    if not query:
        return True
    hay = " ".join([entry.get("name", ""), entry.get("label", ""), entry.get("purpose", ""),
                    entry.get("zone", ""), entry.get("title", "")]
                   + [f.get("text", "") for f in entry.get("fields", [])]).lower().replace("_", " ")
    return all(word in hay for word in query.lower().replace("_", " ").split())


def _project_titles(query: str) -> list:
    from classes.query import Clip, File
    out = []
    for f in File.filter():
        data = f.data if isinstance(f.data, dict) else {}
        path = str(data.get("path") or "")
        if not path.lower().endswith(".svg") or data.get("zenvi_role") == OVERLAY_ROLE:
            continue
        entry = {"file_id": f.id, "name": data.get("name") or os.path.basename(path), "path": f.absolute_path()}
        try:
            doc = title_svg.load(f.absolute_path())
            slots = title_svg.fields(doc)
            entry["fields"] = _field_rows(slots)
            entry["text_color"] = title_svg.text_color(doc)[0]
        except Exception as exc:
            entry["error"] = f"unreadable: {exc}"
        entry["clips"] = []
        for c in Clip.filter(file_id=f.id):
            if c.data.get("file_id") != f.id:
                continue
            position = float(c.data.get("position") or 0.0)
            length = float(c.data.get("end") or 0.0) - float(c.data.get("start") or 0.0)
            entry["clips"].append({"timeline_clip_id": c.id, "track": ui_track_number(int(c.data.get("layer") or 0)),
                                   "position": round(position, 3), "end": round(position + length, 3)})
        if _matches(entry, query):
            out.append(entry)
    return out


@editor_tool(
    "list_title_templates_tool",
    label="List title templates",
    schema=obj({
        "kind": enum(["static", "animated", "project", "all"],
                     "static = the 50 SVG title templates + the user's templates (for add_title_tool); "
                     "animated = the Blender animated titles and their parameters (for "
                     "add_animated_title_tool); project = titles already in this project with their "
                     "current text and the clips using them (for edit_title_tool); all = everything.",
                     "static"),
        "query": string("Optional filter words matched against name, look and text, e.g. 'lower third', "
                        "'gold', 'end card', 'credits', 'Tokyo'.", ""),
    }),
    read_only=True,
    covers=("title.templates", "title.animated"),
)
def list_title_templates(kind="static", query=""):
    """List title templates (and the titles already in the project) with their editable text fields.

    Use before add_title_tool when the user wants a particular look ("lower third",
    "end card", "gold title"), before edit_title_tool to see a title's current text,
    and with kind="animated" before add_animated_title_tool. Each static template
    lists its fields in order (index, role title/subtitle/line, placeholder text),
    its text colour, background and where its text sits (zone top/center/bottom).
    Good defaults: Standard_1 (plain centred title + subtitle), Gray_Box_4 (lower
    third: name + role), Bar_3 / Gold_Bottom (bottom text), OpenShot / Box / Flare
    (full-screen end card), Standard_4 (credits), Solid_Color (colour card).
    Example: {"kind": "static", "query": "lower third"}.
    """
    kind = kind or "static"
    query = str(query or "").strip()
    data = {}
    counts = []
    if kind in ("static", "all"):
        templates = [e for e in (_template_entry(p) for p in title_svg.template_paths()
                                 if p.lower().endswith(".svg")) if _matches(e, query)]
        data["templates"] = templates
        counts.append(f"{len(templates)} title template(s)")
    if kind in ("animated", "all"):
        from classes.editor_tools.titles_text_animated import animated_catalog
        animated, blender = animated_catalog(query)
        data["animated"] = animated
        data["blender"] = blender
        counts.append(f"{len(animated)} animated title(s) (Blender {'found' if blender.get('found') else 'not found'})")
    if kind in ("project", "all"):
        titles = _project_titles(query)
        data["project_titles"] = titles
        counts.append(f"{len(titles)} title(s) in the project")
    summary = "Found " + ", ".join(counts) + (f" matching {query!r}" if query else "") + "."
    return ok(summary, kind=kind, **data)


# ---------------------------------------------------------------------------
# add_title_tool
# ---------------------------------------------------------------------------

_STYLE_PROPS = {
    "font": string("Font family for every line, e.g. 'Arial Black', 'Helvetica Neue', 'Georgia'. Must be "
                   "installed; empty keeps the template's font.", ""),
    "font_weight": enum(["", "bold", "normal"], "Make the text bold or normal weight; empty keeps the template's.", ""),
    "font_style": enum(["", "italic", "normal"], "Italic or upright text; empty keeps the template's.", ""),
    "font_scale": number("Text size relative to the template: 1 = as designed, 1.5 = half again as big, 0.7 = smaller.",
                         1.0, minimum=0.2, maximum=5.0),
    "text_color": string("Colour of every line: '#RRGGBB', '#RRGGBBAA' (alpha = opacity), 'rgba(r,g,b,a)' or a "
                         "basic name ('white', 'yellow'). Empty keeps the template's colours.", ""),
    "background_color": string("Colour of the title's full-frame background (transparent in most templates) "
                               "with optional alpha, e.g. '#000000' for a solid black end card or '#00000080' for a "
                               "50% dark veil over the video. Empty keeps it.", ""),
    "fit_text": boolean("Shrink a line whose text would run off the frame (templates do not wrap text).", True),
}


@editor_tool(
    "add_title_tool",
    label="Add title",
    schema=obj({
        "text": string("Main title text, e.g. 'Tokyo Day 1'. A '\\n' puts the rest on the template's next "
                       "field(s): 'Jane Doe\\nFounder' fills a lower third's name and role.", ""),
        "template": string("Template name from list_title_templates_tool (e.g. 'Standard_1', 'Gray_Box_4', "
                           "'Bar_3', 'OpenShot'), or the path of an existing .svg title to start from. Empty = "
                           "Bar_1 (centre title on a blue bar).", ""),
        "position_seconds": number("Timeline second where the title starts.", 0.0, minimum=0),
        "track": string("UI track number (1 = bottom), name or layer for the title. Empty = the lowest free track "
                        "above every clip with video at that time (a new top track when none is free).", ""),
        "duration_seconds": number("How long the title stays on screen, in seconds.", 5.0, minimum=0.01, maximum=86400),
        "file_name": string("Name for the new title file in Project Files (without .svg). Empty = from the text. "
                            "An existing name gets ' (1)', ' (2)'... instead of being overwritten.", ""),
        "subtitle": string("Text for the template's subtitle field (second line), e.g. a role under a name.", ""),
        "lines": array({"type": "string"}, "Every field in order (as listed by list_title_templates_tool), for "
                       "templates with several lines such as Standard_4 or credits. Overrides text/subtitle; fields "
                       "left out are blanked."),
        **_STYLE_PROPS,
        "fade_in_seconds": number("Fade the title in over this many seconds (0 = appear at once).", 0.0,
                                  minimum=0, maximum=10),
        "fade_out_seconds": number("Fade the title out over this many seconds (0 = disappear at once).", 0.0,
                                   minimum=0, maximum=10),
        "screen_position": enum(["auto", "top", "center", "bottom"],
                                "Where the 16:9 title artboard sits when the project is not 16:9 (vertical "
                                "reels, square): auto = by where the template's text is (lower thirds at the "
                                "bottom, headers at the top).", "auto"),
    }),
    background_safe=True,
    covers=("title.create",),
)
def add_title(text="", template="", position_seconds=0.0, track="", duration_seconds=5.0, file_name="",
              subtitle="", lines=None, font="", font_weight="", font_style="", font_scale=1.0, text_color="",
              background_color="", fit_text=True, fade_in_seconds=0.0, fade_out_seconds=0.0,
              screen_position="auto"):
    """Create an on-screen title from a template and place it on the timeline, over the video.

    Use for titles, title cards, lower thirds, end cards, credits and any static
    text over video ("add a title 'Tokyo Day 1'", "lower third with my name",
    "end card saying Thanks for watching"). Not for subtitles or captions of speech
    (add_captions_tool), countdowns (add_timer_tool), emoji (add_emoji_tool) or 3D
    animated titles (add_animated_title_tool).

    Writes a new SVG title from the template with your text, font and colours,
    adds it to Project Files and places it at position_seconds for duration_seconds
    on the lowest free track above the video (never under it). Placeholder text the
    template has but you did not fill (e.g. 'Sub-Title') is blanked. Optional fades.
    One undo step removes the clip and the file entry.

    Examples: {"text": "Tokyo Day 1", "template": "Standard_1", "text_color": "white",
    "fade_in_seconds": 0.5, "fade_out_seconds": 0.5};
    lower third {"text": "Jane Doe", "subtitle": "Founder, Acme", "template": "Gray_Box_4",
    "position_seconds": 3, "duration_seconds": 4};
    end card {"text": "Thanks for watching", "subtitle": "Subscribe for more", "template": "Standard_1",
    "background_color": "#000000", "text_color": "white", "position_seconds": 58}.
    """
    lines = [str(v) for v in (lines or [])]
    text = str(text or "")
    subtitle = str(subtitle or "")
    path = _resolve_template(template)
    name = _template_name(path)
    doc = _load_doc(path)
    slots = title_svg.fields(doc)
    if not slots and (text.strip() or subtitle.strip() or any(v.strip() for v in lines)):
        raise ToolError(f"{name} has no text; it is a background card (set background_color), or pick a "
                        "template with text")
    if slots and not (text.strip() or subtitle.strip() or any(v.strip() for v in lines)):
        raise ToolError("add_title_tool needs text (or subtitle / lines)")
    style = _Style(font, font_weight, font_style, font_scale, text_color, background_color, fit_text)
    values = _assign_texts(slots, text, subtitle, lines, name, blank_rest=True)
    fps = project_fps()
    position = snap_seconds(position_seconds)
    duration = _frames(duration_seconds)
    if duration < 1.0 / fps - 1e-9:
        raise ToolError(f"duration_seconds must be at least one frame ({1.0 / fps:.3f}s)")
    end = position + duration

    def _precheck():
        # where it goes (a refused call writes nothing), names in use, installed fonts
        plan_overlay_track(position, end, track)
        return _project_title_paths(), installed_font_families()

    taken, families = precheck_on_main(_precheck)
    style.resolve_font(families)

    for slot in slots:
        if slot.index in values:
            title_svg.set_field_text(doc, slot, values[slot.index])
    style_info = style.apply(doc, families)
    fitted = _fit(doc, slots, style)
    zone = title_svg.text_zone(slots, title_svg.artboard_size(doc)[1])
    pos_key = ZONE_POSITION.get(zone if screen_position == "auto" else screen_position, "center")

    first_text = next((values[s.index] for s in slots if values.get(s.index, "").strip()), "") or name
    base = str(file_name or "").strip() or first_text
    out_path = title_svg.unique_title_path(title_dir(), base, taken=taken)
    title_svg.write(doc, out_path)

    def _commit():
        layer, created = plan_overlay_track(position, end, track)
        f = import_media_file(out_path)
        if created:
            create_track(layer, "Titles")
        props = {"gravity": GRAVITY[pos_key]}
        alpha = fade_alpha(0.0, duration, fade_in_seconds, fade_out_seconds)
        if alpha:
            props["alpha"] = alpha
        clip = place_clip(f.id, position, duration, layer, props=props)
        return f.id, clip, layer, created

    try:
        file_id, clip, layer, created = commit_on_main(_commit)
    except CommitTimeout:
        raise                    # it may still finish: keep its SVG
    except Exception:
        _discard(out_path)
        raise
    shown = " / ".join(v for v in (values.get(s.index, "").strip() for s in slots) if v) or name
    t = track_info(layer)
    summary = (f"Added title {shown!r} ({name}) on track {t['track']} at {position:.2f}-{end:.2f}s"
               + (" on a new track" if created else "") + ".")
    receipt = dict(timeline_clip_id=clip.get("id"), file_id=file_id, path=out_path,
                   file_name=os.path.basename(out_path), template=name,
                   fields=_field_rows(slots), position=round(position, 3), duration=round(duration, 3),
                   end=round(end, 3), new_track=created, screen_position=pos_key, **t, **style_info)
    if fade_in_seconds or fade_out_seconds:
        receipt.update(fade_in=fade_in_seconds, fade_out=fade_out_seconds)
    if fitted:
        receipt["text_shrunk_to_fit"] = fitted
    return ok(summary, **receipt)


# ---------------------------------------------------------------------------
# edit_title_tool
# ---------------------------------------------------------------------------

def _svg_file(file_obj):
    data = file_obj.data if isinstance(file_obj.data, dict) else {}
    if not str(data.get("path") or "").lower().endswith(".svg"):
        raise ToolError(f"{data.get('name') or os.path.basename(str(data.get('path') or ''))!r} is not a title "
                        "(only .svg titles can be edited)")
    if data.get("zenvi_role") == OVERLAY_ROLE:
        raise ToolError("that clip is a caption/timer overlay; use edit_captions_tool or add_timer_tool")
    return file_obj


def _resolve_title(timeline_clip_id: str, file_id: str, title_query: str):
    """(File, clip_id or '') for the title to edit. GUI thread (reads project + selection)."""
    from classes.query import Clip, File
    if str(timeline_clip_id or "").strip():
        c = Clip.get(id=str(timeline_clip_id).strip())
        if not c:
            raise ToolError(f"no timeline clip with id={timeline_clip_id!r}")
        f = File.get(id=str(c.data.get("file_id") or ""))
        if not f:
            raise ToolError(f"clip {c.id} has no project file")
        return _svg_file(f), c.id
    if str(file_id or "").strip():
        f = File.get(id=str(file_id).strip())
        if not f:
            raise ToolError(f"no project file with id={file_id!r}")
        return _svg_file(f), ""
    titles = [f for f in File.filter()
              if str(f.data.get("path") or "").lower().endswith(".svg")
              and f.data.get("zenvi_role") != OVERLAY_ROLE]
    if str(title_query or "").strip():
        q = str(title_query).strip().lower()
        scored = []
        for f in titles:
            name = str(f.data.get("name") or os.path.basename(str(f.data.get("path"))))
            try:
                texts = [s.text.strip() for s in title_svg.fields(title_svg.load(f.absolute_path()))]
            except Exception:
                texts = []
            hay = [name.lower()] + [t.lower() for t in texts]
            if any(h == q for h in hay):
                scored.append((2, f))
            elif any(q in h for h in hay):
                scored.append((1, f))
        if not scored:
            raise ToolError(f"no title in the project matches {title_query!r} "
                            "(list_title_templates_tool kind=project lists them)")
        best = max(s for s, _ in scored)
        top = [f for s, f in scored if s == best]
        if len(top) > 1:
            raise ToolError(f"{len(top)} titles match {title_query!r}: "
                            + ", ".join(f"{f.id} ({f.data.get('name') or os.path.basename(f.data.get('path'))})"
                                        for f in top[:6]) + "; pass file_id or timeline_clip_id")
        return top[0], ""
    selected = [Clip.get(id=cid) for cid in selected_clip_ids()]
    selected = [c for c in selected if c and str((c.data.get("reader") or {}).get("path") or "").lower().endswith(".svg")]
    if len(selected) == 1:
        f = File.get(id=str(selected[0].data.get("file_id") or ""))
        if f:
            return _svg_file(f), selected[0].id
    if len(titles) == 1:
        return titles[0], ""
    raise ToolError("which title? pass timeline_clip_id, file_id or title_query "
                    f"({len(titles)} titles in the project; list_title_templates_tool kind=project lists them)")


def _clips_of(file_id: str) -> list:
    from classes.query import Clip
    return [c for c in Clip.filter(file_id=file_id) if c.data.get("file_id") == file_id]


def _version_path(folder: str, current: str) -> str:
    stem = os.path.splitext(os.path.basename(current))[0]
    m = re.match(r"^(.*?) v(\d+)$", stem)
    base, n = (m.group(1), int(m.group(2))) if m else (stem, 1)
    for k in range(n + 1, n + 1000):
        path = os.path.join(folder, f"{base} v{k}.svg")
        if not os.path.exists(path):
            return path
    raise ToolError(f"no free version name for {stem!r}")


@editor_tool(
    "edit_title_tool",
    label="Edit title",
    schema=obj({
        "timeline_clip_id": string("A title clip on the timeline (from add_title_tool or get_timeline_state_tool).", ""),
        "file_id": string("The title's Project Files id instead of a clip.", ""),
        "title_query": string("Find the title by its current text or file name, e.g. 'Tokyo'.", ""),
        "text": string("New main title text ('\\n' continues into the next fields). Empty keeps it.", ""),
        "subtitle": string("New subtitle text. Empty keeps it.", ""),
        "lines": array({"type": "string"}, "New text for fields 1..N in order (list_title_templates_tool "
                       "kind=project shows them); '' blanks a field; fields after N are kept."),
        **_STYLE_PROPS,
        "scope": enum(["all_clips", "this_clip", "duplicate"],
                      "all_clips = change the title everywhere it is used (Edit Title); this_clip = change only "
                      "timeline_clip_id, as its own copy; duplicate = save the changed copy as a new title in "
                      "Project Files without touching any clip (Duplicate).", "all_clips"),
    }),
    background_safe=True,
    covers=("title.edit",),
)
def edit_title(timeline_clip_id="", file_id="", title_query="", text="", subtitle="", lines=None, font="",
               font_weight="", font_style="", font_scale=1.0, text_color="", background_color="",
               fit_text=True, scope="all_clips"):
    """Change an existing title's text, font or colours, or duplicate it.

    Use when the user wants to fix or restyle a title that is already there ("change
    the title to 'Day 2'", "make the lower third yellow", "use Georgia for the end
    card", "make a copy of this title"). Target it by timeline_clip_id (best),
    file_id or title_query (its text); with no target the selected title clip, or the
    project's only title, is used. Only what you pass changes; other fields keep their
    text. scope=all_clips (default) updates every clip showing that title, like the
    editor's Edit Title; this_clip gives just that clip its own edited copy;
    duplicate saves an edited copy as a new title (place it with
    add_clip_to_timeline_tool). One undo step restores the previous look.
    To move, trim or re-time a title clip use the timeline tools instead.

    Example: {"timeline_clip_id": "C1A2", "text": "Tokyo Day 2", "text_color": "#FFD700"}.
    """
    lines = [str(v) for v in (lines or [])]
    text = str(text or "")
    subtitle = str(subtitle or "")
    style = _Style(font, font_weight, font_style, font_scale, text_color, background_color, fit_text)
    has_text = bool(text.strip() or subtitle.strip() or lines)
    if not has_text and not style.changes_anything and scope != "duplicate":
        raise ToolError("nothing to change: pass text, subtitle, lines, font, colours or font_scale "
                        "(or scope='duplicate' to copy the title)")

    def _gather():
        found, found_clip = _resolve_title(timeline_clip_id, file_id, title_query)
        return found, found_clip, _clips_of(found.id), _project_title_paths(), installed_font_families()

    f, clip_id, clips, taken, families = precheck_on_main(_gather)
    if scope == "this_clip" and not clip_id:
        raise ToolError("scope='this_clip' needs timeline_clip_id (the clip that gets its own copy)")
    src_path = f.absolute_path()
    if not os.path.isfile(src_path):
        raise ToolError(f"the title file is missing on disk: {src_path}")
    doc = _load_doc(src_path)
    slots = title_svg.fields(doc)
    name = str(f.data.get("name") or os.path.basename(src_path))
    if has_text and not slots:
        raise ToolError(f"{name} has no text to change")
    values = _assign_texts(slots, text, subtitle, lines, name, blank_rest=False) if has_text else {}
    style.resolve_font(families)

    if scope == "this_clip" and len(clips) <= 1:
        scope_used = "all_clips"
    else:
        scope_used = scope
    affected = clips if scope_used == "all_clips" else [c for c in clips if c.id == clip_id]
    if scope_used != "duplicate":
        locked = [c for c in affected if is_locked(int(c.data.get("layer") or 0))]
        if locked:
            raise ToolError("title clip(s) on locked track(s): "
                            + ", ".join(f"{c.id} (track {ui_track_number(int(c.data.get('layer') or 0))})"
                                        for c in locked) + "; unlock first")

    before_xml = title_svg.to_xml(doc)
    for slot in slots:
        if slot.index in values:
            title_svg.set_field_text(doc, slot, values[slot.index])
    style_info = style.apply(doc, families)
    fitted = _fit(doc, slots, style)
    after = _field_rows(slots)
    if scope_used != "duplicate" and title_svg.to_xml(doc) == before_xml:
        raise ToolError("the title already looks like that; nothing changed")

    folder = title_dir()
    if scope_used == "all_clips":
        out_path = _version_path(folder, src_path)
    else:
        pattern, offset = title_svg.duplicate_pattern(os.path.basename(src_path))
        free = title_svg.free_name(pattern, offset, folder, taken=taken)
        if not free:
            raise ToolError("no free name for the copy")
        out_path = os.path.join(folder, free + ".svg")
    title_svg.write(doc, out_path)
    from classes.media_fingerprint import fingerprint
    new_fp = fingerprint(out_path)

    def _commit():
        from classes.query import Clip, File
        if scope_used == "all_clips":
            current = File.get(id=f.id)
            changes = {"path": out_path, "name": current.data.get("name") or os.path.basename(src_path)}
            if new_fp:
                changes["fingerprint"] = new_fp
            current.data = changes
            current.save()
            ids = []
            for c in _clips_of(f.id):
                reader = dict(c.data.get("reader") or {})
                reader["path"] = out_path
                live = Clip.get(id=c.id)
                live.data = {"reader": reader}
                live.save()
                ids.append(c.id)
            refresh_file_and_clips(f.id, ids)
            return f.id, ids
        new_file = import_media_file(out_path)
        if scope_used == "this_clip":
            live = Clip.get(id=clip_id)
            live.data = {"file_id": new_file.id, "reader": dict(new_file.data)}
            live.save()
            refresh_file_and_clips(new_file.id, [clip_id])
            return new_file.id, [clip_id]
        return new_file.id, []

    try:
        new_file_id, clip_ids = commit_on_main(_commit)
    except CommitTimeout:
        raise                    # it may still finish: keep its SVG
    except Exception:
        _discard(out_path)
        raise
    shown = " / ".join(r["text"] for r in after if r["text"]) or name
    if scope_used == "all_clips":
        summary = f"Updated title {shown!r} on {len(clip_ids)} clip(s)."
    elif scope_used == "this_clip":
        summary = f"Gave clip {clip_id} its own edited title {shown!r}."
    else:
        summary = f"Duplicated the title as {os.path.basename(out_path)!r} ({shown!r}); it is in Project Files."
    receipt = dict(scope=scope_used, file_id=new_file_id, source_file_id=f.id, path=out_path,
                   timeline_clip_ids=clip_ids, fields=after, **style_info)
    if scope != scope_used:
        receipt["note"] = "the title is used by only this clip, so it was edited in place"
    if fitted:
        receipt["text_shrunk_to_fit"] = fitted
    return ok(summary, **receipt)


# Register the sibling tool modules (they import helpers from here).
from classes.editor_tools import (  # noqa: E402,F401
    titles_text_animated,
    titles_text_captions,
    titles_text_overlays,
)
