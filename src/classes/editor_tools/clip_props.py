"""Clip look and motion: properties, keyframes, fades, motion presets, transforms and layouts, volume presets, copying keyframes.

Workstream: clip-props. Tools register themselves with ``@editor_tool`` from
``classes.editor_tools._registry``; helpers live in ``classes.editor_tools._base``
and the property/keyframe model in ``clip_props_model``. The menu presets
(Fade, Motion, Transform, Volume) are in ``clip_props_presets``.
"""

from __future__ import annotations

import copy

from classes.editor_tools import clip_props_model as cpm
from classes.editor_tools._base import (
    BEZIER, CLIP_TARGET, CLIPS_TARGET, CONSTANT, ToolError, array, boolean, describe_clip, enum, get_app,
    integer, interpolation_code, is_locked, mapping, nullable, number, obj, ok, playhead_seconds,
    refresh_preview, resolve_clip, resolve_clips, string, th, ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.keyframe_rules import COPY_KEYFRAME_GROUPS, default_keyframe_value, keyframe_value

# The Clip menu's Copy > Keyframes groups (keyframe_rules is the one definition).
KEYFRAME_GROUPS = tuple(COPY_KEYFRAME_GROUPS)

# CLIPS_TARGET without a mutable [] default, so handlers can default to None.
TARGETS = {
    **CLIPS_TARGET,
    "timeline_clip_ids": array({"type": "string"}, "Timeline clip ids to act on (from get_timeline_state_tool)."),
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def resolve_targets(timeline_clip_ids=None, clip_query="", track="", scope=""):
    """(clips, skipped) for a batch tool.

    Clips named by id or query are refused when their track is locked; a
    scope ('selected' / 'track' / 'all') skips locked clips and reports them.
    """
    ids = []
    for cid in timeline_clip_ids or []:
        text = str(cid).strip()
        if text and text not in ids:
            ids.append(text)
    explicit = bool(ids) or not str(scope or "").strip()
    clips = resolve_clips(ids or None, clip_query, track, scope)
    kept, skipped, seen = [], [], set()
    for clip in clips:
        if clip.id in seen:
            continue
        seen.add(clip.id)
        layer = int(clip.data.get("layer") or 0)
        if is_locked(layer):
            label = ui_track_number(layer) or layer
            if explicit:
                raise ToolError(f"clip {clip.id} is on locked track {label}; unlock the track first")
            skipped.append({"timeline_clip_id": clip.id, "reason": f"track {label} is locked"})
            continue
        kept.append(clip)
    if not kept:
        raise ToolError("every matching clip is on a locked track; unlock it first")
    return kept, skipped


def save_clip_values(clip_id: str, values: dict) -> None:
    """Partial update of one clip (keeps reader, effects and metadata) through the update system."""
    get_app().updates.update(["clips", {"id": clip_id}], values)


def refresh_waveforms(clip_datas: list, tid) -> None:
    """Redraw the timeline waveform of clips whose volume changed (only if one is shown)."""
    files = {}
    for data in clip_datas:
        if (data.get("ui") or {}).get("audio_data") and data.get("file_id"):
            files.setdefault(str(data["file_id"]), []).append(str(data.get("id")))
    if files:
        from classes.waveform import get_audio_data
        get_audio_data(files, transaction_id=tid)


def _transaction():
    return th()._transaction(get_app())


def _num(value):
    return round(value, 4) if isinstance(value, float) else value


def _short(value) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


# ---------------------------------------------------------------------------
# clip.properties_get
# ---------------------------------------------------------------------------

_SUMMARY_KEYS = ("alpha", "scale", "gravity", "scale_x", "scale_y", "location_x", "location_y", "rotation",
                 "volume", "composite")


@editor_tool(
    "get_clip_properties_tool",
    label="Read clip properties",
    schema=obj({
        **CLIP_TARGET,
        "at_seconds": nullable(number(
            "Timeline time (seconds) to read the values at. Default: the playhead if it is over the clip, "
            "otherwise the clip's first frame.")),
        "properties": array({"type": "string"}, "Only these properties (keys such as 'alpha', 'scale_x' or "
                            "labels such as 'Scale X'). Empty = every property.", []),
        "include_keyframes": boolean("List every keyframe of animated properties (frame, clip and timeline "
                                     "seconds, value, interpolation, ease).", True),
    }),
    read_only=True,
    covers=("clip.properties_get",),
)
def get_clip_properties(timeline_clip_id="", clip_query="", track="", at_seconds=None, properties=[],
                        include_keyframes=True):
    """Read every property of one timeline clip, the way the Properties dock shows it.

    Use this before changing how a clip looks or moves: it tells you each
    property's value at a time (opacity 'alpha', 'scale' mode, 'gravity',
    'scale_x'/'scale_y', 'location_x'/'location_y', 'rotation', 'shear_x'/'shear_y',
    'origin_x'/'origin_y', blend mode 'composite', 'volume', 'wave_color',
    'corner_radius', 'margin', ...), its allowed range or choices, whether it can
    be animated and, for animated ones, every keyframe with its frame, clip
    seconds (0 = the clip's first visible frame), timeline seconds, value,
    interpolation and ease. Also lists the clip's effects (id, class). Changes
    nothing.

    Example: get_clip_properties_tool(timeline_clip_id="ABC123", properties=["scale_x", "alpha"])
    """
    clip = resolve_clip(timeline_clip_id, clip_query, track)
    data = clip.data
    ct = cpm.ClipTime.of(data)
    if at_seconds is None:
        t = playhead_seconds()
        frame = ct.from_timeline_seconds(t) if ct.contains(ct.from_timeline_seconds(t)) else ct.first_frame
    else:
        frame = ct.from_timeline_seconds(at_seconds)
        if not ct.contains(frame):
            raise ToolError(f"at_seconds={at_seconds} is outside clip {clip.id} "
                            f"(timeline {ct.position:.3f}-{ct.position + ct.duration:.3f} s)")
    keys = [cpm.resolve_key(name) for name in properties] if properties else cpm.settable_keys()
    out, animated_keys = {}, []
    for key in keys:
        info = cpm.property_info(key, data)
        entry = {"value": cpm.display_value(info, _num(cpm.value_at(info, data, frame))), **info.describe()}
        if info.keyframable:
            is_animated = cpm.animated(info, data)
            entry["animated"] = is_animated
            if is_animated:
                animated_keys.append(key)
                if include_keyframes:
                    if info.kind == "color":
                        entry["keyframes"] = {ch: cpm.describe_points((data.get(key) or {}).get(ch), ct)
                                              for ch in cpm.COLOR_CHANNELS}
                    else:
                        entry["keyframes"] = cpm.describe_points(data.get(key), ct)
        out[key] = entry
    effects = [{"id": e.get("id"), "class_name": e.get("class_name"), "name": e.get("name")}
               for e in data.get("effects") or [] if isinstance(e, dict)]
    head = describe_clip(clip)
    shown = ", ".join(f"{k} {_short(out[k]['value'])}" for k in _SUMMARY_KEYS if k in out)
    summary = (f"Clip {clip.id} {head['title']!r} (track {head['track']}, {head['position']:.2f}-{head['end']:.2f} s) "
               f"at {ct.timeline_seconds(frame):.2f} s: {shown or 'see receipt'}. "
               f"Animated: {', '.join(animated_keys) or 'nothing'}."
               + (f" Effects: {', '.join(str(e['class_name']) for e in effects)}." if effects else ""))
    return ok(summary, **head, frame=frame, at_seconds=ct.timeline_seconds(frame),
              clip_seconds=ct.clip_seconds(frame), first_frame=ct.first_frame, end_frame=ct.end_frame,
              properties=out, effects=effects)


# ---------------------------------------------------------------------------
# clip.properties_set
# ---------------------------------------------------------------------------

@editor_tool(
    "set_clip_properties_tool",
    label="Set clip properties",
    schema=obj({
        **TARGETS,
        "properties": mapping(
            "Property -> value, several at once. Keys are property keys or their Properties-dock labels; "
            "values are numbers, choice names or colors. Examples: {\"alpha\": 0.5} (50% opacity), "
            "{\"scale\": \"Best Fit\"} (Crop | Best Fit | Stretch | None), {\"gravity\": \"top right\"}, "
            "{\"composite\": \"multiply\"} (blend mode: Normal, Darken, Multiply, Color Burn, Lighten, Screen, "
            "Color Dodge, Add, Overlay, Soft Light, Hard Light, Difference, Exclusion), {\"scale_x\": 0.4, "
            "\"scale_y\": 0.4}, {\"location_x\": 0.25} (-1..1 = one frame width), {\"rotation\": 15} (degrees), "
            "{\"scale_x\": -1} (mirror), {\"corner_radius\": 0.1, \"margin\": 0.04}, {\"volume\": 0.8} "
            "(0-1.3), {\"wave_color\": \"#ff8800\"}, {\"display\": \"Timeline\"} (frame-number overlay), "
            "{\"mixing\": \"Average\"}, {\"waveform\": true}."),
        "at_seconds": nullable(number(
            "Leave empty for static values (an animated property is flattened to the new value). Give a "
            "timeline time (seconds) to set the value only at that moment, as a keyframe, like editing in the "
            "Properties dock with the playhead there; other keyframes stay.")),
    }, required=["properties"]),
    covers=("clip.properties_set",),
)
def set_clip_properties(properties, timeline_clip_ids=None, clip_query="", track="", scope="", at_seconds=None):
    """Set one or more properties of one or more timeline clips to fixed values.

    Use it for "make it 50% transparent" (alpha 0.5), "use the multiply blend
    mode" (composite), "fit/fill the frame" (scale Best Fit / Crop), "pin it to the
    top right" (gravity), "make it half size" (scale_x/scale_y 0.5), "move it
    left" (location_x), "tilt it 10 degrees" (rotation), "mirror it"
    (scale_x -1), "rounded corners" (corner_radius), "turn it down" (volume),
    "show frame numbers" (display). Several properties and several clips in
    one call, one undo step. A static value replaces any animation of that
    property (the receipt says so); to animate or change only part of a
    curve use set_keyframes_tool, and for fades, zooms, slides or layouts use
    apply_clip_preset_tool. Timeline position, trim and track are not
    properties here (use the timeline editing tools). Clips on locked tracks
    are refused (or skipped with scope).

    Example: set_clip_properties_tool(timeline_clip_ids=["ABC123"],
    properties={"alpha": 0.5, "composite": "multiply"})
    """
    if not isinstance(properties, dict) or not properties:
        raise ToolError('properties must be an object such as {"alpha": 0.5}')
    keys = {}
    for name in properties:
        key = cpm.resolve_key(name)
        if key in keys.values():
            raise ToolError(f"{name!r} sets {key}, which is already in properties")
        if key == "time":
            raise ToolError("time maps clip frames to source frames (speed); change speed with the speed tools, "
                            "or animate it with set_keyframes_tool(property='time')")
        keys[name] = key
    clips, skipped = resolve_targets(timeline_clip_ids, clip_query, track, scope)

    plans = []
    for clip in clips:
        data = clip.data
        ct = cpm.ClipTime.of(data)
        frame = None
        if at_seconds is not None:
            frame = ct.from_timeline_seconds(at_seconds)
            if not ct.contains(frame):
                raise ToolError(f"at_seconds={at_seconds} is outside clip {clip.id} "
                                f"(timeline {ct.position:.3f}-{ct.position + ct.duration:.3f} s)")
        values, changed, flattened = {}, {}, {}
        for name, raw in properties.items():
            key = keys[name]
            info = cpm.property_info(key, data)
            new = cpm.coerce_value(info, raw, clip.id)
            probe = frame if frame is not None else ct.first_frame
            before = cpm.value_at(info, data, probe)
            if info.keyframable:
                curve, flat_count = _set_curve_value(info, data.get(key), new, frame)
                if curve == data.get(key):
                    continue
                if flat_count:
                    flattened[key] = flat_count
                values[key] = curve
            else:
                if frame is not None:
                    raise ToolError(f"{key} cannot be animated, so it takes no at_seconds; set it without one")
                stored = cpm.stored_static(info, new, data.get(key))
                if stored == data.get(key):
                    continue
                values[key] = stored
            changed[key] = {"before": cpm.display_value(info, _num(before)),
                            "after": cpm.display_value(info, _num(new if not isinstance(new, bool) else int(new)))}
        if values:
            plans.append((clip, values, changed, flattened))

    names = ", ".join(f"{keys[n]}={_short(v)}" for n, v in properties.items())
    if not plans:
        return ok(f"Nothing to change: {names} already set on {len(clips)} clip(s).", changed=False,
                  timeline_clip_ids=[c.id for c in clips], skipped=skipped)
    with _transaction() as tid:
        for clip, values, _changed, _flat in plans:
            save_clip_values(clip.id, values)
        refresh_waveforms([dict(c.data, id=c.id) for c, v, _, _ in plans if "volume" in v], tid)
    refresh_preview()
    receipt = [{"timeline_clip_id": c.id, "changed": ch, **({"flattened_animation": fl} if fl else {})}
               for c, _v, ch, fl in plans]
    flat_note = ""
    if any(fl for *_, fl in plans):
        flat_note = " Replaced an animation with a static value on: " + ", ".join(
            sorted({k for *_, fl in plans for k in fl})) + "."
    when = f" at {at_seconds:.3f} s (keyframe)" if at_seconds is not None else ""
    return ok(f"Set {names}{when} on {len(plans)} clip(s).{flat_note}", changed=True, clips=receipt,
              skipped=skipped)


def _set_curve_value(info, curve, new, frame):
    """(new curve, points flattened) for a static value (frame None) or a keyframe at *frame*."""
    interp = CONSTANT if info.is_choice else BEZIER
    if info.kind == "color":
        base = curve if isinstance(curve, dict) else {}
        out, flattened = {}, 0
        for ch, val in zip(cpm.COLOR_CHANNELS, new):
            out[ch], n = _set_curve_value(_as_float_info(info), base.get(ch), float(val), frame)
            flattened = max(flattened, n)
        return out, flattened
    points = cpm.curve_points(curve)
    if frame is None:
        flattened = len(points) if len(points) > 1 else 0
        if len(points) == 1 and float(points[0]["co"]["Y"]) == float(new):
            return curve, 0
        return {"Points": [cpm.make_point(1, new, interp)]}, flattened
    kept = copy.deepcopy(points)
    for p in kept:
        if float(p["co"]["X"]) == float(frame):
            if float(p["co"]["Y"]) == float(new):
                return curve, 0
            p["co"]["Y"] = float(new)
            return {"Points": kept}, 0
    kept.append(cpm.make_point(frame, new, interp))
    kept.sort(key=lambda p: float(p["co"]["X"]))
    return {"Points": kept}, 0


def _as_float_info(info):
    return cpm.PropertyInfo(key=info.key, label=info.label, kind="float", keyframable=True, choices=(),
                            minimum=0.0, maximum=255.0, default=0.0)


# ---------------------------------------------------------------------------
# clip.keyframes
# ---------------------------------------------------------------------------

_POINT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "frame": integer("Clip frame X exactly as stored: 1-based and counting trimmed-off source frames "
                         "(the first visible frame is round(start*fps)+1).", minimum=1),
        "seconds": number("Seconds from the clip's first visible frame: 0 = where the clip starts on the "
                          "timeline, the clip's duration = its end. Works the same on trimmed clips."),
        "timeline_seconds": number("Timeline time in seconds (must fall inside the clip)."),
        "source_seconds": number("Time in the source media in seconds (inside the clip's trimmed range)."),
        "value": nullable({"type": ["number", "string", "boolean"], "description": (
            "Value at that time: a number (alpha 0-1, scale 1 = 100%, location -1..1 = one frame, "
            "rotation in degrees, volume 0-1.3, time = source frame), a choice name for choice properties "
            "(has_audio: Auto/Off/On), or a color for wave_color. Omit to keep the value the curve already "
            "has there (pins it).")}),
        "interpolation": enum(["bezier", "linear", "constant"],
                              "How the value moves INTO this point from the previous one: bezier (smooth), "
                              "linear, or constant (holds the previous value, then jumps)."),
        "ease": enum(list(cpm.EASE_NAMES), "Bezier ease preset (the Properties dock's list) for the move INTO "
                     "this point; implies bezier."),
    },
}


@editor_tool(
    "set_keyframes_tool",
    label="Animate clip property",
    schema=obj({
        **CLIP_TARGET,
        "occurrence": integer("With clip_query: take the Nth matching clip in timeline order (0 = best match).",
                              0, minimum=0),
        "position_near": nullable(number("With clip_query: prefer the clip under this timeline time (seconds).")),
        "property": string("The clip property to animate: 'scale_x', 'scale_y', 'location_x', 'location_y', "
                           "'rotation', 'alpha', 'volume', 'shear_x', 'shear_y', 'origin_x', 'origin_y', "
                           "'corner_radius', 'margin', 'wave_color', 'has_audio', 'has_video', 'channel_filter', "
                           "'channel_mapping' or 'time' (or its label, e.g. 'Scale X')."),
        "points": array(_POINT, "Keyframes: each gives one time (frame, seconds, timeline_seconds or "
                        "source_seconds) and a value. Example: [{\"seconds\": 0, \"value\": 1}, "
                        "{\"seconds\": 3, \"value\": 1.3, \"ease\": \"ease_in_out\"}]."),
        "mode": enum(["replace", "merge", "replace_range"],
                     "replace = these points become the whole curve; merge = add/update these points and keep the "
                     "others; replace_range = drop existing points between the first and last new point, keep "
                     "the rest.", "replace"),
        "interpolation": enum(["", "bezier", "linear", "constant"],
                              "Default interpolation for points that set none ('' = bezier, constant for choice "
                              "properties).", ""),
        "ease": enum([""] + list(cpm.EASE_NAMES),
                     "Default bezier ease for points that set none (e.g. 'ease_in_out').", ""),
    }, required=["property", "points"]),
    covers=("clip.keyframes",),
)
def set_keyframes(property, points, timeline_clip_id="", clip_query="", track="", occurrence=0, position_near=None,
                  mode="replace", interpolation="", ease=""):
    """Animate one property of one timeline clip with timed keyframes.

    Use it for custom motion the presets do not cover: "zoom from 100% to 130%
    between 2 s and 5 s", "move the logo from left to right", "fade the music
    down to 20% at 10 s", "spin it once", or any exact curve. Times are per
    point: 'seconds' counts from the clip's first visible frame (so 0 is the
    clip's start even when it is trimmed), 'timeline_seconds' is timeline
    time, 'source_seconds' is time in the media, 'frame' is the raw keyframe
    X. Every point must land inside the clip. 'value' is in the property's
    units (scale 1 = 100%, location -1..1 = one frame, rotation degrees, alpha
    0-1, volume 0-1.3); omit it to pin the current value at that time. Shape
    each move with interpolation (bezier, linear, constant) or one of the 28
    bezier ease presets. mode='replace' (default) makes these points the whole
    curve; 'merge' keeps the other points; 'replace_range' clears only the span
    the new points cover. For whole-clip zooms, pans, fades and slides prefer
    apply_clip_preset_tool; to remove keyframes use remove_keyframes_tool.
    One undo step.

    Example: set_keyframes_tool(timeline_clip_id="ABC123", property="scale_x",
    points=[{"seconds": 0, "value": 1}, {"seconds": 4, "value": 1.3, "ease": "ease_in_out"}])
    """
    clip = resolve_clip(timeline_clip_id, clip_query, track, occurrence, position_near)
    data = clip.data
    layer = int(data.get("layer") or 0)
    if is_locked(layer):
        raise ToolError(f"clip {clip.id} is on locked track {ui_track_number(layer) or layer}; unlock it first")
    key = cpm.resolve_key(property)
    info = cpm.property_info(key, data)
    if not info.keyframable:
        raise ToolError(f"{key} holds a single value and cannot be animated; set it with set_clip_properties_tool")
    if not isinstance(points, list) or not points:
        raise ToolError("points must list at least one keyframe, e.g. [{\"seconds\": 0, \"value\": 1}]")
    if ease and interpolation not in ("", "bezier"):
        raise ToolError("ease presets are bezier curves; drop interpolation or set it to 'bezier'")
    ct = cpm.ClipTime.of(data)
    built = _build_points(points, info, data, ct, interpolation, ease)

    old_curve = data.get(key)
    if info.kind == "color":
        base = old_curve if isinstance(old_curve, dict) else {}
        new_curve = {ch: _merge_curve(base.get(ch), [(x, v[i], it, e) for x, v, it, e in built], mode)
                     for i, ch in enumerate(cpm.COLOR_CHANNELS)}
    else:
        new_curve = _merge_curve(old_curve, built, mode)
    values = {key: new_curve}
    if key == "time":
        from classes.clip_utils import clamp_timing_to_media
        trial = copy.deepcopy(data)
        trial["time"] = new_curve
        clamp_timing_to_media(trial, clip)
        values = {k: trial.get(k) for k in ("time", "start", "end", "duration")}
    receipt_points = (cpm.describe_points(new_curve, ct) if info.kind != "color"
                      else cpm.describe_points(new_curve.get("red"), ct))
    if all(data.get(k) == v for k, v in values.items()):
        return ok(f"Nothing to change: {key} on clip {clip.id} already has these keyframes.", changed=False,
                  timeline_clip_id=clip.id, property=key, points=receipt_points)
    before_count = (len(cpm.curve_points((old_curve or {}).get("red"))) if info.kind == "color"
                    else len(cpm.curve_points(old_curve)))
    with _transaction() as tid:
        save_clip_values(clip.id, values)
        if key == "volume":
            refresh_waveforms([dict(data, id=clip.id)], tid)
    refresh_preview()
    span = f"{receipt_points[0]['clip_seconds']:.2f}-{receipt_points[-1]['clip_seconds']:.2f} s into the clip"
    return ok(f"Set {len(built)} keyframe(s) on {key} for clip {clip.id} ({mode}; curve now has "
              f"{len(receipt_points)} point(s), {span}).", changed=True, timeline_clip_id=clip.id, property=key,
              mode=mode, previous_points=before_count, points=receipt_points,
              **({"start": values.get("start"), "end": values.get("end")} if key == "time" else {}))


def _build_points(points, info, data, ct, default_interp, default_ease):
    """[(frame, value, interpolation code, ease preset or None)] sorted by frame; validates everything."""
    presets = cpm.ease_presets()
    built, seen = [], {}
    for i, raw in enumerate(points):
        if not isinstance(raw, dict):
            raise ToolError(f"points[{i}] must be an object like {{\"seconds\": 1, \"value\": 0.5}}")
        given = [k for k in ("frame", "seconds", "timeline_seconds", "source_seconds") if raw.get(k) is not None]
        if len(given) != 1:
            raise ToolError(f"points[{i}] needs exactly one of frame, seconds, timeline_seconds or source_seconds")
        unit, amount = given[0], float(raw[given[0]])
        if unit == "frame":
            if amount != int(amount):
                raise ToolError(f"points[{i}].frame must be a whole number")
            x = int(amount)
        elif unit == "seconds":
            x = ct.from_clip_seconds(amount)
        elif unit == "timeline_seconds":
            x = ct.from_timeline_seconds(amount)
        else:
            x = ct.from_source_seconds(amount)
        if not ct.contains(x):
            raise ToolError(f"points[{i}] ({unit}={raw[unit]}) lands on frame {x}, outside clip {data.get('id')}; "
                            f"valid: {ct.ranges_text()}")
        if x in seen:
            raise ToolError(f"points[{seen[x]}] and points[{i}] land on the same frame {x}")
        seen[x] = i
        if raw.get("value") is None:
            value = cpm.value_at(info, data, x)
        else:
            value = cpm.coerce_value(info, raw["value"], str(data.get("id") or ""))
        if info.key == "time":
            value = int(value)
        point_ease, point_interp = raw.get("ease") or "", raw.get("interpolation") or ""
        if point_ease and point_interp and point_interp != "bezier":
            raise ToolError(f"points[{i}]: ease presets are bezier curves; drop interpolation or use 'bezier'")
        if point_ease:
            interp_name, ease_name = "bezier", point_ease
        else:
            interp_name = point_interp or default_interp or ("constant" if info.is_choice else "bezier")
            ease_name = default_ease if interp_name == "bezier" else ""
        if ease_name and ease_name not in presets:
            raise ToolError(f"points[{i}]: unknown ease {ease_name!r}; choose one of {', '.join(presets)}")
        built.append((x, value, interpolation_code(interp_name), presets.get(ease_name) if ease_name else None))
    built.sort(key=lambda b: b[0])
    return built


def _merge_curve(curve, built, mode):
    existing = copy.deepcopy(cpm.curve_points(curve))
    new_xs = {float(x) for x, *_ in built}
    if mode == "replace":
        base = []
    elif mode == "replace_range":
        lo, hi = min(new_xs), max(new_xs)
        base = [p for p in existing if not lo <= float(p["co"]["X"]) <= hi]
    else:
        base = [p for p in existing if float(p["co"]["X"]) not in new_xs]
    fresh = {float(x): cpm.make_point(x, float(v), code) for x, v, code, _e in built}
    merged = sorted(base + list(fresh.values()), key=lambda p: float(p["co"]["X"]))
    for x, _v, _code, preset in built:
        if preset:
            index = next(i for i, p in enumerate(merged) if p is fresh[float(x)])
            cpm.apply_ease(merged, index, preset)
    return {"Points": merged}


@editor_tool(
    "remove_keyframes_tool",
    label="Remove keyframes",
    schema=obj({
        **TARGETS,
        "properties": array({"type": "string"}, "Properties whose keyframes to remove (keys or labels), or "
                            "[\"all\"] for every animated property."),
        "start_seconds": nullable(number("Only remove keyframes at or after this timeline time (seconds).")),
        "end_seconds": nullable(number("Only remove keyframes at or before this timeline time (seconds).")),
    }, required=["properties"]),
    covers=("clip.keyframes",),
)
def remove_keyframes(properties, timeline_clip_ids=None, clip_query="", track="", scope="", start_seconds=None,
                     end_seconds=None):
    """Remove keyframes (animation points) from clip properties; never deletes clips.

    Use it for "remove the zoom", "stop it moving", "delete the keyframe at
    3 s", "clear all animation on these clips". Give the properties (or
    ["all"] = every animated property) and optionally a timeline time range;
    without a range every keyframe of those properties goes. When a property
    loses its last keyframe it falls back to the Properties dock default
    (alpha 1, scale 1, location 0, rotation 0, shear 0, origin 0.5, volume 1),
    exactly like Remove Keyframe in the Properties dock; to keep some other
    fixed value use set_clip_properties_tool instead. To remove fades use
    apply_clip_preset_tool(preset='fade_none'). One undo step.

    Example: remove_keyframes_tool(timeline_clip_ids=["ABC123"], properties=["scale_x", "scale_y"])
    """
    names = [str(p).strip() for p in (properties or []) if str(p).strip()]
    if not names:
        raise ToolError('properties must list at least one property, or ["all"]')
    want_all = any(n.lower() == "all" for n in names)
    keys = [] if want_all else list(dict.fromkeys(cpm.resolve_key(n) for n in names))
    for key in keys:
        if key == "time":
            raise ToolError("removing time keyframes changes the clip's speed; use the speed tools to reset it")
    clips, skipped = resolve_targets(timeline_clip_ids, clip_query, track, scope)
    plans = []
    for clip in clips:
        data = clip.data
        ct = cpm.ClipTime.of(data)
        lo = ct.from_timeline_seconds(start_seconds) if start_seconds is not None else None
        hi = ct.from_timeline_seconds(end_seconds) if end_seconds is not None else None
        clip_keys = keys
        if want_all:
            clip_keys = [k for k in cpm.settable_keys() if k != "time"
                         and cpm.property_info(k, data).keyframable
                         and cpm.animated(cpm.property_info(k, data), data)]
        values, report = {}, {}
        for key in clip_keys:
            info = cpm.property_info(key, data)
            if not info.keyframable:
                raise ToolError(f"{key} holds a single value and has no keyframes; set it with set_clip_properties_tool")
            if info.kind == "color":
                curves = copy.deepcopy(data.get(key) or {})
                removed = 0
                for ch in cpm.COLOR_CHANNELS:
                    curves[ch], n, _reset = _without_points(curves.get(ch), lo, hi, key, ch)
                    removed = max(removed, n)
                if removed:
                    values[key] = curves
                    report[key] = {"removed": removed}
                continue
            curve, removed, reset = _without_points(data.get(key), lo, hi, key, None)
            if removed:
                values[key] = curve
                report[key] = {"removed": removed, "remaining": len(curve["Points"])}
                if reset is not None:
                    report[key]["reset_to"] = reset
        if values:
            plans.append((clip, values, report))
    if not plans:
        where = "" if start_seconds is None and end_seconds is None else " in that time range"
        raise ToolError(f"no keyframes to remove{where} on {len(clips)} clip(s) for "
                        f"{'any animated property' if want_all else ', '.join(keys)}")
    with _transaction() as tid:
        for clip, values, _report in plans:
            save_clip_values(clip.id, values)
        refresh_waveforms([dict(c.data, id=c.id) for c, v, _ in plans if "volume" in v], tid)
    refresh_preview()
    total = sum(r["removed"] for _c, _v, rep in plans for r in rep.values())
    return ok(f"Removed {total} keyframe(s) from {len(plans)} clip(s).",
              clips=[{"timeline_clip_id": c.id, "properties": rep} for c, _v, rep in plans], skipped=skipped)


def _without_points(curve, lo, hi, key, channel):
    """(curve, removed count, reset value or None) with points in [lo, hi] removed."""
    points = copy.deepcopy(cpm.curve_points(curve))
    keep = [p for p in points
            if not ((lo is None or float(p["co"]["X"]) >= lo) and (hi is None or float(p["co"]["X"]) <= hi))]
    removed = len(points) - len(keep)
    if removed and len(points) == 1 and lo is None and hi is None:
        # A static value is not animation; resetting it is set_clip_properties' job.
        default = default_keyframe_value(key, channel)
        if default is not None and float(points[0]["co"]["Y"]) == float(default):
            return curve, 0, None
    reset = None
    if removed and not keep:
        reset = default_keyframe_value(key, channel)
        if reset is None:
            reset = float(points[0]["co"]["Y"])
        keep = [cpm.make_point(1, reset, BEZIER)]
    return {"Points": keep}, removed, reset


# ---------------------------------------------------------------------------
# clip.copy_attributes
# ---------------------------------------------------------------------------

@editor_tool(
    "copy_clip_keyframes_tool",
    label="Copy keyframes between clips",
    schema=obj({
        "source_clip_id": string("Timeline clip id to copy FROM (preferred).", ""),
        "source_query": string("Describe the source clip instead of an id.", ""),
        "source_track": string("Narrow source_query to a track (UI track number, name or layer).", ""),
        **TARGETS,
        "groups": array(enum(list(KEYFRAME_GROUPS), "A Copy > Keyframes group."),
                        "What to copy, as in the Clip menu's Copy > Keyframes: all, alpha, scale (+gravity), shear, "
                        "rotation (+gravity), location (+gravity), time, volume.", ["all"]),
        "timing": enum(["stretch", "offset", "exact"],
                       "How keyframe times map onto each target: stretch = scaled to the target's length (a "
                       "whole-clip zoom stays whole-clip, a fade-out stays at the end); offset = same seconds from "
                       "each clip's start; exact = the same clip frames, like the editor's Copy/Paste.", "stretch"),
    }),
    covers=("clip.copy_attributes",),
)
def copy_clip_keyframes(source_clip_id="", source_query="", source_track="", timeline_clip_ids=None,
                        clip_query="", track="", scope="", groups=["all"], timing="stretch"):
    """Copy the animation (keyframes) of one clip onto other clips.

    Use it for "give clip B the same zoom as clip A", "copy the fade to all
    clips", "same position and size as that one". The groups match the Clip
    menu's Copy > Keyframes items: all, alpha (opacity), scale, shear, rotation,
    location, time (speed curve) and volume; scale, rotation and location also
    copy gravity. With timing='stretch' (default) the keyframes are scaled to
    each target's length, so a whole-clip Ken Burns or a fade-out lands the
    same way on a shorter or longer clip; 'offset' keeps the seconds from each
    clip's start; 'exact' copies the raw frames like the editor's paste. The
    'time' curve is only copied with timing='exact' (and skipped from 'all'
    otherwise), since stretching a speed curve changes what the target shows.
    Targets exclude the source and must not be on locked tracks. One undo step.

    Example: copy_clip_keyframes_tool(source_clip_id="A1", timeline_clip_ids=["B2", "C3"], groups=["scale", "location"])
    """
    source = resolve_clip(source_clip_id, source_query, source_track)
    wanted = list(dict.fromkeys(str(g).strip().lower() for g in (groups or ["all"]) if str(g).strip()))
    unknown = [g for g in wanted if g not in COPY_KEYFRAME_GROUPS]
    if unknown:
        raise ToolError(f"unknown keyframe group(s) {unknown}; choose from {list(KEYFRAME_GROUPS)}")
    if "time" in wanted and timing != "exact":
        raise ToolError("the time (speed) curve can only be copied with timing='exact'")
    keys = list(dict.fromkeys(k for g in wanted for k in COPY_KEYFRAME_GROUPS[g]))
    skipped_keys = []
    if "time" in keys and timing != "exact":
        keys.remove("time")
        skipped_keys.append("time")
    ids = [str(i).strip() for i in (timeline_clip_ids or []) if str(i).strip() and str(i).strip() != source.id]
    if timeline_clip_ids and not ids:
        raise ToolError("the only target given is the source clip itself; name other clips to copy onto")
    if not ids and not clip_query and not scope:
        raise ToolError("name the target clips (timeline_clip_ids, clip_query or scope)")
    clips, skipped = resolve_targets(ids or None, clip_query, track, scope)
    clips = [c for c in clips if c.id != source.id]
    if not clips:
        raise ToolError("no target clips other than the source")
    src = source.data
    src_time = cpm.ClipTime.of(src)
    plans = []
    for clip in clips:
        data = clip.data
        dst_time = cpm.ClipTime.of(data)
        values = {}
        for key in keys:
            if key not in src:
                continue
            value = src[key]
            if isinstance(value, dict) and isinstance(value.get("Points"), list):
                value = {"Points": _retime_points(value["Points"], src_time, dst_time, timing)}
            else:
                value = copy.deepcopy(value)
            if data.get(key) != value:
                values[key] = value
        if "time" in values:
            from classes.clip_utils import clamp_timing_to_media
            trial = copy.deepcopy(data)
            trial.update(values)
            clamp_timing_to_media(trial, clip)
            values.update({k: trial.get(k) for k in ("time", "start", "end", "duration")})
        if values:
            plans.append((clip, values))
    note = f" ({', '.join(skipped_keys)} not copied: only with timing='exact')" if skipped_keys else ""
    if not plans:
        return ok(f"Nothing to change: the targets already have clip {source.id}'s {', '.join(wanted)} keyframes"
                  f"{note}.", changed=False, source_clip_id=source.id, timeline_clip_ids=[c.id for c in clips],
                  skipped=skipped)
    with _transaction() as tid:
        for clip, values in plans:
            save_clip_values(clip.id, values)
        refresh_waveforms([dict(c.data, id=c.id) for c, v in plans if "volume" in v], tid)
    refresh_preview()
    return ok(f"Copied {', '.join(wanted)} keyframes from clip {source.id} to {len(plans)} clip(s) "
              f"(timing={timing}){note}.", changed=True, source_clip_id=source.id, timing=timing,
              clips=[{"timeline_clip_id": c.id, "properties": sorted(v)} for c, v in plans], skipped=skipped,
              not_copied=skipped_keys)


def _retime_points(points, src, dst, timing):
    """Map keyframe X from the source clip's frames onto the target's.

    Only the source's visible part is mapped: points outside it (e.g. the
    default point at X=1 of a trimmed clip) are replaced by the curve's exact
    value at the visible edges, so they cannot land on the target's first
    frame and override the start of a fade.
    """
    pts = copy.deepcopy(sorted((p for p in points if isinstance(p, dict) and isinstance(p.get("co"), dict)),
                               key=lambda p: float(p["co"]["X"])))
    if timing == "exact" or len(pts) <= 1:
        return pts
    s0, e0 = src.first_frame, src.end_frame
    s1, e1 = dst.first_frame, dst.end_frame
    inside = [p for p in pts if s0 <= float(p["co"]["X"]) <= e0]
    curve = {"Points": pts}
    xs = {float(p["co"]["X"]) for p in inside}
    if s0 not in xs and float(pts[0]["co"]["X"]) < s0:
        inside.insert(0, cpm.make_point(s0, keyframe_value(curve, s0), BEZIER))
    if e0 not in xs and float(pts[-1]["co"]["X"]) > e0:
        edge = cpm.make_point(e0, keyframe_value(curve, e0), int(pts[-1].get("interpolation", BEZIER)))
        inside.append(edge)
    scale = (e1 - s1) / float(e0 - s0) if timing == "stretch" and e0 > s0 else 1.0
    out = []
    for p in inside:
        x = max(1, int(round(s1 + (float(p["co"]["X"]) - s0) * scale)))
        p["co"]["X"] = float(x)
        if out and float(out[-1]["co"]["X"]) == x:
            if x == s1:
                continue  # the first point at the target's start is the start of the move
            out[-1] = p
            continue
        out.append(p)
    return out


# The menu presets register from their own module.
from classes.editor_tools import clip_props_presets  # noqa: E402,F401
