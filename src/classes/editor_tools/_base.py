"""Shared helpers for the ``classes.editor_tools`` workstream modules.

Conventions every editor tool follows (details in docs/agent-tools.md):

* Explicit keyword arguments described by an explicit JSON schema (built
  with the helpers below). The registry coerces what the model sends to the
  schema's types and rejects unknown arguments.
* Raise :class:`ToolError` for anything the caller got wrong or that cannot
  be done; the registry returns it as ``"Error: ..."``, the prefix the
  backend, the chat UI and the MCP server read as a failed call. Validate
  BEFORE the first mutation, so a refused call leaves undo history untouched.
* Return :func:`ok`: one line for the model, then a JSON receipt with the
  ids and values that changed, so the next call never parses prose.
* Mutate only through ``get_app().updates`` (``QueryObject.save()/delete()``
  or ``updates.insert/update/delete``). ``execute_tool`` wraps each call in
  one undo transaction; :func:`on_main` carries it across the GUI-thread hop.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from classes.logger import log

# OpenShot keyframe interpolation codes (openshot.BEZIER / LINEAR / CONSTANT).
BEZIER, LINEAR, CONSTANT = 0, 1, 2
_INTERPOLATION_NAMES = {"bezier": BEZIER, "ease": BEZIER, "smooth": BEZIER,
                        "linear": LINEAR, "constant": CONSTANT, "hold": CONSTANT, "step": CONSTANT}


class ToolError(Exception):
    """A caller-side problem or an impossible request; returned as ``Error: <message>``."""


# ---------------------------------------------------------------------------
# Lazy access to tool_handlers (it imports this package while it initialises)
# ---------------------------------------------------------------------------

def th():
    from classes import tool_handlers
    return tool_handlers


def get_app():
    return th()._get_app()


def on_main(func, *args, timeout=None):
    """Run *func(*args)* on the Qt GUI thread and return its result.

    Inline when already on the GUI thread (mutating tools are dispatched
    there) and under the headless test stub. Keeps the caller's undo
    transaction id across the hop, so a background-safe tool's mutations
    still land in its one undo step.
    """
    return th()._run_on_main_thread(func, *args, timeout=timeout)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def ok(summary: str, **receipt: Any) -> str:
    """Successful result: a one-line summary, then a compact JSON receipt."""
    summary = " ".join(str(summary).split())
    if not receipt:
        return summary
    return summary + "\n" + json.dumps(receipt, separators=(",", ":"), default=str)


def fail(message: str) -> str:
    message = " ".join(str(message).split())
    return message if message.startswith("Error") else "Error: " + message


# ---------------------------------------------------------------------------
# JSON schema builders (every property needs a description)
# ---------------------------------------------------------------------------

def obj(properties: dict, required: Optional[list] = None) -> dict:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


def _with(schema: dict, description: str, default: Any, extra: dict) -> dict:
    schema["description"] = description
    if default is not None:
        schema["default"] = default
    schema.update(extra)
    return schema


def string(description: str, default: Any = None, **extra) -> dict:
    return _with({"type": "string"}, description, default, extra)


def number(description: str, default: Any = None, minimum=None, maximum=None, **extra) -> dict:
    s = {"type": "number"}
    if minimum is not None:
        s["minimum"] = minimum
    if maximum is not None:
        s["maximum"] = maximum
    return _with(s, description, default, extra)


def integer(description: str, default: Any = None, minimum=None, maximum=None, **extra) -> dict:
    s = {"type": "integer"}
    if minimum is not None:
        s["minimum"] = minimum
    if maximum is not None:
        s["maximum"] = maximum
    return _with(s, description, default, extra)


def boolean(description: str, default: Any = None, **extra) -> dict:
    return _with({"type": "boolean"}, description, default, extra)


def enum(values: Iterable, description: str, default: Any = None, **extra) -> dict:
    values = list(values)
    kinds = {type(v) for v in values}
    s: dict = {"enum": values}
    if kinds == {str}:
        s["type"] = "string"
    return _with(s, description, default, extra)


def array(items: dict, description: str, default: Any = None, **extra) -> dict:
    return _with({"type": "array", "items": items}, description, default, extra)


def mapping(description: str, properties: Optional[dict] = None, **extra) -> dict:
    s: dict = {"type": "object"}
    if properties:
        s["properties"] = properties
    return _with(s, description, None, extra)


def nullable(schema: dict) -> dict:
    s = dict(schema)
    t = s.get("type")
    if isinstance(t, str):
        s["type"] = [t, "null"]
    s["nullable"] = True
    return s


# Reusable argument fragments -------------------------------------------------

CLIP_TARGET = {
    "timeline_clip_id": string("Timeline clip id from list_clips_tool / get_timeline_state_tool. Preferred.", ""),
    "clip_query": string("Describe the clip instead of an id (file name, content, 'the 2nd clip on track 1').", ""),
    "track": string("Narrow clip_query to a track: UI track number (1 = bottom), track name, or layer number.", ""),
}

CLIPS_TARGET = {
    "timeline_clip_ids": array({"type": "string"}, "Timeline clip ids to act on.", []),
    "clip_query": string("Describe one clip instead of ids.", ""),
    "track": string("Track for clip_query or scope='track': UI track number (1 = bottom), name, or layer number.", ""),
    "scope": enum(["", "selected", "track", "all"],
                  "Instead of ids/query: 'selected' = the timeline selection, 'track' = every clip on track, "
                  "'all' = every clip on the timeline.", ""),
}


# ---------------------------------------------------------------------------
# Project, fps and time
# ---------------------------------------------------------------------------

def project():
    return get_app().project


def fps_fraction() -> tuple:
    fps = project().get("fps") or {"num": 30, "den": 1}
    try:
        num, den = int(fps.get("num") or 30), int(fps.get("den") or 1)
    except (TypeError, ValueError, AttributeError):
        num, den = 30, 1
    return (num, den or 1)


def project_fps() -> float:
    num, den = fps_fraction()
    return float(num) / float(den)


def seconds_to_frame(seconds: float) -> int:
    """Timeline seconds -> 1-based timeline frame number at project fps."""
    return int(round(max(0.0, float(seconds)) * project_fps())) + 1


def frame_to_seconds(frame: float) -> float:
    return (float(frame) - 1.0) / project_fps()


def snap_seconds(seconds: float) -> float:
    """Snap to the project frame grid (what the timeline does on drop)."""
    fps = project_fps()
    return round(float(seconds) * fps) / fps


def playhead_seconds() -> float:
    try:
        player = get_app().window.preview_thread.player
        return frame_to_seconds(player.Position())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

def layers() -> list:
    return list(project().get("layers") or [])


def resolve_layer(track: Any) -> int:
    """UI track number (1 = bottom), label, track id (L3) or layer number -> layer number."""
    from classes.track_display import normalize_track_or_layer_arg
    if track is None or not str(track).strip():
        raise ToolError("a track is required (UI track number, name, or layer number)")
    layer, err = normalize_track_or_layer_arg(str(track).strip(), layers())
    if err:
        raise ToolError(err)
    if layer is None:
        raise ToolError(f"no track matches {track!r}")
    return int(layer)


def track_label(layer_num: int) -> str:
    from classes.track_display import format_track_label_for_llm
    return format_track_label_for_llm(int(layer_num), layers())


def ui_track_number(layer_num: int) -> Optional[int]:
    from classes.track_display import build_track_stack
    for entry in build_track_stack(layers()):
        if entry["layer_number"] == int(layer_num):
            return int(entry["ui_track"])
    return None


def is_locked(layer_num: int) -> bool:
    for layer in layers():
        try:
            if int(layer.get("number") or 0) == int(layer_num):
                return bool(layer.get("lock"))
        except (TypeError, ValueError):
            continue
    return False


def ensure_unlocked(layer_num: int) -> None:
    if is_locked(layer_num):
        raise ToolError(f"track {ui_track_number(layer_num) or layer_num} is locked; unlock it first")


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

def resolve_clip(timeline_clip_id: str = "", clip_query: str = "", track: str = "",
                 occurrence: int = 0, position_near: Optional[float] = None):
    """One timeline clip (QueryObject) by id, description, or the only/under-playhead clip."""
    res = th()._resolve_timeline_clip_for_tool(
        timeline_clip_id=timeline_clip_id or "", clip_query=clip_query or "",
        track=track or "", occurrence=occurrence or 0, position_near=position_near)
    if not res.ok or not res.clip:
        raise ToolError(res.error or "could not resolve the timeline clip")
    return res.clip


def selected_clip_ids() -> list:
    try:
        return list(get_app().window.selected_clips or [])
    except Exception:
        return []


def resolve_clips(timeline_clip_ids: Optional[list] = None, clip_query: str = "",
                  track: str = "", scope: str = "") -> list:
    """Several clips: explicit ids, a query, the selection, a whole track, or all.

    Raises ToolError when nothing matches, so a batch never silently no-ops.
    """
    from classes.query import Clip
    scope = (scope or "").strip().lower()
    if timeline_clip_ids:
        out = []
        for cid in timeline_clip_ids:
            c = Clip.get(id=str(cid).strip())
            if not c:
                raise ToolError(f"no timeline clip with id={cid!r}")
            out.append(c)
        return out
    if scope == "selected":
        ids = selected_clip_ids()
        if not ids:
            raise ToolError("no clips are selected in the timeline")
        return resolve_clips(ids)
    if scope in ("track", "all"):
        clips = list(Clip.filter())
        if scope == "track":
            layer = resolve_layer(track)
            clips = [c for c in clips if int(c.data.get("layer") or 0) == layer]
        clips.sort(key=lambda c: (float(c.data.get("position") or 0.0), int(c.data.get("layer") or 0)))
        if not clips:
            raise ToolError("the timeline has no clips" if scope == "all" else f"track {track} has no clips")
        return clips
    return [resolve_clip(clip_query=clip_query, track=track)]


def clip_extent(clip_data: dict) -> tuple:
    """(timeline_start, timeline_end, duration) in seconds."""
    position = float(clip_data.get("position") or 0.0)
    start = float(clip_data.get("start") or 0.0)
    end = float(clip_data.get("end") or 0.0)
    duration = max(0.0, end - start)
    return position, position + duration, duration


def clip_frame_at(clip_data: dict, timeline_seconds: float) -> int:
    """Clip-local keyframe X for a timeline time (includes the trimmed start).

    libopenshot evaluates clip keyframes at the clip's own frame number, where
    frame 1 is the first frame of the *source*: a keyframe at the visible start
    of a clip trimmed by `start` seconds sits at round(start*fps)+1.
    """
    fps = project_fps()
    position = float(clip_data.get("position") or 0.0)
    start = float(clip_data.get("start") or 0.0)
    local = max(0.0, float(timeline_seconds) - position) + start
    return int(round(local * fps)) + 1


def describe_clip(clip_obj) -> dict:
    data = clip_obj.data if isinstance(getattr(clip_obj, "data", None), dict) else {}
    tl_start, tl_end, duration = clip_extent(data)
    layer = int(data.get("layer") or 0)
    return {
        "timeline_clip_id": str(clip_obj.id),
        "title": str(data.get("title") or ""),
        "file_id": str(data.get("file_id") or ""),
        "track": ui_track_number(layer),
        "layer": layer,
        "position": round(tl_start, 3),
        "end": round(tl_end, 3),
        "duration": round(duration, 3),
        "source_in": round(float(data.get("start") or 0.0), 3),
        "source_out": round(float(data.get("end") or 0.0), 3),
    }


def timeline_ui():
    """The active timeline widget (native QWidget or web); both expose the menu handlers."""
    win = get_app().window
    timeline = getattr(win, "timeline", None)
    if timeline is None:
        raise ToolError("the timeline is not ready yet")
    return timeline


# ---------------------------------------------------------------------------
# Keyframes (OpenShot JSON)
# ---------------------------------------------------------------------------

def interpolation_code(name: Any) -> int:
    if isinstance(name, int) and not isinstance(name, bool):
        if name in (BEZIER, LINEAR, CONSTANT):
            return name
    key = str(name or "bezier").strip().lower()
    if key not in _INTERPOLATION_NAMES:
        raise ToolError(f"interpolation must be bezier, linear or constant, got {name!r}")
    return _INTERPOLATION_NAMES[key]


def keyframe_point(frame: float, value: float, interpolation: int = BEZIER) -> dict:
    return {
        "co": {"X": float(frame), "Y": float(value)},
        "interpolation": int(interpolation),
        "handle_type": 0,
        "handle_left": {"X": 0.5, "Y": 1.0},
        "handle_right": {"X": 0.5, "Y": 0.0},
    }


def keyframe(points: Iterable[tuple]) -> dict:
    """[(frame, value[, interpolation])...] -> {"Points": [...]} sorted by frame."""
    pts = []
    for p in points:
        interp = p[2] if len(p) > 2 else BEZIER
        pts.append(keyframe_point(p[0], p[1], interp))
    pts.sort(key=lambda pt: pt["co"]["X"])
    return {"Points": pts}


def constant_keyframe(value: float) -> dict:
    return keyframe([(1, value, BEZIER)])


def keyframe_value_at(kf: Any, frame: float, default: float = 0.0) -> float:
    """Evaluate an OpenShot keyframe dict at *frame* (linear between points).

    For reporting and for starting values; playback uses libopenshot's own
    (bezier-aware) evaluation.
    """
    if isinstance(kf, (int, float)) and not isinstance(kf, bool):
        return float(kf)
    pts = (kf or {}).get("Points") if isinstance(kf, dict) else None
    if not pts:
        return float(default)
    pts = sorted(pts, key=lambda p: float(p["co"]["X"]))
    if frame <= float(pts[0]["co"]["X"]):
        return float(pts[0]["co"]["Y"])
    for a, b in zip(pts, pts[1:]):
        ax, bx = float(a["co"]["X"]), float(b["co"]["X"])
        if ax <= frame <= bx:
            if int(a.get("interpolation", BEZIER)) == CONSTANT or bx == ax:
                return float(a["co"]["Y"])
            t = (frame - ax) / (bx - ax)
            return float(a["co"]["Y"]) + t * (float(b["co"]["Y"]) - float(a["co"]["Y"]))
    return float(pts[-1]["co"]["Y"])


_COLOR_NAMES = {"white": "#ffffff", "black": "#000000", "red": "#ff0000", "green": "#00ff00",
                "blue": "#0000ff", "yellow": "#ffff00", "cyan": "#00ffff", "magenta": "#ff00ff",
                "orange": "#ffa500", "purple": "#800080", "pink": "#ffc0cb", "gray": "#808080",
                "grey": "#808080", "transparent": "#00000000"}


def parse_color(value: Any) -> tuple:
    """'#RRGGBB', '#RRGGBBAA', 'rgb(r,g,b[,a])' or a basic color name -> (r, g, b, a) 0-255."""
    s = _COLOR_NAMES.get(str(value or "").strip().lower(), str(value or "").strip())
    if s.startswith("#"):
        h = s[1:]
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        if len(h) in (6, 8):
            try:
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                return r, g, b, (int(h[6:8], 16) if len(h) == 8 else 255)
            except ValueError:
                pass
    if s.lower().startswith("rgb") and "(" in s and ")" in s:
        parts = [p.strip() for p in s[s.find("(") + 1:s.rfind(")")].split(",")]
        try:
            nums = [float(p) for p in parts]
            r, g, b = (int(max(0, min(255, round(n)))) for n in nums[:3])
            a = 255
            if len(nums) > 3:
                a = int(round(nums[3] * 255)) if nums[3] <= 1 else int(nums[3])
            return r, g, b, max(0, min(255, a))
        except (ValueError, IndexError):
            pass
    raise ToolError(f"unrecognised color {value!r}; use #RRGGBB, #RRGGBBAA, rgb(r,g,b) or a basic color name")


def color_keyframe(value: Any) -> dict:
    r, g, b, a = parse_color(value)
    return {"red": constant_keyframe(r), "green": constant_keyframe(g),
            "blue": constant_keyframe(b), "alpha": constant_keyframe(a)}


# ---------------------------------------------------------------------------
# UI refresh
# ---------------------------------------------------------------------------

def refresh_preview() -> None:
    """Ask the preview to redraw the current frame (after property edits)."""
    try:
        get_app().window.refreshFrameSignal.emit()
    except Exception:
        log.debug("preview refresh skipped", exc_info=True)
