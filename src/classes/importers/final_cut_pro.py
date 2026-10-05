"""
 @file
 @brief Import a Final Cut Pro 7 / Premiere Pro XML (xmeml) sequence into the open project as ONE undo step
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

The reverse handoff (Premiere -> Zenvi). Reads the conventions documented in
``classes.exporters.final_cut_pro`` (they are Premiere's own):

* clips keep their place, trims and enable state; a disabled clip or track
  comes in hidden (video off) or muted (audio off), so nothing is lost;
* linked video/audio items become one Zenvi clip when their timing matches;
  a J/L cut keeps them as separate video-only and audio-only clips, and a
  video item with no linked audio gets its audio turned off (Premiere played
  none);
* Cross Dissolves (and other video transitions, with a warning) become Zenvi
  fade transitions over the same frames with a straight-line ramp, so the
  dissolve looks like Premiere's; the two clips overlap on one track like
  Zenvi's own crossfades. Audio cross fades become volume ramps;
* Time Remap: constant speed / reverse become the ``time`` curve Zenvi's own
  Speed menu writes; variable speed (graphdict keys) becomes a linear time
  curve through the same frames;
* Opacity, Audio Levels, Basic Motion (scale, rotation, center, anchor point,
  Distort aspect) and Crop, static or keyframed, become Zenvi keyframes that
  place the clip where Premiere did (``center`` is in source pixels);
* sequence markers become timeline markers with the nearest colour;
* nested sequences are FLATTENED: their clips come in on extra tracks right
  above the track the nest sat on, cut to the part of the nest that was used
  (up to 8 levels deep). A nest's own motion/opacity/speed cannot be applied
  to its clips and is reported;
* generators (titles, mattes, bars, adjustment layers), clip markers and
  effects Zenvi has no equivalent for are left out and reported.

Everything new goes on NEW tracks above the existing ones (``placement``
``new_tracks`` keeps the sequence's own timing; ``at_playhead`` starts it at
the playhead). Parsing, media probing and planning run off the GUI thread
(:func:`plan_import`); :func:`commit_import` makes every change on the GUI
thread inside one undo transaction, so one Undo removes the whole import.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, cast
from urllib.parse import unquote, urlparse

try:  # untrusted XML from other applications
    from defusedxml import ElementTree as SafeET  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - defusedxml is a declared dependency
    from xml.etree import ElementTree as SafeET  # type: ignore[no-redef]
import xml.etree.ElementTree as ET

from classes import frame_time as ft
from classes.exporters.final_cut_pro import PPRO_MARKER_COLORS, TICKS_PER_SECOND
from classes.handoff.transform import (  # noqa: F401  (legacy helpers moved there; kept importable here)
    gravity_offset as _gravity_offset,
    qsize_scaled,
    scale_mode_size as _scale_mode_size,
)
from classes.logger import log

LINEAR, BEZIER, CONSTANT = 1, 0, 2
TRACK_STRIDE = 1000000
MAX_NEST_DEPTH = 8
# A Zenvi fade (black mask, contrast 3) whose brightness runs 0.9253 -> 0.0753 in a straight
# line ramps the clip's alpha 0 -> 255 linearly over the transition, like a Cross Dissolve.
DISSOLVE_BRIGHTNESS = (0.9253, 0.0753)
DISSOLVE_CONTRAST = 3.0
LEGACY_CENTER_LIMIT = 20.0          # |center| beyond this: an old OpenShot export (pixel centres)
# effects whose keyframes may sit straight under <effect> (old Zenvi exports): the parameter they animate
DIRECT_KEY_PARAMS = {"opacity": "opacity", "audiolevels": "level"}
# a per-keyframe <interpolation> name (old Zenvi exports) -> libopenshot interpolation; Premiere and
# FCP7 keys carry none (FCP7's parameter-level FCPCurve is read as linear)
KEY_INTERPOLATIONS = {"linear": LINEAR, "0": LINEAR, "bezier": BEZIER, "ease": BEZIER, "easein": BEZIER,
                      "easeout": BEZIER, "1": BEZIER, "constant": CONSTANT, "hold": CONSTANT, "2": CONSTANT}
STILL_SEQUENCE_RE = r"\d{3,}\.(png|jpe?g|tiff?|exr|dpx|tga|bmp|psd)$"   # name.0001.png: an image sequence
ZENVI_MARKER_RGB = {
    "blue": (0, 0, 255), "red": (255, 0, 0), "green": (0, 160, 0), "yellow": (255, 215, 0),
    "orange": (255, 140, 0), "purple": (160, 32, 240), "pink": (255, 105, 180), "white": (255, 255, 255),
}


class XmlImportError(Exception):
    """The XML cannot be imported; the message says why and what to do."""


class NoClipsError(XmlImportError):
    """No clip of the sequence could be placed; ``missing`` is the media that was not found or not readable."""

    def __init__(self, message: str, missing: Iterable[str] = ()):
        super().__init__(message)
        self.missing = list(missing)


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class XRate:
    timebase: int
    ntsc: bool

    @property
    def fps(self) -> Fraction:
        return Fraction(self.timebase * 1000, 1001) if self.ntsc else Fraction(self.timebase)

    def seconds(self, frames: float) -> float:
        return float(frames) / float(self.fps)


@dataclass
class XFile:
    id: str
    name: str
    pathurl: str
    path: str
    media_source: str
    width: int
    height: int
    duration: Optional[int]
    rate: Optional[XRate]
    has_video: bool
    has_audio: bool
    channels: Optional[int] = None  # <media><audio><channelcount>, when the XML says


@dataclass
class XParam:
    id: str
    name: str
    value: Any                      # float, (h, v), str
    keys: List[Tuple[float, Any]]   # (when, value)
    interpolation: str = ""         # the parameter's <interpolation> (FCP7: FCPCurve, hold)
    valuemin: Optional[float] = None
    valuemax: Optional[float] = None
    # per key, in ``keys`` order: its own <interpolation> name (old Zenvi exports write one) and the
    # flags it carries (``speedvirtualkf``, ``speedkfin``, ... set to TRUE), lower case
    meta: List[Tuple[str, FrozenSet[str]]] = field(default_factory=list)


@dataclass
class XEffect:
    name: str
    effectid: str
    category: str
    mediatype: str
    enabled: bool
    params: Dict[str, XParam]

    def num(self, pid: str, default: float) -> float:
        p = self.params.get(pid)
        if p is None or isinstance(p.value, tuple):
            return default
        try:
            return float(p.value)
        except (TypeError, ValueError):
            return default

    def flag(self, pid: str) -> bool:
        p = self.params.get(pid)
        return p is not None and str(p.value).strip().upper() in ("TRUE", "1")


@dataclass
class XTransition:
    start: int
    end: int
    alignment: str
    name: str
    effectid: str
    mediatype: str

    @property
    def cut(self) -> int:
        if self.alignment in ("start", "start-black"):
            return self.start
        if self.alignment in ("end", "end-black"):
            return self.end
        return self.start + (self.end - self.start) // 2


@dataclass
class XClip:
    id: str
    name: str
    kind: str                       # video | audio
    enabled: bool
    start: int
    end: int
    in_: int
    out: int
    duration: Optional[int]
    rate: Optional[XRate]
    ticks_in: Optional[int]
    ticks_out: Optional[int]
    file: Optional[XFile]
    sequence: Optional["XSequence"]
    effects: List[XEffect]
    links: List[Tuple[str, str]]    # (clip id, mediatype)
    sourcetrack: int                # the media channel / track an audio item plays (1-based)
    generator: bool
    markers: int
    track: "XTrack" = field(repr=False, default=None)  # type: ignore[assignment]
    channel_type: str = ""          # Premiere's premiereChannelType: stereo | mono | ""
    prev: Optional[XTransition] = None
    next: Optional[XTransition] = None

    def effect(self, *ids: str) -> Optional[XEffect]:
        for e in self.effects:
            if e.effectid.lower() in ids and e.enabled:
                return e
        return None


@dataclass
class XTrack:
    kind: str
    number: int                     # 1-based among the sequence's tracks of this kind
    name: str
    enabled: bool
    locked: bool
    exploded_index: int
    exploded_count: int
    items: List[Any]

    @property
    def primary(self) -> bool:
        return self.exploded_index == 0 or self.exploded_count <= 1


@dataclass
class XMarker:
    name: str
    comment: str
    in_: int
    out: int
    color: Optional[int]


@dataclass
class XSequence:
    id: str
    name: str
    rate: XRate
    width: int
    height: int
    duration: int
    par: float
    video: List[XTrack]
    audio: List[XTrack]
    markers: List[XMarker]
    others: List[str] = field(default_factory=list)   # the file's other top-level sequences (names)


def _text(el: Optional[ET.Element], path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _int(el: Optional[ET.Element], path: str, default: Optional[int] = None) -> Optional[int]:
    text = _text(el, path)
    if not text:
        return default
    try:
        return int(round(float(text)))
    except ValueError:
        return default


def _float(text: Any, default: float = 0.0) -> float:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _bool(el: Optional[ET.Element], path: str, default: bool) -> bool:
    text = _text(el, path).upper()
    if text in ("TRUE", "1"):
        return True
    if text in ("FALSE", "0"):
        return False
    return default


def path_from_pathurl(url: str, base_folder: str = "") -> str:
    """A filesystem path from an xmeml ``pathurl`` (file URLs, raw paths, relative paths)."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("file:"):
        parsed = urlparse(raw)
        netloc, path = parsed.netloc, unquote(parsed.path or "")
        if netloc and netloc.lower() != "localhost":
            return "//" + netloc + path                       # UNC: file://server/share/...
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            return path[1:]                                   # /C:/x -> C:/x
        return os.path.normpath(path) if path else ""
    if raw.startswith("@"):
        from classes.path_utils import absolute_media_path
        return absolute_media_path(raw)
    raw = unquote(raw)
    if os.path.isabs(raw) or (len(raw) > 2 and raw[1] == ":"):
        return raw
    return os.path.normpath(os.path.join(base_folder, raw)) if base_folder else raw


class _Parser:
    def __init__(self, base_folder: str):
        self.base = base_folder
        self.files: Dict[str, ET.Element] = {}
        self.sequences: Dict[str, ET.Element] = {}
        self.parsed_files: Dict[str, XFile] = {}
        self.parsed_sequences: Dict[str, XSequence] = {}
        self.depth = 0

    def _rate(self, el: Optional[ET.Element]) -> Optional[XRate]:
        if el is None:
            return None
        node = el.find("rate")
        tb = _int(node, "timebase")
        if node is None or not tb or tb <= 0:
            return None
        return XRate(tb, _bool(node, "ntsc", False))

    def index(self, root: ET.Element) -> None:
        """The first full definition of every ``id`` (later elements may only reference it)."""
        for el in root.iter("file"):
            fid = el.get("id")
            if fid and len(el) and fid not in self.files:
                self.files[fid] = el
        for el in root.iter("sequence"):
            sid = el.get("id")
            if sid and len(el) and sid not in self.sequences:
                self.sequences[sid] = el

    def file(self, el: ET.Element) -> XFile:
        fid = el.get("id") or ""
        if fid in self.parsed_files:
            return self.parsed_files[fid]
        full = self.files.get(fid, el) if fid else el
        url = _text(full, "pathurl")
        width = _int(full, "media/video/samplecharacteristics/width") or _int(full, "width") or 0
        height = _int(full, "media/video/samplecharacteristics/height") or _int(full, "height") or 0
        xf = XFile(id=fid, name=_text(full, "name"), pathurl=url, path=path_from_pathurl(url, self.base),
                   media_source=_text(full, "mediaSource"), width=width, height=height,
                   duration=_int(full, "duration"), rate=self._rate(full),
                   has_video=full.find("media/video") is not None,
                   has_audio=full.find("media/audio") is not None,
                   channels=_int(full, "media/audio/channelcount"))
        if fid:
            self.parsed_files[fid] = xf
        return xf

    @staticmethod
    def keyframes(parent: ET.Element) -> Tuple[List[Tuple[float, Any]], List[Tuple[str, FrozenSet[str]]]]:
        """(when, value) keys under *parent*, sorted by when, and each key's (interpolation, flags)."""
        rows = []
        for k in parent.findall("keyframe"):
            when = _float(_text(k, "when"), float("nan"))
            kv_el = k.find("value")
            if kv_el is None or not math.isfinite(when):
                continue
            if kv_el.find("horiz") is not None:
                kv: Any = (_float(_text(kv_el, "horiz")), _float(_text(kv_el, "vert")))
            else:
                kv = _float(kv_el.text)
            flags = frozenset(c.tag.lower() for c in k if (c.text or "").strip().upper() == "TRUE")
            rows.append((when, kv, (_text(k, "interpolation/name").lower(), flags)))
        rows.sort(key=lambda r: r[0])
        return [(w, v) for w, v, _m in rows], [m for _w, _v, m in rows]

    def effect(self, el: ET.Element, enabled: bool) -> XEffect:
        params = {}
        for p in el.findall("parameter"):
            pid = _text(p, "parameterid") or _text(p, "name")
            value_el = p.find("value")
            value: Any = ""
            if value_el is not None:
                if value_el.find("horiz") is not None:
                    value = (_float(_text(value_el, "horiz")), _float(_text(value_el, "vert")))
                else:
                    value = (value_el.text or "").strip()
            keys, meta = self.keyframes(p)
            vmin, vmax = _text(p, "valuemin"), _text(p, "valuemax")
            params[pid] = XParam(id=pid, name=_text(p, "name"), value=value, keys=keys,
                                 interpolation=_text(p, "interpolation/name").lower(),
                                 valuemin=_float(vmin, 0.0) if vmin else None,
                                 valuemax=_float(vmax, 0.0) if vmax else None, meta=meta)
        effectid = _text(el, "effectid")
        main = DIRECT_KEY_PARAMS.get(effectid.lower())
        if main and main not in params and el.find("keyframe") is not None:
            # old Zenvi / OpenShot exports put an effect's keyframes straight under <effect>
            keys, meta = self.keyframes(el)
            params[main] = XParam(id=main, name=main, value=keys[0][1] if keys else "", keys=keys, meta=meta)
        return XEffect(name=_text(el, "name"), effectid=effectid, category=_text(el, "effectcategory"),
                       mediatype=_text(el, "mediatype"), enabled=enabled, params=params)

    def clip(self, el: ET.Element, kind: str, track: XTrack) -> XClip:
        effects = []
        for filt in el.findall("filter"):
            enabled = _bool(filt, "enabled", True)
            for eff in filt.findall("effect"):
                effects.append(self.effect(eff, enabled))
        if el.tag == "generatoritem":
            for eff in el.findall("effect"):
                effects.append(self.effect(eff, True))
        file_el = el.find("file")
        seq_el = el.find("sequence")
        nested = None
        if seq_el is not None:
            nested = self.sequence(self.sequences.get(seq_el.get("id") or "", seq_el))
        links = [(_text(link, "linkclipref"), _text(link, "mediatype")) for link in el.findall("link")]
        ticks_in = _int(el, "pproTicksIn")
        ticks_out = _int(el, "pproTicksOut")
        xf = self.file(file_el) if file_el is not None else None
        generator = el.tag == "generatoritem" or (xf is not None and not xf.pathurl)
        return XClip(id=el.get("id") or "", name=_text(el, "name"), kind=kind, enabled=_bool(el, "enabled", True),
                     start=_int(el, "start", 0) or 0, end=_int(el, "end", 0) or 0, in_=_int(el, "in", 0) or 0,
                     out=_int(el, "out", 0) or 0, duration=_int(el, "duration"), rate=self._rate(el),
                     ticks_in=ticks_in, ticks_out=ticks_out, file=xf, sequence=nested, effects=effects,
                     links=links, sourcetrack=_int(el, "sourcetrack/trackindex", 1) or 1, generator=generator,
                     markers=len(el.findall("marker")), track=track,
                     channel_type=(el.get("premiereChannelType") or "").lower())

    def sequence(self, el: ET.Element) -> XSequence:
        sid = el.get("id") or ""
        if sid and sid in self.parsed_sequences:
            return self.parsed_sequences[sid]
        self.depth += 1
        try:
            if self.depth > MAX_NEST_DEPTH + 1:
                raise XmlImportError("the XML nests sequences more than %d levels deep" % MAX_NEST_DEPTH)
            rate = self._rate(el) or XRate(30, False)
            fmt = el.find("media/video/format/samplecharacteristics")
            par_text = _text(fmt, "pixelaspectratio", "square").lower()
            seq = XSequence(id=sid, name=_text(el, "name") or "Sequence", rate=rate,
                            width=_int(fmt, "width") or 1920, height=_int(fmt, "height") or 1080,
                            duration=_int(el, "duration", 0) or 0,
                            par=1.0 if par_text in ("", "square") else _float(par_text, 1.0),
                            video=[], audio=[], markers=[])
            if sid:
                self.parsed_sequences[sid] = seq
            for kind, out in (("video", seq.video), ("audio", seq.audio)):
                for number, tr in enumerate(el.findall(f"media/{kind}/track"), start=1):
                    track = XTrack(kind=kind, number=number,
                                   name=tr.get("MZ.TrackName") or _text(tr, "name"),
                                   enabled=_bool(tr, "enabled", True), locked=_bool(tr, "locked", False),
                                   exploded_index=int(_float(tr.get("currentExplodedTrackIndex"), 0)),
                                   exploded_count=int(_float(tr.get("totalExplodedTrackCount"), 1)), items=[])
                    for item in tr:
                        if item.tag in ("clipitem", "generatoritem"):
                            track.items.append(self.clip(item, kind, track))
                        elif item.tag == "transitionitem":
                            eff = item.find("effect")
                            track.items.append(XTransition(
                                start=_int(item, "start", 0) or 0, end=_int(item, "end", 0) or 0,
                                alignment=_text(item, "alignment", "center").lower(),
                                name=_text(eff, "name"), effectid=_text(eff, "effectid"),
                                mediatype=_text(eff, "mediatype", kind)))
                    for i, item in enumerate(track.items):
                        if isinstance(item, XClip):
                            before = track.items[i - 1] if i > 0 else None
                            after = track.items[i + 1] if i + 1 < len(track.items) else None
                            item.prev = before if isinstance(before, XTransition) else None
                            item.next = after if isinstance(after, XTransition) else None
                    out.append(track)
            for m in el.findall("marker"):
                color = _int(m, "pproColor")
                seq.markers.append(XMarker(name=_text(m, "name"), comment=_text(m, "comment"),
                                           in_=_int(m, "in", 0) or 0, out=_int(m, "out", -1) or -1, color=color))
            return seq
        finally:
            self.depth -= 1


def parse_xml(path: str, sequence: str = "") -> XSequence:
    """A sequence of an xmeml file: the one named *sequence*, else the first top-level one.

    Top level = not used as a nest by another sequence (a Premiere export holds its sequence first and
    the nests it uses inline; an FCP7 project can hold several). The others' names are in ``.others``.
    """
    try:
        tree = SafeET.parse(path)
    except SafeET.ParseError as exc:  # type: ignore[attr-defined]
        raise XmlImportError(f"{os.path.basename(path)} is not valid XML: {exc}") from None
    except OSError as exc:
        raise XmlImportError(f"cannot read {path}: {exc}") from None
    except Exception as exc:  # defusedxml refuses entities / DTD tricks
        raise XmlImportError(f"{os.path.basename(path)} cannot be read safely: {exc}") from None
    root = tree.getroot()
    if root is None or root.tag != "xmeml":
        raise XmlImportError(f"{os.path.basename(path)} is not a Final Cut Pro 7 / Premiere XML (no <xmeml>); "
                             "export it from Premiere with File > Export > Final Cut Pro XML")
    parser = _Parser(os.path.dirname(os.path.abspath(path)))
    parser.index(root)
    in_clips = [s_el for item in root.iter("clipitem") for s_el in item.findall("sequence")]
    nested = {s_el.get("id") for s_el in in_clips if s_el.get("id")}
    inline = {id(s_el) for s_el in in_clips}
    tops = [el for el in root.iter("sequence")
            if len(el) and id(el) not in inline and (el.get("id") or "") not in nested]
    if not tops:   # only nests, or an empty <sequence/>: take the first one (it says what it lacks below)
        tops = ([el for el in root.iter("sequence") if len(el)] or list(root.iter("sequence")))[:1]
    if not tops:
        raise XmlImportError(f"{os.path.basename(path)} has no <sequence> to import")
    names = [_text(el, "name") or "Sequence" for el in tops]
    chosen = 0
    if str(sequence or "").strip():
        wanted = str(sequence).strip().lower()
        matches = [i for i, n in enumerate(names) if n.lower() == wanted]
        if not matches:
            raise XmlImportError(f"{os.path.basename(path)} has no sequence named {sequence!r}; it has "
                                 + ", ".join(repr(n) for n in names))
        chosen = matches[0]
    seq_el = tops[chosen]
    seq = parser.sequence(parser.sequences.get(seq_el.get("id") or "", seq_el))
    seq.others = [n for i, n in enumerate(names) if i != chosen]
    if not any(isinstance(i, XClip) for t in seq.video + seq.audio for i in t.items):
        raise XmlImportError(f"{os.path.basename(path)} has no clips (no <clipitem>) in sequence {seq.name!r}")
    return seq


def media_paths(seq: XSequence) -> List[str]:
    """Every media path the sequence (and its nests) uses, in first-use order."""
    out: Dict[str, None] = {}

    def walk(s: XSequence, depth: int = 0):
        for track in s.video + s.audio:
            for item in track.items:
                if isinstance(item, XClip):
                    if item.sequence is not None and depth < MAX_NEST_DEPTH:
                        walk(item.sequence, depth + 1)
                    elif item.file is not None and item.file.path and not item.generator:
                        out.setdefault(item.file.path, None)

    walk(seq)
    return list(out)


# ---------------------------------------------------------------------------
# Planning (pure; times in seconds on the project's frame grid)
# ---------------------------------------------------------------------------

@dataclass
class ProjectInfo:
    """What planning needs from the open project (read on the GUI thread)."""

    fps: Fraction
    width: int
    height: int
    playhead: float = 0.0
    file_ids: Dict[str, str] = field(default_factory=dict)     # path -> existing file id
    file_data: Dict[str, dict] = field(default_factory=dict)   # path -> that file's data (media facts)


@dataclass
class PlannedTrack:
    key: str
    label: str
    kind: str
    locked: bool
    disabled: bool = False


@dataclass
class PlannedClip:
    track: str
    path: str
    title: str
    position: float
    start: float
    end: float
    props: Dict[str, Any]
    crop: Optional[Dict[str, Any]] = None
    file_id: Optional[str] = None       # an existing Project Files entry for the media

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class PlannedTransition:
    track: str
    position: float
    duration: float
    reverse: bool
    label: str
    audio: bool = False      # fade_audio_hint: libopenshot's equal-power audio cross fade


@dataclass
class PlannedMarker:
    position: float
    name: str
    color: str


@dataclass
class ImportPlan:
    source: str
    sequence_name: str
    tracks: List[PlannedTrack]
    clips: List[PlannedClip]
    transitions: List[PlannedTransition]
    markers: List[PlannedMarker]
    media: Dict[str, dict]           # path -> probed reader JSON (new files)
    missing: List[str]
    warnings: List[str]
    placement: str
    offset: float


class _Warn:
    def __init__(self):
        self.order: List[str] = []
        self.counts: Dict[str, int] = {}

    def add(self, msg: str, n: int = 1) -> None:
        if msg not in self.counts:
            self.order.append(msg)
            self.counts[msg] = 0
        self.counts[msg] += n

    def as_list(self) -> List[str]:
        return [m if self.counts[m] == 1 else f"{m} ({self.counts[m]} times)" for m in self.order]


def _point(x: float, y: float, interpolation: int = LINEAR) -> dict:
    p: dict = {"co": {"X": float(x), "Y": float(y)}, "interpolation": int(interpolation)}
    if interpolation == BEZIER:
        p.update({"handle_left": {"X": 0.5, "Y": 1.0}, "handle_right": {"X": 0.5, "Y": 0.0}, "handle_type": 0})
    return p


def _constant_kf(value: float) -> dict:
    return {"Points": [_point(1, value, BEZIER)]}


def _flag_kf(on: bool) -> dict:
    return {"Points": [_point(1, 1.0 if on else 0.0, CONSTANT)]}


def _ease(u: float) -> float:
    """libopenshot's BEZIER segment with Zenvi's default handles (cubic-bezier(0.5, 0, 0.5, 1)) at x = *u*."""
    lo, hi = 0.0, 1.0
    for _ in range(40):                       # x(s) = 1.5 s (1 - s) + s^3 is increasing
        mid = (lo + hi) / 2.0
        if 1.5 * mid * (1.0 - mid) + mid ** 3 < u:
            lo = mid
        else:
            hi = mid
    s = (lo + hi) / 2.0
    return 3.0 * s * s - 2.0 * s * s * s


class _Fn:
    """A Premiere parameter over the clip: keys (held outside them), or a constant.

    Segments are straight lines (Premiere's keys carry no easing) unless *interps* gives each key's
    libopenshot interpolation, which -- as in Zenvi -- shapes the segment that ends at that key.
    """

    def __init__(self, keys: Sequence[Tuple[float, Any]], value: Any, interps: Optional[Sequence[int]] = None):
        self.keys = list(keys)
        self.value = value
        self.interps = list(interps) if interps is not None and len(interps) == len(self.keys) else None

    def at(self, t: float) -> Any:
        keys = self.keys
        if not keys:
            return self.value
        if t <= keys[0][0]:
            return keys[0][1]
        if t >= keys[-1][0]:
            return keys[-1][1]
        for i, ((t0, v0), (t1, v1)) in enumerate(zip(keys, keys[1:])):
            if t0 <= t <= t1:
                f = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
                interp = self.interps[i + 1] if self.interps else LINEAR
                if interp == CONSTANT:
                    f = 1.0 if t >= t1 else 0.0
                elif interp == BEZIER:
                    f = _ease(f)
                if isinstance(v0, tuple):
                    return tuple(a + (b - a) * f for a, b in zip(v0, v1))
                return v0 + (v1 - v0) * f
        return keys[-1][1]

    def times(self) -> List[float]:
        return [k[0] for k in self.keys]

    def interp_map(self, shift: float = 0.0) -> Dict[float, int]:
        """{key time + *shift*: its interpolation} for the keys that are not straight lines."""
        if not self.interps:
            return {}
        return {t + shift: i for (t, _v), i in zip(self.keys, self.interps) if i != LINEAR}


class _Planner:
    def __init__(self, seq: XSequence, info: ProjectInfo, probes: Dict[str, dict], placement: str,
                 missing: Iterable[str], translate: Optional[Callable[[str], str]] = None):
        self._ = translate or (lambda text: text)      # warnings: English for agents, the app's _tr in menus
        self.seq = seq
        self.info = info
        self.fps = info.fps
        self.probes = probes
        self.missing = set(missing)
        self.placement = placement
        self.offset = ft.snap(info.playhead, info.fps) if placement == "at_playhead" else 0.0
        self.warn = _Warn()
        self.tracks: List[PlannedTrack] = []
        self.clips: List[PlannedClip] = []
        self.transitions: List[PlannedTransition] = []
        self.markers: List[PlannedMarker] = []
        self.k = min(info.width / float(seq.width or info.width), info.height / float(seq.height or info.height))
        self._audio_hint: set = set()      # id() of video transitions whose audio cross fade came along
        self.legacy = self._legacy_centers(seq)
        self.legacy_keys = self._legacy_keys(seq)
        self._channel_group: Dict[str, int] = {}    # audio item id -> items playing its channels together
        self._split_channels: set = set()           # channel items of one file kept apart (not in the same state)
        self._image_sequences: List[str] = []
        self.has_links = self._has_links(seq)       # the writer links its A/V items (Premiere, FCP7, Resolve)
        if abs(self.k - 1.0) > 1e-6:
            self.warn.add(self._("the sequence is %(seq)s and the project %(project)s; positions and sizes were "
                                 "scaled to fit") % {"seq": "%dx%d" % (seq.width, seq.height),
                                                     "project": "%dx%d" % (info.width, info.height)})
        if abs(float(seq.rate.fps) - float(info.fps)) > 1e-6:
            self.warn.add(self._("the sequence runs at %(seq).3f fps and the project at %(project).3f fps; times "
                                 "were snapped to the project's frames")
                          % {"seq": float(seq.rate.fps), "project": float(info.fps)})

    # -- helpers ---------------------------------------------------------------
    def snap(self, seconds: float) -> float:
        return ft.snap(max(0.0, float(seconds)), self.fps)

    def x_of(self, seconds_in_x_space: float) -> int:
        return ft.to_frame(seconds_in_x_space, self.fps) + 1

    @staticmethod
    def _legacy_centers(seq: XSequence) -> bool:
        def walk(s, depth=0):
            for track in s.video:
                for item in track.items:
                    if not isinstance(item, XClip):
                        continue
                    if item.sequence is not None and depth < MAX_NEST_DEPTH:
                        if walk(item.sequence, depth + 1):
                            return True
                    motion = item.effect("basic", "basicmotion")
                    if motion is not None and "center" in motion.params:
                        p = motion.params["center"]
                        values = [p.value] + [v for _w, v in p.keys]
                        if any(isinstance(v, tuple) and max(abs(v[0]), abs(v[1])) > LEGACY_CENTER_LIMIT
                               for v in values):
                            return True
            return False

        return walk(seq)

    @staticmethod
    def _has_links(seq: XSequence) -> bool:
        def walk(s, depth=0):
            for track in s.video + s.audio:
                for item in track.items:
                    if isinstance(item, XClip):
                        if item.links:
                            return True
                        if item.sequence is not None and depth < MAX_NEST_DEPTH and walk(item.sequence, depth + 1):
                            return True
            return False

        return walk(seq)

    @staticmethod
    def _legacy_keys(seq: XSequence) -> bool:
        """True for an old Zenvi / OpenShot export: its keyframes carry their own <interpolation>."""
        def walk(s, depth=0):
            for track in s.video + s.audio:
                for item in track.items:
                    if not isinstance(item, XClip):
                        continue
                    if item.sequence is not None and depth < MAX_NEST_DEPTH and walk(item.sequence, depth + 1):
                        return True
                    for e in item.effects:
                        for p in e.params.values():
                            if any(name in KEY_INTERPOLATIONS for name, _flags in p.meta):
                                return True
            return False

        return walk(seq)

    # -- tracks -------------------------------------------------------------------
    def _track(self, key: str, label: str, kind: str, locked: bool, disabled: bool = False) -> str:
        """Register a target track (in stacking order); only tracks that get clips are created."""
        if not any(t.key == key for t in self.tracks):
            self.tracks.append(PlannedTrack(key=key, label=label, kind=kind, locked=locked, disabled=disabled))
        return key

    # -- the sequence ---------------------------------------------------------------
    def plan(self) -> None:
        seq = self.seq
        if seq.others:
            self.warn.add(self._("the XML also holds the sequence(s) %(others)s; only '%(name)s' was imported")
                          % {"others": ", ".join("'%s'" % n for n in seq.others), "name": seq.name})
        self._plan_sequence(seq, shift=self.offset, window=None, prefix="", depth=0, disabled=False)
        if self._image_sequences:
            names = ", ".join("'%s'" % n for n in self._image_sequences[:3])
            if len(self._image_sequences) > 3:
                names += self._(" and %d more") % (len(self._image_sequences) - 3)
            self.warn.add(self._("%(count)d image sequence(s) came in as single still frames (%(names)s); import "
                                 "them into Zenvi as image sequences and replace those clips")
                          % {"count": len(self._image_sequences), "names": names})
        used = {c.track for c in self.clips} | {t.track for t in self.transitions}
        self.tracks = [t for t in self.tracks if t.key in used]
        for m in seq.markers:
            position = self.snap(seq.rate.seconds(m.in_) + self.offset)
            name = m.name or m.comment
            if m.name and m.comment and m.comment != m.name:
                name = f"{m.name}: {m.comment}"
            self.markers.append(PlannedMarker(position=position, name=name, color=_marker_color(m.color)))
            if m.out not in (-1, None) and m.out > m.in_:
                self.warn.add(self._("Zenvi markers are points; marker durations were dropped"))

    def _plan_sequence(self, seq: XSequence, *, shift: float, window: Optional[Tuple[float, float]], prefix: str,
                       depth: int, disabled: bool, audio_only: bool = False, nest_label: str = "") -> None:
        """Plan *seq*'s clips with sequence time t landing at ``t + shift`` (clipped to *window*)."""
        merged_audio: set = set()
        video_by_id: Dict[str, XClip] = {}
        audio_by_id = {i.id: i for t in seq.audio for i in t.items if isinstance(i, XClip)}
        if not audio_only:
            for track in seq.video:
                for item in track.items:
                    if isinstance(item, XClip):
                        video_by_id[item.id] = item
            for track in seq.video:
                label = (nest_label + " " if nest_label else "") + (track.name or f"V{track.number}")
                key = f"{prefix}V{track.number}"
                self._track(key, label, "video", track.locked, disabled or not track.enabled)
                if not track.enabled and any(isinstance(i, XClip) for i in track.items):
                    self.warn.add(self._("track %s was turned off in Premiere; its clips came in with their video "
                                         "off") % (track.name or "V%d" % track.number))
                for item in track.items:
                    if not isinstance(item, XClip):
                        continue
                    partner = self._audio_partner(seq, item, audio_by_id, merged_audio)
                    if partner is not None:
                        group = self._same_group(seq, item, partner, audio_by_id)
                        merged_audio.update(group)
                        self._channel_group[partner.id] = len(group)
                    self._plan_item(seq, item, track, key, label, shift, window, depth,
                                    disabled or not track.enabled, partner)
                    if item.sequence is not None:
                        merged_audio.update(self._nest_audio_ids(seq, item))
                self._plan_track_transitions(seq, track, key, shift, window)
        for track in seq.audio:
            if not track.primary:
                continue
            label = (nest_label + " " if nest_label else "") + (track.name or f"A{track.number}")
            key = f"{prefix}A{track.number}"
            self._track(key, label, "audio", track.locked, disabled or not track.enabled)
            if not track.enabled and any(isinstance(i, XClip) for i in track.items):
                self.warn.add(self._("track %s was muted in Premiere; its clips came in with their audio off")
                              % (track.name or "A%d" % track.number))
            for item in track.items:
                if not isinstance(item, XClip) or item.id in merged_audio:
                    continue
                # the other channels of the same sound (FCP7 / Resolve stereo pairs on two mono tracks,
                # Premiere's dual mono) are one Zenvi clip, not one clip per channel
                siblings = self._channel_siblings(seq, item, audio_by_id, merged_audio)
                merged_audio.update(siblings)
                self._channel_group[item.id] = 1 + len(siblings)
                self._plan_item(seq, item, track, key, label, shift, window, depth,
                                disabled or not track.enabled, None)
        # The second channel of an exploded stereo pair (currentExplodedTrackIndex 1) is the same clip
        # as the first; those tracks are skipped above.

    def _nest_audio_ids(self, seq: XSequence, item: XClip) -> List[str]:
        """Audio items that show the same nest (their content came in with the nest's video)."""
        out = []
        for track in seq.audio:
            for other in track.items:
                if isinstance(other, XClip) and other.sequence is not None and other.sequence is item.sequence:
                    if abs(self._media_span(seq, other)[0] - self._media_span(seq, item)[0]) < 1e-6:
                        out.append(other.id)
        return out

    def _media_span(self, seq: XSequence, item: XClip) -> Tuple[float, float]:
        """Timeline seconds the item's media covers (-1 edges resolved through their transitions)."""
        start = item.start if item.start != -1 else (item.prev.start if item.prev else item.start)
        end = item.end if item.end != -1 else (item.next.end if item.next else item.end)
        return seq.rate.seconds(start), seq.rate.seconds(end)

    def _source_in(self, seq: XSequence, item: XClip) -> float:
        rate = item.rate or seq.rate
        return rate.seconds(item.in_)

    def _audio_partner(self, seq: XSequence, video: XClip, audio_by_id: Dict[str, XClip],
                       taken: set) -> Optional[XClip]:
        """The audio item that plays this video clip's sound with the same timing (merged into one clip)."""
        if video.file is None or video.sequence is not None:
            return None
        span = self._media_span(seq, video)
        tol = 1.0 / float(seq.rate.fps) + 1e-6
        candidates = []
        linked = [cid for cid, mt in video.links if mt == "audio" or (cid in audio_by_id)]
        if linked:
            pool = [audio_by_id[c] for c in linked if c in audio_by_id]
        elif not self.has_links:
            # a writer that links nothing: match the sound by timing, but never a single channel of a file
            # (Zenvi's Separate Audio leaves the picture silent and plays each channel on its own)
            pool = [a for a in audio_by_id.values() if not self._channel_item(a)]
        else:
            return None       # this XML links its clips: an unlinked picture has no sound of its own
        for a in pool:
            if a.id in taken or a.file is None or not a.track.primary:
                continue
            if a.file.path != video.file.path or _speed_of(a) != _speed_of(video):
                continue
            a_span = self._media_span(seq, a)
            # the sound must sit inside the picture (a J/L cut that reaches outside stays separate) and show
            # the same media at the same time
            if a_span[0] < span[0] - tol or a_span[1] > span[1] + tol:
                continue
            if abs(self._source_in(seq, a) - (self._source_in(seq, video) + a_span[0] - span[0])) > tol:
                continue
            candidates.append(a)
        if not candidates:
            return None
        return min(candidates, key=lambda a: (-(self._media_span(seq, a)[1] - self._media_span(seq, a)[0]),
                                              a.track.number))

    def _same_group(self, seq: XSequence, video: XClip, audio: XClip, audio_by_id: Dict[str, XClip]) -> List[str]:
        """The video's sound item and the other channels of the same sound (linked to either, same state)."""
        return [audio.id] + self._channel_siblings(seq, audio, audio_by_id, set(), also=video)

    def _warn_image_sequence(self, item: XClip, reader: dict) -> None:
        """Premiere links an image sequence by its first frame; Zenvi reads that file as one still."""
        xf = item.file
        if xf is None or not (reader.get("media_type") == "image" or reader.get("has_single_image")):
            return
        if xf.duration and 1 < xf.duration < 1000000 and re.search(STILL_SEQUENCE_RE, xf.name or xf.path, re.I):
            name = xf.name or os.path.basename(xf.path)
            if name not in self._image_sequences:
                self._image_sequences.append(name)

    def _channel_siblings(self, seq: XSequence, item: XClip, audio_by_id: Dict[str, XClip],
                          taken: set, also: Optional[XClip] = None) -> List[str]:
        """Audio items that play other channels of *item*'s media with the same timing, in the same state.

        Candidates are the items linked to *item* (or to *also*, its picture). Channel items in another
        state (off, muted track, other Audio Levels) stay separate clips, each playing its own channel;
        so do all of them when the writer links nothing (Zenvi's own Separate Audio clips come back as
        they were). The two halves of an exploded stereo pair are one clip whatever they say.
        """
        if item.file is None or item.sequence is not None or not self._channelable(item):
            return []
        if self.has_links:
            refs = {cid for cid, _mt in item.links} | {o.id for o in audio_by_id.values()
                                                        if any(cid == item.id for cid, _mt in o.links)}
            if also is not None:
                refs |= {cid for cid, _mt in also.links}
            candidates = [audio_by_id[c] for c in sorted(refs) if c in audio_by_id]
        else:
            candidates = list(audio_by_id.values())
        span = self._media_span(seq, item)
        tol = 1.0 / float(seq.rate.fps) + 1e-6
        out = []
        for other in candidates:
            if other is item or other.id in taken or other.file is None or not self._channelable(other):
                continue
            if other.file.path != item.file.path or other.sourcetrack == item.sourcetrack:
                continue
            if _speed_of(other) != _speed_of(item):
                continue
            o_span = self._media_span(seq, other)
            if abs(o_span[0] - span[0]) > tol or abs(o_span[1] - span[1]) > tol:
                continue
            if abs(self._source_in(seq, other) - self._source_in(seq, item)) > tol:
                continue
            exploded = item.track.exploded_count > 1 and other.track.exploded_count > 1
            if exploded or (self.has_links and _same_state(item, other)):
                out.append(other.id)
            else:
                self._split_channels.update((item.id, other.id))
        return out

    def _channelable(self, item: XClip) -> bool:
        """An item that may stand for one channel: no stereo / adaptive type, and not from a mono file."""
        if item.channel_type not in ("", "mono"):
            return False
        return not (item.file is not None and item.file.channels is not None and item.file.channels < 2)

    def _channels_of(self, item: XClip) -> int:
        """The media's channel count: what the XML says, else what the probe read."""
        if item.file is not None and item.file.channels is not None:
            return int(item.file.channels)
        try:
            return int((self.probes.get(item.file.path) or {}).get("channels") or 0) if item.file else 0
        except (TypeError, ValueError):
            return 0

    def _channel_item(self, item: XClip) -> bool:
        """An item that plays one channel of a multi-channel file (not the file's whole sound)."""
        return item.sourcetrack >= 2 or (item.channel_type == "mono" and self._channels_of(item) >= 2)

    def _single_channel(self, item: XClip, reader: dict) -> Optional[int]:
        """The media channel (0-based) an audio item plays on its own, or None when it plays them all.

        One mono item taken from a multi-channel file (Premiere / FCP7 mono tracks, or Zenvi's own
        "each channel" clips) plays only that channel; an exploded stereo pair, a stereo item or a
        group of channel siblings plays the whole file.
        """
        if self._channel_group.get(item.id, 1) > 1 or item.track.exploded_count > 1:
            return None
        try:
            channels = int(reader.get("channels") or 0)
        except (TypeError, ValueError):
            channels = 0
        k = item.sourcetrack - 1
        if not self._channelable(item):                # stereo / adaptive items, or the XML says the file is mono
            return None
        if item.id in self._split_channels:            # one of a channel pair kept apart: its own channel
            return k if channels >= 2 and 0 <= k < channels else None
        if item.channel_type != "mono" and item.sourcetrack < 2:
            # DaVinci Resolve and FCP7 write one item per stereo clip, from the first channel: the whole file
            return None
        return k if channels >= 2 and 0 <= k < channels else None

    # -- one clip ---------------------------------------------------------------------
    def _plan_item(self, seq: XSequence, item: XClip, track: XTrack, key: str, label: str, shift: float,
                   window: Optional[Tuple[float, float]], depth: int, disabled: bool, partner: Optional[XClip]):
        if item.sequence is not None:
            self._plan_nest(seq, item, track, key, label, shift, window, depth, disabled)
            return
        if item.generator or item.file is None:
            what = item.name or "generator"
            self.warn.add(self._("generators and graphics (titles, colour mattes, bars) are not imported: '%s'")
                          % what)
            return
        path = item.file.path
        if not path or path in self.missing or path not in self.probes:
            return   # listed in missing
        if item.markers:
            self.warn.add(self._("clip markers are not imported (Zenvi has timeline markers only)"), item.markers)
        reader = self.probes.get(path) or {}
        m_start, m_end = self._media_span(seq, item)
        if m_end - m_start <= 1e-9:
            return
        position = self.snap(m_start + shift)
        duration = self.snap(m_end + shift) - position
        if duration <= 0:
            return
        props: Dict[str, Any] = {}
        start, end, time_points = self._timing(seq, item, reader, duration)
        if time_points:
            props["time"] = {"Points": time_points}
        video_on = item.enabled and not disabled
        media_has_video = bool(reader.get("has_video", item.file.has_video)) and reader.get("media_type") != "audio"
        media_has_audio = bool(reader.get("has_audio", item.file.has_audio))
        if item.kind == "video":
            if not video_on:
                props["has_video"] = _flag_kf(False)
            if partner is not None:
                audio_on = partner.enabled and partner.track.enabled and not disabled
                if not audio_on and media_has_audio:
                    props["has_audio"] = _flag_kf(False)
            elif media_has_audio:
                props["has_audio"] = _flag_kf(False)
        else:
            if media_has_video:
                props["has_video"] = _flag_kf(False)
            if not video_on:
                props["has_audio"] = _flag_kf(False)
        if item.kind == "video":
            self._video_props(seq, item, reader, props, start)
        audio_item = partner if item.kind == "video" else item
        if audio_item is not None:
            offset, gate, absorbed = 0.0, None, set()
            if partner is not None:
                a_start, a_end = self._media_span(seq, partner)
                offset = a_start - m_start
                if offset > 1e-9 or a_end < m_end - 1e-9:
                    gate = (offset, a_end - m_start)
                for vt, at in ((item.prev, partner.prev), (item.next, partner.next)):
                    if vt is not None and at is not None and at.mediatype == "audio" and \
                            (vt.start, vt.end) == (at.start, at.end):
                        self._audio_hint.add(id(vt))     # libopenshot cross fades the audio itself
                        absorbed.add(id(at))
            self._volume_props(seq, audio_item, props, start, duration, offset=offset, gate=gate,
                               absorbed=absorbed)
            channel = self._single_channel(audio_item, reader)
            if channel is not None and "has_audio" not in props:
                props["channel_filter"] = {"Points": [_point(1, channel, CONSTANT)]}
                self.warn.add(self._("a clip that uses one channel of a stereo file plays only that channel, on "
                                     "its own side (Premiere centres a mono item)"))
        self._warn_image_sequence(item, reader)
        planned = PlannedClip(track=key, path=path, title=item.name or os.path.basename(path), position=position,
                              start=start, end=start + duration, props=props, file_id=reader.get("_existing"))
        planned.crop = self._crop(seq, item, start) if item.kind == "video" else None
        if window is not None:
            planned = self._clip_to_window(planned, (window[0] + shift, window[1] + shift))
            if planned is None:
                return
        self.clips.append(planned)

    def _clip_to_window(self, c: PlannedClip, window: Tuple[float, float]) -> Optional[PlannedClip]:
        lo, hi = self.snap(window[0]), self.snap(window[1])
        a, b = c.position, c.position + c.duration
        if b <= lo + 1e-9 or a >= hi - 1e-9:
            return None
        if a < lo:
            delta = lo - a
            c.position, c.start = lo, c.start + delta     # X space: trimming never touches the curves
        if b > hi:
            c.end = c.end - (b - hi)
        return c

    def _timing(self, seq: XSequence, item: XClip, reader: dict, duration: float):
        """(start, end, time points) of the Zenvi clip: Zenvi's own Speed menu shapes."""
        rate = item.rate or seq.rate
        remap = item.effect("timeremap")
        f = 1.0
        reverse = variable = False
        if remap is not None:
            speed = remap.num("speed", 100.0)
            variable = remap.flag("variablespeed")
            reverse = remap.flag("reverse") or speed < 0
            f = abs(speed) / 100.0 if speed else 0.0
        media_seconds = float(reader.get("duration") or 0.0)
        if reader.get("media_type") == "image" or reader.get("has_single_image"):
            return 0.0, duration, None      # a still: Zenvi images start at 0 (Premiere uses huge in points)
        if not media_seconds and item.file is not None and item.file.duration:
            media_seconds = (item.file.rate or rate).seconds(item.file.duration)
        # where the media starts (start == -1: its in point sits at the transition start)
        if remap is None or (not variable and abs(f - 1.0) < 1e-6 and not reverse):
            src_in = rate.seconds(item.in_)
            if item.ticks_in is not None and remap is None:
                ticks = item.ticks_in / float(TICKS_PER_SECOND)
                if abs(ticks - src_in) < 1.0 / float(rate.fps):
                    src_in = ticks
            start = self.snap(src_in)
            return start, start + duration, None
        if variable:
            keys = _remap_keys(remap.params.get("graphdict"))
            if len(keys) < 2:
                self.warn.add(self._("a variable-speed clip had no speed keyframes; imported at normal speed"))
                start = self.snap(rate.seconds(item.in_))
                return start, start + duration, None
            fn = _Fn(keys, keys[0][1])
            n = max(1, ft.to_frame(duration, self.fps))
            src0 = rate.seconds(fn.at(item.in_))
            start = self.snap(src0)
            sx = self.x_of(start)
            whens = sorted({w for w, _v in keys if item.in_ < w < item.in_ + n * float(rate.fps) / float(self.fps)})
            pts = [(sx, ft.to_frame(src0, self.fps) + 1)]
            for w in whens:
                x = sx + ft.to_frame(rate.seconds(w - item.in_), self.fps)
                pts.append((x, ft.to_frame(rate.seconds(fn.at(w)), self.fps) + 1))
            end_when = item.in_ + duration * float(rate.fps)
            pts.append((sx + n, ft.to_frame(rate.seconds(fn.at(end_when)), self.fps) + 1))
            dedup: Dict[int, int] = {}
            for x, y in pts:
                dedup[int(x)] = max(1, int(y))
            points = [_point(x, y, LINEAR) for x, y in sorted(dedup.items())]
            self.warn.add(self._("variable speed (time remapping) was imported as straight-line speed changes"))
            return start, start + duration, points if len(points) > 1 else None
        if f <= 0:
            start = self.snap(rate.seconds(item.in_))
            hold = ft.to_frame(start, self.fps) + 1
            sx = self.x_of(start)
            n = max(1, ft.to_frame(duration, self.fps))
            return start, start + duration, [_point(sx, hold, LINEAR), _point(sx + n, hold, LINEAR)]
        n = max(1, ft.to_frame(duration, self.fps))
        if reverse:
            total = media_seconds
            if total <= 0:
                self.warn.add(self._("a reversed clip's media length is unknown; imported forward"))
                reverse = False
            else:
                hi = total - rate.seconds(item.in_) * f
                lo = max(0.0, hi - duration * f)
                start = self.snap(lo)
                sx = self.x_of(start)
                y_hi = max(sx, ft.to_frame(hi, self.fps))
                return start, start + duration, [_point(sx, y_hi, LINEAR), _point(sx + n, sx, LINEAR)]
        src_in = rate.seconds(item.in_) * f
        if item.ticks_in is not None:
            ticks = item.ticks_in / float(TICKS_PER_SECOND)
            if abs(ticks - src_in) < 2.0 * f / float(rate.fps) + 1e-6:
                src_in = ticks
        start = self.snap(src_in)
        sx = self.x_of(start)
        y_end = max(sx, ft.to_frame(start + duration * f, self.fps))
        return start, start + duration, [_point(sx, sx, LINEAR), _point(sx + n, y_end, LINEAR)]

    # -- keyframes ----------------------------------------------------------------------
    def _local(self, seq: XSequence, item: XClip, when: float) -> float:
        """Seconds after the clip's media start of an in/out-space ``when``."""
        rate = item.rate or seq.rate
        in_ = item.in_
        if self.legacy_keys:
            when -= 1                 # old Zenvi exports wrote libopenshot's 1-based clip frame as when
        return rate.seconds(when - in_)

    def _fn(self, seq: XSequence, item: XClip, param: Optional[XParam], default: Any,
            convert: Callable[[Any], Any] = lambda v: v) -> _Fn:
        if param is None:
            return _Fn([], default)
        static = param.value
        if isinstance(default, tuple) and not isinstance(static, tuple):
            static = default
        elif not isinstance(default, tuple):
            static = _float(static, default)
        keys = [(self._local(seq, item, w), convert(v)) for w, v in param.keys]
        interps: Optional[List[int]] = None
        if param.interpolation in ("hold", "constant"):
            interps = [CONSTANT] * len(keys)
        elif any(name for name, _flags in param.meta):
            interps = [KEY_INTERPOLATIONS.get(name, LINEAR) for name, _flags in param.meta]
        return _Fn(keys, convert(static), interps)

    def _curve(self, times: List[float], values: List[float], start: float, default: float,
               interps: Optional[Dict[float, int]] = None) -> Optional[dict]:
        """Zenvi keyframes at clip-local *times* (LINEAR, or the key's *interps*); None when it is the default."""
        sx = self.x_of(start)
        pts: Dict[int, Tuple[float, int]] = {}
        for t, v in zip(times, values):
            pts[sx + ft.to_frame(max(0.0, t), self.fps)] = (v, (interps or {}).get(t, LINEAR))
        if not pts:
            return None
        vals = [v for v, _i in pts.values()]
        if all(abs(v - vals[0]) < 1e-9 for v in vals):
            return None if abs(vals[0] - default) < 1e-9 else _constant_kf(vals[0])
        ordered = [(x, v, i) for x, (v, i) in sorted(pts.items())]
        # a key that only repeats its neighbour at either end adds nothing (curves hold outside their keys),
        # nor does one on the straight line between its neighbours
        while len(ordered) > 2 and abs(ordered[-1][1] - ordered[-2][1]) < 1e-9:
            ordered.pop()
        while len(ordered) > 2 and abs(ordered[0][1] - ordered[1][1]) < 1e-9:
            ordered.pop(0)
        i = 1
        while i < len(ordered) - 1:
            (x0, v0, _i0), (x1, v1, i1), (x2, v2, i2) = ordered[i - 1], ordered[i], ordered[i + 1]
            if i1 == LINEAR and i2 == LINEAR and x2 > x0 and abs(v0 + (v2 - v0) * (x1 - x0) / (x2 - x0) - v1) < 1e-9:
                ordered.pop(i)
            else:
                i += 1
        return {"Points": [_point(x, v, interp) for x, v, interp in ordered]}

    def _video_props(self, seq: XSequence, item: XClip, reader: dict, props: dict, start: float) -> None:
        opacity = item.effect("opacity")
        if opacity is not None:
            fn = self._fn(seq, item, opacity.params.get("opacity"), 100.0, lambda v: _float(v, 100.0))
            times = _clip_times(fn.times())
            curve = self._curve(times, [max(0.0, min(1.0, fn.at(t) / 100.0)) for t in times], start, 1.0,
                                fn.interp_map())
            if curve is not None:
                props["alpha"] = curve
        motion = item.effect("basic", "basicmotion")
        distort = item.effect("deformation", "distort")
        for e in item.effects:
            eid = e.effectid.lower()
            if eid in ("basic", "basicmotion", "opacity", "timeremap", "deformation", "distort", "crop"):
                continue
            if not (e.name or e.effectid):
                continue          # an empty placeholder Premiere writes: nothing to carry over
            self.warn.add(self._("Premiere effect '%s' has no Zenvi equivalent; left out") % (e.name or e.effectid))
        self._motion(seq, item, reader, motion, distort, props, start)

    def _motion(self, seq: XSequence, item: XClip, reader: dict, motion: Optional[XEffect],
                distort: Optional[XEffect], props: dict, start: float) -> None:
        info = self.info
        # the media's size as Premiere knew it (center is normalised by it) and as libopenshot reads it
        sw = float(item.file.width if item.file and item.file.width else reader.get("width") or seq.width)
        sh = float(item.file.height if item.file and item.file.height else reader.get("height") or seq.height)
        aw = int(reader.get("width") or sw or info.width)
        ah = int(reader.get("height") or sh or info.height)
        if reader.get("media_type") == "audio" or not aw or not ah:
            return
        fit_w, fit_h = qsize_scaled(aw, ah, int(info.width), int(info.height), "keep")
        params = motion.params if motion is not None else {}
        scale = self._fn(seq, item, params.get("scale"), 100.0, lambda v: _float(v, 100.0))
        rotation = self._fn(seq, item, params.get("rotation"), 0.0, lambda v: _float(v, 0.0))
        center = self._fn(seq, item, params.get("center"), (0.0, 0.0))
        anchor = self._fn(seq, item, params.get("centerOffset"), (0.0, 0.0))
        aspect = self._fn(seq, item, (distort.params.get("aspect") if distort is not None else None), 0.0,
                          lambda v: _float(v, 0.0))
        times = _clip_times(scale.times() + rotation.times() + center.times() + anchor.times() + aspect.times())
        k = self.k
        wx, hx = float(seq.width), float(seq.height)
        rows = {key: [] for key in ("scale_x", "scale_y", "location_x", "location_y", "rotation", "origin_x",
                                    "origin_y")}
        for t in times:
            s = scale.at(t) / 100.0
            asp = aspect.at(t) / 100.0
            cx, cy = center.at(t)
            ax, ay = anchor.at(t)
            if self.legacy:
                # an old OpenShot export: centre in canvas pixels, scale relative to the fit size
                disp_w, disp_h = fit_w * s, fit_h * s
                px = (cx - wx / 2.0) * k + info.width / 2.0
                py = (cy - hx / 2.0) * k + info.height / 2.0
                ox = oy = 0.5
                px, py = px - disp_w * (0.5 - ox), py - disp_h * (0.5 - oy)
            else:
                disp_w = s * (1.0 - asp) * sw * k
                disp_h = s * sh * k
                px = (wx / 2.0 + (cx / max(1e-9, seq.par) + ax) * sw - wx / 2.0) * k + info.width / 2.0
                py = (hx / 2.0 + (cy + ay) * sh - hx / 2.0) * k + info.height / 2.0
                ox, oy = ax + 0.5, ay + 0.5
            lx = (px - disp_w * ox - (info.width - disp_w) / 2.0) / float(info.width)
            ly = (py - disp_h * oy - (info.height - disp_h) / 2.0) / float(info.height)
            rows["scale_x"].append(disp_w / float(fit_w))
            rows["scale_y"].append(disp_h / float(fit_h))
            rows["location_x"].append(lx)
            rows["location_y"].append(ly)
            rows["rotation"].append(rotation.at(t))
            rows["origin_x"].append(ox)
            rows["origin_y"].append(oy)
        defaults = {"scale_x": 1.0, "scale_y": 1.0, "location_x": 0.0, "location_y": 0.0, "rotation": 0.0,
                    "origin_x": 0.5, "origin_y": 0.5}
        shapes = {"scale_x": scale.interp_map(), "scale_y": scale.interp_map(), "location_x": center.interp_map(),
                  "location_y": center.interp_map(), "rotation": rotation.interp_map(),
                  "origin_x": anchor.interp_map(), "origin_y": anchor.interp_map()}
        for key, values in rows.items():
            values = [0.0 if abs(v) < 1e-12 else v for v in values]
            curve = self._curve(times, values, start, defaults[key], shapes[key])
            if curve is not None:
                props[key] = curve
        props.setdefault("scale", 1)
        props.setdefault("gravity", 4)
        if motion is not None:
            for pid in ("antiflicker",):
                if _float(params[pid].value if pid in params else 0.0) > 0:
                    self.warn.add(self._("Premiere's anti-flicker filter has no Zenvi equivalent; left out"))

    def _crop(self, seq: XSequence, item: XClip, start: float) -> Optional[dict]:
        crop = item.effect("crop")
        if crop is None:
            return None
        out = {}
        for side in ("left", "right", "top", "bottom"):
            fn = self._fn(seq, item, crop.params.get(side), 0.0, lambda v: _float(v, 0.0))
            times = _clip_times(fn.times())
            curve = self._curve(times, [max(0.0, min(1.0, fn.at(t) / 100.0)) for t in times], start, -1.0,
                                fn.interp_map())
            out[side] = curve or _constant_kf(0.0)
        return out

    def _volume_props(self, seq: XSequence, item: XClip, props: dict, start: float, duration: float, *,
                      offset: float = 0.0, gate: Optional[Tuple[float, float]] = None,
                      absorbed: Iterable[int] = ()) -> None:
        """Zenvi volume from Audio Levels, audio fades and (merged sound shorter than the picture) a gate.

        *offset* is where the audio item's media starts on the clip (clip-local seconds); outside
        *gate* the clip is silent, as Premiere played no sound there.
        """
        levels = item.effect("audiolevels")
        fn = self._fn(seq, item, levels.params.get("level") if levels is not None else None, 1.0,
                      lambda v: _float(v, 1.0))
        for e in item.effects:
            if e.effectid.lower() not in ("audiolevels", "timeremap") and (e.name or e.effectid):
                self.warn.add(self._("Premiere audio effect '%s' has no Zenvi equivalent; left out")
                              % (e.name or e.effectid))
        skip = set(absorbed)
        fades = [(f0 + offset, f1 + offset, kind, plus3) for f0, f1, kind, plus3, tr in self._audio_fades(seq, item)
                 if id(tr) not in skip]
        frame = 1.0 / float(self.fps)
        times = {t + offset for t in fn.times()} | {0.0}
        for f0, f1, kind, _plus3 in fades:
            times |= {f0, f1, (f0 + f1) / 2.0}
        if gate is not None:
            if gate[0] > 1e-9:
                times |= {gate[0] - frame, gate[0]}
            if gate[1] < duration - 1e-9:
                times |= {gate[1] - frame, gate[1]}
        times_sorted = sorted(t for t in times if -1e-9 <= t <= duration + 1e-9)

        def gain(t: float) -> float:
            if gate is not None and not (gate[0] - 1e-9 <= t < gate[1] - 1e-9):
                return 0.0
            g = max(0.0, fn.at(t - offset))
            for f0, f1, kind, plus3 in fades:
                if f1 <= f0:
                    continue
                x = min(1.0, max(0.0, (t - f0) / (f1 - f0)))
                ramp = x if kind == "in" else 1.0 - x
                if plus3:
                    ramp = math.sin(ramp * math.pi / 2.0)   # constant power, like Cross Fade (+3dB)
                g *= ramp
            return g

        curve = self._curve(times_sorted, [gain(t) for t in times_sorted], start, 1.0, fn.interp_map(offset))
        if curve is not None:
            props["volume"] = curve

    def _audio_fades(self, seq: XSequence, item: XClip) -> List[Tuple[float, float, str, bool, XTransition]]:
        """Item-local (start, end, in|out, constant power, transition) fades from audio transitions at its edges."""
        out = []
        m_start, _m_end = self._media_span(seq, item)
        for tr, side in ((item.prev, "head"), (item.next, "tail")):
            if tr is None or tr.mediatype != "audio":
                continue
            t0 = seq.rate.seconds(tr.start) - m_start
            t1 = seq.rate.seconds(tr.end) - m_start
            plus3 = "3db" in (tr.effectid + tr.name).lower() and "0db" not in (tr.effectid + tr.name).lower()
            out.append((t0, t1, "in" if side == "head" else "out", plus3, tr))
        return out

    # -- transitions -----------------------------------------------------------------------
    def _plan_track_transitions(self, seq: XSequence, track: XTrack, key: str, shift: float,
                                window: Optional[Tuple[float, float]]) -> None:
        for i, item in enumerate(track.items):
            if not isinstance(item, XTransition):
                continue
            before = track.items[i - 1] if i > 0 else None
            after = track.items[i + 1] if i + 1 < len(track.items) else None
            # the clips this transition belongs to: a black-aligned fade has one side only
            if item.alignment == "end-black":
                joined = [before]
            elif item.alignment == "start-black":
                joined = [after]
            else:
                joined = [before, after]
            joined = [x for x in joined if isinstance(x, XClip) and (x.end == -1 or x.start == -1)]
            if any(x.sequence is not None or x.generator for x in joined):
                self.warn.add(self._("transitions next to nested sequences or generators were not imported"))
                continue
            if not any(x.file is not None and x.file.path in self.probes for x in joined):
                continue
            position = seq.rate.seconds(item.start) + shift
            end = seq.rate.seconds(item.end) + shift
            if window is not None:
                lo, hi = window[0] + shift, window[1] + shift
                if position < lo - 1e-9 or end > hi + 1e-9:
                    continue
            name = item.name or item.effectid or "transition"
            if "dissolve" not in name.lower() and "cross" not in name.lower():
                self.warn.add(self._("Premiere transition '%s' became a fade") % name)
            if item.alignment == "end-black":
                reverse = True                      # the clip before fades out (even into a dip to black)
            elif item.alignment == "start-black":
                reverse = False                     # the clip after fades in
            else:
                reverse = isinstance(before, XClip) and not isinstance(after, XClip)
            p = self.snap(position)
            self.transitions.append(PlannedTransition(track=key, position=p, duration=max(
                1.0 / float(self.fps), self.snap(end) - p), reverse=reverse, label=name,
                audio=id(item) in self._audio_hint))

    # -- nests ---------------------------------------------------------------------------------
    def _plan_nest(self, seq: XSequence, item: XClip, track: XTrack, key: str, label: str, shift: float,
                   window: Optional[Tuple[float, float]], depth: int, disabled: bool) -> None:
        nested = item.sequence
        assert nested is not None
        if depth + 1 > MAX_NEST_DEPTH:
            self.warn.add(self._("nested sequence '%(name)s' is more than %(depth)d levels deep; skipped")
                          % {"name": nested.name, "depth": MAX_NEST_DEPTH})
            return
        m_start, m_end = self._media_span(seq, item)
        rate = item.rate or nested.rate
        n_in = rate.seconds(item.in_)
        n_out = n_in + (m_end - m_start)
        inner_window = (n_in, n_out)
        if window is not None:   # a nest inside a nest: also respect the outer window
            outer = (window[0] - (m_start - n_in), window[1] - (m_start - n_in))
            inner_window = (max(inner_window[0], outer[0]), min(inner_window[1], outer[1]))
        for e in item.effects:
            eid = e.effectid.lower()
            if eid in ("basic", "basicmotion", "opacity", "timeremap", "deformation", "crop") and not _identity(e):
                self.warn.add(self._("nested sequence '%(name)s' had its own %(effect)s in Premiere; its clips came "
                                     "in without it") % {"name": nested.name, "effect": e.name or e.effectid})
        self.warn.add(self._("nested sequence '%s' was flattened onto its own tracks") % nested.name)
        prefix = f"{key}>{nested.id or nested.name}#"
        self._plan_sequence(nested, shift=shift + m_start - n_in, window=inner_window,
                            prefix=prefix, depth=depth + 1, disabled=disabled or not item.enabled,
                            audio_only=item.kind == "audio", nest_label=nested.name or "Nest")

    # -- results --------------------------------------------------------------------------------
    def result(self, source: str, media: Dict[str, dict], missing: List[str]) -> ImportPlan:
        # order: audio tracks under the video (A1 right below V1, like Premiere), nests above their track
        video = [t for t in self.tracks if t.kind == "video"]
        audio = [t for t in self.tracks if t.kind == "audio"]
        ordered = list(reversed(audio)) + video
        return ImportPlan(source=source, sequence_name=self.seq.name, tracks=ordered, clips=self.clips,
                          transitions=self.transitions, markers=self.markers, media=media, missing=missing,
                          warnings=self.warn.as_list(), placement=self.placement, offset=self.offset)


def _remap_keys(graph: Optional[XParam]) -> List[Tuple[float, float]]:
    """The (when, media frame) keys that shape a Time Remap graph.

    Premiere's "virtual" keys mark the media start / end and the clip's in / out on the graph
    (``speedkfstart`` .. ``speedkfend``). They normally sit on the curve -- the in / out ones give its
    exact value where the clip starts and ends, inside a curved ramp too -- but real exports sometimes
    carry junk there (``speedkfout`` = -1815255671), which scrubbed the end of the clip back to frame
    one. A value outside ``valuemin``..``valuemax`` (the media's frames) is junk and left out; the rest
    are kept inside that range.
    """
    if graph is None:
        return []
    lo, hi = graph.valuemin, graph.valuemax
    bounded = hi is not None and hi > (lo or 0.0)
    out = []
    for when, value in graph.keys:
        if isinstance(value, tuple):
            continue
        if (lo is not None and value < lo - 1.0) or (bounded and hi is not None and value > hi + 1.0):
            continue
        if lo is not None:
            value = max(lo, value)
        if bounded and hi is not None:
            value = min(hi, value)
        out.append((when, value))
    return out


def _same_state(a: XClip, b: XClip) -> bool:
    """Two audio items that would sound alike: both on or both off, the same track state, the same levels."""
    def levels(item: XClip):
        e = item.effect("audiolevels")
        p = e.params.get("level") if e is not None else None
        return None if p is None else (str(p.value), tuple(p.keys))
    return (a.enabled, a.track.enabled, levels(a)) == (b.enabled, b.track.enabled, levels(b))


def _speed_of(item: XClip) -> Tuple[float, bool, bool]:
    """(speed %, reverse, variable) of an item's Time Remap ((100, False, False) without one)."""
    remap = item.effect("timeremap")
    if remap is None:
        return 100.0, False, False
    return round(abs(remap.num("speed", 100.0)), 3), remap.flag("reverse") or remap.num("speed", 100.0) < 0, \
        remap.flag("variablespeed")


def _clip_times(times: Iterable[float]) -> List[float]:
    """Key times from the clip's first frame on (keys before it collapse into its value at 0)."""
    return sorted({0.0} | {t for t in times if t > 0.0})


def _identity(effect: XEffect) -> bool:
    """True when a motion/opacity/speed effect changes nothing."""
    eid = effect.effectid.lower()
    for pid, p in effect.params.items():
        if p.keys:
            return False
        v = p.value
        if eid in ("basic", "basicmotion"):
            if pid == "scale" and abs(_float(v, 100.0) - 100.0) > 1e-6:
                return False
            if pid == "rotation" and abs(_float(v, 0.0)) > 1e-6:
                return False
            if pid in ("center", "centerOffset") and isinstance(v, tuple) and max(abs(v[0]), abs(v[1])) > 1e-6:
                return False
        if eid == "opacity" and pid == "opacity" and abs(_float(v, 100.0) - 100.0) > 1e-6:
            return False
        if eid == "timeremap" and pid == "speed" and abs(_float(v, 100.0) - 100.0) > 1e-6:
            return False
        if eid == "timeremap" and pid == "reverse" and str(v).upper() == "TRUE":
            return False
    return True


def _marker_color(value: Optional[int]) -> str:
    """A Zenvi marker colour for a Premiere pproColor (0xAABBGGRR); no colour = Premiere's green."""
    if value is None:
        return "green"
    for name, ppro in PPRO_MARKER_COLORS.items():
        if ppro == value and name != "pink":
            return name
    r, g, b = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    return min(ZENVI_MARKER_RGB, key=lambda n: sum((x - y) ** 2 for x, y in zip(ZENVI_MARKER_RGB[n], (r, g, b))))


# ---------------------------------------------------------------------------
# The blocking steps (off the GUI thread) and the commit (GUI thread)
# ---------------------------------------------------------------------------

def read_project_info() -> ProjectInfo:
    """The open project's frame rate, size, playhead and files. GUI thread (cheap)."""
    from classes.app import get_app
    project = get_app().project
    fps = project.get("fps") or {"num": 30, "den": 1}
    try:
        rate = Fraction(int(fps.get("num") or 30), int(fps.get("den") or 1))
    except (TypeError, ValueError, ZeroDivisionError):
        rate = Fraction(30)
    try:
        playhead = (float(get_app().window.preview_thread.player.Position()) - 1.0) / float(rate)
    except Exception:
        playhead = 0.0
    files, data = {}, {}
    for f in project.get("files") or []:
        if isinstance(f, dict) and f.get("path") and f.get("id"):
            key = os.path.normpath(str(f["path"]))
            files[key] = str(f["id"])
            data[key] = {k: copy.deepcopy(v) for k, v in f.items() if k != "ui"}
    return ProjectInfo(fps=rate, width=int(project.get("width") or 1920), height=int(project.get("height") or 1080),
                       playhead=max(0.0, playhead), file_ids=files, file_data=data)


def locate_media(paths: Iterable[str], remap: Optional[Dict[str, str]] = None,
                 search_dirs: Iterable[str] = ()) -> Tuple[Dict[str, str], List[str]]:
    """({xml path: path on disk}, missing): the exact path, the user's remap, else the same file name in
    *search_dirs* (the XML's folder, its ``<stem>_media`` folder) and the folders the user already pointed
    Zenvi at (Find Missing File). Filesystem checks: call off the GUI thread."""
    # Find Missing File's folders, when that dialog module is loaded (the app loads it at startup);
    # importing it here would bind its get_app outside the app.
    find_file = sys.modules.get("windows.views.find_file")
    known = [str(k) for k in (getattr(find_file, "known_paths", None) or [])] if find_file else []
    folders = [d for d in list(search_dirs) + known if d]
    found, missing = {}, []
    for path in paths:
        target = (remap or {}).get(path, path)
        hit = target if target and os.path.isfile(target) else None
        if hit is None:
            name = os.path.basename(target.replace("\\", "/"))
            for folder in folders:
                candidate = os.path.join(folder, name)
                if name and os.path.isfile(candidate):
                    hit = candidate
                    break
        if hit is None:
            missing.append(path)
        else:
            found[path] = os.path.normpath(hit)
    return found, missing


def plan_import(path: str, *, placement: str = "new_tracks", info: Optional[ProjectInfo] = None,
                remap: Optional[Dict[str, str]] = None, probe: Optional[Callable[[str], dict]] = None,
                should_cancel: Optional[Callable[[], bool]] = None,
                translate: Optional[Callable[[str], str]] = None, sequence: str = "") -> ImportPlan:
    """Parse, find and probe the media, and plan the import (blocking; off the GUI thread).

    *sequence* picks a sequence by name when the XML holds several (default: the first top-level one).
    """
    if placement not in ("new_tracks", "at_playhead"):
        raise XmlImportError("placement must be new_tracks or at_playhead")
    path = os.path.abspath(os.path.expanduser(str(path or "")))
    if not os.path.isfile(path):
        raise XmlImportError(f"no XML file at {path}")
    seq = parse_xml(path, sequence)
    if info is None:
        from classes.qt_main_thread import call_on_gui
        info = cast(ProjectInfo, call_on_gui(read_project_info, timeout=30))
    wanted = media_paths(seq)
    xml_dir = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    found, missing = locate_media(wanted, remap, search_dirs=(xml_dir, os.path.join(xml_dir, stem + "_media"),
                                                             os.path.join(xml_dir, "media")))
    if probe is None:
        from classes.handoff.linked_media import probe_media as probe
    probes: Dict[str, dict] = {}
    for original, disk in found.items():
        if should_cancel is not None and should_cancel():
            from classes.handoff.jobs import JobCancelled
            raise JobCancelled("import cancelled")
        if disk in info.file_ids:   # already in Project Files: its data has the media facts
            probes[original] = dict(info.file_data.get(disk) or {}, path=disk, _existing=info.file_ids[disk])
            continue
        try:
            reader = dict(probe(disk))
        except Exception as exc:
            log.warning("Cannot read %s for the XML import: %s", disk, exc)
            missing.append(original)
            continue
        reader["path"] = disk
        probes[original] = reader
    planner = _Planner(seq, info, probes, placement, missing, translate)
    planner.plan()
    if not planner.clips:
        detail = (": missing media " + ", ".join(os.path.basename(m) for m in missing[:5])) if missing else ""
        raise NoClipsError(f"no clip of {seq.name!r} could be imported{detail}", missing)
    for c in planner.clips:   # point clips at the file on disk
        c.path = probes[c.path].get("path", c.path) if c.path in probes else c.path
    media = {probes[k]["path"]: v for k, v in probes.items() if "_existing" not in v}
    if missing:
        planner.warn.add(planner._("%d media file(s) were not found and their clips were skipped") % len(missing))
    return planner.result(path, media, sorted(set(missing)))


def _clip_template() -> dict:
    """libopenshot's default clip JSON (a Clip without a reader: no media is opened)."""
    import openshot
    try:
        data = json.loads(openshot.Clip().Json())
    except TypeError:          # a test double that needs a path
        data = json.loads(openshot.Clip("").Json())
    data.pop("id", None)
    data["effects"] = []
    return data


def _new_track_numbers(count: int) -> List[int]:
    from classes.app import get_app
    numbers = [int(t.get("number") or 0) for t in (get_app().project.get("layers") or [])]
    top = max(numbers, default=0)
    return [top + TRACK_STRIDE * (i + 1) for i in range(count)]


def _fade_source() -> Tuple[dict, dict]:
    """Zenvi's fade transition (catalog entry, reader JSON): resolved before anything changes."""
    from classes import transition_ops as ops
    from classes.editor_tools._base import timeline_ui
    entry, _cands = ops.find_transition("fade")
    if entry is None:
        raise XmlImportError("Zenvi's fade transition is missing from this installation; nothing was imported")
    reader = timeline_ui()._get_transition_reader_json(entry["path"])
    if not isinstance(reader, dict):
        raise XmlImportError("cannot read Zenvi's fade transition image; nothing was imported")
    return entry, reader


def _dissolve_transition(layer: int, position: float, duration: float, fps: float, reverse: bool, title: str,
                         audio: bool = False, source: Optional[Tuple[dict, dict]] = None):
    from classes import transition_ops as ops
    entry, reader = source or _fade_source()
    data = ops.new_mask_transition(None, copy.deepcopy(reader), position=position, layer=layer, duration=duration,
                                   fps_float=fps, title=entry["key"], fade_audio=audio)
    data.pop("id", None)
    frames_n = max(1, int(round(duration * fps)))
    b0, b1 = DISSOLVE_BRIGHTNESS
    if reverse:
        b0, b1 = b1, b0
    data["brightness"] = {"Points": [_point(1, b0, LINEAR), _point(frames_n + 1, b1, LINEAR)]}
    data["contrast"] = {"Points": [_point(1, DISSOLVE_CONTRAST, BEZIER)]}
    return data


def commit_import(plan: ImportPlan) -> dict:
    """Make the import: files, tracks, clips, transitions, markers -- ONE undo step. GUI thread.

    Runs as one preview batch (the editor's IgnoreUpdates, like Undo and
    ``place_clip(ignore_refresh=True)``): playback caching stays off while the
    actions land and the preview redraws once at the end
    (``titles_text_common.end_clip_batch``), instead of re-filling its cache
    after every clip.
    """
    from classes.app import get_app
    from classes.editor_tools.titles_text_common import end_clip_batch
    _begin_preview_batch(get_app())   # before the transaction: entering it lets the event loop run once
    try:
        return _commit(plan)
    finally:
        end_clip_batch()


def _begin_preview_batch(app) -> None:
    try:
        app.window.IgnoreUpdates.emit(True, False)
    except Exception:
        log.debug("preview batch mode unavailable for the XML import", exc_info=True)


def _commit(plan: ImportPlan) -> dict:
    """Everything that can refuse is resolved first; a failure while adding rolls the import back."""
    from classes.app import get_app
    app = get_app()
    fade = _fade_source() if plan.transitions else None
    crop_template = None
    if any(c.crop for c in plan.clips):
        try:
            from classes.editor_tools.titles_text_common import new_effect_json
            crop_template = new_effect_json("Crop")
        except Exception as exc:
            log.warning("Crop effect unavailable; crop not imported", exc_info=True)
            plan.warnings.append(_app_tr("crops were left out: this build has no Crop effect (%s)") % exc)
    updates = app.updates
    history_len, redo = len(updates.actionHistory), list(updates.redoHistory)
    try:
        return _add_to_project(plan, fade, crop_template)
    except Exception as exc:
        _roll_back(updates, history_len, redo)
        if isinstance(exc, XmlImportError):
            raise
        log.error("XML import failed while adding to the project", exc_info=True)
        raise XmlImportError(f"adding the sequence to the project failed ({exc}); nothing was imported") from exc


def _app_tr(text: str) -> str:
    try:
        from classes.app import get_app
        out = get_app()._tr(text)
    except Exception:
        return text
    return out if isinstance(out, str) else text


def _roll_back(updates, history_len: int, redo: list) -> None:
    """Revert exactly the actions this import added (newest first), drop them from history, restore Redo.

    Not ``updates.undo()``: that reverts the whole transaction id, which may include a caller's own
    earlier edits in the same transaction.
    """
    added = list(updates.actionHistory[history_len:])
    del updates.actionHistory[history_len:]
    for action in reversed(added):
        try:
            updates.dispatch_action(updates.get_reverse_action(action))
        except Exception:
            log.error("could not roll back part of a failed XML import (%s %s)", action.type, action.key,
                      exc_info=True)
    updates.redoHistory[:] = redo


def _add_to_project(plan: ImportPlan, fade: Optional[Tuple[dict, dict]], crop_template: Optional[dict]) -> dict:
    from classes import query, track_ops
    from classes.app import get_app
    from classes.clip_placement import apply_audio_only_clip_overrides
    from classes.editor_tools.titles_text_common import fresh_effect
    from classes.updates import nested_transaction
    # untyped on purpose: query's class attributes are declared None (C1 does the same in linked_media)
    Clip, File, Track, Transition = (getattr(query, n) for n in ("Clip", "File", "Track", "Transition"))

    app = get_app()
    fps = app.project.get("fps") or {"num": 30, "den": 1}
    fps_float = float(fps.get("num") or 30) / float(fps.get("den") or 1)
    used_tracks = [t for t in plan.tracks if any(c.track == t.key for c in plan.clips)
                   or any(tr.track == t.key for tr in plan.transitions)]
    numbers = dict(zip([t.key for t in used_tracks], _new_track_numbers(len(used_tracks))))
    summary: Dict[str, Any] = {"track_numbers": [], "clip_ids": [], "transition_ids": [], "marker_ids": [],
                               "file_ids": [], "missing": list(plan.missing)}
    template = _clip_template()
    with nested_transaction(app.updates):
        file_ids: Dict[str, str] = {}
        for path, reader in plan.media.items():
            existing = File.get(path=path)
            if existing:
                file_ids[path] = existing.id
                continue
            f = File()
            data = copy.deepcopy(reader)
            data.pop("_existing", None)
            data["path"] = path
            data.setdefault("name", os.path.basename(path))
            f.data = data
            f.save()
            file_ids[path] = f.id
            summary["file_ids"].append(f.id)
        created = {}
        for t in used_tracks:
            track = Track()
            track.data = {"number": numbers[t.key], "y": 0, "label": t.label, "lock": False}
            track.save()
            created[t.key] = track
            summary["track_numbers"].append(numbers[t.key])
        for c in plan.clips:
            f = File.get(id=c.file_id or file_ids.get(c.path, "")) if (c.file_id or c.path in file_ids) else None
            f = f or File.get(path=c.path)
            if not f:
                continue
            data = copy.deepcopy(template)
            data.update({"file_id": f.id, "title": c.title, "reader": copy.deepcopy(f.data),
                         "layer": numbers[c.track], "position": c.position, "start": c.start, "end": c.end,
                         "duration": float(f.data.get("duration") or c.end)})
            from classes import info
            if f.data.get("media_type") in ("video", "image"):
                data["image"] = os.path.join(info.THUMBNAIL_PATH, "%s.png" % f.id)
            else:
                data["image"] = os.path.join(info.PATH, "images", "AudioThumbnail.svg")
            apply_audio_only_clip_overrides(data, f.data, constant_interpolation=CONSTANT, scale_none=3)
            for key, value in c.props.items():
                data[key] = copy.deepcopy(value)
            if c.crop and crop_template is not None:
                effect = fresh_effect(crop_template)
                for side, curve in c.crop.items():
                    effect[side] = copy.deepcopy(curve)
                data["effects"] = [effect]
            clip = Clip()
            clip.data = data
            clip.save()
            summary["clip_ids"].append(clip.id)
        for tr in plan.transitions:
            data = _dissolve_transition(numbers[tr.track], tr.position, tr.duration, fps_float, tr.reverse, tr.label,
                                        tr.audio, fade)
            t = Transition()
            t.data = data
            t.save()
            summary["transition_ids"].append(t.id)
        for m in plan.markers:
            marker = track_ops.add_marker(m.position, m.name, m.color)
            summary["marker_ids"].append(marker.id)
        for t in used_tracks:   # lock last: the clips went in first
            if t.locked:
                created[t.key].data["lock"] = True
                created[t.key].save()
    summary["track_number"] = summary["track_numbers"][0] if summary["track_numbers"] else None
    return summary


def import_report(plan: ImportPlan, summary: dict, translate: Callable[[str], str]) -> str:
    _ = translate
    lines = [_("Imported %(clips)d clip(s) from %(name)s onto %(tracks)d new track(s).") % {
        "clips": len(summary.get("clip_ids") or []), "name": plan.sequence_name,
        "tracks": len(summary.get("track_numbers") or [])}]
    if plan.missing:
        lines.append(_("Media not found (clips skipped): %s") % ", ".join(os.path.basename(m) for m in plan.missing[:8]))
    if plan.warnings:
        lines.append("")
        lines.extend("• " + w for w in plan.warnings[:12])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File > Import Project > Import XML (and Premiere Pro XML via classes.handoff.premiere)
# ---------------------------------------------------------------------------

def run_import_job(window, path: str, *, placement: str = "new_tracks", prompt: bool = True, title: str = ""):
    """GUI thread: plan in the background, offer to locate missing media, commit as one undo step."""
    from classes.app import get_app
    from classes.handoff import jobs
    from qt_api import QMessageBox

    _ = get_app()._tr
    title = title or _("Import XML")
    info = read_project_info()

    asked: List[bool] = []   # the user is asked for missing media once per import

    def plan_job(remap):
        def work(job):
            return plan_import(path, placement=placement, info=info, remap=remap, should_cancel=job.should_cancel,
                               translate=_)
        return work

    def relocate(missing: List[str]) -> bool:
        """Offer to locate *missing*; True when it re-plans with what the user found."""
        if not prompt or not missing or asked:
            return False
        asked.append(True)
        remap = _ask_for_missing(window, missing)
        if not remap:
            return False
        jobs.submit_job(plan_job(remap), label=_("Importing %s") % os.path.basename(path), kind="xml-import",
                        on_done=finish)
        return True

    def finish(job):
        from windows.handoff_menus import notify
        if job.state == jobs.CANCELLED:
            notify(window, _("Import cancelled"))
            return
        if job.error is not None:
            # Nothing could be placed (an XML from another computer): still offer to point at the media.
            if isinstance(job.error, NoClipsError) and relocate(job.error.missing):
                return
            QMessageBox.warning(window, title, _("Import failed: %s") % job.error)
            return
        plan = job.result
        if relocate(plan.missing):
            return
        try:
            summary = commit_import(plan)
        except Exception as exc:
            log.error("XML import failed", exc_info=True)
            QMessageBox.warning(window, title, _("Import failed: %s") % exc)
            return
        notify(window, _("Imported %d clip(s) from %s") % (len(summary["clip_ids"]), plan.sequence_name))
        if plan.warnings or plan.missing:
            QMessageBox.information(window, title, import_report(plan, summary, _))

    return jobs.submit_job(plan_job(None), label=_("Importing %s") % os.path.basename(path), kind="xml-import",
                           on_done=finish)


def _ask_for_missing(window, missing: List[str]) -> Dict[str, str]:
    """Let the user point at the missing media (GUI thread); {xml path: chosen path}."""
    from windows.views.find_file import find_missing_file
    state: Dict[str, Any] = {}
    remap = {}
    for path in missing:
        chosen, _modified, skipped = find_missing_file(path, prompt_state=state, prompt=True)
        if skipped:
            if state.get("cancelled"):
                break
            continue
        if chosen:
            remap[path] = chosen
    return remap


def import_xml(file_path: Optional[str] = None, prompt: bool = True):
    """Import an FCP7 / Premiere XML (File > Import Project > Import XML).

    With no *file_path* it asks for one and imports in the background (one
    undo step); returns the job. With a path it imports right away (blocking:
    call it off the GUI thread, it hops there only to commit) and returns
    ``{"track_numbers", "clip_ids", "missing", ...}``; missing media is
    skipped (no dialog).
    """
    from classes.app import get_app
    if file_path is not None:
        from classes.qt_main_thread import is_gui_thread
        info = read_project_info() if is_gui_thread() else None
        plan = plan_import(file_path, info=info)
        from classes.qt_main_thread import call_on_gui
        summary = cast(dict, call_on_gui(commit_import, plan, timeout=120))
        summary["warnings"] = plan.warnings
        return summary
    from classes import info as app_info
    from qt_api import QFileDialog
    app = get_app()
    _ = app._tr
    start = os.path.dirname(app.project.current_filepath or "") or app_info.HOME_PATH
    chosen = QFileDialog.getOpenFileName(app.window, _("Import XML..."), start,
                                         _("Final Cut Pro / Premiere XML (*.xml)"))[0]
    if not chosen:
        return None
    return run_import_job(app.window, chosen, prompt=prompt)
