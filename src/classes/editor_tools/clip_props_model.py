"""The clip property model behind the clip-props editor tools.

What properties a timeline clip has (libopenshot's own ``Clip.PropertiesJSON``:
labels, types, ranges, choices), which of them can be animated, what values
the model may send for each (choice names like "Best Fit" or "multiply",
colors, numbers inside the editor's limits), and where a keyframe lands for a
time given in clip, timeline or source seconds.

No tools are registered here; ``clip_props`` and ``clip_props_presets`` use it.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Optional

from classes.editor_tools._base import (
    BEZIER, CONSTANT, LINEAR, ToolError, fps_fraction, keyframe_point, parse_color, project, project_fps,
)
from classes.keyframe_rules import keyframe_value, max_transform_multiple

INTERPOLATION_NAMES = {BEZIER: "bezier", LINEAR: "linear", CONSTANT: "constant"}
INTERPOLATION_CODES = {"bezier": BEZIER, "linear": LINEAR, "constant": CONSTANT}
DEFAULT_HANDLES = (0.5, 0.0, 0.5, 1.0)  # libopenshot's handle_right X/Y, handle_left X/Y

# Keys the generic tools refuse, with the reason the model is told.
TIMELINE_KEYS = ("position", "start", "end", "layer", "duration")
_REFUSED = {
    "id": "the clip id is read-only",
    "duration": "duration is read-only; trim the clip's start/end with the timeline editing tools",
    "position": "position is the clip's place on the timeline; move it with the timeline editing tools",
    "start": "start is the clip's trim in point; trim it with the timeline editing tools",
    "end": "end is the clip's trim out point; trim it with the timeline editing tools",
    "layer": "layer is the clip's track; move it to another track with the timeline editing tools",
}
_NOT_RENDERED = re.compile(r"^perspective_c[1-4]_[xy]$")

# Ranges the editor itself goes beyond libopenshot's nominal ones.
MAX_VOLUME = 1.3  # the Volume menu's 130% ceiling (audio_mix.MAX_LEVEL)
_RANGE_OVERRIDES = {
    "location_x": (-10.0, 10.0),  # slide presets move a PiP a full frame past its offset
    "location_y": (-10.0, 10.0),
    "rotation": (-3600.0, 3600.0),  # spin presets add a full turn to the current angle
    "volume": (0.0, MAX_VOLUME),
}
_SCALE_KEYS = ("scale_x", "scale_y", "shear_x", "shear_y")

_PROPERTY_ALIASES = {
    "opacity": "alpha", "blend": "composite", "blend_mode": "composite", "blendmode": "composite",
    "parent": "parentObjectId", "parent_object_id": "parentObjectId", "frame_number": "display",
    "volume_mixing": "mixing", "enable_audio": "has_audio", "enable_video": "has_video",
    "scale_mode": "scale", "fit": "scale", "anchor_point": "gravity",
}

_CHOICE_ALIASES = {
    "scale": {"fit": "Best Fit", "contain": "Best Fit", "fill": "Crop", "cover": "Crop",
              "distort": "Stretch", "original": "None"},
    "gravity": {"top": "Top Center", "bottom": "Bottom Center", "centre": "Center", "middle": "Center",
                "topmiddle": "Top Center", "bottommiddle": "Bottom Center"},
    "composite": {"over": "Normal", "sourceover": "Normal", "plus": "Add", "additive": "Add",
                  "burn": "Color Burn", "dodge": "Color Dodge", "colourburn": "Color Burn",
                  "colourdodge": "Color Dodge"},
    "has_audio": {"true": "On", "false": "Off", "yes": "On", "no": "Off", "enabled": "On", "disabled": "Off"},
    "has_video": {"true": "On", "false": "Off", "yes": "On", "no": "Off", "enabled": "On", "disabled": "Off"},
    "waveform": {"true": "Yes", "false": "No", "on": "Yes", "off": "No", "show": "Yes", "hide": "No"},
}


def _norm(text: Any) -> str:
    return re.sub(r"[\s_\-/()]+", "", str(text).strip().lower())


# ---------------------------------------------------------------------------
# libopenshot's clip schema (one detached Clip, cached)
# ---------------------------------------------------------------------------

_schema_lock = threading.Lock()
_schema_cache: Optional[tuple] = None


def _load_libopenshot_schema() -> tuple:
    """(PropertiesJSON, Json) of a detached libopenshot Clip. Tests replace this."""
    import openshot

    clip = openshot.Clip()
    return json.loads(clip.PropertiesJSON(1)), json.loads(clip.Json())


def clip_schema() -> tuple:
    global _schema_cache
    with _schema_lock:
        if _schema_cache is None:
            try:
                props, shapes = _load_libopenshot_schema()
            except Exception as exc:  # libopenshot missing or broken: say so, do not guess
                raise ToolError(f"libopenshot could not describe the clip properties ({exc})") from exc
            props = {k: v for k, v in props.items() if isinstance(v, dict)}
            _schema_cache = (props, shapes)
        return _schema_cache


def reset_schema_cache() -> None:
    global _schema_cache
    with _schema_lock:
        _schema_cache = None


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PropertyInfo:
    key: str
    label: str
    kind: str                  # float | int | color | string
    keyframable: bool
    choices: tuple             # ((name, value), ...)
    minimum: Optional[float]
    maximum: Optional[float]
    default: Any

    @property
    def is_choice(self) -> bool:
        return bool(self.choices)

    def choice_name(self, value: Any) -> Optional[str]:
        for name, val in self.choices:
            try:
                if float(val) == float(value):
                    return name
            except (TypeError, ValueError):
                continue
        return None

    def describe(self) -> dict:
        out = {"label": self.label, "type": self.kind, "keyframable": self.keyframable}
        if self.choices:
            out["choices"] = [name for name, _ in self.choices]
        elif self.minimum is not None and self.kind in ("float", "int"):
            out["min"], out["max"] = self.minimum, self.maximum
        return out


def settable_keys() -> list:
    props, _shapes = clip_schema()
    return sorted(k for k in props if k not in _REFUSED)


def resolve_key(name: Any) -> str:
    """A property key from its key, label ("Scale X") or a common alias ("opacity")."""
    props, shapes = clip_schema()
    raw = str(name or "").strip()
    if not raw:
        raise ToolError("a property name is required")
    if raw in props or raw in shapes:
        key = raw
    else:
        wanted = _norm(raw)
        key = next((k for k in props if _norm(k) == wanted), None)
        key = key or next((k for k, v in props.items() if _norm(v.get("name", "")) == wanted), None)
        key = key or _PROPERTY_ALIASES.get(raw.lower()) or _PROPERTY_ALIASES.get(wanted)
        if not key and _NOT_RENDERED.match(raw.lower()):
            key = raw.lower()
    if not key:
        raise ToolError(f"unknown clip property {raw!r}; settable: {', '.join(settable_keys())}")
    if _NOT_RENDERED.match(key):
        raise ToolError(f"{key}: libopenshot 1.0 does not render perspective corners, so the editor "
                        "does not offer them; use scale, shear, rotation or location instead")
    if key in _REFUSED:
        raise ToolError(f"{key}: {_REFUSED[key]}")
    if key not in props:
        raise ToolError(f"{key} is not a clip property the editor shows; settable: {', '.join(settable_keys())}")
    return key


def _is_curve(value: Any) -> bool:
    return isinstance(value, dict) and (isinstance(value.get("Points"), list) or "red" in value)


def property_info(key: str, clip_data: dict) -> PropertyInfo:
    props, shapes = clip_schema()
    meta = props[key]
    kind = str(meta.get("type") or "float")
    current = clip_data.get(key, shapes.get(key))
    keyframable = _is_curve(current) or _is_curve(shapes.get(key))
    choices = tuple((str(c.get("name")), c.get("value")) for c in (meta.get("choices") or [])
                    if isinstance(c, dict))
    lo, hi = meta.get("min"), meta.get("max")
    if key in _RANGE_OVERRIDES:
        lo, hi = _RANGE_OVERRIDES[key]
    elif key in _SCALE_KEYS:
        path = str(((clip_data.get("reader") or {}).get("path")) or "").lower()
        m = max_transform_multiple(project().get("width"), project().get("height"), path.endswith(".svg"))
        lo, hi = -float(m), float(m)
    elif key == "time":
        # time maps clip frames to SOURCE frames: its values run over the media's frames
        from classes.clip_utils import video_length_to_project_frames
        num, den = fps_fraction()
        frames = video_length_to_project_frames(clip_data.get("reader") or {}, project_fps=Fraction(num, den))
        if not frames:
            frames = int(round(float(clip_data.get("end") or 0.0) * project_fps())) + 1
        lo, hi = 1.0, float(max(1, int(frames)))
    if kind == "color":
        lo, hi = 0.0, 255.0
    return PropertyInfo(key=key, label=str(meta.get("name") or key), kind=kind, keyframable=keyframable,
                        choices=choices, minimum=None if lo is None else float(lo),
                        maximum=None if hi is None else float(hi), default=meta.get("value"))


def coerce_value(info: PropertyInfo, value: Any, clip_id: str = "") -> Any:
    """What the model sent -> the value stored in project data (raises ToolError)."""
    where = f"{info.key}" + (f" on clip {clip_id}" if clip_id else "")
    if info.kind == "color":
        return parse_color(value)
    if info.key == "parentObjectId":
        return _coerce_parent(value, clip_id)
    if info.choices:
        return _coerce_choice(info, value, where)
    if info.kind == "string":
        return str(value if value is not None else "")
    if isinstance(value, bool) or value is None:
        raise ToolError(f"{where} must be a number, got {value!r}")
    try:
        number = float(str(value).strip().rstrip("%")) / (100.0 if str(value).strip().endswith("%") else 1.0)
    except ValueError:
        raise ToolError(f"{where} must be a number, got {value!r}") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise ToolError(f"{where} must be finite, got {value!r}")
    if info.minimum is not None and not (info.minimum - 1e-9 <= number <= info.maximum + 1e-9):
        raise ToolError(f"{where} must be between {info.minimum:g} and {info.maximum:g}, got {number:g}")
    if info.kind == "int":
        if abs(number - round(number)) > 1e-9:
            raise ToolError(f"{where} must be a whole number, got {value!r}")
        return int(round(number))
    return float(number)


def _coerce_choice(info: PropertyInfo, value: Any, where: str):
    names = [name for name, _ in info.choices]
    if isinstance(value, bool):
        wanted = _norm("on" if value else "off")
        if info.key == "waveform":
            wanted = _norm("yes" if value else "no")
    elif isinstance(value, (int, float)):
        for name, val in info.choices:
            if float(val) == float(value):
                return int(val) if float(val).is_integer() else val
        raise ToolError(f"{where} must be one of {names} (or their values), got {value!r}")
    else:
        text = str(value)
        try:
            number = float(text)
        except ValueError:
            number = None
        if number is not None:
            return _coerce_choice(info, number, where)
        wanted = _norm(text)
    alias = _CHOICE_ALIASES.get(info.key, {}).get(wanted)
    for name, val in info.choices:
        if _norm(name) == wanted or (alias and name == alias):
            return int(val) if float(val).is_integer() else val
    raise ToolError(f"{where} must be one of {names}, got {value!r}")


def _coerce_parent(value: Any, clip_id: str) -> str:
    """parentObjectId: '' (none), another clip's id, or a tracked object '<effect id>-<index>'."""
    from classes.query import Clip

    text = str(value or "").strip()
    if text.lower() in ("", "none", "null"):
        return ""
    if text == clip_id:
        raise ToolError("a clip cannot be its own parent")
    if Clip.get(id=text):
        return text
    effect_id = text.rsplit("-", 1)[0]
    for clip in Clip.filter():
        for effect in clip.data.get("effects") or []:
            if isinstance(effect, dict) and effect.get("id") == effect_id and effect.get("objects"):
                return text
    raise ToolError(f"parentObjectId {text!r} is neither a timeline clip id nor a tracked object "
                    "(<tracker effect id>-<object index>)")


def stored_static(info: PropertyInfo, value: Any, current: Any) -> Any:
    """A static (non-keyframe) value in the JSON type the clip already uses (bool stays bool)."""
    if isinstance(current, bool) and info.choices:
        return bool(value)
    return value


def display_value(info: PropertyInfo, raw: Any) -> Any:
    if info.kind == "color" and isinstance(raw, tuple):
        return "#%02x%02x%02x%02x" % raw
    if info.choices:
        name = info.choice_name(raw)
        return name if name is not None else raw
    if isinstance(raw, float):
        return round(raw, 6)
    return raw


# ---------------------------------------------------------------------------
# Values at a frame, curves and points
# ---------------------------------------------------------------------------

COLOR_CHANNELS = ("red", "green", "blue", "alpha")


def value_at(info: PropertyInfo, clip_data: dict, frame: float) -> Any:
    """The property's value at a clip frame (exactly as libopenshot evaluates keyframes)."""
    _props, shapes = clip_schema()
    current = clip_data.get(info.key, shapes.get(info.key))
    if info.kind == "color":
        curves = current if isinstance(current, dict) else {}
        default = shapes.get(info.key) if isinstance(shapes.get(info.key), dict) else {}
        out = []
        for ch in COLOR_CHANNELS:
            fallback = keyframe_value(default.get(ch), 1, 255.0 if ch == "alpha" else 0.0)
            out.append(int(round(keyframe_value(curves.get(ch), frame, fallback))))
        return tuple(out)
    if _is_curve(current):
        value = keyframe_value(current, frame, float(info.default or 0.0))
        if info.kind == "int" or info.choices:
            return int(round(value))
        return value
    if current is None:
        return info.default
    if isinstance(current, bool):
        return int(current) if info.choices else current
    return current


def curve_points(curve: Any) -> list:
    pts = curve.get("Points") if isinstance(curve, dict) else None
    pts = [p for p in (pts or []) if isinstance(p, dict) and isinstance(p.get("co"), dict)]
    return sorted(pts, key=lambda p: float(p["co"].get("X", 0.0)))


def animated(info: PropertyInfo, clip_data: dict) -> bool:
    current = clip_data.get(info.key)
    if info.kind == "color" and isinstance(current, dict):
        return any(len(curve_points(current.get(ch))) > 1 for ch in COLOR_CHANNELS)
    return len(curve_points(current)) > 1


def ease_of(prev: Optional[dict], point: dict) -> Optional[str]:
    """Name of the bezier preset shaping the segment that ends at *point* (None if none/other)."""
    if prev is None or int(point.get("interpolation", BEZIER)) != BEZIER:
        return None
    hr = prev.get("handle_right") or {}
    hl = point.get("handle_left") or {}
    try:
        handles = (float(hr.get("X", 0.5)), float(hr.get("Y", 0.0)), float(hl.get("X", 0.5)), float(hl.get("Y", 1.0)))
    except (TypeError, ValueError):
        return None
    if all(abs(a - b) < 1e-6 for a, b in zip(handles, DEFAULT_HANDLES)):
        return "default"
    for name, preset in ease_presets().items():
        if all(abs(a - b) < 1e-6 for a, b in zip(handles, preset)):
            return name
    return "custom"


def make_point(frame: float, value: float, interpolation: int = BEZIER) -> dict:
    x = float(frame)
    return keyframe_point(int(x) if x.is_integer() else x, float(value), interpolation)


def apply_ease(points: list, index: int, preset: tuple) -> None:
    """Shape the segment ending at points[index] with a cubic-bezier preset (x1, y1, x2, y2)."""
    x1, y1, x2, y2 = preset
    point = points[index]
    point["interpolation"] = BEZIER
    point["handle_left"] = {"X": float(x2), "Y": float(y2)}
    if index > 0:
        points[index - 1]["handle_right"] = {"X": float(x1), "Y": float(y1)}


# ---------------------------------------------------------------------------
# Bezier presets (Properties dock > right-click > Bezier)
# ---------------------------------------------------------------------------

def _slug(label: str) -> str:
    text = label.lower().replace("(default)", "").replace("/", "_")
    text = text.replace("(", " ").replace(")", " ")
    return "_".join(text.split())


# The 28 preset names, in the Properties dock's menu order (checked against menu.py by a test).
EASE_NAMES = (
    "ease", "ease_in", "ease_out", "ease_in_out",
    "ease_in_quad", "ease_in_cubic", "ease_in_quart", "ease_in_quint", "ease_in_sine", "ease_in_expo",
    "ease_in_circ", "ease_in_back",
    "ease_out_quad", "ease_out_cubic", "ease_out_quart", "ease_out_quint", "ease_out_sine", "ease_out_expo",
    "ease_out_circ", "ease_out_back",
    "ease_in_out_quad", "ease_in_out_cubic", "ease_in_out_quart", "ease_in_out_quint", "ease_in_out_sine",
    "ease_in_out_expo", "ease_in_out_circ", "ease_in_out_back",
)

_ease_cache: dict = {}


def ease_presets() -> dict:
    """{name: (x1, y1, x2, y2)} from the Properties dock's own preset list."""
    if not _ease_cache:
        from windows.views.menu import keyframe_bezier_presets

        for x1, y1, x2, y2, label in keyframe_bezier_presets(tr=lambda s: s):
            _ease_cache[_slug(label)] = (float(x1), float(y1), float(x2), float(y2))
    return _ease_cache


# ---------------------------------------------------------------------------
# Where keyframes land
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClipTime:
    """Frame arithmetic for one clip: keyframe X is a 1-based clip frame that includes the trim."""
    fps: float
    position: float
    start: float
    end: float

    @classmethod
    def of(cls, clip_data: dict) -> "ClipTime":
        return cls(project_fps(), float(clip_data.get("position") or 0.0),
                   float(clip_data.get("start") or 0.0), float(clip_data.get("end") or 0.0))

    @property
    def first_frame(self) -> int:
        """X of the first visible frame (what the menus call start_of_clip)."""
        return int(round(self.start * self.fps)) + 1

    @property
    def end_frame(self) -> int:
        """X of the clip's end boundary (the menus' end_of_clip)."""
        return max(self.first_frame, int(round(self.end * self.fps)) + 1)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def from_clip_seconds(self, seconds: float) -> int:
        return int(round((self.start + float(seconds)) * self.fps)) + 1

    def from_timeline_seconds(self, seconds: float) -> int:
        return int(round((self.start + float(seconds) - self.position) * self.fps)) + 1

    def from_source_seconds(self, seconds: float) -> int:
        return int(round(float(seconds) * self.fps)) + 1

    def clip_seconds(self, frame: float) -> float:
        return round((float(frame) - 1.0) / self.fps - self.start, 4)

    def timeline_seconds(self, frame: float) -> float:
        return round(self.position + (float(frame) - 1.0) / self.fps - self.start, 4)

    def contains(self, frame: float) -> bool:
        return self.first_frame <= float(frame) <= self.end_frame

    def ranges_text(self) -> str:
        return (f"frames {self.first_frame}-{self.end_frame}, seconds 0-{self.duration:.3f} from the clip's "
                f"start, timeline_seconds {self.position:.3f}-{self.position + self.duration:.3f}, "
                f"source_seconds {self.start:.3f}-{self.end:.3f}")


def describe_points(curve: Any, clip_time: ClipTime, kind: str = "float") -> list:
    pts = curve_points(curve)
    out = []
    for i, p in enumerate(pts):
        x = float(p["co"].get("X", 1.0))
        item = {"frame": int(x) if x.is_integer() else round(x, 3),
                "clip_seconds": clip_time.clip_seconds(x),
                "timeline_seconds": clip_time.timeline_seconds(x),
                "value": round(float(p["co"].get("Y", 0.0)), 6),
                "interpolation": INTERPOLATION_NAMES.get(int(p.get("interpolation", BEZIER)), "bezier")}
        ease = ease_of(pts[i - 1] if i else None, p)
        if ease and ease != "default":
            item["ease"] = ease
        if not clip_time.contains(x):
            item["outside_clip"] = True
        out.append(item)
    return out
