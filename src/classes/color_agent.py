"""
ColorGrade merge / summarize / inspect helpers for agent colour tools.

Pure JSON transforms live here so unit tests do not need Qt or libopenshot.
Handlers in tool_handlers.py own clip resolve, undo, and FrameScope I/O.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional


COLOR_GRADE_CLASS_NAME = "ColorGrade"

# Scalar ColorGrade knobs and their neutral defaults (OpenShot Y values).
SCALAR_DEFAULTS = {
    "temperature": 0.0,
    "tint": 0.0,
    "exposure": 0.0,
    "contrast": 0.0,
    "highlights": 0.0,
    "shadows": 0.0,
    "saturation": 1.0,
    "vibrance": 0.0,
    "mix": 1.0,
    "lut_intensity": 1.0,
}

SCALAR_KEYS = tuple(SCALAR_DEFAULTS.keys())

# Clamp ranges after resolving *_delta. Neutral-zero knobs use [-1, 1];
# saturation / mix / lut_intensity use their ColorGrade ranges (sat 0…2).
_SCALAR_BOUNDS = {
    "saturation": (0.0, 2.0),
    "mix": (0.0, 1.0),
    "lut_intensity": (0.0, 1.0),
}

# Agent-facing aliases → ColorGrade keys
SCALAR_ALIASES = {
    "temp": "temperature",
    "sat": "saturation",
    "lut_strength": "lut_intensity",
    "lutIntensity": "lut_intensity",
}

CURVE_KEYS = ("curve_all", "curve_red", "curve_green", "curve_blue")
CURVE_ALIASES = {
    "masterCurve": "curve_all",
    "master_curve": "curve_all",
    "redCurve": "curve_red",
    "red_curve": "curve_red",
    "greenCurve": "curve_green",
    "green_curve": "curve_green",
    "blueCurve": "curve_blue",
    "blue_curve": "curve_blue",
}
WHEEL_NAMES = ("global", "shadows", "midtones", "highlights")

def is_color_grade_effect(effect_json: Any) -> bool:
    return isinstance(effect_json, dict) and effect_json.get("class_name") == COLOR_GRADE_CLASS_NAME


def find_color_grade(effects: Any) -> Optional[dict]:
    if not isinstance(effects, list):
        return None
    for effect in effects:
        if is_color_grade_effect(effect):
            return effect
    return None


def is_film_grain_effect(effect_json: Any) -> bool:
    return isinstance(effect_json, dict) and effect_json.get("class_name") == "FilmGrain"


def find_film_grain(effects: Any) -> Optional[dict]:
    if not isinstance(effects, list):
        return None
    for effect in effects:
        if is_film_grain_effect(effect):
            return effect
    return None


_GRAIN_SCALAR_KEYS = (
    "amount",
    "size",
    "softness",
    "clump",
    "shadows",
    "midtones",
    "highlights",
    "color_amount",
    "color_variation",
    "evolution",
    "coherence",
)


def summarize_film_grain(effect_json: Optional[dict]) -> dict:
    """Compact FilmGrain effect → agent-facing inspect JSON."""
    if not isinstance(effect_json, dict) or not is_film_grain_effect(effect_json):
        return {"present": False}
    summary: dict[str, Any] = {
        "present": True,
        "id": effect_json.get("id"),
    }
    for key in _GRAIN_SCALAR_KEYS:
        summary[key] = scalar_y(effect_json, key)
    return summary


def grain_meaningfully_differs(
    subject: Optional[dict],
    reference: Optional[dict],
    *,
    eps: float = 0.03,
) -> bool:
    sub = subject if isinstance(subject, dict) else {}
    ref = reference if isinstance(reference, dict) else {}
    if bool(sub.get("present")) != bool(ref.get("present")):
        return True
    if not sub.get("present"):
        return False
    for key in _GRAIN_SCALAR_KEYS:
        try:
            a = float(sub.get(key) if sub.get(key) is not None else 0.0)
            b = float(ref.get(key) if ref.get(key) is not None else 0.0)
        except (TypeError, ValueError):
            continue
        if abs(a - b) >= eps:
            return True
    return False


def match_grain_action(subject_effect: Optional[dict], reference_effect: Optional[dict]) -> dict:
    """How to make subject's FilmGrain look like reference's.

    Returns {"mode": "noop"|"reset"|"paste", "grain": optional paste object}.
    """
    sub_sum = summarize_film_grain(subject_effect)
    ref_sum = summarize_film_grain(reference_effect)
    if not grain_meaningfully_differs(sub_sum, ref_sum):
        return {"mode": "noop"}
    if not ref_sum.get("present"):
        return {"mode": "reset"}
    paste = copy.deepcopy(reference_effect)
    if isinstance(paste, dict):
        paste.pop("id", None)
        paste.pop("order", None)
    return {"mode": "paste", "grain": paste}


def _constant_property(value: float) -> dict:
    return {
        "Points": [
            {
                "co": {"X": 1.0, "Y": float(value)},
                "handle_left": {"X": 0.5, "Y": 1.0},
                "handle_right": {"X": 0.5, "Y": 0.0},
                "handle_type": 0,
                "interpolation": 0,
            }
        ]
    }


def scalar_y(effect_json: dict, key: str, default: Optional[float] = None) -> Optional[float]:
    raw = effect_json.get(key)
    if isinstance(raw, dict):
        points = raw.get("Points")
        if isinstance(points, list) and points:
            co = points[0].get("co") if isinstance(points[0], dict) else None
            if isinstance(co, dict) and "Y" in co:
                try:
                    return float(co["Y"])
                except (TypeError, ValueError):
                    pass
    if default is not None:
        return float(default)
    if key in SCALAR_DEFAULTS:
        return float(SCALAR_DEFAULTS[key])
    return None


def set_scalar(effect_json: dict, key: str, value: float) -> None:
    effect_json[key] = _constant_property(value)


# libopenshot enums (Point.h): InterpolationType BEZIER=0, LINEAR=1; HandleType
# AUTO=0. Curve nodes are LINEAR, as in libopenshot's own default curve, the
# Look menu (color_presets.py) and the Color Grade editor. A BEZIER node with
# these handles bends every segment into an S, so even an identity curve
# crushed the shadows and blew out the highlights.
_CURVE_NODE_INTERPOLATION = 1  # openshot.LINEAR
_CURVE_NODE_HANDLE_TYPE = 0  # openshot.AUTO


def _curve_node(node_id: int, x_value: float, y_value: float) -> dict:
    return {
        "id": int(node_id),
        "x": _constant_property(x_value),
        "y": _constant_property(y_value),
        "left_handle_x": _constant_property(0.5),
        "left_handle_y": _constant_property(1.0),
        "right_handle_x": _constant_property(0.5),
        "right_handle_y": _constant_property(0.0),
        "interpolation": _CURVE_NODE_INTERPOLATION,
        "handle_type": _CURVE_NODE_HANDLE_TYPE,
    }


def points_to_curve(points: list, enabled: bool = True) -> dict:
    nodes = []
    for index, point in enumerate(points):
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            x_value, y_value = float(point[0]), float(point[1])
        elif isinstance(point, dict):
            x_value = float(point.get("x", point.get("X", 0.0)))
            y_value = float(point.get("y", point.get("Y", 0.0)))
        else:
            raise ValueError(f"Invalid curve point: {point!r}")
        nodes.append(_curve_node(index, x_value, y_value))
    if len(nodes) < 2:
        raise ValueError("Curves need at least two points")
    return {
        "enabled": _constant_property(1.0 if enabled else 0.0),
        "nodes": nodes,
    }


def _color_keyframes(hex_color: str) -> dict:
    rgb = hex_color.lstrip("#")
    if len(rgb) != 6:
        raise ValueError(f"Wheel color must be #RRGGBB, got {hex_color!r}")
    return {
        "red": _constant_property(int(rgb[0:2], 16)),
        "green": _constant_property(int(rgb[2:4], 16)),
        "blue": _constant_property(int(rgb[4:6], 16)),
        "alpha": _constant_property(255),
    }


def _wheel_entry(color: str = "#ffffff", amount: float = 0.0, luma: float = 0.0) -> dict:
    return {
        "color": color,
        "color_keyframes": _color_keyframes(color),
        "amount": float(amount),
        "amount_keyframes": _constant_property(amount),
        "luma": float(luma),
        "luma_keyframes": _constant_property(luma),
    }


def default_wheels_data() -> dict:
    return {
        "enabled_keyframes": _constant_property(1.0),
        "global": _wheel_entry(),
        "shadows": _wheel_entry(),
        "midtones": _wheel_entry(),
        "highlights": _wheel_entry(),
    }


def default_curve_data() -> dict:
    return {
        "enabled": _constant_property(1.0),
        "nodes": [
            _curve_node(0, 0.0, 0.0),
            _curve_node(1, 1.0, 1.0),
        ],
    }


def blank_color_grade(effect_id: str = "") -> dict:
    """Neutral ColorGrade payload without calling libopenshot."""
    payload = {
        "class_name": COLOR_GRADE_CLASS_NAME,
        "name": "Color Grade",
        "lut_path": "",
        "wheels": default_wheels_data(),
        "curve_all": default_curve_data(),
        "curve_red": default_curve_data(),
        "curve_green": default_curve_data(),
        "curve_blue": default_curve_data(),
    }
    for key, value in SCALAR_DEFAULTS.items():
        set_scalar(payload, key, value)
    if effect_id:
        payload["id"] = effect_id
    return payload


def create_color_grade_effect_json(generate_id) -> dict:
    """Create a ColorGrade via libopenshot when available; else blank stub."""
    try:
        import openshot
    except ImportError:
        return blank_color_grade(generate_id() if callable(generate_id) else "")

    # Headless unit tests install a MagicMock openshot module.
    if type(openshot).__name__ == "MagicMock" or not hasattr(openshot, "EffectInfo"):
        return blank_color_grade(generate_id() if callable(generate_id) else "")

    try:
        effect = openshot.EffectInfo().CreateEffect(COLOR_GRADE_CLASS_NAME)
        if effect is None:
            raise RuntimeError("CreateEffect returned None")
        effect_id = generate_id()
        effect.Id(effect_id)
        payload = json.loads(effect.Json())
        if not payload.get("id"):
            payload["id"] = effect_id
        return payload
    except Exception as exc:
        raise RuntimeError(f"Could not create ColorGrade effect: {exc}") from exc


def _coerce_float(value, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if out != out or out in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number")
    return out


def _merge_wheel(existing: dict, patch: dict) -> dict:
    base = copy.deepcopy(existing) if isinstance(existing, dict) else _wheel_entry()
    color = patch.get("color") or patch.get("hex")
    if color is not None:
        color_str = str(color).strip()
        if not color_str.startswith("#"):
            color_str = "#" + color_str
        base["color"] = color_str
        base["color_keyframes"] = _color_keyframes(color_str)
    if "amount" in patch and patch["amount"] is not None:
        amount = _coerce_float(patch["amount"], "wheel.amount")
        base["amount"] = amount
        base["amount_keyframes"] = _constant_property(amount)
    if "luma" in patch and patch["luma"] is not None:
        luma = _coerce_float(patch["luma"], "wheel.luma")
        base["luma"] = luma
        base["luma_keyframes"] = _constant_property(luma)
    return base


# Relative nudges resolved against the current grade before merge.
DELTA_KEYS = {
    "temperature_delta": "temperature",
    "tint_delta": "tint",
    "exposure_delta": "exposure",
    "vibrance_delta": "vibrance",
    "contrast_delta": "contrast",
    "highlights_delta": "highlights",
    "shadows_delta": "shadows",
    "saturation_delta": "saturation",
}


def resolve_relative_deltas(effect_json: dict, patch: dict) -> dict:
    """Turn temperature_delta / tint_delta into absolute scalars on *effect_json*."""
    if not isinstance(patch, dict):
        raise ValueError("color patch must be an object")
    out = dict(patch)
    for dkey, target in DELTA_KEYS.items():
        if dkey not in out or out[dkey] is None or out[dkey] == "":
            continue
        delta = _coerce_float(out.pop(dkey), dkey)
        if target in out and out[target] not in (None, ""):
            raise ValueError(f"pass {target} or {dkey}, not both")
        cur = scalar_y(effect_json or {}, target)
        if cur is None:
            cur = float(SCALAR_DEFAULTS.get(target, 0.0))
        lo, hi = _SCALAR_BOUNDS.get(target, (-1.0, 1.0))
        out[target] = max(lo, min(hi, float(cur) + delta))
    return out


def validate_color_patch(patch: dict) -> None:
    """Raise ValueError before opening an undo group."""
    if not isinstance(patch, dict):
        raise ValueError("color patch must be an object")
    if patch.get("color") is not None and not isinstance(patch.get("color"), dict):
        raise ValueError("'color' paste object must be a dict")
    for key, raw in list(patch.items()):
        if key in DELTA_KEYS and raw is not None and not isinstance(raw, bool):
            _coerce_float(raw, key)
            continue
        canon = SCALAR_ALIASES.get(key, key)
        if canon in SCALAR_KEYS and raw is not None and not isinstance(raw, bool):
            value = _coerce_float(raw, canon)
            # Neutral saturation is 1.0. Models often pass 0.2 meaning "a bit
            # more color" and grey out the shot — refuse that class of mistake.
            if canon == "saturation" and value < 0.5:
                raise ValueError(
                    "saturation neutral is 1.0 (not 0). Values below 0.5 look grey. "
                    "For more color use ~1.1–1.3, or color_grade_clip_tool look='vibrant' / "
                    "'boost_color'. For less color use ~0.7–0.9, never 0.2."
                )
    lut = patch.get("lut")
    if lut is not None:
        if not isinstance(lut, dict):
            raise ValueError("lut must be an object with path and optional strength")
        if lut.get("strength") is not None:
            _coerce_float(lut["strength"], "lut.strength")
        if lut.get("intensity") is not None:
            _coerce_float(lut["intensity"], "lut.intensity")
    wheels = patch.get("wheels")
    if wheels is not None:
        if not isinstance(wheels, dict):
            raise ValueError("wheels must be an object")
        for name, wheel in wheels.items():
            if name == "enabled":
                continue
            if name not in WHEEL_NAMES:
                raise ValueError(f"Unknown wheel '{name}'")
            if wheel is not None and not isinstance(wheel, dict):
                raise ValueError(f"wheels.{name} must be an object")
            if isinstance(wheel, dict):
                if wheel.get("amount") is not None:
                    _coerce_float(wheel["amount"], f"wheels.{name}.amount")
                if wheel.get("luma") is not None:
                    _coerce_float(wheel["luma"], f"wheels.{name}.luma")
                color = wheel.get("color") or wheel.get("hex")
                if color is not None:
                    rgb = str(color).lstrip("#")
                    if len(rgb) != 6:
                        raise ValueError(f"wheels.{name}.color must be #RRGGBB")
                    int(rgb, 16)
    for alias, canon in list(CURVE_ALIASES.items()) + [(k, k) for k in CURVE_KEYS]:
        if alias not in patch and canon not in patch:
            continue
        points = patch.get(alias, patch.get(canon))
        if points is None:
            continue
        if isinstance(points, dict) and "nodes" in points:
            continue
        if not isinstance(points, list) or len(points) < 2:
            raise ValueError(f"{canon} needs a list of at least two [x,y] points")
        points_to_curve(points)


def merge_color_grade(effect_json: dict, patch: dict) -> dict:
    """Merge agent patch into an existing ColorGrade effect (unset fields kept)."""
    validate_color_patch(patch)
    payload = copy.deepcopy(effect_json or {})
    if not is_color_grade_effect(payload):
        payload["class_name"] = COLOR_GRADE_CLASS_NAME
    patch = resolve_relative_deltas(payload, patch)
    # Deltas resolve after the first validate; re-check absolute saturation.
    sat = patch.get("saturation")
    if sat is not None and sat != "":
        value = _coerce_float(sat, "saturation")
        if value < 0.5:
            raise ValueError(
                "saturation neutral is 1.0 (not 0). Values below 0.5 look grey. "
                "For more color use ~1.1–1.3, or color_grade_clip_tool look='vibrant' / "
                "'boost_color'. For less color use ~0.7–0.9, never 0.2."
            )

    # Full paste replaces color-bearing fields but keeps id/order.
    color_obj = patch.get("color")
    if isinstance(color_obj, dict):
        keep_id = payload.get("id")
        keep_order = payload.get("order")
        payload = copy.deepcopy(color_obj)
        payload["class_name"] = COLOR_GRADE_CLASS_NAME
        if keep_id:
            payload["id"] = keep_id
        if keep_order is not None:
            payload["order"] = keep_order
        # Continue so additional top-level fields in the same call can nudge.

    for key, raw in patch.items():
        if key in ("color", "lut", "wheels", "reset") or key in CURVE_KEYS or key in CURVE_ALIASES:
            continue
        canon = SCALAR_ALIASES.get(key, key)
        if canon in SCALAR_KEYS and raw is not None:
            set_scalar(payload, canon, _coerce_float(raw, canon))

    if "lut_path" in patch and patch["lut_path"] is not None:
        payload["lut_path"] = str(patch["lut_path"])
    lut = patch.get("lut")
    if isinstance(lut, dict):
        if lut.get("path") is not None:
            payload["lut_path"] = str(lut["path"])
        strength = lut.get("strength", lut.get("intensity"))
        if strength is not None:
            set_scalar(payload, "lut_intensity", _coerce_float(strength, "lut.strength"))

    wheels_patch = patch.get("wheels")
    if isinstance(wheels_patch, dict):
        wheels = payload.get("wheels")
        if not isinstance(wheels, dict):
            wheels = default_wheels_data()
        else:
            wheels = copy.deepcopy(wheels)
        for name in WHEEL_NAMES:
            if name in wheels_patch and wheels_patch[name] is not None:
                wheels[name] = _merge_wheel(wheels.get(name), wheels_patch[name])
        payload["wheels"] = wheels

    for alias, canon in list(CURVE_ALIASES.items()) + [(k, k) for k in CURVE_KEYS]:
        if alias not in patch:
            continue
        points = patch[alias]
        if points is None:
            continue
        if isinstance(points, dict) and "nodes" in points:
            payload[canon] = copy.deepcopy(points)
        else:
            payload[canon] = points_to_curve(points)

    return payload


def summarize_color_grade(effect_json: Optional[dict]) -> dict:
    if not is_color_grade_effect(effect_json):
        return {"present": False}
    summary = {
        "present": True,
        "id": effect_json.get("id"),
        "lut_path": effect_json.get("lut_path") or "",
        "wheels": effect_json.get("wheels") or {},
        "curve_all": effect_json.get("curve_all") or {},
        "curve_red": effect_json.get("curve_red") or {},
        "curve_green": effect_json.get("curve_green") or {},
        "curve_blue": effect_json.get("curve_blue") or {},
    }
    for key in SCALAR_KEYS:
        summary[key] = scalar_y(effect_json, key)
    return summary


def _grade_structure_fingerprint(payload: dict) -> str:
    """Stable JSON for wheels/curves so paste decisions see non-scalar diffs."""
    try:
        return json.dumps(
            {
                "wheels": payload.get("wheels") or {},
                "curve_all": payload.get("curve_all") or {},
                "curve_red": payload.get("curve_red") or {},
                "curve_green": payload.get("curve_green") or {},
                "curve_blue": payload.get("curve_blue") or {},
            },
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        return ""


def grades_meaningfully_differ(
    subject: Optional[dict],
    reference: Optional[dict],
    *,
    eps: float = 0.02,
) -> bool:
    """True when ColorGrade knobs/LUT/wheels/curves differ enough to copy/reset."""
    sub = subject if isinstance(subject, dict) else {}
    ref = reference if isinstance(reference, dict) else {}
    if bool(sub.get("present")) != bool(ref.get("present")):
        return True
    if not sub.get("present"):
        return False
    if str(sub.get("lut_path") or "") != str(ref.get("lut_path") or ""):
        return True
    for key in SCALAR_KEYS:
        try:
            a = float(sub.get(key) if sub.get(key) is not None else SCALAR_DEFAULTS.get(key, 0.0))
            b = float(ref.get(key) if ref.get(key) is not None else SCALAR_DEFAULTS.get(key, 0.0))
        except (TypeError, ValueError):
            continue
        if abs(a - b) >= eps:
            return True
    if _grade_structure_fingerprint(sub) != _grade_structure_fingerprint(ref):
        return True
    return False


def match_grade_action(subject_effect: Optional[dict], reference_effect: Optional[dict]) -> dict:
    """How to make subject's ColorGrade look like reference's (knob-level).

    Returns {"mode": "noop"|"reset"|"paste", "color": optional paste object}.
    """
    sub_sum = summarize_color_grade(subject_effect)
    ref_sum = summarize_color_grade(reference_effect)
    if not grades_meaningfully_differ(sub_sum, ref_sum):
        return {"mode": "noop"}
    if not ref_sum.get("present"):
        return {"mode": "reset"}
    paste = copy.deepcopy(reference_effect)
    # Drop identity so the paste keeps/creates the subject's effect id.
    if isinstance(paste, dict):
        paste.pop("id", None)
        paste.pop("order", None)
    return {"mode": "paste", "color": paste}


def _hist_mean(bins: list) -> Optional[float]:
    if not bins:
        return None
    total = 0.0
    weighted = 0.0
    n = len(bins)
    for i, count in enumerate(bins):
        try:
            c = float(count)
        except (TypeError, ValueError):
            continue
        total += c
        weighted += c * (i / max(n - 1, 1))
    if total <= 0:
        return None
    return weighted / total


def _hist_total(bins: Any) -> float:
    """Number of pixels a histogram was built from (its bins sum to the frame's pixel count)."""
    try:
        return float(sum(float(b) for b in (bins or [])))
    except (TypeError, ValueError):
        return 0.0


def clipped_fraction(value: Any, pixels: float) -> Optional[float]:
    """Clipped shadows/highlights as a 0..1 fraction of the frame.

    libopenshot's FrameScope reports these as pixel COUNTS (3836 clipped pixels in a
    640x360 frame). Everything downstream (look distance, the grade solver, the
    over-grade check) treats them as fractions, so a raw count made a pixel or two of
    difference swamp every other term. Counts are integers above 1, fractions are
    within 0..1, so a value over 1 is a count; an integer-valued 1.0 is one pixel.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0.0:
        return 0.0
    if pixels > 0.0 and (v > 1.0 or v == 1.0):
        return min(1.0, v / pixels)
    return min(1.0, v)


def summarize_scope_video(video: Optional[dict]) -> dict:
    """Compact FrameScope video payload → agent-facing inspect JSON."""
    if not isinstance(video, dict) or not video.get("present"):
        return {"present": False}

    summary = {"present": True}
    scope_summary = video.get("summary") if isinstance(video.get("summary"), dict) else {}
    hist = video.get("histogram") if isinstance(video.get("histogram"), dict) else {}
    summary["avg_luma"] = scope_summary.get("avg_luma")
    pixels = _hist_total(hist.get("luma"))
    summary["clipped_shadows"] = clipped_fraction(scope_summary.get("clipped_shadows"), pixels)
    summary["clipped_highlights"] = clipped_fraction(scope_summary.get("clipped_highlights"), pixels)

    channel_means = {}
    for channel in ("luma", "red", "green", "blue"):
        mean = _hist_mean(list(hist.get(channel) or []))
        if mean is not None:
            channel_means[channel] = round(mean, 4)
    if channel_means:
        summary["channel_means"] = channel_means
        r = channel_means.get("red")
        g = channel_means.get("green")
        b = channel_means.get("blue")
        if r is not None and b is not None:
            summary["warm_cool"] = round(r - b, 4)
        if g is not None and r is not None and b is not None:
            summary["green_magenta"] = round(g - ((r + b) / 2.0), 4)

    return summary


def scope_look_distance(subject: dict, reference: dict) -> Optional[float]:
    """Scalar how-different two isolated scope summaries look (0 ≈ same)."""
    if not isinstance(subject, dict) or not isinstance(reference, dict):
        return None
    if not subject.get("present") or not reference.get("present"):
        return None
    parts: list[float] = []
    for key, weight in (
        ("avg_luma", 1.0),
        ("warm_cool", 1.2),
        ("green_magenta", 1.0),
    ):
        a, b = subject.get(key), reference.get(key)
        if a is None or b is None:
            continue
        try:
            parts.append(abs(float(a) - float(b)) * weight)
        except (TypeError, ValueError):
            continue
    sub_cm = subject.get("channel_means") if isinstance(subject.get("channel_means"), dict) else {}
    ref_cm = reference.get("channel_means") if isinstance(reference.get("channel_means"), dict) else {}
    for ch in ("red", "green", "blue"):
        a, b = sub_cm.get(ch), ref_cm.get(ch)
        if a is None or b is None:
            continue
        try:
            parts.append(abs(float(a) - float(b)) * 0.8)
        except (TypeError, ValueError):
            continue
    if not parts:
        return None
    return round(sum(parts), 4)


# --- LookProfile: dense sample aggregation + inverse grade ---------------------

LOOK_DISTANCE_MATCHED = 0.045
LOOK_DISTANCE_CLOSE = 0.08
_MAX_STEP = {
    "exposure": 0.35,
    "contrast": 0.25,
    "highlights": 0.30,
    "shadows": 0.30,
    "temperature": 0.35,
    "tint": 0.25,
    "saturation": 0.25,
    "vibrance": 0.25,
}


def sample_count_for_duration(duration_s: float) -> int:
    """How many full-frame samples to take across a media duration."""
    try:
        d = float(duration_s)
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0.05:
        return 1  # still / tiny
    # ~1 sample / 25s on long form, denser on short; floor 16, ceil 96.
    n = int(round(d / 25.0)) + 8
    return max(16, min(96, n))


def _hist_percentile(bins: list, percentile: float) -> Optional[float]:
    """Approximate percentile (0–1) from a histogram bin list."""
    if not bins:
        return None
    try:
        p = max(0.0, min(1.0, float(percentile)))
    except (TypeError, ValueError):
        return None
    total = 0.0
    counts: list[float] = []
    for count in bins:
        try:
            c = float(count)
        except (TypeError, ValueError):
            c = 0.0
        counts.append(c)
        total += c
    if total <= 0:
        return None
    target = total * p
    running = 0.0
    n = len(counts)
    for i, c in enumerate(counts):
        running += c
        if running >= target:
            return i / max(n - 1, 1)
    return 1.0


def _sat_proxy(channel_means: dict) -> Optional[float]:
    r = channel_means.get("red")
    g = channel_means.get("green")
    b = channel_means.get("blue")
    if r is None or g is None or b is None:
        return None
    try:
        mx = max(float(r), float(g), float(b))
        mn = min(float(r), float(g), float(b))
    except (TypeError, ValueError):
        return None
    if mx <= 1e-9:
        return 0.0
    return (mx - mn) / mx


def _enrich_scope_for_profile(scope: dict) -> dict:
    """Add sat_proxy onto a summarize_scope_video-style dict."""
    out = dict(scope) if isinstance(scope, dict) else {"present": False}
    if not out.get("present"):
        return out
    cm = out.get("channel_means") if isinstance(out.get("channel_means"), dict) else {}
    sat = _sat_proxy(cm)
    if sat is not None:
        out["sat_proxy"] = round(sat, 4)
    return out


def scope_from_raw_video(video: Optional[dict]) -> dict:
    """summarize_scope_video + contrast_span from luma histogram percentiles."""
    summary = summarize_scope_video(video)
    if not summary.get("present") or not isinstance(video, dict):
        return summary
    hist = video.get("histogram") if isinstance(video.get("histogram"), dict) else {}
    luma = list(hist.get("luma") or [])
    p10 = _hist_percentile(luma, 0.10)
    p90 = _hist_percentile(luma, 0.90)
    if p10 is not None and p90 is not None:
        summary["contrast_span"] = round(float(p90) - float(p10), 4)
        summary["luma_p10"] = round(float(p10), 4)
        summary["luma_p90"] = round(float(p90), 4)
    return _enrich_scope_for_profile(summary)


def _median(values: list) -> Optional[float]:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _percentile_list(values: list, p: float) -> Optional[float]:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    idx = max(0, min(len(clean) - 1, int(round((len(clean) - 1) * p))))
    return clean[idx]


def build_look_profile(scopes) -> dict:
    """Aggregate one or many scope summaries into a LookProfile.

    ``scopes`` may be a single summary dict or a list of them (dense video).
    Temporal aggregation uses median + p10/p90 so one flash frame cannot own the look.
    """
    if isinstance(scopes, dict):
        items = [scopes]
    elif isinstance(scopes, (list, tuple)):
        items = [s for s in scopes if isinstance(s, dict)]
    else:
        return {"present": False, "samples": 0}

    present = [_enrich_scope_for_profile(s) for s in items if s.get("present")]
    if not present:
        return {"present": False, "samples": 0}

    def _collect(key: str) -> list:
        out = []
        for s in present:
            v = s.get(key)
            if v is None:
                continue
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    def _collect_ch(ch: str) -> list:
        out = []
        for s in present:
            cm = s.get("channel_means") if isinstance(s.get("channel_means"), dict) else {}
            v = cm.get(ch)
            if v is None:
                continue
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return out

    profile: dict[str, Any] = {
        "present": True,
        "samples": len(present),
        "avg_luma": _median(_collect("avg_luma")),
        "warm_cool": _median(_collect("warm_cool")),
        "green_magenta": _median(_collect("green_magenta")),
        "sat_proxy": _median(_collect("sat_proxy")),
        "contrast_span": _median(_collect("contrast_span")),
        "clipped_shadows": _median(_collect("clipped_shadows")),
        "clipped_highlights": _median(_collect("clipped_highlights")),
        "channel_means": {},
    }
    for ch in ("red", "green", "blue", "luma"):
        m = _median(_collect_ch(ch))
        if m is not None:
            profile["channel_means"][ch] = round(m, 4)

    luma_vals = _collect("avg_luma")
    warm_vals = _collect("warm_cool")
    if len(luma_vals) >= 3:
        profile["luma_p10"] = _percentile_list(luma_vals, 0.10)
        profile["luma_p90"] = _percentile_list(luma_vals, 0.90)
    if len(warm_vals) >= 3:
        profile["warm_p10"] = _percentile_list(warm_vals, 0.10)
        profile["warm_p90"] = _percentile_list(warm_vals, 0.90)

    for key in (
        "avg_luma", "warm_cool", "green_magenta", "sat_proxy", "contrast_span",
        "clipped_shadows", "clipped_highlights", "luma_p10", "luma_p90",
        "warm_p10", "warm_p90",
    ):
        if profile.get(key) is not None:
            try:
                profile[key] = round(float(profile[key]), 4)
            except (TypeError, ValueError):
                pass
    return profile


def look_profile_distance(subject: dict, reference: dict) -> Optional[float]:
    """Weighted LookProfile distance (0 ≈ same look)."""
    if not isinstance(subject, dict) or not isinstance(reference, dict):
        return None
    if not subject.get("present") or not reference.get("present"):
        return None
    parts: list[float] = []
    for key, weight in (
        ("avg_luma", 1.2),
        ("warm_cool", 1.4),
        ("green_magenta", 1.0),
        ("sat_proxy", 1.1),
        ("contrast_span", 1.0),
        ("clipped_highlights", 0.9),
        ("clipped_shadows", 0.6),
    ):
        a, b = subject.get(key), reference.get(key)
        if a is None or b is None:
            continue
        try:
            parts.append(abs(float(a) - float(b)) * weight)
        except (TypeError, ValueError):
            continue
    sub_cm = subject.get("channel_means") if isinstance(subject.get("channel_means"), dict) else {}
    ref_cm = reference.get("channel_means") if isinstance(reference.get("channel_means"), dict) else {}
    for ch in ("red", "green", "blue"):
        a, b = sub_cm.get(ch), ref_cm.get(ch)
        if a is None or b is None:
            continue
        try:
            parts.append(abs(float(a) - float(b)) * 0.7)
        except (TypeError, ValueError):
            continue
    if not parts:
        return scope_look_distance(subject, reference)
    return round(sum(parts), 4)


def cap_color_patch(patch: dict, *, scale: float = 1.0) -> dict:
    """Clamp per-step ColorGrade merge deltas so one iteration cannot nuke."""
    if not isinstance(patch, dict):
        return {}
    out: dict[str, Any] = {}
    try:
        s = max(0.05, min(1.0, float(scale)))
    except (TypeError, ValueError):
        s = 1.0
    for key, value in patch.items():
        if key not in SCALAR_KEYS and key not in SCALAR_ALIASES:
            if key in ("lut_path", "lut", "color", "reset", "wheels") or str(key).startswith("curve"):
                out[key] = value
            continue
        canon = SCALAR_ALIASES.get(key, key)
        try:
            v = float(value) * s
        except (TypeError, ValueError):
            continue
        limit = _MAX_STEP.get(canon, 0.35)
        out[canon] = round(max(-limit, min(limit, v)), 4)
    return out


def solve_grade_from_profiles(
    subject: dict,
    reference: dict,
    *,
    scale: float = 1.0,
) -> dict:
    """Map LookProfile gap → ColorGrade merge patch (deltas toward reference)."""
    if not subject.get("present") or not reference.get("present"):
        return {}
    patch: dict[str, float] = {}

    def _delta(key: str) -> Optional[float]:
        a, b = subject.get(key), reference.get(key)
        if a is None or b is None:
            return None
        try:
            return float(b) - float(a)
        except (TypeError, ValueError):
            return None

    luma = _delta("avg_luma")
    if luma is not None and abs(luma) >= 0.012:
        patch["exposure"] = max(-0.5, min(0.5, luma * 0.85))

    warm = _delta("warm_cool")
    if warm is not None and abs(warm) >= 0.01:
        patch["temperature"] = max(-0.45, min(0.45, warm * 0.65))

    gm = _delta("green_magenta")
    if gm is not None and abs(gm) >= 0.008:
        patch["tint"] = max(-0.35, min(0.35, gm * 0.55))

    sat = _delta("sat_proxy")
    if sat is not None and abs(sat) >= 0.02:
        patch["saturation"] = max(-0.35, min(0.35, sat * 0.9))
        patch["vibrance"] = max(-0.3, min(0.3, sat * 0.55))

    contrast = _delta("contrast_span")
    if contrast is not None and abs(contrast) >= 0.015:
        patch["contrast"] = max(-0.35, min(0.35, contrast * 0.7))

    hi = _delta("clipped_highlights")
    if hi is not None and abs(hi) >= 0.01:
        patch["highlights"] = max(-0.4, min(0.4, -hi * 1.2))

    sh = _delta("clipped_shadows")
    if sh is not None and abs(sh) >= 0.01:
        patch["shadows"] = max(-0.4, min(0.4, sh * 0.9))

    return cap_color_patch(patch, scale=scale)


def assess_grade_outcome(
    before: dict,
    after: dict,
    goal: dict,
    *,
    outdoor_bright: bool = False,
) -> dict:
    """Judge whether AFTER moved toward GOAL without nuking the image."""
    before_d = look_profile_distance(before, goal)
    after_d = look_profile_distance(after, goal)
    nuke_risk = False
    reasons: list[str] = []

    def _f(d: dict, key: str, default: float = 0.0) -> float:
        try:
            v = d.get(key)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    after_hi = _f(after, "clipped_highlights")
    before_hi = _f(before, "clipped_highlights")
    if after_hi - before_hi >= 0.04 or after_hi >= 0.12:
        nuke_risk = True
        reasons.append("clipped_highlights_spike")
    if outdoor_bright and after_hi >= 0.08:
        nuke_risk = True
        reasons.append("outdoor_highlight_blow")

    after_sh = _f(after, "clipped_shadows")
    before_sh = _f(before, "clipped_shadows")
    if after_sh - before_sh >= 0.06:
        nuke_risk = True
        reasons.append("clipped_shadows_spike")

    if before_d is not None and after_d is not None and after_d > before_d + 0.02:
        nuke_risk = True
        reasons.append("look_distance_worse")

    recovery: dict[str, Any] = {}
    if nuke_risk:
        recovery = cap_color_patch({
            "exposure": -0.12 if after_hi > before_hi else -0.06,
            "highlights": -0.15,
            "contrast": -0.08,
            "vibrance": -0.05,
        })
        if after_hi > before_hi:
            recovery["exposure"] = -min(0.25, 0.08 + (after_hi - before_hi))
            recovery["highlights"] = -min(0.3, 0.1 + (after_hi - before_hi))

    ok = (
        not nuke_risk
        and after_d is not None
        and after_d <= LOOK_DISTANCE_MATCHED
    )
    closer = (
        before_d is not None
        and after_d is not None
        and after_d < before_d - 0.005
    )
    status = "matched" if ok else (
        "recovered_needed" if nuke_risk else (
            "closer_not_exact" if closer else "still_far"
        )
    )
    return {
        "ok": ok,
        "nuke_risk": nuke_risk,
        "look_distance_before": before_d,
        "look_distance_after": after_d,
        "status": status,
        "reasons": reasons,
        "suggested_recovery_patch": recovery,
        "outdoor_bright": outdoor_bright,
    }


def is_outdoor_bright_profile(profile: dict) -> bool:
    """Heuristic: bright outdoor plate that must not get a heavy sunny look."""
    if not isinstance(profile, dict) or not profile.get("present"):
        return False
    try:
        luma = float(profile.get("avg_luma") or 0.0)
        hi = float(profile.get("clipped_highlights") or 0.0)
    except (TypeError, ValueError):
        return False
    return luma >= 0.55 or hi >= 0.05


# --- Looks catalog / resolve / match (Phase 6.3) ---------------------------------
