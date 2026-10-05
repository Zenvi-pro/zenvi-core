"""
 @file
 @brief Final Cut Pro 7 XML (xmeml v4) export, tuned for the Adobe Premiere Pro importer
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2018 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.

Conventions (checked against real Premiere Pro exports -- OpenTimelineIO's
``premiere_example.xml``, published Premiere XML files and the tools that
round-trip with Premiere, not guessed; see the C3 state note):

* Times are whole frames. The sequence ``<rate>`` is a whole ``timebase``
  plus ``ntsc`` (29.97 = 30/TRUE, 23.976 = 24/TRUE); frames are counted at
  the true rate. Every clipitem here uses the sequence rate.
* ``start``/``end`` are sequence frames; ``in``/``out`` are frames of the
  clip's media, counted from the media start, and ``out - in = end - start``.
  An edge that sits in a transition is ``-1``: the outgoing clip's ``out``
  reaches the transition's end, the incoming clip's ``in`` sits at its start
  (cut point = ``(start + end) // 2`` for a centred dissolve).
* A speed-changed clip counts ``in``/``out``/``duration`` (and keyframe
  ``when``) in its *retimed* frames: media frame = retimed frame x speed,
  reversed: media time = duration - retimed x speed. The Time Remap filter
  carries ``speed`` (percent), ``reverse`` and the ``graphdict`` virtual
  keyframes Premiere writes itself.
* Keyframe ``when`` values live in the clip's ``in``/``out`` space.
  Premiere writes bare ``when``/``value`` keys (no easing), so Zenvi's eased
  and hold segments are baked into extra linear keys wherever a straight line
  would leave the curve (sub-pixel, sub-percent tolerances).
* Basic Motion ``center`` is the anchor's offset from the sequence centre in
  *source media* pixels (``(position - frame/2) / media size - anchor``) and
  ``centerOffset`` the anchor relative to the media centre; ``scale`` is a
  percentage of the native media size, ``rotation`` degrees clockwise.
  Non-uniform scale becomes Scale (height) plus Distort ``aspect``.
* Opacity is 0-100; Audio Levels ``level`` is a linear gain (1 = 0 dB, max
  3.98109 = +12 dB). Stereo audio is written as Premiere's exploded track
  pairs; track names are ``MZ.TrackName``; marker colours are ``pproColor``
  (``0xAABBGGRR``, green = default, omitted).
* ``pathurl`` is ``file://localhost/<percent-encoded path>``
  (``file://localhost/C%3a/...`` for drive letters, ``file://server/share``
  for UNC paths). SVG titles are rendered to transparent PNG stills next to
  the XML, because Premiere cannot import SVG.

``export_timeline()`` is the blocking entry point (call it off the GUI
thread with a :class:`TimelineSnapshot` taken on it); ``build_xmeml()`` is
the pure part the tests drive; ``export_xml()`` keeps File > Export Project >
Export XML working.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import uuid as uuid_module
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, cast
from urllib.parse import quote

from classes.handoff.timeline_view import ClipView, FileView, TimelineSnapshot, TrackView, TransitionView
from classes.handoff.transform import (  # noqa: F401  (legacy helpers moved there; kept importable here)
    clip_geometry,
    gravity_offset as _gravity_offset,
    normalized_to_center_pixels as _normalized_to_center_pixels,
    scale_mode_size as _scale_mode_size,
)
from classes.logger import log

TICKS_PER_SECOND = 254016000000          # Premiere's time unit (pproTicks)
XMEML_VERSION = "4"
LEVEL_MAX = 3.98109                      # Audio Levels: +12 dB
SCALE_MAX = 1000.0                       # Basic Motion scale percentage
ROTATION_LIMIT = 8640.0                  # 24 turns, either way
STILL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".psd", ".tga")
CONVERT_STILL_EXTENSIONS = (".webp", ".avif", ".heic", ".heif", ".jxl")   # Premiere may not read these
ALPHA_EXTENSIONS = (".png", ".tif", ".tiff", ".psd", ".tga", ".gif")
DISSOLVE = {"name": "Cross Dissolve", "effectid": "Cross Dissolve", "effectcategory": "Dissolve"}
AUDIO_CROSSFADE = {"name": "Cross Fade (+3dB)", "effectid": "KGAudioTransCrossFade3dB"}
PPRO = {"authoringApp": "PremierePro"}

# Tolerances for baking curves into linear keys (in the XML parameter's own units).
TOL_CENTER_PX = 0.25                     # center/anchor, in source pixels
TOL_SCALE = 0.05                         # percent
TOL_ROTATION = 0.05                      # degrees
TOL_OPACITY = 1.0                        # percent (2.5 8-bit alpha steps)
TOL_LEVEL = 0.002                        # linear gain
TOL_ASPECT = 0.05                        # percent
TOL_REMAP = 0.5                          # media frames
MAX_KEYS = 2000                          # per parameter; beyond that keys are spread evenly

# Zenvi marker colours (classes.track_ops.MARKER_COLORS) -> Premiere pproColor; green is
# Premiere's default and is not written. Pink has no Premiere colour: purple is nearest.
PPRO_MARKER_COLORS = {
    "red": 4281740498, "blue": 4294741314, "orange": 4280578025, "yellow": 4281049552,
    "white": 4294967295, "purple": 4289825711, "pink": 4289825711,
}


class ExportError(Exception):
    """The timeline cannot be written as FCP7 XML; the message says why and what to do."""


# ---------------------------------------------------------------------------
# Conventions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rate:
    """An xmeml ``<rate>``: whole ``timebase`` + ``ntsc``; frames are counted at ``fps``."""

    timebase: int
    ntsc: bool
    fps: Fraction

    @property
    def ntsc_text(self) -> str:
        return "TRUE" if self.ntsc else "FALSE"

    @property
    def drop_frame(self) -> bool:
        return self.ntsc and self.timebase in (30, 60)

    def element(self) -> ET.Element:
        node = ET.Element("rate")
        _sub(node, "timebase", self.timebase)
        _sub(node, "ntsc", self.ntsc_text)
        return node


def rate_for(fps: Any) -> Rate:
    """The xmeml rate for a project frame rate (exact for NTSC, integer and half-integer rates)."""
    value = Fraction(fps) if not isinstance(fps, Fraction) else fps
    if value <= 0:
        raise ExportError(f"invalid frame rate {fps!r}")
    for base in (24, 30, 48, 60, 120):
        ntsc = Fraction(base * 1000, 1001)
        if abs(float(value) - float(ntsc)) < 0.0005 * base:
            return Rate(base, True, ntsc)
    if value.denominator == 1:
        return Rate(int(value), False, value)
    nearest = round(float(value))
    if nearest > 0 and abs(float(value) - nearest) < 1e-6:
        return Rate(nearest, False, Fraction(nearest))
    for k in range(2, 11):   # 12.5 fps -> counted at 25 (two xmeml frames per project frame)
        if (value * k).denominator == 1:
            return Rate(int(value * k), False, value * k)
    return Rate(max(1, nearest), False, Fraction(max(1, nearest)))


def frames(seconds: float, rate: Rate) -> int:
    """Seconds -> whole frames at *rate*, rounded half-up like ``frame_time.to_frame``."""
    value = float(seconds) * float(rate.fps)
    return int(math.floor(value + 0.5)) if value >= 0 else int(math.ceil(value - 0.5))


_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def pathurl(path: str) -> str:
    """Premiere's ``pathurl`` for a local path: ``file://localhost/`` + percent-encoded UTF-8.

    Drive letters are written ``C%3a`` and UNC paths ``file://server/share/...``,
    as Premiere writes them; POSIX paths are made absolute.
    """
    raw = str(path or "")
    if raw.startswith(("\\\\", "//")):
        rest = raw.replace("\\", "/").lstrip("/")
        host, _sep, tail = rest.partition("/")
        return "file://" + quote(host, safe="") + "/" + quote(tail, safe="/")
    if _DRIVE.match(raw):
        tail = raw[2:].replace("\\", "/")
        return "file://localhost/" + raw[0] + "%3a" + quote(tail, safe="/")
    absolute = raw if os.path.isabs(raw) else os.path.abspath(raw)
    return "file://localhost" + quote(absolute.replace("\\", "/"), safe="/")


def num(value: float, places: int = 6) -> str:
    """A compact decimal: no exponent, no trailing zeros, no ``-0``."""
    v = float(value)
    if not math.isfinite(v):
        v = 0.0
    text = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _sub(parent: ET.Element, tag: str, text: Any = None, attrs: Optional[Dict[str, str]] = None) -> ET.Element:
    node = ET.SubElement(parent, tag, {k: str(v) for k, v in (attrs or {}).items()})
    if text is not None:
        node.text = str(text)
    return node


def _safe_stem(text: str, fallback: str = "title") -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(text or "")).strip(" ._") or fallback
    return stem[:60]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class StillJob:
    """A still Premiere cannot read (SVG title, WebP...) rendered to a transparent PNG."""

    source: str
    target: str
    width: int
    height: int
    kind: str          # svg | image


@dataclass
class CopyJob:
    source: str
    target: str


@dataclass
class BuildResult:
    root: ET.Element
    stills: List[StillJob]
    copies: List[CopyJob]
    warnings: List[str]
    counts: Dict[str, int]
    media_dir: str


@dataclass
class ExportResult:
    """What ``export_timeline`` wrote."""

    path: str
    media_dir: Optional[str]
    stills: List[str]
    copied: List[str]
    warnings: List[str]
    counts: Dict[str, int]
    sequence_name: str
    replaced: bool = False

    def as_dict(self) -> dict:
        return {"path": self.path, "media_dir": self.media_dir, "titles": list(self.stills),
                "copied_media": list(self.copied), "warnings": list(self.warnings), "counts": dict(self.counts),
                "sequence_name": self.sequence_name, "replaced": self.replaced}


class _Warnings:
    """Warnings with counts: the same message from many clips is reported once ("... (3 clips)")."""

    def __init__(self):
        self._order: List[str] = []
        self._counts: Dict[str, int] = {}

    def add(self, message: str) -> None:
        if message not in self._counts:
            self._order.append(message)
            self._counts[message] = 0
        self._counts[message] += 1

    def as_list(self) -> List[str]:
        return [m if self._counts[m] == 1 else f"{m} ({self._counts[m]} times)" for m in self._order]


# ---------------------------------------------------------------------------
# Baking curves into linear keys
# ---------------------------------------------------------------------------

def _deviation(values: Sequence[Tuple[float, ...]], a: int, b: int, tol: Tuple[float, ...]) -> Tuple[float, int]:
    """(worst excess over tol, index) of linear interpolation between a and b."""
    worst, where = 0.0, -1
    va, vb = values[a], values[b]
    span = float(b - a)
    for i in range(a + 1, b):
        f = (i - a) / span
        for c in range(len(va)):
            err = abs(va[c] + (vb[c] - va[c]) * f - values[i][c]) / tol[c]
            if err > worst:
                worst, where = err, i
    return worst, where


def bake_keys(values: Sequence[Tuple[float, ...]], tol: Tuple[float, ...], must: Iterable[int] = ()) -> List[int]:
    """Indices into *values* whose straight-line interpolation stays within *tol* of every value.

    Starts from both ends plus *must* (the user's own keyframe frames) and
    splits a segment at its worst frame until no frame is off by more than
    its tolerance. Hold steps come out as a key on each side of the jump.
    """
    n = len(values)
    if n == 0:
        return []
    keys = {0, n - 1} | {i for i in must if 0 <= i < n}
    ordered = sorted(keys)
    pending = list(zip(ordered, ordered[1:]))
    while pending and len(keys) < MAX_KEYS:
        a, b = pending.pop()
        if b - a < 2:
            continue
        worst, where = _deviation(values, a, b, tol)
        if worst > 1.0:
            keys.add(where)
            pending.extend([(a, where), (where, b)])
    if len(keys) >= MAX_KEYS:
        step = max(1, n // MAX_KEYS)
        keys |= set(range(0, n, step))
    out = sorted(keys)

    def same(i, j):
        return all(abs(values[i][c] - values[j][c]) <= tol[c] for c in range(len(values[i])))

    # Premiere holds the first key's value before it and the last key's after it: drop
    # end keys that only repeat their neighbour (a flat start or tail).
    while len(out) > 2 and same(out[-1], out[-2]):
        out.pop()
    while len(out) > 2 and same(out[0], out[1]):
        out.pop(0)
    return out


def _constant(values: Sequence[Tuple[float, ...]], tol: Tuple[float, ...]) -> bool:
    first = values[0]
    return all(abs(v[c] - first[c]) <= tol[c] for v in values for c in range(len(first)))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

@dataclass
class _Media:
    file_id: str            # xmeml file id ("file-1")
    path: str               # what pathurl points at
    name: str
    width: int
    height: int
    duration: int           # media length in sequence frames (stills: the sequence length)
    has_video: bool
    has_audio: bool
    channels: int
    sample_rate: int
    still: bool
    alpha: bool
    written: bool = False


@dataclass
class _Transition:
    start: int
    end: int
    alignment: str          # center | start-black | end-black
    media: str              # video | audio

    @property
    def cut(self) -> int:
        if self.alignment == "start-black":
            return self.start
        if self.alignment == "end-black":
            return self.end
        return self.start + (self.end - self.start) // 2


@dataclass
class _Span:
    """Where one XML item of a clip sits: visible edges and the media it covers."""

    start: int              # visible start (the cut when a transition leads in)
    end: int                # visible end
    media_start: int        # sequence frame where its media begins (transition start)
    media_end: int          # sequence frame where its media ends (transition end)
    head: Optional[_Transition] = None
    tail: Optional[_Transition] = None
    join_from: Optional[str] = None     # clip id of the dissolve partner before it
    join_to: Optional[str] = None


@dataclass
class _Plan:
    clip: ClipView
    track: TrackView
    media: _Media
    start: int              # visible window on the sequence grid
    end: int
    video: bool
    audio: bool
    speed: str              # normal | constant | variable
    factor: float
    reversed: bool
    in_frame: int           # in/out-space frame shown at ``start``
    item_duration: int
    graph: List[Tuple[int, int, str]] = field(default_factory=list)   # graphdict (when, value, flag)
    masks: List[TransitionView] = field(default_factory=list)         # fade transitions folded into opacity
    vspan: Optional[_Span] = None
    aspan: Optional[_Span] = None
    items: List[Tuple[str, int, int, str]] = field(default_factory=list)  # (kind, track no, clip index, id)

    @property
    def id(self) -> str:
        return self.clip.id

    def in_at(self, frame: int) -> int:
        """The in/out-space frame shown at sequence *frame* (retimed frames advance with the timeline)."""
        return self.in_frame + (frame - self.start)


class XmemlBuilder:
    """Builds the xmeml tree for a snapshot (pure: no Qt, no project access, no file writes)."""

    def __init__(self, snapshot: TimelineSnapshot, xml_path: str, *, media_dir: Optional[str] = None,
                 collect_media: bool = False, sequence_name: str = "", sequence_uuid: Optional[str] = None,
                 translate: Optional[Callable[[str], str]] = None):
        self._ = translate or (lambda text: text)      # warnings: English for agents, the app's _tr in menus
        self.snap = snapshot
        self.xml_path = os.path.abspath(xml_path)
        stem = os.path.splitext(os.path.basename(self.xml_path))[0]
        self.media_dir = os.path.abspath(media_dir or os.path.join(os.path.dirname(self.xml_path), stem + "_media"))
        self.collect = bool(collect_media)
        self.name = sequence_name or snapshot.name or stem
        self.uuid = sequence_uuid or str(uuid_module.uuid4())
        self.rate = rate_for(snapshot.fps)
        self.w, self.h = int(snapshot.width), int(snapshot.height)
        self.warn = _Warnings()
        self.stills: List[StillJob] = []
        self.copies: List[CopyJob] = []
        self.media: Dict[str, _Media] = {}
        self._clip_ids = 0
        self._file_ids = 0
        self._copy_targets: Dict[str, str] = {}
        self._track_plans: Dict[int, List[_Plan]] = {}
        self._items: Dict[str, ET.Element] = {}
        self.counts = {"clips": 0, "video_items": 0, "audio_items": 0, "transitions": 0, "markers": 0,
                       "video_tracks": 0, "audio_tracks": 0, "titles": 0}
        if self.rate.fps != snapshot.fps and not self.rate.ntsc:
            self.warn.add(self._("the project's %(fps)s fps has no whole xmeml timebase; frames are counted at "
                                 "%(base)d fps") % {"fps": "%g" % float(snapshot.fps), "base": self.rate.timebase})

    # -- frames ---------------------------------------------------------------
    def f(self, seconds: float) -> int:
        return frames(seconds, self.rate)

    def t(self, frame: int) -> float:
        return float(Fraction(int(frame)) / self.rate.fps)

    # -- media ----------------------------------------------------------------
    def _media_for(self, fv: FileView) -> _Media:
        known = self.media.get(fv.id or fv.path)
        if known is not None:
            return known
        self._file_ids += 1
        ext = os.path.splitext(fv.path)[1].lower()
        width = int(fv.width or 0) or self.w
        height = int(fv.height or 0) or self.h
        still = bool(fv.is_still or fv.is_title)
        path, alpha = fv.path, ext in ALPHA_EXTENSIONS or fv.is_title
        link = fv.zenvi_link or {}
        render = link.get("render") if isinstance(link, dict) else None
        if isinstance(render, dict) and render.get("codec") in ("prores4444", "qtrle"):
            alpha = True
        tag = (fv.id or str(self._file_ids))[:8]
        if fv.is_title or ext in CONVERT_STILL_EXTENSIONS:
            kind = "svg" if fv.is_title else "image"
            folder = "titles" if fv.is_title else "stills"
            target = os.path.join(self.media_dir, folder, f"{_safe_stem(os.path.splitext(fv.name)[0])}-{tag}.png")
            self.stills.append(StillJob(fv.path, target, width, height, kind))
            path, alpha, still = target, True, True
            if fv.is_title:
                self.counts["titles"] += 1
        elif self.collect and fv.path:
            path = self._collect(fv.path)
        if fv.is_image_sequence:
            self.warn.add(self._("image sequence '%s' is written as its file pattern; import the sequence in "
                                 "Premiere and relink it") % os.path.basename(fv.path))
        data = fv.data or {}
        try:
            channels = int(data.get("channels") or 2)
        except (TypeError, ValueError):
            channels = 2
        try:
            sample_rate = int(data.get("sample_rate") or self.snap.sample_rate or 48000)
        except (TypeError, ValueError):
            sample_rate = 48000
        duration = self.f(fv.duration) if fv.duration and not still else 0
        media = _Media(file_id=f"file-{self._file_ids}", path=path, name=os.path.basename(path) or fv.name,
                       width=width, height=height, duration=duration,
                       has_video=bool(fv.has_video) and fv.media_type != "audio",
                       has_audio=bool(fv.has_audio) and not still, channels=max(1, channels),
                       sample_rate=sample_rate or 48000, still=still, alpha=alpha)
        self.media[fv.id or fv.path] = media
        return media

    def _collect(self, source: str) -> str:
        known = self._copy_targets.get(source)
        if known:
            return known
        base = os.path.basename(source) or "media"
        stem, ext = os.path.splitext(base)
        target = os.path.join(self.media_dir, base)
        n = 2
        taken = set(self._copy_targets.values())
        while target in taken:
            target = os.path.join(self.media_dir, f"{stem} {n}{ext}")
            n += 1
        self._copy_targets[source] = target
        self.copies.append(CopyJob(source, target))
        return target

    # -- clip plans -----------------------------------------------------------
    def _plan(self, clip: ClipView, track: TrackView) -> Optional[_Plan]:
        title = clip.title or clip.id
        if clip.file is None:
            self.warn.add(self._("clip '%s' has no media file; skipped") % title)
            return None
        start, end = self.f(clip.timeline_in), self.f(clip.timeline_out)
        if end - start < 1:
            return None
        media = self._media_for(clip.file)
        video = media.has_video and clip.has_video is not False
        audio = media.has_audio and clip.has_audio is not False
        if not video and not audio:
            self.warn.add(self._("clip '%s' has its video and audio turned off; skipped") % title)
            return None
        if clip.parent_id:
            self.warn.add(self._("clip '%s' follows a parent clip in Zenvi; exported at its own transform") % title)
        n = end - start
        speed, factor, reversed_ = "normal", 1.0, False
        kind = clip.speed.kind
        if media.still:
            in_frame, duration = 0, 0          # set from the sequence length later
        elif kind == "normal":
            in_frame = self.f(clip.source_in)
            duration = max(media.duration, in_frame + n)
            if media.duration and in_frame + n > media.duration + 1:
                self.warn.add(self._("clip '%s' runs past the end of its media; Premiere may shorten it") % title)
        elif kind == "constant" and clip.speed.factor:
            speed, factor, reversed_ = "constant", float(clip.speed.factor), bool(clip.speed.reversed)
            total = media.duration or (self.f(clip.source_out) + 1)
            if reversed_:
                in_frame = int(round((total - self.f(clip.source_out)) / factor))
            else:
                in_frame = int(round(self.f(clip.source_in) / factor))
            in_frame = max(0, in_frame)
            duration = max(int(round(total / factor)), in_frame + n)
        else:
            speed = "variable"
            in_frame, duration = 0, 0          # graph built below
        plan = _Plan(clip=clip, track=track, media=media, start=start, end=end, video=video, audio=audio,
                     speed=speed, factor=factor, reversed=reversed_, in_frame=in_frame, item_duration=duration)
        if speed == "constant":
            plan.graph = self._constant_graph(plan, media.duration or duration)
        elif speed == "variable":
            self._variable_graph(plan)
            if plan.audio:
                plan.audio = False
                self.warn.add(self._("clip '%s' has variable speed or a freeze frame: Premiere time remapping "
                                     "moves only video, so its audio was left out") % title)
            self.warn.add(self._("clip '%s' has variable speed or a freeze frame; exported as Premiere time "
                                 "remapping -- check its speed keyframes in Premiere") % title)
        self._warn_unmapped(plan)
        return plan

    def _constant_graph(self, plan: _Plan, total: int) -> List[Tuple[int, int, str]]:
        f, out = plan.factor, plan.in_frame + (plan.end - plan.start)
        end = plan.item_duration
        if plan.reversed:
            def value(w):
                return int(round(total - w * f))
        else:
            def value(w):
                return int(round(w * f))
        keys = [(0, value(0), "speedkfstart"), (plan.in_frame, value(plan.in_frame), "speedkfin"),
                (out, value(out), "speedkfout"), (end, value(end), "speedkfend")]
        merged: Dict[int, Tuple[int, int, str]] = {}
        for when, val, flag in keys:   # in == 0 shares the start key, like Premiere's own export
            if when not in merged or flag in ("speedkfin", "speedkfout"):
                merged[when] = (when, max(0, val), flag)
        return [merged[k] for k in sorted(merged)]

    def _variable_graph(self, plan: _Plan) -> None:
        clip, n = plan.clip, plan.end - plan.start
        mframes = [clip.source_frame_at(self.t(plan.start + k)) - 1 for k in range(n + 1)]
        w0 = max(0, mframes[0])
        plan.in_frame = w0
        idx = bake_keys([(float(m),) for m in mframes], (TOL_REMAP,),
                        must=self._key_offsets(plan, [clip.time] if clip.time is not None else []))
        total = plan.media.duration or (max(mframes) + 1)
        graph = []
        if w0 > 0:
            graph.append((0, 0, "speedkfstart"))
        for k in idx:
            flag = "speedkfin" if k == 0 else ("speedkfout" if k == n else "")
            graph.append((w0 + k, max(0, mframes[k]), flag))
        tail = max(0, total - mframes[n])
        end = w0 + n + tail
        if tail > 0:
            graph.append((end, total, "speedkfend"))
        plan.graph = graph
        plan.item_duration = max(end, w0 + n)
        span = abs(mframes[n] - mframes[0])
        plan.factor = span / float(n) if n else 1.0

    def _warn_unmapped(self, plan: _Plan) -> None:
        clip, title = plan.clip, plan.clip.title or plan.clip.id
        for effect in clip.effects:
            if effect.class_name == "Crop" and self._crop_static(effect) is not None:
                continue
            self.warn.add(self._("effect '%s' has no Premiere equivalent in FCP XML; left out")
                          % (effect.name or effect.class_name))
        for key, what in (("shear_x", self._("shear")), ("shear_y", self._("shear")),
                          ("corner_radius", self._("rounded corners"))):
            curve = clip.curves.get(key)
            if curve is not None and (curve.is_animated or abs(curve.first_value) > 1e-9):
                self.warn.add(self._("clip '%(clip)s' uses %(what)s, which Premiere's Basic Motion cannot carry; left "
                                     "out") % {"clip": title, "what": what})

    # -- transitions ------------------------------------------------------------
    def _match_transitions(self, track: TrackView, plans: List[_Plan]) -> None:
        vplans = [p for p in plans if p.video]
        heads: Dict[str, _Transition] = {}
        tails: Dict[str, _Transition] = {}
        joins: Dict[Tuple[str, str], _Transition] = {}
        # libopenshot fades audio under a transition only when its fade_audio_hint is set (equal power,
        # Timeline::ResolveTransitionAudioGains) -- Premiere's Cross Fade (+3dB). Without it both clips
        # play at full volume, so their audio simply overlaps (on separate lanes).
        a_heads: Dict[str, _Transition] = {}
        a_tails: Dict[str, _Transition] = {}
        a_joins: Dict[Tuple[str, str], bool] = {}
        for tv in track.transitions:
            audio_hint = bool((tv.data or {}).get("fade_audio_hint"))
            ts, te = self.f(tv.position), self.f(tv.end)
            if te - ts < 1:
                continue
            mask = os.path.splitext(os.path.basename(tv.mask_path or ""))[0].lower()
            fade = mask in ("", "fade")
            label = tv.title or mask or "transition"
            best = None
            for i, a in enumerate(vplans):
                for b in vplans[i + 1:]:
                    if not (a.start < b.start < a.end):
                        continue
                    lo, hi = max(ts, b.start), min(te, a.end)
                    if hi - lo >= 1 and (best is None or hi - lo > best[0]):
                        best = (hi - lo, a, b, lo, hi)
            if best is not None and best[1].id not in tails and best[2].id not in heads:
                _cover, a, b, lo, hi = best
                tr = _Transition(lo, hi, "center", "video")
                joins[(a.id, b.id)] = tr
                tails[a.id] = heads[b.id] = tr
                if audio_hint and a.audio and b.audio:
                    a_joins[(a.id, b.id)] = True
                    a_tails[a.id] = a_heads[b.id] = _Transition(lo, hi, "center", "audio")
                if not fade:
                    self.warn.add(self._("the '%s' wipe has no exact Premiere equivalent; exported as a Cross "
                                         "Dissolve") % label)
                if tv.reversed:
                    self.warn.add(self._("a reversed '%s' transition was exported as a normal Cross Dissolve") % label)
                if abs(lo - ts) > 1 or abs(hi - te) > 1 or abs(lo - b.start) > 1 or abs(hi - a.end) > 1:
                    self.warn.add(self._("a transition that does not cover its clips' overlap was exported as a "
                                         "Cross Dissolve over the part that does"))
                continue
            edge = None
            for p in vplans:
                if p.id not in heads and abs(p.start - ts) <= 1 and te <= p.end + 1:
                    edge = ("head", p, _Transition(p.start, min(te, p.end), "start-black", "video"))
                    break
                if p.id not in tails and abs(p.end - te) <= 1 and ts >= p.start - 1:
                    edge = ("tail", p, _Transition(max(ts, p.start), p.end, "end-black", "video"))
                    break
            if edge is not None:
                side, p, tr = edge
                (heads if side == "head" else tails)[p.id] = tr
                if audio_hint and p.audio:
                    (a_heads if side == "head" else a_tails)[p.id] = _Transition(tr.start, tr.end, tr.alignment,
                                                                                 "audio")
                if not fade:
                    self.warn.add(self._("the '%s' wipe has no exact Premiere equivalent; exported as a Cross "
                                         "Dissolve") % label)
                if tv.reversed == (side == "head"):
                    self.warn.add(self._("a '%s' transition that fades the wrong way for its clip edge was exported "
                                         "as a plain dissolve") % label)
                continue
            if fade:
                top = [p for p in vplans if p.start < te and p.end > ts]
                for p in top:
                    p.masks.append(tv)
                self.warn.add(self._("a fade in the middle of a clip was exported as opacity keyframes"))
            else:
                self.warn.add(self._("the '%s' wipe does not sit on a clip edge; left out") % label)
        # visible / media spans per item kind
        for p in plans:
            head, tail = heads.get(p.id), tails.get(p.id)
            partner_in = next((a for (a, b) in joins if b == p.id), None)
            partner_out = next((b for (a, b) in joins if a == p.id), None)
            if p.video:
                p.vspan = self._span(p, head, tail, partner_in, partner_out)
            if p.audio:
                a_in = partner_in if partner_in and (partner_in, p.id) in a_joins else None
                a_out = partner_out if partner_out and (p.id, partner_out) in a_joins else None
                p.aspan = self._span(p, a_heads.get(p.id), a_tails.get(p.id), a_in, a_out)

    def _span(self, p: _Plan, head: Optional[_Transition], tail: Optional[_Transition],
              join_from: Optional[str], join_to: Optional[str]) -> _Span:
        start, media_start, end, media_end = p.start, p.start, p.end, p.end
        if head is not None:
            start, media_start = head.cut, head.start
        if tail is not None:
            end, media_end = tail.cut, tail.end
        return _Span(start, end, media_start, media_end, head, tail, join_from, join_to)

    # -- lanes ----------------------------------------------------------------
    @staticmethod
    def _lanes(plans: List[_Plan], kind: str) -> List[List[_Plan]]:
        """Clips that overlap without a dissolve go on extra lanes (tracks) above, as Zenvi stacks them."""
        lanes: List[List[_Plan]] = []
        lane_of: Dict[str, int] = {}

        def span(p: _Plan) -> _Span:
            found = p.vspan if kind == "video" else p.aspan
            assert found is not None, "spans are set by _match_transitions"
            return found

        for p in sorted(plans, key=lambda q: (span(q).start, q.id)):
            s = span(p)
            if s.join_from is not None and s.join_from in lane_of:
                k = lane_of[s.join_from]
                if lanes[k][-1].id == s.join_from:
                    lanes[k].append(p)
                    lane_of[p.id] = k
                    continue
            for k, lane in enumerate(lanes):
                last = span(lane[-1])
                if last.join_to is not None:
                    continue          # reserved for its dissolve partner
                if last.end <= s.start:
                    lane.append(p)
                    lane_of[p.id] = k
                    break
            else:
                lanes.append([p])
                lane_of[p.id] = len(lanes) - 1
        return lanes

    # -- XML ------------------------------------------------------------------
    def build(self) -> BuildResult:
        snap = self.snap
        all_plans: List[_Plan] = []
        video_lanes: List[Tuple[TrackView, int, List[_Plan]]] = []
        audio_lanes: List[Tuple[TrackView, int, List[_Plan]]] = []
        for track in snap.tracks:
            plans = [p for p in (self._plan(c, track) for c in track.clips) if p is not None]
            if not plans:
                continue
            self._track_plans[track.number] = plans
            self._match_transitions(track, plans)
            all_plans.extend(plans)
            for k, lane in enumerate(self._lanes([p for p in plans if p.video], "video")):
                video_lanes.append((track, k, lane))
            for k, lane in enumerate(self._lanes([p for p in plans if p.audio], "audio")):
                audio_lanes.append((track, k, lane))
        self.counts["clips"] = len(all_plans)
        ends = [p.vspan.end for p in all_plans if p.vspan] + [p.aspan.end for p in all_plans if p.aspan]
        seq_frames = max(ends, default=0)
        for p in all_plans:
            if p.media.still:
                p.in_frame = 0
                p.media.duration = max(p.media.duration, seq_frames, p.end - p.start)
                p.item_duration = p.media.duration

        root = ET.Element("xmeml", {"version": XMEML_VERSION})
        seq = _sub(root, "sequence", attrs={"id": "sequence-1", "explodedTracks": "true"})
        _sub(seq, "uuid", self.uuid)
        _sub(seq, "duration", seq_frames)
        seq.append(self.rate.element())
        _sub(seq, "name", self.name)
        media = _sub(seq, "media")
        video = _sub(media, "video")
        fmt = _sub(_sub(video, "format"), "samplecharacteristics")
        fmt.append(self.rate.element())
        _sub(fmt, "width", self.w)
        _sub(fmt, "height", self.h)
        par = snap.pixel_aspect
        _sub(fmt, "anamorphic", "FALSE" if par == 1 else "TRUE")
        _sub(fmt, "pixelaspectratio", "square" if par == 1 else num(float(par)))
        _sub(fmt, "fielddominance", "none")
        _sub(fmt, "colordepth", 24)

        for number, (track, lane_no, lane) in enumerate(video_lanes, start=1):
            self._video_track(video, track, lane_no, lane, number)
        if not video_lanes:
            empty = _sub(video, "track")
            _sub(empty, "enabled", "TRUE")
            _sub(empty, "locked", "FALSE")
        self.counts["video_tracks"] = max(1, len(video_lanes))

        audio = _sub(media, "audio")
        _sub(audio, "numOutputChannels", 2)
        afmt = _sub(_sub(audio, "format"), "samplecharacteristics")
        _sub(afmt, "depth", 16)
        _sub(afmt, "samplerate", snap.sample_rate or 48000)
        outputs = _sub(audio, "outputs")
        for index in (1, 2):
            group = _sub(outputs, "group")
            _sub(group, "index", index)
            _sub(group, "numchannels", 1)
            _sub(group, "downmix", 0)
            _sub(_sub(group, "channel"), "index", index)
        a_number = 0
        for track, lane_no, lane in audio_lanes:
            stereo = any(p.media.channels >= 2 for p in lane)
            for channel in ((1, 2) if stereo else (1,)):
                a_number += 1
                self._audio_track(audio, track, lane_no, lane, a_number, channel, stereo)
        if not audio_lanes:
            empty = _sub(audio, "track")
            _sub(empty, "enabled", "TRUE")
            _sub(empty, "locked", "FALSE")
        self.counts["audio_tracks"] = max(1, a_number)

        self._links(all_plans)
        tc = _sub(seq, "timecode")
        tc.append(self.rate.element())
        sep = ";" if self.rate.drop_frame else ":"
        _sub(tc, "string", sep.join(["00"] * 4))
        _sub(tc, "frame", 0)
        _sub(tc, "displayformat", "DF" if self.rate.drop_frame else "NDF")
        for marker in snap.markers:
            m = _sub(seq, "marker")
            _sub(m, "comment", "")
            _sub(m, "name", marker.name or "")
            _sub(m, "in", max(0, self.f(marker.time)))
            _sub(m, "out", -1)
            color = PPRO_MARKER_COLORS.get(str(marker.color or "").lower())
            if color is not None:
                _sub(m, "pproColor", color)
            self.counts["markers"] += 1
        return BuildResult(root=root, stills=list(self.stills), copies=list(self.copies),
                           warnings=self.warn.as_list(), counts=dict(self.counts), media_dir=self.media_dir)

    def _track_name(self, track: TrackView, lane_no: int) -> str:
        base = track.label or f"Track {track.index + 1}"
        return base if lane_no == 0 else f"{base} ({lane_no + 1})"

    def _video_track(self, parent, track: TrackView, lane_no: int, lane: List[_Plan], number: int):
        node = _sub(parent, "track", attrs={"MZ.TrackName": self._track_name(track, lane_no)})
        index = 0
        for p in lane:
            span = p.vspan
            assert span is not None
            if span.head is not None and span.join_from is None:
                index += 1
                node.append(self._transition_item(span.head))
            index += 1
            node.append(self._clip_item(p, "video", span, number, index))
            if span.tail is not None:
                index += 1
                node.append(self._transition_item(span.tail))
        _sub(node, "enabled", "TRUE")
        _sub(node, "locked", "TRUE" if track.locked else "FALSE")
        return node

    def _audio_track(self, parent, track: TrackView, lane_no: int, lane: List[_Plan], number: int, channel: int,
                     stereo: bool):
        attrs = {"currentExplodedTrackIndex": str(channel - 1), "totalExplodedTrackCount": "2" if stereo else "1",
                 "premiereTrackType": "Stereo" if stereo else "Mono",
                 "MZ.TrackName": self._track_name(track, lane_no)}
        node = ET.SubElement(parent, "track", attrs)
        index = 0
        for p in lane:
            span = p.aspan
            assert span is not None
            if span.head is not None and span.join_from is None:
                index += 1
                node.append(self._transition_item(span.head))
            index += 1
            node.append(self._clip_item(p, "audio", span, number, index, channel=channel if stereo else 1,
                                        stereo=stereo))
            if span.tail is not None:
                index += 1
                node.append(self._transition_item(span.tail))
        _sub(node, "enabled", "TRUE")
        _sub(node, "locked", "TRUE" if track.locked else "FALSE")
        if stereo:
            _sub(node, "outputchannelindex", channel)
        return node

    def _transition_item(self, tr: _Transition) -> ET.Element:
        node = ET.Element("transitionitem")
        _sub(node, "start", tr.start)
        _sub(node, "end", tr.end)
        _sub(node, "alignment", tr.alignment)
        cut_seconds = float(Fraction(tr.cut - tr.start) / self.rate.fps)
        _sub(node, "cutPointTicks", int(round(cut_seconds * TICKS_PER_SECOND)))
        node.append(self.rate.element())
        effect = _sub(node, "effect")
        spec = DISSOLVE if tr.media == "video" else AUDIO_CROSSFADE
        _sub(effect, "name", spec["name"])
        _sub(effect, "effectid", spec["effectid"])
        if "effectcategory" in spec:
            _sub(effect, "effectcategory", spec["effectcategory"])
        _sub(effect, "effecttype", "transition")
        _sub(effect, "mediatype", tr.media)
        _sub(effect, "wipecode", 0)
        _sub(effect, "wipeaccuracy", 100)
        _sub(effect, "startratio", 0)
        _sub(effect, "endratio", 1)
        _sub(effect, "reverse", "FALSE")
        self.counts["transitions"] += 1
        return node

    def _clip_item(self, p: _Plan, kind: str, span: _Span, track_number: int, index: int, *, channel: int = 1,
                   stereo: bool = False) -> ET.Element:
        self._clip_ids += 1
        item_id = f"clipitem-{self._clip_ids}"
        attrs = {"id": item_id}
        if kind == "audio":
            attrs["premiereChannelType"] = "stereo" if stereo else "mono"
        node = ET.Element("clipitem", attrs)
        _sub(node, "name", p.clip.title or p.media.name)
        _sub(node, "enabled", "TRUE")
        _sub(node, "duration", p.item_duration)
        node.append(self.rate.element())
        in_value = p.in_at(span.media_start)
        out_value = in_value + (span.media_end - span.media_start)
        _sub(node, "start", -1 if span.head is not None else span.start)
        _sub(node, "end", -1 if span.tail is not None else span.end)
        _sub(node, "in", in_value)
        _sub(node, "out", out_value)
        if kind == "video":
            _sub(node, "alphatype", "straight" if p.media.alpha else "none")
            _sub(node, "pixelaspectratio", "square")
            _sub(node, "anamorphic", "FALSE")
        node.append(self._file_element(p.media))
        if kind == "audio":
            src = _sub(node, "sourcetrack")
            _sub(src, "mediatype", "audio")
            _sub(src, "trackindex", channel)
        frames_range = range(span.media_start, span.media_end)
        if kind == "video":
            self._motion_filters(node, p, frames_range)
            self._crop_filter(node, p, frames_range)
            self._opacity_filter(node, p, frames_range)
        else:
            self._levels_filter(node, p, frames_range)
        if p.speed != "normal" and (kind == "video" or p.speed == "constant"):
            self._time_remap(node, p, kind)
        p.items.append((kind, track_number, index, item_id))
        self._items[item_id] = node
        self.counts["video_items" if kind == "video" else "audio_items"] += 1
        return node

    def _file_element(self, m: _Media) -> ET.Element:
        node = ET.Element("file", {"id": m.file_id})
        if m.written:
            return node
        m.written = True
        _sub(node, "name", m.name)
        _sub(node, "pathurl", pathurl(m.path))
        node.append(self.rate.element())
        _sub(node, "duration", m.duration)
        tc = _sub(node, "timecode")
        tc.append(self.rate.element())
        _sub(tc, "string", "00:00:00:00")
        _sub(tc, "frame", 0)
        _sub(tc, "displayformat", "NDF")
        media = _sub(node, "media")
        if m.has_video:
            sc = _sub(_sub(media, "video"), "samplecharacteristics")
            sc.append(self.rate.element())
            _sub(sc, "width", m.width)
            _sub(sc, "height", m.height)
            _sub(sc, "anamorphic", "FALSE")
            _sub(sc, "pixelaspectratio", "square")
            _sub(sc, "fielddominance", "none")
        if m.has_audio:
            audio = _sub(media, "audio")
            sc = _sub(audio, "samplecharacteristics")
            _sub(sc, "depth", 16)
            _sub(sc, "samplerate", m.sample_rate)
            _sub(audio, "channelcount", m.channels)
        return node

    # -- filters --------------------------------------------------------------
    def _key_offsets(self, p: _Plan, curves) -> List[int]:
        """Frame offsets (from the plan's start) of the curves' own keyframes inside the clip."""
        out = set()
        for curve in curves:
            if curve is None or curve.is_constant:
                continue
            for point in curve.points:
                out.add(self.f(point.time) - p.start)
        return sorted(out)

    def _param(self, effect: ET.Element, pid: str, name: str, values: List[Tuple[float, ...]], whens: List[int],
               tol: Tuple[float, ...], must: Iterable[int], *, lo=None, hi=None, point: bool = False,
               fmt: Callable[[float], str] = num) -> None:
        node = _sub(effect, "parameter", attrs=PPRO)
        _sub(node, "parameterid", pid)
        _sub(node, "name", name)
        if lo is not None:
            _sub(node, "valuemin", lo)
        if hi is not None:
            _sub(node, "valuemax", hi)

        def put(parent, v):
            value = _sub(parent, "value")
            if point:
                _sub(value, "horiz", fmt(v[0]))
                _sub(value, "vert", fmt(v[1]))
            else:
                value.text = fmt(v[0])

        put(node, values[0])
        if not _constant(values, tol):
            for k in bake_keys(values, tol, must):
                kf = _sub(node, "keyframe")
                _sub(kf, "when", whens[k])
                put(kf, values[k])

    def _effect(self, node: ET.Element, name: str, effectid: str, category: str, media: str) -> ET.Element:
        effect = _sub(_sub(node, "filter"), "effect")
        _sub(effect, "name", name)
        _sub(effect, "effectid", effectid)
        _sub(effect, "effectcategory", category)
        _sub(effect, "effecttype", "motion" if category == "motion" else category)
        _sub(effect, "mediatype", media)
        return effect

    def _motion_values(self, p: _Plan, frames_range: range):
        clip, media = p.clip, p.media
        par = float(self.snap.pixel_aspect or 1)
        rows = []
        for fr in frames_range:
            g = clip_geometry(clip, self.t(fr), self.w, self.h, src_w=media.width, src_h=media.height)
            sw, sh = float(g.source_width), float(g.source_height)
            ax, ay = g.origin_x - 0.5, g.origin_y - 0.5
            cx = ((g.anchor_x - self.w / 2.0) / sw - ax) * par
            cy = (g.anchor_y - self.h / 2.0) / sh - ay
            aspect = (1.0 - (g.scale_x / g.scale_y)) * 100.0 if abs(g.scale_y) > 1e-12 else 0.0
            rows.append(((cx, cy), (ax, ay), g.scale_y * 100.0, g.rotation, aspect, sw, sh))
        return rows

    def _motion_filters(self, node: ET.Element, p: _Plan, frames_range: range) -> None:
        if not frames_range:
            return
        rows = self._motion_values(p, frames_range)
        sw, sh = rows[0][5], rows[0][6]
        # scale and aspect are percentages of the source: keep their error under TOL_CENTER_PX pixels too
        tol_scale = min(TOL_SCALE, 100.0 * TOL_CENTER_PX / max(sw, sh))
        tol_aspect = min(TOL_ASPECT, 100.0 * TOL_CENTER_PX / max(sw, sh))
        centers = [r[0] for r in rows]
        anchors = [r[1] for r in rows]
        scales = [(min(SCALE_MAX, r[2]),) for r in rows]
        rotations = [(max(-ROTATION_LIMIT, min(ROTATION_LIMIT, r[3])),) for r in rows]
        aspects = [(r[4],) for r in rows]
        identity = (_constant(centers, (TOL_CENTER_PX / sw, TOL_CENTER_PX / sh))
                    and all(abs(c) <= TOL_CENTER_PX / max(sw, sh) for c in centers[0])
                    and _constant(anchors, (1e-6, 1e-6)) and all(abs(a) < 1e-6 for a in anchors[0])
                    and _constant(scales, (tol_scale,)) and abs(scales[0][0] - 100.0) <= tol_scale
                    and _constant(rotations, (TOL_ROTATION,)) and abs(rotations[0][0]) <= TOL_ROTATION)
        if any(r[2] > SCALE_MAX for r in rows):
            self.warn.add(self._("clip '%(clip)s' is scaled above %(max)s%%; Premiere's Basic Motion stops there")
                          % {"clip": p.clip.title, "max": "%g" % SCALE_MAX})
        whens = [p.in_at(fr) for fr in frames_range]
        clip = p.clip
        must = self._key_offsets(p, [clip.curves.get(k) for k in ("location_x", "location_y", "scale_x", "scale_y",
                                                                  "rotation", "origin_x", "origin_y")])
        offset = frames_range.start - p.start
        must = [m - offset for m in must]
        if not identity:
            effect = self._effect(node, "Basic Motion", "basic", "motion", "video")
            _sub(effect, "pproBypass", "false")
            self._param(effect, "scale", "Scale", scales, whens, (tol_scale,), must, lo=0, hi=1000)
            self._param(effect, "rotation", "Rotation", rotations, whens, (TOL_ROTATION,), must, lo=-8640, hi=8640)
            self._param(effect, "center", "Center", centers, whens, (TOL_CENTER_PX / sw, TOL_CENTER_PX / sh), must,
                        point=True)
            self._param(effect, "centerOffset", "Anchor Point", anchors, whens,
                        (TOL_CENTER_PX / sw, TOL_CENTER_PX / sh), must, point=True)
            self._param(effect, "antiflicker", "Anti-flicker Filter", [(0.0,)], [0], (1.0,), (), lo="0.0", hi="1.0")
        if not (_constant(aspects, (tol_aspect,)) and abs(aspects[0][0]) <= tol_aspect):
            effect = self._effect(node, "Distort", "deformation", "motion", "video")
            self._param(effect, "aspect", "Aspect", aspects, whens, (tol_aspect,), must, lo=-10000, hi=10000)
            self.warn.add(self._("clip '%s' is scaled unevenly; exported as Scale (height) plus Distort Aspect -- "
                                 "check its proportions in Premiere") % p.clip.title)

    @staticmethod
    def _crop_static(effect) -> Optional[dict]:
        """Crop effect values when they map to FCP7 Crop (no offset, no resize), else None."""
        params = effect.params
        if params.get("resize"):
            return None
        for key in ("x", "y"):
            curve = effect.curve(key)
            if curve is not None and (curve.is_animated or abs(curve.first_value) > 1e-9):
                return None
        sides = {}
        for key in ("left", "right", "top", "bottom"):
            curve = effect.curve(key)
            sides[key] = curve
        return sides

    def _crop_filter(self, node: ET.Element, p: _Plan, frames_range: range) -> None:
        for effect in p.clip.effects:
            if effect.class_name != "Crop":
                continue
            sides = self._crop_static(effect)
            if sides is None:
                continue
            filt = self._effect(node, "Crop", "crop", "motion", "video")
            whens = [p.in_at(fr) for fr in frames_range]
            for key in ("left", "right", "top", "bottom"):
                curve = sides[key]
                values = [((curve.value_at(self.t(fr)) if curve is not None else 0.0) * 100.0,)
                          for fr in frames_range]
                self._param(filt, key, key.capitalize(), values, whens, (TOL_OPACITY,), (), lo=0, hi=100)

    def _mask_alpha(self, p: _Plan, tv: TransitionView, fr: int) -> float:
        """The Mask transition's alpha on the clip at sequence frame *fr* (libopenshot Mask::GetFrame, black mask)."""
        t = self.t(fr)
        if not (tv.position - 1e-9 <= t < tv.end - 1e-9):
            return 1.0
        later = [q for q in self._track_plans.get(p.track.number, [])
                 if q.video and q.start <= fr < q.end and q.start > p.start]
        if later:
            return 1.0   # the mask only applies to the top clip of its track
        brightness = tv.brightness.value_at(t)
        contrast = tv.contrast.value_at(t)
        factor = 20.0 / max(0.5, 20.0 - contrast)
        adjusted = int(factor * (int(255 * brightness) - 128) + 128)
        adjusted = max(0, min(255, adjusted))
        alpha = max(0, min(255, 255 - adjusted))
        if (tv.data or {}).get("mask_invert"):
            alpha = 255 - alpha
        return alpha / 255.0

    def _opacity_filter(self, node: ET.Element, p: _Plan, frames_range: range) -> None:
        if not frames_range:
            return
        curve = p.clip.curve("alpha")
        values = []
        for fr in frames_range:
            alpha = curve.value_at(self.t(fr))
            for tv in p.masks:
                alpha *= self._mask_alpha(p, tv, fr)
            values.append((max(0.0, min(100.0, alpha * 100.0)),))
        if _constant(values, (TOL_OPACITY,)) and abs(values[0][0] - 100.0) <= TOL_OPACITY:
            return
        whens = [p.in_at(fr) for fr in frames_range]
        offset = frames_range.start - p.start
        must = [m - offset for m in self._key_offsets(p, [curve])]
        effect = self._effect(node, "Opacity", "opacity", "motion", "video")
        self._param(effect, "opacity", "opacity", values, whens, (TOL_OPACITY,), must, lo=0, hi=100)

    def _levels_filter(self, node: ET.Element, p: _Plan, frames_range: range) -> None:
        if not frames_range:
            return
        curve = p.clip.curve("volume")
        raw = [curve.value_at(self.t(fr)) for fr in frames_range]
        if any(v > LEVEL_MAX + 1e-6 for v in raw):
            self.warn.add(self._("clip '%s' is louder than Premiere's +12 dB Audio Levels limit; capped")
                          % p.clip.title)
        values = [(max(0.0, min(LEVEL_MAX, v)),) for v in raw]
        if _constant(values, (TOL_LEVEL,)) and abs(values[0][0] - 1.0) <= TOL_LEVEL:
            return
        whens = [p.in_at(fr) for fr in frames_range]
        offset = frames_range.start - p.start
        must = [m - offset for m in self._key_offsets(p, [curve])]
        effect = _sub(_sub(node, "filter"), "effect")
        _sub(effect, "name", "Audio Levels")
        _sub(effect, "effectid", "audiolevels")
        _sub(effect, "effectcategory", "audiolevels")
        _sub(effect, "effecttype", "audiolevels")
        _sub(effect, "mediatype", "audio")
        self._param(effect, "level", "Level", values, whens, (TOL_LEVEL,), must, lo=0, hi=LEVEL_MAX)

    def _time_remap(self, node: ET.Element, p: _Plan, kind: str) -> None:
        effect = _sub(_sub(node, "filter"), "effect")
        _sub(effect, "name", "Time Remap")
        _sub(effect, "effectid", "timeremap")
        if kind == "video":
            _sub(effect, "effectcategory", "motion")
            _sub(effect, "effecttype", "motion")
            _sub(effect, "mediatype", "video")

        def simple(pid, value, lo=None, hi=None):
            param = _sub(effect, "parameter", attrs=PPRO)
            _sub(param, "parameterid", pid)
            _sub(param, "name", pid)
            if lo is not None:
                _sub(param, "valuemin", lo)
                _sub(param, "valuemax", hi)
            _sub(param, "value", value)

        simple("variablespeed", 1 if p.speed == "variable" else 0, 0, 1)
        simple("speed", num(p.factor * 100.0, 4), -100000, 100000)
        simple("reverse", "TRUE" if p.reversed else "FALSE")
        simple("frameblending", "FALSE")
        if kind != "video" or not p.graph:
            return
        graph = _sub(effect, "parameter", attrs=PPRO)
        _sub(graph, "parameterid", "graphdict")
        _sub(graph, "name", "graphdict")
        _sub(graph, "valuemin", 0)
        _sub(graph, "valuemax", max(v for _w, v, _f in p.graph))
        _sub(graph, "value", 0)
        for when, value, flag in p.graph:
            kf = _sub(graph, "keyframe")
            _sub(kf, "when", when)
            _sub(kf, "value", value)
            if p.speed == "constant" or flag:
                _sub(kf, "speedvirtualkf", "TRUE" if p.speed == "constant" else "FALSE")
            if flag:
                _sub(kf, flag, "TRUE")
        _sub(_sub(graph, "interpolation"), "name", "FCPCurve")

    def _links(self, plans: List[_Plan]) -> None:
        """Premiere's A/V links: every item of a clip lists all of them (itself included)."""
        for p in plans:
            if len(p.items) < 2:
                continue
            for _kind, _no, _idx, item_id in p.items:
                el = self._items.get(item_id)
                if el is None:
                    continue
                for kind, number, index, ref in p.items:
                    link = ET.Element("link")
                    _sub(link, "linkclipref", ref)
                    _sub(link, "mediatype", kind)
                    _sub(link, "trackindex", number)
                    _sub(link, "clipindex", index)
                    if kind == "audio":
                        _sub(link, "groupindex", 1)
                    el.append(link)    # Premiere's order: ... file, sourcetrack, filter*, link*


def build_xmeml(snapshot: TimelineSnapshot, xml_path: str, *, media_dir: Optional[str] = None,
                collect_media: bool = False, sequence_name: str = "", sequence_uuid: Optional[str] = None,
                translate: Optional[Callable[[str], str]] = None) -> BuildResult:
    """The xmeml tree for *snapshot* plus the stills to render and media to copy (pure)."""
    builder = XmemlBuilder(snapshot, xml_path, media_dir=media_dir, collect_media=collect_media,
                           sequence_name=sequence_name, sequence_uuid=sequence_uuid, translate=translate)
    result = builder.build()
    problems = validate_xmeml(result.root)
    if problems:
        raise ExportError("the XML Zenvi built is not valid FCP7 XML (please report this): " + "; ".join(problems[:5]))
    return result


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------

def _int(node: Optional[ET.Element], tag: str) -> Optional[int]:
    if node is None:
        return None
    text = node.findtext(tag)
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _cut(tr: ET.Element) -> Optional[int]:
    s, e = _int(tr, "start"), _int(tr, "end")
    if s is None or e is None:
        return None
    alignment = (tr.findtext("alignment") or "center").strip()
    if alignment in ("start", "start-black"):
        return s
    if alignment in ("end", "end-black"):
        return e
    return s + (e - s) // 2


def validate_xmeml(root: ET.Element) -> List[str]:
    """Problems that would make Premiere reject or misread the file (empty list = valid).

    Checks the required elements, unique clipitem ids, file definitions,
    links, durations (``out - in`` matches the clip's place on the track, -1
    edges resolved through their transitions), non-overlapping items per
    track, keyframe order and transition shape.
    """
    problems: List[str] = []
    if root.tag != "xmeml" or root.get("version") not in ("4", "5"):
        problems.append("the root element must be <xmeml version=\"4\">")
    sequences = root.findall("sequence")
    if len(sequences) != 1:
        return problems + ["exactly one top-level <sequence> is required"]
    seq = sequences[0]
    for tag in ("name", "duration", "rate", "media"):
        if seq.find(tag) is None:
            problems.append(f"<sequence> needs <{tag}>")
    timebase = _int(seq.find("rate"), "timebase")
    if not timebase or timebase <= 0:
        problems.append("the sequence rate needs a positive whole <timebase>")
    if (seq.findtext("rate/ntsc") or "").strip() not in ("TRUE", "FALSE"):
        problems.append("the sequence rate needs <ntsc>TRUE|FALSE</ntsc>")
    if not _int(seq.find("media/video/format/samplecharacteristics"), "width"):
        problems.append("the sequence format needs a width and height")
    seq_duration = _int(seq, "duration") or 0
    ids, files_defined, files_used, links = set(), set(), set(), []
    for kind in ("video", "audio"):
        for t_no, track in enumerate(seq.findall(f"media/{kind}/track"), start=1):
            items = [el for el in track if el.tag in ("clipitem", "transitionitem", "generatoritem")]
            last_end = None
            for i, el in enumerate(items):
                if el.tag == "transitionitem":
                    s, e = _int(el, "start"), _int(el, "end")
                    if s is None or e is None or e <= s:
                        problems.append(f"{kind} track {t_no}: a transition needs start < end")
                    if (el.findtext("alignment") or "") not in ("start", "center", "end", "start-black", "end-black"):
                        problems.append(f"{kind} track {t_no}: transition alignment is invalid")
                    eff = el.find("effect")
                    if eff is None or not eff.findtext("effectid") or eff.findtext("effecttype") != "transition":
                        problems.append(f"{kind} track {t_no}: transition needs an effect of type transition")
                    continue
                cid = el.get("id")
                if not cid or cid in ids:
                    problems.append(f"clipitem id {cid!r} is missing or used twice")
                ids.add(cid)
                start, end = _int(el, "start"), _int(el, "end")
                cin, cout = _int(el, "in"), _int(el, "out")
                if None in (start, end, cin, cout) or el.findtext("name") is None:
                    problems.append(f"clipitem {cid}: name, start, end, in and out are required")
                    continue
                assert start is not None and end is not None and cin is not None and cout is not None
                prev = items[i - 1] if i > 0 else None
                nxt = items[i + 1] if i + 1 < len(items) else None
                media_start, vis_start = start, start
                media_end, vis_end = end, end
                if start == -1:
                    if prev is None or prev.tag != "transitionitem":
                        problems.append(f"clipitem {cid}: start -1 needs a transition before it")
                        continue
                    media_start, vis_start = _int(prev, "start"), _cut(prev)
                if end == -1:
                    if nxt is None or nxt.tag != "transitionitem":
                        problems.append(f"clipitem {cid}: end -1 needs a transition after it")
                        continue
                    media_end, vis_end = _int(nxt, "end"), _cut(nxt)
                if None in (media_start, media_end, vis_start, vis_end):
                    problems.append(f"clipitem {cid}: its transitions have no start/end")
                    continue
                assert media_start is not None and media_end is not None
                assert vis_start is not None and vis_end is not None
                if vis_end <= vis_start or cout <= cin or cin < 0:
                    problems.append(f"clipitem {cid}: needs in < out and start < end")
                if cout - cin != media_end - media_start:
                    problems.append(f"clipitem {cid}: out - in ({cout - cin}) does not match its length on the "
                                    f"track ({media_end - media_start})")
                duration = _int(el, "duration")
                if duration is not None and duration < cout:
                    problems.append(f"clipitem {cid}: out ({cout}) is past its duration ({duration})")
                if last_end is not None and vis_start < last_end:
                    problems.append(f"{kind} track {t_no}: clipitem {cid} overlaps the item before it")
                last_end = vis_end
                if vis_end > seq_duration:
                    problems.append(f"clipitem {cid}: ends after the sequence duration")
                file_el = el.find("file")
                if file_el is None or not file_el.get("id"):
                    problems.append(f"clipitem {cid}: needs a <file id=...>")
                else:
                    fid = file_el.get("id")
                    files_used.add(fid)
                    if len(file_el):
                        if fid in files_defined:
                            problems.append(f"file {fid} is defined twice")
                        files_defined.add(fid)
                        if file_el.findtext("pathurl") is None and file_el.findtext("mediaSource") is None:
                            problems.append(f"file {fid}: needs a pathurl")
                        for tag in ("name", "rate", "duration"):
                            if file_el.find(tag) is None:
                                problems.append(f"file {fid}: needs <{tag}>")
                for param in el.iter("parameter"):
                    whens = [_int(k, "when") for k in param.findall("keyframe")]
                    if any(w is None for w in whens) or any(b <= a for a, b in zip(whens, whens[1:])):  # type: ignore[operator]
                        problems.append(f"clipitem {cid}: keyframes of {param.findtext('parameterid')} are not in "
                                        "time order")
                for link in el.findall("link"):
                    links.append((cid, link.findtext("linkclipref")))
    for fid in sorted(files_used - files_defined):
        problems.append(f"file {fid} is referenced but never defined")
    for cid, ref in links:
        if ref not in ids:
            problems.append(f"clipitem {cid} links to unknown clipitem {ref!r}")
    for marker in seq.findall("marker"):
        m_in, m_out = _int(marker, "in"), _int(marker, "out")
        if m_in is None or m_in < 0 or m_out is None or (m_out != -1 and m_out < m_in):
            problems.append("a marker needs in >= 0 and out -1 or >= in")
    return problems


# ---------------------------------------------------------------------------
# Writing (blocking: call off the GUI thread)
# ---------------------------------------------------------------------------

def _xml_text(root: ET.Element) -> str:
    ET.indent(root, space="\t")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _install(path: str, write: Callable[[str], Any]) -> None:
    """Write through ``<path>.partial`` and move it into place (never a half-written file at *path*)."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    partial = path + ".partial"
    try:
        write(partial)
        os.replace(partial, path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise


def render_still_png(source: str, target: str, width: int, height: int, kind: str = "svg") -> None:
    """Render an SVG title (or a still Premiere cannot read) to a transparent PNG. Qt painting: a QThread."""
    from qt_api import QImage, QPainter, QSvgRenderer, Qt

    def write(partial: str) -> None:
        image = QImage(int(width), int(height), QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        try:
            if kind == "svg":
                renderer = QSvgRenderer(source)
                if not renderer.isValid():
                    raise ExportError(f"the title {os.path.basename(source)} is not a valid SVG")
                renderer.render(painter)
            else:
                still = QImage(source)
                if still.isNull():
                    raise ExportError(f"cannot read the image {os.path.basename(source)}")
                painter.drawImage(0, 0, still.scaled(int(width), int(height)))
        finally:
            painter.end()
        if not image.save(partial, "PNG"):
            raise ExportError(f"could not write {target}")

    _install(target, write)


def _render_stills_qt(stills: List[StillJob]) -> None:
    from classes.handoff import jobs

    def work():
        for job in stills:
            render_still_png(job.source, job.target, job.width, job.height, job.kind)

    jobs.run_on_qthread(work, 10 * 60)


def _copy_media(copies: List[CopyJob], should_cancel: Optional[Callable[[], bool]] = None,
                on_progress: Optional[Callable[[float, str], None]] = None) -> List[str]:
    copied = []
    for i, job in enumerate(copies):
        if should_cancel is not None and should_cancel():
            from classes.handoff.jobs import JobCancelled
            raise JobCancelled("export cancelled")
        if on_progress is not None:
            on_progress(i / max(1, len(copies)), f"Copying {os.path.basename(job.source)}")
        if not os.path.isfile(job.source):
            raise ExportError(f"the media {job.source} is missing; relink it before collecting media")
        try:
            same = os.path.isfile(job.target) and os.path.getsize(job.target) == os.path.getsize(job.source) \
                and int(os.path.getmtime(job.target)) == int(os.path.getmtime(job.source))
        except OSError:
            same = False
        if not same:
            _install(job.target, lambda partial, src=job.source: shutil.copy2(src, partial))
        copied.append(job.target)
    return copied


def export_timeline(snapshot: TimelineSnapshot, xml_path: str, *, collect_media: bool = False,
                    media_dir: Optional[str] = None, sequence_name: str = "", sequence_uuid: Optional[str] = None,
                    render_stills: Optional[Callable[[List[StillJob]], None]] = None,
                    on_progress: Optional[Callable[[float, str], None]] = None,
                    should_cancel: Optional[Callable[[], bool]] = None,
                    translate: Optional[Callable[[str], str]] = None) -> ExportResult:
    """Write *snapshot* as Premiere-ready FCP7 XML at *xml_path* (blocking; off the GUI thread).

    Titles are rendered to PNG stills (and media copied when
    *collect_media*) into ``<xml stem>_media/`` first; the XML is written
    last, through a temporary file, so it never points at files that are not
    there yet. Raises ExportError with what to do on failure.
    """
    if not snapshot.clips:
        raise ExportError("the timeline has no clips to export")
    xml_path = os.path.abspath(xml_path)
    if not xml_path.lower().endswith(".xml"):
        xml_path += ".xml"
    replaced = os.path.exists(xml_path)
    result = build_xmeml(snapshot, xml_path, media_dir=media_dir, collect_media=collect_media,
                         sequence_name=sequence_name, sequence_uuid=sequence_uuid, translate=translate)
    if on_progress is not None:
        on_progress(0.1, "Rendering titles")
    if result.stills:
        (render_stills or _render_stills_qt)(result.stills)
    copied = _copy_media(result.copies, should_cancel, on_progress) if result.copies else []
    if should_cancel is not None and should_cancel():
        from classes.handoff.jobs import JobCancelled
        raise JobCancelled("export cancelled")
    text = _xml_text(result.root)

    def write(partial: str) -> None:
        with open(partial, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    _install(xml_path, write)
    if on_progress is not None:
        on_progress(1.0, "Exported")
    used_media_dir = result.media_dir if (result.stills or result.copies) else None
    seq_name = result.root.findtext("sequence/name") or ""
    log.info("Exported FCP7 XML %s (%s)", xml_path, result.counts)
    return ExportResult(path=xml_path, media_dir=used_media_dir, stills=[s.target for s in result.stills],
                        copied=copied, warnings=list(result.warnings), counts=result.counts,
                        sequence_name=seq_name, replaced=replaced)


# ---------------------------------------------------------------------------
# The app: File > Export Project > Export XML (and the Premiere entries in classes.handoff.premiere)
# ---------------------------------------------------------------------------

def snapshot_from_app() -> TimelineSnapshot:
    """The open project's snapshot, taken on the GUI thread from any thread."""
    from classes.qt_main_thread import call_on_gui
    return cast(TimelineSnapshot, call_on_gui(TimelineSnapshot.from_app, timeout=60))


def default_export_path(ext: str = ".xml", suffix: str = "") -> str:
    """Next to the saved project (``<name><suffix>.xml``), else in the home folder."""
    from classes import info
    from classes.app import get_app
    project_path = getattr(get_app().project, "current_filepath", "") or ""
    if project_path:
        stem = os.path.splitext(os.path.basename(project_path))[0]
        return os.path.join(os.path.dirname(project_path), stem + suffix + ext)
    return os.path.join(info.HOME_PATH, "Untitled Project" + suffix + ext)


def report_text(result: ExportResult, translate: Callable[[str], str]) -> str:
    """A short human report: what was written, then the warnings."""
    _ = translate
    lines = [_("Wrote %s") % result.path]
    if result.stills:
        lines.append(_("%d title(s) rendered as PNG stills in %s") % (len(result.stills), result.media_dir))
    if result.copied:
        lines.append(_("%d media file(s) copied to %s") % (len(result.copied), result.media_dir))
    if result.warnings:
        lines.append("")
        lines.append(_("Some things could not be carried over exactly:"))
        lines.extend("• " + w for w in result.warnings[:12])
        if len(result.warnings) > 12:
            lines.append(_("…and %d more") % (len(result.warnings) - 12))
    return "\n".join(lines)


def run_export_job(window, xml_path: str, *, collect_media: bool = False, open_folder: bool = False,
                   title: str = "", done_message: str = ""):
    """GUI thread: snapshot now, export on the handoff executor, report when done. Returns the Job."""
    from classes.app import get_app
    from classes.handoff import jobs
    from qt_api import QMessageBox

    app = get_app()
    _ = app._tr
    snapshot = TimelineSnapshot.from_app()
    if not snapshot.clips:
        QMessageBox.information(window, title or _("Export XML"), _("The timeline has no clips to export."))
        return None

    def work(job):
        return export_timeline(snapshot, xml_path, collect_media=collect_media, translate=_,
                               on_progress=lambda f, m: job.report(f, m), should_cancel=job.should_cancel)

    def on_done(job):
        from windows.handoff_menus import notify
        if job.state == jobs.CANCELLED:
            notify(window, _("Export cancelled"))
            return
        if job.error is not None:
            QMessageBox.warning(window, title or _("Export XML"), _("Export failed: %s") % job.error)
            return
        result = job.result
        notify(window, done_message or (_("Exported %s") % os.path.basename(result.path)))
        if result.warnings:
            QMessageBox.information(window, title or _("Export XML"), report_text(result, _))
        if open_folder:
            from qt_api import QDesktopServices, QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(result.path)))

    return jobs.submit_job(work, label=_("Exporting %s") % os.path.basename(xml_path), kind="xml-export",
                           on_done=on_done)


def export_xml(file_path: Optional[str] = None):
    """Export the timeline as FCP7 XML (Premiere conventions).

    With no *file_path* (File > Export Project > Export XML) it asks for a
    path and exports in the background; returns the job. With a path it
    exports right away (blocking -- call off the GUI thread) and returns the
    written path; ExportError says what went wrong.
    """
    if file_path is not None:
        result = export_timeline(snapshot_from_app(), file_path)
        return result.path
    from classes.app import get_app
    from qt_api import QFileDialog
    app = get_app()
    _ = app._tr
    chosen = QFileDialog.getSaveFileName(app.window, _("Export XML..."), default_export_path(),
                                         _("Final Cut Pro XML (*.xml)"))[0]
    if not chosen:
        return None
    if not chosen.lower().endswith(".xml"):
        chosen += ".xml"
    return run_export_job(app.window, chosen, title=_("Export XML"))
