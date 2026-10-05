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
X). ``ClipView.position`` / ``start`` / ``end`` are the project's own keys
(timeline start, source in, source out); ``timeline_in`` / ``timeline_out``
are where the clip shows on the timeline.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from classes.handoff.keyframes import BEZIER, LINEAR, Curve, as_fraction, resolve_points
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


def speed_from_time(time_kf: Any, repeat_active: bool = False) -> SpeedInfo:
    """Speed implied by a clip ``time`` keyframe (source frames Y over clip frames X).

    The constant factor uses ``(|y1 - y0| + 1) / (x1 - x0)`` like
    ``timeline_edit_time.effective_speed`` (both ends inclusive).
    """
    if repeat_active:
        return SpeedInfo("variable", None)
    pts = resolve_points(time_kf)
    segments = [(a, b) for a, b in zip(pts, pts[1:]) if b[0] > a[0]]
    if not segments:
        return SpeedInfo("normal", 1.0)
    slopes = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in segments]
    if all(abs(s) < 1e-9 for s in slopes):
        return SpeedInfo("freeze", 0.0)
    # One speed throughout: every segment a straight line (bezier time segments are ramps,
    # constant ones are holds) with the same slope (rounded frame ends allow ~2 %).
    straight = all(b[2] == LINEAR or (b[2] != BEZIER and abs(b[1] - a[1]) < 1e-9) for a, b in segments)
    tolerance = max(0.02, abs(slopes[0]) * 0.02)
    if not straight or any(abs(s - slopes[0]) > tolerance for s in slopes):
        return SpeedInfo("variable", None)
    x0, y0, x1, y1 = pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]
    factor = (abs(y1 - y0) + 1.0) / (x1 - x0)
    rev = y1 < y0
    if not rev and abs(factor - 1.0) < 0.01:
        return SpeedInfo("normal", 1.0)
    return SpeedInfo("constant", factor, rev)


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


@dataclass(frozen=True)
class ClipView:
    """A timeline clip."""

    id: str
    title: str
    track_index: int          # 0 = bottom track
    layer: int                # project layer number (track ``number``)
    position: float           # timeline start (s)
    start: float              # source in (s)
    end: float                # source out (s)
    timeline_in: float        # == position
    timeline_out: float       # position + (end - start)
    fps: Fraction
    speed: SpeedInfo
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
        rate = float(self.fps)
        first = int(round(self.start * rate)) + 1
        last = int(round(self.end * rate))
        return first, max(first, last)


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
        data = copy.deepcopy(dict(project))
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
                speed=speed_from_time(cd.get("time"), _repeat_active(cd)),
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


def _with_track(item, index: int):
    from dataclasses import replace
    return replace(item, track_index=index)


__all__ = [
    "TimelineSnapshot", "TrackView", "ClipView", "FileView", "EffectView", "TransitionView", "MarkerView",
    "SpeedInfo", "speed_from_time", "resolve_media_path", "CLIP_CURVE_KEYS", "as_fraction",
]
