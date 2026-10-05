"""A Zenvi timeline as a HyperFrames project (``export_to_hyperframes_tool``, File > Export Project).

The project ``npx hyperframes preview | lint | render`` accept, laid out like
``hyperframes init`` makes one (``hyperframes.json``, ``meta.json``,
``package.json`` scripts pinned to the CLI version Zenvi verified), plus:

* ``index.html``: one root composition (``data-composition-id="zenvi-timeline"``,
  the project's size, fps and length). Every clip is a primitive --
  ``<video>``, ``<img>`` (stills and SVG titles) or ``<audio>`` -- with
  ``data-start``, ``data-duration``, ``data-media-start``, ``data-track-index``
  (track 1 = 0, painted bottom-up with ``z-index``), ``data-volume`` /
  ``data-automation`` volume lanes, ``data-playback-rate`` for constant speed
  and ``data-zenvi-clip-id``.
* Geometry from :mod:`classes.handoff.transform` (libopenshot 1.0's
  ``Clip::get_transform``): the element box is the scale-mode size, and
  ``translate / rotate / skew / scale`` about the clip's origin put it where
  Zenvi draws it. A static clip is plain CSS.
* A GSAP timeline for keyframes: a transform channel that follows one Zenvi
  curve becomes one tween per keyframe segment, eased by ``zenviEase`` -- the
  bezier evaluation libopenshot uses, so renders match frame for frame;
  channels that mix curves (scale with an off-centre gravity, crop moves,
  shear) are sampled per frame (GSAP array keyframes).
* Transitions become opacity cross-fades (``fade`` exactly is approximated
  as linear; wipes say so in the warnings).
* ``<script type="application/json" id="zenvi-timeline">``: the Zenvi
  project itself (files, clips, effects, tracks, markers), so importing the
  folder back restores every clip natively and losslessly.
* Media copied (or linked, ``copy_media=False``) into ``assets/``.

GSAP is loaded from the jsDelivr CDN exactly like the ``hyperframes init``
template (GSAP's own licence; Zenvi never ships it). Everything blocks (disk):
take the snapshot on the GUI thread, export off it.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from classes.handoff import transform as tf
from classes.handoff.hyperframes import cli as hf_cli
from classes.handoff.hyperframes.parser import INDEX, TIMELINE_SCRIPT_ID, read_document
from classes.handoff.keyframes import Curve
from classes.handoff.linked_media import LinkError
from classes.handoff.timeline_view import ClipView, FileView, TimelineSnapshot, TransitionView

EXPORT_VERSION = 1
ROOT_ID = "zenvi-timeline"
GSAP_CDN = "https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"
ASSETS = "assets"
MAX_VOLUME = 3.98
PROJECT_KEYS = ("fps", "width", "height", "pixel_ratio", "display_ratio", "sample_rate", "channels",
                "channel_layout", "profile", "duration", "files", "clips", "effects", "layers", "markers",
                "settings", "id", "version")
ZENVI_EASE_JS = """function zenviEase(x1, y1, x2, y2, frames) {
        // libopenshot 1.0's cubic bezier (Keyframe::GetValue): bisect x to 0.01 frame, return y
        var tol = 0.01 / Math.max(frames || 1, 1e-9);
        return function (p) {
          if (p <= 0) return 0;
          if (p >= 1) return 1;
          var t = 0.5, step = 0.25, u = 0.5, x = 0;
          for (var i = 0; i < 100; i++) {
            u = 1 - t;
            x = 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t;
            if (Math.abs(p - x) < tol) break;
            if (x > p) t -= step; else t += step;
            step /= 2;
          }
          u = 1 - t;
          return 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t;
        };
      }"""


class ExportError(LinkError):
    """The timeline cannot be exported as asked; the message says why."""


@dataclass
class ExportResult:
    output_dir: str
    index: str
    files: List[str]
    assets: Dict[str, str]
    clips: int
    warnings: List[str] = field(default_factory=list)
    duration: float = 0.0
    replaced_changes: List[str] = field(default_factory=list)   # files edited since the last export, replaced


# ---------------------------------------------------------------------------
# Raw project copy (GUI thread) for the lossless JSON
# ---------------------------------------------------------------------------

def raw_project(project: Dict[str, Any]) -> dict:
    """The parts of a project dict a restore needs (no undo history, no waveform caches). Cheap: GUI ok."""
    out: Dict[str, Any] = {}
    for key in PROJECT_KEYS:
        if key in project:
            out[key] = _copy_without_waveforms(project[key])
    return out


def _copy_without_waveforms(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "ui" and isinstance(v, dict):
                v = {kk: vv for kk, vv in v.items() if kk != "audio_data"}
            out[k] = _copy_without_waveforms(v)
        return out
    if isinstance(value, list):
        return [_copy_without_waveforms(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(value: float, digits: int = 6) -> str:
    """A compact JS / CSS number."""
    if not math.isfinite(value):
        return "0"
    text = ("%." + str(digits) + "f") % value
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if text in ("-0", "") else text


def _seconds(value: float) -> str:
    return _num(round(value, 6), 6)


def _safe_name(text: str, fallback: str = "media") -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-._")
    return (base or fallback)[:80]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(text or "").lower()).strip("-")[:60] or "zenvi-export"


def _fps_attr(snapshot: TimelineSnapshot) -> str:
    fps = snapshot.fps
    return str(fps.numerator) if fps.denominator == 1 else "%d/%d" % (fps.numerator, fps.denominator)


def _is_previous_export(folder: str) -> bool:
    index = os.path.join(folder, INDEX)
    if not os.path.isfile(index):
        return False
    try:
        doc = read_document(folder, INDEX)
    except LinkError:
        return False
    return any(e.tag == "script" and e.id == TIMELINE_SCRIPT_ID for e in doc.iter())


# ---------------------------------------------------------------------------
# The export folder: what Zenvi wrote there (a manifest), and what changed since
# ---------------------------------------------------------------------------
#
# Every export writes ``.zenvi-export.json``: each file Zenvi put in the folder with how to tell whether it
# changed (index.html by structure -- HyperFrames Studio stamps data-hf-id attributes, which change nothing;
# small text files by sha256; media by size + mtime; links by target). Exporting again into that folder
# replaces exactly those files, and only when none of them was changed since (else ExportChanged, unless
# overwrite_changes); the user's own files stay. The manifest is the only list of files Zenvi deletes, and
# its paths must stay inside the folder (assets/ inside assets/, never through a linked assets/).

MANIFEST = ".zenvi-export.json"
MANIFEST_VERSION = 1


class ExportChanged(ExportError):
    """Files of an earlier Zenvi export in the folder were changed since; ``changed`` lists them."""

    def __init__(self, message: str, changed: Sequence[str]):
        super().__init__(message)
        self.changed = list(changed)


@dataclass
class OutputPlan:
    target: str
    previous: Optional[Dict[str, dict]]     # the earlier export's files (manifest), None for a new folder
    changed: List[str] = field(default_factory=list)


def _safe_rel(rel: Any) -> Optional[str]:
    """*rel* as a clean folder-relative path, or None when it is absolute or climbs out with ``..``."""
    text = str(rel or "").replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = [x for x in text.split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _inside(path: str, folder: str) -> bool:
    """Whether *path* (its own location: a link is not followed) is inside *folder* once resolved."""
    where = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    base = os.path.realpath(folder)
    return where.startswith(base.rstrip(os.sep) + os.sep)


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _record(path: str, rel: str) -> dict:
    """How the manifest remembers a file Zenvi wrote (to tell later whether it was changed)."""
    if os.path.islink(path):
        return {"link": os.readlink(path)}
    if rel == INDEX:
        from classes.handoff.hyperframes.provider import canonical_html_digest
        return {"html": canonical_html_digest(path)}
    if not rel.startswith(ASSETS + "/"):
        return {"sha256": _file_sha256(path)}
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _changed(path: str, rel: str, entry: Any) -> bool:
    if not os.path.lexists(path):
        return False  # gone: nothing of anyone's to lose
    if os.path.isdir(path) and not os.path.islink(path):
        return True
    try:
        return _record(path, rel) != entry
    except OSError:
        return True


def read_manifest(folder: str) -> Optional[dict]:
    path = os.path.join(folder, MANIFEST)
    if not os.path.isfile(path) or os.path.islink(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


def plan_output(output_dir: str, *, overwrite_changes: bool = False) -> OutputPlan:
    """Check the export folder: new, empty, or an earlier Zenvi export whose files are all unchanged.

    Raises ExportError for a busy folder that is no Zenvi export (or one whose record points outside it,
    or whose assets/ is a link), and ExportChanged when files of the earlier export were changed since --
    unless *overwrite_changes*.
    """
    if not str(output_dir or "").strip():
        raise ExportError("say where to write the HyperFrames project: output_dir (a new or empty folder)")
    path = os.path.abspath(os.path.expanduser(str(output_dir).strip()))
    if os.path.exists(path) and not os.path.isdir(path):
        raise ExportError(f"{path} is a file; pick a folder for the HyperFrames project")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise ExportError(f"the folder {parent} does not exist")
    if not os.path.isdir(path):
        return OutputPlan(path, None)
    path = os.path.realpath(path)
    manifest = read_manifest(path)
    visible = [n for n in os.listdir(path) if not n.startswith(".")]
    if manifest is None:
        if not visible:
            return OutputPlan(path, None)
        if _is_previous_export(path):
            raise ExportError(f"{path} holds a HyperFrames export from an earlier Zenvi build that kept no record "
                              "of its files, so Zenvi cannot tell its files from yours; export to a new folder")
        raise ExportError(f"{path} is not empty and is not an earlier Zenvi export; pick a new or empty folder "
                          "so nothing of yours is overwritten")
    if os.path.islink(os.path.join(path, ASSETS)):
        raise ExportError(f"{os.path.join(path, ASSETS)} is a link to another folder; Zenvi does not write into "
                          "or clean up a folder outside the export -- export to a new folder")
    files: Dict[str, dict] = {}
    for rel, entry in manifest["files"].items():
        safe = _safe_rel(rel)
        full = os.path.join(path, *safe.split("/")) if safe else ""
        if safe is None or safe == MANIFEST or not _inside(full, path) or (
                safe.startswith(ASSETS + "/") and not _inside(full, os.path.join(path, ASSETS))):
            raise ExportError(f"the export record in {path} lists {rel!r}, which is not a file inside the folder; "
                              "export to a new folder")
        files[safe] = entry if isinstance(entry, dict) else {}
    changed = sorted(rel for rel, entry in files.items() if _changed(os.path.join(path, *rel.split("/")), rel, entry))
    if changed and not overwrite_changes:
        shown = ", ".join(changed[:6]) + (" and %d more" % (len(changed) - 6) if len(changed) > 6 else "")
        raise ExportChanged(f"{shown} in {path} changed since Zenvi exported it (in HyperFrames?). Import the folder "
                            "first to bring those edits into Zenvi, or export to a new folder", changed)
    return OutputPlan(path, files, changed)


def check_output_dir(output_dir: str, *, overwrite_changes: bool = False) -> str:
    """The absolute export folder (see :func:`plan_output`)."""
    return plan_output(output_dir, overwrite_changes=overwrite_changes).target


# ---------------------------------------------------------------------------
# Channels: what each GSAP property does over a clip
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    """One GSAP property of a clip: a constant, exact eased segments, or per-frame samples."""

    prop: str
    initial: float
    segments: List[Tuple[float, float, float, float, str, Optional[Tuple[float, float, float, float]], float]] = \
        field(default_factory=list)   # (t0, t1, v0, v1, kind, bezier, frames); kind bezier | linear | hold
    samples: Optional[List[float]] = None   # values on every frame of the clip window

    @property
    def animated(self) -> bool:
        return bool(self.segments) or self.samples is not None


def _window(clip: ClipView, fps: float) -> Tuple[float, float, int]:
    """(first frame time, end time, frame count) of a clip on the timeline grid."""
    first = round(clip.timeline_in * fps) / fps
    count = max(1, int(round(clip.duration * fps)))
    return first, first + count / fps, count


def affine_channel(prop: str, curve: Curve, a: float, b: float, clip: ClipView, fps: float) -> Channel:
    """``a * curve + b`` over the clip: its segments where they touch the clip's window, else constant."""
    t_in, t_out, _n = _window(clip, fps)
    value_in = a * curve.value_at(t_in) + b
    if curve.is_constant:
        return Channel(prop, value_in)
    ch = Channel(prop, value_in)
    for seg in curve.segments():
        t0, t1 = seg.start.time, seg.end.time
        if t1 < t_in - 1e-9 or t0 > t_out + 1e-9 or t1 <= t0:
            continue
        v0 = a * seg.start.value + b
        v1 = a * seg.end.value + b
        frames = seg.end.frame - seg.start.frame
        if seg.easing == "hold":
            ch.segments.append((t0, t1, v0, v1, "hold", None, frames))
        elif seg.easing == "linear":
            ch.segments.append((t0, t1, v0, v1, "linear", None, frames))
        else:
            ch.segments.append((t0, t1, v0, v1, "bezier", seg.bezier, frames))
    if not ch.segments:
        return Channel(prop, value_in)
    first = curve.points[0]
    if first.time > t_in + 1e-9:  # libopenshot holds the first value before the first key
        ch.initial = a * first.value + b
    return ch


def sampled_channel(prop: str, fn: Callable[[float], float], clip: ClipView, fps: float) -> Channel:
    t_in, _t_out, n = _window(clip, fps)
    values = [fn(t_in + k / fps) for k in range(n)]
    if all(abs(v - values[0]) < 1e-9 for v in values):
        return Channel(prop, values[0])
    return Channel(prop, values[0], samples=values)


# ---------------------------------------------------------------------------
# Clip geometry
# ---------------------------------------------------------------------------

_GRAVITY_X = {tf.GRAVITY_TOP_LEFT: 0.0, tf.GRAVITY_LEFT: 0.0, tf.GRAVITY_BOTTOM_LEFT: 0.0,
              tf.GRAVITY_TOP: -0.5, tf.GRAVITY_CENTER: -0.5, tf.GRAVITY_BOTTOM: -0.5,
              tf.GRAVITY_TOP_RIGHT: -1.0, tf.GRAVITY_RIGHT: -1.0, tf.GRAVITY_BOTTOM_RIGHT: -1.0}
_GRAVITY_Y = {tf.GRAVITY_TOP_LEFT: 0.0, tf.GRAVITY_TOP: 0.0, tf.GRAVITY_TOP_RIGHT: 0.0,
              tf.GRAVITY_LEFT: -0.5, tf.GRAVITY_CENTER: -0.5, tf.GRAVITY_RIGHT: -0.5,
              tf.GRAVITY_BOTTOM_LEFT: -1.0, tf.GRAVITY_BOTTOM: -1.0, tf.GRAVITY_BOTTOM_RIGHT: -1.0}
_GRAVITY_FRAC = {0.0: 0.0, -0.5: 0.5, -1.0: 1.0}


@dataclass
class Layout:
    left: float
    top: float
    width: float
    height: float
    origin_x: float
    origin_y: float
    channels: Dict[str, Channel]
    warnings: List[str] = field(default_factory=list)


def clip_layout(clip: ClipView, canvas_w: int, canvas_h: int, fps: float,
                opacity_fn: Optional[Callable[[float], float]] = None) -> Layout:
    """The CSS box and GSAP channels that draw *clip* where libopenshot 1.0 draws it."""
    warnings: List[str] = []
    W, H = float(canvas_w), float(canvas_h)
    f = clip.file
    src_w = (f.width if f is not None and f.width else 0) or canvas_w
    src_h = (f.height if f is not None and f.height else 0) or canvas_h
    t_in, _t_out, _n = _window(clip, fps)
    curves = {k: clip.curve(k) for k in ("alpha", "location_x", "location_y", "scale_x", "scale_y", "rotation",
                                          "origin_x", "origin_y", "shear_x", "shear_y", "margin")}
    for key in ("origin_x", "origin_y", "margin"):
        if curves[key].is_animated:
            warnings.append(f"{clip.title!r}: its animated {key.replace('_', ' ')} is exported at its first value")
    margin = max(0.0, min(0.5, curves["margin"].value_at(t_in)))
    margin_px = margin * min(W, H)
    lw, lh = max(1.0, W - 2 * margin_px), max(1.0, H - 2 * margin_px)
    sx, sy = curves["scale_x"], curves["scale_y"]
    if clip.scale_mode == tf.SCALE_NONE:
        # libopenshot decodes a SCALE_NONE source at its largest scale first (transform.delivered_size)
        max_sx = max((p.value for p in sx.points), default=1.0)
        max_sy = max((p.value for p in sy.points), default=1.0)
        size_w, size_h = tf.delivered_size(int(round(src_w)), int(round(src_h)), max_sx, max_sy,
                                           still=bool(f is not None and f.is_still))
    else:
        size_w, size_h = tf.scaled_source_size(int(round(src_w)), int(round(src_h)), clip.scale_mode,
                                               math.trunc(lw), math.trunc(lh))
    size_w, size_h = float(size_w), float(size_h)
    ox, oy = curves["origin_x"].value_at(t_in), curves["origin_y"].value_at(t_in)
    gx1, gy1 = _GRAVITY_X.get(clip.gravity, -0.5), _GRAVITY_Y.get(clip.gravity, -0.5)
    gx0 = margin_px + _GRAVITY_FRAC[gx1] * lw
    gy0 = margin_px + _GRAVITY_FRAC[gy1] * lh
    left, top = gx0 + gx1 * size_w, gy0 + gy1 * size_h
    lx, ly = curves["location_x"], curves["location_y"]
    crop = clip.scale_mode == tf.SCALE_CROP

    def exact_geometry(t: float) -> tf.Geometry:
        return tf.clip_geometry(clip, t, canvas_w, canvas_h)

    channels: Dict[str, Channel] = {}
    for axis, (s_curve, l_curve, g1, o, size, extent, base) in {
            "x": (sx, lx, gx1, ox, size_w, W, left), "y": (sy, ly, gy1, oy, size_h, H, top)}.items():
        k = size * (g1 + o)
        if crop and (s_curve.is_animated or l_curve.is_animated):
            def fn(t, axis=axis, base=base, s_curve=s_curve, o=o, size=size):
                g = exact_geometry(t)
                pos = g.x if axis == "x" else g.y
                return pos - base - (1.0 - s_curve.value_at(t)) * o * size
            channels[axis] = sampled_channel(axis, fn, clip, fps)
        elif crop:
            g = exact_geometry(t_in)
            pos = g.x if axis == "x" else g.y
            channels[axis] = Channel(axis, pos - base - (1.0 - s_curve.value_at(t_in)) * o * size)
        elif s_curve.is_animated and abs(k) > 1e-12 and l_curve.is_animated:
            channels[axis] = sampled_channel(
                axis, lambda t, s=s_curve, lc=l_curve, k=k, e=extent: k * (s.value_at(t) - 1.0) + e * lc.value_at(t),
                clip, fps)
        elif s_curve.is_animated and abs(k) > 1e-12:
            channels[axis] = affine_channel(axis, s_curve, k, -k + extent * l_curve.value_at(t_in), clip, fps)
        else:
            channels[axis] = affine_channel(axis, l_curve, extent, k * (s_curve.value_at(t_in) - 1.0), clip, fps)
    channels["scaleX"] = affine_channel("scaleX", sx, 1.0, 0.0, clip, fps)
    channels["scaleY"] = affine_channel("scaleY", sy, 1.0, 0.0, clip, fps)
    channels["rotation"] = affine_channel("rotation", curves["rotation"], 1.0, 0.0, clip, fps)
    for key, prop in (("shear_x", "skewX"), ("shear_y", "skewY")):
        c = curves[key]
        if c.is_animated:
            channels[prop] = sampled_channel(prop, lambda t, c=c: math.degrees(math.atan(c.value_at(t))), clip, fps)
        else:
            channels[prop] = Channel(prop, math.degrees(math.atan(c.value_at(t_in))))
    alpha = curves["alpha"]
    if opacity_fn is not None:
        channels["opacity"] = sampled_channel("opacity", lambda t: max(0.0, min(1.0, opacity_fn(t))), clip, fps)
    else:
        channels["opacity"] = affine_channel("opacity", alpha, 1.0, 0.0, clip, fps)
    return Layout(left, top, size_w, size_h, ox, oy, channels, warnings)


# ---------------------------------------------------------------------------
# Transitions -> opacity and sound (libopenshot 1.0 Timeline + Mask, frame for frame)
# ---------------------------------------------------------------------------
#
# A transition is a Mask on its track. libopenshot applies it to the TOP clip only -- of the clips that
# cover the frame on that track, the one that starts last (Timeline::GetFrame, is_top_clip) -- and only on
# the frames the transition covers. With "fade audio" set (fade_audio_hint) it also fades the sound of
# the (at most two) clips it covers: equal power, the clip whose start is nearer the transition's start
# fading in (sin), the other out (cos) (Timeline::ResolveTransitionAudioGains).

UNIFORM_MASKS = {"fade.svg": (0, 255)}   # mask file -> (gray, alpha): the fade mask is solid black


def _frames(start: float, end: float, fps: float) -> Tuple[int, int]:
    """[first, last] 0-based timeline frames libopenshot gives something placed from *start* to *end*."""
    return int(round(start * fps)), int(round(end * fps)) - 1


def _covers(clip: ClipView, f: int, fps: float) -> bool:
    first, last = _frames(clip.timeline_in, clip.timeline_out, fps)
    return first <= f <= last


def top_clip(track_clips: Sequence[ClipView], f: int, fps: float) -> Optional[ClipView]:
    """The clip libopenshot applies a track's transitions to on frame *f*: the latest-starting one there."""
    top, top_start = None, None
    for c in track_clips:
        if _covers(c, f, fps):
            start = int(round(c.timeline_in * fps))
            if top_start is None or start > top_start:
                top, top_start = c, start
    return top


def mask_opacity(tr: TransitionView, f: int, fps: float) -> Optional[float]:
    """What a uniform mask (the fade) leaves of the top clip on timeline frame *f* (Mask::GetFrame); None
    for an image mask (a wipe), which has no single value."""
    uniform = UNIFORM_MASKS.get(os.path.basename(tr.mask_path or "").lower())
    if uniform is None:
        return None
    gray, a = uniform
    local = f - _frames(tr.position, tr.end, fps)[0] + 1        # the transition's own frame number
    brightness = tr.brightness.value_at_frame(local)
    contrast = tr.contrast.value_at_frame(local)
    factor = 20.0 / max(0.5, 20.0 - contrast)
    adjusted = max(0, min(255, int(factor * ((gray + int(255 * brightness)) - 128) + 128)))
    alpha = max(0, min(255, a - adjusted))
    if tr.data.get("mask_invert"):
        alpha = 255 - alpha
    return alpha / 255.0


def _wipe_opacity(tr: TransitionView, f: int, fps: float) -> float:
    """An image mask (wipe) as a cross-fade over the transition, in the mask's direction."""
    first, last = _frames(tr.position, tr.end, fps)
    p = 1.0 if last <= first else max(0.0, min(1.0, (f - first) / float(last - first)))
    return 1.0 - p if tr.reversed else p


def transition_opacity(clip: ClipView, track_clips: Sequence[ClipView], transitions: Sequence[TransitionView],
                       fps: float) -> Optional[Callable[[float], float]]:
    """factor(t) the track's transitions multiply *clip*'s opacity by (None when they never touch it)."""
    mine = []
    for tr in transitions:
        first, last = _frames(tr.position, tr.end, fps)
        frames = [f for f in range(max(first, _frames(clip.timeline_in, clip.timeline_out, fps)[0]),
                                   min(last, _frames(clip.timeline_in, clip.timeline_out, fps)[1]) + 1)
                  if top_clip(track_clips, f, fps) is clip]
        if frames:
            mine.append((tr, set(frames)))
    if not mine:
        return None

    def factor(t: float) -> float:
        f = int(round(t * fps))
        value = 1.0
        for tr, frames in mine:
            if f in frames:
                m = mask_opacity(tr, f, fps)
                value *= _wipe_opacity(tr, f, fps) if m is None else m
        return value
    return factor


def _audible(clip: ClipView) -> bool:
    return clip.has_audio is not False and clip.file is not None and bool(clip.file.has_audio)


def transition_gain(clip: ClipView, track_clips: Sequence[ClipView], transitions: Sequence[TransitionView],
                    fps: float) -> Optional[Callable[[float], float]]:
    """gain(t) the track's audio-fading transitions put on *clip*'s sound (None when they never do)."""
    fading = [tr for tr in transitions if tr.data.get("fade_audio_hint")]
    if not fading or not _audible(clip):
        return None

    def gain_at(f: int) -> float:
        active = [tr for tr in fading if _frames(tr.position, tr.end, fps)[0] <= f <= _frames(tr.position, tr.end,
                                                                                               fps)[1]]
        if len(active) != 1:
            return 1.0
        tr = active[0]
        audible = [c for c in track_clips if _audible(c) and _covers(c, f, fps)]
        if len(audible) > 2 or clip not in audible:
            return 1.0
        if len(audible) == 2:
            top_audio = max(audible, key=lambda c: int(round(c.timeline_in * fps)))
            is_top = top_clip(track_clips, f, fps) is clip
            if is_top != (clip is top_audio):
                return 1.0
        start_pos = int(round(tr.position * fps)) + 1
        end_pos = int(round(tr.end * fps))
        if end_pos <= start_pos:
            return 1.0
        clip_start = int(round(clip.timeline_in * fps)) + 1
        clip_end = int(round(clip.timeline_out * fps))
        fades_in = abs(start_pos - clip_start) <= abs(end_pos - clip_end)
        p = max(0.0, min(1.0, (f + 1 - start_pos) / float(end_pos - start_pos)))
        return math.sin(p * math.pi / 2) if fades_in else math.cos(p * math.pi / 2)

    first, last = _frames(clip.timeline_in, clip.timeline_out, fps)
    if all(abs(gain_at(f) - 1.0) < 1e-12 for f in range(first, last + 1)):
        return None
    return lambda t: gain_at(int(round(t * fps)))


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def volume_attrs(clip: ClipView, fps: float, gain: Optional[Callable[[float], float]] = None) -> Dict[str, str]:
    """``data-volume`` (constant) or a ``data-automation`` volume lane (clip-local seconds).

    *gain(t)*: a transition's audio fade on top of the clip's volume (sampled per frame).
    """
    vol = clip.curve("volume")
    t_in, _t_out, n = _window(clip, fps)
    if vol.is_constant and gain is None:
        value = max(0.0, min(MAX_VOLUME, vol.value_at(t_in)))
        return {} if abs(value - 1.0) < 1e-9 else {"data-volume": _num(value, 4)}
    exact_linear = gain is None and all(seg.easing == "linear" for seg in vol.segments())
    points: List[Tuple[float, float]] = []
    if exact_linear:
        for p in vol.points:
            local = p.time - t_in
            if -1e-9 <= local <= clip.duration + 1e-9:
                points.append((max(0.0, local), p.value))
        if not points or points[0][0] > 1e-9:
            points.insert(0, (0.0, vol.value_at(t_in)))
        end = clip.duration
        if points[-1][0] < end - 1e-9:
            points.append((end, vol.value_at(t_in + end)))
    else:
        g = gain or (lambda _t: 1.0)
        points = [(k / fps, vol.value_at(t_in + k / fps) * g(t_in + k / fps)) for k in range(n)]
    lane = {"version": 1, "lanes": [{"target": "volume", "points": [
        {"t": round(t, 6), "v": round(max(0.0, min(MAX_VOLUME, v)), 6)} for t, v in points]}]}
    return {"data-automation": json.dumps(lane, separators=(",", ":"))}


# ---------------------------------------------------------------------------
# index.html
# ---------------------------------------------------------------------------

@dataclass
class _Element:
    clip: ClipView
    tag: str
    element_id: str
    src: str
    attrs: Dict[str, str]
    css: Dict[str, str]
    script: List[str]


def _media_kind(clip: ClipView) -> Optional[str]:
    f = clip.file
    if f is None:
        return None
    if f.media_type == "audio" or clip.has_video is False or not f.has_video:
        return "audio"
    if f.is_still or f.is_title:
        return "img"
    return "video"


def _tween_lines(selector: str, ch: Channel, fps: float) -> List[str]:
    sel = json.dumps(selector)
    out = []
    for t0, t1, v0, v1, kind, bez, frames in ch.segments:
        if kind == "hold":
            out.append("tl.set(%s, { %s: %s }, %s);" % (sel, ch.prop, _num(v1), _seconds(t1)))
            continue
        if kind == "linear":
            ease = '"none"'
        else:
            x1, y1, x2, y2 = bez or (0.5, 0.0, 0.5, 1.0)
            ease = "zenviEase(%s, %s, %s, %s, %s)" % (_num(x1), _num(y1), _num(x2), _num(y2), _num(frames))
        out.append("tl.fromTo(%s, { %s: %s }, { %s: %s, duration: %s, ease: %s, immediateRender: false }, %s);" % (
            sel, ch.prop, _num(v0), ch.prop, _num(v1), _seconds(t1 - t0), ease, _seconds(t0)))
    return out


def _clip_element(clip: ClipView, kind: str, src: str, snapshot: TimelineSnapshot, track_index: int,
                  track_clips: Sequence[ClipView], transitions: Sequence[TransitionView],
                  warnings: List[str]) -> _Element:
    fps = float(snapshot.fps)
    eid = "c-" + _safe_name(clip.id, "clip")
    t_in, _t_out, n = _window(clip, fps)
    duration = n / fps
    attrs: Dict[str, str] = {"id": eid}
    if kind != "audio":
        attrs["class"] = "clip"
    attrs["src"] = src
    attrs["data-start"] = _seconds(t_in)
    attrs["data-duration"] = _seconds(duration)
    attrs["data-track-index"] = str(track_index)
    speed = clip.speed
    media_start = clip.source_in
    if kind in ("video", "audio"):
        if speed.kind == "constant" and not speed.reversed and speed.factor:
            attrs["data-playback-rate"] = _num(speed.factor, 6)
        elif speed.kind not in ("normal", "constant") or speed.reversed:
            warnings.append(f"{clip.title!r} plays {speed.kind}{' reversed' if speed.reversed else ''}; HyperFrames "
                            "gets it at normal speed from its in point")
        if media_start > 1e-6:
            attrs["data-media-start"] = _seconds(media_start)
    attrs["data-zenvi-clip-id"] = clip.id
    css: Dict[str, str] = {}
    script: List[str] = []
    if kind in ("video", "audio"):
        has_audio = clip.has_audio is not False and (clip.file is not None and clip.file.has_audio)
        volume = volume_attrs(clip, fps, transition_gain(clip, track_clips, transitions, fps)) if has_audio else {}
        rate = speed.factor if speed.kind == "constant" and not speed.reversed else None
        if has_audio and rate is not None and abs(rate - 1.0) > 1e-9:
            warnings.append(f"{clip.title!r} plays at {rate:g}x: Zenvi's speed change shifts its pitch, "
                            "HyperFrames keeps it")
        if kind == "video":
            if has_audio:
                attrs["data-has-audio"] = "true"
            else:
                attrs["muted"] = ""
            attrs["playsinline"] = ""
        attrs.update(volume)
    if kind != "audio":
        factor = transition_opacity(clip, track_clips, transitions, fps)
        alpha = clip.curve("alpha")
        layout = clip_layout(clip, snapshot.width, snapshot.height, fps,
                             (lambda t: alpha.value_at(t) * factor(t)) if factor is not None else None)
        warnings.extend(layout.warnings)
        css.update({"left": _num(layout.left, 3) + "px", "top": _num(layout.top, 3) + "px",
                    "width": _num(layout.width, 3) + "px", "height": _num(layout.height, 3) + "px",
                    "z-index": str(track_index + 1), "object-fit": "fill",
                    "transform-origin": "%s%% %s%%" % (_num(layout.origin_x * 100, 4), _num(layout.origin_y * 100, 4))})
        chs = layout.channels
        animated = [c for c in chs.values() if c.animated]
        if not animated:
            css["opacity"] = _num(chs["opacity"].initial, 6)
            parts = []
            if abs(chs["x"].initial) > 1e-9 or abs(chs["y"].initial) > 1e-9:
                parts.append("translate(%spx, %spx)" % (_num(chs["x"].initial, 4), _num(chs["y"].initial, 4)))
            if abs(chs["rotation"].initial) > 1e-9:
                parts.append("rotate(%sdeg)" % _num(chs["rotation"].initial, 6))
            if abs(chs["skewX"].initial) > 1e-9 or abs(chs["skewY"].initial) > 1e-9:
                parts.append("skew(%sdeg, %sdeg)" % (_num(chs["skewX"].initial, 6), _num(chs["skewY"].initial, 6)))
            if abs(chs["scaleX"].initial - 1) > 1e-9 or abs(chs["scaleY"].initial - 1) > 1e-9:
                parts.append("scale(%s, %s)" % (_num(chs["scaleX"].initial, 6), _num(chs["scaleY"].initial, 6)))
            if parts:
                css["transform"] = " ".join(parts)
        else:
            sel = "#" + eid
            init = ", ".join("%s: %s" % (c.prop, _num(c.initial)) for c in chs.values())
            skew = any(abs(c.initial) > 1e-9 or c.animated for c in (chs["skewX"], chs["skewY"]))
            # the starting state outside the timeline (a set at 0 inside it does not render at frame 0)
            script.append('gsap.set(%s, { %s%s });' % (json.dumps(sel), init, ', skewType: "simple"' if skew else ""))
            sampled = [c for c in chs.values() if c.samples is not None]
            for c in chs.values():
                if c.segments:
                    script.extend(_tween_lines(sel, c, fps))
            if sampled:
                keys = ", ".join("%s: [%s]" % (c.prop, ", ".join(_num(v) for v in (c.samples or [])))
                                 for c in sampled)
                script.append('tl.to(%s, { keyframes: { %s, easeEach: "none" }, duration: %s, ease: "none", '
                              'immediateRender: false }, %s);' % (json.dumps(sel), keys, _seconds((n - 1) / fps),
                                                                  _seconds(t_in)))
    return _Element(clip, kind, eid, src, attrs, css, script)


def _unsupported(clip: ClipView) -> List[str]:
    out = []
    for e in clip.effects:
        out.append(f"{clip.title!r}: the {e.name or e.class_name} effect is not exported (HyperFrames has no "
                   "equivalent; it plays without it)")
    data = clip.data
    corner = clip.curve("corner_radius")
    if corner.is_animated or abs(corner.first_value) > 1e-9:
        out.append(f"{clip.title!r}: rounded corners are not exported")
    if clip.parent_id:
        out.append(f"{clip.title!r} follows a parent clip; HyperFrames gets its own transform only")
    composite = data.get("composite")
    if composite not in (None, 0, "0"):
        out.append(f"{clip.title!r}: blend mode {composite} is not exported")
    return out


def build_index(snapshot: TimelineSnapshot, media: Dict[str, str], raw: dict, *,
                warnings: List[str], assets: Dict[str, str], originals: Dict[str, str]) -> Tuple[str, int, float]:
    """(index.html text, clip count, duration) for *snapshot*; *media* maps file id -> project-relative src."""
    fps = float(snapshot.fps)
    W, H = snapshot.width, snapshot.height
    elements: List[_Element] = []
    for track in snapshot.tracks:
        for clip in track.clips:
            kind = _media_kind(clip)
            if kind is None:
                warnings.append(f"{clip.title!r} has no media; it was left out")
                continue
            src = media.get(clip.file.id) if clip.file is not None else None
            if not src:
                continue
            warnings.extend(_unsupported(clip))
            elements.append(_clip_element(clip, kind, src, snapshot, track.index, track.clips, track.transitions,
                                          warnings))
        for tr in track.transitions:
            name = os.path.basename(tr.mask_path or "")
            if name.lower() not in UNIFORM_MASKS:
                warnings.append(f"the {tr.title or name or 'mask'} transition at {tr.position:.2f}s is exported as "
                                "a cross-fade")
            if tr.data.get("replace_image"):
                warnings.append(f"the {tr.title or name} transition at {tr.position:.2f}s shows its mask image in "
                                "Zenvi; HyperFrames gets a cross-fade")
    duration = max([snapshot.duration] + [0.0])
    duration = max(round(duration * fps) / fps, 1.0 / fps)
    css_rules = []
    body = []
    script = []
    element_records: Dict[str, dict] = {}
    for el in elements:
        if el.css:
            css_rules.append("      #%s { %s; }" % (el.element_id, "; ".join("%s: %s" % kv for kv in el.css.items())))
        attr_text = "".join((' %s' % k) if v == "" and k in ("muted", "playsinline") else
                            ' %s="%s"' % (k, html.escape(v, quote=True)) for k, v in el.attrs.items())
        if el.tag == "img":
            body.append("      <img%s />" % attr_text)
        else:
            body.append("      <%s%s></%s>" % (el.tag, attr_text, el.tag))
        script.extend("      " + line for line in el.script)
        element_records[el.clip.id] = {
            "kind": el.tag, "element": el.element_id, "src": el.src, "start": float(el.attrs["data-start"]),
            "duration": float(el.attrs["data-duration"]), "media_start": float(el.attrs.get("data-media-start", 0)),
            "track": int(el.attrs["data-track-index"])}
    script_text = "\n".join(script)
    timeline_json = {
        "zenvi_timeline": EXPORT_VERSION,
        "source_project": snapshot.name,
        "generator": "Zenvi %s" % _zenvi_version(),
        "fps": {"num": snapshot.fps.numerator, "den": snapshot.fps.denominator},
        "width": W, "height": H, "duration": duration,
        "zenvi": {"project": raw, "assets": assets, "originals": originals, "elements": element_records},
    }
    blob = json.dumps(timeline_json, ensure_ascii=False, separators=(",", ":"), default=str)
    blob = blob.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = html.escape(snapshot.name or "Zenvi export", quote=False)
    text = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    <title>{title}</title>
    <script src="{gsap}"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; background: #000000; }}
      #root {{ position: relative; width: {w}px; height: {h}px; overflow: hidden; }}
      .clip {{ position: absolute; left: 0; top: 0; }}
{rules}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="{root}" data-root="true" data-start="0" data-duration="{duration}" data-width="{w}" data-height="{h}" data-fps="{fps}">
{body}
    </div>
    <script type="application/json" id="{json_id}">{blob}</script>
    <script>
      window.__timelines = window.__timelines || {{}};
      {ease}
      const tl = gsap.timeline({{ paused: true }});
{script}
      tl.set({{}}, {{}}, {duration});
      window.__timelines["{root}"] = tl;
    </script>
  </body>
</html>
""".format(w=W, h=H, title=title, gsap=GSAP_CDN, rules="\n".join(css_rules), root=ROOT_ID,
           duration=_seconds(duration), fps=_fps_attr(snapshot), body="\n".join(body), json_id=TIMELINE_SCRIPT_ID,
           blob=blob, ease=ZENVI_EASE_JS, script=script_text)
    return text, len(elements), duration


def _zenvi_version() -> str:
    try:
        from classes import info
        return str(info.VERSION)
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Project files
# ---------------------------------------------------------------------------

def meta_files(name: str) -> Dict[str, str]:
    """hyperframes.json / meta.json / package.json as ``hyperframes init`` writes them."""
    slug = _slug(name)
    version = hf_cli.PINNED_VERSION
    hyperframes_json = {
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
        "paths": {"blocks": "compositions", "components": "compositions/components", "assets": "assets"},
        "media": {"autoProxy": True},
    }
    meta = {"id": slug, "name": name or slug,
            "createdAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    package = {"name": slug, "private": True, "type": "module", "scripts": {
        cmd: "npx --yes hyperframes@%s %s" % (version, sub)
        for cmd, sub in (("dev", "preview"), ("check", "check"), ("render", "render"), ("publish", "publish"))}}
    return {"hyperframes.json": json.dumps(hyperframes_json, indent=2) + "\n",
            "meta.json": json.dumps(meta, indent=2) + "\n",
            "package.json": json.dumps(package, indent=2) + "\n"}


def readme(name: str, warnings: Sequence[str], copy_media: bool) -> str:
    version = hf_cli.PINNED_VERSION
    notes = "\n".join("- " + w for w in warnings) or "- Nothing was approximated."
    return f"""# {name or 'Zenvi export'} (HyperFrames)

Exported from Zenvi. `index.html` plays the Zenvi timeline: every clip is a
HyperFrames primitive (`<video>`, `<img>`, `<audio>`) with its timing,
placement, keyframes (a GSAP timeline) and audio levels.

## Preview, check, render

```bash
npx hyperframes@{version} preview      # HyperFrames Studio
npx hyperframes@{version} lint
npx hyperframes@{version} render --output render.mp4
```

(`npm run dev`, `npm run check` and `npm run render` do the same.) Node.js 22+
and ffmpeg are required.

## Back to Zenvi

File > Import Project > HyperFrames Project... (or `import_hyperframes_project_tool`)
on this folder restores the original Zenvi clips natively from the
`zenvi-timeline` JSON inside `index.html` -- keep that script tag. Timing
changes made here to the exported clips (start, duration, in point, track)
are picked up; new clips and compositions you add come in as HyperFrames
clips.

## Media

{"Media files are copies in `assets/`." if copy_media else "Media in `assets/` are links to the original files (copy_media=false)."}

## What did not translate exactly

{notes}

## Licences

HyperFrames is Apache-2.0 (https://github.com/heygen-com/hyperframes). GSAP is
loaded from the jsDelivr CDN under GSAP's own no-charge licence
(https://gsap.com/standard-license); it is not part of this folder.
"""


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _asset_name(f: FileView, taken: set) -> str:
    stem, ext = os.path.splitext(os.path.basename(f.path) or f.name or "media")
    base = _safe_name(stem, "media") + (ext.lower() if re.match(r"^\.[A-Za-z0-9]{1,6}$", ext or "") else "")
    name, n = base, 2
    while name.lower() in taken:
        name = "%s-%d%s" % (os.path.splitext(base)[0], n, os.path.splitext(base)[1])
        n += 1
    taken.add(name.lower())
    return name


def _bring(src: str, dst: str, copy_media: bool, warnings: List[str]) -> None:
    if not copy_media:
        try:
            os.symlink(src, dst)
            return
        except (OSError, NotImplementedError):
            warnings.append(f"could not link {os.path.basename(src)} (symlinks need permission here); it was copied")
    shutil.copy2(src, dst)


def _remap_paths(value: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: (mapping.get(v, v) if k == "path" and isinstance(v, str) else _remap_paths(v, mapping))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_remap_paths(v, mapping) for v in value]
    return value


def export_project(snapshot: TimelineSnapshot, raw: dict, output_dir: str, *, copy_media: bool = True,
                   overwrite_changes: bool = False,
                   on_progress: Optional[Callable[[Optional[float], str], None]] = None,
                   should_cancel: Optional[Callable[[], bool]] = None) -> ExportResult:
    """Write *snapshot* as a HyperFrames project in *output_dir* (staged, then moved in). Blocking.

    Over an earlier Zenvi export only its own files are replaced, all together (:func:`_install`);
    files changed there since raise ExportChanged unless *overwrite_changes*.
    """
    plan = plan_output(output_dir, overwrite_changes=overwrite_changes)
    target = plan.target
    progress = on_progress or (lambda _f, _m: None)
    cancel = should_cancel or (lambda: False)
    if not snapshot.clips:
        raise ExportError("the timeline has no clips to export")
    warnings: List[str] = []
    used = snapshot.used_files()
    parent = os.path.dirname(target)
    staging = tempfile.mkdtemp(prefix=".zenvi-export-", dir=parent)
    try:
        os.makedirs(os.path.join(staging, ASSETS))
        media: Dict[str, str] = {}
        assets: Dict[str, str] = {}
        originals: Dict[str, str] = {}
        path_map: Dict[str, str] = {}
        # never a name the user's own files in assets/ already have
        user_assets = os.path.join(target, ASSETS)
        taken: set = {n.lower() for n in (os.listdir(user_assets) if os.path.isdir(user_assets) else [])
                      if (ASSETS + "/" + n) not in (plan.previous or {})}
        for i, f in enumerate(used):
            from classes.handoff.jobs import JobCancelled
            if cancel():
                raise JobCancelled("the HyperFrames export was cancelled")
            progress(0.8 * i / max(1, len(used)), "Copying %s" % (f.name or os.path.basename(f.path)))
            if f.is_image_sequence:
                warnings.append(f"{f.name!r} is an image sequence; HyperFrames has no image-sequence clip, so it was "
                                "left out")
                continue
            if not f.path or not os.path.isfile(f.path):
                warnings.append(f"{f.name or f.path!r} is missing on disk; its clips were left out")
                continue
            name = _asset_name(f, taken)
            _bring(f.path, os.path.join(staging, ASSETS, name), copy_media, warnings)
            rel = ASSETS + "/" + name
            media[f.id] = rel
            assets[f.id] = rel
            originals[f.id] = f.path
            path_map[f.path] = rel
        project_copy = _remap_paths(raw, path_map)
        progress(0.85, "Writing index.html")
        text, count, duration = build_index(snapshot, media, project_copy, warnings=warnings, assets=assets,
                                            originals=originals)
        if count == 0:
            raise ExportError("none of the timeline's clips could be exported: " + "; ".join(warnings[:3]))
        with open(os.path.join(staging, INDEX), "w", encoding="utf-8") as fh:
            fh.write(text)
        name = snapshot.name if snapshot.name and snapshot.name != "Untitled" else os.path.basename(target)
        for fname, content in meta_files(name).items():
            with open(os.path.join(staging, fname), "w", encoding="utf-8") as fh:
                fh.write(content)
        warnings = list(dict.fromkeys(warnings))
        with open(os.path.join(staging, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(readme(name, warnings, copy_media))
        _write_manifest(staging)
        progress(0.95, "Installing the project")
        files = _install(staging, plan)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    progress(1.0, "Exported")
    return ExportResult(output_dir=target, index=os.path.join(target, INDEX), files=files, assets=assets,
                        clips=count, warnings=warnings, duration=duration, replaced_changes=list(plan.changed))


def _staged_files(staging: str) -> List[str]:
    out = []
    for dirpath, _dirs, filenames in os.walk(staging):
        for name in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, name), staging).replace(os.sep, "/"))
    return sorted(out)


def _write_manifest(staging: str) -> None:
    files = {rel: _record(os.path.join(staging, *rel.split("/")), rel) for rel in _staged_files(staging)
             if rel != MANIFEST}
    with open(os.path.join(staging, MANIFEST), "w", encoding="utf-8") as fh:
        json.dump({"zenvi_export": MANIFEST_VERSION, "generator": "Zenvi %s" % _zenvi_version(), "files": files},
                  fh, indent=1, sort_keys=True)
        fh.write("\n")


def _install(staging: str, plan: OutputPlan) -> List[str]:
    """Move the staged project into the folder.

    New or empty folder: the staged folder is renamed into place. Over an earlier Zenvi export: its files
    (the manifest's, nothing else) move to a hidden backup, the new ones move in, the new manifest last;
    any failure puts the earlier export back as it was. The user's own files are never touched -- a new
    file whose name one of them has stops the export before anything moves.
    """
    target = plan.target
    written = [rel for rel in _staged_files(staging) if rel != MANIFEST]
    if plan.previous is None:
        if not os.path.exists(target):
            os.replace(staging, target)
            return written
        if not os.listdir(target):
            os.rmdir(target)
            os.replace(staging, target)
            return written
    previous = plan.previous or {}
    for rel in written + [MANIFEST]:
        dst = os.path.join(target, *rel.split("/"))
        if rel != MANIFEST and os.path.lexists(dst) and rel not in previous:
            raise ExportError(f"{rel} in {target} is not from Zenvi's earlier export; move it away or export to a "
                              "new folder")
    backup = tempfile.mkdtemp(prefix=".zenvi-export-old-", dir=os.path.dirname(target))
    moved_old: List[str] = []
    moved_new: List[str] = []
    try:
        for rel in sorted(previous) + [MANIFEST]:
            src = os.path.join(target, *rel.split("/"))
            if os.path.lexists(src) and not (os.path.isdir(src) and not os.path.islink(src)):
                dst = os.path.join(backup, *rel.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
                moved_old.append(rel)
        for rel in written + [MANIFEST]:  # the manifest last: it says the folder holds this export
            src = os.path.join(staging, *rel.split("/"))
            dst = os.path.join(target, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)
            moved_new.append(rel)
    except BaseException:
        restored = True
        for rel in reversed(moved_new):
            try:
                os.replace(os.path.join(target, *rel.split("/")), os.path.join(staging, *rel.split("/")))
            except OSError:
                restored = False
        for rel in reversed(moved_old):
            try:
                os.replace(os.path.join(backup, *rel.split("/")), os.path.join(target, *rel.split("/")))
            except OSError:
                restored = False
        if not restored:
            from classes.logger import log
            log.error("the HyperFrames export could not put every earlier file back; they are in %s", backup)
            backup = ""
        raise
    finally:
        if backup:
            shutil.rmtree(backup, ignore_errors=True)
    _prune_empty(os.path.join(target, ASSETS))
    return written


def _prune_empty(folder: str) -> None:
    try:
        if os.path.isdir(folder) and not os.path.islink(folder) and not os.listdir(folder):
            os.rmdir(folder)
    except OSError:
        pass


__all__ = [
    "EXPORT_VERSION", "ROOT_ID", "GSAP_CDN", "ExportError", "ExportResult", "raw_project", "check_output_dir",
    "Channel", "affine_channel", "sampled_channel", "Layout", "clip_layout", "volume_attrs", "build_index",
    "meta_files", "readme", "export_project", "top_clip", "mask_opacity", "transition_opacity", "transition_gain",
    "MANIFEST", "ExportChanged", "OutputPlan", "plan_output", "read_manifest",
]
