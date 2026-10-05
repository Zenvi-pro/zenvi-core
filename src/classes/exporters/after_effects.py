"""Zenvi -> After Effects: an ExtendScript (.jsx) that rebuilds a Zenvi timeline as an AE composition.

``build_ae_script(snapshot, media_map=..., title_assets=..., mask_assets=..., options=...)``
turns a :class:`~classes.handoff.timeline_view.TimelineSnapshot` into one
script (SPEC section 3.7): first line ``// Zenvi -> After Effects export v1``
(with the arrow), ``#target aftereffects``, ES3 only, one IIFE, one undo
group ("Import Zenvi project"), a project folder ``Zenvi - <project>`` (em
dash) with ``Footage`` (and ``Titles``) subfolders, every media file
imported once (a placeholder when it is missing, never an abort), the comp
with the project's size, pixel aspect, frame rate, duration and a black
background, opened in the viewer, and a JSON summary string as the
script's value. ``var ZENVI_EXPORT`` carries the metadata.

It is pure: the caller (``classes.handoff.after_effects_export``) collects
media, renders title PNGs and wipe masks, and passes where each file is.

Mapping (libopenshot 1.0 semantics; see ``classes.handoff.keyframes`` and
``classes.handoff.transform`` for the ports they rest on):

* **Layer order.** Tracks bottom to top, clips by position: AE's
  ``layers.add`` puts each new layer on top, so a higher Zenvi track (and on
  one track the later, overlapping clip that libopenshot draws last) ends up
  higher. Track names, numbers and locks go to the layer comment / lock.
* **Timing.** libopenshot shows timeline frame f of a clip at clip frame
  ``n = f - (round(position*fps)+1) + round(start*fps)+1`` and, with a ``time``
  curve of 2+ points, source frame ``round(time(n))`` (Timeline.cpp, Clip.cpp
  ``apply_timemapping``; the engine's ``frameWindow`` / ``remappedClipFrame``).
  ``start``/``end`` are clip-frame times, not source times, once a time curve
  exists, so the source time is always read from the curve: no curve gives
  ``startTime = inPoint - round(start*fps)/fps``; a straight forward curve
  over the visible frames gives ``stretch = 100/k`` with ``startTime`` placing
  the right source frame at the in point; anything else (reverse, freeze,
  ramps, a hold after the curve's last point, loops) becomes Time Remap keys
  from the curve, eased like the curve or sampled per frame. After Effects
  shows the source frame whose span holds the source time (a floor), so with
  a curve the source times are shifted into the frame libopenshot rounds to
  (``_timing``); every frame is checked against ``ClipView.source_frame_at``.
* **Transform.** For every frame the clip shows, ``transform.geometry`` (the
  port of ``Clip::get_transform``) gives the canvas placement; AE Anchor
  Point = origin x source size, Position = the origin's canvas point, Scale =
  canvas px per source px x 100, Rotation, Opacity = alpha x 100. A
  property that is an affine function of one curve keeps that curve's keys
  and eases (cut exactly at the in/out points); anything else -- a position
  that also depends on scale (non-centre gravity, origin not 0.5, crop mode),
  an opacity multiplied by a fade -- is sampled per frame. Position becomes
  one spatial property when x and y move in step, else X/Y Position with
  separate dimensions. Shear has no layer equivalent and warns.
* **Audio.** Audio Levels = 20*log10(volume) dB on both channels (floor
  -96 dB, ceiling +12 dB), keyed per frame when the volume or an audio
  crossfade moves it; ``has_audio`` off -> audio switch off; ``has_video`` off
  -> video switch off; audio-only files are audio layers.
* **Titles.** Simple templates become a precomp of text and shape layers
  (``after_effects_titles``); the others a transparent PNG rendered with
  QSvgRenderer like libopenshot does. The choice per title is in the report.
* **Effects.** ``after_effects_effects`` (mapping table there); unmapped
  effects warn and are listed in the layer comment.
* **Transitions.** A Mask transition affects the top clip of its track at
  each frame (the overlapping clip that starts last; Timeline::apply_effects
  via the engine's ``planFrame``). A uniform mask (Zenvi's Fade) multiplies
  that clip's opacity by the Mask kernel's own per-frame result
  (``Mask.cpp``: brightness shift, contrast steepening); other masks become a
  Gradient Wipe on that clip with the mask image as a hidden guide layer,
  Transition Completion from brightness and Softness from contrast
  (approximate; ``_wipe_completion``). ``fade_audio_hint`` transitions add
  libopenshot's equal-power audio crossfade to the levels.
* **Markers** -> comp markers (name as comment, colour as label).
* **Linked clips** (``zenvi_link``) import their rendered media with the
  link in the layer comment; an After Effects link whose comp is in the open
  project uses that comp instead.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from classes.exporters import after_effects_keys as K
from classes.exporters.after_effects_effects import EffectContext, decode_size, keys_spec, map_effects
from classes.exporters.after_effects_js import es3_problems, js_str, js_value
from classes.exporters.after_effects_runtime import RUNTIME
from classes.exporters.after_effects_titles import RectItem, TextItem, TitleLayout
from classes.handoff.keyframes import Curve, float32
from classes.handoff.transform import geometry

HEADER = "// Zenvi → After Effects export v1"
FORMAT_VERSION = 1
UNDO_NAME = "Import Zenvi project"
FOLDER_PREFIX = "Zenvi — "

DB_FLOOR = -96.0
DB_CEILING = 12.0

# Tolerances: eased keys must match the exact values within these at every frame;
# sampled keys keep linear interpolation within them.
TOL_PX = 0.02
TOL_PERCENT = 0.01
TOL_DEGREES = 0.005
TOL_OPACITY = 0.05
TOL_DB = 0.1
TOL_SOURCE_FRAMES = 0.001  # Time Remap / stretch, in source frames

# Zenvi marker colours -> AE label indices (After Effects default label colours).
MARKER_LABELS = {"red": 1, "yellow": 2, "aqua": 3, "pink": 4, "lavender": 5, "peach": 6, "seafoam": 7,
                 "blue": 8, "green": 9, "purple": 10, "orange": 11, "brown": 12, "fuchsia": 13, "magenta": 13,
                 "cyan": 14, "sandstone": 15, "darkgreen": 16, "gray": 0, "grey": 0, "white": 0}
# One label colour per track (cycled), so tracks stay recognisable in the layer list.
TRACK_LABELS = (9, 8, 10, 11, 2, 14, 4, 3, 13, 6)
# libopenshot clip ``composite`` (QPainter::CompositionMode) -> AE BlendingMode.
BLEND_MODES = {12: "ADD", 13: "MULTIPLY", 14: "SCREEN", 15: "OVERLAY", 16: "DARKEN", 17: "LIGHTEN",
               18: "COLOR_DODGE", 19: "COLOR_BURN", 20: "HARD_LIGHT", 21: "SOFT_LIGHT", 22: "DIFFERENCE",
               23: "EXCLUSION"}
JUSTIFY = {"start": "LEFT_JUSTIFY", "middle": "CENTER_JUSTIFY", "end": "RIGHT_JUSTIFY"}


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MediaRef:
    """Where the script finds one file: ``rel`` (from the script's folder) first, then ``abs``."""

    abs: str
    rel: Optional[str] = None
    missing: bool = False


@dataclass(frozen=True)
class TitleAsset:
    """How a title file is exported: ``native`` (a layout) or ``png`` (an image of ``width`` x ``height``)."""

    mode: str
    reason: str = ""
    layout: Optional[TitleLayout] = None
    image: Optional[MediaRef] = None
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class MaskAsset:
    """A transition's mask image: ``uniform`` (one grey level), ``image`` (a file to wipe with) or ``missing``."""

    kind: str
    gray: int = 0
    alpha: int = 255
    image: Optional[MediaRef] = None
    width: int = 0
    height: int = 0


@dataclass
class AeExportOptions:
    include_audio: bool = True
    interactive: bool = True      # the script shows a summary alert when someone runs it by hand
    generator: str = ""           # e.g. "Zenvi 1.0.188"
    created: str = ""             # ISO time (empty in tests and golden files)


@dataclass
class AeExport:
    jsx: str
    warnings: List[str]
    stats: Dict[str, Any]
    titles: List[Dict[str, str]] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict, repr=False)   # the script's DATA literal


class AeExportError(ValueError):
    """The timeline cannot be exported (e.g. no clips); the message says why."""


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class _Ctx:
    def __init__(self, snapshot, media_map, title_assets, mask_assets, options: AeExportOptions):
        self.snapshot = snapshot
        self.fps: Fraction = snapshot.fps
        self.fps_f = float(snapshot.fps)
        self.width = int(snapshot.width)
        self.height = int(snapshot.height)
        self.media_map: Mapping[str, MediaRef] = media_map or {}
        self.title_assets: Mapping[str, TitleAsset] = title_assets or {}
        self.mask_assets: Mapping[str, MaskAsset] = mask_assets or {}
        self.options = options
        self.warnings: List[str] = []
        self._warned: set = set()
        self.footage: Dict[str, dict] = {}
        self.titles: Dict[str, dict] = {}
        self.guides: Dict[str, dict] = {}
        self.title_report: Dict[str, Dict[str, str]] = {}
        self.stats: Dict[str, Any] = {"layers": 0, "footage": 0, "titles_native": 0, "titles_png": 0,
                                      "effects_mapped": 0, "effects_unmapped": 0, "transitions_fade": 0,
                                      "transitions_wipe": 0, "markers": 0, "sampled_properties": 0,
                                      "eased_properties": 0, "skipped_audio": 0, "missing_media": 0}

    def warn(self, text: str) -> None:
        if text not in self._warned:
            self._warned.add(text)
            self.warnings.append(text)

    def snap(self, t: float) -> float:
        """*t* on the frame grid (libopenshot rounds positions half away from zero)."""
        frames = math.floor(float(t) * self.fps_f + 0.5)
        return float(Fraction(frames) / self.fps)

    def frame_index(self, t: float) -> int:
        return int(math.floor(float(t) * self.fps_f + 0.5))


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _rgb(c: Optional[Sequence[float]]) -> Optional[List[float]]:
    return None if c is None else [round(float(v), 6) for v in c[:3]]


def _rgba(c: Optional[Sequence[float]]) -> Optional[List[float]]:
    return None if c is None else [round(float(v), 6) for v in c[:3]] + [1.0]


# ---------------------------------------------------------------------------
# Footage
# ---------------------------------------------------------------------------

def _file_kind(f) -> str:
    if f.media_type == "audio" or (not f.has_video and f.has_audio):
        return "audio"
    if f.is_image_sequence:
        return "sequence"
    if f.is_still or f.is_title:
        return "still"
    return "video"


def _link_comment(link: Mapping[str, Any]) -> str:
    from classes.handoff.linked_media import kind_label, link_props
    raw = link.get("source")
    source: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    kind = str(link.get("kind") or "")
    parts = [f"Zenvi linked clip: {kind_label(kind) if kind else 'source'}"]
    if source.get("composition"):
        parts.append(f"composition {source.get('composition')}")
    if source.get("project_dir"):
        parts.append(f"project {source.get('project_dir')}")
    if source.get("aep"):
        parts.append(f"After Effects project {source.get('aep')}")
    try:
        props = link_props(dict(link))
    except Exception:
        props = {}
    if props:
        import json
        text = json.dumps(props, sort_keys=True, ensure_ascii=False)
        parts.append("props " + (text if len(text) <= 300 else text[:297] + "..."))
    return "; ".join(parts)


def _ae_comp_link(link: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if not isinstance(link, Mapping) or link.get("kind") != "aftereffects":
        return None
    raw = link.get("source")
    source: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    key = source.get("composition_key")
    try:
        key = int(key) if key is not None else None
    except (TypeError, ValueError):
        key = None
    if not key:
        return None
    return {"id": key, "name": str(source.get("composition") or ""), "aep": str(source.get("aep") or "")}


def _footage_entry(ctx: _Ctx, key: str, name: str, ref: MediaRef, kind: str, *, width: int, height: int,
                   fps: float, duration: float, comment: str = "", title: bool = False,
                   aecomp: Optional[dict] = None) -> str:
    if key in ctx.footage:
        return ctx.footage[key]["id"]
    fid = "F%d" % (len(ctx.footage) + 1)
    entry: Dict[str, Any] = {
        "id": fid, "name": name, "kind": kind, "abs": ref.abs, "rel": ref.rel,
        # importPlaceholder needs a size of at least 4 px, a frame rate and a duration
        "w": max(4, int(width or ctx.width)), "h": max(4, int(height or ctx.height)),
        "fps": round(_clamp(float(fps or ctx.fps_f), 1.0, 99.0), 6),
        "dur": round(_clamp(float(duration or 10.0), 1.0 / ctx.fps_f, 10800.0), 6),
    }
    if kind == "sequence":
        entry["seq"] = True
    if comment:
        entry["comment"] = comment
    if title:
        entry["title"] = True
    if aecomp:
        entry["aecomp"] = aecomp
    if ref.missing:
        entry["missing"] = True
        ctx.stats["missing_media"] += 1
        ctx.warn(f"Missing media: {name} ({ref.abs}); the script uses a placeholder you can relink in After "
                 "Effects (File > Replace Footage > File)")
    ctx.footage[key] = entry
    return fid


def _media_source(ctx: _Ctx, f) -> Optional[Tuple[str, int, int]]:
    """(footage id, AE source width, height) of a media file, or None when it has no media reference."""
    ref = ctx.media_map.get(f.id)
    if ref is None:
        if not f.path:
            return None
        ref = MediaRef(abs=f.path)
    link = f.zenvi_link if f.is_linked else None
    comment = _link_comment(link) if link else ""
    fid = _footage_entry(ctx, "file:" + f.id, f.name or os.path.basename(f.path), ref, _file_kind(f),
                         width=f.width, height=f.height, fps=float(f.fps) if f.fps else ctx.fps_f,
                         duration=f.duration, comment=comment, aecomp=_ae_comp_link(link))
    return fid, int(f.width or ctx.width), int(f.height or ctx.height)


def _title_source(ctx: _Ctx, f, longest: float) -> Optional[Tuple[str, int, int]]:
    asset = ctx.title_assets.get(f.id)
    name = os.path.splitext(f.name or os.path.basename(f.path))[0] or "Title"
    if asset is None:
        ctx.warn(f"Title {name}: no rendered image or layout was prepared; the title is skipped")
        return None
    if asset.mode == "native" and asset.layout is not None:
        key = "title:" + f.id
        if key not in ctx.titles:
            ctx.titles[key] = _title_comp(ctx, "T%d" % (len(ctx.titles) + 1), name, asset.layout, longest, f)
            ctx.stats["titles_native"] += 1
            ctx.title_report[f.id] = {"title": name, "mode": "native",
                                      "detail": _describe_layout(asset.layout)}
        else:
            ctx.titles[key]["dur"] = max(ctx.titles[key]["dur"], round(longest, 6))
        tw = int(round(asset.layout.width))
        th = int(round(asset.layout.height))
        return ctx.titles[key]["id"], tw, th
    if asset.image is None:
        ctx.warn(f"Title {name}: no image was rendered; the title is skipped")
        return None
    if f.id not in ctx.title_report:
        ctx.stats["titles_png"] += 1
        ctx.title_report[f.id] = {"title": name, "mode": "png",
                                  "detail": asset.reason or "rendered as a transparent PNG"}
    fid = _footage_entry(ctx, "titlepng:" + f.id, name + ".png", asset.image, "still", width=asset.width,
                         height=asset.height, fps=ctx.fps_f, duration=longest, title=True,
                         comment=f"Zenvi title {name} rendered as an image: {asset.reason or 'a complex template'}")
    return fid, int(asset.width or f.width or ctx.width), int(asset.height or f.height or ctx.height)


def _describe_layout(layout: TitleLayout) -> str:
    from classes.exporters.after_effects_titles import describe
    return "; ".join([describe(layout)] + list(layout.notes))


def _title_comp(ctx: _Ctx, tid: str, name: str, layout: TitleLayout, duration: float, f) -> dict:
    items: List[dict] = []
    for item in layout.items:
        if isinstance(item, RectItem):
            items.append({
                "type": "rect", "name": item.name or "Rectangle", "w": item.width, "h": item.height,
                "cx": item.x + item.width / 2.0, "cy": item.y + item.height / 2.0, "round": item.rx,
                "fill": _rgba(item.fill), "fo": item.fill_opacity * 100.0,
                "stroke": _rgba(item.stroke), "sw": item.stroke_width, "so": item.stroke_opacity * 100.0,
                "op": item.opacity * 100.0,
            })
        elif isinstance(item, TextItem):
            items.append({
                "type": "text", "name": item.name, "text": item.text, "x": item.x, "y": item.y,
                "size": item.size, "family": item.family or "Arial", "bold": item.bold, "italic": item.italic,
                "just": JUSTIFY.get(item.anchor, "LEFT_JUSTIFY"), "fill": _rgb(item.fill),
                "stroke": _rgb(item.stroke), "sw": item.stroke_width, "track": item.tracking,
                "op": item.opacity * 100.0,
            })
    return {"id": tid, "name": name, "w": max(4, int(round(layout.width))), "h": max(4, int(round(layout.height))),
            "fps": round(ctx.fps_f, 6), "dur": round(_clamp(duration, 1.0 / ctx.fps_f, 10800.0), 6),
            "comment": f"Zenvi title {name} ({f.path})", "items": items}


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def mask_factor(brightness: float, contrast: float, gray: int = 0, alpha: int = 255, invert: bool = False) -> float:
    """The opacity libopenshot's Mask leaves on a pixel of grey *gray* (Mask.cpp:77-100, float32 math)."""
    cf = float32(20.0 / max(0.5, float32(20.0 - float32(contrast))))
    shift = math.trunc(255.0 * brightness)
    adjusted = int(_clamp(math.trunc(float32(cf * (gray + shift - 128) + 128)), 0, 255))
    diff = int(_clamp(alpha - adjusted, 0, 255))
    if invert:
        diff = 255 - diff
    return diff / 255.0


def _wipe_completion(brightness: float, contrast: float) -> Tuple[float, float]:
    """(Transition Completion %, Transition Softness %) for a Gradient Wipe standing in for a Mask.

    libopenshot reveals a mask pixel of luminance L at brightness b with a
    linear ramp ``cf*(255*(L+b) - 128) + 128`` of width 1/cf (cf from
    contrast), so the layer is fully hidden at ``b >= h`` and fully shown at
    ``b <= -h`` with ``h = 0.5 + 0.5/cf``. The wipe sweeps its completion
    linearly over that range with a softness of the ramp's width. AE's
    exact softness curve is not documented, so this is approximate.
    """
    cf = float32(20.0 / max(0.5, float32(20.0 - float32(contrast))))
    h = 0.5 + 0.5 / cf
    completion = _clamp(50.0 * (1.0 + brightness / h), 0.0, 100.0)
    softness = _clamp(100.0 / cf, 0.0, 100.0)
    return completion, softness


@dataclass
class _ClipTransitions:
    fade: Dict[int, float] = field(default_factory=dict)              # frame index -> opacity factor
    gain: Dict[int, float] = field(default_factory=dict)              # frame index -> audio gain
    wipes: Dict[Tuple[str, bool], Dict[int, Tuple[float, float]]] = field(default_factory=dict)


def _plan_transitions(ctx: _Ctx) -> Dict[str, _ClipTransitions]:
    plan: Dict[str, _ClipTransitions] = {}
    for track in ctx.snapshot.tracks:
        if not track.transitions:
            continue
        windows = [(c, ctx.frame_index(c.timeline_in), ctx.frame_index(c.timeline_out)) for c in track.clips]
        for tv in track.transitions:
            k0, k1 = ctx.frame_index(tv.position), ctx.frame_index(tv.end)
            if k1 <= k0:
                continue
            asset = ctx.mask_assets.get(tv.mask_path)
            data = tv.data or {}
            if data.get("replace_image") in (True, 1, "1", "true"):
                ctx.warn(f"Transition {tv.title or tv.id} shows its mask image (Replace Image); not exported")
                continue
            if asset is None or asset.kind == "missing":
                ctx.warn(f"Transition {tv.title or tv.id}: its wipe image is missing "
                         f"({tv.mask_path or 'no image'}); the transition is skipped")
                continue
            invert = data.get("mask_invert") in (True, 1, "1", "true")
            if asset.kind == "uniform":
                ctx.stats["transitions_fade"] += 1
            else:
                ctx.stats["transitions_wipe"] += 1
            for k in range(k0, k1):
                t = k / ctx.fps_f
                top = None
                for clip, c0, c1 in windows:
                    if c0 <= k < c1 and (top is None or c0 > top[1]):
                        top = (clip, c0)
                if top is None:
                    continue
                entry = plan.setdefault(top[0].id, _ClipTransitions())
                b = K.exact_value(tv.brightness, t)
                c = K.exact_value(tv.contrast, t)
                if asset.kind == "uniform":
                    entry.fade[k] = entry.fade.get(k, 1.0) * mask_factor(b, c, asset.gray, asset.alpha, invert)
                else:
                    entry.wipes.setdefault((tv.mask_path, invert), {})[k] = _wipe_completion(b, c)
            if data.get("fade_audio_hint") in (True, 1, "1", "true"):
                _audio_crossfade(ctx, plan, windows, k0, k1)
    return plan


def _audio_crossfade(ctx: _Ctx, plan: Dict[str, _ClipTransitions], windows, k0: int, k1: int) -> None:
    """libopenshot's equal-power gains for the clips under a ``fade_audio_hint`` Mask (ResolveTransitionAudioGains)."""
    audible = [(c, c0, c1) for c, c0, c1 in windows
               if c0 < k1 and c1 > k0 and c.file is not None and (c.file.has_audio or c.file.media_type == "audio")
               and c.has_audio is not False]
    if not audible or len(audible) > 2:
        return
    ms, me = k0 + 1, k1   # 1-based first / last frame of the mask
    for clip, c0, c1 in audible:
        fades_in = abs(ms - (c0 + 1)) <= abs(me - c1)
        entry = plan.setdefault(clip.id, _ClipTransitions())
        for k in range(max(k0, c0), min(k1, c1)):
            s = _clamp((k - ms) / float(me - ms), 0.0, 1.0) if me > ms else 1.0
            gain = math.sin(s * math.pi / 2.0) if fades_in else math.cos(s * math.pi / 2.0)
            entry.gain[k] = entry.gain.get(k, 1.0) * gain


def _guide_for(ctx: _Ctx, mask_path: str) -> Optional[str]:
    asset = ctx.mask_assets.get(mask_path)
    if asset is None or asset.image is None:
        return None
    if mask_path in ctx.guides:
        return ctx.guides[mask_path]["key"]
    name = os.path.splitext(os.path.basename(mask_path))[0] or "wipe"
    fid = _footage_entry(ctx, "mask:" + mask_path, "Wipe " + name + os.path.splitext(asset.image.abs)[1],
                         asset.image, "still", width=asset.width, height=asset.height, fps=ctx.fps_f,
                         duration=max(1.0, ctx.snapshot.duration))
    key = "G%d" % (len(ctx.guides) + 1)
    ctx.guides[mask_path] = {"key": key, "src": fid, "name": "Wipe image: " + name,
                             "comment": f"Zenvi transition wipe image {mask_path}; read by Gradient Wipe effects"}
    return key


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

def _time_curve(clip) -> Optional[Curve]:
    """The clip's ``time`` curve when libopenshot remaps with it (``ClipView.time``), else None."""
    return clip.time


def _ae_frames(values: Sequence[float], fps: float) -> List[int]:
    """1-based source frames After Effects shows for these source times: the frame whose span holds each."""
    return [int(math.floor(v * fps + 1e-7)) + 1 for v in values]


def _as_stretch(keys: K.Track, whole: bool, offset: float, t_in: float, frames: Sequence[float],
                shown: Sequence[int], fps: float) -> Optional[Dict[str, Any]]:
    """startTime + stretch when the source times are one straight forward line that shows *shown*."""
    slopes = [(b[0] - a[0]) / (t1 - t0) for a, b, t0, t1 in zip(keys.values, keys.values[1:], keys.times,
                                                                   keys.times[1:]) if t1 > t0]
    if not (keys.kinds and all(k == K.LINEAR for k in keys.kinds) and slopes and slopes[0] > 1e-6
            and all(abs(s - slopes[0]) <= 1e-6 * max(1.0, abs(slopes[0])) for s in slopes)):
        return None
    speed = 1.0 if abs(slopes[0] - 1.0) < 1e-9 else slopes[0]
    source_in = K.track_value(keys, t_in)[0]
    if whole:
        # whole source frames (normal speed, 2x, 3x...): a frame-aligned start, like any other layer
        source_in -= offset / fps
    start = t_in - source_in / speed
    if _ae_frames([(t - start) * speed for t in frames], fps) != list(shown):
        return None
    return {"start": start, "stretch": 100 if speed == 1.0 else 100.0 / speed}


def _timing(ctx: _Ctx, clip, file, t_in: float, t_out: float, frames: List[float]) -> Dict[str, Any]:
    """startTime / stretch / Time Remap keys showing the right source frame at every frame.

    libopenshot shows source frame ``max(1, round(time(n)))``
    (``ClipView.source_frame_at``); After Effects shows the frame whose span
    holds the layer's source time, and keeps Time Remap values in single
    precision. The source times follow the curve shifted by the middle of
    the range of shifts that shows libopenshot's frame at every frame. That
    range always holds half a frame (``floor(x - 1/2) = round(x) - 1``); its
    middle keeps the values as far from frame boundaries as the curve allows
    (half a frame for whole frames, a quarter after 0.5x's .5 ties). The
    result is checked frame by frame; keys that would still show another
    frame (sampled keys near a tie) become one key per change of frame, in
    the middle of its frame.
    """
    tc = _time_curve(clip)
    if tc is None:
        tau0 = math.floor(clip.start * ctx.fps_f + 0.5) / ctx.fps_f
        return {"start": t_in - tau0, "stretch": 100}
    fps = ctx.fps_f
    raw = [max(1.0, K.exact_value(tc, t)) for t in frames]
    shown = [clip.source_frame_at(t) for t in frames]
    limit = float(file.duration) if file is not None and file.duration else None
    if limit:
        last = max(1, int(math.floor(limit * fps + 0.5)))  # the media's last frame; libopenshot holds it
        if max(shown) > last:
            media = file.name if file is not None else "its media"
            ctx.warn(f"{clip.title}: its speed curve reaches past the end of {media}; the last frame is held")
        raw = [min(x, float(last)) for x in raw]
        shown = [min(s, last) for s in shown]
    tol = [TOL_SOURCE_FRAMES / fps]
    # AE shows frame s for the curve value x shifted by d (frames) when s - x <= d < s - x + 1
    gaps = [s - x for s, x in zip(shown, raw)]
    offset = (max(gaps) + min(gaps) + 1.0) / 2.0
    samples = [(x - 1.0 + offset) / fps for x in raw]
    keys: Any = K.build_property(frames, [K.Dimension(samples, tc)], t_in=t_in, t_out=t_out, tol=tol).keys
    if isinstance(keys, K.Track):
        stretch = _as_stretch(keys, all(abs(g) < 1e-6 for g in gaps), offset, t_in, frames, shown, fps)
        if stretch is not None:
            return stretch
        values = [K.track_value(keys, t)[0] for t in frames]
    else:
        values = [keys[0]] * len(frames)
    wrong = [i for i, (a, s) in enumerate(zip(_ae_frames(values, fps), shown)) if a != s]
    if wrong:
        # one key per change of frame, each in the middle of its frame
        steps = K.sampled_track(frames, [((s - 0.5) / fps,) for s in shown], tol)
        pinned = None
        if isinstance(keys, K.Track) and not keys.sampled:
            # libopenshot rounds its own (approximate) bezier value, which can sit across a .5 from the exact
            # curve: key just those frames inside the frame Zenvi shows and keep the ease everywhere else
            cuts = sorted({frames[k] for i in wrong for k in (i - 1, i, i + 1) if 0 <= k < len(frames)})
            cut = K.build_property(frames, [K.Dimension(samples, tc)], t_in=t_in, t_out=t_out, tol=tol,
                                   cuts=cuts).keys
            if isinstance(cut, K.Track) and not cut.sampled:
                at = {round(t, 9): j for j, t in enumerate(cut.times)}
                pinned = K.pin(cut, {at[round(frames[i], 9)]: ((shown[i] - 0.5) / fps,) for i in wrong})
                if _ae_frames([K.track_value(pinned, t)[0] for t in frames], fps) != shown:
                    pinned = None
        keys = pinned if pinned is not None and len(pinned.times) <= len(steps.times) else steps
    if isinstance(keys, K.Track):
        if keys.sampled:
            ctx.stats["sampled_properties"] += 1
        if len(keys.times) > 1 and any(abs(v[0] - keys.values[0][0]) > tol[0] for v in keys.values):
            return {"start": t_in, "stretch": 100, "remap": keys_spec(keys)}
        keys = keys.values[0]
    # one source frame held for the whole clip
    return {"start": t_in, "stretch": 100, "remap": {"t": [t_in], "v": [keys[0]], "i": []}}


_TRANSFORM_CURVES = ("alpha", "location_x", "location_y", "scale_x", "scale_y", "rotation", "origin_x", "origin_y",
                     "shear_x", "shear_y", "margin")


def _note_track(ctx: _Ctx, built: K.BuildResult) -> None:
    if isinstance(built.keys, K.Track):
        if built.keys.sampled:
            ctx.stats["sampled_properties"] += 1
        else:
            ctx.stats["eased_properties"] += 1


def _transform(ctx: _Ctx, clip, file, src_w: int, src_h: int, t_in: float, t_out: float, frames: List[float],
               fade: Dict[int, float]) -> Dict[str, Any]:
    curves = {k: clip.curve(k) for k in _TRANSFORM_CURVES}
    animated = any(K.is_animated_in(curves[k], t_in, t_out) for k in _TRANSFORM_CURVES)
    eval_frames = frames if (animated or fade) else frames[:1]
    file_w = float(file.width) if file is not None and file.width else float(src_w or ctx.width)
    file_h = float(file.height) if file is not None and file.height else float(src_h or ctx.height)
    fx = file_w / float(src_w) if src_w else 1.0
    fy = file_h / float(src_h) if src_h else 1.0
    # libopenshot decodes a SCALE_NONE source at its size times the clip's largest scale (transform.delivered_size)
    max_sx = max((p.value for p in curves["scale_x"].points), default=1.0)
    max_sy = max((p.value for p in curves["scale_y"].points), default=1.0)
    still = bool(file is not None and (file.is_still or file.is_title))
    cols: Dict[str, List[float]] = {k: [] for k in ("ax", "ay", "px", "py", "sx", "sy", "rot", "op")}
    sheared = False
    for t in eval_frames:
        v = {k: K.exact_value(curves[k], t) for k in _TRANSFORM_CURVES}
        g = geometry(file_w, file_h, ctx.width, ctx.height, scale_mode=clip.scale_mode, gravity=clip.gravity,
                     scale_x=v["scale_x"], scale_y=v["scale_y"], location_x=v["location_x"],
                     location_y=v["location_y"], rotation=v["rotation"], origin_x=v["origin_x"],
                     origin_y=v["origin_y"], shear_x=0.0, shear_y=0.0, alpha=v["alpha"], margin=v["margin"],
                     max_scale_x=max_sx, max_scale_y=max_sy, still=still)
        if abs(v["shear_x"]) > 1e-6 or abs(v["shear_y"]) > 1e-6:
            sheared = True
        cols["ax"].append(v["origin_x"] * src_w)
        cols["ay"].append(v["origin_y"] * src_h)
        cols["px"].append(g.anchor_x)
        cols["py"].append(g.anchor_y)
        cols["sx"].append(g.scale_x * fx * 100.0)
        cols["sy"].append(g.scale_y * fy * 100.0)
        cols["rot"].append(g.rotation)
        factor = fade.get(ctx.frame_index(t), 1.0) if fade else 1.0
        cols["op"].append(_clamp(v["alpha"], 0.0, 1.0) * 100.0 * factor)
    if sheared:
        ctx.warn(f"{clip.title}: shear has no After Effects layer equivalent; the layer is not sheared")
    if eval_frames is not frames:
        return {"anchor": [round(cols["ax"][0], 6), round(cols["ay"][0], 6)],
                "pos": [round(cols["px"][0], 6), round(cols["py"][0], 6)],
                "scale": [round(cols["sx"][0], 6), round(cols["sy"][0], 6)],
                "rot": round(cols["rot"][0], 6), "op": round(cols["op"][0], 6)}

    def prop(dims, tol, spatial=False) -> K.BuildResult:
        built = K.build_property(frames, dims, t_in=t_in, t_out=t_out, tol=tol, spatial=spatial)
        _note_track(ctx, built)
        return built

    anchor = prop([K.Dimension(cols["ax"], curves["origin_x"]), K.Dimension(cols["ay"], curves["origin_y"])],
                  [TOL_PX, TOL_PX], spatial=True)
    if isinstance(anchor.keys, K.Track) and not (anchor.keys.spatial or anchor.keys.sampled):
        anchor = K.BuildResult(K.sampled_track(frames, list(zip(cols["ax"], cols["ay"])), [TOL_PX, TOL_PX]))
    position = prop([K.Dimension(cols["px"], curves["location_x"]), K.Dimension(cols["py"], curves["location_y"])],
                    [TOL_PX, TOL_PX], spatial=True)
    if isinstance(position.keys, K.Track) and not (position.keys.spatial or position.keys.sampled):
        pos_spec: Any = {"sep": 1,
                         "x": keys_spec(prop([K.Dimension(cols["px"], curves["location_x"])], [TOL_PX]).keys),
                         "y": keys_spec(prop([K.Dimension(cols["py"], curves["location_y"])], [TOL_PX]).keys)}
    else:
        pos_spec = keys_spec(position.keys)
    scale = prop([K.Dimension(cols["sx"], curves["scale_x"]), K.Dimension(cols["sy"], curves["scale_y"])],
                 [TOL_PERCENT, TOL_PERCENT])
    rotation = prop([K.Dimension(cols["rot"], curves["rotation"])], [TOL_DEGREES])
    opacity = prop([K.Dimension(cols["op"], curves["alpha"])], [TOL_OPACITY])
    return {"anchor": keys_spec(anchor.keys), "pos": pos_spec, "scale": keys_spec(scale.keys),
            "rot": keys_spec(rotation.keys), "op": keys_spec(opacity.keys)}


def _levels(ctx: _Ctx, clip, t_in: float, t_out: float, frames: List[float], gain: Dict[int, float]) -> Any:
    volume = clip.curve("volume")
    animated = K.is_animated_in(volume, t_in, t_out) or bool(gain)
    use = frames if animated else frames[:1]
    db = []
    clipped = False
    for t in use:
        v = max(0.0, K.exact_value(volume, t)) * (gain.get(ctx.frame_index(t), 1.0) if gain else 1.0)
        level = 20.0 * math.log10(v) if v > 1e-12 else DB_FLOOR
        if level > DB_CEILING:
            clipped = True
        db.append(_clamp(level, DB_FLOOR, DB_CEILING))
    if clipped:
        ctx.warn(f"{clip.title}: volume above +{DB_CEILING:g} dB was limited to +{DB_CEILING:g} dB")
    if not animated:
        return None if abs(db[0]) < 1e-9 else [round(db[0], 6), round(db[0], 6)]
    built = K.build_property(frames, [K.Dimension(db, None), K.Dimension(list(db), None)], t_in=t_in, t_out=t_out,
                             tol=[TOL_DB, TOL_DB])
    _note_track(ctx, built)
    keys = built.keys
    if not isinstance(keys, K.Track) and abs(keys[0]) < 1e-9:
        return None
    return keys_spec(keys)


def _wipe_effects(ctx: _Ctx, clip, frames: List[float], t_in: float, t_out: float,
                  wipes: Dict[Tuple[str, bool], Dict[int, Tuple[float, float]]]) -> List[dict]:
    out = []
    for (mask_path, invert), per_frame in wipes.items():
        guide = _guide_for(ctx, mask_path)
        if guide is None:
            continue
        completion, softness = [], []
        run_soft = [s for _, s in per_frame.values()]
        default_soft = run_soft[0] if run_soft else 0.0
        for t in frames:
            c, s = per_frame.get(ctx.frame_index(t), (0.0, default_soft))
            completion.append(c)
            softness.append(s)
        c_keys = K.build_property(frames, [K.Dimension(completion, None)], t_in=t_in, t_out=t_out, tol=[0.05]).keys
        s_keys = K.build_property(frames, [K.Dimension(softness, None)], t_in=t_in, t_out=t_out, tol=[0.05]).keys
        name = os.path.splitext(os.path.basename(mask_path))[0]
        out.append({"label": "Wipe transition", "name": "Wipe " + name, "opts": [{"match": "ADBE Gradient Wipe", "params": [
            {"ids": ["ADBE Gradient Wipe-0001", "Transition Completion"], "val": keys_spec(c_keys)},
            {"ids": ["ADBE Gradient Wipe-0002", "Transition Softness"], "val": keys_spec(s_keys)},
            {"ids": ["ADBE Gradient Wipe-0003", "Gradient Layer"], "val": {"layer": guide}},
            {"ids": ["ADBE Gradient Wipe-0004", "Gradient Placement"], "val": 3},
            {"ids": ["ADBE Gradient Wipe-0005", "Invert Gradient"], "val": 0 if invert else 1},
        ]}]})
    return out


def _effects(ctx: _Ctx, clip, file, src_w: int, src_h: int, t_in: float, t_out: float,
             frames: List[float]) -> Tuple[List[dict], List[dict], List[str]]:
    if not clip.effects:
        return [], [], []
    dw, dh = decode_size(clip, file, ctx.width, ctx.height) if file is not None else (src_w, src_h)
    natural_w = float(file.width) if file is not None and file.width else float(src_w)
    ectx = EffectContext(frames=frames, t_in=t_in, t_out=t_out, decode_scale=(dw / natural_w) if natural_w else 1.0,
                         decode_width=dw, decode_height=dh, source_width=float(src_w), source_height=float(src_h),
                         clip_name=clip.title or clip.id, warn=ctx.warn)
    mapped = map_effects(clip, ectx)
    fx = []
    for e in mapped.effects:
        e = dict(e)
        e["opts"] = e.pop("try")
        fx.append(e)
    ctx.stats["effects_mapped"] += len(fx)
    unmapped = [n for n in mapped.unmapped if n]
    if unmapped:
        ctx.stats["effects_unmapped"] += len(unmapped)
        ctx.warn(f"{clip.title}: no After Effects equivalent for {', '.join(sorted(set(unmapped)))}; "
                 "listed in the layer comment")
    return fx, mapped.masks, unmapped


def _clip_layer(ctx: _Ctx, clip, track, plan: Dict[str, _ClipTransitions], index: int) -> Optional[dict]:
    f = clip.file
    title = clip.title or (f.name if f else clip.id)
    if f is None:
        ctx.warn(f"{title}: the clip has no media file; skipped")
        return None
    t_in, t_out = ctx.snap(clip.timeline_in), ctx.snap(clip.timeline_out)
    if t_out <= t_in:
        return None
    kind = _file_kind(f)
    if kind == "audio" and not ctx.options.include_audio:
        ctx.stats["skipped_audio"] += 1
        return None
    source = _title_source(ctx, f, t_out - t_in) if f.is_title else _media_source(ctx, f)
    if source is None:
        return None
    src_id, src_w, src_h = source
    layer_kind = "audio" if kind == "audio" else ("still" if kind == "still" else "av")
    frames = K.frame_times(t_in, t_out, ctx.fps_f)
    tr = plan.get(clip.id, _ClipTransitions())
    layer: Dict[str, Any] = {"key": "L%d" % index, "name": title, "src": src_id, "kind": layer_kind,
                             "inp": t_in, "outp": t_out}
    if layer_kind == "still":
        layer.update({"start": t_in, "stretch": 100})
    else:
        layer.update(_timing(ctx, clip, f, t_in, t_out, frames))
    if layer_kind != "audio":
        layer["tf"] = _transform(ctx, clip, f, src_w, src_h, t_in, t_out, frames, tr.fade)
    has_audio = kind == "audio" or bool(f.has_audio)
    if has_audio and layer_kind != "still":
        if clip.has_audio is False or not ctx.options.include_audio:
            layer["audio"] = False
        else:
            levels = _levels(ctx, clip, t_in, t_out, frames, tr.gain)
            if levels is not None:
                layer["levels"] = levels
    if clip.has_video is False and layer_kind != "audio":
        layer["video"] = False
    fx, masks, unmapped = ([], [], []) if layer_kind == "audio" else _effects(ctx, clip, f, src_w, src_h, t_in,
                                                                              t_out, frames)
    fx.extend(_wipe_effects(ctx, clip, frames, t_in, t_out, tr.wipes))
    if fx:
        layer["fx"] = fx
    if masks:
        layer["masks"] = masks
    composite = clip.data.get("composite") if clip.data is not None else None
    try:
        composite = int(composite) if composite is not None else 0
    except (TypeError, ValueError):
        composite = 0
    if composite in BLEND_MODES:
        layer["blend"] = BLEND_MODES[composite]
    elif composite:
        ctx.warn(f"{title}: blend mode {composite} has no After Effects equivalent; Normal is used")
    if clip.parent_id:
        ctx.warn(f"{title}: it follows another clip or a tracked object in Zenvi (parent); parenting is not "
                 "exported, so it does not move with it")
    corner = clip.curve("corner_radius")
    if layer_kind != "audio" and (corner.is_animated or abs(corner.first_value) > 1e-9):
        ctx.warn(f"{title}: rounded corners are not exported")
    layer["label"] = TRACK_LABELS[track.index % len(TRACK_LABELS)]
    comment = [f"Zenvi clip {clip.id} on track {track.index + 1} ({track.name})"]
    if f.is_linked and f.zenvi_link is not None:
        comment.append(_link_comment(f.zenvi_link))
    if unmapped:
        comment.append("Zenvi effects not exported: " + ", ".join(unmapped))
    layer["comment"] = "\n".join(comment)
    if track.locked:
        layer["lock"] = True
    return layer


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------

def _markers(ctx: _Ctx) -> List[dict]:
    merged: Dict[float, dict] = {}
    for m in ctx.snapshot.markers:
        t = ctx.snap(m.time)
        entry = merged.setdefault(round(t, 9), {"t": t, "c": "", "lb": MARKER_LABELS.get(m.color.replace(" ", ""), 0)})
        entry["c"] = (entry["c"] + " / " if entry["c"] else "") + (m.name or "Marker")
    ctx.stats["markers"] = len(merged)
    return [merged[k] for k in sorted(merged)]


def _comp_duration(ctx: _Ctx) -> float:
    end = max(ctx.snapshot.duration, 1.0 / ctx.fps_f)
    if end > 10800.0:
        ctx.warn("The timeline is longer than After Effects' 3 hour comp limit; the comp is 3 hours long")
        end = 10800.0
    return ctx.snap(end) if ctx.snap(end) >= end - 1e-9 else ctx.snap(end) + 1.0 / ctx.fps_f


def build_ae_script(snapshot, *, media_map: Optional[Mapping[str, MediaRef]] = None,
                    title_assets: Optional[Mapping[str, TitleAsset]] = None,
                    mask_assets: Optional[Mapping[str, MaskAsset]] = None,
                    options: Optional[AeExportOptions] = None) -> AeExport:
    """The After Effects script for *snapshot* (see the module docstring for the mapping)."""
    options = options or AeExportOptions()
    if not snapshot.clips:
        raise AeExportError("the timeline has no clips to export")
    ctx = _Ctx(snapshot, media_map, title_assets, mask_assets, options)
    plan = _plan_transitions(ctx)
    layers: List[dict] = []
    for track in snapshot.tracks:
        for clip in track.clips:
            layer = _clip_layer(ctx, clip, track, plan, len(layers) + 1)
            if layer is not None:
                layers.append(layer)
    if not layers:
        raise AeExportError("no clip on the timeline has media After Effects can show")
    if ctx.stats["skipped_audio"]:
        ctx.warn(f"{ctx.stats['skipped_audio']} audio-only clip(s) left out (Include audio is off)")
    ctx.stats["layers"] = len(layers)
    ctx.stats["footage"] = len(ctx.footage)
    name = snapshot.name or "Untitled"
    duration = _comp_duration(ctx)
    comp = {"name": name, "w": max(4, min(30000, ctx.width)), "h": max(4, min(30000, ctx.height)),
            "par": round(_clamp(float(snapshot.pixel_aspect or 1), 0.01, 100.0), 6),
            "fps": round(_clamp(ctx.fps_f, 1.0, 999.0), 9), "dur": round(duration, 9), "bg": [0, 0, 0]}
    markers = _markers(ctx)
    for t in ctx.titles.values():
        t["dur"] = round(max(t["dur"], duration), 6)
    generator = options.generator or "Zenvi"
    source = snapshot.project_path or ""
    comment = (f"Built by {generator} from {source or name}. " +
               (f"{len(ctx.warnings)} note(s) from the export; see README.txt next to the script."
                if ctx.warnings else "Nothing was left out."))
    data = {
        "folder": FOLDER_PREFIX + name,
        "comp": comp,
        "footage": list(ctx.footage.values()),
        "titles": list(ctx.titles.values()),
        "guides": list(ctx.guides.values()),
        "layers": layers,
        "markers": markers,
        "comment": comment,
        "notes": list(ctx.warnings),
        "title_folder": bool(ctx.titles) or any(e.get("title") for e in ctx.footage.values()),
    }
    meta: Dict[str, Any] = {"version": FORMAT_VERSION, "project": name, "project_path": source,
                            "generator": generator, "fps": {"num": snapshot.fps.numerator,
                                                            "den": snapshot.fps.denominator},
                            "width": ctx.width, "height": ctx.height, "interactive": bool(options.interactive)}
    if options.created:
        meta["created"] = options.created
    jsx = "\n".join([
        HEADER,
        "#target aftereffects",
        "// Built by " + _ascii(generator) + " from " + _ascii(source or name) + ".",
        "// Run it in After Effects with File > Scripts > Run Script File..., or from Zenvi with",
        "// File > Send To > After Effects (needs the Zenvi Link panel). It builds one undo step:",
        "// Edit > Undo \"" + UNDO_NAME + "\" removes everything it made.",
        "// ES3 (ExtendScript); everything below is plain ASCII.",
        "",
        "(function () {",
        "    var ZENVI_EXPORT = " + js_value(meta, indent=4) + ";",
        "",
        "    var DATA = " + js_value(data, indent=4) + ";",
        RUNTIME.rstrip(),
        "",
        "    return zenviBuild(ZENVI_EXPORT, DATA);",
        "}());",
        "",
    ])
    problems = es3_problems(jsx)
    if problems:  # a bug in this module, never the user's data: fail loudly
        raise AssertionError("the After Effects script is not ES3-clean: " + "; ".join(problems[:5]))
    titles = [ctx.title_report[k] for k in ctx.title_report]
    return AeExport(jsx=jsx, warnings=list(ctx.warnings), stats=dict(ctx.stats), titles=titles, data=data)


def _ascii(text: str) -> str:
    """Comment-safe ASCII (the script is ASCII after its first line)."""
    out = js_str(text)[1:-1]
    return out.replace("*/", "* /")


__all__ = ["build_ae_script", "AeExport", "AeExportOptions", "AeExportError", "MediaRef", "TitleAsset", "MaskAsset",
           "mask_factor", "HEADER", "UNDO_NAME", "FOLDER_PREFIX", "FORMAT_VERSION"]
