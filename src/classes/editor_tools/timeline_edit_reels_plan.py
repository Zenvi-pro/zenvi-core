"""Reel styles and planning for build_reel_tool: pure data and functions, no project access.

Ported from zenvi-web ``packages/engine/src/reels/styles.ts`` and ``reels/plan.ts``
(same styles, same numbers, same rounding), so a reel cuts the same on the
desktop and on the web. Shot lengths are in beats of the music (or of the
style's ``default_bpm`` without music), so the cut lands on the beat whatever
the tempo. Paces come from measured short-form edits: hype montages cut on
1/2 beats with 1/4-beat bursts (2-4 cuts/s at 120-150 BPM), cinematic
montages hold 1-2 beats, motion-led ads change scene every bar or two.

Planning:

  music window  the loudest stretch of the track that starts on a downbeat (a
                quiet intro is skipped), its start snapped to the project's
                frame grid; the reel ends on a downbeat
  cuts          the style's beat pattern from the first beat, the climax
                pattern from two thirds in, the last shot held under the end
                card; every cut on the frame grid, so shots meet without gaps
  shots         media in the order given (story order), the last item saved
                for the final shot (the payoff); a clip's first use starts a
                third of the way in, and a long clip shows a later moment
                each time it comes back; a shot no clip can fill is split on
                the beat instead of leaving a gap

Looks and camera moves keep the web's ids (``color.boost_color``,
``animate.cam_push_in``); timeline_edit_reels maps them onto the desktop's
Look presets and clip-menu motion presets.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from classes.frame_time import round_half_up

EPS = 1e-6
# Never cut faster than this, seconds.
MIN_SHOT_SECONDS = 0.2
# Slowest a clip may play to fill a shot.
MIN_SPEED = 0.5


def js_round(value: float) -> int:
    """JavaScript's Math.round for the non-negative values planning rounds."""
    return round_half_up(value)


def round3(value: float) -> float:
    return js_round(value * 1000) / 1000


# ---------------------------------------------------------------------------
# Styles (reels/styles.ts)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReelTypography:
    case: str                 # "upper" | "as_is": hook and end-card text case
    weight: int               # CSS font weight
    tracking: float           # letter spacing, em
    end_plate: bool           # a translucent dark plate behind the end card
    hook_in: str              # how the hook title enters (clip preset)


@dataclass(frozen=True)
class ReelStyle:
    id: str
    name: str
    summary: str              # what it suits, one line (tool descriptions, the brief)
    default_seconds: float
    default_bpm: float        # tempo of the cut grid when there is no music
    pattern: Tuple[float, ...]            # shot lengths in beats, cycled through the body
    climax: Optional[Tuple[float, ...]]   # shot lengths from two thirds in, when faster
    first_beats: float        # beats the first shot holds (under the hook title)
    last_beats: float         # beats the last shot holds (under the end card)
    motion_video: Optional[str]           # camera move per video shot; None keeps the frame still
    motion_image: str
    flash: str                # "none" | "downbeats" | "climax_downbeats": white flash frames on cuts
    looks: Tuple[str, ...]    # Look presets applied to every shot, in order
    clip_volume: float        # share of the shots' own sound kept under the music (0 = music only)
    fade_in: bool             # fade the picture in from black
    fade_out: bool            # and out to black
    typography: ReelTypography
    pace: str                 # brief lines
    look: str


_BOLD = ReelTypography(case="upper", weight=900, tracking=0.02, end_plate=False, hook_in="animate.pop_in")

REEL_STYLE_IDS = ("hype", "cinematic", "highlight", "match_cut", "minimal", "editorial", "retro")

REEL_STYLES: Dict[str, ReelStyle] = {
    "hype": ReelStyle(
        id="hype", name="Hype montage",
        summary="Energetic montage for launches, sports, events and travel: fast cuts on the beat, flashes on "
                "the downbeats, punchy colour.",
        default_seconds=15, default_bpm=128,
        pattern=(1, 1, 1, 1, 0.5, 0.5, 1, 1), climax=(0.5, 0.5, 0.5, 0.5, 1, 0.5, 0.5, 1),
        first_beats=2, last_beats=4,
        motion_video="animate.cam_push_in", motion_image="animate.ken_burns_in",
        flash="downbeats", looks=("color.boost_color", "film_grain.35mm_fine"),
        clip_volume=0, fade_in=False, fade_out=True, typography=_BOLD,
        pace="Fast cuts that land on the beat, faster in the last third, white flashes on the downbeats",
        look="Punchy colour and contrast, fine film grain, slow push-ins",
    ),
    "cinematic": ReelStyle(
        id="cinematic", name="Cinematic montage",
        summary="Moody, filmic montage for stories, brands and places: steady 2-beat shots, slow push-ins, "
                "grain, fades.",
        default_seconds=20, default_bpm=96,
        pattern=(2, 2, 2, 2), climax=(1, 1, 2, 1, 1, 2),
        first_beats=4, last_beats=4,
        motion_video="animate.cam_push_in", motion_image="animate.ken_burns_in",
        flash="none", looks=("color.lift_shadows", "film_grain.35mm_classic"),
        clip_volume=0, fade_in=True, fade_out=True,
        typography=ReelTypography(case="upper", weight=600, tracking=0.28, end_plate=False,
                                  hook_in="animate.wipe_in_fade"),
        pace="Shots hold two beats and tighten to one in the climax; fades in and out",
        look="Lifted shadows, 35mm grain, slow push-ins, spaced capitals",
    ),
    "highlight": ReelStyle(
        id="highlight", name="Highlight reel",
        summary="The best moments of the person's own clips (games, trips, events): cut on the beat, building "
                "to the big moment.",
        default_seconds=12, default_bpm=120,
        pattern=(2, 1, 1, 2), climax=(1, 1, 0.5, 0.5, 1),
        first_beats=2, last_beats=4,
        motion_video="animate.cam_push_in", motion_image="animate.ken_burns_in",
        flash="climax_downbeats", looks=("color.boost_color",),
        clip_volume=0.15, fade_in=False, fade_out=True, typography=_BOLD,
        pace="Cuts on the beat that speed up into the climax, then hold on the payoff",
        look="Punchy colour, a little of the real sound under the music",
    ),
    "match_cut": ReelStyle(
        id="match_cut", name="Match cuts",
        summary="Shots that share a shape or pose cut on every beat (circles, hands, horizons): the frame "
                "stays still so the shapes line up.",
        default_seconds=15, default_bpm=120,
        pattern=(1,), climax=None,
        first_beats=2, last_beats=4,
        motion_video=None, motion_image="animate.none",
        flash="none", looks=("color.warm_up", "film_grain.16mm_classic"),
        clip_volume=0, fade_in=False, fade_out=True,
        typography=ReelTypography(case="upper", weight=400, tracking=0.45, end_plate=False,
                                  hook_in="animate.wipe_in_fade"),
        pace="A hard cut on every beat; no camera moves, so matching shapes line up",
        look="Warm film, 16mm grain, small spaced capitals",
    ),
    "minimal": ReelStyle(
        id="minimal", name="Calm and minimal",
        summary="Slow, airy edit for wellness, food, interiors and products: long holds, gentle moves, soft "
                "fades.",
        default_seconds=20, default_bpm=80,
        pattern=(4,), climax=None,
        first_beats=4, last_beats=6,
        motion_video="animate.cam_push_in", motion_image="animate.ken_burns_in",
        flash="none", looks=("color.warm_up", "film_grain.35mm_fine"),
        clip_volume=0, fade_in=True, fade_out=True,
        typography=ReelTypography(case="as_is", weight=400, tracking=0.04, end_plate=False,
                                  hook_in="animate.wipe_in_fade"),
        pace="Long four-beat holds with gentle moves and soft fades",
        look="Warm, soft, fine grain, light type",
    ),
    "editorial": ReelStyle(
        id="editorial", name="Editorial",
        summary="Graphic, magazine-like cut for fashion, design and tech: crisp cuts on 1/2 beats, labels on "
                "the shots, clean contrast.",
        default_seconds=12, default_bpm=110,
        pattern=(1, 0.5, 0.5, 1, 1, 0.5, 0.5), climax=None,
        first_beats=2, last_beats=4,
        motion_video=None, motion_image="animate.none",
        flash="none", looks=("color.auto_contrast",),
        clip_volume=0, fade_in=False, fade_out=False,
        typography=ReelTypography(case="upper", weight=800, tracking=-0.01, end_plate=True,
                                  hook_in="animate.slide_in_left"),
        pace="Crisp cuts on half beats with a label on each shot",
        look="Clean contrast, bold tight type, still frames",
    ),
    "retro": ReelStyle(
        id="retro", name="Retro tape",
        summary="Nostalgic camcorder feel for vlogs, throwbacks and music: tape artefacts, warm colour, super 8 "
                "grain.",
        default_seconds=15, default_bpm=100,
        pattern=(2, 1, 1), climax=None,
        first_beats=2, last_beats=4,
        motion_video=None, motion_image="animate.ken_burns_in",
        flash="none", looks=("analog_tape.vhs", "film_grain.super_8", "color.warm_up"),
        clip_volume=0.1, fade_in=True, fade_out=True,
        typography=ReelTypography(case="upper", weight=800, tracking=0.08, end_plate=False,
                                  hook_in="animate.pop_in"),
        pace="Relaxed one- and two-beat cuts",
        look="VHS tape, super 8 grain, warm colour",
    ),
}


# ---------------------------------------------------------------------------
# Planning (reels/plan.ts)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReelMedia:
    file_id: str
    kind: str                 # "video" | "image"
    start: float              # usable source range, seconds (videos)
    end: float
    label: str = ""


@dataclass(frozen=True)
class MusicBeats:
    bpm: float
    beats: Sequence[float]                 # beat times in the music file, seconds
    downbeats: Sequence[float]
    beat_energy: Optional[Sequence[float]]  # loudness per beat (0..1), parallel to beats
    duration: float                        # length of the music file, seconds


@dataclass
class PlannedShot:
    file_id: str
    kind: str
    start: float              # timeline range, seconds
    end: float
    source_start: float       # source window (videos), seconds
    source_end: float
    speed: float              # 1 as shot, below 1 for a clip slowed to fill its shot
    on_downbeat: bool         # the cut into this shot is on a downbeat
    climax: bool              # the shot starts in the last third
    label: str = ""


@dataclass
class ReelPlan:
    duration: float           # total length, seconds
    bpm: float
    music_start: Optional[float]   # where the music starts in its file, seconds; None without music
    beats: List[float]        # beat and downbeat times on the timeline
    downbeats: List[float]
    shots: List[PlannedShot]


def choose_music_start(music: MusicBeats, seconds: float) -> float:
    """The downbeat the music should start on: the loudest *seconds* of the track (ties go to the earlier one)."""
    candidates = list(music.downbeats) if music.downbeats else list(music.beats)
    if not candidates:
        return 0.0
    energy = list(music.beat_energy) if music.beat_energy is not None else [1.0]
    # Relative loudness, whatever scale the analysis used.
    loudest = max([*energy, EPS])

    def energy_between(start: float, stop: float) -> float:
        total = 0.0
        count = 0
        for i, t in enumerate(music.beats):
            if start - EPS <= t < stop - EPS:
                value = music.beat_energy[i] if music.beat_energy is not None and i < len(music.beat_energy) \
                    else loudest
                total += value / loudest
                count += 1
        return total / count if count > 0 else 0.0

    best = candidates[0]
    best_score = energy_between(best, best + seconds)
    for start in candidates[1:]:
        if start + seconds > music.duration + EPS:
            break
        score = energy_between(start, start + seconds)
        if score > best_score + 0.02:
            best = start
            best_score = score
    return best


@dataclass
class _Grid:
    beats: List[float]
    downbeats: List[float]
    bpm: float
    music_start: Optional[float]


def _timeline_grid(style: ReelStyle, music: Optional[MusicBeats], seconds: float,
                   frame_rate: Optional[float]) -> _Grid:
    """Beat grid of the reel's timeline: the music's beats from its start, extended at its tempo past its end."""
    if music is None or len(music.beats) < 2 or not music.bpm > 0:
        period = 60 / style.default_bpm
        beats = []
        t = 0.0
        while t <= seconds + EPS:
            beats.append(round3(t))
            t += period
        return _Grid(beats, [b for i, b in enumerate(beats) if i % 4 == 0], style.default_bpm, None)
    chosen = choose_music_start(music, seconds)
    start = js_round(chosen * frame_rate) / frame_rate if frame_rate and frame_rate > 0 else chosen
    beats = [round3(max(0.0, t - start)) for t in music.beats if t >= start - 0.05]
    period = 60 / music.bpm
    while len(beats) < 2 or beats[-1] < seconds:
        beats.append(round3((beats[-1] if beats else 0.0) + period))
    in_music = [round3(max(0.0, t - start)) for t in music.downbeats if t >= start - 0.05]
    # Past the track's last downbeat, keep counting bars from it.
    downbeats = list(in_music)
    last_down = in_music[-1] if in_music else 0.0
    last_index = next((i for i, t in enumerate(beats) if abs(t - last_down) < 0.02), -1)
    if last_index >= 0:
        downbeats.extend(beats[i] for i in range(last_index + 4, len(beats), 4))
    return _Grid(beats, downbeats, music.bpm, round3(start))


def _time_at_beat(beats: Sequence[float], k: float) -> float:
    """Time of fractional beat *k* on the grid (interpolated; extrapolated at the last gap)."""
    i = math.floor(k)
    if i + 1 < len(beats):
        return beats[i] + (k - i) * (beats[i + 1] - beats[i])
    last = len(beats) - 1
    gap = beats[last] - beats[last - 1]
    return beats[last] + (k - last) * gap


def cut_beats(style: ReelStyle, total_beats: float) -> List[float]:
    """Cut points in beats: the first shot, the pattern (climax from two thirds in), the held last shot."""
    if total_beats <= style.first_beats + EPS:
        return [0.0, total_beats]
    cuts = [0.0, min(style.first_beats, total_beats)]
    k = cuts[1]
    i = 0
    while True:
        pattern = style.climax if style.climax and k >= (total_beats * 2) / 3 - EPS else style.pattern
        nxt = k + pattern[i % len(pattern)]
        if nxt > total_beats - style.last_beats + EPS:
            break
        cuts.append(nxt)
        k = nxt
        i += 1
    if total_beats - cuts[-1] > EPS:
        cuts.append(total_beats)
    return cuts


@dataclass
class _Slot:
    start: float
    end: float
    k0: float                 # beat indexes, so a slot that cannot be filled splits on the beat
    k1: float


def plan_reel(style: ReelStyle, media: Sequence[ReelMedia], music: Optional[MusicBeats], *,
              seconds: Optional[float] = None, frame_rate: Optional[float] = None) -> ReelPlan:
    """Plan a reel: music window, cut points, and the media and source window of every shot.

    *seconds* is the target length (default: the style's); the reel ends on the
    last downbeat before it. *frame_rate* is the project's: cuts and the music's
    in-point snap to its frame grid (as the clips will), so the beats stay where
    the cuts are.
    """
    if not media:
        raise ValueError("A reel needs at least one video or image.")
    target = max(2.0, float(seconds if seconds is not None else style.default_seconds))
    grid = _timeline_grid(style, music, target, frame_rate)

    # End on the last downbeat in the target (or the last beat), never past the music.
    music_left = music.duration - grid.music_start if music is not None and grid.music_start is not None \
        else math.inf
    limit = min(target, music_left) + EPS
    down_end = next((t for t in reversed(grid.downbeats) if t <= limit and t >= target * 0.6), None)
    beat_end = next((t for t in reversed(grid.beats) if t <= limit), None)
    duration = round3(down_end if down_end is not None else beat_end if beat_end is not None
                      else min(target, music_left))
    total_beats = next((i for i, t in enumerate(grid.beats) if abs(t - duration) < 1e-3), -1)
    beats_in_reel = float(total_beats) if total_beats >= 0 else duration / (60 / grid.bpm)

    # Cuts sit on the frame grid, so one shot ends exactly where the next begins
    # (rounding each clip's position and length on its own left one-frame gaps).
    fps = frame_rate if frame_rate and frame_rate > 0 else None

    def on_frame(t: float) -> float:
        return js_round(t * fps) / fps if fps else round3(t)

    cuts = cut_beats(style, beats_in_reel)
    slots = [_Slot(on_frame(_time_at_beat(grid.beats, cuts[i])), on_frame(_time_at_beat(grid.beats, cuts[i + 1])),
                   cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
    if slots:
        slots[-1].end = on_frame(duration)

    cursors: Dict[str, float] = {}

    def next_from(m: ReelMedia, length: float) -> float:
        # A clip's first use starts a third of the way in: stock and phone footage often opens on a
        # rack focus or a shake.
        if m.file_id in cursors:
            return cursors[m.file_id]
        return m.start + max(0.0, (m.end - m.start - length) * 0.3)

    def fits(m: ReelMedia, length: float) -> bool:
        return m.kind == "image" or m.end - m.start >= length * MIN_SPEED - EPS

    # The last item is the payoff: it closes the reel (when there are enough items to tell a story).
    payoff = media[-1] if len(media) >= 3 else None
    body = list(media[:-1]) if payoff is not None else list(media)

    shots: List[PlannedShot] = []
    rotation = 0
    queue: Deque[_Slot] = deque(slots)
    while queue:
        slot = queue.popleft()
        length = slot.end - slot.start
        is_last = not queue
        chosen: Optional[ReelMedia] = None
        if is_last and payoff is not None and fits(payoff, length):
            chosen = payoff
        j = 0
        while chosen is None and j < len(body):
            m = body[(rotation + j) % len(body)]
            if fits(m, length):
                chosen = m
                rotation = (rotation + j + 1) % len(body)
            j += 1
        if chosen is None:
            # Nothing is long enough: split on the beat (down to the shortest cut) instead of leaving a gap.
            mid = (slot.k0 + slot.k1) / 2
            at = on_frame(_time_at_beat(grid.beats, mid))
            if at - slot.start >= MIN_SHOT_SECONDS and slot.end - at >= MIN_SHOT_SECONDS:
                queue.appendleft(_Slot(at, slot.end, mid, slot.k1))
                queue.appendleft(_Slot(slot.start, at, slot.k0, mid))
                continue
            chosen = max(media, key=lambda m: m.end - m.start)
        source_start = 0.0
        source_end = length
        speed = 1.0
        if chosen.kind == "video":
            usable = chosen.end - chosen.start
            if usable >= length - EPS:
                source_start = next_from(chosen, length)
                if source_start + length > chosen.end + EPS:
                    source_start = chosen.start
                # On the frame grid too: an in-point and an out-point rounded to frames on their own can lose a frame.
                source_start = on_frame(source_start)
                if source_start + length > chosen.end + EPS:
                    source_start = max(chosen.start, on_frame(chosen.end - length - (0.5 / fps if fps else 0)))
                source_end = source_start + length
                # Next time this clip comes back, show a later moment.
                cursors[chosen.file_id] = source_end + min(0.5, length / 4)
            else:
                source_start = chosen.start
                source_end = chosen.end
                speed = max(MIN_SPEED, usable / length)
        on_downbeat = any(abs(d - slot.start) < 0.03 for d in grid.downbeats)
        shots.append(PlannedShot(
            file_id=chosen.file_id, kind=chosen.kind, start=slot.start, end=slot.end,
            source_start=round3(source_start), source_end=round3(source_end), speed=round3(speed),
            on_downbeat=on_downbeat, climax=slot.start >= (duration * 2) / 3 - EPS, label=chosen.label,
        ))
    shots.sort(key=lambda s: s.start)
    return ReelPlan(
        duration=on_frame(duration), bpm=grid.bpm, music_start=grid.music_start,
        beats=[t for t in grid.beats if t <= duration + EPS],
        downbeats=[t for t in grid.downbeats if t <= duration + EPS],
        shots=shots,
    )


def music_from_beats(times: Sequence[float], duration: float) -> MusicBeats:
    """Music from beat times given by the caller (evenly spaced: tempo from the median gap)."""
    beats = sorted(float(t) for t in times)
    gaps = sorted(b - a for a, b in zip(beats, beats[1:]))
    median = gaps[len(gaps) // 2] if gaps else 0.5
    return MusicBeats(bpm=60 / median if median > 0 else 120, beats=beats,
                      downbeats=[t for i, t in enumerate(beats) if i % 4 == 0], beat_energy=None,
                      duration=duration)


def flash_indexes(style: ReelStyle, shots: Sequence[PlannedShot], spacing: float) -> List[int]:
    """The shots whose cut flashes: downbeats (in the climax only, for some styles), *spacing* s apart."""
    out: List[int] = []
    last = -math.inf
    for index, shot in enumerate(shots):
        if index == 0 or not shot.on_downbeat or style.flash == "none":
            continue
        if style.flash == "climax_downbeats" and not shot.climax:
            continue
        if shot.start - last < spacing:
            continue
        out.append(index)
        last = shot.start
    return out
