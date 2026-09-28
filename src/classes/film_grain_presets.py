"""
 @file
 @brief Reusable Film Grain preset payload helpers
"""

import copy


FILM_GRAIN_CLASS_NAME = "FilmGrain"

FILM_GRAIN_PRESET_NONE = "none"
FILM_GRAIN_PRESET_35MM_FINE = "35mm_fine"
FILM_GRAIN_PRESET_35MM_CLASSIC = "35mm_classic"
FILM_GRAIN_PRESET_35MM_GRITTY = "35mm_gritty"
FILM_GRAIN_PRESET_16MM_CLASSIC = "16mm_classic"
FILM_GRAIN_PRESET_SUPER_8 = "super_8"
FILM_GRAIN_PRESET_HIGH_ISO = "high_iso"


def _constant_property(value):
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


def _set_scalar(effect_json, key, value):
    effect_json[key] = _constant_property(value)


def is_film_grain_effect(effect_json):
    if not isinstance(effect_json, dict):
        return False
    return effect_json.get("class_name") == FILM_GRAIN_CLASS_NAME


def _base_values():
    return {
        "amount": 0.25,
        "size": 0.20,
        "softness": 0.25,
        "clump": 0.20,
        "shadows": 0.80,
        "midtones": 1.00,
        "highlights": 0.55,
        "color_amount": 0.20,
        "color_variation": 0.35,
        "evolution": 0.65,
        "coherence": 0.55,
    }


def apply_film_grain_preset(effect_json, preset_name):
    payload = copy.deepcopy(effect_json or {})

    values = _base_values()
    if preset_name == FILM_GRAIN_PRESET_35MM_FINE:
        values.update({
            "amount": 0.14,
            "size": 0.12,
            "softness": 0.35,
            "clump": 0.10,
            "shadows": 0.65,
            "midtones": 0.70,
            "highlights": 0.35,
            "color_amount": 0.08,
            "color_variation": 0.20,
            "evolution": 0.45,
            "coherence": 0.75,
        })
    elif preset_name == FILM_GRAIN_PRESET_35MM_CLASSIC:
        values.update({
            "amount": 0.24,
            "size": 0.18,
            "softness": 0.22,
            "clump": 0.18,
            "shadows": 0.85,
            "midtones": 1.00,
            "highlights": 0.55,
            "color_amount": 0.16,
            "color_variation": 0.30,
            "evolution": 0.60,
            "coherence": 0.62,
        })
    elif preset_name == FILM_GRAIN_PRESET_35MM_GRITTY:
        values.update({
            "amount": 0.34,
            "size": 0.32,
            "softness": 0.28,
            "clump": 0.30,
            "shadows": 0.95,
            "midtones": 1.00,
            "highlights": 0.62,
            "color_amount": 0.20,
            "color_variation": 0.38,
            "evolution": 0.66,
            "coherence": 0.56,
        })
    elif preset_name == FILM_GRAIN_PRESET_16MM_CLASSIC:
        values.update({
            "amount": 0.42,
            "size": 0.46,
            "softness": 0.38,
            "clump": 0.44,
            "shadows": 1.00,
            "midtones": 1.00,
            "highlights": 0.70,
            "color_amount": 0.28,
            "color_variation": 0.48,
            "evolution": 0.75,
            "coherence": 0.45,
        })
    elif preset_name == FILM_GRAIN_PRESET_SUPER_8:
        values.update({
            "amount": 0.62,
            "size": 0.72,
            "softness": 0.50,
            "clump": 0.70,
            "shadows": 1.00,
            "midtones": 0.95,
            "highlights": 0.85,
            "color_amount": 0.42,
            "color_variation": 0.65,
            "evolution": 0.88,
            "coherence": 0.32,
        })
    elif preset_name == FILM_GRAIN_PRESET_HIGH_ISO:
        values.update({
            "amount": 0.52,
            "size": 0.24,
            "softness": 0.18,
            "clump": 0.24,
            "shadows": 1.00,
            "midtones": 0.95,
            "highlights": 0.42,
            "color_amount": 0.55,
            "color_variation": 0.78,
            "evolution": 0.82,
            "coherence": 0.38,
        })
    else:
        raise ValueError("Unknown film grain preset: {}".format(preset_name))

    for key, value in values.items():
        _set_scalar(payload, key, value)

    if "seed" not in payload:
        payload["seed"] = 1

    return payload
