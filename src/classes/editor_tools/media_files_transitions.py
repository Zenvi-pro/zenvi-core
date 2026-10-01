"""Transitions on the timeline: list, change, reverse, remove, copy, and crossfades between clips.

Workstream: media-files. The rules are the timeline's own, in
``classes.transition_ops`` (shared with ``TimelineView``): how a Mask transition
is built, how its curves follow a new length, how Reverse mirrors them. Edits
are saved like the Properties dock saves them (``Transition.save``), so each
tool call is one undo step. Mask images are read with the timeline's cached
reader (a Qt image reader: GUI thread, which is where these tools run).

A Mask transition sits on a track over the overlap of two clips (a crossfade or
wipe from the earlier clip to the later one) or over one clip's edge (a fade in
or out). It only shows where clips overlap, so changing a crossfade's length
also moves the later clip (and rippled items after it on that track).
"""

from __future__ import annotations

import copy
import os
import random

from classes import transition_ops
from classes.editor_tools._base import (
    ToolError,
    array,
    boolean,
    enum,
    get_app,
    interpolation_code,
    is_locked,
    keyframe,
    mapping,
    nullable,
    number,
    obj,
    ok,
    project_fps,
    refresh_preview,
    resolve_layer,
    snap_seconds,
    string,
    timeline_ui,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.logger import log

EPS = 1e-4

TRANSITION_TARGET = {
    "transition_ids": array({"type": "string"}, "Transition ids (list_timeline_transitions_tool or "
                                                "get_timeline_state_tool 'Effects/Transitions'). Preferred."),
    "between_clip_ids": array({"type": "string"}, "Instead of ids: the two timeline clip ids the transition "
                                                  "joins, e.g. [clip_a, clip_b]."),
    "track": string("Track to look on (UI track number, 1 = bottom; name; or layer number). With scope='track' "
                    "every transition on it.", ""),
    "at_seconds": nullable(number("Instead of ids: the transition under this timeline time (on `track` when "
                                  "given).", minimum=0)),
    "scope": enum(["", "selected", "track", "all"],
                  "'selected' = selected transitions, 'track' = every transition on `track`, 'all' = every "
                  "transition on the timeline.", ""),
}

KEYFRAME_ITEM = mapping("One keyframe of a transition curve.", properties={
    "seconds": number("Time from the START of the transition, 0 = its first frame.", minimum=0),
    "value": number("Curve value (brightness -1..1: 1 = wipe not started, -1 = finished; contrast 0..20)."),
    "interpolation": enum(["bezier", "linear", "constant"], "Easing into the next point.", "bezier"),
}, required=["seconds", "value"], additionalProperties=False)


# ---------------------------------------------------------------------------
# Reading transitions
# ---------------------------------------------------------------------------

def _span(data):
    pos = float(data.get("position") or 0.0)
    return pos, pos + max(0.0, float(data.get("end") or 0.0) - float(data.get("start") or 0.0))


def _layer_of(data):
    try:
        return int(data.get("layer") or 0)
    except (TypeError, ValueError):
        return 0


def _clips_on(layer_num):
    from classes.query import Clip
    return sorted((c for c in Clip.filter() if _layer_of(c.data) == int(layer_num)),
                  key=lambda c: float(c.data.get("position") or 0.0))


def _transitions():
    from classes.query import Transition
    return [t for t in Transition.filter() if str(t.data.get("type") or "Mask") == "Mask"
            or "brightness" in t.data]


def _frame():
    return 1.0 / project_fps()


def mask_name(data) -> str:
    reader = transition_ops.mask_reader(data)
    path = str(reader.get("path") or data.get("resource") or "")
    return os.path.splitext(os.path.basename(path))[0] if path else ""


def _first_value(kf, default=None):
    pts = (kf or {}).get("Points") if isinstance(kf, dict) else None
    if not pts:
        return default
    try:
        return float(sorted(pts, key=lambda p: float(p["co"]["X"]))[0]["co"]["Y"])
    except (KeyError, TypeError, ValueError):
        return default


def joined_pair(data):
    """(clip_a, clip_b) when the transition covers the overlap of two clips on its track, else None."""
    t0, t1 = _span(data)
    over = [c for c in _clips_on(_layer_of(data))
            if _span(c.data)[0] < t1 - EPS and _span(c.data)[1] > t0 + EPS]
    best = None
    for i, a in enumerate(over):
        for b in over[i + 1:]:
            a0, a1 = _span(a.data)
            b0, b1 = _span(b.data)
            if b0 < a0:
                a, b, a0, a1, b0, b1 = b, a, b0, b1, a0, a1
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo <= EPS or b0 <= a0 + EPS:
                continue
            cover = min(hi, t1) - max(lo, t0)
            if best is None or cover > best[0]:
                best = (cover, a, b)
    return (best[1], best[2]) if best else None


def _edge(data):
    """('start'|'end', clip) when the transition sits on one clip's first or last part."""
    t0, t1 = _span(data)
    tol = _frame() * 1.5
    for c in _clips_on(_layer_of(data)):
        c0, c1 = _span(c.data)
        if abs(c0 - t0) <= tol and t1 <= c1 + tol:
            return "start", c
        if abs(c1 - t1) <= tol and t0 >= c0 - tol:
            return "end", c
    return None, None


def describe_transition(t) -> dict:
    data = t.data if isinstance(t.data, dict) else {}
    t0, t1 = _span(data)
    layer = _layer_of(data)
    out = {
        "transition_id": str(t.id),
        "mask": mask_name(data),
        "title": str(data.get("title") or ""),
        "track": ui_track_number(layer),
        "layer": layer,
        "position": round(t0, 3),
        "end": round(t1, 3),
        "duration": round(t1 - t0, 3),
        "direction": transition_ops.direction_of(data) or "custom",
        "contrast": _first_value(data.get("contrast")),
        "audio_crossfade": bool(data.get("fade_audio_hint")),
        "invert_mask": bool(data.get("mask_invert")),
    }
    if data.get("replace_image"):
        out["replace_image"] = True
    pair = joined_pair(data)
    if pair:
        out["between_clip_ids"] = [pair[0].id, pair[1].id]
        # Reversing OR inverting alone makes the later clip show first and the earlier one
        # flash back before the cut; both together wipe from the other side, in order.
        forward = (out["direction"] == "default") != out["invert_mask"]
        out["order"] = "earlier_to_later" if forward else "later_first"
        a1 = _span(pair[0].data)[1]
        b0 = _span(pair[1].data)[0]
        out["clip_overlap"] = round(max(0.0, a1 - b0), 3)
    else:
        side, clip = _edge(data)
        if clip is not None:
            out["on_clip_" + side] = clip.id
    return out


def resolve_transitions(transition_ids=None, between_clip_ids=None, track="", at_seconds=None, scope=""):
    """Transitions by id, clip pair, time, selection, track or all. Raises ToolError when none match."""
    from classes.query import Clip, Transition

    scope = (scope or "").strip().lower()
    if transition_ids:
        out = []
        for tid in transition_ids:
            t = Transition.get(id=str(tid).strip())
            if not t:
                raise ToolError(f"no transition with id={tid!r} (list_timeline_transitions_tool lists them)")
            out.append(t)
        return out
    if between_clip_ids:
        ids = [str(i).strip() for i in between_clip_ids if str(i).strip()]
        if len(ids) != 2:
            raise ToolError("between_clip_ids takes exactly two timeline clip ids")
        clips = [Clip.get(id=i) for i in ids]
        for cid, c in zip(ids, clips):
            if not c:
                raise ToolError(f"no timeline clip with id={cid!r}")
        a, b = sorted(clips, key=lambda c: float(c.data.get("position") or 0.0))
        found = []
        for t in _transitions():
            pair = joined_pair(t.data)
            if pair and {pair[0].id, pair[1].id} == {a.id, b.id}:
                found.append(t)
        if not found:
            raise ToolError("no transition joins those two clips (add one with add_transitions_between_clips_tool)")
        return found
    layer = resolve_layer(track) if str(track or "").strip() else None
    if scope == "selected":
        try:
            ids = list(get_app().window.selected_transitions or [])
        except Exception:
            ids = []
        if not ids:
            raise ToolError("no transitions are selected in the timeline")
        return resolve_transitions(transition_ids=ids)
    candidates = [t for t in _transitions() if layer is None or _layer_of(t.data) == layer]
    candidates.sort(key=lambda t: (_span(t.data)[0], _layer_of(t.data)))
    if scope in ("track", "all"):
        if scope == "track" and layer is None:
            raise ToolError("scope='track' needs track")
        if not candidates:
            raise ToolError("the timeline has no transitions" if scope == "all" else f"track {track} has no transitions")
        return candidates
    if at_seconds is not None:
        t = float(at_seconds)
        half = _frame() / 2
        under = [x for x in candidates if _span(x.data)[0] - half <= t <= _span(x.data)[1] + half]
        if not under:
            near = sorted(candidates, key=lambda x: min(abs(_span(x.data)[0] - t), abs(_span(x.data)[1] - t)))
            under = [x for x in near[:1] if min(abs(_span(x.data)[0] - t), abs(_span(x.data)[1] - t)) <= 0.5]
        if not under:
            raise ToolError(f"no transition at {t:.2f}s" + (f" on track {track}" if layer is not None else ""))
        if len(under) > 1:
            raise ToolError(f"{len(under)} transitions at {t:.2f}s on different tracks "
                            f"({', '.join(x.id for x in under)}); pass track or transition_ids")
        return under
    if len(candidates) == 1:
        return candidates
    if not candidates:
        raise ToolError("the timeline has no transitions" + (f" on track {track}" if layer is not None else ""))
    raise ToolError(f"{len(candidates)} transitions match; say which with transition_ids, between_clip_ids, "
                    "at_seconds or scope")


def _ensure_unlocked(items):
    locked = sorted({ui_track_number(_layer_of(i.data)) or _layer_of(i.data)
                     for i in items if is_locked(_layer_of(i.data))})
    if locked:
        raise ToolError(f"track(s) {locked} are locked; unlock them first")


def _mask_reader_json(path):
    """Reader JSON for a mask image/video (the timeline's cache; GUI thread)."""
    reader = None
    try:
        reader = timeline_ui()._get_transition_reader_json(path)
    except ToolError:
        raise
    except Exception:
        log.debug("timeline reader cache unavailable for %s", path, exc_info=True)
    if not isinstance(reader, dict):
        raise ToolError(f"cannot read the transition image {path!r}")
    return reader


def _pick_mask(name):
    entry, candidates = transition_ops.find_transition(name)
    if entry is None:
        hint = f"; did you mean {', '.join(candidates)}" if candidates else ""
        raise ToolError(f"unknown transition {name!r}{hint} (list_transitions_tool / search_transitions_tool)")
    return entry


# Neutral values for keys an edit may add (see media_files.seed_missing_keys: keeps undo exact).
TRANSITION_KEY_DEFAULTS = {"mask_invert": False, "fade_audio_hint": False, "replace_image": False,
                           "title": "Transition", "mask_loop_mode": 0, "mask_time_mode": 1}


def _save(t, data):
    from classes.editor_tools.media_files import seed_missing_keys
    seed_missing_keys(t, {k: v for k, v in TRANSITION_KEY_DEFAULTS.items() if k in data})
    removed = [k for k in t.data if k not in data]
    t.data = data
    t.save()
    for key in removed:
        get_app().updates.delete(list(t.key) + [key])


def _curve_from_points(points, duration):
    fps = project_fps()
    out = []
    for p in points:
        sec = float(p["seconds"])
        if sec > duration + 1e-6:
            raise ToolError(f"keyframe at {sec:.3f}s is past the end of the {duration:.3f}s transition")
        out.append((int(round(sec * fps)) + 1, float(p["value"]),
                    interpolation_code(p.get("interpolation") or "bezier")))
    if not out:
        raise ToolError("a keyframe list needs at least one point")
    return keyframe(out)


# ---------------------------------------------------------------------------
# list_timeline_transitions_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "list_timeline_transitions_tool",
    label="List timeline transitions",
    schema=obj({
        "track": string("Only this track (UI track number, 1 = bottom; name; or layer number). '' = all.", ""),
        "timeline_clip_id": string("Only transitions touching this timeline clip.", ""),
        "at_seconds": nullable(number("Only the transition(s) under this timeline time.", minimum=0)),
    }),
    read_only=True,
    covers=("transition.list",),
)
def list_timeline_transitions(track="", timeline_clip_id="", at_seconds=None):
    """List the transitions placed on the timeline, with what each one does.

    For each: id, mask shape (fade, wipe_left_to_right, circle_in_to_out...), track, start,
    end and duration (timeline seconds), direction of the curve ('default' / 'reversed'),
    edge softness (contrast), audio crossfade, and either the two clips it joins
    (between_clip_ids, their overlap, and order: 'earlier_to_later' as expected, or
    'later_first' = the later clip shows first and flashes back) or the clip edge it fades
    (on_clip_start = fade in, on_clip_end = fade out).
    Use before changing, reversing, copying or removing a transition. The catalog of
    transitions you can add is list_transitions_tool / search_transitions_tool. Read-only.
    """
    layer = resolve_layer(track) if str(track or "").strip() else None
    items = [t for t in _transitions() if layer is None or _layer_of(t.data) == layer]
    if at_seconds is not None:
        half = _frame() / 2
        items = [t for t in items if _span(t.data)[0] - half <= float(at_seconds) <= _span(t.data)[1] + half]
    rows = [describe_transition(t) for t in sorted(items, key=lambda t: (_span(t.data)[0], _layer_of(t.data)))]
    if timeline_clip_id:
        cid = str(timeline_clip_id).strip()
        rows = [r for r in rows if cid in (r.get("between_clip_ids") or [])
                or cid in (r.get("on_clip_start"), r.get("on_clip_end"))]
    where = f" on track {track}" if layer is not None else ""
    return ok(f"{len(rows)} transition(s){where}.", transitions=rows)


# ---------------------------------------------------------------------------
# update_transition_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "update_transition_tool",
    label="Change transition",
    schema=obj({
        **TRANSITION_TARGET,
        "duration_seconds": number("New length in seconds (0 = keep). On a crossfade between two clips the later "
                                   "clip moves so the clips overlap by exactly this much (see keep_overlap).",
                                   0.0, minimum=0),
        "keep_overlap": boolean("With duration_seconds on a crossfade: do not move any clip (the transition may "
                                "then run past the overlap and dip to the track below).", False),
        "ripple": boolean("With duration_seconds on a crossfade: move everything after the later clip on that "
                          "track too, so no gap or overlap opens further down. false = move only the later clip.",
                          True),
        "position_seconds": nullable(number("Move the transition to start at this timeline time (clips stay).",
                                            minimum=0)),
        "mask": string("Change the wipe shape: a transition name ('fade', 'wipe_left_to_right', 'circle_in_to_out', "
                       "any list_transitions_tool name) or an image file path. '' = keep.", ""),
        "mask_file_id": string("Use this project image file (e.g. a gradient or logo) as the wipe shape. '' = keep.",
                               ""),
        "flip_wipe_side": boolean("Wipe from the other side (left-to-right becomes right-to-left, circle-out "
                                  "becomes circle-in) and still go from the earlier clip to the later one. Works for "
                                  "any mask (inverts it and reverses its curve).", False),
        "invert_mask": nullable(boolean("Raw 'mask invert' property. Alone it makes the later clip show first "
                                        "(it flashes back before the cut); use flip_wipe_side instead.")),
        "contrast": nullable(number("Edge hardness of the wipe: 0 = very soft/blurry edge, 3 = the default soft "
                                    "edge, 20 = a hard line.", minimum=0, maximum=20)),
        "brightness_keyframes": array(KEYFRAME_ITEM, "Replace the progress curve (brightness): 1 = not started, "
                                                     "-1 = finished; times from the transition start."),
        "contrast_keyframes": array(KEYFRAME_ITEM, "Replace the edge-hardness curve over time."),
        "interpolation": enum(["", "bezier", "linear", "constant"],
                              "Easing of the progress curve: bezier = ease in/out (default), linear = constant "
                              "speed, constant = jump. '' = keep.", ""),
        "audio_crossfade": nullable(boolean("Also crossfade the two clips' audio under the transition "
                                            "(equal-power).")),
        "replace_image": nullable(boolean("Show the mask image itself instead of transitioning (a preview aid).")),
    }),
    covers=("transition.update",),
)
def update_transition(transition_ids=None, between_clip_ids=None, track="", at_seconds=None, scope="",
                      duration_seconds=0.0, keep_overlap=False, ripple=True, position_seconds=None, mask="",
                      mask_file_id="", flip_wipe_side=False, invert_mask=None, contrast=None, brightness_keyframes=None,
                      contrast_keyframes=None, interpolation="", audio_crossfade=None, replace_image=None):
    """Change a transition on the timeline: length, position, wipe shape, softness, curves, audio crossfade.

    "Make the crossfade 2 seconds" -> duration_seconds=2: on a transition between two clips
    the later clip (and, with ripple=true, everything after it on that track) moves so the
    clips overlap by 2 s and the transition covers the overlap; other tracks never move. On
    a fade at a clip's start/end the transition keeps that edge. The progress curve is
    rescaled to the new length (with the default soft edge the visible dissolve is shorter
    than the transition; a lower contrast makes it more gradual). "Use a circle wipe
    instead" -> mask="circle_in_to_out"; "make the wipe go the other way" ->
    flip_wipe_side=true (or the mirrored mask, e.g. wipe_right_to_left); "harder edge" ->
    contrast=20. Changes every chosen transition in one undo step. Refused on locked
    tracks. Never deletes clips.
    Example: between_clip_ids=["A","B"], duration_seconds=2.
    """
    from classes.query import File

    targets = resolve_transitions(transition_ids, between_clip_ids, track, at_seconds, scope)
    _ensure_unlocked(targets)
    new_dur = float(duration_seconds or 0.0)
    if new_dur:
        new_dur = snap_seconds(new_dur)
        if new_dur < _frame():
            raise ToolError("duration_seconds must be at least one frame")
    if flip_wipe_side and invert_mask is not None:
        raise ToolError("pass flip_wipe_side or invert_mask, not both")
    if not (new_dur or position_seconds is not None or mask or mask_file_id or flip_wipe_side
            or invert_mask is not None
            or contrast is not None or brightness_keyframes or contrast_keyframes or interpolation
            or audio_crossfade is not None or replace_image is not None):
        raise ToolError("nothing to change: pass duration_seconds, position_seconds, mask, flip_wipe_side, contrast, "
                        "keyframes, interpolation, audio_crossfade or replace_image")
    if mask and mask_file_id:
        raise ToolError("pass mask or mask_file_id, not both")
    if position_seconds is not None and len(targets) > 1:
        raise ToolError("position_seconds moves one transition; several were chosen")
    fps = project_fps()

    new_reader, new_title = None, None
    if mask:
        entry = _pick_mask(mask)
        new_reader, new_title = _mask_reader_json(entry["path"]), entry["key"]
    elif mask_file_id:
        f = File.get(id=str(mask_file_id).strip())
        if not f:
            raise ToolError(f"no project file with id={mask_file_id!r}")
        if str(f.data.get("media_type") or "") not in ("image", "video"):
            raise ToolError("the mask must be an image or video file")
        new_reader = copy.deepcopy(f.data)
        new_reader.pop("ai_metadata", None)
        new_title = os.path.splitext(os.path.basename(str(f.data.get("path") or "")))[0]

    # Plan every change on copies first; nothing is saved until all targets validate.
    # Ripples add up left to right, so positions are tracked live while planning.
    targets.sort(key=lambda x: (_layer_of(x.data), _span(x.data)[0]))
    live = {}

    def lpos(item):
        return live.get(item.id, float(item.data.get("position") or 0.0))

    def lspan(item):
        s0, s1 = _span(item.data)
        return lpos(item), lpos(item) + (s1 - s0)

    plans, notes = [], []
    for t in targets:
        old = copy.deepcopy(t.data)
        data = copy.deepcopy(t.data)
        t0, t1 = lspan(t)
        data["position"] = t0
        dur = t1 - t0
        if new_dur and abs(new_dur - dur) > EPS:
            pair = joined_pair(old)
            side, _edge_clip = _edge(old) if not pair else (None, None)
            if pair and not keep_overlap:
                a, b = pair
                a0, a1 = lspan(a)
                b0, b1 = lspan(b)
                a_len, b_len = a1 - a0, b1 - b0
                if new_dur >= min(a_len, b_len) - EPS:
                    raise ToolError(f"a {new_dur:.2f}s crossfade needs both clips to be longer; the shorter clip "
                                    f"is {min(a_len, b_len):.2f}s")
                delta = (a1 - new_dur) - b0        # move the later clip so the overlap == new_dur
                if b0 + delta < a0 + EPS:
                    raise ToolError("the later clip cannot start before the earlier one")
                layer_num = _layer_of(b.data)
                if ripple:
                    for item in _clips_on(layer_num) + [x for x in _transitions() if _layer_of(x.data) == layer_num]:
                        if item.id not in (t.id, a.id) and lpos(item) >= b0 - EPS:
                            live[item.id] = lpos(item) + delta
                else:
                    if delta > EPS:
                        blocked = [c for c in _clips_on(layer_num) if c.id not in (a.id, b.id)
                                   and b1 - EPS <= lspan(c)[0] < b1 + delta - EPS]
                        if blocked:
                            raise ToolError("moving only the later clip would overlap the next clip; use "
                                            "ripple=true")
                    live[b.id] = b0 + delta
                data["position"] = snap_seconds(a1 - new_dur)
                notes.append(f"moved clip {b.id}{' and later items' if ripple else ''} by {delta:+.2f}s")
            elif side == "end":
                data["position"] = max(0.0, snap_seconds(t1 - new_dur))
            data["start"] = 0.0 if transition_ops.uses_static_mask(data) else float(data.get("start") or 0.0)
            data["end"] = float(data["start"]) + new_dur
            transition_ops.rescale_curves(data, dur, new_dur, fps)
        if position_seconds is not None:
            data["position"] = snap_seconds(float(position_seconds))
        if new_reader is not None:
            was_static = transition_ops.uses_static_mask(old)
            data["reader"] = copy.deepcopy(new_reader)
            data.pop("mask_reader", None)
            data.pop("resource", None)
            if new_title:
                data["title"] = new_title
            if was_static != transition_ops.uses_static_mask(data):
                # Still <-> animated masks use different curves: start from that kind's defaults.
                direction = transition_ops.direction_of(old)
                transition_ops.set_mask_defaults(data, fps)
                if direction == "reversed":
                    transition_ops.reverse_transition_data(data)
        length = _span(data)[1] - _span(data)[0]
        if brightness_keyframes:
            data["brightness"] = _curve_from_points(brightness_keyframes, length)
        if contrast_keyframes:
            data["contrast"] = _curve_from_points(contrast_keyframes, length)
        if contrast is not None:
            data["contrast"] = keyframe([(1, float(contrast))])
        if interpolation:
            code = interpolation_code(interpolation)
            for p in (data.get("brightness") or {}).get("Points", []):
                p["interpolation"] = code
        if invert_mask is not None:
            data["mask_invert"] = bool(invert_mask)
        if flip_wipe_side:
            data["mask_invert"] = not bool(data.get("mask_invert"))
            transition_ops.reverse_transition_data(data)
        if audio_crossfade is not None:
            data["fade_audio_hint"] = bool(audio_crossfade)
        if replace_image is not None:
            data["replace_image"] = bool(replace_image)
        live[t.id] = float(data["position"])
        if data != old:
            plans.append((t, data))

    from classes.query import Clip, Transition
    moved_items = []
    for item_id, pos in live.items():
        item = Clip.get(id=item_id) or Transition.get(id=item_id)
        if item is not None and abs(float(item.data.get("position") or 0.0) - pos) > EPS:
            moved_items.append((item, pos))
    _ensure_unlocked([item for item, _ in moved_items])
    if any(pos < -EPS for _, pos in moved_items):
        raise ToolError("that would move a clip before the start of the timeline")
    if not plans and not moved_items:
        return ok("No change: the transition(s) already have those settings.", changed=False,
                  transitions=[describe_transition(t) for t in targets])

    planned_ids = {t.id for t, _ in plans}
    for item, pos in moved_items:
        if item.id in planned_ids:
            continue
        item.data["position"] = snap_seconds(max(0.0, pos))
        item.save()
    for t, data in plans:
        _save(t, data)
    refresh_preview()
    rows = [describe_transition(t) for t in targets]
    summary = f"Updated {len(plans)} transition(s)"
    if notes:
        summary += "; " + "; ".join(dict.fromkeys(notes))
    return ok(summary + ".", changed=True, transitions=rows,
              moved_ids=[item.id for item, _ in moved_items if item.id not in planned_ids])


# ---------------------------------------------------------------------------
# reverse_transition_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "reverse_transition_tool",
    label="Reverse transition",
    schema=obj(dict(TRANSITION_TARGET)),
    covers=("transition.reverse",),
)
def reverse_transition(transition_ids=None, between_clip_ids=None, track="", at_seconds=None, scope=""):
    """Reverse transitions (Transition menu > Reverse Transition): play the transition backwards.

    Mirrors the progress and edge curves in time. Right for a fade on ONE clip edge: a
    fade-in becomes a fade-out and back. On a crossfade/wipe BETWEEN two clips it makes the
    later clip show first and the earlier one flash back before the cut (the receipt's
    order becomes 'later_first'), which is rarely wanted: for "make the wipe go the other
    way" use update_transition_tool(flip_wipe_side=true). Several transitions in one undo
    step (scope='track' reverses a whole track). Refused on locked tracks; never deletes clips.
    """
    targets = resolve_transitions(transition_ids, between_clip_ids, track, at_seconds, scope)
    _ensure_unlocked(targets)
    changed = []
    for t in targets:
        before = copy.deepcopy(t.data)
        data = transition_ops.reverse_transition_data(copy.deepcopy(t.data))
        if data != before:
            _save(t, data)
            changed.append(t)
    if not changed:
        return ok("No change: those transitions have constant curves, so reversing does nothing.", changed=False,
                  transitions=[describe_transition(t) for t in targets])
    refresh_preview()
    return ok(f"Reversed {len(changed)} transition(s).", changed=True,
              transitions=[describe_transition(t) for t in changed])


# ---------------------------------------------------------------------------
# remove_transition_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "remove_transition_tool",
    label="Remove transition",
    schema=obj(dict(TRANSITION_TARGET)),
    covers=("transition.remove",),
)
def remove_transition(transition_ids=None, between_clip_ids=None, track="", at_seconds=None, scope=""):
    """Remove transitions from the timeline (Transition menu > Remove Transition). Never deletes clips.

    The clips stay exactly where they are: two clips that overlapped keep overlapping and
    now cut at the start of the later clip. "Remove all transitions on track 2" ->
    scope='track', track='2'. One undo step brings them back. Refused on locked tracks.
    To delete timeline clips use delete_from_timeline_tool.
    """
    targets = resolve_transitions(transition_ids, between_clip_ids, track, at_seconds, scope)
    _ensure_unlocked(targets)
    win = get_app().window
    removed = []
    for t in targets:
        removed.append(describe_transition(t))
        try:
            win.removeSelection(t.id, "transition")
            win.emit_selection_signal()
            win.show_property_timeout()
        except Exception:
            log.debug("could not clear the transition selection", exc_info=True)
        t.delete()
    refresh_preview()
    return ok(f"Removed {len(removed)} transition(s); the clips were not changed.",
              removed_transition_ids=[r["transition_id"] for r in removed], removed=removed)


# ---------------------------------------------------------------------------
# copy_transition_tool
# ---------------------------------------------------------------------------

_PASTE_EXCLUDED = ("id", "position", "layer", "start", "end", "duration")
_COPY_KEYS = {
    "keyframes": ("brightness", "contrast"),
    "brightness": ("brightness",),
    "contrast": ("contrast",),
    "mask": ("reader", "mask_reader", "title", "mask_invert"),
}


@editor_tool(
    "copy_transition_tool",
    label="Copy transition settings",
    schema=obj({
        "source_transition_id": string("The transition to copy from."),
        **TRANSITION_TARGET,
        "what": enum(["all", "keyframes", "brightness", "contrast", "mask"],
                     "all = every setting (Copy > Transition, then Paste); keyframes = progress + edge curves "
                     "(Copy > Keyframes > All); brightness / contrast = one curve; mask = the wipe image only.",
                     "all"),
        "fit_duration": boolean("Rescale copied curves to each target's length, so a curve copied from a 1 s "
                                "transition still finishes at the end of a 2 s one.", True),
    }, required=["source_transition_id"]),
    covers=("transition.copy_paste",),
)
def copy_transition(source_transition_id, transition_ids=None, between_clip_ids=None, track="", at_seconds=None,
                    scope="", what="all", fit_duration=True):
    """Copy one transition's settings onto other transitions (Copy > Transition / Keyframes, then Paste).

    "Make all the transitions on track 1 like this one" -> source_transition_id=X,
    scope='track', track='1'. Targets keep their own position, track and length (the
    Paste rule); with fit_duration the curves are fitted to each target's length. One undo
    step. To add new transitions use add_transitions_between_clips_tool. Refused on locked
    tracks; never deletes clips.
    """
    from classes.query import Transition

    src = Transition.get(id=str(source_transition_id).strip())
    if not src:
        raise ToolError(f"no transition with id={source_transition_id!r}")
    targets = [t for t in resolve_transitions(transition_ids, between_clip_ids, track, at_seconds, scope)
               if t.id != src.id]
    if not targets:
        raise ToolError("choose at least one other transition to paste onto")
    _ensure_unlocked(targets)
    fps = project_fps()
    src_data = copy.deepcopy(src.data)
    s0, s1 = _span(src_data)
    keys = [k for k in src_data if k not in _PASTE_EXCLUDED] if what == "all" else \
        [k for k in _COPY_KEYS[what] if k in src_data]
    changed = []
    for t in targets:
        data = copy.deepcopy(t.data)
        before = copy.deepcopy(data)
        for k in keys:
            data[k] = copy.deepcopy(src_data[k])
        if what in ("all", "mask") and "reader" in src_data and "mask_reader" not in src_data:
            data.pop("mask_reader", None)
        t0, t1 = _span(data)
        if fit_duration and any(k in keys for k in ("brightness", "contrast")):
            only = {k: data[k] for k in ("brightness", "contrast") if k in keys}
            probe = dict(data, **only)
            transition_ops.rescale_curves(probe, s1 - s0, t1 - t0, fps)
            for k in only:
                data[k] = probe[k]
        if data != before:
            _save(t, data)
            changed.append(t)
    if not changed:
        return ok("No change: the targets already match.", changed=False)
    refresh_preview()
    return ok(f"Copied {what} from transition {src.id} onto {len(changed)} transition(s).", changed=True,
              transitions=[describe_transition(t) for t in changed])


# ---------------------------------------------------------------------------
# add_transitions_between_clips_tool
# ---------------------------------------------------------------------------

def _clip_media_length(data):
    reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
    try:
        return float(reader.get("duration") or data.get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _retimed(data):
    pts = (data.get("time") or {}).get("Points") if isinstance(data.get("time"), dict) else None
    return bool(pts and len(pts) > 1)


@editor_tool(
    "add_transitions_between_clips_tool",
    label="Add transitions between clips",
    schema=obj({
        "track": string("The track whose consecutive clips get transitions (UI track number, 1 = bottom; name; or "
                        "layer number). Required unless timeline_clip_ids are given.", ""),
        "timeline_clip_ids": array({"type": "string"}, "Only join these clips (all on one track, joined in "
                                                       "timeline order). Leave out for every clip on the track."),
        "transition": string("'fade' (crossfade, default), 'random', or any transition name ('wipe_left_to_right', "
                             "'circle_in_to_out', list_transitions_tool).", "fade"),
        "duration_seconds": number("Length of each transition in seconds. 0 = the Default Transition Length "
                                   "preference.", 1.0, minimum=0),
        "method": enum(["ripple", "handles", "existing_overlaps"],
                       "How clips come to overlap. ripple (default, like Project Files > Add to Timeline): each "
                       "later clip and everything after it on the track moves earlier, so the track gets shorter by "
                       "one transition per join; other tracks do not move. handles: keep every cut in place and use "
                       "extra media before/after the cut (keeps sync with music/other tracks; joins without enough "
                       "spare media are skipped). existing_overlaps: only where clips already overlap; nothing "
                       "moves.", "ripple"),
        "audio_crossfade": boolean("Also crossfade the clips' audio (equal-power) under each transition.", True),
        "replace_existing": boolean("Replace a transition already on a join (otherwise that join is skipped).",
                                    False),
        "max_gap_seconds": number("Clips further apart than this are not joined (a gap stays a gap).", 0.05,
                                  minimum=0, maximum=10),
    }),
    covers=("transition.auto",),
)
def add_transitions_between_clips(track="", timeline_clip_ids=None, transition="fade", duration_seconds=1.0,
                                  method="ripple", audio_crossfade=True, replace_existing=False,
                                  max_gap_seconds=0.05):
    """Add a transition at every join between consecutive clips on a track ("crossfades between all clips").

    Use for "add crossfades between all my clips", "put a 0.5 s dissolve between every shot
    on track 1", "random transitions between the photos". Each join gets one transition of
    `duration_seconds` over the overlap of the two clips (clamped to half of the shorter
    clip). Read `method` for how the overlap is made; the receipt lists every join added,
    skipped (gap, too short, already has one, no spare media) and every clip moved. One undo
    step. Refused on a locked track; never deletes clips. For one specific pair you can also
    use apply_transition_tool.
    Example: track="1", transition="fade", duration_seconds=0.5.
    """
    from classes.query import Clip, Transition

    if timeline_clip_ids:
        chosen = []
        for cid in timeline_clip_ids:
            c = Clip.get(id=str(cid).strip())
            if not c:
                raise ToolError(f"no timeline clip with id={cid!r}")
            chosen.append(c)
        layer_nums = {_layer_of(c.data) for c in chosen}
        if len(layer_nums) != 1:
            raise ToolError("timeline_clip_ids must all be on one track")
        layer_num = layer_nums.pop()
        if str(track or "").strip() and resolve_layer(track) != layer_num:
            raise ToolError("timeline_clip_ids are not on that track")
        chosen_ids = {c.id for c in chosen}
    else:
        if not str(track or "").strip():
            raise ToolError("say which track (track) or which clips (timeline_clip_ids)")
        layer_num = resolve_layer(track)
        chosen_ids = None
    if is_locked(layer_num):
        raise ToolError(f"track {ui_track_number(layer_num) or layer_num} is locked; unlock it first")
    settings = get_app().get_settings()
    length = float(duration_seconds or 0.0) or float(settings.get("default-transition-length") or 1.0)
    length = snap_seconds(length)
    fps = project_fps()
    frame = 1.0 / fps
    if length < frame:
        raise ToolError("duration_seconds must be at least one frame")

    randomize = str(transition or "").strip().lower() == "random"
    pool = [e for e in transition_ops.catalog(("common", "extra"))] if randomize else None
    if randomize and not pool:
        raise ToolError("no transition images are installed")
    entry = None if randomize else _pick_mask(transition or "fade")

    clips = _clips_on(layer_num)
    if chosen_ids is not None:
        missing = chosen_ids - {c.id for c in clips}
        if missing:
            raise ToolError(f"clips {sorted(missing)} are not on that track")
    if len(clips) < 2:
        raise ToolError(f"track {ui_track_number(layer_num) or layer_num} has fewer than two clips to join")
    existing = [t for t in _transitions() if _layer_of(t.data) == layer_num]

    # Live positions as the plan shifts clips (ripple).
    pos = {c.id: float(c.data.get("position") or 0.0) for c in clips}
    start = {c.id: float(c.data.get("start") or 0.0) for c in clips}
    end = {c.id: float(c.data.get("end") or 0.0) for c in clips}
    tpos = {t.id: float(t.data.get("position") or 0.0) for t in existing}
    joins, skipped, replaced = [], [], []
    for a, b in zip(clips, clips[1:]):
        if chosen_ids is not None and not (a.id in chosen_ids and b.id in chosen_ids):
            continue
        a_len, b_len = end[a.id] - start[a.id], end[b.id] - start[b.id]
        a_end = pos[a.id] + a_len
        gap = pos[b.id] - a_end
        label = f"{a.id}->{b.id}"
        overlap = max(0.0, -gap)
        on_join = [t for t in existing if (lambda s: s[0] < max(a_end, pos[b.id]) + frame
                                           and s[1] > min(a_end, pos[b.id]) - frame)(
            (tpos[t.id], tpos[t.id] + _span(t.data)[1] - _span(t.data)[0]))]
        if on_join and not replace_existing:
            skipped.append({"join": label, "reason": "already has a transition"})
            continue
        d = min(length, 0.5 * a_len, 0.5 * b_len)
        d = max(frame, round(d * fps) / fps)
        if a_len < 2 * frame or b_len < 2 * frame:
            skipped.append({"join": label, "reason": "clip too short"})
            continue
        if overlap > frame / 2:
            # Already overlapping: use that overlap as it is.
            joins.append({"a": a, "b": b, "position": pos[b.id], "duration": min(overlap, a_len, b_len),
                          "replaces": on_join})
            continue
        if method == "existing_overlaps":
            skipped.append({"join": label, "reason": "clips do not overlap"})
            continue
        if gap > float(max_gap_seconds) + EPS:
            skipped.append({"join": label, "reason": f"{gap:.2f}s gap"})
            continue
        if method == "handles":
            half = d / 2
            if _retimed(a.data) or _retimed(b.data):
                skipped.append({"join": label, "reason": "speed-changed clip"})
                continue
            # The cut sits mid-gap; a plays on past it and b starts earlier, each by its
            # spare media, so both keep their frames at the same timeline times.
            cut = a_end + max(0.0, gap) / 2
            extend_a = cut + half - a_end
            # b's new start lands on the frame grid, so position and in-point move together
            pull_b = pos[b.id] - snap_seconds(cut - half)
            if _clip_media_length(a.data) - end[a.id] < extend_a - EPS or start[b.id] < pull_b - EPS:
                skipped.append({"join": label, "reason": "not enough spare media around the cut"})
                continue
            end[a.id] += extend_a
            pos[b.id] -= pull_b
            start[b.id] -= pull_b
            joins.append({"a": a, "b": b, "position": pos[b.id], "duration": d, "replaces": on_join})
            continue
        # ripple: pull b and everything after it earlier so the overlap is d
        shift = -(d + gap)
        b_pos = pos[b.id]
        for c in clips:
            if pos[c.id] >= b_pos - EPS and c.id != a.id:
                pos[c.id] += shift
        for t in existing:
            if tpos[t.id] >= b_pos - EPS:
                tpos[t.id] += shift
        for j in joins:
            if j["position"] >= b_pos - EPS:
                j["position"] += shift
        joins.append({"a": a, "b": b, "position": pos[b.id], "duration": d, "replaces": on_join})

    if not joins:
        why = "; ".join(f"{s['join']}: {s['reason']}" for s in skipped[:8])
        raise ToolError(f"no join to add a transition to ({why})" if why else "no consecutive clips to join")
    if any(p < -EPS for p in pos.values()):
        raise ToolError("that would move a clip before the start of the timeline")

    # Apply: clip edits, shifted transitions, replaced transitions, new transitions.
    moved = []
    for c in clips:
        new_pos = snap_seconds(max(0.0, pos[c.id]))
        changes = {}
        if abs(new_pos - float(c.data.get("position") or 0.0)) > EPS:
            changes["position"] = new_pos
        if abs(start[c.id] - float(c.data.get("start") or 0.0)) > EPS:
            changes["start"] = start[c.id]
        if abs(end[c.id] - float(c.data.get("end") or 0.0)) > EPS:
            changes["end"] = end[c.id]
        if changes:
            c.data.update(changes)
            c.save()
            moved.append({"timeline_clip_id": c.id, **{k: round(v, 3) for k, v in changes.items()}})
    replaced_ids = {t.id for j in joins for t in j["replaces"]}
    for t in existing:
        if t.id in replaced_ids:
            replaced.append(t.id)
            t.delete()
        elif abs(tpos[t.id] - float(t.data.get("position") or 0.0)) > EPS:
            t.data["position"] = snap_seconds(max(0.0, tpos[t.id]))
            t.save()
    readers = {}
    added = []
    for j in joins:
        e = random.choice(pool) if randomize else entry
        if e["path"] not in readers:
            readers[e["path"]] = _mask_reader_json(e["path"])
        data = transition_ops.new_mask_transition(
            None, readers[e["path"]], position=snap_seconds(max(0.0, j["position"])), layer=layer_num,
            duration=snap_seconds(j["duration"]), fps_float=fps, title=e["key"],
            fade_audio=bool(audio_crossfade))
        data.pop("id", None)
        t = Transition()
        t.data = data
        t.save()
        added.append({"transition_id": t.id, "mask": e["key"], "between_clip_ids": [j["a"].id, j["b"].id],
                      "position": data["position"], "duration": data["end"]})
    refresh_preview()
    summary = f"Added {len(added)} transition(s) on track {ui_track_number(layer_num) or layer_num}"
    if moved and method == "ripple":
        summary += f"; moved {len(moved)} clip(s) earlier to make the overlaps"
    elif moved:
        summary += f"; extended {len(moved)} clip(s) into their spare media"
    if skipped:
        summary += f"; skipped {len(skipped)} join(s)"
    return ok(summary + ".", changed=True, added=added, skipped=skipped, moved_clips=moved,
              replaced_transition_ids=replaced)

