"""Captions: libopenshot's Caption effect, as the Captions dock uses it (titles-text workstream).

A caption track is a Caption effect on the clip it captions; its cues are in
the clip's source time (see ``classes.caption_cues``), so they stay on the
spoken words when the clip is moved or trimmed, and the Captions dock edits
them. Captions not tied to one clip (an SRT for the finished edit, cues given
in timeline seconds without a clip) go on a transparent "Captions" overlay clip
that spans the cues.
"""

from __future__ import annotations

import copy
import os
from typing import List, Optional, Tuple

from classes import caption_cues
from classes.caption_cues import Cue
from classes.editor_tools._base import (
    ToolError, array, boolean, clip_extent, enum, get_app, integer, is_locked, keyframe_value_at,
    number, obj, ok, resolve_clip, string, th, ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.titles_text_common import (
    commit_on_main, precheck_on_main,
    color_hex_alpha, color_keyframes, constant, installed_font_families,
    fresh_effect, is_vertical, new_effect_json, overlay_file, overlay_file_path, create_track, place_clip,
    plan_overlay_track, project_size, resolve_font, track_info,
)

MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
CAPTION_ROLE = "captions"

# Styles: sizes are fractions of the frame's shorter side (px = size * min(w, h)),
# stroke / padding / corner fractions of the text size.
CAPTION_STYLES = {
    "classic": {"text_size": 0.05, "text_color": "#ffffff", "stroke_color": "#000000", "stroke_width": 0.0,
                "background_color": "#000000", "background_opacity": 0.6, "position": "bottom",
                "font": "", "padding": 0.35, "corner": 0.15, "fade": 0.0},
    "reels": {"text_size": 0.075, "text_color": "#ffffff", "stroke_color": "#000000", "stroke_width": 0.09,
              "background_color": "#000000", "background_opacity": 0.0, "position": "bottom",
              "font": "Arial Black", "padding": 0.3, "corner": 0.15, "fade": 0.0},
    "minimal": {"text_size": 0.045, "text_color": "#ffffff", "stroke_color": "#202020", "stroke_width": 0.035,
                "background_color": "#000000", "background_opacity": 0.0, "position": "bottom",
                "font": "", "padding": 0.3, "corner": 0.15, "fade": 0.1},
    "yellow": {"text_size": 0.055, "text_color": "#ffe14d", "stroke_color": "#000000", "stroke_width": 0.07,
               "background_color": "#000000", "background_opacity": 0.0, "position": "bottom",
               "font": "Arial Black", "padding": 0.3, "corner": 0.15, "fade": 0.0},
    "boxed": {"text_size": 0.05, "text_color": "#ffffff", "stroke_color": "#000000", "stroke_width": 0.0,
              "background_color": "#000000", "background_opacity": 0.85, "position": "bottom",
              "font": "", "padding": 0.45, "corner": 0.05, "fade": 0.0},
}
POSITIONS = ("bottom", "center", "top")
# libopenshot fills then strokes each glyph, so an outline eats half its width into the letters:
# thick outlines only read on heavy fonts. Without one, preset outlines are thinned to this.
HEAVY_FONT_HINTS = ("black", "heavy", "impact", "extrabold", "ultra")
THIN_OUTLINE = 0.035


def is_heavy_font(font: str) -> bool:
    return any(h in str(font or "").lower() for h in HEAVY_FONT_HINTS)


# ---------------------------------------------------------------------------
# Style -> Caption effect properties
# ---------------------------------------------------------------------------

class CaptionStyle:
    """A resolved caption look, in frame-relative units, convertible to Caption properties."""

    def __init__(self, preset: str = "classic", text_size: float = 0.0, font: str = "", text_color: str = "",
                 stroke_color: str = "", stroke_width: float = -1.0, background_color: str = "",
                 background_opacity: float = -1.0, position: str = "", fade_seconds: float = -1.0,
                 base: Optional[dict] = None):
        if preset and preset not in CAPTION_STYLES:
            raise ToolError(f"style must be one of {sorted(CAPTION_STYLES)}")
        values = dict(base or CAPTION_STYLES[preset or "classic"])
        if preset and base is not None:
            values.update(CAPTION_STYLES[preset])
            values["text_alpha"] = 1.0
        if text_size:
            values["text_size"] = float(text_size)
        if font:
            values["font"] = str(font).strip()
        if str(text_color or "").strip():
            values["text_color"], values["text_alpha"] = color_hex_alpha(text_color, "text_color")
        if str(stroke_color or "").strip():
            values["stroke_color"] = color_hex_alpha(stroke_color, "stroke_color")[0]
        self.explicit_stroke = float(stroke_width) >= 0
        if self.explicit_stroke:
            values["stroke_width"] = float(stroke_width)
        if str(background_color or "").strip():
            values["background_color"], bg_alpha = color_hex_alpha(background_color, "background_color")
            if bg_alpha < 1.0:
                values["background_opacity"] = bg_alpha          # '#RRGGBBAA' says how opaque the box is
            elif float(values.get("background_opacity") or 0.0) <= 0.0:
                values["background_opacity"] = 0.6               # naming a box colour asks for a visible box
        if float(background_opacity) >= 0:
            values["background_opacity"] = float(background_opacity)
        self.text_alpha = float(values.get("text_alpha", 1.0))
        if position:
            if position not in POSITIONS:
                raise ToolError(f"position must be one of {list(POSITIONS)}")
            values["position"] = position
        if float(fade_seconds) >= 0:
            values["fade"] = float(fade_seconds)
        self.values = values

    def resolve_font(self, families):
        """Match the style's font against the installed fonts (None = unknown, accept as given)."""
        if self.values.get("font"):
            try:
                self.values["font"] = resolve_font(self.values["font"], families)
            except ToolError:
                if self.values["font"] in {v["font"] for v in CAPTION_STYLES.values()}:
                    self.values["font"] = ""   # a preset's font is a nicety, not a requirement
                else:
                    raise
        if (not self.explicit_stroke and not is_heavy_font(self.values.get("font"))
                and float(self.values.get("stroke_width") or 0.0) > THIN_OUTLINE):
            self.values["stroke_width"] = THIN_OUTLINE   # keep the letters' fill visible

    def properties(self, lines: int = 2) -> dict:
        v = self.values
        w, h = project_size()
        short = float(min(w, h))
        font_px = max(4.0, float(v["text_size"]) * short)
        to_units = 600.0 / float(w)            # Caption scales every size by width / 600
        line_spacing = float(v.get("line_spacing", 1.0))
        top = caption_top(v["position"], font_px, line_spacing, h, lines, is_vertical())
        margin = 0.06 if is_vertical() else 0.08
        props = {
            "caption_font": v.get("font") or "sans",
            "font_size": constant(round(font_px * to_units, 3)),
            "font_alpha": constant(round(self.text_alpha, 4)),
            "color": color_keyframes(v["text_color"]),
            "stroke": color_keyframes(v["stroke_color"]),
            "stroke_width": constant(round(float(v["stroke_width"]) * font_px * to_units, 3)),
            "background": color_keyframes(v["background_color"]),
            "background_alpha": constant(round(float(v["background_opacity"]), 4)),
            "background_padding": constant(round(float(v.get("padding", 0.35)) * font_px * to_units, 3)),
            "background_corner": constant(round(float(v.get("corner", 0.15)) * font_px * to_units, 3)),
            "left": constant(margin),
            "right": constant(margin),
            "top": constant(round(top, 4)),
            "bottom": constant(0.0),
            "line_spacing": constant(line_spacing),
            "fade_in": constant(float(v.get("fade", 0.0))),
            "fade_out": constant(float(v.get("fade", 0.0))),
            "apply_before_clip": False,
        }
        return props

    def summary(self) -> dict:
        v = self.values
        return {"text_size": v["text_size"], "font": v.get("font") or "sans", "text_color": v["text_color"],
                "stroke_color": v["stroke_color"], "stroke_width": v["stroke_width"],
                "background_color": v["background_color"], "background_opacity": v["background_opacity"],
                "position": v["position"], "fade_seconds": v.get("fade", 0.0)}


def caption_top(position: str, font_px: float, line_spacing: float, height: int, lines: int,
                vertical: bool) -> float:
    """Caption 'top' margin that puts a block of *lines* at the bottom / centre / top safe area.

    libopenshot draws the first baseline one line-spacing below ``top`` and
    wraps downward; line spacing is about 1.2 em for sans fonts.
    """
    ls = 1.2 * font_px
    h = float(height)
    if position == "top":
        safe = 0.12 if vertical else 0.06
        top = safe - 0.45 * font_px / h
    elif position == "center":
        top = 0.5 - (ls - 0.25 * font_px) / h
    else:
        safe = 0.20 if vertical else 0.08
        block = ls + max(0, lines - 1) * ls * line_spacing + 0.3 * font_px
        top = (1.0 - safe) - block / h
    return max(0.0, min(0.95, top))


def style_from_effect(effect: dict) -> dict:
    """Frame-relative style values read back from a Caption effect (for restyling)."""
    w, h = project_size()
    short = float(min(w, h))
    units = float(w) / 600.0

    def kf(name, default):
        return keyframe_value_at(effect.get(name), 1, default)

    def color(name, default):
        c = effect.get(name)
        if not isinstance(c, dict):
            return default
        try:
            r, g, b = (int(round(keyframe_value_at(c.get(k), 1, 0))) for k in ("red", "green", "blue"))
            return "#%02x%02x%02x" % (r, g, b)
        except (TypeError, ValueError):
            return default

    font_px = kf("font_size", 30.0) * units
    top = kf("top", 0.75)
    position = "top" if top < 0.3 else ("center" if top < 0.6 else "bottom")
    return {
        "text_size": round(font_px / short, 4) if short else 0.05,
        "font": "" if effect.get("caption_font") in (None, "", "sans") else effect.get("caption_font"),
        "text_color": color("color", "#ffffff"),
        "text_alpha": kf("font_alpha", 1.0),
        "stroke_color": color("stroke", "#000000"),
        "stroke_width": round(kf("stroke_width", 0.0) * units / font_px, 4) if font_px else 0.0,
        "background_color": color("background", "#000000"),
        "background_opacity": kf("background_alpha", 0.0),
        "position": position,
        "padding": round(kf("background_padding", 20.0) * units / font_px, 4) if font_px else 0.35,
        "corner": round(kf("background_corner", 10.0) * units / font_px, 4) if font_px else 0.15,
        "fade": kf("fade_in", 0.0),
        "line_spacing": kf("line_spacing", 1.0),
    }


# ---------------------------------------------------------------------------
# Targets and time bases
# ---------------------------------------------------------------------------

def _clip_window(data: dict) -> Tuple[float, float, float]:
    """(position, source start, source end) of a clip."""
    return float(data.get("position") or 0.0), float(data.get("start") or 0.0), float(data.get("end") or 0.0)


def timeline_to_caption(data: dict, t: float) -> float:
    position, start, _ = _clip_window(data)
    return float(t) - position + start


def caption_to_timeline(data: dict, t: float) -> float:
    position, start, _ = _clip_window(data)
    return position + float(t) - start


def _has_time_curve(data: dict) -> bool:
    pts = ((data.get("time") or {}).get("Points") if isinstance(data.get("time"), dict) else None) or []
    return len(pts) > 1


def caption_effects(data: dict) -> list:
    return [e for e in (data.get("effects") or []) if isinstance(e, dict)
            and (e.get("class_name") == "Caption" or e.get("type") == "Caption")]


def _transcript_cues(clip_id: str, data: dict) -> List[Cue]:
    """Transcript cues (Zenvi indexing, file ai_metadata) for a clip, in its source seconds."""
    raw = th()._clip_transcript_cues(clip_id) or []
    start = float(data.get("start") or 0.0)
    cues = []
    for c in raw:
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        if c.get("source_start") is not None:
            s = float(c.get("source_start"))
            e = float(c.get("source_end") if c.get("source_end") is not None else s)
        else:
            s = float(c.get("start") or 0.0) + start
            e = float(c.get("end") or 0.0) + start
        if e > s:
            cues.append(Cue(s, e, text))
    return cues


def _clip_label(c) -> str:
    return str(c.data.get("title") or c.id)


def _resolve_targets(clip_id: str, clip_query: str, track_index: int, need_clip: bool) -> list:
    """Clips to caption (QueryObjects). GUI thread."""
    from classes.query import Clip
    from classes.track_display import display_index_to_layer_number
    if clip_id:
        c = Clip.get(id=clip_id)
        if not c:
            raise ToolError(f"no timeline clip with id={clip_id!r}")
        return [c]
    if clip_query:
        return [resolve_clip(clip_query=clip_query)]
    if track_index:
        from classes.editor_tools._base import layers
        try:
            layer = display_index_to_layer_number(int(track_index), layers())
        except Exception:
            layer = None
        if not layer:
            raise ToolError(f"there is no track {track_index} (UI track 1 = bottom)")
        clips = [c for c in Clip.filter(layer=layer) if int(c.data.get("layer") or 0) == int(layer)]
        if not clips:
            raise ToolError(f"track {track_index} has no clips")
        return sorted(clips, key=lambda c: float(c.data.get("position") or 0.0))
    if need_clip:
        return sorted(Clip.filter(), key=lambda c: (float(c.data.get("position") or 0.0),
                                                    int(c.data.get("layer") or 0)))
    return []


def _find_effect(effect_id: str, clip_id: str, clip_query: str):
    """(clip QueryObject, Caption effect dict) to edit. GUI thread."""
    from classes.query import Clip
    candidates = []
    for c in Clip.filter():
        for e in caption_effects(c.data):
            candidates.append((c, e))
    if effect_id:
        for c, e in candidates:
            if e.get("id") == effect_id:
                return c, e
        raise ToolError(f"no Caption effect with id={effect_id!r}")
    if clip_id or clip_query:
        c = Clip.get(id=clip_id) if clip_id else resolve_clip(clip_query=clip_query)
        if not c:
            raise ToolError(f"no timeline clip with id={clip_id!r}")
        effects = caption_effects(c.data)
        if not effects:
            raise ToolError(f"clip {c.id} ({_clip_label(c)}) has no captions; add them with add_captions_tool")
        if len(effects) > 1:
            raise ToolError(f"clip {c.id} has {len(effects)} Caption effects; pass effect_id "
                            f"({', '.join(e.get('id') for e in effects)})")
        return c, effects[0]
    try:
        selected = set(get_app().window.selected_effects or [])
    except Exception:
        selected = set()
    chosen = [(c, e) for c, e in candidates if e.get("id") in selected]
    if len(chosen) == 1:
        return chosen[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ToolError("there are no captions in the project; add them with add_captions_tool")
    raise ToolError("which captions? pass effect_id or timeline_clip_id: "
                    + ", ".join(f"{e.get('id')} on {c.id} ({_clip_label(c)})" for c, e in candidates[:6]))


def _cue_rows(data: dict, cues: List[Cue]) -> list:
    _, start, end = _clip_window(data)
    rows = []
    for i, cue in enumerate(sorted(cues, key=lambda c: (c.start, c.end)), start=1):
        rows.append({
            "index": i,
            "start": round(caption_to_timeline(data, cue.start), 3),
            "end": round(caption_to_timeline(data, cue.end), 3),
            "source_start": round(cue.start, 3),
            "source_end": round(cue.end, 3),
            "text": caption_cues.display_text(cue.text),
            "visible": cue.end > start and cue.start < end,
        })
    return rows


def _save_effects(clip_id: str, effects: list) -> None:
    get_app().updates.update(["clips", {"id": clip_id}], {"effects": effects})


def _refresh() -> None:
    try:
        get_app().window.refreshFrameSignal.emit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# add_captions_tool
# ---------------------------------------------------------------------------

_STYLE_ARGS = {
    "style": enum(sorted(CAPTION_STYLES),
                  "Look preset: classic = white text on a dark box at the bottom (subtitles); reels = big bold "
                  "white text with a black outline (Instagram/TikTok/Shorts); minimal = small white text with a "
                  "thin outline; yellow = yellow text with a black outline (TV subtitles); boxed = white on a "
                  "solid black box. The arguments below override parts of it.", "classic"),
    "font": string("Font family, e.g. 'Arial Black', 'Helvetica Neue'. Empty = the style's font.", ""),
    "text_size": number("Text height as a fraction of the frame's shorter side: 0.04 small, 0.05 normal "
                        "subtitles, 0.075 big reel captions. 0 = the style's size.", 0.0, minimum=0, maximum=0.3),
    "text_color": string("Text colour '#RRGGBB' / '#RRGGBBAA' / 'rgba(...)' / name. Empty = the style's.", ""),
    "stroke_color": string("Outline colour. Empty = the style's.", ""),
    "stroke_width": number("Outline thickness relative to the text height: 0 = none, 0.03 thin, 0.09 heavy "
                           "(heavy outlines only read with heavy fonts such as Arial Black). -1 = the style's.",
                           -1.0, minimum=-1, maximum=0.5),
    "background_color": string("Colour of the box behind the text (alpha in '#RRGGBBAA' sets its opacity). "
                               "Empty = the style's.", ""),
    "background_opacity": number("Opacity of the box behind the text, 0 (no box) to 1. -1 = the style's.",
                                 -1.0, minimum=-1, maximum=1),
    "position": enum(["", "bottom", "center", "top"],
                     "Where the captions sit: bottom (above the safe margin, higher on vertical video), center "
                     "or top. Empty = the style's (bottom).", ""),
    "fade_seconds": number("Fade each caption in and out over this many seconds (0-3). -1 = the style's.",
                           -1.0, minimum=-1, maximum=3),
}


def _style_from_args(style, font, text_size, text_color, stroke_color, stroke_width, background_color,
                     background_opacity, position, fade_seconds, base=None) -> CaptionStyle:
    return CaptionStyle(style, text_size, font, text_color, stroke_color, stroke_width, background_color,
                        background_opacity, position, fade_seconds, base=base)


def _prepare(cues: List[Cue], uppercase: bool) -> Tuple[List[Cue], int]:
    out, changed = [], 0
    for cue in cues:
        text = cue.text.upper() if uppercase else cue.text
        safe, was_changed = caption_cues.safe_text(text)
        if not safe:
            continue
        changed += int(was_changed)
        out.append(Cue(cue.start, cue.end, safe))
    return out, changed


def _read_subtitle_file(path: str) -> List[Cue]:
    path = os.path.abspath(os.path.expanduser(str(path).strip()))
    if not os.path.isfile(path):
        raise ToolError(f"subtitle file not found: {path}")
    if os.path.getsize(path) > MAX_SUBTITLE_BYTES:
        raise ToolError("subtitle file is larger than 5 MB")
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    cues = caption_cues.parse_subtitles(text)
    if not cues:
        raise ToolError(f"no cues found in {os.path.basename(path)} (expected SRT or WebVTT timings "
                        "like 00:00:01,000 --> 00:00:03,000)")
    return cues


def _given_cues(cues) -> List[Cue]:
    out = []
    for i, c in enumerate(cues or [], start=1):
        if not isinstance(c, dict):
            raise ToolError(f"cues[{i}] must be an object with start, end and text")
        text = str(c.get("text") or "").strip()
        try:
            start, end = float(c.get("start")), float(c.get("end"))
        except (TypeError, ValueError):
            raise ToolError(f"cues[{i}] needs numeric start and end seconds") from None
        if not text:
            raise ToolError(f"cues[{i}] has no text")
        if start < 0 or end <= start:
            raise ToolError(f"cues[{i}] must have 0 <= start < end (got {start}-{end})")
        out.append(Cue(start, end, text))
    return out


CUE_ITEM = {"type": "object", "properties": {
    "start": {"type": "number", "description": "Start second."},
    "end": {"type": "number", "description": "End second (after start)."},
    "text": {"type": "string", "description": "Caption text; '\\n' for a second line."},
}, "required": ["start", "end", "text"], "additionalProperties": False}


@editor_tool(
    "add_captions_tool",
    label="Add captions",
    schema=obj({
        "cues": array(CUE_ITEM, "Captions to show, each {start, end, text} in timeline seconds (or the clip's "
                      "source seconds with time_base='source'). Use for text you already have."),
        "srtPath": string("Path of an .srt or .vtt subtitle file to burn in. With a clip its times are that "
                          "clip's media time; without one, timeline time.", ""),
        "clipId": string("Timeline clip to caption (the speaker's clip). Same as timeline_clip_id.", ""),
        "timeline_clip_id": string("Timeline clip to caption.", ""),
        "clip_query": string("Describe the clip to caption instead of an id, e.g. 'talking head'.", ""),
        "trackIndex": integer("Caption every clip on this UI track (1 = bottom) from its transcript. 0 = not "
                              "used.", 0, minimum=0),
        "time_base": enum(["auto", "timeline", "source"],
                          "What cue times mean: timeline seconds, or the clip's source (media) seconds. auto = "
                          "source for subtitle files and transcripts, timeline for cues.", "auto"),
        "overlay_track": string("Track for the transparent 'Captions' overlay clip used when no clip is "
                                "captioned. Empty = the lowest free track above the video.", ""),
        **_STYLE_ARGS,
        "uppercase": boolean("Show the captions in capital letters.", False),
        "maxWords": integer("Longest caption, in words, when phrasing a transcript (subtitle files and cues "
                            "are kept as written).", 8, minimum=1, maximum=24),
        "maxChars": integer("Longest caption, in characters, when phrasing a transcript.", 42,
                            minimum=8, maximum=120),
        "replace_existing": boolean("Replace the captions a clip already has (true) or add a second caption "
                                    "track to it (false).", True),
        "language": string("Accepted for compatibility with the on-device speech tools; the transcript comes "
                           "from Zenvi's media index, which already knows the language.", "auto"),
        "modelId": string("Accepted for compatibility; not used (the transcript comes from the media index).", ""),
        "engine": string("Accepted for compatibility; not used (the transcript comes from the media index).",
                         "auto"),
    }),
    background_safe=True,
    covers=("caption.add",),
)
def add_captions(cues=None, srtPath="", clipId="", timeline_clip_id="", clip_query="", trackIndex=0,
                 time_base="auto", overlay_track="", style="classic", font="", text_size=0.0, text_color="",
                 stroke_color="", stroke_width=-1.0, background_color="", background_opacity=-1.0, position="",
                 fade_seconds=-1.0, uppercase=False, maxWords=8, maxChars=42, replace_existing=True,
                 language="auto", modelId="", engine="auto"):
    """Burn captions / subtitles into the video: from the speech transcript, a subtitle file, or given text.

    Use for "add captions from what I say", "caption the talking head", "burn in these
    subtitles (captions.srt)", "captions like Instagram reels". Three sources:
    cues=[{start, end, text}] (timeline seconds); srtPath=an .srt/.vtt file; or neither,
    to caption from the transcript Zenvi's media index stored for the clip's file
    (clips that are not indexed yet are refused -- index them first). Captions go on
    the captioned clip as a Caption effect (cue times follow the clip's media, so
    trimming keeps them on the words; the Captions dock can edit them). With cues or a
    subtitle file and no clip, they go on a transparent 'Captions' overlay clip above
    the video. A clip's existing captions are replaced unless replace_existing=false.
    One undo step. Not for titles or other free text (add_title_tool).

    Examples: {"timeline_clip_id": "C1", "style": "reels"};
    {"srtPath": "/path/captions.srt", "timeline_clip_id": "C1", "style": "classic"};
    {"cues": [{"start": 1, "end": 3.5, "text": "Welcome back!"}], "style": "yellow", "position": "top"}.
    """
    clip_id = str(clipId or timeline_clip_id or "").strip()
    if clipId and timeline_clip_id and clipId != timeline_clip_id:
        raise ToolError("clipId and timeline_clip_id name different clips; pass one")
    given = _given_cues(cues)
    if given and str(srtPath or "").strip():
        raise ToolError("pass either cues or srtPath, not both")
    source = "cues" if given else ("file" if str(srtPath or "").strip() else "transcript")
    if source != "transcript" and trackIndex:
        raise ToolError("trackIndex picks clips to caption from their transcript; with cues or srtPath pass "
                        "timeline_clip_id (or nothing, for an overlay over the timeline)")
    file_cues = _read_subtitle_file(srtPath) if source == "file" else []
    cap_style = _style_from_args(style, font, text_size, text_color, stroke_color, stroke_width,
                                 background_color, background_opacity, position, fade_seconds)
    query = str(clip_query or "").strip()
    overlay_mode = not (clip_id or query or trackIndex) and source != "transcript"
    if overlay_mode and time_base == "source":
        raise ToolError("time_base='source' needs the clip whose media the times refer to")

    def _precheck():
        found = _resolve_targets(clip_id, query, int(trackIndex or 0), source == "transcript")
        if overlay_mode:
            raw = file_cues if source == "file" else given
            plan_overlay_track(min(q.start for q in raw), max(q.end for q in raw), overlay_track)
        # libopenshot objects are made on the GUI thread (Qt font state is not safe from a
        # Python thread); refuses here, before any change, when libopenshot has no Caption.
        return found, installed_font_families(), new_effect_json("Caption")

    targets, families, caption_template = precheck_on_main(_precheck)
    cap_style.resolve_font(families)
    warnings: List[str] = []
    plans = []   # (clip QueryObject, [Cue in caption time])

    if source == "transcript":
        if time_base == "timeline":
            raise ToolError("transcript captions are always in the clip's media time; drop time_base")
        for c in targets:
            cues_c = _transcript_cues(c.id, c.data)
            if not cues_c:
                continue
            phrased = []
            for cue in cues_c:
                phrased.extend(caption_cues.split_cue(cue, maxWords, maxChars))
            plans.append((c, phrased))
        if not plans:
            names = ", ".join(f"{c.id} ({_clip_label(c)})" for c in targets[:5]) or "the timeline has no clips"
            raise ToolError("no transcript is available to caption from (" + names + "). Zenvi fills it "
                            "when it indexes a file; check with get_clips_with_full_metadata_tool, index with "
                            "reindex_project_file_tool, or pass cues / srtPath instead")
    elif targets:
        c = targets[0]
        base = time_base if time_base != "auto" else ("source" if source == "file" else "timeline")
        raw = file_cues if source == "file" else given
        mapped = [Cue(timeline_to_caption(c.data, q.start), timeline_to_caption(c.data, q.end), q.text)
                  if base == "timeline" else Cue(q.start, q.end, q.text) for q in raw]
        plans.append((c, mapped))
    else:
        raw = file_cues if source == "file" else given
        plans.append((None, raw))

    # Keep what a clip can show; refuse locked clips before touching anything.
    final = []
    for c, cues_c in plans:
        if c is None:
            final.append((c, cues_c))
            continue
        if is_locked(int(c.data.get("layer") or 0)):
            raise ToolError(f"clip {c.id} is on locked track {ui_track_number(int(c.data.get('layer') or 0))}; "
                            "unlock it first")
        _, start, end = _clip_window(c.data)
        inside = [q for q in cues_c if q.end > start and q.start < end]
        dropped = len(cues_c) - len(inside)
        if dropped:
            warnings.append(f"{dropped} cue(s) fall outside the visible part of clip {c.id} and were skipped")
        if not inside:
            raise ToolError(f"none of the {len(cues_c)} cue(s) fall inside clip {c.id}'s visible range "
                            f"({caption_to_timeline(c.data, start):.2f}-{caption_to_timeline(c.data, end):.2f}s "
                            "on the timeline); check time_base")
        if _has_time_curve(c.data):
            warnings.append(f"clip {c.id} is retimed; caption times assume normal speed")
        final.append((c, inside))

    prepared = []
    safe_changed = 0
    for c, cues_c in final:
        ready, changed = _prepare(sorted(cues_c, key=lambda q: (q.start, q.end)), uppercase)
        safe_changed += changed
        if not ready:
            raise ToolError("the captions have no text left to show")
        prepared.append((c, ready))
    if safe_changed:
        warnings.append(f"{safe_changed} caption(s) were adjusted so libopenshot draws them fully "
                        "(':' between digits and one-letter lines)")

    overlay = None
    if prepared[0][0] is None:
        ready = prepared[0][1]
        span_start = min(q.start for q in ready)
        span_end = max(q.end for q in ready)
        w, h = project_size()
        overlay = (overlay_file_path(w, h), span_start, span_end)

    props = cap_style.properties(lines=2)

    def _commit():
        from classes.query import Clip
        results = []
        for c, ready in prepared:
            if c is None:
                path, span_start, span_end = overlay
                layer, created = plan_overlay_track(span_start, span_end, overlay_track)
                f = overlay_file(path)
                if created:
                    create_track(layer, "Captions")
                clip = place_clip(f.id, span_start, span_end - span_start, layer, title="Captions",
                                  props={"zenvi_role": CAPTION_ROLE})
                target_id = clip["id"]
                shifted = [Cue(q.start - span_start, q.end - span_start, q.text) for q in ready]
                data = Clip.get(id=target_id).data
                entry = {"timeline_clip_id": target_id, "overlay": True, "new_track": created,
                         **track_info(layer)}
            else:
                target_id = c.id
                shifted = ready
                data = Clip.get(id=target_id).data
                entry = {"timeline_clip_id": target_id, "overlay": False,
                         "track": ui_track_number(int(data.get("layer") or 0))}
            effects = copy.deepcopy(list(data.get("effects") or []))
            existing = caption_effects({"effects": effects})
            if existing and replace_existing:
                effect = existing[0]
                effect.update(copy.deepcopy(props))
                effect["caption_text"] = caption_cues.build_caption_text(shifted)
                entry["replaced"] = True
            else:
                effect = fresh_effect(caption_template)
                effect.update(copy.deepcopy(props))
                effect["caption_text"] = caption_cues.build_caption_text(shifted)
                effects.append(effect)
                entry["replaced"] = False
            _save_effects(target_id, effects)
            entry.update(effect_id=effect.get("id"), cues=len(shifted),
                         first=round(caption_to_timeline(Clip.get(id=target_id).data, shifted[0].start), 3),
                         last=round(caption_to_timeline(Clip.get(id=target_id).data, shifted[-1].end), 3))
            results.append(entry)
        _refresh()
        return results

    results = commit_on_main(_commit)
    total = sum(r["cues"] for r in results)
    where = ("a Captions overlay on track %s" % results[0].get("track") if results[0]["overlay"]
             else ", ".join(r["timeline_clip_id"] for r in results))
    summary = f"Added {total} caption(s) from {'the transcript' if source == 'transcript' else source} on {where}."
    receipt = dict(source=source, captioned=results, style=style, look=cap_style.summary())
    if warnings:
        receipt["warnings"] = warnings
    return ok(summary, **receipt)


# ---------------------------------------------------------------------------
# list_captions_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "list_captions_tool",
    label="List captions",
    schema=obj({
        "timeline_clip_id": string("Only this clip's captions. Empty = every caption track in the project.", ""),
    }),
    read_only=True,
    covers=("caption.edit",),
)
def list_captions(timeline_clip_id=""):
    """List every caption track (Caption effect) with its numbered cues and look.

    Use before edit_captions_tool (cue numbers, current text, timeline times) or to
    answer "what do the captions say". Cue start/end are timeline seconds (source_start
    / source_end are the clip's media time the effect stores); visible=false marks cues
    outside the clip's trimmed range. Read-only.
    """
    from classes.query import Clip
    wanted = str(timeline_clip_id or "").strip()
    tracks = []
    for c in Clip.filter():
        if wanted and c.id != wanted:
            continue
        for e in caption_effects(c.data):
            cues = caption_cues.parse_caption_text(e.get("caption_text") or "")
            tracks.append({
                "timeline_clip_id": c.id,
                "clip_title": _clip_label(c),
                "effect_id": e.get("id"),
                "overlay": c.data.get("zenvi_role") == CAPTION_ROLE,
                "track": ui_track_number(int(c.data.get("layer") or 0)),
                "clip_start": round(clip_extent(c.data)[0], 3),
                "clip_end": round(clip_extent(c.data)[1], 3),
                "cues": _cue_rows(c.data, cues),
                "look": {k: v for k, v in style_from_effect(e).items()
                         if k in ("text_size", "font", "text_color", "stroke_color", "stroke_width",
                                  "background_color", "background_opacity", "position", "fade")},
            })
    if wanted and not tracks:
        c = Clip.get(id=wanted)
        if not c:
            raise ToolError(f"no timeline clip with id={wanted!r}")
    total = sum(len(t["cues"]) for t in tracks)
    return ok(f"{len(tracks)} caption track(s), {total} cue(s).", caption_tracks=tracks)


# ---------------------------------------------------------------------------
# edit_captions_tool
# ---------------------------------------------------------------------------

EDIT_ITEM = {"type": "object", "properties": {
    "index": {"type": "integer", "minimum": 1, "description": "Cue number from list_captions_tool (1 = first)."},
    "text": {"type": "string", "description": "New text for the cue."},
    "start": {"type": "number", "description": "New start, timeline seconds."},
    "end": {"type": "number", "description": "New end, timeline seconds."},
    "delete": {"type": "boolean", "description": "Remove this cue."},
}, "required": ["index"], "additionalProperties": False}


@editor_tool(
    "edit_captions_tool",
    label="Edit captions",
    schema=obj({
        "effect_id": string("Caption effect id from list_captions_tool.", ""),
        "timeline_clip_id": string("The captioned clip (when it has one caption track).", ""),
        "clip_query": string("Describe the captioned clip instead of an id.", ""),
        "cue_edits": array(EDIT_ITEM, "Changes to numbered cues: new text, new start/end (timeline seconds) or "
                           "delete=true. Numbers are the order before this call."),
        "add_cues": array(CUE_ITEM, "New cues {start, end, text} in timeline seconds (like the Captions dock's "
                          "Insert Caption)."),
        "find": string("Replace this text in every cue (e.g. a misspelled name)...", ""),
        "replace": string("...with this text.", ""),
        "shift_seconds": number("Move every cue later (positive) or earlier (negative) to fix sync.", 0.0,
                                minimum=-3600, maximum=3600),
        "style": enum(["", *sorted(CAPTION_STYLES)], "Switch to a look preset (then apply the overrides below). "
                      "Empty keeps the current look.", ""),
        **{k: v for k, v in _STYLE_ARGS.items() if k != "style"},
        "uppercase": boolean("Turn every cue's text into capital letters.", False),
    }),
    covers=("caption.edit",),
)
def edit_captions(effect_id="", timeline_clip_id="", clip_query="", cue_edits=None, add_cues=None, find="",
                  replace="", shift_seconds=0.0, style="", font="", text_size=0.0, text_color="",
                  stroke_color="", stroke_width=-1.0, background_color="", background_opacity=-1.0, position="",
                  fade_seconds=-1.0, uppercase=False):
    """Change existing captions: fix a cue's words or timing, add or delete cues, shift sync, restyle.

    Use after add_captions_tool or for captions made in the Captions dock ("fix the
    typo in caption 3", "the captions are half a second late", "make the captions
    yellow and bigger", "add a caption 'Thanks!' at 12 s for 2 s", "delete the last
    caption"). Get cue numbers from list_captions_tool. Times are timeline seconds.
    With no target the selected Caption effect, or the project's only caption track,
    is used. One undo step. To remove captions entirely use remove_captions_tool.

    Example: {"timeline_clip_id": "C1", "cue_edits": [{"index": 2, "text": "Today we explore Tokyo."}],
    "shift_seconds": -0.4, "style": "reels"}.
    """
    edits = list(cue_edits or [])
    extra = _given_cues(add_cues)
    restyle = any([style, font, text_size, str(text_color).strip(), str(stroke_color).strip(),
                   stroke_width is not None and float(stroke_width) >= 0, str(background_color).strip(),
                   background_opacity is not None and float(background_opacity) >= 0, position,
                   fade_seconds is not None and float(fade_seconds) >= 0])
    if replace and not find:
        raise ToolError("replace needs find (the text to replace)")
    if not (edits or extra or find or shift_seconds or restyle or uppercase):
        raise ToolError("nothing to change: pass cue_edits, add_cues, find/replace, shift_seconds, uppercase "
                        "or style fields")

    c, effect = _find_effect(str(effect_id or "").strip(), str(timeline_clip_id or "").strip(),
                             str(clip_query or "").strip())
    if is_locked(int(c.data.get("layer") or 0)):
        raise ToolError(f"clip {c.id} is on a locked track; unlock it first")
    data = c.data
    cues = sorted(caption_cues.parse_caption_text(effect.get("caption_text") or ""), key=lambda q: (q.start, q.end))
    count = len(cues)
    changes = []

    delete = set()
    for i, ed in enumerate(edits, start=1):
        if not isinstance(ed, dict):
            raise ToolError(f"cue_edits[{i}] must be an object")
        idx = int(ed.get("index") or 0)
        if idx < 1 or idx > count:
            raise ToolError(f"cue_edits[{i}]: there is no cue {idx} (the captions have {count})")
        cue = cues[idx - 1]
        if ed.get("delete"):
            delete.add(idx - 1)
            changes.append(f"deleted cue {idx}")
            continue
        if "text" in ed:
            new_text = str(ed.get("text") or "").strip()
            if not new_text:
                raise ToolError(f"cue_edits[{i}]: empty text (use delete=true to remove the cue)")
            cue.text = new_text
            changes.append(f"cue {idx} text")
        start = timeline_to_caption(data, float(ed["start"])) if ed.get("start") is not None else cue.start
        end = timeline_to_caption(data, float(ed["end"])) if ed.get("end") is not None else cue.end
        if end <= start:
            raise ToolError(f"cue_edits[{i}]: end must be after start")
        if (start, end) != (cue.start, cue.end):
            cue.start, cue.end = start, end
            changes.append(f"cue {idx} timing")
    cues = [q for i, q in enumerate(cues) if i not in delete]

    _, src_start, src_end = _clip_window(data)
    for q in extra:
        s, e = timeline_to_caption(data, q.start), timeline_to_caption(data, q.end)
        if e <= src_start or s >= src_end:
            raise ToolError(f"the new cue at {q.start:.2f}-{q.end:.2f}s is outside clip {c.id} "
                            f"({caption_to_timeline(data, src_start):.2f}-{caption_to_timeline(data, src_end):.2f}s)")
        cues.append(Cue(max(s, src_start), min(e, src_end), q.text))
        changes.append("added a cue")

    if find:
        hits = 0
        for q in cues:
            shown = caption_cues.display_text(q.text)
            if find in shown:
                hits += shown.count(find)
                q.text = shown.replace(find, replace)
        if not hits:
            raise ToolError(f"no caption contains {find!r}")
        changes.append(f"replaced {hits} x {find!r}")
    if shift_seconds:
        for q in cues:
            q.start += float(shift_seconds)
            q.end += float(shift_seconds)
        cues = [q for q in cues if q.end > 0]
        for q in cues:
            q.start = max(0.0, q.start)
        changes.append(f"shifted {shift_seconds:+.2f}s")
    if uppercase:
        for q in cues:
            q.text = caption_cues.display_text(q.text).upper()
        changes.append("uppercase")

    ready, _changed = _prepare([Cue(q.start, q.end, caption_cues.display_text(q.text)) for q in cues], False)
    if not ready:
        raise ToolError("that would leave no captions; use remove_captions_tool to remove them")

    new_effect = copy.deepcopy(effect)
    new_effect["caption_text"] = caption_cues.build_caption_text(ready)
    look = None
    if restyle:
        cap_style = _style_from_args(style, font, text_size, text_color, stroke_color, stroke_width,
                                     background_color, background_opacity, position, fade_seconds,
                                     base=style_from_effect(effect))
        cap_style.resolve_font(installed_font_families())     # edit_captions runs on the GUI thread
        new_effect.update(copy.deepcopy(cap_style.properties(lines=2)))
        look = cap_style.summary()
        changes.append("restyled")
    if new_effect == effect:
        raise ToolError("the captions already look like that; nothing changed")

    effects = [new_effect if e.get("id") == effect.get("id") else e for e in (data.get("effects") or [])]
    _save_effects(c.id, effects)
    _refresh()
    from classes.query import Clip
    rows = _cue_rows(Clip.get(id=c.id).data, caption_cues.parse_caption_text(new_effect["caption_text"]))
    receipt = dict(timeline_clip_id=c.id, effect_id=effect.get("id"), changes=changes, cues=rows)
    if look:
        receipt["look"] = look
    return ok(f"Updated captions on {c.id}: {', '.join(changes)} ({len(rows)} cue(s)).", **receipt)


# ---------------------------------------------------------------------------
# remove_captions_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "remove_captions_tool",
    label="Remove captions",
    schema=obj({
        "effect_id": string("Caption effect id from list_captions_tool.", ""),
        "timeline_clip_id": string("Remove the captions of this clip.", ""),
        "clip_query": string("Describe the captioned clip instead of an id.", ""),
        "all_captions": boolean("Remove every caption track in the project.", False),
    }),
    covers=("caption.edit",),
)
def remove_captions(effect_id="", timeline_clip_id="", clip_query="", all_captions=False):
    """Remove captions (Caption effects) from clips. It never deletes clips.

    Use for "remove the captions / subtitles". Target one caption track by effect_id,
    the captions of one clip by timeline_clip_id / clip_query, or all_captions=true.
    The clips themselves stay; a transparent 'Captions' overlay clip that carried them
    stays too (delete it with delete_from_timeline_tool if the user wants it gone).
    One undo step brings the captions back.
    """
    from classes.query import Clip
    removed = []
    targets = {}
    if all_captions:
        for c in Clip.filter():
            effects = caption_effects(c.data)
            if effects:
                targets[c.id] = (c, {e.get("id") for e in effects})
        if not targets:
            raise ToolError("there are no captions in the project")
    elif str(effect_id or "").strip():
        c, e = _find_effect(str(effect_id).strip(), "", "")
        targets[c.id] = (c, {e.get("id")})
    elif str(timeline_clip_id or "").strip() or str(clip_query or "").strip():
        cid = str(timeline_clip_id or "").strip()
        c = Clip.get(id=cid) if cid else resolve_clip(clip_query=str(clip_query).strip())
        if not c:
            raise ToolError(f"no timeline clip with id={cid!r}")
        effects = caption_effects(c.data)
        if not effects:
            raise ToolError(f"clip {c.id} has no captions")
        targets[c.id] = (c, {e.get("id") for e in effects})
    else:
        c, e = _find_effect("", "", "")
        targets[c.id] = (c, {e.get("id")})
    locked = [c.id for c, _ in targets.values() if is_locked(int(c.data.get("layer") or 0))]
    if locked:
        raise ToolError(f"clip(s) on locked tracks: {', '.join(locked)}; unlock first")
    overlays = []
    for cid, (c, ids) in targets.items():
        effects = [e for e in (c.data.get("effects") or []) if e.get("id") not in ids]
        _save_effects(cid, effects)
        removed.extend(ids)
        if c.data.get("zenvi_role") == CAPTION_ROLE:
            overlays.append(cid)
    _refresh()
    receipt = dict(removed_effect_ids=sorted(removed), timeline_clip_ids=sorted(targets))
    summary = f"Removed {len(removed)} caption track(s) from {len(targets)} clip(s)."
    if overlays:
        receipt["empty_overlay_clip_ids"] = overlays
        summary += " The transparent Captions overlay clip(s) remain; delete them with delete_from_timeline_tool."
    return ok(summary, **receipt)

