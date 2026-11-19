"""Text on screen: HyperFrames titles, animated (Blender) titles, captions, timer overlays, emoji.

Workstream: titles-text. Tools register themselves with ``@editor_tool`` from
``classes.editor_tools._registry``; helpers live in ``classes.editor_tools._base``
and ``titles_text_common``.

add_title_tool draws a title as a HyperFrames composition (``classes.hyperframes_titles``):
animated type rendered on this computer into a transparent video (an opaque one for a
full-frame card) and placed above the footage. edit_title_tool renders such a title
again with the changes; it also edits the SVG titles the Title Editor makes, with the
rules it shares with that dialog through ``classes.title_svg``.

The sibling modules register the rest: ``titles_text_captions`` (HyperFrames caption
overlays), ``titles_text_overlays`` (Timer, emoji) and ``titles_text_animated`` (Blender).

add_title_tool requests: "add a title 'Tokyo Day 1'", "lower third with my
name", "end card saying Thanks for watching". Example calls: {"text": "Tokyo
Day 1", "subtitle": "Shibuya at night"}; lower third {"text": "Jane Doe",
"subtitle": "Founder, Acme", "style": "lower_third", "position_seconds": 3,
"duration_seconds": 4}; end card {"text": "Thanks for watching", "subtitle":
"Subscribe for more", "style": "end_card", "position_seconds": 58}. A
background_color of '#00000080' is a 50% dark veil over the video.

edit_title_tool requests: "change the title to 'Day 2'", "make the lower third
yellow", "use Georgia for the end card", "make a copy of this title". Example:
{"timeline_clip_id": "C1A2", "text": "Tokyo Day 2", "text_color": "#FFD700"}.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from typing import Dict, List

from classes import hyperframes_local as hf
from classes import hyperframes_titles as ht
from classes import title_svg
from classes.editor_tools._base import (
    ToolError, array, enum, is_locked, number, obj, ok, project_fps, selected_clip_ids,
    snap_seconds, string, th, ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.titles_text_common import (
    CommitTimeout, color_hex_alpha, commit_on_main, create_track, import_media_file,
    installed_font_families, place_clip, plan_overlay_track, precheck_on_main, project_size,
    refresh_file_and_clips, resolve_font, title_dir, track_info, warning_note, warnings_field, OVERLAY_ROLE,
)

RENDER_TIMEOUT = 900
SCREEN_POSITIONS = ("auto",) + ht.ZONES
SETUP_HINT = ("HyperFrames is not set up on this computer yet; call "
              "run_tool(name='hyperframes_setup_tool', args={'install': true}) first, then add the title again")


# ---------------------------------------------------------------------------
# SVG title documents (the Title Editor's)
# ---------------------------------------------------------------------------

def _load_doc(path: str):
    try:
        return title_svg.load(path)
    except Exception as exc:
        raise ToolError(f"cannot read title SVG {os.path.basename(path)}: {exc}") from None


def _field_rows(slots) -> list:
    return [{"index": s.index, "role": s.role, "text": s.text.strip()} for s in slots]


def _assign_texts(slots, text: str, subtitle: str, lines: List[str], name: str) -> Dict[int, str]:
    """Map text / subtitle / lines onto the title's fields (index -> new text)."""
    values: Dict[int, str] = {}
    count = len(slots)
    if lines:
        if len(lines) > count:
            raise ToolError(f"{name} has {count} text field(s) {[s.role for s in slots]} but {len(lines)} lines "
                            "were given")
        for i, value in enumerate(lines):
            values[slots[i].index] = str(value)
        return values
    parts = [p.strip() for p in str(text or "").split("\n")]
    while parts and not parts[-1]:
        parts.pop()
    sub_slot = title_svg.field_by_role(slots, "subtitle")
    if subtitle.strip() and sub_slot is None:
        raise ToolError(f"{name} has a single text line; drop subtitle")
    main = title_svg.field_by_role(slots, "title")
    order = [s for s in [main, sub_slot] if s is not None]
    order += [s for s in slots if s not in order]
    if subtitle.strip():
        order = [s for s in order if s is not sub_slot]
        values[sub_slot.index] = subtitle.strip()
    if len(parts) > len(order):
        raise ToolError(f"{name} has {count} text field(s) but the text has {len(parts)} lines; pass lines=[...]")
    for slot, value in zip(order, parts):
        values[slot.index] = value
    return values


class _Style:
    """Validated font / colour / size changes for an SVG title document."""

    def __init__(self, font="", font_scale=1.0, text_color="", background_color=""):
        self.font = str(font or "").strip()
        self.font_scale = float(font_scale or 1.0)
        self.text_color = color_hex_alpha(text_color, "text_color") if str(text_color or "").strip() else None
        self.background = (color_hex_alpha(background_color, "background_color")
                           if str(background_color or "").strip() else None)

    @property
    def changes_anything(self) -> bool:
        return bool(self.font or abs(self.font_scale - 1.0) > 1e-9 or self.text_color or self.background)

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
        if self.font or abs(self.font_scale - 1.0) > 1e-9:
            title_svg.apply_font(nodes, family=self.font or None, font_size_ratio=self.font_scale)
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


def _fit(doc, slots) -> dict:
    fitted = {}
    width, _h = title_svg.artboard_size(doc)
    for slot in slots:
        ratio = title_svg.fit_field(slot, width)
        if ratio < 0.999:
            fitted[str(slot.index)] = round(ratio, 3)
    return fitted


def _project_paths() -> list:
    from classes.query import File
    return [f.absolute_path() for f in File.filter()]


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
# HyperFrames titles
# ---------------------------------------------------------------------------

def _colour(value, what: str) -> str:
    """Any colour form the old tool took ('yellow', 'rgba(...)', '#RRGGBBAA') as '#rrggbb[aa]'."""
    if not str(value or "").strip():
        return ""
    hex_, alpha = color_hex_alpha(value, what)
    return hex_ + ("" if alpha >= 1 else "%02x" % round(alpha * 255))


def _spec(style, text, subtitle, lines, text_color, accent_color, background_color, font, font_scale,
          screen_position) -> dict:
    try:
        return ht.spec(style, title=text, subtitle=subtitle, lines=lines,
                       text_color=_colour(text_color, "text_color"),
                       accent_color=_colour(accent_color, "accent_color"),
                       background_color=_colour(background_color, "background_color"), font=font,
                       font_scale=font_scale, zone="" if screen_position in ("", "auto") else screen_position)
    except ht.TitleError as exc:
        raise ToolError(str(exc)) from None


def _hf_fields(s: dict) -> dict:
    return {"lines": list(s["lines"])} if s["lines"] else {"title": s["title"], "subtitle": s["subtitle"]}


def _hf_shown(s: dict) -> str:
    return " / ".join(v for v in ([s["title"], s["subtitle"]] + list(s["lines"])) if v) or "colour card"


def _hf_metadata(s: dict, duration: float) -> dict:
    return {"source": ht.SOURCE, "title": s, "title_duration": round(duration, 6), "analyzed": True,
            "short_summary": "%s title: %s" % (ht.STYLES[s["style"]]["label"], _hf_shown(s))}


def _free_path(folder: str, base: str, ext: str, taken=()) -> str:
    """'<base><ext>' in *folder*, else '<base> (1)<ext>'...: free on disk and not a Project Files path in *taken*."""
    base = title_svg.safe_base_name(base)
    used = {os.path.normcase(os.path.abspath(p)) for p in taken}
    path, n = os.path.join(folder, base + ext), 0
    while os.path.exists(path) or os.path.normcase(os.path.abspath(path)) in used:
        n += 1
        path = os.path.join(folder, "%s (%d)%s" % (base, n, ext))
    return path


def _render_title(s: dict, size, fps: float, duration: float, out_path: str) -> None:
    """Render title spec *s* with HyperFrames into *out_path* (.mov with alpha, or .mp4 when opaque)."""
    project = "title-%d" % time.time_ns()
    try:
        root = hf.project_dir(project)
        os.makedirs(root)
        with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(ht.compile_composition(s, size[0], size[1], duration, fps))
        opaque = ht.is_opaque(s)
        rel = "renders/title." + ("mp4" if opaque else "webm")
        code, out = hf.run(project, "hyperframes", ["render", ".", "--format", rel.rsplit(".", 1)[1], "-o", rel,
                                                    "--fps", str(max(1, int(round(fps)))), "--quiet"],
                           timeout=RENDER_TIMEOUT)
        if code != 0:
            raise ToolError("the title render failed (exit code %d):\n%s" % (code, out[-1500:]))
        src = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(src) or os.path.getsize(src) <= 0:
            raise ToolError("the title render produced no video")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        if opaque:
            shutil.move(src, out_path)
            return
        _path, err = th()._reencode_alpha_for_openshot(src, output_path=out_path, width=size[0], height=size[1])
        if err:
            _discard(out_path)
            raise ToolError(err)
    except hf.HyperframesError as exc:
        raise ToolError(str(exc)) from None
    finally:
        shutil.rmtree(hf.project_dir(project), ignore_errors=True)


# ---------------------------------------------------------------------------
# list_title_templates_tool
# ---------------------------------------------------------------------------

def _matches(entry: dict, query: str) -> bool:
    if not query:
        return True
    fields = entry.get("fields", [])
    if isinstance(fields, dict):                    # a HyperFrames title: {title, subtitle} or {lines}
        texts = [v if isinstance(v, str) else " ".join(v) for v in fields.values()]
    else:                                           # SVG field rows, or a style's field names
        texts = [f.get("text", "") if isinstance(f, dict) else "" for f in fields]
    hay = " ".join([entry.get("name", ""), entry.get("label", ""), entry.get("look", ""), entry.get("style", ""),
                    entry.get("zone", "")] + texts).lower().replace("_", " ")
    return all(word in hay for word in query.lower().replace("_", " ").split())


def _clip_rows(file_id: str) -> list:
    rows = []
    for c in _clips_of(file_id):
        position = float(c.data.get("position") or 0.0)
        length = float(c.data.get("end") or 0.0) - float(c.data.get("start") or 0.0)
        rows.append({"timeline_clip_id": c.id, "track": ui_track_number(int(c.data.get("layer") or 0)),
                     "position": round(position, 3), "end": round(position + length, 3)})
    return rows


def _project_titles(query: str) -> list:
    from classes.query import File
    out = []
    for f in File.filter():
        data = f.data if isinstance(f.data, dict) else {}
        path = str(data.get("path") or "")
        s = ht.title_spec(data)
        if s:
            entry = {"file_id": f.id, "kind": "hyperframes", "name": data.get("name") or os.path.basename(path),
                     "path": f.absolute_path(), "style": s["style"], "fields": _hf_fields(s)}
        elif path.lower().endswith(".svg") and data.get("zenvi_role") != OVERLAY_ROLE:
            entry = {"file_id": f.id, "kind": "svg", "name": data.get("name") or os.path.basename(path),
                     "path": f.absolute_path()}
            try:
                doc = title_svg.load(f.absolute_path())
                entry["fields"] = _field_rows(title_svg.fields(doc))
                entry["text_color"] = title_svg.text_color(doc)[0]
            except Exception as exc:
                entry["error"] = f"unreadable: {exc}"
        else:
            continue
        entry["clips"] = _clip_rows(f.id)
        if _matches(entry, query):
            out.append(entry)
    return out


@editor_tool(
    "list_title_templates_tool",
    label="List title templates",
    schema=obj({
        "kind": enum(["styles", "animated", "project", "all"],
                     "styles = the title styles run_tool(name='add_title_tool') draws (HyperFrames); animated = the Blender "
                     "animated titles and their parameters (for add_animated_title_tool); project = titles "
                     "already in this project with their current text and the clips using them (for "
                     "edit_title_tool); all = everything.", "styles"),
        "query": string("Optional filter words matched against name, look and text, e.g. 'lower third', "
                        "'end card', 'credits', 'Tokyo'.", ""),
    }),
    read_only=True,
    covers=("title.templates", "title.animated"),
)
def list_title_templates(kind="styles", query=""):
    """List the title styles (and the titles already in the project) with the fields they show.

    Use before run_tool(name='add_title_tool') when the user wants a particular look ("lower third",
    "end card", "credits"), before run_tool(name='edit_title_tool') to see a title's current text,
    and with kind="animated" before run_tool(name='add_animated_title_tool'). Each style lists its
    look, the fields it draws (title / subtitle, or lines) and where it sits (zone top/center/bottom).
    Example: {"kind": "styles", "query": "lower third"}.
    """
    kind = kind or "styles"
    query = str(query or "").strip()
    data = {}
    counts = []
    if kind in ("styles", "all"):
        styles = [e for e in ({"style": key, "label": s["label"], "look": s["description"], "zone": s["zone"],
                               "fields": list(s["fields"])} for key, s in ht.STYLES.items()) if _matches(e, query)]
        data["styles"] = styles
        counts.append(f"{len(styles)} title style(s)")
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

_LOOK_PROPS = {
    "style": enum(list(ht.STYLES), "The look (list_title_templates_tool describes each): headline (big title, "
                  "words rise in), lower_third (name + role on a plate), end_card (full-frame card), bold_box "
                  "(social text on solid boxes), kicker (small tracked label), credits (lines).", ht.DEFAULT_STYLE),
    "font": string("Font family, e.g. 'Georgia' or a Google Fonts family such as 'Playfair Display'. Empty = "
                   "Inter.", ""),
    "font_scale": number("Text size vs the style's (1 = as designed).", 1.0, minimum=0.2, maximum=5.0),
    "text_color": string("Text colour: '#RRGGBB', '#RRGGBBAA' (alpha = opacity), 'rgba(r,g,b,a)' or a name. "
                         "Empty = the style's (white; dark on bold_box).", ""),
    "accent_color": string("Rule, bar and box colour. Empty = the style's (warm yellow; white boxes on "
                           "bold_box).", ""),
    "background_color": string("Full-frame background, alpha allowed: '#00000080' is a 50% dark veil over the "
                               "video; end_card defaults to near-black. Empty = none.", ""),
    "screen_position": enum(list(SCREEN_POSITIONS), "Where the text sits: auto = the style's own place "
                            "(lower_third bottom, kicker top, the rest centre). On 9:16 the text stays clear of "
                            "the platforms' UI bands.", "auto"),
}


@editor_tool(
    "add_title_tool",
    label="Add title",
    schema=obj({
        "text": string("Main title text. A second line ('Jane Doe\\nFounder') becomes the subtitle; on bold_box "
                       "every line gets its own box; on credits every line is an entry.", ""),
        "subtitle": string("Second line, e.g. a role under a name.", ""),
        "lines": array({"type": "string"}, "Entries for credits (one per line); on other styles the first is "
                       "the title and the rest the subtitle. Overrides text/subtitle."),
        **_LOOK_PROPS,
        "position_seconds": number("Timeline second where the title starts.", 0.0, minimum=0),
        "track": string("UI track number (1 = bottom), name or layer. Empty = the lowest free track above every "
                        "clip with video at that time (a new top track when none is free). A busy named track "
                        "falls back to the first free track above it (with a warning).", ""),
        "duration_seconds": number("Seconds on screen (the animation in and out fits inside it).", 5.0,
                                   minimum=0.01, maximum=600),
        "file_name": string("Project Files name (no extension). Empty = from the text. Taken names get ' (1)', "
                            "' (2)'... (never overwritten).", ""),
    }),
    background_safe=True,
    covers=("title.create",),
)
def add_title(text="", subtitle="", lines=None, style=ht.DEFAULT_STYLE, font="", font_scale=1.0, text_color="",
              accent_color="", background_color="", screen_position="auto", position_seconds=0.0, track="",
              duration_seconds=5.0, file_name=""):
    """Create an animated on-screen title and place it on the timeline, over the video.

    For titles, title cards, lower thirds, end cards, credits and any text over
    video. Not for captions of speech (add_captions_tool), countdowns
    (run_tool(name='add_timer_tool')), emoji (run_tool(name='add_emoji_tool')) or 3D
    Blender titles (run_tool(name='add_animated_title_tool')). The title is a
    HyperFrames composition rendered on this computer (from a few seconds to a
    minute) into a transparent video, added to Project Files and placed above the
    video (never under it); end_card renders an opaque full-frame card. No text
    with a background_color makes a blank colour card, e.g. a 0.1 s white flash
    frame. One undo step removes the clip and the file entry.
    """
    lines = [str(v) for v in (lines or [])]
    s = _spec(style or ht.DEFAULT_STYLE, str(text or ""), str(subtitle or ""), lines, text_color, accent_color,
              background_color, font, font_scale, screen_position)
    fps = project_fps()
    position = snap_seconds(position_seconds)
    duration = _frames(duration_seconds)
    if duration < 1.0 / fps - 1e-9:
        raise ToolError(f"duration_seconds must be at least one frame ({1.0 / fps:.3f}s)")
    end = position + duration

    def _precheck():
        plan_overlay_track(position, end, track)        # where it goes (a refused call renders nothing)
        return project_size(), _project_paths()

    size, taken = precheck_on_main(_precheck)
    if not hf.status()["ready"]:
        raise ToolError(SETUP_HINT)
    opaque = ht.is_opaque(s)
    base = str(file_name or "").strip() or (s["title"] or (s["lines"] or [""])[0]).split("\n")[0] or "Title"
    out_path = _free_path(title_dir(), base, ".mp4" if opaque else ".mov", taken)
    _render_title(s, size, fps, duration, out_path)
    ai = _hf_metadata(s, duration)
    moved = []

    def _commit():
        layer, created = plan_overlay_track(position, end, track, moved=moved)
        f = import_media_file(out_path, extra={"ai_metadata": ai})
        if created:
            create_track(layer, "Titles")
        clip = place_clip(f.id, position, duration, layer)
        return f.id, clip, layer, created

    try:
        file_id, clip, layer, created = commit_on_main(_commit)
    except CommitTimeout:
        raise                    # it may still finish: keep its render
    except Exception:
        _discard(out_path)
        raise
    label = ht.STYLES[s["style"]]["label"]
    t = track_info(layer)
    warnings = list(moved)
    summary = (f"Added title {_hf_shown(s)!r} ({label}) on track {t['track']} at {position:.2f}-{end:.2f}s"
               + (" on a new track" if created else "") + "." + warning_note(warnings))
    return ok(summary, timeline_clip_id=clip.get("id"), file_id=file_id, path=out_path,
              file_name=os.path.basename(out_path), style=s["style"], fields=_hf_fields(s),
              position=round(position, 3), duration=round(duration, 3), end=round(end, 3), new_track=created,
              screen_position=s["zone"], opaque=opaque, **t, **warnings_field(warnings))


# ---------------------------------------------------------------------------
# edit_title_tool
# ---------------------------------------------------------------------------

def _title_file(file_obj):
    data = file_obj.data if isinstance(file_obj.data, dict) else {}
    if ht.title_spec(data):
        return file_obj
    if not str(data.get("path") or "").lower().endswith(".svg"):
        raise ToolError(f"{data.get('name') or os.path.basename(str(data.get('path') or ''))!r} is not a title "
                        "(only titles made with run_tool(name='add_title_tool') or the Title Editor can be edited)")
    if data.get("zenvi_role") == OVERLAY_ROLE:
        raise ToolError("that clip is a timer overlay, not a title; use run_tool(name='add_timer_tool')")
    return file_obj


def _is_title(data: dict) -> bool:
    return bool(ht.title_spec(data)) or (str(data.get("path") or "").lower().endswith(".svg")
                                          and data.get("zenvi_role") != OVERLAY_ROLE)


def _title_texts(f) -> list:
    s = ht.title_spec(f.data)
    if s:
        return [s["title"], s["subtitle"]] + list(s["lines"])
    try:
        return [slot.text.strip() for slot in title_svg.fields(title_svg.load(f.absolute_path()))]
    except Exception:
        return []


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
        return _title_file(f), c.id
    if str(file_id or "").strip():
        f = File.get(id=str(file_id).strip())
        if not f:
            raise ToolError(f"no project file with id={file_id!r}")
        return _title_file(f), ""
    titles = [f for f in File.filter() if _is_title(f.data)]
    if str(title_query or "").strip():
        q = str(title_query).strip().lower()
        scored = []
        for f in titles:
            name = str(f.data.get("name") or os.path.basename(str(f.data.get("path"))))
            hay = [name.lower()] + [t.lower() for t in _title_texts(f) if t]
            if any(h == q for h in hay):
                scored.append((2, f))
            elif any(q in h for h in hay):
                scored.append((1, f))
        if not scored:
            raise ToolError(f"no title in the project matches {title_query!r} "
                            "(run_tool(name='list_title_templates_tool', args={'kind': 'project'}) lists them)")
        best = max(s for s, _ in scored)
        top = [f for s, f in scored if s == best]
        if len(top) > 1:
            raise ToolError(f"{len(top)} titles match {title_query!r}: "
                            + ", ".join(f"{f.id} ({f.data.get('name') or os.path.basename(f.data.get('path'))})"
                                        for f in top[:6]) + "; pass file_id or timeline_clip_id")
        return top[0], ""
    selected = [Clip.get(id=cid) for cid in selected_clip_ids()]
    selected = [c for c in selected if c]
    title_ids = {f.id for f in titles}
    selected = [c for c in selected if str(c.data.get("file_id") or "") in title_ids]
    if len(selected) == 1:
        return File.get(id=str(selected[0].data.get("file_id"))), selected[0].id
    if len(titles) == 1:
        return titles[0], ""
    raise ToolError("which title? pass timeline_clip_id, file_id or title_query "
                    f"({len(titles)} titles in the project; run_tool(name='list_title_templates_tool', args={{'kind': 'project'}}) lists them)")


def _clips_of(file_id: str) -> list:
    from classes.query import Clip
    return [c for c in Clip.filter(file_id=file_id) if c.data.get("file_id") == file_id]


def _version_path(folder: str, current: str, ext: str, taken=()) -> str:
    stem = os.path.splitext(os.path.basename(current))[0]
    m = re.match(r"^(.*?) v(\d+)$", stem)
    base, n = (m.group(1), int(m.group(2))) if m else (stem, 1)
    used = {os.path.normcase(os.path.abspath(p)) for p in taken}
    for k in range(n + 1, n + 1000):
        path = os.path.join(folder, f"{base} v{k}{ext}")
        if not os.path.exists(path) and os.path.normcase(os.path.abspath(path)) not in used:
            return path
    raise ToolError(f"no free version name for {stem!r}")


def _edited_spec(old: dict, text, subtitle, lines, style, font, font_scale, text_color, accent_color,
                 background_color, screen_position) -> dict:
    """*old* with only what was passed changed (font_scale multiplies the current size)."""
    new_style = style or old["style"]
    if lines:
        title, sub = "", ""
    else:
        title = text if text.strip() else (old["title"] or "\n".join(old["lines"]))
        sub = subtitle if subtitle.strip() else ("" if "\n" in text else old["subtitle"])
    if screen_position not in ("", "auto"):
        zone = screen_position
    else:
        zone = old["zone"] if new_style == old["style"] else ""
    return _spec(new_style, title, sub, lines, text_color or old["text_color"],
                 accent_color or old["accent_color"], background_color or old["background_color"],
                 font or old["font"], max(0.2, min(5.0, old["font_scale"] * float(font_scale or 1.0))), zone)


def _commit_edit(f, scope_used, clip_id, out_path, src_path, extra=None, replace=False):
    """Point the title's clips (all_clips), one clip (this_clip) or nothing (duplicate) at *out_path*. GUI thread.

    *replace* (a rendered title): all_clips imports the render as a new Project Files entry, with its own
    probe, plays it in every clip and removes the old entry; an SVG title keeps its entry, re-pointed.
    """
    from classes.media_fingerprint import fingerprint
    from classes.query import Clip, File
    clips = _clips_of(f.id) if scope_used == "all_clips" else [c for c in _clips_of(f.id) if c.id == clip_id]
    if scope_used != "duplicate":
        _refuse_locked(clips)
    if scope_used == "all_clips" and replace:
        new_file = import_media_file(out_path, extra=extra)
        ids = []
        for c in clips:
            live = Clip.get(id=c.id)
            live.data = {"file_id": new_file.id, "reader": dict(new_file.data)}
            live.save()
            ids.append(c.id)
        old = File.get(id=f.id)
        if old:
            old.delete()
        refresh_file_and_clips(new_file.id, ids)
        return new_file.id, ids
    new_fp = fingerprint(out_path)
    if scope_used == "all_clips":
        current = File.get(id=f.id)
        changes = {"path": out_path, "name": current.data.get("name") or os.path.basename(src_path)}
        if new_fp:
            changes["fingerprint"] = new_fp
        changes.update(extra or {})
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
    new_file = import_media_file(out_path, extra=extra)
    if scope_used == "this_clip":
        live = Clip.get(id=clip_id)
        live.data = {"file_id": new_file.id, "reader": dict(new_file.data)}
        live.save()
        refresh_file_and_clips(new_file.id, [clip_id])
        return new_file.id, [clip_id]
    return new_file.id, []


def _refuse_locked(clips) -> None:
    locked = [c for c in clips if is_locked(int(c.data.get("layer") or 0))]
    if locked:
        raise ToolError("title clip(s) on locked track(s): "
                        + ", ".join(f"{c.id} (track {ui_track_number(int(c.data.get('layer') or 0))})"
                                    for c in locked) + "; unlock first")


def _commit_or_discard(func, out_path):
    try:
        return commit_on_main(func)
    except CommitTimeout:
        raise                    # it may still finish: keep its file
    except Exception:
        _discard(out_path)
        raise


@editor_tool(
    "edit_title_tool",
    label="Edit title",
    schema=obj({
        "timeline_clip_id": string("A title clip on the timeline. Best target.", ""),
        "file_id": string("The title's Project Files id instead of a clip.", ""),
        "title_query": string("Find the title by its current text or file name, e.g. 'Tokyo'.", ""),
        "text": string("New main text ('\\n' continues into the subtitle). Empty keeps it.", ""),
        "subtitle": string("New subtitle. Empty keeps it.", ""),
        "lines": array({"type": "string"}, "New lines (credits entries, or an SVG title's fields 1..N in order; "
                       "'' blanks an SVG field)."),
        **dict(_LOOK_PROPS, style=string("Change a run_tool(name='add_title_tool') title to another style "
                                         "(list_title_templates_tool lists them). Empty keeps it.", ""),
               font_scale=number("Text size vs now (1 = unchanged).", 1.0, minimum=0.2, maximum=5.0),
               text_color=string("New text colour ('#RRGGBB[AA]', rgba() or a name). Empty keeps it.", ""),
               accent_color=string("New rule, bar or box colour (HyperFrames titles). Empty keeps it.", ""),
               background_color=string("New full-frame background, alpha allowed. Empty keeps it.", "")),
        "scope": enum(["all_clips", "this_clip", "duplicate"],
                      "all_clips = every clip using the title (Edit Title); this_clip = only timeline_clip_id, "
                      "as its own copy; duplicate = save the edit as a new Project Files title, no clip changes "
                      "(place it with add_clip_to_timeline_tool).", "all_clips"),
    }),
    background_safe=True,
    covers=("title.edit",),
)
def edit_title(timeline_clip_id="", file_id="", title_query="", text="", subtitle="", lines=None, style="",
               font="", font_scale=1.0, text_color="", accent_color="", background_color="",
               screen_position="auto", scope="all_clips"):
    """Change an existing title's text, style, font or colours, or duplicate it.

    Titles made with run_tool(name='add_title_tool') are rendered again with the change (HyperFrames, on
    this computer); SVG titles from the Title Editor are rewritten. With no target,
    the selected title clip or the project's only title is used. Only what you pass
    changes. One undo step restores the previous look. To move, trim or re-time a
    title clip use the timeline tools.
    """
    lines = [str(v) for v in (lines or [])]
    text = str(text or "")
    subtitle = str(subtitle or "")
    style = str(style or "").strip()
    has_text = bool(text.strip() or subtitle.strip() or lines)
    looks = bool(style or str(accent_color or "").strip() or screen_position not in ("", "auto"))
    svg_style = _Style(font, font_scale, text_color, background_color)
    if not has_text and not looks and not svg_style.changes_anything and scope != "duplicate":
        raise ToolError("nothing to change: pass text, subtitle, lines, style, font, colours or font_scale "
                        "(or scope='duplicate' to copy the title)")

    def _gather():
        found, found_clip = _resolve_title(timeline_clip_id, file_id, title_query)
        return found, found_clip, _clips_of(found.id), _project_paths(), installed_font_families(), \
            project_size()

    f, clip_id, clips, taken, families, size = precheck_on_main(_gather)
    if scope == "this_clip" and not clip_id:
        raise ToolError("scope='this_clip' needs timeline_clip_id (the clip that gets its own copy)")
    scope_used = "all_clips" if scope == "this_clip" and len(clips) <= 1 else scope
    affected = clips if scope_used == "all_clips" else [c for c in clips if c.id == clip_id]
    if scope_used != "duplicate":
        _refuse_locked(affected)
    src_path = f.absolute_path()
    old = ht.title_spec(f.data)
    if old:
        return _edit_hyperframes(f, old, clip_id, scope, scope_used, src_path, size, taken, text, subtitle, lines, style,
                                 font, font_scale, text_color, accent_color, background_color, screen_position)
    if looks:
        raise ToolError("this is an SVG title from the Title Editor: style, accent_color and screen_position only "
                        "apply to HyperFrames titles; make one with run_tool(name='add_title_tool') instead")
    return _edit_svg(f, clip_id, scope, scope_used, src_path, taken, families, svg_style, text, subtitle, lines,
                     has_text)


def _edit_hyperframes(f, old, clip_id, scope, scope_used, src_path, size, taken, text, subtitle, lines, style, font,
                      font_scale, text_color, accent_color, background_color, screen_position):
    new = _edited_spec(old, text, subtitle, lines, style, font, font_scale, text_color, accent_color,
                       background_color, screen_position)
    if scope_used != "duplicate" and new == old:
        raise ToolError("the title already looks like that; nothing changed")
    if not hf.status()["ready"]:
        raise ToolError(SETUP_HINT)
    duration = float((f.data.get("ai_metadata") or {}).get("title_duration") or f.data.get("duration") or 5.0)
    ext = ".mp4" if ht.is_opaque(new) else ".mov"
    folder = title_dir()
    if scope_used == "all_clips":
        out_path = _version_path(folder, src_path, ext, taken)
    else:
        out_path = _free_path(folder, os.path.splitext(os.path.basename(src_path))[0], ext, taken)
    _render_title(new, size, project_fps(), duration, out_path)
    new_id, clip_ids = _commit_or_discard(
        lambda: _commit_edit(f, scope_used, clip_id, out_path, src_path, {"ai_metadata": _hf_metadata(new, duration)},
                             replace=True),
        out_path)
    return _edit_receipt(scope, scope_used, clip_id, out_path, new_id, f.id, clip_ids, _hf_shown(new),
                         dict(style=new["style"], fields=_hf_fields(new)))


def _edit_svg(f, clip_id, scope, scope_used, src_path, taken, families, style, text, subtitle, lines, has_text):
    if not os.path.isfile(src_path):
        raise ToolError(f"the title file is missing on disk: {src_path}")
    doc = _load_doc(src_path)
    slots = title_svg.fields(doc)
    name = str(f.data.get("name") or os.path.basename(src_path))
    if has_text and not slots:
        raise ToolError(f"{name} has no text to change")
    values = _assign_texts(slots, text, subtitle, lines, name) if has_text else {}
    style.resolve_font(families)

    before_xml = title_svg.to_xml(doc)
    for slot in slots:
        if slot.index in values:
            title_svg.set_field_text(doc, slot, values[slot.index])
    style_info = style.apply(doc, families)
    fitted = _fit(doc, slots)
    after = _field_rows(slots)
    if scope_used != "duplicate" and title_svg.to_xml(doc) == before_xml:
        raise ToolError("the title already looks like that; nothing changed")

    folder = title_dir()
    if scope_used == "all_clips":
        out_path = _version_path(folder, src_path, ".svg", taken)
    else:
        pattern, offset = title_svg.duplicate_pattern(os.path.basename(src_path))
        free = title_svg.free_name(pattern, offset, folder, taken=taken)
        if not free:
            raise ToolError("no free name for the copy")
        out_path = os.path.join(folder, free + ".svg")
    title_svg.write(doc, out_path)
    new_id, clip_ids = _commit_or_discard(lambda: _commit_edit(f, scope_used, clip_id, out_path, src_path),
                                          out_path)
    shown = " / ".join(r["text"] for r in after if r["text"]) or name
    extra = dict(fields=after, **style_info)
    if fitted:
        extra["text_shrunk_to_fit"] = fitted
    return _edit_receipt(scope, scope_used, clip_id, out_path, new_id, f.id, clip_ids, shown, extra)


def _edit_receipt(scope, scope_used, clip_id, out_path, new_id, source_id, clip_ids, shown, extra):
    if scope_used == "all_clips":
        summary = f"Updated title {shown!r} on {len(clip_ids)} clip(s)."
    elif scope_used == "this_clip":
        summary = f"Gave clip {clip_id} its own edited title {shown!r}."
    else:
        summary = f"Duplicated the title as {os.path.basename(out_path)!r} ({shown!r}); it is in Project Files."
    receipt = dict(scope=scope_used, file_id=new_id, source_file_id=source_id, path=out_path,
                   timeline_clip_ids=clip_ids, **extra)
    if scope != scope_used:
        receipt["note"] = "the title is used by only this clip, so it was edited in place"
    return ok(summary, **receipt)


# Register the sibling tool modules (they import helpers from here).
from classes.editor_tools import (  # noqa: E402,F401
    titles_text_animated,
    titles_text_captions,
    titles_text_overlays,
)
