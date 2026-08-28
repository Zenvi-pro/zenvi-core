"""Pure helpers for agent audio mixing and context-aware ducking (no Qt deps).

Volume automation is written as OpenShot clip ``volume`` keyframe Points. Point
X is a **1-based clip-source frame**, matching ``Volume_Triggered``:

    start_of_clip = round(clip["start"] * fps) + 1
    end_of_clip   = round(clip["end"]   * fps) + 1

Speech windows are carried in **timeline seconds** everywhere in this module and
converted to source frames only when points are built.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

# libopenshot InterpolationType
BEZIER = 0
LINEAR = 1
CONSTANT = 2

# Ducking defaults (seconds unless noted).
DEFAULT_DUCK_DB = -12.0
DEFAULT_ATTACK = 0.150
DEFAULT_RELEASE = 0.400
DEFAULT_PAD_BEFORE = 0.200
DEFAULT_PAD_AFTER = 0.300
DEFAULT_RESTORE_MARGIN = 0.250

# Waveform energy gate (secondary speech signal).
DEFAULT_ENERGY_THRESHOLD = 0.06
DEFAULT_ENERGY_MIN_RUN = 0.25

# Audio-only source at or above this duration reads as a music bed, not a sting.
MUSIC_MIN_DURATION = 8.0

MAX_LEVEL = 1.3  # matches the 0-130% volume menu range

# Auto ducking: how far under the speech a bed should sit, and the range the
# derived attenuation is allowed to occupy. A bed that is already quiet is
# barely touched; one slamming over the voice is cut hard.
AUTO_DUCK_HEADROOM_DB = -12.0
AUTO_DUCK_MIN_DB = -24.0
AUTO_DUCK_MAX_DB = -3.0

_EPS = 1e-6

ROLE_SPEECH = "speech"
ROLE_MUSIC = "music"
ROLE_SFX = "sfx"
ROLE_AMBIENT = "ambient"
ROLE_SILENT = "silent"
ROLE_UNKNOWN = "unknown"

BED_ROLES = (ROLE_MUSIC, ROLE_SFX)



def _f(value, default=0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def db_to_gain(db) -> float:
    """Decibels to a linear volume multiplier (0 dB -> 1.0)."""
    return 10.0 ** (_f(db, 0.0) / 20.0)


def gain_to_db(gain) -> float:
    """Linear volume multiplier to decibels (0 or negative -> floor)."""
    g = _f(gain, 0.0)
    if g <= 0.0:
        return -120.0
    return 20.0 * math.log10(g)


def fps_float(fps) -> float:
    """Project fps dict {'num':..., 'den':...} (or a number) to a float."""
    if isinstance(fps, dict):
        num = _f(fps.get("num"), 30.0)
        den = _f(fps.get("den"), 1.0)
        return num / den if den else 30.0
    return _f(fps, 30.0) or 30.0


# --------------------------------------------------------------------------
# Timeline / source geometry
# --------------------------------------------------------------------------

def clip_source_frame_range(clip_data: dict, fps) -> Tuple[int, int]:
    """(first, last) 1-based source frame for a clip, as Volume_Triggered does."""
    f = fps_float(fps)
    data = clip_data if isinstance(clip_data, dict) else {}
    first = int(round(_f(data.get("start"), 0.0) * f)) + 1
    last = int(round(_f(data.get("end"), 0.0) * f)) + 1
    if last < first:
        last = first
    return first, last


def clip_timeline_extent(clip_data: dict) -> Tuple[float, float]:
    """(start, end) of a placement in timeline seconds."""
    data = clip_data if isinstance(clip_data, dict) else {}
    pos = _f(data.get("position"), 0.0)
    dur = max(0.0, _f(data.get("end"), 0.0) - _f(data.get("start"), 0.0))
    return pos, pos + dur


def timeline_to_source_frame(t_timeline, clip_data: dict, fps) -> int:
    """Timeline seconds to a 1-based source frame, clamped inside the clip."""
    data = clip_data if isinstance(clip_data, dict) else {}
    f = fps_float(fps)
    source_seconds = (
        _f(t_timeline, 0.0) - _f(data.get("position"), 0.0) + _f(data.get("start"), 0.0)
    )
    frame = int(round(source_seconds * f)) + 1
    first, last = clip_source_frame_range(data, fps)
    return max(first, min(last, frame))


def source_to_timeline_seconds(t_source, clip_data: dict) -> float:
    """Root-source seconds to timeline seconds for a placement."""
    data = clip_data if isinstance(clip_data, dict) else {}
    return _f(t_source, 0.0) - _f(data.get("start"), 0.0) + _f(data.get("position"), 0.0)


# --------------------------------------------------------------------------
# Window algebra (timeline seconds)
# --------------------------------------------------------------------------

def merge_windows(windows: Sequence[Sequence[float]], gap: float = 0.0) -> List[Tuple[float, float]]:
    """Sort, drop empty/inverted, and merge windows separated by less than gap."""
    clean = []
    for win in windows or []:
        try:
            s, e = _f(win[0]), _f(win[1])
        except (TypeError, IndexError, KeyError):
            continue
        if e - s <= _EPS:
            continue
        clean.append((s, e))
    if not clean:
        return []
    clean.sort()
    gap = max(0.0, _f(gap, 0.0))
    merged = [clean[0]]
    for s, e in clean[1:]:
        last_s, last_e = merged[-1]
        if s - last_e <= gap + _EPS:
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))
    return merged


def clamp_windows(windows: Sequence[Sequence[float]], lo: float, hi: float) -> List[Tuple[float, float]]:
    """Intersect windows with [lo, hi], dropping anything that falls outside."""
    out = []
    for s, e in windows or []:
        s2, e2 = max(_f(s), _f(lo)), min(_f(e), _f(hi))
        if e2 - s2 > _EPS:
            out.append((s2, e2))
    return out


def invert_windows(windows: Sequence[Sequence[float]], lo: float, hi: float) -> List[Tuple[float, float]]:
    """Gaps between windows inside [lo, hi] - where a ducked bed is restored."""
    gaps = []
    cursor = _f(lo)
    for s, e in merge_windows(windows):
        if s - cursor > _EPS:
            gaps.append((cursor, min(s, _f(hi))))
        cursor = max(cursor, e)
    if _f(hi) - cursor > _EPS:
        gaps.append((cursor, _f(hi)))
    return [(s, e) for s, e in gaps if e - s > _EPS]


# --------------------------------------------------------------------------
# Audio role detection
# --------------------------------------------------------------------------

def _reader_of(clip_data: dict, file_data: Optional[dict]) -> dict:
    """Reader info, preferring the clip's own copy (may be popped by an edit)."""
    data = clip_data if isinstance(clip_data, dict) else {}
    reader = data.get("reader")
    if isinstance(reader, dict) and reader:
        return reader
    if isinstance(file_data, dict):
        nested = file_data.get("reader")
        if isinstance(nested, dict) and nested:
            return nested
        return file_data
    return {}


def has_audio_stream(clip_data: dict, file_data: Optional[dict] = None) -> bool:
    """Same predicate Split_Audio_Triggered uses: has_audio and channels > 0."""
    reader = _reader_of(clip_data, file_data)
    has_audio = reader.get("has_audio")
    has_audio = True if has_audio is None else bool(has_audio)
    if not has_audio:
        return False
    channels = reader.get("channels")
    if channels is not None:
        try:
            return int(channels) > 0
        except (TypeError, ValueError):
            return True
    return True


def _has_video_stream(clip_data: dict, file_data: Optional[dict]) -> bool:
    from classes.image_types import is_audio_only_media

    reader = _reader_of(clip_data, file_data)
    # Extension/media_type first: libopenshot reports has_video=True for
    # cover-art MP3s, which would otherwise hide a music bed from BED_ROLES.
    probe = dict(reader)
    for key in ("path", "media_type"):
        value = (file_data or {}).get(key)
        if value and not probe.get(key):
            probe[key] = value
    if is_audio_only_media(probe):
        return False
    has_video = reader.get("has_video")
    if has_video is not None:
        return bool(has_video)
    return True


def _cues_of(effective_metadata: Optional[dict]) -> List[dict]:
    cues = (effective_metadata or {}).get("transcript_cues")
    return [c for c in cues if isinstance(c, dict)] if isinstance(cues, list) else []


def snap_cut_out_of_speech(cut_source, cues, *, max_shift=None):
    """Move *cut_source* off a spoken line, to the nearer edge of its cue.

    Cutting mid-word is the reported "sliced through the middle of a said
    speech" bug. Cues are in the same source-seconds frame as *cut_source*.
    Returns (snapped_cut, cue) - cue is None when the cut was already clean.
    """
    cut = _f(cut_source, 0.0)
    for cue in cues or []:
        if not isinstance(cue, dict):
            continue
        start = _f(cue.get("source_start", cue.get("start")), None)
        end = _f(cue.get("source_end", cue.get("end")), None)
        if start is None or end is None or end <= start:
            continue
        # Strictly inside: a cut exactly on a boundary is already clean.
        if not (start + _EPS < cut < end - _EPS):
            continue
        nearer = start if (cut - start) <= (end - cut) else end
        if max_shift is not None and abs(nearer - cut) > max_shift:
            return cut, None
        return nearer, cue
    return cut, None


def classify_clip_audio_role(
    clip_data: dict,
    file_data: Optional[dict] = None,
    effective_metadata: Optional[dict] = None,
) -> str:
    """Derive an audio role from data that already exists - no re-indexing.

    A clip with speech cues is ALWAYS 'speech', even when music dominates the
    file, so a mixed source is never auto-selected as a ducking bed.
    """
    if not has_audio_stream(clip_data, file_data):
        return ROLE_SILENT
    meta = effective_metadata if isinstance(effective_metadata, dict) else {}
    if _cues_of(meta) or meta.get("has_speech") is True:
        return ROLE_SPEECH
    if not meta.get("analyzed"):
        return ROLE_UNKNOWN
    if not _has_video_stream(clip_data, file_data):
        data = clip_data if isinstance(clip_data, dict) else {}
        duration = max(0.0, _f(data.get("end"), 0.0) - _f(data.get("start"), 0.0))
        return ROLE_MUSIC if duration >= MUSIC_MIN_DURATION else ROLE_SFX
    return ROLE_AMBIENT


# --------------------------------------------------------------------------
# Speech windows
# --------------------------------------------------------------------------

def speech_windows_from_cues(
    clip_data: dict,
    effective_metadata: Optional[dict],
) -> List[Tuple[float, float]]:
    """Transcript cues to timeline-second windows, clipped to the placement.

    Cues carry root-source seconds in ``source_start``/``source_end`` when the
    metadata was rebased; otherwise start/end are already clip-local.
    """
    data = clip_data if isinstance(clip_data, dict) else {}
    tl_start, tl_end = clip_timeline_extent(data)
    src_start = _f(data.get("start"), 0.0)
    windows = []
    for cue in _cues_of(effective_metadata):
        if cue.get("source_start") is not None:
            s = _f(cue.get("source_start"))
            e = _f(cue.get("source_end"), s)
        else:
            s = _f(cue.get("start")) + src_start
            e = _f(cue.get("end"), 0.0) + src_start
        if e <= s:
            continue
        windows.append(
            (source_to_timeline_seconds(s, data), source_to_timeline_seconds(e, data))
        )
    return clamp_windows(merge_windows(windows), tl_start, tl_end)


def speech_windows_from_energy(
    clip_data: dict,
    *,
    threshold: float = DEFAULT_ENERGY_THRESHOLD,
    min_run: float = DEFAULT_ENERGY_MIN_RUN,
) -> List[Tuple[float, float]]:
    """Loud runs in the clip's cached waveform, as timeline-second windows.

    Secondary signal only - this cannot tell speech from music, so callers must
    already know the clip carries speech.
    """
    data = clip_data if isinstance(clip_data, dict) else {}
    samples = ((data.get("ui") or {}).get("audio_data") or [])
    if not isinstance(samples, list) or len(samples) < 2:
        return []
    tl_start, tl_end = clip_timeline_extent(data)
    span = tl_end - tl_start
    if span <= _EPS:
        return []
    values = [abs(_f(s, 0.0)) for s in samples]
    peak = max(values)
    if peak <= _EPS:
        return []
    gate = peak * max(0.0, _f(threshold, DEFAULT_ENERGY_THRESHOLD))
    step = span / float(len(values))
    windows = []
    run_start = None
    for i, val in enumerate(values):
        if val >= gate:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            windows.append((tl_start + run_start * step, tl_start + i * step))
            run_start = None
    if run_start is not None:
        windows.append((tl_start + run_start * step, tl_end))
    min_run = max(0.0, _f(min_run, DEFAULT_ENERGY_MIN_RUN))
    return [w for w in merge_windows(windows, gap=min_run) if w[1] - w[0] >= min_run]


def refine_windows_with_energy(
    windows: Sequence[Sequence[float]],
    clip_data: dict,
    *,
    threshold: float = DEFAULT_ENERGY_THRESHOLD,
    min_run: float = DEFAULT_ENERGY_MIN_RUN,
) -> List[Tuple[float, float]]:
    """Trim dead lead/tail off cue windows using waveform energy.

    Returns the input untouched when no waveform is cached or the refinement
    would erase a window (cues stay authoritative).
    """
    base = merge_windows(windows)
    if not base:
        return base
    energetic = speech_windows_from_energy(
        clip_data, threshold=threshold, min_run=min_run
    )
    if not energetic:
        return base
    refined = []
    for s, e in base:
        parts = clamp_windows(energetic, s, e)
        refined.extend(parts if parts else [(s, e)])
    return merge_windows(refined)


# --------------------------------------------------------------------------
# Volume curve
# --------------------------------------------------------------------------

def make_point(x, y, interpolation: int = LINEAR) -> Dict[str, Any]:
    """A volume keyframe Point dict (same shape properties_model writes).

    LINEAR, not BEZIER: bezier handles on a duck ramp can overshoot past the
    base level and clip the bed, and hand-rolled points carry no handle geometry.
    """
    return {
        "co": {"X": float(int(round(_f(x, 1.0)))), "Y": max(0.0, _f(y, 1.0))},
        "interpolation": int(interpolation),
    }


def curve_points(clip_data: dict) -> List[Dict[str, Any]]:
    """Existing volume Points, sorted by X."""
    volume = (clip_data or {}).get("volume")
    points = volume.get("Points") if isinstance(volume, dict) else None
    if not isinstance(points, list):
        return []
    valid = [p for p in points if isinstance(p, dict) and isinstance(p.get("co"), dict)]
    return sorted(valid, key=lambda p: _f(p["co"].get("X"), 0.0))


def evaluate_volume_curve(points: Sequence[dict], x, default: float = 1.0) -> float:
    """Piecewise-linear value of a volume curve at source frame x."""
    pts = [p for p in (points or []) if isinstance(p, dict) and isinstance(p.get("co"), dict)]
    if not pts:
        return _f(default, 1.0)
    pts = sorted(pts, key=lambda p: _f(p["co"].get("X"), 0.0))
    target = _f(x, 1.0)
    if target <= _f(pts[0]["co"].get("X"), 1.0):
        return _f(pts[0]["co"].get("Y"), default)
    if target >= _f(pts[-1]["co"].get("X"), 1.0):
        return _f(pts[-1]["co"].get("Y"), default)
    for left, right in zip(pts, pts[1:]):
        x0, y0 = _f(left["co"].get("X")), _f(left["co"].get("Y"), default)
        x1, y1 = _f(right["co"].get("X")), _f(right["co"].get("Y"), default)
        if x0 <= target <= x1:
            if x1 - x0 <= _EPS:
                return y1
            return y0 + (y1 - y0) * ((target - x0) / (x1 - x0))
    return _f(pts[-1]["co"].get("Y"), default)


def auto_duck_db(
    bed_level: Optional[float],
    speech_level: Optional[float] = None,
    *,
    headroom_db: float = AUTO_DUCK_HEADROOM_DB,
    floor_db: float = AUTO_DUCK_MIN_DB,
    ceiling_db: float = AUTO_DUCK_MAX_DB,
) -> float:
    """dB of attenuation that puts *bed_level* `headroom_db` under *speech_level*.

    A fixed -12 dB duck sounds identical whatever the sources are, which is why
    every mix came out the same. Deriving it from the measured levels means an
    already-quiet bed is nudged and a loud one is cut.
    """
    bed = bed_level if bed_level and bed_level > 0 else 1.0
    speech = speech_level if speech_level and speech_level > 0 else 1.0
    needed = gain_to_db(speech) - abs(headroom_db) - gain_to_db(bed)
    return max(floor_db, min(ceiling_db, needed))


def current_static_level(clip_data: dict) -> Optional[float]:
    """The clip's level when it is a flat gain, else None (automated curve)."""
    pts = curve_points(clip_data)
    if not pts:
        return 1.0
    values = {round(_f(p["co"].get("Y"), 1.0), 6) for p in pts}
    return values.pop() if len(values) == 1 else None


def build_duck_points(
    clip_data: dict,
    fps,
    windows: Sequence[Sequence[float]],
    *,
    duck_gain: float,
    attack: float = DEFAULT_ATTACK,
    release: float = DEFAULT_RELEASE,
    pad_before: float = DEFAULT_PAD_BEFORE,
    pad_after: float = DEFAULT_PAD_AFTER,
) -> List[Dict[str, Any]]:
    """Volume Points that duck a bed under speech windows and restore between.

    Existing automation is preserved: every boundary rides the current curve, and
    existing points inside a duck window are scaled by duck_gain, so a music clip
    that already fades in keeps its fade.
    """
    data = clip_data if isinstance(clip_data, dict) else {}
    first, last = clip_source_frame_range(data, fps)
    padded = ducked_windows(
        data, windows, attack=attack, release=release,
        pad_before=pad_before, pad_after=pad_after,
    )
    if not padded:
        return []

    existing = curve_points(data)
    gain = max(0.0, _f(duck_gain, 1.0))

    def base_at(frame: int) -> float:
        return evaluate_volume_curve(existing, frame, default=1.0)

    ducked_frames: Dict[int, float] = {}
    unity_frames: Dict[int, float] = {}
    for s, e in padded:
        fs = timeline_to_source_frame(s, data, fps)
        fe = timeline_to_source_frame(e, data, fps)
        f_in = timeline_to_source_frame(s - max(0.0, _f(attack)), data, fps)
        f_out = timeline_to_source_frame(e + max(0.0, _f(release)), data, fps)
        if f_in >= fs and fs > first:
            f_in = fs - 1
        if f_out <= fe and fe < last:
            f_out = fe + 1
        unity_frames[f_in] = base_at(f_in)
        unity_frames[f_out] = base_at(f_out)
        for frame in (fs, fe):
            ducked_frames[frame] = base_at(frame) * gain

    # Keep existing automation outside the ducked spans; scale it inside them.
    duck_frame_spans = [
        (timeline_to_source_frame(s, data, fps), timeline_to_source_frame(e, data, fps))
        for s, e in padded
    ]
    for point in existing:
        frame = int(round(_f(point["co"].get("X"), 1.0)))
        if frame in ducked_frames or frame in unity_frames:
            continue
        value = _f(point["co"].get("Y"), 1.0)
        inside = any(fs <= frame <= fe for fs, fe in duck_frame_spans)
        if inside:
            ducked_frames[frame] = value * gain
        else:
            unity_frames[frame] = value

    merged = dict(unity_frames)
    merged.update(ducked_frames)  # a ducked boundary always wins a collision
    return [make_point(frame, merged[frame]) for frame in sorted(merged)]


def ducked_windows(
    clip_data: dict,
    windows: Sequence[Sequence[float]],
    *,
    attack: float = DEFAULT_ATTACK,
    release: float = DEFAULT_RELEASE,
    pad_before: float = DEFAULT_PAD_BEFORE,
    pad_after: float = DEFAULT_PAD_AFTER,
) -> List[Tuple[float, float]]:
    """Speech windows padded, merged and clipped to one clip's timeline extent.

    Windows closer together than attack+release+margin are merged so the bed
    does not pump between adjacent sentences.
    """
    data = clip_data if isinstance(clip_data, dict) else {}
    tl_start, tl_end = clip_timeline_extent(data)
    padded = [
        (_f(s) - max(0.0, _f(pad_before)), _f(e) + max(0.0, _f(pad_after)))
        for s, e in merge_windows(windows)
    ]
    gap = max(0.0, _f(attack)) + max(0.0, _f(release)) + DEFAULT_RESTORE_MARGIN
    return clamp_windows(merge_windows(padded, gap=gap), tl_start, tl_end)


def build_static_level_points(
    clip_data: dict,
    fps,
    level: float,
    *,
    start_seconds: Optional[float] = None,
    end_seconds: Optional[float] = None,
    fade: float = 0.150,
    scale: bool = False,
) -> List[Dict[str, Any]]:
    """Points for a flat gain over the whole clip, or over one timeline window."""
    data = clip_data if isinstance(clip_data, dict) else {}
    first, last = clip_source_frame_range(data, fps)
    target = max(0.0, min(MAX_LEVEL, _f(level, 1.0)))
    existing = curve_points(data)

    def value_at(frame: int) -> float:
        if not scale:
            return target
        return max(0.0, evaluate_volume_curve(existing, frame, default=1.0) * target)

    if start_seconds is None and end_seconds is None:
        return [make_point(first, value_at(first))]

    tl_start, tl_end = clip_timeline_extent(data)
    s = _f(start_seconds, tl_start) if start_seconds is not None else tl_start
    e = _f(end_seconds, tl_end) if end_seconds is not None else tl_end
    window = clamp_windows([(s, e)], tl_start, tl_end)
    if not window:
        return []
    s, e = window[0]
    fade = max(0.0, _f(fade, 0.0))
    fs = timeline_to_source_frame(s, data, fps)
    fe = timeline_to_source_frame(e, data, fps)
    f_in = timeline_to_source_frame(s - fade, data, fps)
    f_out = timeline_to_source_frame(e + fade, data, fps)
    if f_in >= fs and fs > first:
        f_in = fs - 1
    if f_out <= fe and fe < last:
        f_out = fe + 1

    frames: Dict[int, float] = {}
    for frame in (f_in, f_out):
        frames[frame] = evaluate_volume_curve(existing, frame, default=1.0)
    for frame in (fs, fe):
        frames[frame] = value_at(frame)
    return [make_point(frame, frames[frame]) for frame in sorted(frames)]
