"""
 @file
 @brief Clip > Look presets as pure edits of a clip's effect list

 The Look menu (timeline Color / Film Grain / Analog Tape / Sharpen / Blur /
 Shadow / Glow / Reset Look) and the agent's look tools share these rules:
 which effects a preset replaces, which ids and order it keeps, and what
 Reset Look removes. Every function takes the clip's current ``effects``
 list and returns the new list, or ``None`` when the preset would change
 nothing (so callers can skip saving and leave undo history alone).
"""

import copy

from classes.color_presets import (
    COLOR_GRADE_CLASS_NAME,
    COLOR_PRESET_RESET,
    apply_color_grade_preset,
    is_color_grade_effect,
)
from classes.film_grain_presets import (
    FILM_GRAIN_CLASS_NAME,
    FILM_GRAIN_PRESET_NONE,
    apply_film_grain_preset,
    is_film_grain_effect,
)

LOOK_EFFECT_UI_MENU = "look"

LOOK_RESET_EFFECT_CLASSES = {
    COLOR_GRADE_CLASS_NAME,
    FILM_GRAIN_CLASS_NAME,
}

LOOK_EFFECT_PRESETS = {
    "AnalogTape": {
        "none": {},
        "subtle": {
            "bleed": 0.25,
            "noise": 0.18,
            "softness": 0.15,
            "static_bands": 0.05,
            "stripe": 0.06,
            "tracking": 0.20,
        },
        "vhs": {
            "bleed": 0.55,
            "noise": 0.35,
            "softness": 0.35,
            "static_bands": 0.18,
            "stripe": 0.20,
            "tracking": 0.45,
        },
        "heavy": {
            "bleed": 0.85,
            "noise": 0.60,
            "softness": 0.55,
            "static_bands": 0.35,
            "stripe": 0.40,
            "tracking": 0.75,
        },
    },
    "Blur": {
        "none": {},
        "soft_focus": {"horizontal_radius": 3.0, "vertical_radius": 3.0, "sigma": 1.5, "iterations": 2.0},
        "medium": {"horizontal_radius": 8.0, "vertical_radius": 8.0, "sigma": 4.0, "iterations": 3.0},
        "heavy": {"horizontal_radius": 20.0, "vertical_radius": 20.0, "sigma": 8.0, "iterations": 4.0},
    },
    "Glow": {
        "none": {},
        "soft_white": {"mode": 0, "opacity": 0.35, "blur_radius": 18.0, "spread": 0.15, "color": "#ffffffff"},
        "warm": {"mode": 0, "opacity": 0.45, "blur_radius": 24.0, "spread": 0.20, "color": "#ffd28cff"},
        "neon": {"mode": 0, "opacity": 0.65, "blur_radius": 16.0, "spread": 0.35, "color": "#35d7ffff"},
        "inner": {"mode": 1, "opacity": 0.45, "blur_radius": 12.0, "spread": 0.25, "color": "#ffffffff"},
    },
    "Shadow": {
        "none": {},
        "subtle": {"opacity": 0.30, "blur_radius": 12.0, "spread": 0.05, "distance": 8.0, "angle": 135.0, "color": "#000000ff"},
        "soft": {"opacity": 0.45, "blur_radius": 28.0, "spread": 0.10, "distance": 14.0, "angle": 135.0, "color": "#000000ff"},
        "strong": {"opacity": 0.70, "blur_radius": 18.0, "spread": 0.25, "distance": 16.0, "angle": 135.0, "color": "#000000ff"},
        "long": {"opacity": 0.45, "blur_radius": 24.0, "spread": 0.12, "distance": 44.0, "angle": 135.0, "color": "#000000ff"},
    },
    "Sharpen": {
        "none": {},
        "subtle": {"amount": 4.0, "radius": 1.5, "threshold": 0.0},
        "medium": {"amount": 9.0, "radius": 2.5, "threshold": 0.0},
        "strong": {"amount": 16.0, "radius": 3.5, "threshold": 0.0},
    },
}


def _constant_point(value):
    """One keyframe point at frame 1 (the shape openshot.Point(1, v, BEZIER).Json() produces)."""
    return {
        "co": {"X": 1.0, "Y": float(value)},
        "handle_left": {"X": 0.5, "Y": 1.0},
        "handle_right": {"X": 0.5, "Y": 0.0},
        "handle_type": 0,
        "interpolation": 0,
    }


def _effects_list(effects):
    if isinstance(effects, list):
        return list(effects)
    return list(effects) if effects else []


def is_look_managed_effect(effect_json, class_name=None):
    """True for an effect the Look menu created (tagged ``ui-menu: look``)."""
    if not isinstance(effect_json, dict):
        return False
    if effect_json.get("ui-menu") != LOOK_EFFECT_UI_MENU:
        return False
    return class_name is None or effect_json.get("class_name") == class_name


def parse_effect_color(value):
    """'#RRGGBB' / '#RRGGBBAA' -> {red, green, blue, alpha} (0-255), else None."""
    if not isinstance(value, str):
        return None
    color = value.strip()
    if color.startswith("#"):
        color = color[1:]
    if len(color) not in (6, 8):
        return None
    try:
        red = int(color[0:2], 16)
        green = int(color[2:4], 16)
        blue = int(color[4:6], 16)
        alpha = int(color[6:8], 16) if len(color) == 8 else 255
    except ValueError:
        return None
    return {"red": red, "green": green, "blue": blue, "alpha": alpha}


def set_effect_property_value(effect_json, property_name, value):
    """Set a preset value on an effect: constant keyframe, color channels, or a plain key."""
    property_data = effect_json.get(property_name)
    color_channels = parse_effect_color(value)
    if color_channels and isinstance(property_data, dict):
        for channel, channel_value in color_channels.items():
            channel_data = property_data.get(channel)
            if isinstance(channel_data, dict) and isinstance(channel_data.get("Points"), list):
                channel_data["Points"] = [_constant_point(channel_value)]
    elif isinstance(property_data, dict) and isinstance(property_data.get("Points"), list):
        property_data["Points"] = [_constant_point(value)]
    elif property_name in effect_json:
        effect_json[property_name] = value


def _replace_matches(effects, matching_indexes, preset_effect):
    """Put *preset_effect* where the first match was (keeping its id and order); drop other matches."""
    if matching_indexes:
        existing_effect = effects[matching_indexes[0]]
        if existing_effect.get("id"):
            preset_effect["id"] = existing_effect["id"]
        if "order" in existing_effect:
            preset_effect["order"] = existing_effect["order"]
        effects[matching_indexes[0]] = preset_effect
        for index in reversed(matching_indexes[1:]):
            del effects[index]
    else:
        effects.append(preset_effect)
    return effects


def apply_look_effect_preset(effects, class_name, preset_name, create_effect_json):
    """Analog Tape / Sharpen / Blur / Shadow / Glow preset (``none`` removes the Look effect).

    *create_effect_json(class_name)* returns a fresh effect dict with a new id
    (it may raise RuntimeError when libopenshot lacks the effect).
    """
    presets = LOOK_EFFECT_PRESETS.get(class_name, {})
    if preset_name not in presets:
        raise ValueError("Unknown {} look preset: {}".format(class_name, preset_name))
    effects = _effects_list(effects)
    matching_indexes = [
        index for index, effect_json in enumerate(effects)
        if is_look_managed_effect(effect_json, class_name)
    ]
    if preset_name == "none":
        if not matching_indexes:
            return None
        return [e for e in effects if not is_look_managed_effect(e, class_name)]

    preset_effect = create_effect_json(class_name)
    preset_effect["ui-menu"] = LOOK_EFFECT_UI_MENU
    for property_name, value in presets[preset_name].items():
        set_effect_property_value(preset_effect, property_name, value)
    return _replace_matches(effects, matching_indexes, preset_effect)


def apply_color_look_preset(effects, preset_name, create_effect_json):
    """Look > Color preset (Auto Contrast, Lift Shadows, Warm Up, Boost Color) or ``reset``.

    Replaces the clip's Color Grade effect (keeping its id and order) with a
    fresh one carrying the preset; ``reset`` removes Color Grade effects.
    """
    effects = _effects_list(effects)
    matching_indexes = [
        index for index, effect_json in enumerate(effects)
        if is_color_grade_effect(effect_json)
    ]
    if preset_name == COLOR_PRESET_RESET:
        if not matching_indexes:
            return None
        return [e for e in effects if not is_color_grade_effect(e)]
    preset_effect = apply_color_grade_preset(create_effect_json(COLOR_GRADE_CLASS_NAME), preset_name)
    return _replace_matches(effects, matching_indexes, preset_effect)


def apply_film_grain_look_preset(effects, preset_name, create_effect_json):
    """Look > Film Grain preset, or ``none`` to remove Film Grain effects."""
    effects = _effects_list(effects)
    matching_indexes = [
        index for index, effect_json in enumerate(effects)
        if is_film_grain_effect(effect_json)
    ]
    if preset_name == FILM_GRAIN_PRESET_NONE:
        if not matching_indexes:
            return None
        return [e for e in effects if not is_film_grain_effect(e)]
    source_effect = (
        effects[matching_indexes[0]]
        if matching_indexes
        else create_effect_json(FILM_GRAIN_CLASS_NAME)
    )
    preset_effect = apply_film_grain_preset(copy.deepcopy(source_effect), preset_name)
    return _replace_matches(effects, matching_indexes, preset_effect)


def reset_look(effects):
    """Look > Reset Look: drop Color Grade, Film Grain and every Look-tagged effect."""
    if not isinstance(effects, list):
        return None
    filtered = [
        effect_json for effect_json in effects
        if not isinstance(effect_json, dict)
        or (
            effect_json.get("class_name") not in LOOK_RESET_EFFECT_CLASSES
            and not is_look_managed_effect(effect_json)
        )
    ]
    if len(filtered) == len(effects):
        return None
    return filtered
