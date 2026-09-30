"""Timer overlays (libopenshot Timer effect) and emoji stickers (Emojis dock). Workstream: titles-text."""

from __future__ import annotations

import copy
import os

from classes import emoji_catalog
from classes.editor_tools._base import (
    ToolError, boolean, clip_extent, enum, get_app, integer, is_locked, number, obj, ok, on_main,
    project_fps, snap_seconds, string, ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.titles_text_common import (
    GRAVITY, SCREEN_POSITIONS, color_hex_alpha, color_keyframes, constant, installed_font_families, new_effect_json, overlay_file, overlay_file_path, create_track,
    place_clip, plan_overlay_track, project_size, resolve_font, track_info,
)

TIMER_ROLE = "timer"
TIMER_MODES = {"count_up": 0, "count_down": 1, "clock": 2, "timecode": 3, "frame_number": 4}
TIMER_FORMATS = {"mm:ss": 0, "hh:mm:ss": 1, "hh:mm:ss.mmm": 2, "timecode": 3, "frames": 4}
TIME_CLIP, TIME_SOURCE = 0, 1


def _offsets(screen_position: str, margin_percent: float):
    """(x_offset %, y_offset %) that keep an overlay *margin_percent* in from the edges it hugs."""
    m = float(margin_percent)
    x = -m if screen_position.endswith("right") else (m if screen_position.endswith("left") else 0.0)
    y = m if screen_position.startswith("top") else (-m if screen_position.startswith("bottom") else 0.0)
    return x, y


def _timeline_end() -> float:
    from classes.query import Clip
    return max((clip_extent(c.data)[1] for c in Clip.filter()), default=0.0)


# ---------------------------------------------------------------------------
# add_timer_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "add_timer_tool",
    label="Add timer",
    schema=obj({
        "mode": enum(list(TIMER_MODES),
                     "count_down = remaining time to zero; count_up = elapsed time (stopwatch); clock = a time "
                     "of day starting at start_at_seconds; timecode = HH:MM:SS:FF; frame_number = frame count.",
                     "count_down"),
        "position_seconds": number("Timeline second where the timer appears (overlay mode).", 0.0, minimum=0),
        "duration_seconds": number("How long the timer shows. 0 = automatic: a countdown's length, otherwise "
                                   "until the end of the timeline.", 0.0, minimum=0, maximum=86400),
        "countdown_from_seconds": number("count_down: the number it starts from (0 = the timer's duration).",
                                         0.0, minimum=0, maximum=86400),
        "start_at_seconds": number("count_up: starting offset; clock: the time of day to start at, in seconds "
                                   "after midnight (9:30 = 34200).", 0.0, minimum=-86400, maximum=86400),
        "format": enum(list(TIMER_FORMATS), "How the time is written.", "mm:ss"),
        "prefix": string("Text before the time, e.g. 'T-' or 'Starts in '.", ""),
        "suffix": string("Text after the time, e.g. ' left'.", ""),
        "screen_position": enum(SCREEN_POSITIONS, "Where on the frame the timer sits.", "bottom_center"),
        "margin_percent": number("Distance from the frame edges it hugs, in % of the frame.", 3.0,
                                 minimum=0, maximum=40),
        "font": string("Font family, e.g. 'Menlo', 'Arial Black'. Empty = the default sans font.", ""),
        "text_size": number("Digit height as a fraction of the frame's shorter side (0.07 = normal, 0.15 = a big "
                            "centre countdown).", 0.07, minimum=0.01, maximum=0.5),
        "text_color": string("Digit colour ('#RRGGBB[AA]', 'rgba(...)', name).", "#ffffff"),
        "stroke_color": string("Outline colour.", "#000000"),
        "stroke_width": number("Outline thickness relative to the digit height (0 = none).", 0.03,
                               minimum=0, maximum=0.5),
        "background": boolean("Draw a rounded box behind the digits.", True),
        "background_color": string("Box colour.", "#000000"),
        "background_opacity": number("Box opacity 0-1.", 0.45, minimum=0, maximum=1),
        "timeline_clip_id": string("Draw the timer on this clip instead of a new overlay (it then follows the "
                                   "clip; count_up shows the time since the clip's first visible frame).", ""),
        "track": string("Overlay track: UI track number (1 = bottom), name or layer. Empty = the lowest free track "
                        "above the video.", ""),
    }),
    background_safe=True,
    covers=("text.timer",),
)
def add_timer(mode="count_down", position_seconds=0.0, duration_seconds=0.0, countdown_from_seconds=0.0,
              start_at_seconds=0.0, format="mm:ss", prefix="", suffix="", screen_position="bottom_center",
              margin_percent=3.0, font="", text_size=0.07, text_color="#ffffff", stroke_color="#000000",
              stroke_width=0.03, background=True, background_color="#000000", background_opacity=0.45,
              timeline_clip_id="", track=""):
    """Put a live timer on screen: countdown, stopwatch, clock, timecode or frame counter.

    Use for "add a 10-second countdown", "show a stopwatch in the top right", "burn in
    timecode", "a clock starting at 9:30". By default it adds a transparent 'Timer'
    overlay clip from position_seconds for duration_seconds on the lowest free track
    above the video; with timeline_clip_id the Timer effect goes on that clip instead.
    Rendered by libopenshot's Timer effect (live digits, not an image). The time is
    written as mm:ss unless format says otherwise ('00:10' counts to '00:00'). One undo
    step. For static text use add_title_tool.

    Examples: {"mode": "count_down", "countdown_from_seconds": 10, "position_seconds": 5,
    "screen_position": "center", "text_size": 0.15}; {"mode": "timecode", "format": "timecode",
    "screen_position": "bottom_right"}.
    """
    fps = project_fps()
    font_name = str(font or "").strip()
    if font_name:
        font_name = resolve_font(font_name, on_main(installed_font_families))
    fg, fg_alpha = color_hex_alpha(text_color, "text_color")
    stroke_hex = color_hex_alpha(stroke_color, "stroke_color")[0]
    bg_hex, bg_alpha = color_hex_alpha(background_color, "background_color")
    if bg_alpha < 1.0:
        background_opacity = bg_alpha
    attach_id = str(timeline_clip_id or "").strip()

    w, h = project_size()
    font_px = float(text_size) * min(w, h)
    units = 600.0 / float(w)   # Timer scales sizes by width / 600
    x_off, y_off = _offsets(screen_position, margin_percent)
    props = {
        "mode": TIMER_MODES[mode],
        "format": TIMER_FORMATS[format],
        "clamp": 1,
        "gravity": GRAVITY[screen_position],
        "show_background": 1 if background else 0,
        "font_name": font_name or "sans",
        "prefix": str(prefix or ""),
        "suffix": str(suffix or ""),
        "color": color_keyframes(fg),
        "font_alpha": constant(fg_alpha),
        "stroke": color_keyframes(stroke_hex),
        "stroke_width": constant(round(float(stroke_width) * font_px * units, 3)),
        "background": color_keyframes(bg_hex),
        "background_alpha": constant(float(background_opacity)),
        "background_padding": constant(round(0.25 * font_px * units, 3)),
        "background_corner": constant(round(0.15 * font_px * units, 3)),
        "font_size": constant(round(font_px * units, 3)),
        "x_offset": constant(x_off),
        "y_offset": constant(y_off),
        "apply_before_clip": False,
    }

    if attach_id:
        def _attach():
            from classes.query import Clip
            c = Clip.get(id=attach_id)
            if not c:
                raise ToolError(f"no timeline clip with id={attach_id!r}")
            layer = int(c.data.get("layer") or 0)
            if is_locked(layer):
                raise ToolError(f"clip {c.id} is on locked track {ui_track_number(layer)}; unlock it first")
            start = float(c.data.get("start") or 0.0)
            length = float(c.data.get("end") or 0.0) - start
            effect = new_effect_json("Timer")
            effect.update(copy.deepcopy(props))
            effect["time_source"] = TIME_SOURCE
            offset = start_at_seconds - start if mode in ("count_up", "count_down") else start_at_seconds
            effect["start_time"] = constant(offset)
            effect["end_time"] = constant(countdown_from_seconds or 0.0)
            effects = copy.deepcopy(list(c.data.get("effects") or [])) + [effect]
            get_app().updates.update(["clips", {"id": c.id}], {"effects": effects})
            return c.id, effect["id"], layer, float(c.data.get("position") or 0.0), length
        clip_id, effect_id, layer, position, length = on_main(_attach)
        summary = f"Added a {mode.replace('_', ' ')} timer to clip {clip_id} ({screen_position.replace('_', ' ')})."
        return ok(summary, timeline_clip_id=clip_id, effect_id=effect_id, overlay=False, mode=mode, format=format,
                  position=round(position, 3), duration=round(length, 3), screen_position=screen_position,
                  track=ui_track_number(layer))

    position = snap_seconds(position_seconds)
    if duration_seconds:
        duration = float(duration_seconds)
    elif mode == "count_down":
        duration = float(countdown_from_seconds or 10.0)
    else:
        end = on_main(_timeline_end)
        duration = end - position if end - position > 1.0 else 10.0
    duration = max(1, int(round(duration * fps))) / fps
    end = position + duration
    on_main(plan_overlay_track, position, end, track)
    carrier = overlay_file_path(w, h)

    def _commit():
        layer, created = plan_overlay_track(position, end, track)
        f = overlay_file(carrier)
        if created:
            create_track(layer, "Timer")
        effect = new_effect_json("Timer")
        effect.update(copy.deepcopy(props))
        effect["time_source"] = TIME_CLIP
        effect["start_time"] = constant(start_at_seconds)
        effect["end_time"] = constant(countdown_from_seconds or 0.0)
        clip = place_clip(f.id, position, duration, layer, title="Timer",
                          props={"zenvi_role": TIMER_ROLE, "effects": [effect]})
        return clip, effect["id"], layer, created

    clip, effect_id, layer, created = on_main(_commit)
    t = track_info(layer)
    what = mode.replace("_", " ")
    summary = (f"Added a {what} timer on track {t['track']} at {position:.2f}-{end:.2f}s "
               f"({screen_position.replace('_', ' ')}).")
    return ok(summary, timeline_clip_id=clip.get("id"), effect_id=effect_id, overlay=True, mode=mode, format=format,
              position=round(position, 3), duration=round(duration, 3), end=round(end, 3),
              countdown_from=countdown_from_seconds or (duration if mode == "count_down" else 0),
              screen_position=screen_position, new_track=created, **t)


# ---------------------------------------------------------------------------
# search_emojis_tool / add_emoji_tool
# ---------------------------------------------------------------------------

GROUP_IDS = ["", "smileys-emotion", "people-body", "animals-nature", "food-drink", "travel-places",
             "activities", "objects", "symbols", "flags", "extras-openmoji", "extras-unicode", "user"]


@editor_tool(
    "search_emojis_tool",
    label="Search emoji",
    schema=obj({
        "query": string("A name or word ('fire', 'heart', 'thumbs up', 'coffee'), an emoji character ('🔥') or an "
                        "OpenMoji code ('1F525'). Empty lists a group.", ""),
        "group": enum(GROUP_IDS, "Only this emoji group (the Emojis dock's dropdown). Empty = all.", ""),
        "limit": integer("How many matches to return.", 10, minimum=1, maximum=50),
    }),
    read_only=True,
    covers=("emoji.add",),
)
def search_emojis(query="", group="", limit=10):
    """Find emoji in the Emojis dock's 1,239 OpenMoji stickers by name, character, code or group.

    Use when the user wants to see options ("what fire emojis are there", "show me food
    emoji") or when add_emoji_tool picked the wrong one. Each match has the emoji
    character, its name, group and code (pass the code to add_emoji_tool). Read-only.
    """
    q = str(query or "").strip()
    if not q and not group:
        raise ToolError("pass a query (e.g. 'fire') or a group")
    matches = emoji_catalog.search(q, group, limit)
    if not matches:
        raise ToolError(f"no emoji matches {q!r}" + (f" in group {group}" if group else ""))
    rows = [dict(e.as_dict(), score=round(s, 1)) for s, e in matches]
    return ok(f"{len(rows)} emoji match {q or group!r}: " + ", ".join(f"{r['emoji']} {r['name']}" for r in rows[:8]),
              matches=rows)


@editor_tool(
    "add_emoji_tool",
    label="Add emoji",
    schema=obj({
        "emoji": string("Which emoji: a name ('fire', 'red heart', 'party popper'), the character ('🔥') or an "
                        "OpenMoji code from search_emojis_tool ('1F525').", ""),
        "group": enum(GROUP_IDS, "Narrow a name to one emoji group.", ""),
        "position_seconds": number("Timeline second where the emoji appears.", 0.0, minimum=0),
        "duration_seconds": number("How long it stays, in seconds. 0 = the editor's default image length "
                                   "(Preferences, 10 s unless changed).", 0.0, minimum=0, maximum=86400),
        "screen_position": enum(SCREEN_POSITIONS, "Where on the frame the emoji sits.", "center"),
        "size": number("Emoji size as a fraction of the frame's shorter side (0.2 = a fifth).", 0.2,
                       minimum=0.02, maximum=1.0),
        "margin": number("Distance from the edges it hugs, as a fraction of the shorter side.", 0.04,
                         minimum=0, maximum=0.4),
        "track": string("UI track number (1 = bottom), name or layer. Empty = the lowest free track above the "
                        "video.", ""),
    }, required=["emoji"]),
    background_safe=True,
    covers=("emoji.add",),
)
def add_emoji(emoji, group="", position_seconds=0.0, duration_seconds=0.0, screen_position="center", size=0.2,
              margin=0.04, track=""):
    """Put an emoji sticker on the video, e.g. "put a 🔥 emoji top right", "add a heart at 5 seconds".

    Finds the emoji by name, character or code among the Emojis dock's OpenMoji
    stickers (search_emojis_tool shows alternatives), adds it to Project Files the
    way the dock does, and places it on the lowest free track above the video at
    position_seconds, sized and pinned to screen_position. The receipt lists close
    alternatives in case the match was not the intended one. One undo step.

    Example: {"emoji": "fire", "screen_position": "top_right", "size": 0.15, "position_seconds": 2,
    "duration_seconds": 3}.
    """
    matches = emoji_catalog.search(str(emoji or "").strip(), group, 6)
    if not matches:
        raise ToolError(f"no emoji matches {emoji!r}" + (f" in group {group}" if group else "")
                        + "; try search_emojis_tool with a simpler word")
    best_score, best = matches[0]
    if best_score < 20.0:
        raise ToolError(f"no good emoji match for {emoji!r}; closest: "
                        + ", ".join(f"{e.char} {e.name} ({e.code})" for _, e in matches[:5]))
    if not os.path.isfile(best.path):
        raise ToolError(f"emoji file missing: {best.path}")
    fps = project_fps()
    position = snap_seconds(position_seconds)
    duration = float(duration_seconds or 0.0) or float(
        get_app().get_settings().get("default-image-length") or 10.0)
    duration = max(1, int(round(duration * fps))) / fps
    end = position + duration
    on_main(plan_overlay_track, position, end, track)

    w, h = project_size()
    short = float(min(w, h))
    x_off, y_off = _offsets(screen_position, 1.0)
    props = {
        "gravity": GRAVITY[screen_position],
        "scale_x": constant(float(size)),
        "scale_y": constant(float(size)),
        "location_x": constant(round(x_off * float(margin) * short / w, 5)),
        "location_y": constant(round(y_off * float(margin) * short / h, 5)),
    }

    def _commit():
        layer, created = plan_overlay_track(position, end, track)
        f = emoji_catalog.add_emoji_file(best.path, best.name)
        if created:
            create_track(layer, "Emoji")
        clip = place_clip(f.id, position, duration, layer, props=props)
        return f.id, clip, layer, created

    try:
        file_id, clip, layer, created = on_main(_commit)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not add {best.name}: {exc}") from None
    t = track_info(layer)
    summary = (f"Added {best.char} {best.name} on track {t['track']} at {position:.2f}-{end:.2f}s "
               f"({screen_position.replace('_', ' ')}, size {size:g}).")
    alternatives = [e.as_dict() for _, e in matches[1:6]]
    return ok(summary, timeline_clip_id=clip.get("id"), file_id=file_id, emoji=best.as_dict(),
              position=round(position, 3), duration=round(duration, 3), end=round(end, 3),
              screen_position=screen_position, size=size, new_track=created, alternatives=alternatives, **t)

