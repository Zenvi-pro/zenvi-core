"""Effects and color: effect catalog and editing, analysis effects, Look presets, color grading, LUTs, scopes, chroma key, audio effects.

Workstream: effects-color. Tools register themselves with ``@editor_tool`` from
``classes.editor_tools._registry``; helpers live in ``classes.editor_tools._base``.

Effects live inside their clip (``clip["effects"]``). Adding or removing one
rewrites the clip's ``effects`` list in one update; changing properties
updates only the changed keys of that effect (the Properties dock's path).
Look presets go through ``classes.look_presets`` (the Clip > Look menu's
rules) and grades through ``classes.color_presets``. Processing effects
(Stabilizer, Tracker, Object Detector, Object Mask) run in
``effects_color_process``; frame analysis in ``effects_color_analysis``.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import threading
from typing import Any, Optional

from classes import effect_ops
from classes.editor_tools._base import (
    BEZIER,
    CONSTANT,
    CLIP_TARGET,
    ToolError,
    array,
    boolean,
    clip_extent,
    clip_frame_at,
    color_keyframe,
    constant_keyframe,
    enum,
    get_app,
    integer,
    interpolation_code,
    is_locked,
    keyframe_value_at,
    mapping,
    nullable,
    number,
    obj,
    ok,
    on_main,
    parse_color,
    project_fps,
    refresh_preview,
    resolve_clip,
    resolve_clips,
    string,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

# Effect keys a tool never sets: identity, placement (effects follow their clip),
# and data that only processing writes.
HIDDEN_PROPERTIES = frozenset({
    "id", "position", "layer", "start", "end", "duration", "order", "class_name", "type", "name",
    "description", "has_audio", "has_video", "has_tracked_object", "objects", "protobuf_data_path",
    "BaseFPS", "TimeScale", "ui", "ui-menu", "parent_effect_id",
})
# Settable on every effect; listed once by list_effects_tool instead of per effect.
COMMON_PROPERTIES = ("apply_before_clip", "mask_invert", "mask_loop_mode", "mask_time_mode",
                     "mask_reader", "mask_source_id")
PROCESSING_EFFECTS = ("Stabilizer", "Tracker", "ObjectDetection", "ObjectMask")
# Video effects that make pictures out of sound, so they belong on audio clips too.
AUDIO_REACTIVE_EFFECTS = frozenset({"AudioVisualization", "BeatSync"})

_ALIASES = {
    "greenscreen": "ChromaKey", "bluescreen": "ChromaKey", "chroma": "ChromaKey", "keyer": "ChromaKey",
    "grade": "ColorGrade", "colorgrading": "ColorGrade", "colorcorrection": "ColorGrade",
    "lut": "ColorMap", "lookup": "ColorMap", "brightnesscontrast": "Brightness", "contrast": "Brightness",
    "grain": "FilmGrain", "vhs": "AnalogTape", "tape": "AnalogTape", "eq": "ParametricEQ",
    "equalizer": "ParametricEQ", "equaliser": "ParametricEQ", "denoise": "DenoiseImage",
    "stabilize": "Stabilizer", "stabilise": "Stabilizer", "stabilization": "Stabilizer",
    "track": "Tracker", "objecttracker": "Tracker", "objectdetector": "ObjectDetection",
    "detection": "ObjectDetection", "segmentation": "ObjectMask", "invert": "Negate",
    "negative": "Negate", "letterbox": "Bars", "robot": "Robotization", "whisper": "Whisperization",
    "noisegate": "Expander", "gate": "Expander", "360": "SphericalProjection",
    "fisheye": "SphericalProjection", "flare": "LensFlare", "dropshadow": "Shadow",
    "visualizer": "AudioVisualization", "waveform": "AudioVisualization",
}

_CATALOG: Optional[dict] = None


def _load_raw_catalog() -> dict:
    """{class_name: {"info", "properties", "defaults"}} straight from libopenshot."""
    import openshot

    out = {}
    for entry in json.loads(openshot.EffectInfo.Json()):
        class_name = entry.get("class_name")
        effect = openshot.EffectInfo().CreateEffect(class_name) if class_name else None
        if effect is None:
            continue
        out[class_name] = {
            "info": entry,
            "properties": json.loads(effect.PropertiesJSON(1)),
            "defaults": json.loads(effect.Json()),
        }
    return out


def _is_keyframe(value) -> bool:
    return isinstance(value, dict) and isinstance(value.get("Points"), list)


def _is_color(value) -> bool:
    return isinstance(value, dict) and all(_is_keyframe(value.get(c)) for c in ("red", "green", "blue"))


def _color_hex(value) -> Optional[str]:
    if not _is_color(value):
        return None
    chans = []
    for c in ("red", "green", "blue", "alpha"):
        pts = (value.get(c) or {}).get("Points") or []
        if not pts:
            if c == "alpha":
                chans.append(255)
                continue
            return None
        chans.append(int(round(float(pts[0]["co"]["Y"]))))
    return "#%02x%02x%02x%02x" % tuple(max(0, min(255, v)) for v in chans)


def _default_of(value):
    if _is_keyframe(value):
        pts = value["Points"]
        return float(pts[0]["co"]["Y"]) if pts else None
    if _is_color(value):
        return _color_hex(value)
    if isinstance(value, (bool, int, float, str)):
        return value
    return None


def _build_catalog(raw: dict) -> dict:
    out = {}
    for class_name, entry in raw.items():
        info = entry.get("info") or {}
        defaults = entry.get("defaults") or {}
        props = {}
        names = [k for k in (entry.get("properties") or {}) if isinstance(entry["properties"][k], dict)]
        names += [k for k in defaults if k not in names]
        for pname in names:
            if pname in HIDDEN_PROPERTIES:
                continue
            p = (entry.get("properties") or {}).get(pname)
            p = p if isinstance(p, dict) else {}
            if p.get("readonly"):
                continue
            current = defaults.get(pname)
            ptype = p.get("type")
            if not ptype:
                if _is_color(current):
                    ptype = "color"
                elif _is_keyframe(current) or isinstance(current, float):
                    ptype = "float"
                elif isinstance(current, bool):
                    ptype = "bool"
                elif isinstance(current, int):
                    ptype = "int"
                elif isinstance(current, str):
                    ptype = "string"
                else:
                    continue
            spec = {"type": ptype, "keyframable": bool(p.get("keyframe")) or _is_keyframe(current)}
            lo, hi = p.get("min"), p.get("max")
            if ptype in ("float", "int") and isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo < hi:
                spec["min"], spec["max"] = lo, hi
            if p.get("choices"):
                spec["choices"] = [{"name": c.get("name"), "value": c.get("value")} for c in p["choices"]]
            default = _default_of(current)
            if default is not None:
                spec["default"] = round(default, 4) if isinstance(default, float) else default
            props[pname] = spec
        has_video = bool(info.get("has_video"))
        has_audio = bool(info.get("has_audio"))
        out[class_name] = {
            "effect": class_name,
            "name": info.get("name") or class_name,
            "description": info.get("description") or "",
            "kind": "audio" if has_audio and not has_video else "video",
            "needs_processing": class_name in PROCESSING_EFFECTS,
            "tracks_objects": bool(info.get("has_tracked_object")),
            "properties": props,
        }
    return out


def catalog() -> dict:
    """The effect catalog (cached after the first call; libopenshot's list does not change)."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _build_catalog(_load_raw_catalog())
    return _CATALOG


def _norm(text: Any) -> str:
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def resolve_effect_name(name: str) -> str:
    """Class name, display name ('Chroma Key (Greenscreen)') or alias ('greenscreen') -> class name."""
    cat = catalog()
    key = _norm(name)
    if not key:
        raise ToolError("name the effect (see list_effects_tool)")
    for cn, spec in cat.items():
        if key in (_norm(cn), _norm(spec["name"])):
            return cn
    alias = _ALIASES.get(key)
    if alias in cat:
        return alias
    for cn, spec in cat.items():  # "Chroma Key" matches "Chroma Key (Greenscreen)"
        if _norm(spec["name"]).startswith(key) and len(key) >= 4:
            return cn
    close = difflib.get_close_matches(str(name), list(cat), n=4, cutoff=0.5)
    hint = f"; did you mean {', '.join(close)}?" if close else ""
    raise ToolError(f"unknown effect {name!r}{hint} list_effects_tool lists all {len(cat)} effects")


def effect_kind(class_name: str) -> str:
    return catalog().get(class_name, {}).get("kind", "video")


def new_effect_json(class_name: str) -> dict:
    try:
        return effect_ops.create_effect_json(class_name)
    except RuntimeError as exc:
        raise ToolError(f"this libopenshot build cannot create a {class_name} effect ({exc})") from None


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

def clip_has_picture(data: dict) -> bool:
    reader = data.get("reader") or {}
    if reader.get("media_type") == "audio":
        return False
    has_video = reader.get("has_video")
    return True if has_video is None else bool(has_video)


def clip_has_sound(data: dict) -> bool:
    reader = data.get("reader") or {}
    if reader.get("media_type") == "image":
        return False
    has_audio = reader.get("has_audio")
    return True if has_audio is None else bool(has_audio)


def _clip_label(clip) -> str:
    data = clip.data
    return f"{clip.id} ({data.get('title') or 'clip'}, track {ui_track_number(int(data.get('layer') or 0))})"


def compatibility_problem(clip, class_name: str) -> Optional[str]:
    """Why *class_name* cannot go on this clip, or None."""
    data = clip.data
    if is_locked(int(data.get("layer") or 0)):
        return f"clip {_clip_label(clip)} is on a locked track; unlock the track first"
    if effect_kind(class_name) == "audio":
        if not clip_has_sound(data):
            return f"{class_name} is an audio effect and clip {_clip_label(clip)} has no sound"
    elif not clip_has_picture(data) and class_name not in AUDIO_REACTIVE_EFFECTS:
        return f"{class_name} is a video effect and clip {_clip_label(clip)} is audio-only"
    return None


def target_clips(timeline_clip_id="", clip_query="", track="", timeline_clip_ids=None, scope="",
                 occurrence=0, position_near=None) -> tuple:
    """(clips, explicit): explicit ids/query/single clip, or a selection/track/all scope."""
    ids = [str(i).strip() for i in (timeline_clip_ids or []) if str(i).strip()]
    if timeline_clip_id:
        ids = [str(timeline_clip_id).strip()] + [i for i in ids if i != str(timeline_clip_id).strip()]
    if ids:
        return resolve_clips(ids), True
    if scope:
        clips = resolve_clips(scope=scope, track=track)
        return clips, scope == "selected"
    return [resolve_clip("", clip_query, track, occurrence=occurrence or 0, position_near=position_near)], True


def split_compatible(clips, explicit, class_name) -> tuple:
    """Compatible clips + skipped notes; any problem on an explicitly named clip is an error."""
    ok_clips, skipped = [], []
    for clip in clips:
        problem = compatibility_problem(clip, class_name)
        if problem:
            if explicit:
                raise ToolError(problem)
            skipped.append({"timeline_clip_id": clip.id, "reason": problem})
        else:
            ok_clips.append(clip)
    if not ok_clips:
        raise ToolError(skipped[0]["reason"] if skipped else "no clips to change")
    return ok_clips, skipped


def clip_effects(clip) -> list:
    effects = clip.data.get("effects")
    return [e for e in effects if isinstance(e, dict)] if isinstance(effects, list) else []


def find_effect(effect_id: str):
    """(clip QueryObject, effect dict) for an effect id anywhere on the timeline."""
    from classes.query import Clip
    eid = str(effect_id or "").strip()
    for clip in Clip.filter():
        for effect in clip_effects(clip):
            if str(effect.get("id")) == eid:
                return clip, effect
    raise ToolError(f"no effect with id={effect_id!r} on the timeline (get_clip_effects_tool lists them)")


# How long an edit waits for a busy GUI thread. The hop itself is a few project updates.
EDIT_HOP_TIMEOUT = 120


def apply_on_main(func, timeout: float = EDIT_HOP_TIMEOUT):
    """Run the edit *func* on the GUI thread; if the caller gives up waiting, the edit never runs.

    ``on_main`` leaves a timed-out call queued, so a slow GUI thread would apply the
    edit after the tool already answered "Error" -- the model then retries or reports a
    failure for a change that happened. The guard makes the timeout mean "nothing
    changed": a queued edit that starts after the deadline is dropped.
    """
    lock = threading.Lock()
    state = {"cancelled": False, "started": False}

    def _guarded():
        with lock:
            if state["cancelled"]:
                return None
            state["started"] = True
        return func()

    try:
        return on_main(_guarded, timeout=timeout)
    except Exception as exc:
        if type(exc).__name__ != "MainThreadTimeout":
            raise
        with lock:
            state["cancelled"] = True
            started = state["started"]
        if started:  # it began right at the deadline and will finish on the GUI thread
            return None
        raise ToolError(f"the editor's GUI thread stayed busy for {timeout:g}s, so nothing was changed; "
                        "it is safe to try again") from None


def save_clip_effects(changes: list) -> None:
    """Write [(clip_id, new_effects_list)] on the GUI thread: one update per clip, one undo step per call."""
    from classes.query import Clip

    def _apply():
        missing = [cid for cid, _e in changes if not Clip.get(id=cid)]
        if missing:
            raise ToolError(f"clip {missing[0]} was removed while the tool ran; nothing changed")
        updates = get_app().updates
        for cid, effects in changes:
            updates.update(["clips", {"id": cid}], {"effects": effects})
        refresh_preview()

    apply_on_main(_apply)


def save_effect_properties(changes: list) -> None:
    """Write [(clip_id, effect_id, {key: value})] with partial effect updates (Properties dock path)."""
    def _apply():
        updates = get_app().updates
        for cid, eid, values in changes:
            updates.update(["clips", {"id": cid}, "effects", {"id": eid}], values)
        refresh_preview()

    apply_on_main(_apply)


# ---------------------------------------------------------------------------
# Property values
# ---------------------------------------------------------------------------

def _choice_value(pname, spec, value):
    choices = spec.get("choices") or []
    if isinstance(value, str):
        key = _norm(value)
        for c in choices:
            if _norm(c["name"]) == key:
                return c["value"]
        for c in choices:
            if key and _norm(c["name"]).startswith(key):
                return c["value"]
        try:
            value = float(value)
        except ValueError:
            names = ", ".join(str(c["name"]) for c in choices)
            raise ToolError(f"{pname} must be one of: {names}; got {value!r}") from None
    if isinstance(value, bool):
        value = int(value)
    for c in choices:
        if float(c["value"]) == float(value):
            return c["value"]
    names = ", ".join(f"{c['name']}={c['value']}" for c in choices)
    raise ToolError(f"{pname} must be one of: {names}; got {value!r}")


def _number(pname, spec, value):
    if spec.get("choices"):
        return float(_choice_value(pname, spec, value))
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "on"):
            return 1.0
        if s in ("false", "no", "off"):
            return 0.0
        try:
            value = float(s.rstrip("s%").strip())
        except ValueError:
            raise ToolError(f"{pname} must be a number, got {value!r}") from None
    if not isinstance(value, (int, float)):
        raise ToolError(f"{pname} must be a number, got {value!r}")
    value = float(value)
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and hi is not None and not (lo - 1e-9 <= value <= hi + 1e-9):
        raise ToolError(f"{pname} must be between {lo:g} and {hi:g}, got {value:g}")
    return value


def _keyframe_list(value):
    """Keyframe input -> [(time, value, interpolation|None)] or None when *value* is static."""
    if isinstance(value, dict) and "keyframes" in value:
        value = value["keyframes"]
    if not isinstance(value, list):
        return None
    out = []
    for item in value:
        if isinstance(item, dict):
            if "time" not in item or "value" not in item:
                raise ToolError('each keyframe needs "time" (timeline seconds) and "value"')
            out.append((item["time"], item["value"], item.get("interpolation")))
        elif isinstance(item, (list, tuple)) and len(item) in (2, 3):
            out.append((item[0], item[1], item[2] if len(item) == 3 else None))
        else:
            raise ToolError('keyframes are [{"time": seconds, "value": v, "interpolation": "linear"}...]')
    if not out:
        raise ToolError("the keyframe list is empty")
    return out


def _keyframe_frame(clip_data, t) -> int:
    try:
        t = float(t)
    except (TypeError, ValueError):
        raise ToolError(f"keyframe time must be timeline seconds, got {t!r}") from None
    start, end, _d = clip_extent(clip_data)
    slack = 1.0 / project_fps()
    if t < start - slack or t > end + slack:
        raise ToolError(f"keyframe time {t:g}s is outside the clip ({start:g}-{end:g}s on the timeline)")
    return clip_frame_at(clip_data, min(max(t, start), end))


def _points(clip_data, pname, spec, kfs, default_interp, convert):
    pts = {}
    for t, v, interp in kfs:
        frame = _keyframe_frame(clip_data, t)
        code = interpolation_code(interp) if interp not in (None, "") else default_interp
        pts[frame] = (frame, convert(v), code)
    return [pts[k] for k in sorted(pts)]


def _file_reader(value):
    from classes.query import File
    key = str(value or "").strip()
    if key.lower() in ("", "none"):
        return {"type": ""}
    f = File.get(id=key)
    if not f:
        for cand in File.filter():
            path = str(cand.data.get("path") or "")
            if os.path.basename(path).lower() == key.lower() or path == key:
                f = cand
                break
    if not f:
        raise ToolError(f"no project file {value!r} for the image/video input (use a file id from list_files_tool)")
    return copy.deepcopy(f.data)


def convert_property(class_name: str, pname: str, value: Any, effect_json: dict, clip_data: dict,
                     default_interp: int = BEZIER, luts=None) -> Any:
    """A model-supplied value -> the effect JSON value for *pname* (validated; nothing mutated)."""
    spec = catalog()[class_name]["properties"].get(pname)
    if spec is None:
        if pname in HIDDEN_PROPERTIES:
            raise ToolError(f"{pname} is not settable on an effect (effects follow their clip)")
        names = sorted(catalog()[class_name]["properties"])
        close = difflib.get_close_matches(pname, names, n=3, cutoff=0.5)
        hint = f" (did you mean {', '.join(close)}?)" if close else ""
        raise ToolError(f"{class_name} has no property {pname!r}{hint}; it has: {', '.join(names)}")
    ptype = spec["type"]
    current = effect_json.get(pname)
    kfs = _keyframe_list(value) if ptype not in ("colorgrade_curve", "colorgrade_wheels") else None

    if ptype == "color":
        if kfs:
            parsed = [(t, parse_color(v), i) for t, v, i in kfs]
            out = {}
            for idx, channel in enumerate(("red", "green", "blue", "alpha")):
                pts = _points(clip_data, pname, spec, [(t, c[idx], i) for t, c, i in parsed],
                              default_interp, float)
                out[channel] = {"Points": [_point(*p) for p in pts]}
            return out
        return color_keyframe(value)

    if ptype in ("float", "int", "bool"):
        keyframed = _is_keyframe(current) or (current is None and spec.get("keyframable"))
        if keyframed:
            interp = CONSTANT if spec.get("choices") else default_interp
            if kfs:
                pts = _points(clip_data, pname, spec, kfs, interp, lambda v: _number(pname, spec, v))
                return {"Points": [_point(*p) for p in pts]}
            return {"Points": [_point(1, _number(pname, spec, value), interp)]}
        if kfs:
            raise ToolError(f"{pname} cannot be keyframed; give one value")
        num = _number(pname, spec, value)
        if isinstance(current, bool) or ptype == "bool":
            return bool(num)
        if isinstance(current, int) or ptype == "int":
            return int(round(num))
        return num

    if kfs:
        raise ToolError(f"{pname} cannot be keyframed; give one value")
    if ptype == "string":
        if pname == "lut_path":
            try:
                return effect_ops.resolve_lut(value, luts)
            except ValueError as exc:
                raise ToolError(str(exc)) from None
        return "" if value is None else str(value)
    if ptype in ("font", "caption"):
        return str(value)
    if ptype == "reader":
        return _file_reader(value)
    if ptype == "colorgrade_curve":
        return curve_value(pname, value)
    if ptype == "colorgrade_wheels":
        return wheels_value(value, current)
    raise ToolError(f"{pname} ({ptype}) is not settable by tools; use the Properties dock")


def _point(frame, value, interpolation=BEZIER) -> dict:
    return {"co": {"X": float(frame), "Y": float(value)}, "interpolation": int(interpolation),
            "handle_type": 0, "handle_left": {"X": 0.5, "Y": 1.0}, "handle_right": {"X": 0.5, "Y": 0.0}}


def curve_value(pname: str, value: Any) -> dict:
    """[[x, y], ...] or [{"x", "y"}...] (0-1, input -> output brightness) -> a Color Grade curve."""
    from classes.color_presets import curve_data, default_curve_data
    if isinstance(value, dict) and "nodes" in value:
        return value
    if value in (None, "", "reset", "none", "linear"):
        return default_curve_data()
    if not isinstance(value, list) or len(value) < 2:
        raise ToolError(f"{pname} needs at least 2 points like [[0,0],[0.5,0.6],[1,1]] (x = input, y = output, 0-1)")
    pts = []
    for p in value:
        if isinstance(p, dict):
            x, y = p.get("x"), p.get("y")
        elif isinstance(p, (list, tuple)) and len(p) == 2:
            x, y = p
        else:
            raise ToolError(f"{pname} points are [x, y] pairs in 0-1")
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            raise ToolError(f"{pname} points are [x, y] pairs in 0-1") from None
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ToolError(f"{pname} point ({x:g}, {y:g}) is outside 0-1")
        pts.append({"x": x, "y": y})
    pts.sort(key=lambda p: p["x"])
    if len({p["x"] for p in pts}) != len(pts):
        raise ToolError(f"{pname} has two points with the same x")
    if len(pts) > 16:
        raise ToolError(f"{pname} takes at most 16 points")
    return curve_data(pts)


WHEELS = ("global", "shadows", "midtones", "highlights")


def wheels_value(value: Any, current: Any) -> dict:
    """{"shadows": {"color": "#2040ff", "amount": 0.2, "luma": -0.05}, ...} merged onto the current wheels."""
    from classes.color_presets import default_wheels_data, wheel_entry
    if value in (None, "", "reset", "none"):
        return default_wheels_data()
    if not isinstance(value, dict):
        raise ToolError('wheels is {"shadows": {"color": "#hex", "amount": 0-1, "luma": -1..1}, ...}')
    out = copy.deepcopy(current) if isinstance(current, dict) and current.get("global") else default_wheels_data()
    for name, wheel in value.items():
        if name == "enabled":
            out["enabled_keyframes"] = constant_keyframe(1.0 if wheel else 0.0)
            continue
        if name not in WHEELS:
            raise ToolError(f"unknown color wheel {name!r}; use {', '.join(WHEELS)}")
        if not isinstance(wheel, dict):
            raise ToolError(f"wheel {name} needs {{color, amount, luma}}")
        prev = out.get(name) or {}
        color = wheel.get("color", prev.get("color", "#ffffff"))
        r, g, b, _a = parse_color(color)
        try:
            amount = float(wheel.get("amount", prev.get("amount", 0.0)))
            luma = float(wheel.get("luma", prev.get("luma", 0.0)))
        except (TypeError, ValueError):
            raise ToolError(f"wheel {name}: amount and luma are numbers") from None
        if not 0 <= amount <= 1:
            raise ToolError(f"wheel {name}: amount must be 0-1, got {amount:g}")
        if not -1 <= luma <= 1:
            raise ToolError(f"wheel {name}: luma must be -1..1, got {luma:g}")
        out[name] = wheel_entry("#%02x%02x%02x" % (r, g, b), amount, luma)
    return out


def prepare_properties(class_name: str, properties: Optional[dict], effect_json: dict, clip_data: dict,
                       interpolation: str = "bezier", luts=None) -> dict:
    """Validate and convert every property BEFORE anything is saved; returns {key: json_value}."""
    if properties is None:
        return {}
    if not isinstance(properties, dict):
        raise ToolError('properties is an object like {"horizontal_radius": 12, "sigma": 4}')
    default_interp = interpolation_code(interpolation)
    return {str(k): convert_property(class_name, str(k), v, effect_json, clip_data, default_interp, luts)
            for k, v in properties.items()}


def needs_luts(properties: Optional[dict]) -> bool:
    return isinstance(properties, dict) and "lut_path" in properties


def effect_value_summary(class_name: str, effect: dict, clip_data: dict) -> dict:
    """Current values of an effect's settable properties (keyframed ones as timed points)."""
    spec = catalog().get(class_name, {}).get("properties", {})
    fps = project_fps()
    position = float(clip_data.get("position") or 0.0)
    start = float(clip_data.get("start") or 0.0)
    out = {}
    for pname, pspec in spec.items():
        if pname in COMMON_PROPERTIES and pname not in ("mask_source_id", "mask_invert"):
            continue
        value = effect.get(pname)
        if _is_keyframe(value):
            pts = value.get("Points") or []
            if len(pts) <= 1:
                out[pname] = round(float(pts[0]["co"]["Y"]), 4) if pts else pspec.get("default")
            else:
                out[pname] = {"keyframes": [
                    {"time": round(position + (float(p["co"]["X"]) - 1) / fps - start, 3),
                     "value": round(float(p["co"]["Y"]), 4)} for p in pts]}
        elif _is_color(value):
            out[pname] = _color_hex(value)
        elif pspec["type"] in ("colorgrade_curve", "colorgrade_wheels", "reader"):
            out[pname] = _rich_summary(pspec["type"], value)
        elif isinstance(value, (bool, int, float, str)):
            out[pname] = value
    return out


def _rich_summary(ptype, value):
    if not isinstance(value, dict):
        return None
    if ptype == "reader":
        return os.path.basename(str(value.get("path") or "")) or None
    if ptype == "colorgrade_curve":
        nodes = value.get("nodes") or []
        return [[round(keyframe_value_at(n.get("x"), 1), 3), round(keyframe_value_at(n.get("y"), 1), 3)]
                for n in nodes]
    return {w: {"color": (value.get(w) or {}).get("color"), "amount": (value.get(w) or {}).get("amount"),
                "luma": (value.get(w) or {}).get("luma")} for w in WHEELS if isinstance(value.get(w), dict)}


# ---------------------------------------------------------------------------
# Schema fragments
# ---------------------------------------------------------------------------

PROPERTIES_ARG = mapping(
    "Effect properties to set, as {name: value}. A value is a number, true/false, a choice name "
    "(\"HSV/HSL hue\"), a color (\"#00ff00\") or text; or keyframes [{\"time\": 2.0, \"value\": 10, "
    "\"interpolation\": \"linear\"}] in timeline seconds to animate it. list_effects_tool(effect=...) "
    "lists every property with its range, default and choices.")
INTERPOLATION_ARG = enum(["bezier", "linear", "constant"],
                         "Default interpolation for keyframes that do not name one.", "bezier")
TARGETS = {
    **CLIP_TARGET,
    "timeline_clip_ids": array({"type": "string"}, "Several timeline clip ids to change in one step."),
    "scope": enum(["", "selected", "track", "all"],
                  "Instead of ids/query: 'selected' = timeline selection, 'track' = every clip on `track`, "
                  "'all' = every clip. Clips that cannot take the effect (locked track, no picture/sound) are "
                  "skipped and listed.", ""),
}


# ---------------------------------------------------------------------------
# Catalog tools
# ---------------------------------------------------------------------------

@editor_tool(
    "list_effects_tool",
    label="List effects",
    schema=obj({
        "kind": enum(["all", "video", "audio"], "Only video (picture) or audio (sound) effects.", "all"),
        "query": string("Filter by words in the name or description ('blur', 'color', 'voice').", ""),
        "effect": string("One effect (class or display name) to describe in full, with every property.", ""),
        "include_properties": boolean("Include each listed effect's properties (ranges, defaults, choices).", False),
    }),
    read_only=True,
    covers=("effect.catalog",),
)
def list_effects(kind="all", query="", effect="", include_properties=False):
    """List the effects Zenvi can put on clips (libopenshot 1.0's full set: 34 video, 9 audio) with what they
    do, and, for one effect or with include_properties, every property with its range, default and choices.

    Use this to find the right effect and the exact property names before add_effect_tool /
    update_effect_tool. For common requests prefer the intent tools: color_grade_clip_tool (grades,
    LUTs, black & white), apply_look_preset_tool (film grain, VHS, glow...), chroma_key_clip_tool
    (green screen), process_clip_effect_tool (stabilize, track, detect objects), apply_audio_effect_tool
    (compressor, echo, EQ, robot voice). Effects marked needs_processing analyse the clip first.
    Example: list_effects_tool(effect="Blur") -> horizontal_radius 0-100, sigma, iterations, mask_mode...
    """
    cat = catalog()
    if effect:
        cn = resolve_effect_name(effect)
        spec = cat[cn]
        return ok(f"{spec['name']} ({cn}): {spec['description']}", effect=spec,
                  common_properties=_common_properties())
    q = _norm(query)
    words = [w for w in str(query or "").lower().split() if w]
    rows = []
    for cn, spec in sorted(cat.items(), key=lambda kv: (kv[1]["kind"], kv[0])):
        if kind != "all" and spec["kind"] != kind:
            continue
        hay = f"{cn} {spec['name']} {spec['description']}".lower()
        if q and not (q in _norm(hay) or all(w in hay for w in words)):
            continue
        row = {k: spec[k] for k in ("effect", "name", "description", "kind")}
        if spec["needs_processing"]:
            row["needs_processing"] = True
        if include_properties:
            row["properties"] = {k: v for k, v in spec["properties"].items() if k not in COMMON_PROPERTIES}
        else:
            row["property_names"] = [k for k in spec["properties"] if k not in COMMON_PROPERTIES]
        rows.append(row)
    if not rows:
        raise ToolError(f"no {kind if kind != 'all' else ''} effect matches {query!r}".replace("  ", " "))
    return ok(f"{len(rows)} effect(s)" + (f" matching {query!r}" if query else ""), effects=rows,
              common_properties=_common_properties() if include_properties else list(COMMON_PROPERTIES))


def _common_properties() -> dict:
    sample = catalog().get("Blur") or next(iter(catalog().values()))
    out = {k: v for k, v in sample["properties"].items() if k in COMMON_PROPERTIES}
    out["mask_source_id"] = {"type": "string", "about": "effect id of a Tracker / Object Detector / Object Mask "
                             "effect: limit this effect to the tracked region (mask_invert=1 for outside it)"}
    return out


@editor_tool(
    "get_clip_effects_tool",
    label="Clip effects",
    schema=obj({**TARGETS,
                "detail": enum(["full", "summary"], "full = current property values too; summary = ids only.",
                               "full")}),
    read_only=True,
    covers=("effect.update", "effect.catalog"),
)
def get_clip_effects(timeline_clip_id="", clip_query="", track="", timeline_clip_ids=None, scope="",
                     detail="full"):
    """List the effects already applied to clip(s): effect ids, class, name, and their current property
    values (keyframed properties as timed points). Use it before update_effect_tool / remove_effect_tool
    to get effect ids, or to answer "what effects are on this clip?". Timeline listings do not show
    effects; this does.
    """
    clips, _explicit = target_clips(timeline_clip_id, clip_query, track, timeline_clip_ids, scope)
    rows = []
    total = 0
    for clip in clips:
        effects = []
        for e in clip_effects(clip):
            cn = str(e.get("class_name") or e.get("type") or "")
            row = {"effect_id": e.get("id"), "effect": cn, "name": e.get("name") or cn,
                   "kind": effect_kind(cn)}
            if e.get("ui-menu") == "look":
                row["look_preset"] = True
            if detail == "full" and cn in catalog():
                row["values"] = effect_value_summary(cn, e, clip.data)
            effects.append(row)
        total += len(effects)
        rows.append({"timeline_clip_id": clip.id, "title": clip.data.get("title"),
                     "track": ui_track_number(int(clip.data.get("layer") or 0)), "effects": effects})
    summary = f"{total} effect(s) on {len(rows)} clip(s)"
    if len(rows) == 1:
        names = ", ".join(e["effect"] for e in rows[0]["effects"]) or "none"
        summary = f"Clip {rows[0]['timeline_clip_id']} has {total} effect(s): {names}"
    return ok(summary, clips=rows)


# ---------------------------------------------------------------------------
# Add / update / remove / copy
# ---------------------------------------------------------------------------

def _add_or_update_plan(clip, class_name, properties, if_exists, interpolation, luts):
    """New effects list for one clip + the receipt row (validation only; nothing saved)."""
    effects = copy.deepcopy(clip_effects(clip))
    existing = [i for i, e in enumerate(effects) if e.get("class_name") == class_name]
    if existing and if_exists == "update":
        idx = existing[0]
        values = prepare_properties(class_name, properties, effects[idx], clip.data, interpolation, luts)
        changed = {k: v for k, v in values.items() if effects[idx].get(k) != v}
        effects[idx].update(changed)
        return effects, {"timeline_clip_id": clip.id, "effect_id": effects[idx].get("id"),
                         "action": "updated" if changed else "unchanged"}, bool(changed)
    effect_json = new_effect_json(class_name)
    effect_json.update(prepare_properties(class_name, properties, effect_json, clip.data, interpolation, luts))
    effects.append(effect_json)
    return effects, {"timeline_clip_id": clip.id, "effect_id": effect_json.get("id"), "action": "added"}, True


@editor_tool(
    "add_effect_tool",
    label="Add effect",
    schema=obj({
        **TARGETS,
        "effect": string("Effect to add: class or display name from list_effects_tool (Blur, Sharpen, "
                         "ChromaKey, ColorGrade, Pixelate, Compressor, Echo, ParametricEQ...).", ""),
        "effect_name": string("Same as effect (either works).", ""),
        "occurrence": integer("With clip_query: take the Nth match (1 = first). 0 = the best match.", 0, minimum=0),
        "position_near": nullable(number("With clip_query: prefer the clip nearest this timeline time (seconds).")),
        "properties": PROPERTIES_ARG,
        "interpolation": INTERPOLATION_ARG,
        "if_exists": enum(["update", "add"],
                          "When the clip already has this effect: 'update' changes that one (default, "
                          "avoids stacking duplicates); 'add' stacks another copy.", "update"),
        "processing": mapping(
            "Only for Stabilizer, Tracker, ObjectDetection, ObjectMask: the analysis options, same names as "
            "process_clip_effect_tool (smoothing, region, region_time, tracker_type, model, device, "
            "download_model, class_filter, confidence, points, negative_points, boxes, mask_quality)."),
    }),
    background_safe=True,
    covers=("effect.add", "audio.effects", "effect.process"),
)
def add_effect(timeline_clip_id="", clip_query="", track="", timeline_clip_ids=None, scope="",
               effect="", effect_name="", occurrence=0, position_near=None, properties=None,
               interpolation="bezier", if_exists="update", processing=None):
    """Add any effect to one or more clips, optionally with its properties set or keyframed, as one
    undo step. Use when the user names an effect ("add a blur", "pixelate it", "add a compressor to the
    voice", "negative", "wave distortion") or when no intent tool fits.

    Prefer: color_grade_clip_tool for grading/LUTs/black & white, apply_look_preset_tool for the Look
    menu presets (film grain, VHS/analog tape, glow, shadow, soft focus), chroma_key_clip_tool for green
    screen, apply_audio_effect_tool for voice/music presets, process_clip_effect_tool for Stabilizer /
    Tracker / Object Detector / Object Mask (add_effect_tool also runs those, with `processing`).

    properties: {"horizontal_radius": 12, "vertical_radius": 12} or keyframes
    {"pixelization": [{"time": 1, "value": 0.9}, {"time": 3, "value": 0}]} (timeline seconds).
    To limit an effect to a tracked object (blur a face/plate): properties
    {"mask_source_id": "<Tracker/ObjectDetection effect id>"} (add "mask_invert": 1 for everything else).
    Refused: locked track, a video effect on an audio-only clip, an audio effect on a clip without sound,
    unknown effect/property, out-of-range values. Returns effect ids for update_effect_tool.
    """
    name = str(effect or effect_name or "").strip()
    if not name:
        raise ToolError("add_effect_tool needs effect (e.g. \"Blur\"; list_effects_tool lists them)")
    class_name = resolve_effect_name(name)
    if if_exists not in ("update", "add"):
        raise ToolError("if_exists must be 'update' or 'add'")
    if class_name in PROCESSING_EFFECTS:
        from classes.editor_tools import effects_color_process as proc
        many = [i for i in (timeline_clip_ids or []) if str(i).strip()]
        if scope or len(many) > 1 or (many and timeline_clip_id and many[0] != timeline_clip_id):
            raise ToolError(f"{class_name} analyses one clip at a time; name one clip")
        clip_id = timeline_clip_id or (many[0] if many else "")
        opts = dict(processing or {}) if isinstance(processing, dict) else {}
        if processing not in (None, {}) and not isinstance(processing, dict):
            raise ToolError("processing is an object of process_clip_effect_tool options")
        return proc.run_processing_tool(
            clip_id, clip_query, track, class_name, opts, properties, interpolation,
            occurrence=occurrence, position_near=position_near)
    if processing:
        raise ToolError(f"processing options only apply to {', '.join(PROCESSING_EFFECTS)}")
    clips, explicit = target_clips(timeline_clip_id, clip_query, track, timeline_clip_ids, scope,
                                   occurrence, position_near)
    clips, skipped = split_compatible(clips, explicit, class_name)
    luts = effect_ops.list_luts() if needs_luts(properties) else None
    changes, rows = [], []
    for clip in clips:
        effects, row, changed = _add_or_update_plan(clip, class_name, properties, if_exists, interpolation, luts)
        rows.append(row)
        if changed:
            changes.append((clip.id, effects))
    display = catalog()[class_name]["name"]
    if not changes:
        return ok(f"{display} is already on {len(rows)} clip(s) with these settings; nothing changed.",
                  effect=class_name, changed=False, clips=rows, skipped=skipped)
    save_clip_effects(changes)
    added = sum(1 for r in rows if r["action"] == "added")
    updated = sum(1 for r in rows if r["action"] == "updated")
    parts = [f"added {display} to {added} clip(s)" if added else "",
             f"updated the existing {display} on {updated} clip(s)" if updated else ""]
    summary = " and ".join(p for p in parts if p)
    summary = summary[:1].upper() + summary[1:] + "."
    if skipped:
        summary += f" Skipped {len(skipped)} clip(s)."
    first = rows[0]
    return ok(summary, effect=class_name, effect_id=first["effect_id"], timeline_clip_id=first["timeline_clip_id"],
              clips=rows, skipped=skipped, changed=True)


def _effects_to_edit(effect_id, timeline_clip_id, clip_query, track, timeline_clip_ids, scope, effect,
                     occurrence):
    """[(clip, effect_dict)] targeted by id, or by clip(s) + effect class (+ Nth of that class)."""
    if effect_id:
        clip, eff = find_effect(effect_id)
        if is_locked(int(clip.data.get("layer") or 0)):
            raise ToolError(f"clip {_clip_label(clip)} is on a locked track; unlock the track first")
        return [(clip, eff)], []
    if not effect:
        raise ToolError("give effect_id (from get_clip_effects_tool), or a clip plus the effect name")
    class_name = resolve_effect_name(effect)
    clips, explicit = target_clips(timeline_clip_id, clip_query, track, timeline_clip_ids, scope)
    out, skipped = [], []
    for clip in clips:
        if is_locked(int(clip.data.get("layer") or 0)):
            msg = f"clip {_clip_label(clip)} is on a locked track; unlock the track first"
            if explicit:
                raise ToolError(msg)
            skipped.append({"timeline_clip_id": clip.id, "reason": msg})
            continue
        matches = [e for e in clip_effects(clip) if e.get("class_name") == class_name]
        if not matches:
            msg = f"clip {_clip_label(clip)} has no {class_name} effect (add it with add_effect_tool)"
            if explicit:
                raise ToolError(msg)
            skipped.append({"timeline_clip_id": clip.id, "reason": msg})
            continue
        if occurrence and occurrence > len(matches):
            raise ToolError(f"clip {clip.id} has only {len(matches)} {class_name} effect(s)")
        out.append((clip, matches[(occurrence or 1) - 1]))
    if not out:
        raise ToolError(skipped[0]["reason"] if skipped else "no effect to change")
    return out, skipped


@editor_tool(
    "update_effect_tool",
    label="Change effect",
    schema=obj({
        "effect_id": string("Effect id from get_clip_effects_tool / add_effect_tool. Preferred.", ""),
        **TARGETS,
        "effect": string("Instead of effect_id: the effect name on the clip(s) (Blur, ColorGrade...).", ""),
        "occurrence": integer("With effect: which one if a clip has several of that effect (1 = first).", 0,
                              minimum=0),
        "properties": PROPERTIES_ARG,
        "interpolation": INTERPOLATION_ARG,
    }, required=["properties"]),
    background_safe=True,
    covers=("effect.update",),
)
def update_effect(*, properties, effect_id="", timeline_clip_id="", clip_query="", track="",
                  timeline_clip_ids=None, scope="", effect="", occurrence=0, interpolation="bezier"):
    """Change properties of an effect that is already on a clip: static values, or keyframes to animate
    them ("make the blur stronger", "fade the pixelation out over 2 seconds", "set the echo mix to 0.2").
    Target one effect by effect_id, or name the effect class on one or several clips (scope='all' for
    "every clip's blur"). Only the given properties change; one undo step.

    Keyframes: {"sigma": [{"time": 0, "value": 8}, {"time": 2, "value": 0, "interpolation": "linear"}]}
    with timeline seconds inside the clip. Colors: "#RRGGBB". Choices by name ("Basic keying").
    For ColorGrade prefer color_grade_clip_tool. Refused: unknown or read-only property, out-of-range
    value, locked track, a clip without that effect.
    """
    if not isinstance(properties, dict) or not properties:
        raise ToolError('properties is required, e.g. {"sigma": 6}')
    targets, skipped = _effects_to_edit(effect_id, timeline_clip_id, clip_query, track, timeline_clip_ids,
                                        scope, effect, occurrence)
    luts = effect_ops.list_luts() if needs_luts(properties) else None
    changes, rows = [], []
    for clip, eff in targets:
        cn = str(eff.get("class_name") or "")
        if cn not in catalog():
            raise ToolError(f"effect {eff.get('id')} ({cn}) is not in this libopenshot build")
        values = prepare_properties(cn, properties, eff, clip.data, interpolation, luts)
        changed = {k: v for k, v in values.items() if eff.get(k) != v}
        rows.append({"timeline_clip_id": clip.id, "effect_id": eff.get("id"), "effect": cn,
                     "changed": sorted(changed)})
        if changed:
            changes.append((clip.id, eff.get("id"), changed))
    if not changes:
        return ok("Those values are already set; nothing changed.", changed=False, effects=rows)
    save_effect_properties(changes)
    names = sorted({k for _c, _e, v in changes for k in v})
    return ok(f"Updated {', '.join(names)} on {len(changes)} effect(s).", changed=True, effects=rows,
              skipped=skipped)


@editor_tool(
    "remove_effect_tool",
    label="Remove effect",
    schema=obj({
        "effect_ids": array({"type": "string"}, "Effect ids to remove (get_clip_effects_tool lists them)."),
        **TARGETS,
        "effect": string("Instead of ids: remove this effect (Blur, ChromaKey...) from the target clip(s).", ""),
        "all_effects": boolean("Remove every effect from the target clip(s).", False),
    }),
    covers=("effect.remove",),
)
def remove_effect(effect_ids=None, timeline_clip_id="", clip_query="", track="", timeline_clip_ids=None,
                  scope="", effect="", all_effects=False):
    """Remove effects from clips (effect badge > Remove Effect). It never deletes timeline clips; to
    delete clips use delete_from_timeline_tool. Target effect ids, or an effect name on clip(s)
    ("remove the blur", "take the green screen key off"), or all_effects=true to strip every effect.
    For Look presets you can also use apply_look_preset_tool(look="reset_look"). One undo step.
    """
    from classes.query import Clip
    per_clip: dict = {}
    order: list = []
    ids = [str(i).strip() for i in (effect_ids or []) if str(i).strip()]
    if ids:
        for eid in ids:
            clip, _eff = find_effect(eid)
            if is_locked(int(clip.data.get("layer") or 0)):
                raise ToolError(f"clip {_clip_label(clip)} is on a locked track; unlock the track first")
            if clip.id not in per_clip:
                order.append(clip.id)
            per_clip.setdefault(clip.id, set()).add(eid)
    else:
        if not effect and not all_effects:
            raise ToolError("give effect_ids, an effect name with the clip(s), or all_effects=true")
        class_name = resolve_effect_name(effect) if effect else None
        clips, explicit = target_clips(timeline_clip_id, clip_query, track, timeline_clip_ids, scope)
        for clip in clips:
            if is_locked(int(clip.data.get("layer") or 0)):
                if explicit:
                    raise ToolError(f"clip {_clip_label(clip)} is on a locked track; unlock the track first")
                continue
            hits = {str(e.get("id")) for e in clip_effects(clip)
                    if class_name is None or e.get("class_name") == class_name}
            if hits:
                order.append(clip.id)
                per_clip[clip.id] = hits
    if not per_clip:
        what = f"a {effect} effect" if effect else "effects"
        return ok(f"No clip has {what} to remove; nothing changed.", changed=False, removed=[])
    changes, removed = [], []
    for cid in order:
        clip = Clip.get(id=cid)
        keep = [e for e in clip_effects(clip) if str(e.get("id")) not in per_clip[cid]]
        removed += [{"timeline_clip_id": cid, "effect_id": e.get("id"), "effect": e.get("class_name")}
                    for e in clip_effects(clip) if str(e.get("id")) in per_clip[cid]]
        changes.append((cid, keep))
    save_clip_effects(changes)
    return ok(f"Removed {len(removed)} effect(s) from {len(changes)} clip(s).", changed=True, removed=removed)


@editor_tool(
    "copy_effects_tool",
    label="Copy effects",
    schema=obj({
        "source_clip_id": string("Clip to copy effects from (timeline clip id).", ""),
        "source_clip_query": string("Or describe the source clip.", ""),
        "timeline_clip_ids": array({"type": "string"}, "Clips to paste onto."),
        "clip_query": string("Or describe one target clip.", ""),
        "track": string("Track for clip_query or scope='track'.", ""),
        "scope": enum(["", "selected", "track", "all"],
                      "Targets instead of ids: selection, a whole track, or every clip (the source is skipped).",
                      ""),
        "effects": array({"type": "string"}, "Only these effects (names), e.g. [\"ColorGrade\"]. Empty = all."),
        "mode": enum(["merge", "replace", "append"],
                     "merge = Paste in the editor: an effect of the same kind is overwritten, others added; "
                     "replace = targets end up with exactly the copied effects; append = always add copies.",
                     "merge"),
    }),
    covers=("effect.copy_paste",),
)
def copy_effects(source_clip_id="", source_clip_query="", timeline_clip_ids=None, clip_query="", track="",
                 scope="", effects=None, mode="merge"):
    """Copy the effects of one clip onto other clips (Copy > Effects, then Paste): "give all clips the same
    grade as this one", "copy the blur and sharpen to clip 3". One undo step for every target.
    mode='merge' matches the editor's Paste (same-kind effect overwritten, new ones appended); pasted
    effects get new ids.
    """
    source = resolve_clip(source_clip_id, source_clip_query, "")
    wanted = [resolve_effect_name(e) for e in (effects or []) if str(e).strip()]
    copied = [e for e in clip_effects(source) if not wanted or e.get("class_name") in wanted]
    if not copied:
        what = ", ".join(wanted) if wanted else "any effects"
        raise ToolError(f"source clip {_clip_label(source)} has no {what} to copy")
    ids = [i for i in (timeline_clip_ids or []) if str(i).strip() and str(i).strip() != source.id]
    if ids:
        clips, explicit = resolve_clips(ids), True
    elif scope:
        clips, explicit = [c for c in resolve_clips(scope=scope, track=track) if c.id != source.id], False
    elif clip_query:
        clips, explicit = [resolve_clip("", clip_query, track)], True
    else:
        raise ToolError("name the target clips (timeline_clip_ids, clip_query or scope)")
    if not clips:
        raise ToolError("no target clips other than the source")
    classes = {e.get("class_name") for e in copied}
    changes, skipped, rows = [], [], []
    for clip in clips:
        problem = next((p for p in (compatibility_problem(clip, cn) for cn in classes) if p), None)
        if problem:
            if explicit:
                raise ToolError(problem)
            skipped.append({"timeline_clip_id": clip.id, "reason": problem})
            continue
        current = clip_effects(clip)
        if mode == "merge":
            new = effect_ops.merge_effects_by_class(current, copied)
        else:
            fresh = copy.deepcopy(copied)
            effect_ops.assign_new_effect_ids(fresh)
            new = fresh if mode == "replace" else copy.deepcopy(current) + fresh
        changes.append((clip.id, new))
        rows.append({"timeline_clip_id": clip.id, "effects": [e.get("class_name") for e in new]})
    if not changes:
        raise ToolError(skipped[0]["reason"] if skipped else "no target clips")
    save_clip_effects(changes)
    return ok(f"Copied {len(copied)} effect(s) from clip {source.id} onto {len(changes)} clip(s) ({mode}).",
              source_clip_id=source.id, copied=sorted(c for c in classes if c), clips=rows, skipped=skipped)


# The Look, grade, audio and processing tools live in their own modules; importing
# them registers their tools with this workstream.
from classes.editor_tools import effects_color_looks  # noqa: E402,F401
from classes.editor_tools import effects_color_process  # noqa: E402,F401
from classes.editor_tools import effects_color_analysis  # noqa: E402,F401

