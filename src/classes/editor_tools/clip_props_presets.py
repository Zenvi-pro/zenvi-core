"""apply_clip_preset_tool: every Fade, Motion, Transform and Volume preset of the timeline's clip menu.

Each preset calls the same handler the menu item calls
(``TimelineView.Fade_Triggered`` / ``Animate_Triggered`` / ``Rotate_Triggered`` /
``Crop_Triggered`` / ``Layout_Triggered`` / ``No_Transform_Triggered`` /
``Volume_Triggered``) with the same menu constant, inside the tool call's one
undo transaction. Workstream: clip-props.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from classes.editor_tools._base import (
    ToolError, enum, get_app, integer, nullable, number, obj, ok, playhead_seconds, refresh_preview, timeline_ui,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.clip_props import TARGETS, resolve_targets, save_clip_values
from classes.editor_tools.clip_props_model import ClipTime, curve_points, mirror_blocker


@dataclass(frozen=True)
class Preset:
    name: str
    category: str        # fade | motion | transform | volume
    menu: str            # where a person finds it
    call: str            # fade | animate | rotate | crop | layout | no_transform | volume | flip
    args: tuple = field(default_factory=tuple)
    zone: str = ""       # in | out | emphasis | whole: what duration_seconds / at_seconds mean


PRESETS: dict = {}


def _add(name, category, menu, call, *args, zone=""):
    PRESETS[name] = Preset(name, category, menu, call, tuple(args), zone)


# ── Fade (picture and sound) ─────────────────────────────────────────────────
_add("fade_none", "fade", "Fade > No Fade", "fade", "NONE", "Entire Clip")
_add("fade_in", "fade", "Fade > Fade In (duration_seconds, default 1 s)", "fade", "IN_FAST", "Start of Clip", zone="in")
_add("fade_in_fast", "fade", "Fade > Fade In > Fast (1 s)", "fade", "IN_FAST", "Start of Clip", zone="in")
_add("fade_in_slow", "fade", "Fade > Fade In > Slow (3 s)", "fade", "IN_SLOW", "Start of Clip", zone="in")
_add("fade_out", "fade", "Fade > Fade Out (duration_seconds, default 1 s)", "fade", "OUT_FAST", "End of Clip", zone="out")
_add("fade_out_fast", "fade", "Fade > Fade Out > Fast (1 s)", "fade", "OUT_FAST", "End of Clip", zone="out")
_add("fade_out_slow", "fade", "Fade > Fade Out > Slow (3 s)", "fade", "OUT_SLOW", "End of Clip", zone="out")
_add("fade_in_out", "fade", "Fade > Fade In and Out (duration_seconds, default 1 s)", "fade", "IN_OUT_FAST",
     "Entire Clip", zone="in")
_add("fade_in_out_fast", "fade", "Fade > Fade In and Out > Fast (1 s)", "fade", "IN_OUT_FAST", "Entire Clip", zone="in")
_add("fade_in_out_slow", "fade", "Fade > Fade In and Out > Slow (3 s)", "fade", "IN_OUT_SLOW", "Entire Clip", zone="in")

# ── Motion (visual clips) ────────────────────────────────────────────────────
_add("motion_none", "motion", "Motion > No Motion", "animate", "NONE")
for _dir, _enum in (("bottom", "UP"), ("left", "LEFT"), ("right", "RIGHT"), ("top", "DOWN")):
    _add(f"back_in_from_{_dir}", "motion", f"Motion > In > Back In > From {_dir.title()}", "animate",
         f"BACK_IN_{_enum}", zone="in")
_add("blur_in", "motion", "Motion > In > Blur In", "animate", "BLUR_IN", zone="in")
_add("bounce_in", "motion", "Motion > In > Bounce In > Center", "animate", "BOUNCE_IN", zone="in")
for _dir, _enum in (("bottom", "UP"), ("left", "LEFT"), ("right", "RIGHT"), ("top", "DOWN")):
    _add(f"bounce_in_from_{_dir}", "motion", f"Motion > In > Bounce In > From {_dir.title()}", "animate",
         f"BOUNCE_IN_{_enum}", zone="in")
_add("pop_in", "motion", "Motion > In > Pop In", "animate", "POP_IN", zone="in")
for _dir in ("bottom", "left", "right", "top"):
    _add(f"slide_in_from_{_dir}", "motion", f"Motion > In > Slide In > From {_dir.title()}", "animate",
         f"SLIDE_IN_{_dir.upper()}", zone="in")
_add("spiral_in", "motion", "Motion > In > Spiral In", "animate", "SPIRAL_IN", zone="in")
for _prefix, _label, _enum in (("wipe_in", "Wipe In", "WIPE_IN"), ("blur_wipe_in", "Blur Wipe In", "BLUR_WIPE_IN")):
    _add(f"{_prefix}_circle_expand", "motion", f"Motion > In > {_label} > Circle Expand", "animate",
         f"{_enum}_CIRCLE_EXPAND", zone="in")
    _add(f"{_prefix}_circle_shrink", "motion", f"Motion > In > {_label} > Circle Shrink", "animate",
         f"{_enum}_CIRCLE_SHRINK", zone="in")
    for _dir in ("bottom", "left", "right", "top"):
        _add(f"{_prefix}_from_{_dir}", "motion", f"Motion > In > {_label} > From {_dir.title()}", "animate",
             f"{_enum}_{_dir.upper()}", zone="in")
for _dir, _enum in (("bottom", "DOWN"), ("left", "LEFT"), ("right", "RIGHT"), ("top", "UP")):
    _add(f"back_out_to_{_dir}", "motion", f"Motion > Out > Back Out > To {_dir.title()}", "animate",
         f"BACK_OUT_{_enum}", zone="out")
_add("blur_out", "motion", "Motion > Out > Blur Out", "animate", "BLUR_OUT", zone="out")
_add("bounce_out", "motion", "Motion > Out > Bounce Out > Center", "animate", "BOUNCE_OUT", zone="out")
for _dir, _enum in (("bottom", "DOWN"), ("left", "LEFT"), ("right", "RIGHT"), ("top", "UP")):
    _add(f"bounce_out_to_{_dir}", "motion", f"Motion > Out > Bounce Out > To {_dir.title()}", "animate",
         f"BOUNCE_OUT_{_enum}", zone="out")
_add("pop_out", "motion", "Motion > Out > Pop Out", "animate", "POP_OUT", zone="out")
for _dir in ("bottom", "left", "right", "top"):
    _add(f"slide_out_to_{_dir}", "motion", f"Motion > Out > Slide Out > To {_dir.title()}", "animate",
         f"SLIDE_OUT_{_dir.upper()}", zone="out")
_add("spiral_out", "motion", "Motion > Out > Spiral Out", "animate", "SPIRAL_OUT", zone="out")
for _prefix, _label, _enum in (("wipe_out", "Wipe Out", "WIPE_OUT"), ("blur_wipe_out", "Blur Wipe Out", "BLUR_WIPE_OUT")):
    _add(f"{_prefix}_circle_expand", "motion", f"Motion > Out > {_label} > Circle Expand", "animate",
         f"{_enum}_CIRCLE_EXPAND", zone="out")
    _add(f"{_prefix}_circle_shrink", "motion", f"Motion > Out > {_label} > Circle Shrink", "animate",
         f"{_enum}_CIRCLE_SHRINK", zone="out")
    for _dir in ("bottom", "left", "right", "top"):
        _add(f"{_prefix}_to_{_dir}", "motion", f"Motion > Out > {_label} > To {_dir.title()}", "animate",
             f"{_enum}_{_dir.upper()}", zone="out")
for _name, _label, _enum in (("bounce", "Bounce", "BOUNCE"), ("flash", "Flash", "FLASH"),
                             ("heartbeat", "Heartbeat", "HEART_BEAT"), ("jello", "Jello", "JELLO"),
                             ("pulse", "Pulse", "PULSE"), ("rubber_band", "Rubber Band", "RUBBER_BAND"),
                             ("shake_x", "Shake X", "SHAKE_X"), ("shake_y", "Shake Y", "SHAKE_Y"),
                             ("swing", "Swing", "SWING"), ("tada", "Tada", "TADA"), ("wobble", "Wobble", "WOBBLE")):
    _add(_name, "motion", f"Motion > Emphasis > {_label}", "animate", _enum, zone="emphasis")
_add("zoom_in", "motion", "Motion > Camera > Zoom > In (100% -> 120%)", "animate", "CAM_PUSH_IN", zone="whole")
_add("zoom_out", "motion", "Motion > Camera > Zoom > Out (120% -> 100%)", "animate", "CAM_PULL_OUT", zone="whole")
for _name, _label, _enum in (("pan_auto", "Auto Direction", "CAM_PAN_AUTO"),
                             ("pan_left_to_right", "Left to Right", "CAM_PAN_RIGHT"),
                             ("pan_right_to_left", "Right to Left", "CAM_PAN_LEFT"),
                             ("pan_top_to_bottom", "Top to Bottom", "CAM_PAN_DOWN"),
                             ("pan_bottom_to_top", "Bottom to Top", "CAM_PAN_UP")):
    _add(_name, "motion", f"Motion > Camera > Pan > {_label}", "animate", _enum, zone="whole")
for _io, _io_label in (("in", "In"), ("out", "Out")):
    _add(f"ken_burns_{_io}", "motion", f"Motion > Camera > Zoom & Pan > {_io_label} > Auto Direction", "animate",
         f"KEN_BURNS_{_io.upper()}", zone="whole")
    for _dir, _dir_label in (("left_to_right", "Left to Right"), ("right_to_left", "Right to Left"),
                             ("top_to_bottom", "Top to Bottom"), ("bottom_to_top", "Bottom to Top")):
        _add(f"ken_burns_{_io}_{_dir}", "motion", f"Motion > Camera > Zoom & Pan > {_io_label} > {_dir_label}",
             "animate", f"KEN_BURNS_{_io.upper()}_{_dir.upper()}", zone="whole")
_add("credits_scroll_up", "motion", "Motion > Credits > Scroll Up", "animate", "CREDITS_UP", zone="whole")
_add("credits_scroll_down", "motion", "Motion > Credits > Scroll Down", "animate", "CREDITS_DOWN", zone="whole")

# ── Transform (visual clips) ─────────────────────────────────────────────────
_add("transform_none", "transform", "Transform > No Transform (no rotation, no crop, reset layout)", "no_transform")
_add("rotate_none", "transform", "Transform > Rotate > No Rotation", "rotate", "NONE")
_add("rotate_90_right", "transform", "Transform > Rotate > Rotate 90 (Right)", "rotate", "RIGHT_90")
_add("rotate_90_left", "transform", "Transform > Rotate > Rotate 90 (Left)", "rotate", "LEFT_90")
_add("rotate_180", "transform", "Transform > Rotate > Rotate 180 (Flip)", "rotate", "FLIP_180")
_add("flip_horizontal", "transform", "Mirror left-right (scale_x sign; not in the menu)", "flip", "scale_x")
_add("flip_vertical", "transform", "Mirror top-bottom (scale_y sign; not in the menu)", "flip", "scale_y")
_add("crop_none", "transform", "Transform > Crop > No Crop", "crop", "none")
_add("crop", "transform", "Transform > Crop > Crop (No Resize)", "crop", "crop")
_add("crop_resize", "transform", "Transform > Crop > Crop (Resize)", "crop", "resize")
_add("layout_reset", "transform", "Transform > Layout > Reset Layout", "layout", "NONE")
for _name, _label, _enum in (("center", "Center", "CENTER"), ("top_left", "Top Left", "TOP_LEFT"),
                             ("top_right", "Top Right", "TOP_RIGHT"), ("bottom_left", "Bottom Left", "BOTTOM_LEFT"),
                             ("bottom_right", "Bottom Right", "BOTTOM_RIGHT")):
    _add(f"layout_quarter_{_name}", "transform", f"Transform > Layout > 1/4 Size - {_label}", "layout", _enum)
_add("layout_grid", "transform", "Transform > Layout > Show All (Maintain Ratio)", "layout", "ALL_WITH_ASPECT")
_add("layout_grid_stretch", "transform", "Transform > Layout > Show All (Distort)", "layout", "ALL_WITHOUT_ASPECT")

# ── Volume (clips with sound) ────────────────────────────────────────────────
_add("volume_reset", "volume", "Audio > Volume > Reset Volume", "volume", "NONE", "Entire Clip")
_add("volume_level", "volume", "Audio > Volume > Level (level_percent 0-130)", "volume", "LEVEL", "Entire Clip")
_add("volume_fade_in", "volume", "Audio > Volume > Fade In (duration_seconds, default 1 s)", "volume",
     "FADE_IN_FAST", "Start of Clip", zone="in")
_add("volume_fade_in_fast", "volume", "Audio > Volume > Fade In > Fast", "volume", "FADE_IN_FAST", "Start of Clip", zone="in")
_add("volume_fade_in_slow", "volume", "Audio > Volume > Fade In > Slow", "volume", "FADE_IN_SLOW", "Start of Clip", zone="in")
_add("volume_fade_out", "volume", "Audio > Volume > Fade Out (duration_seconds, default 1 s)", "volume",
     "FADE_OUT_FAST", "End of Clip", zone="out")
_add("volume_fade_out_fast", "volume", "Audio > Volume > Fade Out > Fast", "volume", "FADE_OUT_FAST", "End of Clip", zone="out")
_add("volume_fade_out_slow", "volume", "Audio > Volume > Fade Out > Slow", "volume", "FADE_OUT_SLOW", "End of Clip", zone="out")
_add("volume_fade_in_out", "volume", "Audio > Volume > Fade In and Out (duration_seconds, default 1 s)", "volume",
     "FADE_IN_OUT_FAST", "Entire Clip", zone="in")
_add("volume_fade_in_out_fast", "volume", "Audio > Volume > Fade In and Out > Fast", "volume", "FADE_IN_OUT_FAST",
     "Entire Clip", zone="in")
_add("volume_fade_in_out_slow", "volume", "Audio > Volume > Fade In and Out > Slow", "volume", "FADE_IN_OUT_SLOW",
     "Entire Clip", zone="in")

PRESET_NAMES = tuple(PRESETS)


# ---------------------------------------------------------------------------
# Which clips a preset applies to
# ---------------------------------------------------------------------------

def _has_visual(data: dict) -> bool:
    """What the clip menu calls visual: the reader has video, or the clip draws its waveform."""
    reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
    has_video = reader.get("has_video")
    return has_video is None or bool(has_video) or bool(data.get("waveform", False))


def _has_audio(data: dict) -> bool:
    reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
    has_audio = reader.get("has_audio")
    return True if has_audio is None else bool(has_audio)


def _applies(preset: Preset, data: dict):
    """None when the preset applies to the clip, else the reason it is skipped."""
    if preset.category in ("motion", "transform") and not _has_visual(data):
        return "audio-only clip (no picture to move or transform)"
    if preset.category == "volume" and not _has_audio(data):
        return "clip has no sound"
    if preset.call == "flip":
        return mirror_blocker(preset.args[0], data.get("gravity", 4))
    return None


def _single_value(curve):
    pts = curve_points(curve)
    return float(pts[0]["co"]["Y"]) if len(pts) == 1 else None


def _already(preset: Preset, data: dict, level_percent) -> bool:
    """Reset/level presets that would write what the clip already has (skip the handler: no undo step)."""
    if preset.name == "fade_none":
        return ((not _has_visual(data) or _single_value(data.get("alpha")) == 1.0)
                and (not _has_audio(data) or _single_value(data.get("volume")) == 1.0))
    if preset.name == "volume_reset":
        return _single_value(data.get("volume")) == 1.0
    if preset.name == "volume_level":
        return _single_value(data.get("volume")) == float(level_percent) / 100.0
    return False


# ---------------------------------------------------------------------------
# History hygiene: a preset that changed nothing leaves no undo step
# ---------------------------------------------------------------------------

def _is_noop_action(action) -> bool:
    if getattr(action, "type", None) != "update":
        return False
    values, old = getattr(action, "values", None), getattr(action, "old_values", None)
    return isinstance(values, dict) and isinstance(old, dict) and all(old.get(k) == v for k, v in values.items())


class _History:
    """Remember the undo/redo stacks before a handler runs; drop the updates that changed nothing."""

    def __init__(self, updates):
        self.updates = updates
        self.start = len(updates.actionHistory)
        self.redo = list(updates.redoHistory)

    def drop_noops(self) -> bool:
        history = self.updates.actionHistory
        added = history[self.start:]
        real = [a for a in added if not _is_noop_action(a)]
        if len(real) != len(added):
            del history[self.start:]
            history.extend(real)
        if not real:
            self.updates.redoHistory[:] = self.redo
        try:
            self.updates.update_watchers()
        except Exception:
            pass
        return bool(real)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

def _snapshot(clip_ids):
    from classes.query import Clip
    out = {}
    for cid in clip_ids:
        c = Clip.get(id=cid)
        if c:
            data = copy.deepcopy(c.data)
            data.pop("ui", None)
            out[cid] = data
    return out


def _diff(before: dict, after: dict) -> dict:
    changed = sorted(k for k in set(before) | set(after) if k != "effects" and before.get(k) != after.get(k))
    old = {e.get("id"): e for e in before.get("effects") or [] if isinstance(e, dict)}
    new = {e.get("id"): e for e in after.get("effects") or [] if isinstance(e, dict)}
    out = {"changed": changed}
    added = [{"id": i, "class_name": e.get("class_name")} for i, e in new.items() if i not in old]
    removed = [{"id": i, "class_name": e.get("class_name")} for i, e in old.items() if i not in new]
    updated = [{"id": i, "class_name": e.get("class_name")} for i, e in new.items() if i in old and old[i] != e]
    if added:
        out["effects_added"] = added
    if removed:
        out["effects_removed"] = removed
    if updated:
        out["effects_changed"] = updated
    return out


def _call_handler(preset: Preset, ids: list, tid, duration_seconds, at_seconds, level_percent):
    from windows.views.timeline_backend import enums

    tl = timeline_ui()
    if preset.call == "fade":
        tl.Fade_Triggered(enums.MenuFade[preset.args[0]], ids, preset.args[1], transaction_id=tid,
                          fade_seconds=duration_seconds)
    elif preset.call == "animate":
        tl.Animate_Triggered(enums.MenuAnimate[preset.args[0]], ids, transaction_id=tid,
                             zone_seconds=duration_seconds, emphasis_seconds=at_seconds)
    elif preset.call == "rotate":
        tl.Rotate_Triggered(enums.MenuRotate[preset.args[0]], ids)
    elif preset.call == "crop":
        tl.Crop_Triggered(ids, preset.args[0])
    elif preset.call == "layout":
        tl.Layout_Triggered(enums.MenuLayout[preset.args[0]], ids)
    elif preset.call == "no_transform":
        tl.No_Transform_Triggered(ids)
    elif preset.call == "volume":
        level = float(level_percent) if level_percent is not None else 1.0
        tl.Volume_Triggered(enums.MenuVolume[preset.args[0]], ids, preset.args[1], level, transaction_id=tid,
                            fade_seconds=duration_seconds)
    elif preset.call == "flip":
        _flip(ids, preset.args[0])
    else:  # pragma: no cover - table and dispatcher are kept in step by tests
        raise ToolError(f"preset {preset.name} has no handler")


def _flip(ids, key):
    """Mirror clips by negating every point of scale_x or scale_y (keeps zoom animation)."""
    from classes.query import Clip
    for cid in ids:
        clip = Clip.get(id=cid)
        curve = copy.deepcopy(clip.data.get(key) or {"Points": []})
        for p in curve.get("Points") or []:
            p["co"]["Y"] = -float(p["co"]["Y"])
        save_clip_values(cid, {key: curve})


_DOC_GROUPS = {}
for _p in PRESETS.values():
    _DOC_GROUPS.setdefault(_p.category, []).append(_p.name)


@editor_tool(
    "apply_clip_preset_tool",
    label="Apply clip preset",
    schema=obj({
        **TARGETS,
        "preset": enum(list(PRESET_NAMES), "The Clip-menu preset to apply (see the tool description for the list)."),
        "duration_seconds": nullable(number(
            "Length in seconds of a fade (fade_in/fade_out/fade_in_out and volume_fade_*; default 1 s, "
            "*_fast = 1 s, *_slow = 3 s) or of an In / Out / Emphasis motion (default 1 s). Camera moves "
            "and credits always span the whole clip.", minimum=0.04, maximum=600)),
        "at_seconds": nullable(number(
            "Emphasis presets only: timeline time (seconds) where the emphasis starts. Default: the playhead "
            "if it is over the clip, else the clip's start.")),
        "level_percent": nullable(integer("volume_level only: the level in percent, 0-130 (100 = unchanged).",
                                          minimum=0, maximum=130)),
    }, required=["preset"]),
    covers=("clip.fade", "clip.motion", "clip.transform", "clip.volume_presets"),
)
def apply_clip_preset(preset, timeline_clip_ids=None, clip_query="", track="", scope="", duration_seconds=None,
                      at_seconds=None, level_percent=None):
    """Apply a one-click Fade, Motion, Transform or Volume preset from the timeline's clip menu.

    Exactly what the clip menu does, on one or many clips, as one undo step.
    Use it for "fade in the first clip", "fade everything in and out over 2
    seconds", "slow zoom on the speaker" (zoom_in), "Ken Burns the photos"
    (ken_burns_in), "slide the title in from the left", "make it bounce",
    "picture-in-picture top right" (layout_quarter_top_right), "rotate it 90
    degrees", "mirror it" (flip_horizontal), "crop it" (crop / crop_resize adds
    the Crop effect; set its left/top/right/bottom with the effect tools), "tile
    these clips in a grid" (layout_grid), "reset the transform", "set the music
    to 60%" (volume_level + level_percent=60), "fade the music out".

    Presets:
    fade (picture and sound): fade_none, fade_in, fade_in_fast, fade_in_slow,
    fade_out, fade_out_fast, fade_out_slow, fade_in_out, fade_in_out_fast,
    fade_in_out_slow.
    motion In (first second): back_in_from_{bottom,left,right,top}, blur_in,
    bounce_in, bounce_in_from_{bottom,left,right,top}, pop_in,
    slide_in_from_{bottom,left,right,top}, spiral_in,
    wipe_in_{circle_expand,circle_shrink}, wipe_in_from_{bottom,left,right,top},
    blur_wipe_in_{circle_expand,circle_shrink}, blur_wipe_in_from_{...}.
    motion Out (last second): back_out_to_{...}, blur_out, bounce_out,
    bounce_out_to_{...}, pop_out, slide_out_to_{bottom,left,right,top},
    spiral_out, wipe_out_{circle_expand,circle_shrink}, wipe_out_to_{...},
    blur_wipe_out_{circle_expand,circle_shrink}, blur_wipe_out_to_{...}.
    motion Emphasis (1 s at the playhead or at_seconds): bounce, flash,
    heartbeat, jello, pulse, rubber_band, shake_x, shake_y, swing, tada, wobble.
    motion Camera (whole clip): zoom_in, zoom_out, pan_auto, pan_left_to_right,
    pan_right_to_left, pan_top_to_bottom, pan_bottom_to_top, ken_burns_in,
    ken_burns_in_{left_to_right,right_to_left,top_to_bottom,bottom_to_top},
    ken_burns_out, ken_burns_out_{...}; credits_scroll_up, credits_scroll_down;
    motion_none resets all motion (scale, position, rotation, shear, opacity,
    motion blur/wipe effects).
    transform: transform_none, rotate_none, rotate_90_right, rotate_90_left,
    rotate_180, flip_horizontal, flip_vertical, crop_none, crop, crop_resize,
    layout_reset, layout_quarter_{center,top_left,top_right,bottom_left,
    bottom_right} (half width and height = picture-in-picture), layout_grid,
    layout_grid_stretch.
    volume (clips with sound): volume_reset, volume_level, volume_fade_in,
    volume_fade_in_fast, volume_fade_in_slow, volume_fade_out, _fast, _slow,
    volume_fade_in_out, _fast, _slow.

    flip_horizontal/flip_vertical mirror in place only when the clip's gravity is
    centred on that axis (Center, Top Center, Bottom Center for left-right); a
    corner PiP is refused with how to fix it.
    Motion presets are relative to the clip's current look and only replace
    keyframes inside their zone, so they combine (e.g. zoom_in plus fade_in).
    Re-applying a blur or wipe motion adds another Blur/Mask effect; use
    motion_none first to start over. On clips shorter than 6 s prefer
    fade_in_out over separate fade_in and fade_out. Motion and transform
    presets skip audio-only clips, volume presets skip silent clips, and clips
    on locked tracks are refused (skipped with scope); the receipt lists what
    changed and which effects were added (e.g. the Crop effect id).

    Example: apply_clip_preset_tool(scope="all", preset="fade_in_out", duration_seconds=2)
    """
    spec = PRESETS.get(preset)
    if spec is None:
        raise ToolError(f"unknown preset {preset!r}")
    if duration_seconds is not None:
        if spec.zone not in ("in", "out", "emphasis"):
            raise ToolError(f"{preset} takes no duration_seconds: "
                            + ("camera moves and credits span the whole clip; trim the clip or animate part of it "
                               "with set_keyframes_tool" if spec.zone == "whole" else "it has no length"))
    if at_seconds is not None and spec.zone != "emphasis":
        raise ToolError(f"at_seconds only sets where an Emphasis preset starts; {preset} is not one")
    if spec.name == "volume_level":
        if level_percent is None:
            raise ToolError("volume_level needs level_percent (0-130, 100 = unchanged)")
    elif level_percent is not None:
        raise ToolError("level_percent only goes with preset='volume_level'")

    clips, skipped = resolve_targets(timeline_clip_ids, clip_query, track, scope)
    targets = []
    for clip in clips:
        reason = _applies(spec, clip.data)
        if reason:
            skipped.append({"timeline_clip_id": clip.id, "reason": reason})
        else:
            targets.append(clip)
    if not targets:
        raise ToolError(f"{preset} does not apply to the chosen clip(s): "
                        + "; ".join(f"{s['timeline_clip_id']}: {s['reason']}" for s in skipped))
    if at_seconds is not None:
        inside = [c for c in targets if ClipTime.of(c.data).contains(ClipTime.of(c.data).from_timeline_seconds(at_seconds))]
        if not inside:
            raise ToolError(f"at_seconds={at_seconds} is not over any of the chosen clips")

    pending = [c for c in targets if not _already(spec, c.data, level_percent)]
    ids = [c.id for c in pending]
    info = _receipt_info(spec, targets, duration_seconds, at_seconds, level_percent)
    if not pending:
        return ok(f"Nothing to change: {spec.menu} is already how {len(targets)} clip(s) look.", changed=False,
                  preset=preset, timeline_clip_ids=[c.id for c in targets], skipped=skipped, **info)

    app = get_app()
    before = _snapshot(ids)
    history = _History(app.updates)
    from classes.updates import nested_transaction
    with nested_transaction(app.updates) as tid:
        _call_handler(spec, ids, tid, duration_seconds, at_seconds, level_percent)
    after = _snapshot(ids)
    changes = {cid: _diff(before[cid], after.get(cid, {})) for cid in before}
    changed_ids = [cid for cid, d in changes.items() if d["changed"] or len(d) > 1]
    history.drop_noops()
    refresh_preview()
    if not changed_ids:
        return ok(f"Nothing to change: {spec.menu} left {len(ids)} clip(s) as they were.", changed=False,
                  preset=preset, timeline_clip_ids=ids, skipped=skipped, **info)
    props = sorted({k for cid in changed_ids for k in changes[cid]["changed"]})
    note = f" Skipped {len(skipped)} clip(s)." if skipped else ""
    return ok(f"Applied {spec.menu} to {len(changed_ids)} clip(s); changed {', '.join(props) or 'effects'}.{note}",
              changed=True, preset=preset, menu=spec.menu,
              clips=[{"timeline_clip_id": cid, **changes[cid]} for cid in changed_ids], skipped=skipped, **info)


def _receipt_info(spec: Preset, targets, duration_seconds, at_seconds, level_percent) -> dict:
    info = {}
    if spec.call in ("fade", "volume") and spec.zone:
        default = 3.0 if spec.args[0].endswith("SLOW") else 1.0
        info["fade_seconds"] = float(duration_seconds) if duration_seconds else default
    elif spec.zone in ("in", "out", "emphasis"):
        info["zone_seconds"] = float(duration_seconds) if duration_seconds else 1.0
    if spec.zone == "emphasis":
        t = at_seconds if at_seconds is not None else playhead_seconds()
        starts = {}
        for c in targets:
            ct = ClipTime.of(c.data)
            frame = ct.from_timeline_seconds(t)
            starts[c.id] = round(t if ct.contains(frame) else ct.position, 3)
        info["emphasis_starts_at"] = starts
    if spec.name == "volume_level":
        info["level_percent"] = level_percent
    return info
