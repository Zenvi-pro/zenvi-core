"""Edits made to a Zenvi export's readable timeline, brought back onto the original Zenvi objects.

``src/zenvi/timeline.json`` carries the readable timeline (what the Remotion
renderer plays) and, under ``zenvi``, the original project plus ``baseline``:
the readable timeline exactly as it was exported. On re-import the two
readable copies are compared entity by entity and field by field, and each
change is applied to the original object it came from -- every other key,
known or not, is kept, so an untouched export restores bit-identical.

What comes back:

* clips: ``position`` (timeline start), ``start`` / ``end`` (source in / out)
  -- seconds, the same fields the Remotion renderer reads --, ``track`` / ``layer``,
  ``title``, ``fileId`` / ``src`` (another file of the export), ``hasAudio`` /
  ``hasVideo``, ``scaleMode``, ``gravity``, ``blendMode`` and the keyframes of
  every exported property (alpha, location, scale, rotation, origin, shear,
  margin, corner radius, volume: value, frame and easing; frames are clip
  frames, 0 = the clip's first frame before any trim); a clip removed
  from ``clips`` is deleted; a copy with a new ``id`` is a duplicate;
* markers (by ``id``): time / frame, name, colour; removed and new markers;
* transitions (by ``id``): ``from``, ``durationInFrames`` (curves refit like a
  drag-trim), track / layer, title; removed transitions;
* tracks (by ``layer``): name, locked; tracks clips move to are created.

Everything is validated first; an impossible value is refused with a warning
naming the clip and field (the original value stays). Changes that cannot be
mapped back -- speed (``playbackRate``, holds, ramps), effects (``filters``,
``crop``), the composition, the background, new media -- are reported, never
guessed. Pure: no Qt, no project access.
"""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Optional, Set, Tuple

from classes.handoff.keyframes import BEZIER, CONSTANT, LINEAR, Curve
from classes.handoff.remotion.exporter import BLEND_MODES, CLIP_KEYS
from classes.handoff.timeline_view import source_frames, time_remaps

TRACK_STEP = 1000000
MARKER_COLORS = ("blue", "red", "green", "yellow", "orange", "purple", "pink", "white")  # track_ops.MARKER_COLORS
MAX_VOLUME = 1.3        # clip_props_model.MAX_VOLUME (the Volume menu's 130 % ceiling)
# libopenshot 1.0 Clip::PropertiesJSON ranges, widened where the editor goes beyond them
# (editor_tools.clip_props_model._RANGE_OVERRIDES); scale/shear use keyframe_rules.max_transform_multiple.
RANGES: Dict[str, Tuple[float, float]] = {
    "alpha": (0.0, 1.0), "location_x": (-10.0, 10.0), "location_y": (-10.0, 10.0),
    "rotation": (-3600.0, 3600.0), "origin_x": (0.0, 1.0), "origin_y": (0.0, 1.0), "volume": (0.0, MAX_VOLUME),
    "margin": (0.0, 0.5), "corner_radius": (0.0, 0.5),
}
SCALE_LIKE = ("scale_x", "scale_y", "shear_x", "shear_y")
# readable clip keys that follow from other data: an edit to them alone cannot be applied
DERIVED_CLIP_KEYS = {
    "kind": "the kind follows from the media",
    "transparent": "transparency follows from the media",
    "sourceWidth": "the source size follows from the media",
    "sourceHeight": "the source size follows from the media",
    "maxScale": "it follows from the scale keyframes",
    "filters": "effect edits are not brought back; change the effect in Zenvi",
    "crop": "effect edits are not brought back; change the Crop effect in Zenvi",
}
# fields of older exports / Remotion habits that are not the timing source of truth
TIMING_HINTS = {
    "from": "move the clip with 'position' (seconds); the renderer derives its frames",
    "durationInFrames": "change 'end' (source out, seconds); the renderer derives its frames",
}
_BLEND_CODES = {name: code for code, name in BLEND_MODES.items()}


@dataclass(frozen=True)
class Edit:
    """One change brought back (``field`` ``deleted`` / ``added`` for whole objects)."""

    kind: str          # clip | marker | transition | track
    id: str
    field: str
    before: Any = None
    after: Any = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "field": self.field, "before": self.before, "after": self.after}


class _Refused(Exception):
    """A field edit that cannot be applied; the message is the warning."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _number(value: Any) -> Optional[float]:
    """*value* as a float when it is a finite number (not a bool), else None."""
    return float(value) if _is_number(value) else None


def _whole(value: Any) -> Optional[int]:
    """*value* as an int when it is a whole finite number (not a bool), else None."""
    number = _number(value)
    return int(number) if number is not None and number == int(number) else None


def _point(x: float, y: float, interpolation: int = BEZIER) -> dict:
    return {"co": {"X": float(x), "Y": float(y)}, "handle_left": {"X": 0.5, "Y": 1.0},
            "handle_right": {"X": 0.5, "Y": 0.0}, "handle_type": 0, "interpolation": int(interpolation)}


def _constant(value: float) -> dict:
    return {"Points": [_point(1, value)]}


class _Context:
    def __init__(self, project: dict, timeline: dict):
        fps = project.get("fps") or {}
        try:
            self.fps = Fraction(int(fps.get("num") or 30), int(fps.get("den") or 1))
        except (TypeError, ValueError, ZeroDivisionError):
            self.fps = Fraction(30, 1)
        self.width = int(project.get("width") or 1920)
        self.height = int(project.get("height") or 1080)
        self.files = {str(f.get("id")): f for f in project.get("files") or [] if isinstance(f, dict)}
        self.layers = {int(ly.get("number") or 0) for ly in project.get("layers") or [] if isinstance(ly, dict)}
        self.warnings: List[str] = []
        self.applied: List[Edit] = []
        self.taken: Set[str] = set()
        for key in ("files", "clips", "effects", "markers", "layers"):
            for item in project.get(key) or []:
                if isinstance(item, dict) and item.get("id"):
                    self.taken.add(str(item["id"]))
        for c in project.get("clips") or []:
            for e in (c.get("effects") or []) if isinstance(c, dict) else []:
                if isinstance(e, dict) and e.get("id"):
                    self.taken.add(str(e["id"]))
        media: Dict[str, str] = {}
        baseline_media = ((timeline.get("zenvi") or {}).get("baseline") or {}).get("media") or {}
        for source in (baseline_media, timeline.get("media") or {}):
            if isinstance(source, dict):
                for fid, entry in source.items():
                    if isinstance(entry, dict) and isinstance(entry.get("src"), str):
                        media[entry["src"]] = str(fid)
        self.src_to_file = media
        self.new_layers: Dict[int, dict] = {}

    @property
    def fps_f(self) -> float:
        return float(self.fps)

    def snap(self, seconds: float) -> float:
        frames = math.floor(float(seconds) * self.fps_f + 0.5)
        return frames / self.fps_f

    def new_id(self, wanted: Any = None) -> str:
        text = str(wanted or "").strip()
        if text and text not in self.taken and text.isalnum() and len(text) <= 64:
            self.taken.add(text)
            return text
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        while True:
            candidate = "".join(random.choice(chars) for _ in range(10))
            if candidate not in self.taken:
                self.taken.add(candidate)
                return candidate

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def note(self, kind: str, oid: str, field: str, before: Any, after: Any) -> None:
        self.applied.append(Edit(kind, str(oid), field, before, after))

    def ensure_layer(self, number: int, label: str = "", lock: bool = False) -> None:
        if number in self.layers or number in self.new_layers:
            return
        self.new_layers[number] = {"id": self.new_id(), "number": int(number), "y": 0, "label": str(label or ""),
                                   "lock": bool(lock)}


# ---------------------------------------------------------------------------
# Keyframes
# ---------------------------------------------------------------------------

def _range_for(key: str, ctx: _Context, clip: dict) -> Tuple[float, float]:
    if key in SCALE_LIKE:
        from classes.keyframe_rules import max_transform_multiple
        path = str(((clip.get("reader") or {}).get("path")) or "").lower()
        m = float(max_transform_multiple(ctx.width, ctx.height, path.endswith(".svg")))
        return -m, m
    return RANGES.get(key, (-1e9, 1e9))


def _easing_ok(easing: Any) -> bool:
    if easing is None or easing in ("linear", "hold"):
        return True
    return (isinstance(easing, list) and len(easing) == 4 and all(_is_number(v) for v in easing)
            and 0.0 <= float(easing[0]) <= 1.0 and 0.0 <= float(easing[2]) <= 1.0)


def keyframe_from_keys(keys: Any, offset: int, original: Any, lo: float, hi: float, what: str, *,
                       base_keys: Any = None, base_offset: Optional[int] = None) -> dict:
    """A libopenshot keyframe from readable ``[{frame, value, easing}]``.

    Frames count from the clip's first visible frame; *offset* is its trimmed
    frames (``X = frame + 1 + offset``). Each key's easing shapes the segment
    ending at it. *base_keys* (the exported keys, one per point of *original*)
    let untouched points come back verbatim -- exact values, handles and any
    other keys -- when the clip's trim (*base_offset*) did not change; edited
    points are rebuilt (reusing the point object at their index for unknown
    keys). Raises _Refused with the reason.
    """
    if not isinstance(keys, list) or not keys:
        raise _Refused(f"{what}: needs a list of at least one keyframe")
    pts: Dict[float, Tuple[float, Any, dict]] = {}
    for i, k in enumerate(keys):
        if not isinstance(k, dict):
            raise _Refused(f"{what}: keyframe {i + 1} is not an object")
        frame, value, easing = _number(k.get("frame")), _number(k.get("value")), k.get("easing")
        if frame is None or value is None:
            raise _Refused(f"{what}: keyframe {i + 1} needs a numeric frame and value")
        if not lo - 1e-9 <= value <= hi + 1e-9:
            raise _Refused(f"{what}: {value} is outside {lo:g}..{hi:g}")
        if not _easing_ok(easing):
            raise _Refused(f"{what}: keyframe {i + 1} easing must be 'linear', 'hold' or [x1, y1, x2, y2] with "
                           "x1 and x2 in 0..1")
        x = frame + 1.0 + float(offset)
        if x < 1.0:
            raise _Refused(f"{what}: frame {frame} is before the start of the clip's source")
        pts[round(x, 6)] = (value, easing, k)
    found = original.get("Points") if isinstance(original, dict) else None
    base_points: List[Any] = found if isinstance(found, list) else []
    verbatim: Dict[int, int] = {}  # output index -> original point index
    if base_offset is not None and base_offset == offset and isinstance(base_keys, list) \
            and len(base_keys) == len(base_points):
        unused = list(range(len(base_keys)))
        for i, x in enumerate(sorted(pts)):
            key = pts[x][2]
            for j in unused:
                if base_keys[j] == key and isinstance(base_points[j], dict):
                    verbatim[i] = j
                    unused.remove(j)
                    break
    out: List[dict] = []
    for i, x in enumerate(sorted(pts)):
        value, easing, _key = pts[x]
        if i in verbatim:
            out.append(copy.deepcopy(base_points[verbatim[i]]))
            if i > 0 and isinstance(easing, list) and not (i - 1 in verbatim and verbatim[i - 1] == verbatim[i] - 1):
                out[-2]["handle_right"] = {"X": float(easing[0]), "Y": float(easing[1])}  # a new neighbour
            continue
        src = base_points[i] if i < len(base_points) and isinstance(base_points[i], dict) else {}
        p = copy.deepcopy(src) if src else _point(x, value)
        p["co"] = dict(p.get("co") or {}, X=float(x), Y=float(value))
        p.setdefault("handle_left", {"X": 0.5, "Y": 1.0})
        p.setdefault("handle_right", {"X": 0.5, "Y": 0.0})
        p.setdefault("handle_type", 0)
        if i == 0:
            p.setdefault("interpolation", BEZIER)  # no segment ends at the first point
        elif easing == "hold":
            p["interpolation"] = CONSTANT
        elif isinstance(easing, list):
            p["interpolation"] = BEZIER
            p["handle_left"] = {"X": float(easing[2]), "Y": float(easing[3])}
            out[-1]["handle_right"] = {"X": float(easing[0]), "Y": float(easing[1])}
        else:
            p["interpolation"] = LINEAR
        out.append(p)
    result = {k: copy.deepcopy(v) for k, v in original.items() if k != "Points"} if isinstance(original, dict) else {}
    result["Points"] = out
    return result


def _flag(value: bool) -> dict:
    return {"Points": [_point(1, 1.0 if value else 0.0, CONSTANT)]}


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

def _changed(base: dict, cur: dict) -> Set[str]:
    return {k for k in set(base) | set(cur) if base.get(k) != cur.get(k)}


def _label(entry: dict) -> str:
    return f"clip {entry.get('title') or entry.get('id')!r}"


def _media_seconds(clip: dict, ctx: _Context) -> Optional[float]:
    f = ctx.files.get(str(clip.get("file_id") or "")) or clip.get("reader") or {}
    if not isinstance(f, dict) or f.get("media_type") == "image" or f.get("has_single_image"):
        return None
    try:
        seconds = float(f.get("duration") or 0.0)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _source_frame_span(clip: dict, ctx: _Context, start: float, end: float) -> Tuple[int, int]:
    """(lowest, highest) 1-based media frame clip frames *start*..*end* show (the ``time`` remap applied)."""
    rate = ctx.fps_f
    first = int(round(start * rate)) + 1
    last = max(first, int(round(end * rate)))
    time_kf = clip.get("time")
    if not time_remaps(time_kf):
        return first, last
    return source_frames(Curve.from_json(time_kf, fps=ctx.fps), first, last)


def _window_problem(clip: dict, ctx: _Context, start: float, end: float, old_start: Optional[float],
                    old_end: Optional[float]) -> Optional[str]:
    """Why clip frames *start*..*end* would show frames past the end of the clip's media, else None.

    Through the clip's ``time`` curve when it remaps time: a slowed clip's
    window is longer than its media, a frozen one holds a frame. A window
    that shows no more of the media than the old one (*old_start*..*old_end*)
    is never refused.
    """
    media = _media_seconds(clip, ctx)
    if media is None:
        return None
    rate = ctx.fps_f
    if not time_remaps(clip.get("time")):
        if end > media + 0.5 / rate:
            return f"end {end:g} s is past the end of its {media:g} s media"
        return None
    _low, high = _source_frame_span(clip, ctx, start, end)
    limit = int(round(media * rate))
    if high <= limit:
        return None
    if old_start is not None and old_end is not None and high <= _source_frame_span(clip, ctx, old_start, old_end)[1]:
        return None
    return (f"start {start:g} s / end {end:g} s would show media frame {high} through its speed curve, past the end "
            f"of its {media:g} s ({limit} frame) media")


def apply_clip(clip: dict, base: dict, cur: dict, ctx: _Context, *, tracks: List[dict]) -> None:
    """Apply the differences between *base* and *cur* (readable entries) onto *clip* (in place)."""
    changed = _changed(base, cur)
    if not changed:
        return
    cid, label = str(clip.get("id")), _label(cur)

    for key in sorted(changed & set(DERIVED_CLIP_KEYS)):
        ctx.warn(f"{label}: '{key}' was edited but not brought back: {DERIVED_CLIP_KEYS[key]}")
    known = {"id", "title", "fileId", "src", "track", "layer", "position", "start", "end", "time", "keyframes",
             "hasAudio", "hasVideo", "scaleMode", "gravity", "blendMode"}
    for key in sorted(changed & set(TIMING_HINTS)):
        ctx.warn(f"{label}: '{key}' was edited but not brought back: {TIMING_HINTS[key]}")
    for key in sorted(changed - known - set(DERIVED_CLIP_KEYS) - set(TIMING_HINTS)):
        ctx.warn(f"{label}: '{key}' is not a field Zenvi reads back; ignored")

    if "title" in changed:
        if isinstance(cur.get("title"), str):
            ctx.note("clip", cid, "title", clip.get("title"), cur["title"])
            clip["title"] = cur["title"]
        else:
            ctx.warn(f"{label}: title must be text; kept {clip.get('title')!r}")

    # media: another file of the export (it must cover the clip's window, checked with the timing below)
    target: Optional[str] = None
    if changed & {"fileId", "src"}:
        if "fileId" in changed and str(cur.get("fileId")) in ctx.files:
            target = str(cur["fileId"])
        elif "src" in changed and cur.get("src") in ctx.src_to_file:
            target = ctx.src_to_file[cur["src"]]
        if target is None:
            ctx.warn(f"{label}: its media was changed to {cur.get('src') or cur.get('fileId')!r}, which is not one "
                     "of the exported media files; new media is not brought back (import it in Zenvi)")
        elif target == str(clip.get("file_id")):
            target = None

    # timing
    rate = ctx.fps_f
    position = float(clip.get("position") or 0.0)
    try:
        if "position" in changed:
            seconds = _number(cur.get("position"))
            if seconds is None or seconds < 0:
                raise _Refused(f"{label}: position must be 0 or more seconds, got {cur.get('position')!r}")
            position = ctx.snap(seconds)
        if abs(position - float(clip.get("position") or 0.0)) > 1e-9:
            ctx.note("clip", cid, "position", clip.get("position"), round(position, 6))
            clip["position"] = position
    except _Refused as exc:
        ctx.warn(f"{exc}; kept {clip.get('position')}")

    start, end = float(clip.get("start") or 0.0), float(clip.get("end") or 0.0)
    new_start, new_end = start, end
    try:
        if "start" in changed:
            seconds = _number(cur.get("start"))
            if seconds is None or seconds < 0:
                raise _Refused(f"{label}: start (source in) must be 0 or more seconds, got {cur.get('start')!r}")
            new_start = ctx.snap(seconds)
        if "end" in changed:
            seconds = _number(cur.get("end"))
            if seconds is None:
                raise _Refused(f"{label}: end (source out) must be seconds, got {cur.get('end')!r}")
            new_end = ctx.snap(seconds)
        if (new_start, new_end) != (start, end) and new_end - new_start < 1.0 / rate - 1e-9:
            raise _Refused(f"{label}: end {new_end:g} s must be at least one frame after start {new_start:g} s")
    except _Refused as exc:
        ctx.warn(f"{exc}; kept {start:g}-{end:g} s")
        new_start, new_end = start, end
    media_clip = dict(clip, file_id=target) if target else clip
    if (new_start, new_end) != (start, end):
        why = _window_problem(media_clip, ctx, new_start, new_end, start, end)
        if why:
            ctx.warn(f"{label}: {why}; kept {start:g}-{end:g} s")
            new_start, new_end = start, end
    if target is not None:
        why = _window_problem(media_clip, ctx, new_start, new_end, None, None)
        if why:
            ctx.warn(f"{label}: its media was changed to {cur.get('src') or cur.get('fileId')!r}, but {why}; kept "
                     "its media")
            target = None
        else:
            ctx.note("clip", cid, "file_id", clip.get("file_id"), target)
            clip["file_id"] = target
            clip["reader"] = copy.deepcopy(ctx.files[target])
    if new_start != start:
        ctx.note("clip", cid, "start", start, round(new_start, 6))
    if new_end != end:
        ctx.note("clip", cid, "end", end, round(new_end, 6))
    clip["start"], clip["end"] = new_start, new_end
    if "time" in changed:
        ctx.warn(f"{label}: speed edits (time: mode / playbackRate / map) are not brought back; change the "
                 "clip's speed in Zenvi")

    # track
    try:
        layer = None
        if "layer" in changed:
            layer = _whole(cur.get("layer"))
            if layer is None or layer <= 0:
                raise _Refused(f"{label}: layer must be a positive track number, got {cur.get('layer')!r}")
        elif "track" in changed:
            index = _whole(cur.get("track"))
            if index is None or not 0 <= index < len(tracks):
                raise _Refused(f"{label}: track {cur.get('track')!r} is not one of the {len(tracks)} tracks "
                               "(0 = bottom); set 'layer' to put it on a new track")
            layer = int(tracks[index].get("layer") or 0)
        if layer is not None and layer != int(clip.get("layer") or 0):
            ctx.ensure_layer(layer)
            ctx.note("clip", cid, "layer", clip.get("layer"), layer)
            clip["layer"] = layer
    except _Refused as exc:
        ctx.warn(f"{exc}; kept layer {clip.get('layer')}")

    # keyframes (clip frames: X = frame + 1, whatever the trim)
    if "keyframes" in changed:
        raw_base, raw_cur = base.get("keyframes"), cur.get("keyframes")
        base_kf: Dict[str, Any] = raw_base if isinstance(raw_base, dict) else {}
        cur_kf: Optional[Dict[str, Any]] = raw_cur if isinstance(raw_cur, dict) else None
        if cur_kf is None:
            ctx.warn(f"{label}: keyframes must be an object of property -> keyframes; kept them")
        else:
            for key in sorted(set(base_kf) | set(cur_kf)):
                if base_kf.get(key) == cur_kf.get(key):
                    continue
                if key not in CLIP_KEYS:
                    ctx.warn(f"{label}: keyframes.{key} is not a property Zenvi exports; ignored")
                    continue
                try:
                    if key not in cur_kf:  # removed: the property's default
                        value = _constant(CLIP_KEYS[key])
                    else:
                        lo, hi = _range_for(key, ctx, clip)
                        value = keyframe_from_keys(cur_kf[key], 0, clip.get(key), lo, hi,
                                                   f"{label}: keyframes.{key}", base_keys=base_kf.get(key),
                                                   base_offset=0)
                except _Refused as exc:
                    ctx.warn(f"{exc}; kept the clip's {key}")
                    continue
                ctx.note("clip", cid, "keyframes." + key, None, cur_kf.get(key))
                clip[key] = value

    for readable, key in (("hasAudio", "has_audio"), ("hasVideo", "has_video")):
        if readable in changed:
            value = cur.get(readable)
            if isinstance(value, bool):
                ctx.note("clip", cid, key, base.get(readable), value)
                clip[key] = _flag(value)
            else:
                ctx.warn(f"{label}: {readable} must be true or false; kept it")
    for readable, key, hi in (("scaleMode", "scale", 3), ("gravity", "gravity", 8)):
        if readable in changed:
            choice = _whole(cur.get(readable))
            if choice is not None and 0 <= choice <= hi:
                ctx.note("clip", cid, key, clip.get(key), choice)
                clip[key] = choice
            else:
                ctx.warn(f"{label}: {readable} must be 0..{hi}, got {cur.get(readable)!r}; kept {clip.get(key)}")
    if "blendMode" in changed:
        value = cur.get("blendMode")
        if value is None or value == "normal":
            code: Optional[int] = 0
        else:
            code = _BLEND_CODES.get(str(value))
        if code is None:
            ctx.warn(f"{label}: blendMode must be one of {', '.join(sorted(_BLEND_CODES))} or null; kept it")
        else:
            ctx.note("clip", cid, "composite", clip.get("composite"), code)
            clip["composite"] = code


def _similarity(a: dict, b: dict) -> int:
    return sum(1 for k in set(a) | set(b) if k != "id" and a.get(k) == b.get(k))


# ---------------------------------------------------------------------------
# Markers, transitions, tracks
# ---------------------------------------------------------------------------

def _marker_time(entry: dict, base: Optional[dict], ctx: _Context) -> Optional[float]:
    """The marker's new time: ``time`` wins over ``frame`` when both changed. None = unchanged/invalid."""
    base = base or {}
    if "time" in entry and entry.get("time") != base.get("time"):
        seconds = _number(entry.get("time"))
        if seconds is not None and seconds >= 0:
            return ctx.snap(seconds)
        raise _Refused(f"marker {entry.get('name') or entry.get('id')!r}: time must be 0 or more seconds")
    if "frame" in entry and entry.get("frame") != base.get("frame"):
        frame = _whole(entry.get("frame"))
        if frame is not None and frame >= 0:
            return frame / ctx.fps_f
        raise _Refused(f"marker {entry.get('name') or entry.get('id')!r}: frame must be a frame number 0 or more")
    return None


def _apply_markers(project: dict, baseline: dict, timeline: dict, ctx: _Context) -> None:
    current = timeline.get("markers")
    if current == baseline.get("markers"):
        return
    if not isinstance(current, list):
        ctx.warn("markers must be a list; the markers were kept as exported")
        return
    base_by_id = {str(m.get("id")): m for m in baseline.get("markers") or [] if isinstance(m, dict) and m.get("id")}
    originals = {str(m.get("id")): m for m in project.get("markers") or [] if isinstance(m, dict)}
    seen: Set[str] = set()
    added: List[dict] = []
    for entry in current:
        if not isinstance(entry, dict):
            ctx.warn("a marker entry is not an object; ignored")
            continue
        mid = str(entry.get("id") or "")
        name = entry.get("name")
        try:
            if mid in base_by_id and mid not in seen and mid in originals:
                seen.add(mid)
                marker, base = originals[mid], base_by_id[mid]
                when = _marker_time(entry, base, ctx)
                if when is not None:
                    ctx.note("marker", mid, "position", marker.get("position"), round(when, 6))
                    marker["position"] = when
                if entry.get("name") != base.get("name"):
                    if not isinstance(name, str):
                        raise _Refused(f"marker {mid}: name must be text")
                    ctx.note("marker", mid, "name", marker.get("name"), name)
                    marker["name"] = name
                if entry.get("color") != base.get("color"):
                    color = str(entry.get("color") or "").lower()
                    if color not in MARKER_COLORS:
                        raise _Refused(f"marker {mid}: color must be one of {', '.join(MARKER_COLORS)}")
                    ctx.note("marker", mid, "color", base.get("color"), color)
                    marker["vector"], marker["icon"] = color, f"{color}.png"
            else:
                when = _marker_time(entry, None, ctx)
                if when is None:
                    raise _Refused("a new marker needs a time (seconds) or a frame")
                color = str(entry.get("color") or "blue").lower()
                if color not in MARKER_COLORS:
                    raise _Refused(f"new marker: color must be one of {', '.join(MARKER_COLORS)}")
                marker = {"id": ctx.new_id(mid), "position": when, "icon": f"{color}.png", "vector": color}
                if isinstance(name, str) and name:
                    marker["name"] = name
                added.append(marker)
                ctx.note("marker", marker["id"], "added", None, round(when, 6))
        except _Refused as exc:
            ctx.warn(f"{exc}; ignored that edit")
    deleted = [mid for mid in base_by_id if mid not in seen]
    if deleted:
        project["markers"] = [m for m in project.get("markers") or []
                              if not (isinstance(m, dict) and str(m.get("id")) in deleted)]
        for mid in deleted:
            ctx.note("marker", mid, "deleted", None, None)
    project.setdefault("markers", []).extend(added)


def _apply_transitions(project: dict, baseline: dict, timeline: dict, ctx: _Context, tracks: List[dict]) -> None:
    current = timeline.get("transitions")
    if current == baseline.get("transitions"):
        return
    if not isinstance(current, list):
        ctx.warn("transitions must be a list; the transitions were kept as exported")
        return
    from classes import transition_ops
    base_by_id = {str(t.get("id")): t for t in baseline.get("transitions") or [] if isinstance(t, dict)}
    originals = {str(t.get("id")): t for t in project.get("effects") or [] if isinstance(t, dict)}
    seen: Set[str] = set()
    rate = ctx.fps_f
    for entry in current:
        if not isinstance(entry, dict):
            continue
        tid = str(entry.get("id") or "")
        if tid not in base_by_id or tid not in originals or tid in seen:
            ctx.warn(f"transition {entry.get('title') or tid!r} is new; new transitions are not brought back "
                     "(add them in Zenvi)")
            continue
        seen.add(tid)
        base, data = base_by_id[tid], originals[tid]
        changed = _changed(base, entry)
        label = f"transition {entry.get('title') or tid!r}"
        for key in sorted(changed & {"opacity", "kind", "mask"}):
            ctx.warn(f"{label}: '{key}' follows from the transition's curves and mask; change it in Zenvi")
        try:
            if "from" in changed:
                frame = _whole(entry.get("from"))
                if frame is None or frame < 0:
                    raise _Refused(f"{label}: from must be a frame number 0 or more")
                ctx.note("transition", tid, "position", data.get("position"), round(frame / rate, 6))
                data["position"] = frame / rate
            if "durationInFrames" in changed:
                frames = _whole(entry.get("durationInFrames"))
                if frames is None or frames < 1:
                    raise _Refused(f"{label}: durationInFrames must be 1 or more")
                old = float(data.get("end") or 0.0) - float(data.get("start") or 0.0)
                new = frames / rate
                transition_ops.rescale_curves(data, old, new, rate)
                data["end"] = float(data.get("start") or 0.0) + new
                ctx.note("transition", tid, "duration", round(old, 6), round(new, 6))
            layer = None
            wanted_layer, wanted_track = _whole(entry.get("layer")), _whole(entry.get("track"))
            if "layer" in changed and wanted_layer is not None and wanted_layer > 0:
                layer = wanted_layer
            elif "track" in changed and wanted_track is not None and 0 <= wanted_track < len(tracks):
                layer = int(tracks[wanted_track].get("layer") or 0)
            elif changed & {"layer", "track"}:
                raise _Refused(f"{label}: its track/layer is not valid")
            if layer is not None and layer != int(data.get("layer") or 0):
                ctx.ensure_layer(layer)
                ctx.note("transition", tid, "layer", data.get("layer"), layer)
                data["layer"] = layer
            if "title" in changed and isinstance(entry.get("title"), str):
                ctx.note("transition", tid, "title", data.get("title"), entry["title"])
                data["title"] = entry["title"]
        except _Refused as exc:
            ctx.warn(f"{exc}; kept the rest of it")
    deleted = [tid for tid in base_by_id if tid not in seen]
    if deleted:
        project["effects"] = [t for t in project.get("effects") or []
                              if not (isinstance(t, dict) and str(t.get("id")) in deleted)]
        for tid in deleted:
            ctx.note("transition", tid, "deleted", None, None)


def _apply_tracks(project: dict, baseline: dict, timeline: dict, ctx: _Context) -> List[dict]:
    """Track names/locks; returns the track list clips' ``track`` indexes refer to."""
    current = timeline.get("tracks")
    base_tracks = [t for t in baseline.get("tracks") or [] if isinstance(t, dict)]
    if not isinstance(current, list):
        return base_tracks
    tracks = [t for t in current if isinstance(t, dict) and _whole(t.get("layer")) is not None]
    if current == baseline.get("tracks"):
        return tracks
    base_by_layer = {int(t.get("layer") or 0): t for t in base_tracks}
    layers = {int(ly.get("number") or 0): ly for ly in project.get("layers") or [] if isinstance(ly, dict)}
    for t in tracks:
        number = _whole(t.get("layer")) or 0
        base = base_by_layer.get(number)
        if base is None:
            if number not in layers:
                ctx.ensure_layer(number, str(t.get("name") or ""), bool(t.get("locked")))
                ctx.note("track", str(number), "added", None, t.get("name"))
            continue
        layer = layers.get(number)
        if layer is None:
            continue
        if t.get("name") != base.get("name") and isinstance(t.get("name"), str):
            default_name = f"Track {int(base.get('index') or 0) + 1}"
            label = "" if t["name"] == default_name else t["name"]
            ctx.note("track", str(number), "label", layer.get("label"), label)
            layer["label"] = label
        if t.get("locked") != base.get("locked") and isinstance(t.get("locked"), bool):
            ctx.note("track", str(number), "lock", layer.get("lock"), t["locked"])
            layer["lock"] = t["locked"]
    return tracks


# ---------------------------------------------------------------------------
# The whole timeline
# ---------------------------------------------------------------------------

def apply_edits(project: dict, timeline: dict) -> Tuple[dict, List[str], List[dict]]:
    """(*project* with the readable timeline's edits applied, warnings, applied edits as dicts).

    *project* is the original (``zenvi.project``, media paths already remapped);
    it is not modified. Without a ``zenvi.baseline`` (an export older than this
    feature) nothing can be compared and the warnings say so.
    """
    baseline = (timeline.get("zenvi") or {}).get("baseline")
    out = copy.deepcopy(project)
    if not isinstance(baseline, dict):
        return out, ["src/zenvi/timeline.json was edited, but this export has no baseline to compare with; the "
                     "timeline was restored as exported"], []
    ctx = _Context(out, timeline)
    for key, why in (("composition", "the composition's size, fps and length follow from the Zenvi project"),
                     ("background", "Zenvi projects have no background colour setting"),
                     ("media", "media entries follow from the project's files")):
        if timeline.get(key) != baseline.get(key):
            ctx.warn(f"'{key}' was edited but not brought back: {why}")
    tracks = _apply_tracks(out, baseline, timeline, ctx)

    current = timeline.get("clips")
    if current != baseline.get("clips"):
        if not isinstance(current, list):
            ctx.warn("clips must be a list; the clips were restored as exported")
        else:
            base_by_id = {str(c.get("id")): c for c in baseline.get("clips") or [] if isinstance(c, dict)}
            originals = {str(c.get("id")): c for c in out.get("clips") or [] if isinstance(c, dict)}
            seen: Set[str] = set()
            duplicates: List[dict] = []
            for entry in current:
                if not isinstance(entry, dict):
                    ctx.warn("a clip entry is not an object; ignored")
                    continue
                cid = str(entry.get("id") or "")
                if cid in base_by_id and cid in originals and cid not in seen:
                    seen.add(cid)
                    apply_clip(originals[cid], base_by_id[cid], entry, ctx, tracks=tracks)
                    continue
                candidates = [b for b in base_by_id.values() if str(b.get("fileId")) == str(entry.get("fileId"))
                              and str(b.get("id")) in originals]
                if not candidates:
                    ctx.warn(f"{_label(entry)} is new and its media {entry.get('fileId')!r} is not an exported "
                             "file; new media is not brought back (import it in Zenvi)")
                    continue
                template = max(candidates, key=lambda b: _similarity(b, entry))
                clip = copy.deepcopy(originals[str(template["id"])])
                clip["id"] = ctx.new_id(cid)
                for effect in clip.get("effects") or []:
                    if isinstance(effect, dict):
                        effect["id"] = ctx.new_id()
                apply_clip(clip, template, entry, ctx, tracks=tracks)
                ctx.note("clip", clip["id"], "added", str(template["id"]), None)
                duplicates.append(clip)
            deleted = [cid for cid in base_by_id if cid not in seen and cid in originals]
            if deleted:
                out["clips"] = [c for c in out.get("clips") or []
                                if not (isinstance(c, dict) and str(c.get("id")) in deleted)]
                for cid in deleted:
                    ctx.note("clip", cid, "deleted", None, None)
            out.setdefault("clips", []).extend(duplicates)
            for c in out.get("clips") or []:  # a clip that followed a deleted one no longer points at it
                if isinstance(c, dict) and str(c.get("parentObjectId") or "") in deleted:
                    ctx.note("clip", str(c.get("id")), "parentObjectId", c["parentObjectId"], "")
                    ctx.warn(f"{_label(c)} followed a clip that was deleted; it no longer follows it")
                    c["parentObjectId"] = ""

    _apply_transitions(out, baseline, timeline, ctx, tracks)
    _apply_markers(out, baseline, timeline, ctx)
    if ctx.new_layers:
        out.setdefault("layers", []).extend(ctx.new_layers[n] for n in sorted(ctx.new_layers))
    return out, ctx.warnings, [e.as_dict() for e in ctx.applied]


__all__ = ["apply_edits", "apply_clip", "keyframe_from_keys", "Edit", "RANGES", "MARKER_COLORS"]
