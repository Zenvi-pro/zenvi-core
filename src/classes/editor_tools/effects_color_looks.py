"""Effects and color: Look presets, color grading with named looks and LUTs, audio effect presets, chroma key.

Workstream: effects-color (see ``effects_color``). The Look tool applies the
Clip > Look menu's own rules (``classes.look_presets``); grades build on
``classes.color_presets`` and keep ONE Color Grade effect per clip.
"""

from __future__ import annotations

import copy

from classes import effect_ops
from classes.editor_tools._base import (
    CLIP_TARGET,
    ToolError,
    boolean,
    constant_keyframe,
    enum,
    get_app,
    is_locked,
    keyframe_value_at,
    layers,
    mapping,
    nullable,
    number,
    obj,
    ok,
    on_main,
    parse_color,
    refresh_preview,
    resolve_clip,
    string,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.effects_color import (
    TARGETS,
    catalog,
    clip_effects,
    compatibility_problem,
    curve_value,
    new_effect_json,
    prepare_properties,
    save_clip_effects,
    split_compatible,
    target_clips,
    wheels_value,
)

# ---------------------------------------------------------------------------
# Look presets (Clip > Look)
# ---------------------------------------------------------------------------

_FILM_GRAIN = ("35mm_fine", "35mm_classic", "35mm_gritty", "16mm_classic", "super_8", "high_iso")
LOOK_PRESETS = {
    **{f"color_{p}": ("color", p) for p in ("auto_contrast", "lift_shadows", "warm_up", "boost_color")},
    "remove_color": ("color", "reset"),
    **{f"film_grain_{p}": ("film_grain", p) for p in _FILM_GRAIN},
    "remove_film_grain": ("film_grain", "none"),
    **{f"analog_tape_{p}": ("AnalogTape", p) for p in ("subtle", "vhs", "heavy")},
    "remove_analog_tape": ("AnalogTape", "none"),
    **{f"sharpen_{p}": ("Sharpen", p) for p in ("subtle", "medium", "strong")},
    "remove_sharpen": ("Sharpen", "none"),
    **{f"blur_{p}": ("Blur", p) for p in ("soft_focus", "medium", "heavy")},
    "remove_blur": ("Blur", "none"),
    **{f"glow_{p}": ("Glow", p) for p in ("soft_white", "warm", "neon", "inner")},
    "remove_glow": ("Glow", "none"),
    **{f"shadow_{p}": ("Shadow", p) for p in ("subtle", "soft", "strong", "long")},
    "remove_shadow": ("Shadow", "none"),
    "reset_look": ("reset", None),
}
_FAMILY_CLASS = {"color": "ColorGrade", "film_grain": "FilmGrain", "reset": "ColorGrade"}


def _creator(class_name):
    try:
        return effect_ops.create_effect_json(class_name)
    except RuntimeError as exc:
        raise ToolError(f"this libopenshot build cannot create a {class_name} effect ({exc})") from None


def look_effects(effects, family, preset):
    """The clip's effect list after a Look preset (None = unchanged)."""
    from classes import look_presets
    if family == "color":
        return look_presets.apply_color_look_preset(effects, preset, _creator)
    if family == "film_grain":
        return look_presets.apply_film_grain_look_preset(effects, preset, _creator)
    if family == "reset":
        return look_presets.reset_look(effects)
    return look_presets.apply_look_effect_preset(effects, family, preset, _creator)


def _same(a, b) -> bool:
    return a == b


@editor_tool(
    "apply_look_preset_tool",
    label="Apply look preset",
    schema=obj({
        **TARGETS,
        "look": enum(sorted(LOOK_PRESETS),
                     "Clip > Look preset. color_* = Color Grade presets; film_grain_* = film stocks; "
                     "analog_tape_* = VHS/home-video; sharpen_*, blur_* (soft focus), glow_*, shadow_* "
                     "(drop shadow for cut-outs/titles); remove_<family> removes that look; reset_look "
                     "removes every look effect (Color Grade, Film Grain and Look presets)."),
    }, required=["look"]),
    covers=("look.presets",),
)
def apply_look_preset(*, look, timeline_clip_id="", clip_query="", track="", timeline_clip_ids=None, scope=""):
    """Apply one of the editor's Clip > Look presets to clips, exactly as the menu does: "add film
    grain", "make it look like VHS", "soft focus", "add a glow", "drop shadow under the logo",
    "sharpen it", "remove the look". Re-applying a preset of the same family replaces it (no stacking).

    For a real color grade ("cinematic", "teal & orange", "black and white", "fix the exposure",
    a LUT) use color_grade_clip_tool; for other effects add_effect_tool. Audio-only clips and locked
    tracks are refused. One undo step for all clips.
    """
    if look not in LOOK_PRESETS:
        raise ToolError(f"unknown look {look!r}; choose one of {', '.join(sorted(LOOK_PRESETS))}")
    family, preset = LOOK_PRESETS[look]
    check_class = _FAMILY_CLASS.get(family, family)
    clips, explicit = target_clips(timeline_clip_id, clip_query, track, timeline_clip_ids, scope)
    clips, skipped = split_compatible(clips, explicit, check_class)
    changes, rows = [], []
    for clip in clips:
        before = copy.deepcopy(clip_effects(clip))
        after = look_effects(copy.deepcopy(before), family, preset)
        if after is None or _same(before, after):
            rows.append({"timeline_clip_id": clip.id, "changed": False})
            continue
        changes.append((clip.id, after))
        rows.append({"timeline_clip_id": clip.id, "changed": True,
                     "effects": [e.get("class_name") for e in after if isinstance(e, dict)]})
    if not changes:
        state = "has no such look to remove" if look.startswith("remove_") or look == "reset_look" \
            else "already has this look"
        return ok(f"Every target clip {state}; nothing changed.", look=look, changed=False, clips=rows,
                  skipped=skipped)
    save_clip_effects(changes)
    verb = "Removed" if look.startswith("remove_") or look == "reset_look" else "Applied"
    return ok(f"{verb} look {look} on {len(changes)} clip(s).", look=look, changed=True, clips=rows,
              skipped=skipped)


# ---------------------------------------------------------------------------
# Color grade
# ---------------------------------------------------------------------------

GRADE_PARAMS = ("exposure", "contrast", "highlights", "shadows", "saturation", "vibrance", "temperature",
                "tint", "mix")

# Named looks: a Color preset (or neutral), then a built-in LUT, then parameter changes.
NAMED_LOOKS = {
    "neutral": {"base": None, "about": "every control neutral, no LUT (undo a grade)"},
    "natural": {"base": "auto_contrast", "about": "gentle contrast S-curve, a little vibrance"},
    "auto_contrast": {"base": "auto_contrast", "about": "Look > Color > Auto Contrast"},
    "lift_shadows": {"base": "lift_shadows", "about": "Look > Color > Lift Shadows (open up dark footage)"},
    "warm_up": {"base": "warm_up", "about": "Look > Color > Warm Up"},
    "boost_color": {"base": "boost_color", "about": "Look > Color > Boost Color"},
    "cinematic": {"base": "auto_contrast", "lut": "cinematic_&_blockbuster/teal_&_orange_cinema",
                  "lut_intensity": 0.6, "params": {"saturation": 0.95, "highlights": -0.1, "shadows": -0.05},
                  "about": "blockbuster teal/orange LUT at 60%, contrast curve, slightly muted"},
    "teal_orange": {"base": "auto_contrast", "lut": "teal_&_orange_vibes/signature_teal_&_orange",
                    "lut_intensity": 0.8, "params": {"vibrance": 0.1},
                    "about": "strong teal shadows / orange skin tones"},
    "warm": {"base": "warm_up", "params": {"temperature": 0.25}, "about": "warmer white balance"},
    "golden_hour": {"base": "warm_up", "lut": "teal_&_orange_vibes/sunset_orange", "lut_intensity": 0.6,
                    "params": {"temperature": 0.15}, "about": "sunset orange glow"},
    "cool": {"base": None, "params": {"temperature": -0.22, "tint": -0.03}, "about": "cooler, bluer"},
    "vintage": {"base": "lift_shadows", "lut": "film_stock_&_vintage/faded_memories", "lut_intensity": 0.7,
                "params": {"saturation": 0.85, "contrast": -0.05}, "about": "faded film, lifted blacks"},
    "black_and_white": {"base": None, "params": {"saturation": 0.0, "vibrance": 0.0, "contrast": 0.15},
                        "about": "monochrome with a little contrast"},
    "film_noir": {"base": None, "params": {"saturation": 0.0, "contrast": 0.35, "shadows": -0.12,
                                           "highlights": 0.05},
                  "about": "hard-contrast black and white"},
    "moody": {"base": None, "lut": "dark_&_moody/cold_shadows", "lut_intensity": 0.6,
              "params": {"exposure": -0.15, "contrast": 0.2, "saturation": 0.8, "shadows": -0.15},
              "about": "darker, desaturated, cold shadows"},
    "vibrant": {"base": "boost_color", "lut": "vibrant_&_colorful/color_pop", "lut_intensity": 0.5,
                "about": "punchy saturated color"},
    "bright_airy": {"base": "lift_shadows", "params": {"exposure": 0.2, "contrast": -0.08, "saturation": 0.92,
                                                       "highlights": -0.1},
                    "about": "light, soft, lifted"},
}


def _grade_param_schema(name, lo, hi, about):
    return nullable(number(f"{about} Range {lo:g}..{hi:g}; leave out to keep the current/look value.",
                           minimum=lo, maximum=hi))


def _grade_values(effect_json) -> dict:
    out = {p: round(keyframe_value_at(effect_json.get(p), 1, 0.0), 4) for p in GRADE_PARAMS}
    out["lut"] = (effect_json.get("lut_path") or "").replace("\\", "/").rsplit("/", 2)[-2:]
    out["lut"] = "/".join(out["lut"])[:-5] if effect_json.get("lut_path") else ""
    out["lut_intensity"] = round(keyframe_value_at(effect_json.get("lut_intensity"), 1, 1.0), 4)
    return out


@editor_tool(
    "color_grade_clip_tool",
    label="Color grade",
    schema=obj({
        **TARGETS,
        "look": enum([""] + sorted(NAMED_LOOKS),
                     "Named grade: " + "; ".join(f"{k} = {v['about']}" for k, v in NAMED_LOOKS.items())
                     + ". Starts from neutral; the other arguments then adjust it.", ""),
        "exposure": _grade_param_schema("exposure", -2, 2, "Brightness in stops (+0.3 = brighter)."),
        "contrast": _grade_param_schema("contrast", -1, 1, "Contrast."),
        "highlights": _grade_param_schema("highlights", -1, 1, "Bright areas (negative recovers blown skies)."),
        "shadows": _grade_param_schema("shadows", -1, 1, "Dark areas (positive lifts them)."),
        "saturation": _grade_param_schema("saturation", 0, 4, "Color intensity: 1 = unchanged, 0 = black and white."),
        "vibrance": _grade_param_schema("vibrance", -1, 1, "Boosts muted colors more than saturated ones."),
        "temperature": _grade_param_schema("temperature", -1, 1, "White balance: + warmer (orange), - cooler (blue)."),
        "tint": _grade_param_schema("tint", -1, 1, "White balance: + magenta, - green."),
        "mix": _grade_param_schema("mix", 0, 1, "Grade strength: 1 = full, 0.5 = half-way to the original."),
        "lut": string("LUT to apply: a name from list_luts_tool ('teal & orange cinema', "
                      "'film_stock_&_vintage/classic_film'), a .cube path, or 'none' to remove it.", ""),
        "lut_intensity": _grade_param_schema("lut_intensity", 0, 1, "How strongly the LUT applies."),
        "wheels": mapping("Color wheels, e.g. {\"shadows\": {\"color\": \"#2a6cff\", \"amount\": 0.15}, "
                          "\"highlights\": {\"color\": \"#ffb36b\", \"amount\": 0.12, \"luma\": 0.02}}: tint color, "
                          "amount 0-1, luma -1..1 per wheel (global, shadows, midtones, highlights)."),
        "curves": mapping("Tone curves {\"all\"|\"red\"|\"green\"|\"blue\": [[x, y], ...]} with input->output points "
                          "in 0-1, e.g. an S-curve {\"all\": [[0,0],[0.25,0.2],[0.75,0.82],[1,1]]}; 'reset' flattens."),
        "reset": boolean("Start from a neutral grade instead of the clip's current one.", False),
    }),
    background_safe=True,
    covers=("color.grade", "color.lut", "look.presets"),
)
def color_grade_clip(timeline_clip_id="", clip_query="", track="", timeline_clip_ids=None, scope="", look="",
                     exposure=None, contrast=None, highlights=None, shadows=None, saturation=None, vibrance=None,
                     temperature=None, tint=None, mix=None, lut="", lut_intensity=None, wheels=None, curves=None,
                     reset=False):
    """Color grade clips with the Color Grade effect: a named look ("make it cinematic", "teal and
    orange", "warm it up", "cooler", "vintage", "black and white", "film noir", "moody", "vibrant",
    "bright and airy"), a LUT, and/or exact controls (exposure, contrast, highlights, shadows,
    saturation, vibrance, temperature, tint, wheels, curves). Each clip keeps ONE Color Grade effect:
    it is updated in place, never stacked. One undo step for all clips.

    Without look/reset only the given controls change and the rest of the current grade stays
    ("a bit brighter" -> exposure=0.3). With a look the grade starts from neutral, then the look,
    then your controls ("cinematic but less saturated" -> look="cinematic", saturation=0.8).
    To fix exposure or color casts objectively, call analyze_frame_colors_tool first and use its
    suggested values, then analyze again to verify. Refused: audio-only clips, locked tracks,
    unknown LUT names (closest matches listed), out-of-range values.
    """
    explicit_params = {k: v for k, v in (("exposure", exposure), ("contrast", contrast), ("highlights", highlights),
                                         ("shadows", shadows), ("saturation", saturation), ("vibrance", vibrance),
                                         ("temperature", temperature), ("tint", tint), ("mix", mix))
                       if v is not None}
    if look and look not in NAMED_LOOKS:
        raise ToolError(f"unknown look {look!r}; choose one of {', '.join(sorted(NAMED_LOOKS))}")
    if not (look or explicit_params or lut or lut_intensity is not None or wheels or curves or reset):
        raise ToolError("say what to change: a look, a LUT, or one of exposure/contrast/.../curves")
    clips, explicit = target_clips(timeline_clip_id, clip_query, track, timeline_clip_ids, scope)
    clips, skipped = split_compatible(clips, explicit, "ColorGrade")

    spec = NAMED_LOOKS.get(look) or {}
    luts = effect_ops.list_luts() if (lut or spec.get("lut")) else None
    try:
        look_lut = effect_ops.resolve_lut(spec["lut"], luts) if spec.get("lut") else ""
        lut_path = effect_ops.resolve_lut(lut, luts) if lut else None
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if curves is not None and not isinstance(curves, dict):
        raise ToolError('curves is {"all": [[0,0],[1,1]], "red": [...]}')
    for key in (curves or {}):
        if key not in ("all", "red", "green", "blue"):
            raise ToolError(f"unknown curve {key!r}; use all, red, green or blue")

    from classes.color_presets import apply_color_grade_preset, neutral_color_grade
    changes, rows = [], []
    for clip in clips:
        effects = copy.deepcopy(clip_effects(clip))
        idxs = [i for i, e in enumerate(effects) if e.get("class_name") == "ColorGrade"]
        existing = effects[idxs[0]] if idxs else None
        if look or reset or existing is None:
            payload = new_effect_json("ColorGrade")
            payload = apply_color_grade_preset(payload, spec["base"]) if spec.get("base") \
                else neutral_color_grade(payload)
            if existing is not None:
                payload["id"] = existing.get("id", payload.get("id"))
                if "order" in existing:
                    payload["order"] = existing["order"]
                if existing.get("ui"):
                    payload["ui"] = existing["ui"]
            if look_lut:
                payload["lut_path"] = look_lut
                payload["lut_intensity"] = constant_keyframe(spec.get("lut_intensity", 1.0))
            values = dict(spec.get("params") or {})
        else:
            payload = copy.deepcopy(existing)
            values = {}
        values.update(explicit_params)
        if lut_intensity is not None:
            values["lut_intensity"] = lut_intensity
        payload.update(prepare_properties("ColorGrade", values, payload, clip.data))
        if lut_path is not None:
            payload["lut_path"] = lut_path
        if wheels:
            payload["wheels"] = wheels_value(wheels, payload.get("wheels"))
        for key, pts in (curves or {}).items():
            payload[f"curve_{key}"] = curve_value(f"curves.{key}", pts)
        if existing is not None and payload == existing:
            rows.append({"timeline_clip_id": clip.id, "effect_id": existing.get("id"), "changed": False,
                         "grade": _grade_values(payload)})
            continue
        if idxs:
            effects[idxs[0]] = payload
            for i in reversed(idxs[1:]):
                del effects[i]
        else:
            effects.append(payload)
        changes.append((clip.id, effects))
        rows.append({"timeline_clip_id": clip.id, "effect_id": payload.get("id"), "changed": True,
                     "grade": _grade_values(payload)})
    if not changes:
        return ok("The grade already has these values; nothing changed.", changed=False, clips=rows,
                  skipped=skipped)
    save_clip_effects(changes)
    what = f"look {look}" if look else ", ".join(sorted(explicit_params) + (["lut"] if lut else [])) or "grade"
    return ok(f"Graded {len(changes)} clip(s): {what}.", look=look, changed=True, clips=rows, skipped=skipped)


@editor_tool(
    "list_luts_tool",
    label="List LUTs",
    schema=obj({"query": string("Filter by words in the LUT name or category ('teal', 'film', 'moody').", "")}),
    read_only=True,
    covers=("color.lut",),
)
def list_luts(query=""):
    """List the LUTs (3D color lookup tables, .cube) available for color grading: the 50 built-in ones
    in 6 categories (Cinematic & Blockbuster, Dark & Moody, Film Stock & Vintage, Teal & Orange Vibes,
    Utility & Correction, Vibrant & Colorful) plus any in the user's LUT folder. Pass an `id` to
    color_grade_clip_tool(lut=...) to apply one with an intensity.
    """
    words = [w for w in str(query or "").lower().replace("&", " ").split() if w]
    rows = []
    for lut in effect_ops.list_luts():
        hay = f"{lut['id']} {lut['name']} {lut['category']}".lower().replace("_", " ")
        if words and not all(w in hay for w in words):
            continue
        rows.append({"id": lut["id"], "name": lut["name"], "category": lut["category"] or "User-Defined",
                     "builtin": lut["builtin"]})
    if not rows:
        raise ToolError(f"no LUT matches {query!r}" if query else "no LUT files were found")
    return ok(f"{len(rows)} LUT(s)" + (f" matching {query!r}" if query else ""), luts=rows)


# ---------------------------------------------------------------------------
# Audio effect presets
# ---------------------------------------------------------------------------

AUDIO_PRESETS = {
    "voice_compressor": ("Compressor", {"threshold": -18.0, "ratio": 3.0, "attack": 5.0, "release": 120.0,
                                        "makeup_gain": 3.0}, "even out a voice (gentle 3:1)"),
    "strong_compressor": ("Compressor", {"threshold": -28.0, "ratio": 8.0, "attack": 2.0, "release": 80.0,
                                         "makeup_gain": 6.0}, "podcast/broadcast squash"),
    "noise_gate": ("Expander", {"threshold": -45.0, "ratio": 4.0, "attack": 1.0, "release": 100.0,
                                "makeup_gain": 0.0}, "quiet the room noise between phrases"),
    "echo": ("Echo", {"echo_time": 0.25, "feedback": 0.35, "mix": 0.35}, "clear echo"),
    "slapback": ("Echo", {"echo_time": 0.09, "feedback": 0.15, "mix": 0.3}, "short room slap"),
    "canyon_echo": ("Echo", {"echo_time": 0.6, "feedback": 0.55, "mix": 0.45}, "long repeating echo"),
    "low_cut": ("ParametricEQ", {"filter_type": 1, "frequency": 90, "q_factor": 0.707, "gain": 0},
                "remove rumble / wind below 90 Hz"),
    "high_cut": ("ParametricEQ", {"filter_type": 0, "frequency": 8000, "q_factor": 0.707, "gain": 0},
                 "tame hiss above 8 kHz"),
    "bass_boost": ("ParametricEQ", {"filter_type": 2, "frequency": 120, "q_factor": 0.707, "gain": 6},
                   "+6 dB low shelf at 120 Hz"),
    "treble_boost": ("ParametricEQ", {"filter_type": 3, "frequency": 6000, "q_factor": 0.707, "gain": 5},
                     "+5 dB high shelf at 6 kHz"),
    "voice_presence": ("ParametricEQ", {"filter_type": 6, "frequency": 3000, "q_factor": 1.0, "gain": 4},
                       "+4 dB at 3 kHz for clarity"),
    "telephone": ("ParametricEQ", {"filter_type": 4, "frequency": 1500, "q_factor": 1.2, "gain": 0},
                  "band-pass phone/radio voice"),
    "robot": ("Robotization", {"fft_size": 5, "hop_size": 1, "window_type": 2}, "robotic voice"),
    "whisper": ("Whisperization", {"fft_size": 4, "hop_size": 2, "window_type": 2}, "whispering voice"),
    "distortion": ("Distortion", {"distortion_type": 1, "input_gain": 12, "output_gain": -8, "tone": 4},
                   "soft-clipped grit"),
    "add_noise": ("Noise", {"level": 15}, "white noise bed (15%)"),
    "sync_delay": ("Delay", {"delay_time": 0.1}, "delay the sound (fix A/V sync; set delay_time seconds)"),
}


@editor_tool(
    "apply_audio_effect_tool",
    label="Apply audio effect",
    schema=obj({
        **TARGETS,
        "preset": enum(sorted(AUDIO_PRESETS),
                       "Audio effect preset: " + "; ".join(f"{k} = {v[2]}" for k, v in AUDIO_PRESETS.items()) + "."),
        "properties": mapping("Override preset values, e.g. {\"ratio\": 4} or {\"delay_time\": 0.25}; names from "
                              "list_effects_tool(effect=...)."),
    }, required=["preset"]),
    covers=("audio.effects",),
)
def apply_audio_effect(*, preset, timeline_clip_id="", clip_query="", track="", timeline_clip_ids=None, scope="",
                       properties=None):
    """Put an audio effect on clips' sound with sensible settings: "add a compressor to the voice", "add
    echo", "remove the low rumble", "boost the bass", "make it sound like a phone call", "robot voice",
    "whisper effect", "noise gate", "delay the audio 100 ms to fix sync". A clip that already has the
    same effect type is updated (not stacked). Tune further with update_effect_tool; remove with
    remove_effect_tool. For volume and ducking use the volume tools, not this. Refused: clips without
    sound (images, titles, muted-in-source), locked tracks. One undo step.
    """
    if preset not in AUDIO_PRESETS:
        raise ToolError(f"unknown preset {preset!r}; choose one of {', '.join(sorted(AUDIO_PRESETS))}")
    class_name, values, _about = AUDIO_PRESETS[preset]
    if class_name not in catalog():
        raise ToolError(f"this libopenshot build has no {class_name} effect")
    merged = dict(values)
    if properties:
        if not isinstance(properties, dict):
            raise ToolError('properties is an object like {"ratio": 4}')
        merged.update(properties)
    clips, explicit = target_clips(timeline_clip_id, clip_query, track, timeline_clip_ids, scope)
    clips, skipped = split_compatible(clips, explicit, class_name)
    changes, rows = [], []
    for clip in clips:
        effects = copy.deepcopy(clip_effects(clip))
        idxs = [i for i, e in enumerate(effects) if e.get("class_name") == class_name]
        target = effects[idxs[0]] if idxs else new_effect_json(class_name)
        props = prepare_properties(class_name, merged, target, clip.data)
        changed = {k: v for k, v in props.items() if target.get(k) != v}
        if idxs and not changed:
            rows.append({"timeline_clip_id": clip.id, "effect_id": target.get("id"), "action": "unchanged"})
            continue
        target.update(props)
        if not idxs:
            effects.append(target)
        changes.append((clip.id, effects))
        rows.append({"timeline_clip_id": clip.id, "effect_id": target.get("id"),
                     "action": "updated" if idxs else "added"})
    if not changes:
        return ok(f"{preset} is already applied with these settings; nothing changed.", changed=False,
                  clips=rows, skipped=skipped)
    save_clip_effects(changes)
    return ok(f"Applied {preset} ({class_name}) to {len(changes)} clip(s).", effect=class_name, preset=preset,
              settings=merged, changed=True, clips=rows, skipped=skipped)


# ---------------------------------------------------------------------------
# Chroma key
# ---------------------------------------------------------------------------

KEY_METHODS = {"basic_soft": 11, "basic": 0, "hsv_hue": 1, "hsv_saturation": 2, "hsl_saturation": 3,
               "hsv_value": 4, "hsl_luminance": 5, "lch_luminosity": 6, "lch_chroma": 7, "lch_hue": 8,
               "cie_distance": 9, "cbcr_vector": 10}
_KEY_COLORS = {"green": "#00b140", "blue": "#0047bb"}


def _free_on_layer(layer, start, end, exclude_id) -> bool:
    from classes.query import Clip
    for c in Clip.filter(layer=layer):
        if c.id == exclude_id:
            continue
        s = float(c.data.get("position") or 0.0)
        e = s + max(0.0, float(c.data.get("end") or 0.0) - float(c.data.get("start") or 0.0))
        if s < end - 1e-6 and e > start + 1e-6:
            return False
    return True


def _placement(keyed, background, align):
    """(layer, position, new_track_number|None) putting *keyed* above *background*, overlapping it."""
    kd, bd = keyed.data, background.data
    k_dur = max(0.0, float(kd.get("end") or 0.0) - float(kd.get("start") or 0.0))
    k_pos = float(kd.get("position") or 0.0)
    b_pos = float(bd.get("position") or 0.0)
    b_end = b_pos + max(0.0, float(bd.get("end") or 0.0) - float(bd.get("start") or 0.0))
    overlaps = k_pos < b_end and k_pos + k_dur > b_pos
    if align == "start" or (align == "auto" and not overlaps):
        k_pos = b_pos
    b_layer = int(bd.get("layer") or 0)
    k_layer = int(kd.get("layer") or 0)
    if k_layer > b_layer and _free_on_layer(k_layer, k_pos, k_pos + k_dur, keyed.id):
        return k_layer, k_pos, None
    above = sorted(int(t.get("number") or 0) for t in layers() if int(t.get("number") or 0) > b_layer)
    for number_ in above:
        if not is_locked(number_) and _free_on_layer(number_, k_pos, k_pos + k_dur, keyed.id):
            return number_, k_pos, None
    top = max([int(t.get("number") or 0) for t in layers()] + [b_layer])
    return top + 1000000, k_pos, top + 1000000


@editor_tool(
    "chroma_key_clip_tool",
    label="Chroma key",
    schema=obj({
        **CLIP_TARGET,
        "key_color": string("Screen color to remove: 'auto' (sample the frame edges), 'green', 'blue', or #RRGGBB.",
                            "auto"),
        "method": enum(sorted(KEY_METHODS), "Keying method; basic_soft (default) suits most green/blue screens, "
                       "hsv_hue helps uneven lighting, cbcr_vector for spill-heavy footage.", "basic_soft"),
        "fuzz": number("Tolerance around the key color (0-125): raise it if screen remains, lower it if the "
                       "subject gets holes.", 20.0, minimum=0, maximum=125),
        "halo": number("Edge softening/spill cleanup (0-125).", 10.0, minimum=0, maximum=125),
        "background_clip_id": string("Background clip to show through (timeline clip id). The keyed clip is put "
                                     "on a track above it.", ""),
        "background_query": string("Or describe the background clip ('the beach clip').", ""),
        "align": enum(["auto", "start", "keep"],
                      "With a background: auto = move the keyed clip to the background's start only if they do "
                      "not overlap in time; start = always start together; keep = keep the keyed clip's time.",
                      "auto"),
        "sample_time": nullable(number("Timeline time to sample the key color from (default: middle of the clip).")),
    }),
    background_safe=True,
    covers=("color.chroma_key",),
)
def chroma_key_clip(timeline_clip_id="", clip_query="", track="", key_color="auto", method="basic_soft",
                    fuzz=20.0, halo=10.0, background_clip_id="", background_query="", align="auto",
                    sample_time=None):
    """Remove a green or blue screen from a clip with the Chroma Key effect, and optionally composite it
    over a background clip ("remove the green screen and put me on the beach"). key_color='auto'
    samples the screen color from the frame edges; one ChromaKey per clip (updated if present).
    With background_clip_id/background_query the keyed clip moves to a free track above the
    background (a new top track if needed) so the background shows through, and to the background's
    start if they did not overlap. One undo step for key + placement.

    Check the result with analyze_frame_colors_tool on the keyed clip (transparent_pct should be the
    screen's share of the frame) or on the composite. Refused: audio-only clips, locked tracks, a
    background that is the same clip, 'auto' when the frame edges are not a green/blue screen.
    """
    clip = resolve_clip(timeline_clip_id, clip_query, track)
    problem = compatibility_problem(clip, "ChromaKey")
    if problem:
        raise ToolError(problem)
    background = None
    if background_clip_id or background_query:
        background = resolve_clip(background_clip_id, background_query, "")
        if background.id == clip.id:
            raise ToolError("the background must be a different clip than the keyed one")
    if method not in KEY_METHODS:
        raise ToolError(f"method must be one of {', '.join(sorted(KEY_METHODS))}")

    color_source = "given"
    key = str(key_color or "auto").strip().lower()
    if key == "auto":
        from classes.editor_tools.effects_color_analysis import sample_screen_color
        hex_color, detail = sample_screen_color(clip, sample_time)
        color_source = f"sampled ({detail})"
    else:
        hex_color = _KEY_COLORS.get(key, key_color)
        r, g, b, _a = parse_color(hex_color)
        hex_color = "#%02x%02x%02x" % (r, g, b)

    effects = copy.deepcopy(clip_effects(clip))
    idxs = [i for i, e in enumerate(effects) if e.get("class_name") == "ChromaKey"]
    target = effects[idxs[0]] if idxs else new_effect_json("ChromaKey")
    props = prepare_properties("ChromaKey", {"color": hex_color, "keymethod": KEY_METHODS[method],
                                             "fuzz": fuzz, "halo": halo}, target, clip.data)
    target.update(props)
    if not idxs:
        effects.append(target)

    placement = None
    if background is not None:
        layer, position, new_track = _placement(clip, background, align)
        placement = {"layer": layer, "position": round(position, 4), "new_track": new_track is not None}

    def _apply():
        from classes.query import Clip, Track
        if not Clip.get(id=clip.id):
            raise ToolError(f"clip {clip.id} was removed while the tool ran; nothing changed")
        updates = get_app().updates
        values = {"effects": effects}
        if placement:
            if placement["new_track"]:
                t = Track()
                t.data = {"number": placement["layer"], "y": 0, "label": "", "lock": False}
                t.save()
            if int(clip.data.get("layer") or 0) != placement["layer"]:
                values["layer"] = placement["layer"]
            if abs(float(clip.data.get("position") or 0.0) - placement["position"]) > 1e-6:
                values["position"] = placement["position"]
        updates.update(["clips", {"id": clip.id}], values)
        refresh_preview()

    on_main(_apply)
    summary = f"Keyed out {hex_color} ({color_source}, {method}, fuzz {fuzz:g}, halo {halo:g}) on clip {clip.id}."
    receipt = {"timeline_clip_id": clip.id, "effect_id": target.get("id"), "key_color": hex_color,
               "key_color_source": color_source, "method": method, "fuzz": fuzz, "halo": halo}
    if placement:
        track_no = ui_track_number(placement["layer"])
        summary += (f" It now sits on track {track_no} above background {background.id}"
                    + (" (new track)" if placement["new_track"] else "")
                    + f", starting at {placement['position']:g}s.")
        receipt.update(background_clip_id=background.id, track=track_no, layer=placement["layer"],
                       position=placement["position"], new_track=placement["new_track"])
    return ok(summary, **receipt)

