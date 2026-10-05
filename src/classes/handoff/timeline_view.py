"""An immutable, read-only snapshot of a Zenvi project for the handoff exporters.

``TimelineSnapshot.from_project(project_dict, project_path=None)`` reads the
project JSON (``get_app().project._data`` or a parsed ``.zvn``) once and
hands exporters plain values: fps as a Fraction, tracks bottom -> top, clips
per track by position, files with absolute paths, keyframes as
:class:`~classes.handoff.keyframes.Curve` objects with timeline times, the
speed a clip's ``time`` curve implies, effects, transitions and markers.

Take the snapshot on the GUI thread (a deep copy of the project data is the
only cost) or from a copy, then export off the GUI thread: nothing here
touches Qt, libopenshot or the live project.

Units: seconds everywhere except ``Curve`` point ``frame`` values (libopenshot
X). ``ClipView.position`` / ``start`` / ``end`` are the project's own keys:
the timeline start and the clip-frame window (X axis). They equal the source
in/out only when the clip does not remap time; ``source_in`` / ``source_out``
are always the media actually shown, ``speed`` says how it plays (within the
visible window) and ``time`` is the remap curve for variable speeds
(``source_time_at(t)``). ``timeline_in`` / ``timeline_out`` are where the clip
shows on the timeline.
"""

from __future__ import annotations

import copy
import math
import os
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from classes.handoff.keyframes import BEZIER, CONSTANT, LINEAR, Curve, as_fraction, resolve_points
from classes.handoff.transform import (
    GRAVITY_CENTER, REPAIRED_WHEN_EMPTY, SCALE_FIT, TRANSFORM_DEFAULTS,
)

# Clip keyframes an exporter reads, with the default an absent key has.
CLIP_CURVE_KEYS = ("alpha", "location_x", "location_y", "scale_x", "scale_y", "rotation", "volume",
                   "origin_x", "origin_y", "shear_x", "shear_y", "margin", "corner_radius")
SVG_SUFFIXES = (".svg", ".svgz")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN -> default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _frac(value: Any, default: Fraction) -> Fraction:
    if isinstance(value, dict):
        try:
            num, den = int(value.get("num") or 0), int(value.get("den") or 0)
            if num > 0 and den > 0:
                return Fraction(num, den)
        except (TypeError, ValueError):
            pass
    return default


def resolve_media_path(raw: str, project_path: Optional[str] = None) -> str:
    """Absolute path of a project media path, resolved like ``path_utils.absolute_media_path``.

    Absolute paths (what an open project holds in memory) return without
    touching the app; tokens (``@assets/``, ``@transitions/`` ...) and
    relative paths resolve against *project_path*, or the open project when
    it is None.
    """
    raw = str(raw or "")
    if not raw:
        return ""
    normalized = raw.replace("\\", "/")
    if os.path.isabs(normalized) and not normalized.startswith("~"):
        return os.path.normpath(normalized)
    from classes.path_utils import absolute_media_path
    if project_path:
        return absolute_media_path(raw, project_file=project_path)
    return absolute_media_path(raw)


@dataclass(frozen=True)
class FileView:
    """A Project Files entry."""

    id: str
    path: str                 # absolute
    raw_path: str             # as stored in the project
    name: str
    media_type: str           # video | audio | image (libopenshot/files_model naming)
    has_video: bool
    has_audio: bool
    width: int
    height: int
    duration: float
    fps: Optional[Fraction]
    is_title: bool            # an SVG title (Title Editor / add_title_tool)
    is_image_sequence: bool
    zenvi_link: Optional[Mapping[str, Any]]
    data: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def is_linked(self) -> bool:
        return isinstance(self.zenvi_link, Mapping)

    @property
    def is_still(self) -> bool:
        return self.media_type == "image" and not self.is_image_sequence

    @classmethod
    def from_data(cls, data: dict, project_path: Optional[str] = None) -> "FileView":
        raw_path = str(data.get("path") or "")
        path = resolve_media_path(raw_path, project_path)
        name = str(data.get("name") or os.path.basename(raw_path))
        media_type = str(data.get("media_type") or "")
        fps = _frac(data.get("fps"), Fraction(0)) or None
        link = data.get("zenvi_link")
        return cls(
            id=str(data.get("id") or ""),
            path=path,
            raw_path=raw_path,
            name=name,
            media_type=media_type,
            has_video=bool(data.get("has_video", media_type != "audio")),
            has_audio=bool(data.get("has_audio", False)),
            width=_i(data.get("width")),
            height=_i(data.get("height")),
            duration=_f(data.get("duration")),
            fps=fps,
            is_title=path.lower().endswith(SVG_SUFFIXES),
            is_image_sequence="%" in raw_path,
            zenvi_link=MappingProxyType(copy.deepcopy(link)) if isinstance(link, dict) else None,
            data=MappingProxyType(copy.deepcopy(data)),
        )


@dataclass(frozen=True)
class SpeedInfo:
    """What a clip's ``time`` curve does to playback.

    kind: ``normal`` (1x forward), ``constant`` (one speed for the whole clip,
    ``factor`` x, possibly ``reversed``), ``freeze`` (one source frame held),
    or ``variable`` (ramps, freeze-then-play, repeats -- export by sampling).
    ``factor`` is source seconds per timeline second (2.0 = twice as fast),
    None for ``variable``.
    """

    kind: str
    factor: Optional[float]
    reversed: bool = False

    @property
    def is_normal(self) -> bool:
        return self.kind == "normal"


def _round_half_away(value: float) -> int:
    return int(math.floor(value + 0.5)) if value >= 0 else int(math.ceil(value - 0.5))


def time_remaps(time_kf: Any) -> bool:
    """True when libopenshot remaps frames with this ``time`` keyframe (``time.GetLength() > 1``)."""
    pts = resolve_points(time_kf)
    return len(pts) >= 2 and _round_half_away(pts[-1][0]) > 1


def speed_from_time(time_kf: Any, repeat_active: bool = False,
                    frame_range: Optional[Tuple[float, float]] = None) -> SpeedInfo:
    """Speed implied by a clip ``time`` keyframe over the clip frames it shows.

    *frame_range* is the visible window of clip frames (``ClipView.frame_range()``,
    libopenshot X, inclusive); only that part of the curve counts, and frames
    before the first / after the last point hold that point's source frame.
    ``constant`` needs one straight (LINEAR) slope across the whole window; a
    window that mixes playing and holding, or bends (BEZIER), is ``variable``.
    The constant factor uses ``(|y1 - y0| + 1) / (x1 - x0)`` over the linear
    segments involved, like ``timeline_edit_time.effective_speed`` (both ends
    inclusive).
    """
    if repeat_active:
        return SpeedInfo("variable", None)
    pts = resolve_points(time_kf)
    if len(pts) < 2 or _round_half_away(pts[-1][0]) <= 1:
        return SpeedInfo("normal", 1.0)
    a, b = (pts[0][0], pts[-1][0]) if frame_range is None else (float(frame_range[0]), float(frame_range[1]))
    if b < a:
        a, b = b, a
    pieces = []  # (from_x, to_x, kind, slope, segment)
    if a < pts[0][0]:
        pieces.append((a, min(b, pts[0][0]), "hold", 0.0, None))
    for p, q in zip(pts, pts[1:]):
        lo, hi = max(a, p[0]), min(b, q[0])
        if q[0] <= p[0] or hi < lo or (hi == lo and not (a == b and p[0] <= a <= q[0])):
            continue
        if abs(q[1] - p[1]) < 1e-9 or q[2] == CONSTANT:
            pieces.append((lo, hi, "hold", 0.0, (p, q)))
        elif q[2] == LINEAR:
            pieces.append((lo, hi, "line", (q[1] - p[1]) / (q[0] - p[0]), (p, q)))
        else:
            pieces.append((lo, hi, "curve", None, (p, q)))
    if b > pts[-1][0]:
        pieces.append((max(a, pts[-1][0]), b, "hold", 0.0, None))
    spans = [pc for pc in pieces if pc[1] - pc[0] >= 1.0 - 1e-9] or pieces
    if not spans:
        return SpeedInfo("normal", 1.0)
    if any(pc[2] == "curve" for pc in spans):
        return SpeedInfo("variable", None)
    if all(pc[2] == "hold" for pc in spans):
        return SpeedInfo("freeze", 0.0)
    if any(pc[2] == "hold" for pc in spans):
        return SpeedInfo("variable", None)  # plays, then holds (or the reverse)
    slopes = [pc[3] for pc in spans]
    tolerance = max(0.02, abs(slopes[0]) * 0.02)
    if any(abs(sl - slopes[0]) > tolerance for sl in slopes):
        return SpeedInfo("variable", None)
    segs = [pc[4] for pc in spans]
    x0, y0 = segs[0][0][0], segs[0][0][1]
    x1, y1 = segs[-1][1][0], segs[-1][1][1]
    factor = (abs(y1 - y0) + 1.0) / (x1 - x0)
    rev = y1 < y0
    if not rev and abs(factor - 1.0) < 0.01:
        return SpeedInfo("normal", 1.0)
    return SpeedInfo("constant", factor, rev)


def source_frames(time_curve: Optional[Curve], first: int, last: int) -> Tuple[int, int]:
    """(lowest, highest) source frame libopenshot shows for clip frames first..last (inclusive).

    ``source frame = max(1, time.GetLong(X))`` when the clip remaps time,
    else X itself. Evaluates the window's ends, every point inside it and
    every frame of a bezier segment inside it (where extremes can hide).
    """
    if time_curve is None:
        return first, last
    xs = {first, last}
    pts = time_curve.points
    for p, q in zip(pts, pts[1:]):
        lo, hi = max(first, int(math.floor(p.frame))), min(last, int(math.ceil(q.frame)))
        if hi < lo:
            continue
        for x in (lo, hi, int(round(p.frame)), int(round(q.frame))):
            for dx in (-1, 0, 1):
                if first <= x + dx <= last:
                    xs.add(x + dx)
        if q.interpolation == BEZIER and abs(q.value - p.value) > 1e-9 and hi - lo <= 20000:
            xs.update(range(lo, hi + 1))
    values = [max(1, _round_half_away(time_curve.value_at_frame(x))) for x in xs]
    return min(values), max(values)


@dataclass(frozen=True)
class EffectView:
    """An effect on a clip: plain values as they are, keyframes as Curves, colours as dicts of Curves."""

    id: str
    class_name: str
    name: str
    params: Mapping[str, Any]
    data: Mapping[str, Any] = field(repr=False, compare=False)

    def curve(self, key: str) -> Optional[Curve]:
        value = self.params.get(key)
        return value if isinstance(value, Curve) else None


def _frame_range(start: float, end: float, fps: Fraction) -> Tuple[int, int]:
    rate = float(fps)
    first = int(round(start * rate)) + 1
    last = int(round(end * rate))
    return first, max(first, last)


@dataclass(frozen=True)
class ClipView:
    """A timeline clip."""

    id: str
    title: str
    track_index: int          # 0 = bottom track
    layer: int                # project layer number (track ``number``)
    position: float           # timeline start (s)
    start: float              # clip-frame in (s): the project's ``start`` (= source in unless time is remapped)
    end: float                # clip-frame out (s): the project's ``end``
    timeline_in: float        # == position
    timeline_out: float       # position + (end - start)
    fps: Fraction
    speed: SpeedInfo
    time: Optional[Curve]     # the ``time`` remap curve (clip frame X -> source frame Y), None = no remap
    source_in: float          # earliest source second shown (start of that source frame)
    source_out: float         # end of the latest source frame shown; [source_in, source_out) is the media used
    scale_mode: int           # transform.SCALE_* (0 crop, 1 fit, 2 stretch, 3 none)
    gravity: int              # transform.GRAVITY_* (4 centre)
    file: Optional[FileView]
    curves: Mapping[str, Curve]
    effects: Tuple[EffectView, ...]
    has_audio: Optional[bool]  # None = automatic (the file decides)
    has_video: Optional[bool]
    parent_id: str
    data: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def duration(self) -> float:
        return self.timeline_out - self.timeline_in

    @property
    def is_linked(self) -> bool:
        return self.file is not None and self.file.is_linked

    def curve(self, key: str) -> Curve:
        """The clip's curve for *key* (a constant default curve for keys it lacks)."""
        found = self.curves.get(key)
        if found is not None:
            return found
        return Curve.constant(TRANSFORM_DEFAULTS.get(key, 0.0), fps=self.fps, position=self.position,
                              start=self.start)

    def frame_range(self) -> Tuple[int, int]:
        """First and last visible clip frame (libopenshot X), inclusive."""
        return _frame_range(self.start, self.end, self.fps)

    def source_frame_at(self, time: float) -> int:
        """1-based source frame libopenshot shows at timeline second *time* (time remap applied)."""
        x = int(round(self.curve("alpha").frame_at(time)))
        if self.time is None:
            return max(1, x)
        return max(1, _round_half_away(self.time.value_at_frame(x)))

    def source_time_at(self, time: float) -> float:
        """Source second (start of the frame) shown at timeline second *time*."""
        return (self.source_frame_at(time) - 1) / float(self.fps)


@dataclass(frozen=True)
class TransitionView:
    """A transition (libopenshot Mask) on a track. Its curves use transition-local frames."""

    id: str
    title: str
    track_index: int
    layer: int
    position: float
    duration: float
    end: float
    mask_path: str            # absolute path of the wipe image ('' = none)
    brightness: Curve
    contrast: Curve
    reversed: bool            # brightness rises (-1 -> 1) instead of the default fall
    data: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True)
class MarkerView:
    id: str
    time: float
    name: str
    color: str


@dataclass(frozen=True)
class TrackView:
    index: int                # 0 = bottom
    number: int               # project layer number
    label: str
    locked: bool
    clips: Tuple[ClipView, ...]
    transitions: Tuple[TransitionView, ...]

    @property
    def name(self) -> str:
        return self.label or f"Track {self.index + 1}"


def _curves_for(data: dict, fps: Fraction, position: float, start: float) -> Dict[str, Curve]:
    out: Dict[str, Curve] = {}
    for key in CLIP_CURVE_KEYS:
        default = TRANSFORM_DEFAULTS.get(key, 0.0)
        if key not in data or data.get(key) is None:
            out[key] = Curve.constant(default, fps=fps, position=position, start=start)
            continue
        value = data.get(key)
        curve = Curve.from_json(value, fps=fps, position=position, start=start,
                                default=default if key in REPAIRED_WHEN_EMPTY else 0.0)
        out[key] = curve
    return out


def _is_keyframe(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("Points"), list)


def _is_color(value: Any) -> bool:
    return isinstance(value, dict) and all(_is_keyframe(value.get(c)) for c in ("red", "green", "blue"))


_EFFECT_META_KEYS = frozenset({"id", "class_name", "name", "type", "short_name", "description", "has_audio",
                               "has_video", "has_tracked_object", "order", "position", "start", "end",
                               "duration", "layer", "parent_effect_id", "apply_before_clip", "ui"})


def _effect_view(data: dict, fps: Fraction, position: float, start: float) -> EffectView:
    params: Dict[str, Any] = {}
    for key, value in data.items():
        if key in _EFFECT_META_KEYS:
            continue
        if _is_keyframe(value):
            params[key] = Curve.from_json(value, fps=fps, position=position, start=start)
        elif _is_color(value):
            params[key] = MappingProxyType({c: Curve.from_json(value.get(c), fps=fps, position=position,
                                                               start=start)
                                            for c in ("red", "green", "blue", "alpha") if c in value})
        else:
            params[key] = copy.deepcopy(value)
    return EffectView(
        id=str(data.get("id") or ""),
        class_name=str(data.get("class_name") or data.get("type") or ""),
        name=str(data.get("name") or data.get("class_name") or ""),
        params=MappingProxyType(params),
        data=MappingProxyType(copy.deepcopy(data)),
    )


def _auto_flag(value: Any) -> Optional[bool]:
    """has_audio / has_video keyframes: -1 automatic, 0 off, 1 on."""
    pts = resolve_points(value) if value is not None else []
    if not pts:
        return None
    y = pts[0][1]
    if y < 0:
        return None
    return y > 0.5


def _repeat_active(data: dict) -> bool:
    cache = data.get("repeat_cache")
    if not isinstance(cache, dict) or not cache:
        return False
    try:
        return (abs(float(cache.get("start", 0.0)) - float(data.get("start", 0.0))) > 1e-6
                or abs(float(cache.get("end", 0.0)) - float(data.get("end", 0.0))) > 1e-6)
    except (TypeError, ValueError):
        return True


def _marker_color(data: dict) -> str:
    vector = str(data.get("vector") or "").strip().lower()
    if vector:
        return vector
    icon = str(data.get("icon") or "").strip().lower()
    return icon[:-4] if icon.endswith(".png") else (icon or "blue")


@dataclass(frozen=True)
class TimelineSnapshot:
    """Everything an exporter reads, frozen at one moment."""

    fps: Fraction
    width: int
    height: int
    pixel_aspect: Fraction
    display_ratio: Fraction
    sample_rate: int
    channels: int
    channel_layout: int
    duration: float           # end of the last clip or transition
    timeline_length: float    # the project's own ``duration`` (the timeline canvas)
    tracks: Tuple[TrackView, ...]          # bottom -> top
    markers: Tuple[MarkerView, ...]
    files: Mapping[str, FileView]
    project_path: Optional[str]
    name: str
    profile: str

    # -- construction -----------------------------------------------------
    @classmethod
    def from_project(cls, project: Mapping[str, Any], project_path: Optional[str] = None) -> "TimelineSnapshot":
        """Snapshot *project* (a project dict); *project_path* resolves relative and ``@assets`` paths."""
        data = _copy_project(project)
        fps = _frac(data.get("fps"), Fraction(30, 1))
        width, height = _i(data.get("width"), 1920) or 1920, _i(data.get("height"), 1080) or 1080
        files = {}
        for fd in data.get("files") or []:
            if isinstance(fd, dict) and fd.get("id"):
                fv = FileView.from_data(fd, project_path)
                files[fv.id] = fv

        layers = sorted((ly for ly in (data.get("layers") or []) if isinstance(ly, dict)),
                        key=lambda ly: _i(ly.get("number")))
        index_of = {_i(ly.get("number")): i for i, ly in enumerate(layers)}

        clips_by_layer: Dict[int, List[ClipView]] = {}
        end_of_content = 0.0
        for cd in data.get("clips") or []:
            if not isinstance(cd, dict):
                continue
            layer = _i(cd.get("layer"))
            if layer not in index_of:
                # a clip on a track the layer list lacks: give it a lane so nothing is dropped
                index_of[layer] = -1
            position, start, end = _f(cd.get("position")), _f(cd.get("start")), _f(cd.get("end"))
            fv = files.get(str(cd.get("file_id") or ""))
            if fv is None and isinstance(cd.get("reader"), dict):
                fv = FileView.from_data(dict(cd["reader"], id=str(cd.get("file_id") or "")), project_path)
            first_x, last_x = _frame_range(start, end, fps)
            time_curve = (Curve.from_json(cd.get("time"), fps=fps, position=position, start=start)
                          if time_remaps(cd.get("time")) else None)
            lo_frame, hi_frame = source_frames(time_curve, first_x, last_x)
            clip = ClipView(
                id=str(cd.get("id") or ""),
                title=str(cd.get("title") or (fv.name if fv else "")),
                track_index=0,
                layer=layer,
                position=position,
                start=start,
                end=end,
                timeline_in=position,
                timeline_out=position + max(0.0, end - start),
                fps=fps,
                speed=speed_from_time(cd.get("time"), _repeat_active(cd), (first_x, last_x)),
                time=time_curve,
                source_in=(lo_frame - 1) / float(fps),
                source_out=hi_frame / float(fps),
                scale_mode=_i(cd.get("scale"), SCALE_FIT),
                gravity=_i(cd.get("gravity"), GRAVITY_CENTER),
                file=fv,
                curves=MappingProxyType(_curves_for(cd, fps, position, start)),
                effects=tuple(_effect_view(e, fps, position, start) for e in (cd.get("effects") or [])
                              if isinstance(e, dict)),
                has_audio=_auto_flag(cd.get("has_audio")),
                has_video=_auto_flag(cd.get("has_video")),
                parent_id=str(cd.get("parentObjectId") or ""),
                data=MappingProxyType(cd),
            )
            clips_by_layer.setdefault(layer, []).append(clip)
            end_of_content = max(end_of_content, clip.timeline_out)

        transitions_by_layer: Dict[int, List[TransitionView]] = {}
        for td in data.get("effects") or []:
            if not isinstance(td, dict):
                continue
            layer = _i(td.get("layer"))
            if layer not in index_of:
                index_of[layer] = -1
            position = _f(td.get("position"))
            tstart, tend = _f(td.get("start")), _f(td.get("end"))
            duration = max(0.0, tend - tstart)
            reader = td.get("reader")
            if not isinstance(reader, dict):
                reader = td.get("mask_reader")
            if not isinstance(reader, dict):
                reader = {}
            brightness = Curve.from_json(td.get("brightness"), fps=fps, position=position, start=tstart)
            tv = TransitionView(
                id=str(td.get("id") or ""),
                title=str(td.get("title") or td.get("type") or ""),
                track_index=0,
                layer=layer,
                position=position,
                duration=duration,
                end=position + duration,
                mask_path=resolve_media_path(str(reader.get("path") or ""), project_path),
                brightness=brightness,
                contrast=Curve.from_json(td.get("contrast"), fps=fps, position=position, start=tstart),
                reversed=brightness.first_value < brightness.last_value,
                data=MappingProxyType(td),
            )
            transitions_by_layer.setdefault(layer, []).append(tv)
            end_of_content = max(end_of_content, tv.end)

        # Tracks the layer list does not know (stray clips) slot in by number.
        numbers = sorted(index_of)
        known = {_i(ly.get("number")): ly for ly in layers}
        tracks = []
        for idx, number in enumerate(numbers):
            ly = known.get(number, {})
            clips = sorted(clips_by_layer.get(number, []), key=lambda c: (c.position, c.id))
            trans = sorted(transitions_by_layer.get(number, []), key=lambda t: (t.position, t.id))
            clips = tuple(_with_track(c, idx) for c in clips)
            trans = tuple(_with_track(t, idx) for t in trans)
            tracks.append(TrackView(index=idx, number=number, label=str(ly.get("label") or ""),
                                    locked=bool(ly.get("lock")), clips=clips, transitions=trans))

        markers = []
        for md in data.get("markers") or []:
            if isinstance(md, dict):
                markers.append(MarkerView(id=str(md.get("id") or ""), time=_f(md.get("position")),
                                          name=str(md.get("name") or ""), color=_marker_color(md)))
        markers.sort(key=lambda m: (m.time, m.id))

        name = os.path.splitext(os.path.basename(project_path))[0] if project_path else ""
        return cls(
            fps=fps,
            width=width,
            height=height,
            pixel_aspect=_frac(data.get("pixel_ratio"), Fraction(1, 1)),
            display_ratio=_frac(data.get("display_ratio"), Fraction(width, height)),
            sample_rate=_i(data.get("sample_rate"), 48000) or 48000,
            channels=_i(data.get("channels"), 2) or 2,
            channel_layout=_i(data.get("channel_layout"), 3),
            duration=end_of_content,
            timeline_length=_f(data.get("duration")),
            tracks=tuple(tracks),
            markers=tuple(markers),
            files=MappingProxyType(files),
            project_path=project_path,
            name=name or "Untitled",
            profile=str(data.get("profile") or ""),
        )

    @classmethod
    def from_app(cls) -> "TimelineSnapshot":
        """Snapshot the open project. Call on the GUI thread (it reads the live project)."""
        from classes.app import get_app
        app = get_app()
        return cls.from_project(app.project._data, getattr(app.project, "current_filepath", None) or None)

    # -- queries ------------------------------------------------------------
    @property
    def fps_float(self) -> float:
        return float(self.fps)

    @property
    def frame_seconds(self) -> float:
        return float(1 / self.fps)

    @property
    def clips(self) -> Tuple[ClipView, ...]:
        """Every clip, by timeline start then track (bottom first)."""
        out = [c for t in self.tracks for c in t.clips]
        out.sort(key=lambda c: (c.position, c.track_index, c.id))
        return tuple(out)

    @property
    def transitions(self) -> Tuple[TransitionView, ...]:
        out = [tr for t in self.tracks for tr in t.transitions]
        out.sort(key=lambda tr: (tr.position, tr.track_index, tr.id))
        return tuple(out)

    def clip(self, clip_id: str) -> Optional[ClipView]:
        for c in self.clips:
            if c.id == clip_id:
                return c
        return None

    def file(self, file_id: str) -> Optional[FileView]:
        return self.files.get(file_id)

    def clips_of_file(self, file_id: str) -> Tuple[ClipView, ...]:
        return tuple(c for c in self.clips if c.file is not None and c.file.id == file_id)

    def linked_files(self) -> Tuple[FileView, ...]:
        return tuple(f for f in self.files.values() if f.is_linked)

    def used_files(self) -> Tuple[FileView, ...]:
        """Files with at least one clip on the timeline, in first-use order."""
        seen: Dict[str, FileView] = {}
        for c in self.clips:
            if c.file is not None and c.file.id not in seen:
                seen[c.file.id] = c.file
        return tuple(seen.values())

    def seconds_to_frame(self, seconds: float) -> int:
        """0-based timeline frame (half-up), like ``frame_time.to_frame``."""
        from classes import frame_time
        return frame_time.to_frame(seconds, self.fps)

    def track(self, index: int) -> TrackView:
        return self.tracks[index]


def _copy_project(project: Mapping[str, Any]) -> dict:
    """A deep copy of the project that shares the waveform sample lists (``ui.audio_data``).

    They are large (tens of thousands of floats per clip) and replaced
    wholesale, never edited in place -- the same rule as
    ``query.QueryObject._get_cached_child`` -- so sharing them keeps
    ``from_app()`` on the GUI thread cheap.
    """
    memo: Dict[int, Any] = {}
    for key in ("clips", "files", "effects"):
        for item in project.get(key) or []:
            ui = item.get("ui") if isinstance(item, dict) else None
            audio = ui.get("audio_data") if isinstance(ui, dict) else None
            if isinstance(audio, list) and audio:
                memo[id(audio)] = audio
    return copy.deepcopy(dict(project), memo)


def _with_track(item, index: int):
    from dataclasses import replace
    return replace(item, track_index=index)


__all__ = [
    "TimelineSnapshot", "TrackView", "ClipView", "FileView", "EffectView", "TransitionView", "MarkerView",
    "SpeedInfo", "speed_from_time", "time_remaps", "source_frames", "resolve_media_path", "CLIP_CURVE_KEYS",
    "as_fraction",
]
