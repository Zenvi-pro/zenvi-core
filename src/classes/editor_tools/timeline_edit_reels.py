"""build_reel_tool: a finished short-form edit in one call, from media already in the project.

Workstream: timeline-edit. A port of zenvi-web's ``tools/catalog/reels.ts``:
cuts land on the music's beats, every shot gets the style's camera move and
look, white flashes hit the downbeats, and the hook title, shot labels, end
card and the music bed (faded out) go on their own tracks. The whole edit is
ONE undo step, and a refused or failed call leaves history as it was.

``timeline_edit_reels_plan`` says how each style cuts, moves, grades and titles
and places every shot; ``timeline_edit_reels_cards`` draws the titles. This
module reads the project on the GUI thread, then works off it: the music's
beats (``classes.speech.beats``: ffmpeg and plain Python), the title SVGs
(written where add_title_tool writes titles) and the media's clip JSON. The
edit itself is one GUI-thread hop, one batch edit (``IgnoreUpdates``, as Slice
and undo/redo do), through the editor's own code paths:

* clips: ``timeline_ops.plan_placement`` / ``insert_placement_step`` (the Add to
  Timeline dialog); a clip slowed to fill its shot: ``retime.retime_clip``
  (Clip > Speed);
* the shots' camera moves: ``camera_motion`` (Clip > Motion > Camera's
  keyframes, shared with the menu's handler), written into each shot before
  it is placed, so a shot costs one save;
* the hook and end-card entrances: ``TimelineView.Animate_Triggered``
  (Clip > Motion);
* looks: ``effects_color_looks.look_effects`` (Clip > Look);
* title files: ``import_media_file``, like the Title Editor's Save.

Every shot uses scale mode crop, so landscape footage fills a vertical frame
instead of being letterboxed (at the frame's own aspect crop is fit).
Sub-clip files keep to their in/out points, and every position, in-point and
length sits exactly on the project's frame grid.
"""

from __future__ import annotations

import copy
import math
import os
import re
import time
import types
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from classes import frame_time as ft
from classes import timeline_ops, title_svg
from classes.editor_tools._base import (
    BEZIER,
    LINEAR,
    ToolError,
    array,
    boolean,
    clip_frame_at,
    constant_keyframe,
    enum,
    get_app,
    is_locked,
    keyframe_point,
    nullable,
    number,
    obj,
    ok,
    string,
    timeline_ui,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.timeline_edit_common import extend_timeline, refresh, track_name
from classes.editor_tools.timeline_edit_reels_cards import card_svg, flash_svg, js_number
from classes.editor_tools.timeline_edit_reels_plan import (
    REEL_STYLE_IDS,
    REEL_STYLES,
    MusicBeats,
    PlannedShot,
    ReelMedia,
    ReelPlan,
    ReelStyle,
    flash_indexes,
    music_from_beats,
    plan_reel,
    round3,
)
from classes.editor_tools.titles_text_common import (
    TRACK_STEP,
    CommitTimeout,
    commit_on_main,
    create_track,
    import_media_file,
    precheck_on_main,
    project_size,
    title_dir,
)
from classes.logger import log

TOOL = "build_reel_tool"
# How long a white flash fades, seconds (three frames at 30 fps).
FLASH_SECONDS = 0.1
# Flash at most once per this many seconds: a flash on every bar strobes.
FLASH_SPACING_SECONDS = 3.0
# Tracks the reel uses, bottom to top.
TRACK_ROLES = ("music", "video", "flash", "labels", "titles")
# openshot.SCALE_CROP: fill the frame, cropping what overflows (the same as fit at the frame's aspect).
SCALE_CROP = 0
# openshot.GRAVITY_CENTER
GRAVITY_CENTER = 4


# ---------------------------------------------------------------------------
# Media and names
# ---------------------------------------------------------------------------

def visual_kind(file_data: dict) -> Optional[str]:
    """'video', 'image', or None for audio and anything without a picture."""
    from classes.clip_utils import is_single_image_media
    from classes.image_types import is_audio_only_media
    if is_audio_only_media(file_data):
        return None
    if is_single_image_media(file_data):
        return "image"
    return "video" if file_data.get("has_video") else None


def usable_range(file_data: dict) -> Tuple[float, float]:
    """The usable part of a video: its in/out, less the first fifth of a second (camera settling) on longer clips."""
    from classes.clip_placement import source_window_for_file
    lo, hi = source_window_for_file(file_data)
    return (lo + 0.2, hi - 0.1) if hi - lo > 3 else (lo, hi)


def display_aspect(file_data: dict) -> Optional[float]:
    """Width / height as shown (with the pixel aspect ratio), or None when unknown."""
    try:
        w, h = float(file_data.get("width") or 0), float(file_data.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if not (w > 0 and h > 0):
        return None
    ratio = file_data.get("pixel_ratio") if isinstance(file_data.get("pixel_ratio"), dict) else {}
    try:
        num, den = float(ratio.get("num") or 0), float(ratio.get("den") or 0)
    except (TypeError, ValueError):
        num = den = 0.0
    par = num / den if num > 0 and den > 0 else 1.0
    return (w * par) / h


def display_name(file_id: str, file_data: dict) -> str:
    """'/media/goal_cam-2.mp4' -> 'goal cam 2' (the brief's story line, refusals)."""
    raw = str(file_data.get("name") or file_data.get("path") or file_id)
    base = re.split(r"[\\/]", raw)[-1]
    return re.sub(r"[_-]+", " ", re.sub(r"\.[A-Za-z0-9]+$", "", base)).strip() or file_id


def aspect_name(width: int, height: int) -> str:
    ratio = width / height
    for name, value in (("vertical 9:16", 9 / 16), ("landscape 16:9", 16 / 9), ("square 1:1", 1.0),
                        ("portrait 4:5", 4 / 5)):
        if abs(ratio - value) < 0.02:
            return name
    return f"{width}×{height}"


def story_line(plan: ReelPlan, names: Dict[str, str]) -> str:
    order: List[str] = []
    for shot in plan.shots:
        name = names.get(shot.file_id, shot.file_id)
        if not order or order[-1] != name:
            order.append(name)
    unique = [n for i, n in enumerate(order) if order.index(n) == i]
    if len(unique) == 1:
        return unique[0]
    last = order[-1]
    middle = [n for n in unique if n != unique[0] and n != last][:5]
    return ", ".join([unique[0], *middle]) + f", ends on {last}"


# ---------------------------------------------------------------------------
# Presets: the web's ids -> the desktop's Look and Motion menus
# ---------------------------------------------------------------------------

def look_preset(look_id: str) -> Tuple[str, Optional[str]]:
    """'film_grain.35mm_fine' -> the Clip > Look menu's (family, preset)."""
    from classes.editor_tools.effects_color_looks import LOOK_PRESETS
    return LOOK_PRESETS[look_id.replace(".", "_", 1)]


def motion_action(preset_id: Optional[str]):
    """'animate.pop_in' -> MenuAnimate.POP_IN; None for no move."""
    if not preset_id or preset_id == "animate.none":
        return None
    from windows.views.timeline_backend.enums import MenuAnimate
    return MenuAnimate[preset_id.split(".", 1)[1].upper()]


# The shots' camera moves (Clip > Motion > Camera): the menu's keyframes, written
# into each shot before it is placed, so a shot is one save instead of two.
CAMERA_MOVES = ("animate.cam_push_in", "animate.cam_pull_out", "animate.ken_burns_in", "animate.ken_burns_out")


def camera_move(preset_id: Optional[str], frame_size: Tuple[int, int], file_data: dict):
    """The camera_motion keyframe values Clip > Motion gives *preset_id* (None for no move)."""
    from classes import camera_motion
    if not preset_id or preset_id == "animate.none":
        return None
    if preset_id in ("animate.cam_push_in", "animate.cam_pull_out"):
        return camera_motion.push_pull_keyframes(zoom_in=preset_id.endswith("_in"))
    if preset_id in ("animate.ken_burns_in", "animate.ken_burns_out"):
        source = camera_motion.source_dimensions_from_reader(file_data)
        return camera_motion.ken_burns_keyframes(preset_id.endswith("_in"), camera_motion.KEN_BURNS_AUTO,
                                                 frame_size[0], frame_size[1], *source)
    raise ValueError(f"{preset_id} is not a camera move")


# ---------------------------------------------------------------------------
# What the tool reads before planning (GUI thread)
# ---------------------------------------------------------------------------

@dataclass
class _Project:
    files: Dict[str, dict]
    paths: Dict[str, str]
    order: List[str]
    clip_count: int
    fps: Fraction
    width: int
    height: int
    title_paths: List[str]


def _reel_layers() -> List[int]:
    """The layers the reel would use: the lowest five that exist (new ones are added unlocked)."""
    from classes.editor_tools.timeline_edit_common import track_numbers_bottom_up
    return track_numbers_bottom_up()[:len(TRACK_ROLES)]


def _check_tracks(replace: bool) -> None:
    """Refuse when a track the reel writes to (or clears, with replace) is locked."""
    from classes.query import Clip, Transition
    needed = set(_reel_layers())
    if replace:
        needed |= {int(i.data.get("layer") or 0) for i in list(Clip.filter()) + list(Transition.filter())}
    locked = sorted(n for n in needed if is_locked(n))
    if locked:
        names = ", ".join(track_name(n) for n in locked)
        raise ToolError(f"{names} {'is' if len(locked) == 1 else 'are'} locked and the reel needs "
                        f"{'it' if len(locked) == 1 else 'them'}; unlock first (Track menu > Unlock Track)")


# What the tool reads of a project file (a snapshot the worker thread can keep).
_FILE_KEYS = ("path", "name", "media_type", "has_video", "has_audio", "has_single_image", "width", "height",
              "pixel_ratio", "duration", "start", "end", "reader")


def _read_project(replace: bool) -> _Project:
    from classes.clip_utils import project_fps_fraction
    from classes.query import Clip, File
    _check_tracks(replace)
    files, paths, order, titles = {}, {}, [], []
    for f in File.filter():
        fid = str(f.id)
        data = f.data if isinstance(f.data, dict) else {}
        files[fid] = {key: copy.deepcopy(data[key]) for key in _FILE_KEYS if key in data}
        paths[fid] = f.absolute_path()
        order.append(fid)
        if str(files[fid].get("path") or "").lower().endswith(".svg"):
            titles.append(paths[fid])
    width, height = project_size()
    return _Project(files=files, paths=paths, order=order, clip_count=len(list(Clip.filter())),
                    fps=Fraction(project_fps_fraction()), width=width, height=height, title_paths=titles)


# ---------------------------------------------------------------------------
# Beats of the music (worker thread)
# ---------------------------------------------------------------------------

def _music_beats(music_id: str, music_data: dict, path: str, given: List[float],
                 warnings: List[str]) -> Optional[MusicBeats]:
    """The music's beats: given, tracked here, or None (cut to the style's tempo, with a warning)."""
    from classes.speech import beats as speech_beats
    length = float(music_data.get("duration") or 0.0)
    if len(given) >= 2:
        return music_from_beats(given, length)
    try:
        tracked = speech_beats.detect_beats(path)
    except InterruptedError:
        raise
    except Exception as exc:
        message = str(exc)
        log.warning("build_reel_tool: beat detection failed for %s: %s", path, message)
        reason = (f"Error: {music_id} has no audio." if "no audio" in message.lower()
                  else f"Error: Beat detection failed: {message}")
        warnings.append(f"{reason} Cut to the style's tempo instead.")
        return None
    times = [float(b["timeSec"]) for b in tracked.get("beats") or []]
    if len(times) < 2:
        warnings.append("No steady beat in the music: cut to the style's tempo instead.")
        return None
    return MusicBeats(
        bpm=float(tracked.get("bpm") or 0.0), beats=times,
        downbeats=[float(d["timeSec"]) for d in tracked.get("downbeats") or []],
        beat_energy=tracked.get("beatEnergy"),
        duration=max(length, float(tracked.get("duration") or 0.0)),
    )


# ---------------------------------------------------------------------------
# Building the edit (GUI thread)
# ---------------------------------------------------------------------------

@dataclass
class _Job:
    """Everything the GUI-thread hop needs, computed beforehand."""
    style: ReelStyle
    plan: ReelPlan
    replace: bool
    fps: Fraction
    frame_size: Tuple[int, int]
    files: Dict[str, dict]
    music_id: Optional[str]
    templates: Dict[str, dict]                     # file id -> libopenshot clip JSON
    flashes: List[int]
    hook: Optional[str] = None                     # card paths
    end: Optional[str] = None
    flash: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)


def _frame(seconds: float, fps: Fraction) -> int:
    return ft.to_frame(seconds, fps)


def _seconds(frame: int, fps: Fraction) -> float:
    return ft.to_seconds(frame, fps)


def _merge_points(curve: Optional[dict], points: List[Tuple[int, float]]) -> dict:
    """Add linear points to a keyframe curve, replacing any at the same frame (Keyframe::AddPoint)."""
    base = copy.deepcopy(curve) if isinstance(curve, dict) else {}
    kept = [p for p in base.get("Points") or [] if isinstance(p, dict)]
    for x, value in points:
        kept = [p for p in kept if float((p.get("co") or {}).get("X", -1)) != float(x)]
        kept.append(keyframe_point(x, value, LINEAR))
    kept.sort(key=lambda p: float((p.get("co") or {}).get("X", 0)))
    base["Points"] = kept
    return base


def _fade(data: dict, value_from: float, value_to: float, start: float, end: float) -> None:
    """Fade a clip's opacity between two timeline times (merged into its current curve)."""
    data["alpha"] = _merge_points(data.get("alpha"), [(clip_frame_at(data, start), value_from),
                                                      (clip_frame_at(data, end), value_to)])


def _placed(file_id: str, template: dict, position: float, layer: int, start: Optional[float],
            end: float) -> dict:
    """A new clip as the Add to Timeline dialog builds it (not saved)."""
    from classes.query import File
    file_obj = File.get(id=file_id)
    if file_obj is None:
        raise ToolError(f"project file {file_id} disappeared while the reel was being built")
    steps = timeline_ops.plan_placement([{"file": file_obj, "start": start, "end": end}], position, layer,
                                        read_clip_json=lambda _path: copy.deepcopy(template))
    if not steps:
        raise ToolError(f"the editor could not place {display_name(file_id, file_obj.data)}")
    return steps[0]["clip"]


def _insert(data: dict) -> str:
    clip_id, _transition = timeline_ops.insert_placement_step({"clip": data, "transition": None})
    return str(clip_id)


def _reel_tracks() -> Dict[str, int]:
    """The reel's tracks, bottom to top: the lowest five, adding tracks on top when there are fewer."""
    from classes.editor_tools.timeline_edit_common import track_numbers_bottom_up
    numbers = track_numbers_bottom_up()
    while len(numbers) < len(TRACK_ROLES):
        numbers.append(create_track((numbers[-1] if numbers else 0) + TRACK_STEP, ""))
    return dict(zip(TRACK_ROLES, numbers[:len(TRACK_ROLES)]))


def _shot_clip(job: _Job, shot: PlannedShot, index: int, layer: int, warnings: List[str]) -> dict:
    """A shot's clip: source window, speed, crop, camera move, sound, looks and the reel's opening/closing fades."""
    from windows.views.retime import retime_clip

    from classes.camera_motion import apply_camera_keyframes
    from classes.editor_tools.effects_color_looks import look_effects
    fps, style = job.fps, job.style
    file_data = job.files[shot.file_id]
    first, last = _frame(shot.start, fps), _frame(shot.end, fps)
    position, length = _seconds(first, fps), _seconds(last, fps) - _seconds(first, fps)
    if shot.kind == "video":
        lo, hi = usable_range(file_data)
        if shot.speed < 0.999:
            data = _placed(shot.file_id, job.templates[shot.file_id], position, layer,
                           _seconds(_frame(shot.source_start, fps), fps), _seconds(_frame(shot.source_end, fps), fps))
            # Slowed to fill its shot: the clip's length becomes the shot's.
            retime_clip(types.SimpleNamespace(data=data), float(data["start"]) + length, position)
        else:
            # Same length as the shot, on the frame grid, inside the usable range.
            span = last - first
            in_frame = min(max(_frame(shot.source_start, fps), _frame(lo, fps)), max(0, _frame(hi, fps) - span))
            data = _placed(shot.file_id, job.templates[shot.file_id], position, layer,
                           _seconds(in_frame, fps), _seconds(in_frame + span, fps))
    else:
        data = _placed(shot.file_id, job.templates[shot.file_id], position, layer, None, length)
    if display_aspect(file_data) is not None:
        # Fill the frame: landscape footage in a vertical reel is cropped, not letterboxed, and a
        # near-match (1080x2048 in 1080x1920) leaves no thin bars.
        data["scale"] = SCALE_CROP
    move = camera_move(style.motion_image if shot.kind == "image" else style.motion_video, job.frame_size, file_data)
    if move is not None:
        # Clip > Motion > Camera over the whole shot: centred and cropped to fill, as the menu leaves it.
        rate = float(fps)
        data["gravity"] = GRAVITY_CENTER
        data["scale"] = SCALE_CROP
        apply_camera_keyframes(data, move, round(float(data["start"]) * rate) + 1,
                               round(float(data["end"]) * rate) + 1,
                               lambda frame, value: keyframe_point(frame, value, BEZIER))
    if file_data.get("has_audio"):
        data["volume"] = constant_keyframe(style.clip_volume)
    effects = list(data.get("effects") or [])
    for look in style.looks:
        family, preset = look_preset(look)
        try:
            changed = look_effects(effects, family, preset)
        except ToolError as exc:
            note = f"skipped the {look} look: {exc}"
            if note not in warnings:
                warnings.append(note)
            continue
        if changed is not None:
            effects = changed
    data["effects"] = effects
    end = position + length
    if index == 0 and style.fade_in:
        _fade(data, 0.0, 1.0, position, position + min(0.6, length / 2))
    if index == len(job.plan.shots) - 1 and style.fade_out:
        _fade(data, 1.0, 0.0, end - min(0.8, length / 2), end)
    return data


def _check_timeline(replace: bool) -> None:
    """The timeline is still as the plan assumed (another call may have edited it meanwhile)."""
    from classes.query import Clip
    count = len(list(Clip.filter()))
    if count and not replace:
        raise ToolError(f"the timeline already has {count} clip(s). Pass replace=true to rebuild it, or build in "
                        "a new project.")
    _check_tracks(replace)


def _import_cards(job: _Job) -> Tuple[Dict[str, str], Dict[str, dict]]:
    """Add the title SVGs to Project Files (like the Title Editor's Save); their file ids and clip JSON."""
    files: Dict[str, str] = {}
    templates: Dict[str, dict] = {}
    cards = {"hook": job.hook, "end": job.end, "flash": job.flash,
             **{f"label:{text}": path for text, path in job.labels.items()}}
    for key, path in cards.items():
        if path:
            f = import_media_file(path)
            files[key] = str(f.id)
            templates[key] = timeline_ops.clip_json(f.absolute_path())
    return files, templates


def _record(job: _Job, card_files: Dict[str, str], card_templates: Dict[str, dict], tid: str) -> dict:
    """Make the reel on the timeline (GUI thread, inside the call's undo step *tid*)."""
    window = get_app().window
    # One batch edit, as Slice and undo/redo make theirs: the minimap and the
    # preview catch up once at the end instead of after each of the reel's saves.
    window.IgnoreUpdates.emit(True, True)
    try:
        return _record_batch(job, card_files, card_templates, tid, window)
    finally:
        window.IgnoreUpdates.emit(False, True)


def _record_batch(job: _Job, card_files: Dict[str, str], card_templates: Dict[str, dict], tid: str,
                  window) -> dict:
    from classes.query import Clip, Transition

    fps, style, plan = job.fps, job.style, job.plan
    warnings: List[str] = []
    for transition in list(Transition.filter()):
        transition.delete()
    for clip in list(Clip.filter()):
        clip.delete()
    tracks = _reel_tracks()

    if job.music_id is not None:
        from classes.clip_placement import source_window_for_file
        start = plan.music_start or 0.0
        first = _frame(start, fps)
        # The bed runs the length of the reel, but never past the end of the music.
        last = min(first + _frame(plan.duration, fps),
                   max(first + 1, math.floor(source_window_for_file(job.files[job.music_id])[1] * fps)))
        bed = _placed(job.music_id, job.templates[job.music_id], 0.0, tracks["music"], _seconds(first, fps),
                      _seconds(last, fps))
        out = min(1.2, plan.duration / 4)
        points = [(clip_frame_at(bed, 0.0), 0.0 if start > 0 else 1.0)]
        if start > 0:
            points.append((clip_frame_at(bed, 0.12), 1.0))
        points += [(clip_frame_at(bed, plan.duration - out), 1.0), (clip_frame_at(bed, plan.duration), 0.0)]
        bed["volume"] = _merge_points({"Points": []}, points)
        _insert(bed)

    shot_ids: List[str] = []
    flash_frames = max(1, _frame(FLASH_SECONDS, fps))
    for index, shot in enumerate(plan.shots):
        shot_ids.append(_insert(_shot_clip(job, shot, index, tracks["video"], warnings)))
        position = _seconds(_frame(shot.start, fps), fps)
        length = _seconds(_frame(shot.end, fps), fps) - position
        if job.flash and index in job.flashes:
            flash = _placed(card_files["flash"], card_templates["flash"], position, tracks["flash"], None,
                            _seconds(flash_frames, fps))
            flash["title"] = "Flash"
            _fade(flash, 0.75, 0.0, position, position + _seconds(flash_frames, fps))
            _insert(flash)
        key = f"label:{shot.label}" if shot.label else ""
        if key in card_files:
            label = _placed(card_files[key], card_templates[key], position, tracks["labels"], None, length)
            out = min(0.25, length / 4)
            _fade(label, 1.0, 0.0, position + length - out, position + length)
            _insert(label)

    def place_title(key: str, position: float, length: float) -> str:
        clip_id = _insert(_placed(card_files[key], card_templates[key], position, tracks["titles"], None, length))
        action = motion_action(style.typography.hook_in)
        if action is not None:
            # The entrance: Clip > Motion's own handler (its save ends the batch, so resume it).
            timeline_ui().Animate_Triggered(action, [clip_id], transaction_id=tid)
            window.IgnoreUpdates.emit(True, True)
        # Leave a little before the next thing happens (after the entrance, as the web does).
        live = Clip.get(id=clip_id)
        if live is not None:
            out = min(0.25, length / 4)
            faded = dict(live.data)
            _fade(faded, 1.0, 0.0, position + length - out, position + length)
            live.data = {"alpha": faded["alpha"]}
            live.save()
        return clip_id

    titles: Dict[str, Optional[str]] = {"hook": None, "end_card": None}
    total = _frame(plan.duration, fps)
    if "hook" in card_files:
        first_shot = plan.shots[0]
        hold = min(3.0, max(1.6, first_shot.end - first_shot.start))
        titles["hook"] = place_title("hook", 0.0, _seconds(max(1, min(_frame(hold, fps), total)), fps))
    if "end" in card_files:
        last_shot = plan.shots[-1]
        at = _frame(last_shot.start + min(0.3, (last_shot.end - last_shot.start) * 0.15), fps)
        if total - at >= 1:
            titles["end_card"] = place_title("end", _seconds(at, fps), _seconds(total, fps) - _seconds(at, fps))

    extend_timeline()
    refresh()
    return {"tracks": tracks, "track_numbers": {role: ui_track_number(layer) for role, layer in tracks.items()},
            "shot_ids": shot_ids, "titles": titles, "warnings": warnings}


def _commit(job: _Job) -> dict:
    """Check the timeline, import the title files and record the reel as one undo step (GUI thread).

    A failure part-way undoes what was recorded, so history is as it was.
    """
    from classes.updates import nested_transaction
    updates = get_app().updates
    _check_timeline(job.replace)
    mark = len(updates.actionHistory)
    redo = list(updates.redoHistory)
    tid = None
    try:
        with nested_transaction(updates) as tid:
            card_files, card_templates = _import_cards(job)
            return _record(job, card_files, card_templates, tid)
    except BaseException:
        _roll_back(updates, mark, redo, tid)
        raise


def _roll_back(updates, mark: int, redo: list, tid: Optional[str]) -> None:
    """Undo what a failed build recorded and restore the redo stack, so history is as it was."""
    try:
        history = updates.actionHistory
        if len(history) > mark:
            if history[-1].transaction != tid:
                # Something else was recorded after the build (undo would take that back instead).
                log.error("build_reel_tool: another edit landed after the failed build; it was not rolled back")
                return
            updates.undo()
        updates.redoHistory[:] = redo
        updates.update_watchers()
    except Exception:
        log.error("build_reel_tool: could not roll back a failed build", exc_info=True)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

_STYLE_LINES = " ".join(f"{sid}: {REEL_STYLES[sid].summary}" for sid in REEL_STYLE_IDS)


@editor_tool(
    TOOL,
    label="Build a reel",
    schema=obj({
        "style": enum(list(REEL_STYLE_IDS), f"How the reel is cut and looks (default hype). {_STYLE_LINES}",
                      "hype"),
        "file_ids": array({"type": "string", "minLength": 1},
                          "Project Files videos and images to use, in story order; the last one closes the reel. "
                          "Default: every video and image in Project Files, in their order.",
                          minItems=1, maxItems=60),
        "music_file_id": string("Music in Project Files to cut to. Default: the longest audio file there. Without "
                                "music the cuts follow the style's own tempo.", ""),
        "duration_seconds": nullable(number("Target length, seconds (default per style, 12-20). The reel ends on "
                                            "the last downbeat before it.", minimum=4, maximum=120)),
        "hook": string("Opening title over the first shots: 2-6 punchy words.", "", maxLength=80),
        "end_card": string("Closing title over the last shot (a call to action, a name, a date).", "",
                           maxLength=80),
        "end_card_subtitle": string("A smaller line under the end card.", "", maxLength=80),
        "labels": array({"type": "string", "maxLength": 40},
                        "Small labels shown on each file's shots, in file_ids order (\"\" for none).", maxItems=60),
        "beats": array({"type": "number", "minimum": 0},
                       "Beat times of the music in seconds (e.g. from detect_beats_tool) instead of analysing it "
                       "here.", maxItems=4000),
        "replace": boolean("Clear the timeline first when it already has clips (they are removed in the same undo "
                           "step).", False),
    }),
    background_safe=True,
    covers=("reel.build",),
)
def build_reel(style="hype", file_ids=None, music_file_id="", duration_seconds=None, hook="", end_card="",
               end_card_subtitle="", labels=None, beats=None, replace=False):
    """Cut a finished short-form video (Reels, TikTok, Shorts, ads) in one step from media already in the
    project: shots cut on the music's beats, camera moves, flashes, a look, an opening hook title, shot
    labels, an end card and the music faded out. One undo step.

    Put the videos/images and one music track in Project Files first (imports, stock, generated media) and
    give file_ids in story order with the payoff last. The music is analysed on this machine (tempo, beat
    grid, downbeats, loudness): the reel starts on a downbeat of its loudest stretch and ends on a downbeat;
    without music the cuts follow the style's own tempo. Pick the style that fits the ask (hype, cinematic,
    highlight, match_cut, minimal, editorial, retro). Set the frame first (set_project_profile_tool, e.g.
    vertical 9:16 for Reels/TikTok/Shorts): footage whose aspect differs from the frame is cropped to fill it.
    The timeline must be empty unless replace=true. The reel uses the bottom five tracks (music, video,
    flash, labels, titles) and adds tracks when there are fewer.

    The receipt has the timeline's beat grid (beats, downbeats: where anything else should land on the
    beat), every shot's timeline and source range with its timeline_clip_id, the hook and end-card clip ids
    (edit_title_tool changes their text), the tracks, and a brief (format, story, pace, look).

    Example: {"style": "hype", "file_ids": ["F1", "F2", "F3", "F4"], "hook": "Match day",
    "end_card": "Full game Sunday", "duration_seconds": 15}.
    """
    reel_style = REEL_STYLES[style]
    if file_ids is not None and not file_ids:
        raise ToolError("file_ids needs at least one Project Files id (or leave it out to use every video "
                        "and image)")
    started = time.monotonic()
    phases: Dict[str, float] = {}

    def lap(name: str) -> None:
        phases[name] = round(time.monotonic() - started - sum(phases.values()), 2)

    project = precheck_on_main(_read_project, bool(replace))
    files = project.files
    names = {fid: display_name(fid, data) for fid, data in files.items()}

    # Validate everything before any work (a refused call leaves history untouched).
    ids = [str(f).strip() for f in file_ids] if file_ids is not None else \
        [fid for fid in project.order if visual_kind(files[fid]) is not None]
    unknown = [fid for fid in ids if fid not in files]
    if unknown:
        raise ToolError(f"unknown file id(s): {', '.join(unknown)}.")
    not_visual = [fid for fid in ids if visual_kind(files[fid]) is None]
    if not_visual:
        raise ToolError(f"these are not videos or images: {', '.join(not_visual)}.")
    if not ids:
        raise ToolError("Project Files has no videos or images to cut. Import some first.")
    if project.clip_count > 0 and not replace:
        raise ToolError(f"the timeline already has {project.clip_count} clip(s). Pass replace=true to rebuild it, "
                        "or build in a new project.")
    music_id: Optional[str] = None
    music_file_id = str(music_file_id or "").strip()
    if music_file_id:
        if music_file_id not in files:
            raise ToolError(f"unknown music_file_id {music_file_id}.")
        if not files[music_file_id].get("has_audio"):
            raise ToolError(f"{music_file_id} has no audio.")
        music_id = music_file_id
    else:
        from classes.image_types import is_audio_only_media
        candidates = [fid for fid in project.order if is_audio_only_media(files[fid]) and fid not in ids]
        candidates.sort(key=lambda fid: -float(files[fid].get("duration") or 0.0))
        music_id = candidates[0] if candidates else None
    for fid in dict.fromkeys(ids + ([music_id] if music_id else [])):
        if not os.path.isfile(project.paths[fid]):
            raise ToolError(f"{names[fid]} is missing on disk ({project.paths[fid]}); relink it "
                            "(update_project_files_tool) or leave it out")

    label_of: Dict[str, str] = {}
    for fid, label in zip(ids, labels or []):
        if str(label).strip() and fid not in label_of:
            label_of[fid] = str(label).strip()
    media = []
    for fid in ids:
        kind = visual_kind(files[fid])
        start, end = usable_range(files[fid]) if kind == "video" else (0.0, 0.0)
        media.append(ReelMedia(file_id=fid, kind=str(kind), start=start, end=end, label=label_of.get(fid, "")))

    # The music's beats: given, tracked here, or none (cut to the style's tempo).
    warnings: List[str] = []
    lap("read")
    music = _music_beats(music_id, files[music_id], project.paths[music_id], list(beats or []), warnings) \
        if music_id else None
    try:
        plan = plan_reel(reel_style, media, music, seconds=duration_seconds, frame_rate=float(project.fps))
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    lap("beats")

    # The media's clip JSON (libopenshot opens each file once; stills are painted by Qt on the GUI thread).
    templates: Dict[str, dict] = {}
    for fid in dict.fromkeys([s.file_id for s in plan.shots] + ([music_id] if music_id else [])):
        path = project.paths[fid]
        try:
            templates[fid] = (precheck_on_main(timeline_ops.clip_json, path) if timeline_ops.paints_with_qt(path)
                              else timeline_ops.clip_json(path))
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"cannot read {names[fid]}: {exc}") from None

    job = _Job(style=reel_style, plan=plan, replace=bool(replace), fps=project.fps,
               frame_size=(project.width, project.height),
               files=files, music_id=music_id, templates=templates,
               flashes=flash_indexes(reel_style, plan.shots, FLASH_SPACING_SECONDS))

    # Title images, written where add_title_tool writes titles (before the edit is recorded).
    lap("media")
    folder = title_dir()
    written: List[str] = []
    taken = list(project.title_paths)

    def write_card(base: str, svg: str) -> str:
        path = title_svg.unique_title_path(folder, base, taken=taken)
        title_svg.write(title_svg.parse_string(svg), path)
        written.append(path)
        taken.append(path)
        return path

    def card(text: str, role: str, subtitle: str = "") -> str:
        return card_svg(project.width, project.height, text, role, reel_style.typography, subtitle)

    try:
        if str(hook or "").strip():
            job.hook = write_card(f"Reel hook {hook}", card(hook, "hook"))
        if str(end_card or "").strip():
            job.end = write_card(f"Reel end {end_card}", card(end_card, "end", str(end_card_subtitle or "")))
        if job.flashes:
            path = os.path.join(folder, f"Reel flash {project.width}x{project.height}.svg")
            if not os.path.isfile(path):
                title_svg.write(title_svg.parse_string(flash_svg(project.width, project.height)), path)
                written.append(path)
            job.flash = path
        for text in dict.fromkeys(s.label for s in plan.shots if s.label):
            job.labels[text] = write_card(f"Reel label {text}", card(text, "label"))
    except OSError as exc:
        _discard(written)
        raise ToolError(f"failed to write the reel's titles: {exc}") from None

    lap("titles")
    try:
        built = commit_on_main(_commit, job)
    except CommitTimeout:
        raise                    # it may still finish: keep its titles
    except Exception:
        _discard(written)
        raise

    lap("edit")
    log.info("build_reel_tool: %s reel, %d shots, %.1f s; seconds per phase %s", reel_style.id, len(plan.shots),
             plan.duration, phases)
    warnings += built["warnings"]
    brief = {
        "format": (f"{js_number(ft.round_half_up(plan.duration * 10) / 10)} seconds, "
                   f"{aspect_name(project.width, project.height)}"),
        "story": story_line(plan, names),
        "pace": f"{reel_style.pace} ({ft.round_half_up(plan.bpm)} BPM)",
        "look": reel_style.look,
    }
    tracks = built["tracks"]
    summary = (f"Built a {reel_style.name.lower()} reel: {len(plan.shots)} shots cut to "
               f"{ft.round_half_up(plan.bpm)} BPM, {brief['format']}."
               + "".join(f" Warning: {w}" for w in warnings))
    receipt = dict(
        style=reel_style.id,
        duration=round3(plan.duration),
        bpm=plan.bpm,
        music_file_id=music_id,
        music_start=plan.music_start,
        # The timeline's beat grid: where to put anything else that should land on the beat.
        beats=plan.beats,
        downbeats=plan.downbeats,
        shots=[{"file_id": s.file_id, "start": round3(s.start), "end": round3(s.end),
                "source_start": s.source_start, "source_end": s.source_end, "timeline_clip_id": cid}
               for s, cid in zip(plan.shots, built["shot_ids"])],
        tracks=tracks,
        track_numbers=built["track_numbers"],
        title_clips=built["titles"],
        brief=brief,
    )
    if warnings:
        receipt["warnings"] = warnings
    return ok(summary, **receipt)


def _discard(paths: List[str]) -> None:
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
