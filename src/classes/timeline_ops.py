"""Timeline edits shared by the editor UI and the agent tools (no Qt).

The menu handlers in ``windows/views/timeline.py`` / ``main_window.py`` and the
editor tools in ``classes/editor_tools/timeline_edit.py`` call these, so a rule
such as "Remove All Gaps moves overlapping clips together" lives in one place.
Everything here mutates through ``classes.query`` objects (``save()``), i.e.
through the update manager, and never touches the transaction id itself:
callers group the edits with ``classes.updates.nested_transaction``.
"""

import copy
import json
import os
import random
import threading

import openshot

from classes.query import Clip, Transition

# Positions closer than this are the same instant (well under one frame at 240 fps).
EPSILON = 1e-6


# ---------------------------------------------------------------------------
# Items on a track
# ---------------------------------------------------------------------------

def item_span(data):
    """(timeline_start, timeline_end) of a clip or transition dict."""
    position = float(data.get("position", 0.0) or 0.0)
    duration = float(data.get("end", 0.0) or 0.0) - float(data.get("start", 0.0) or 0.0)
    return position, position + max(0.0, duration)


def layer_items(layer_number, clips=True, transitions=True):
    """Clips and/or transitions on one layer, sorted by position."""
    items = []
    if clips:
        items += Clip.filter(layer=layer_number)
    if transitions:
        items += Transition.filter(layer=layer_number)
    return sorted(items, key=lambda item: item.data.get("position", 0.0))


def overlapping(layer_number, start, end, exclude_ids=(), transitions=False, tolerance=EPSILON):
    """Items on *layer_number* whose span overlaps [start, end) by more than *tolerance*."""
    exclude = set(exclude_ids or ())
    hits = []
    for item in layer_items(layer_number, clips=True, transitions=transitions):
        if item.id in exclude:
            continue
        item_start, item_end = item_span(item.data)
        if item_start < end - tolerance and item_end > start + tolerance:
            hits.append(item)
    return hits


def track_end(layer_number, exclude_ids=()):
    """Timeline end of the last clip on a layer (0.0 when the layer is empty)."""
    exclude = set(exclude_ids or ())
    ends = [item_span(c.data)[1] for c in Clip.filter(layer=layer_number) if c.id not in exclude]
    return max(ends) if ends else 0.0


# ---------------------------------------------------------------------------
# Gaps (Remove Gap / Remove All Gaps / ripple after a removal)
# ---------------------------------------------------------------------------

def find_gap(layer_number, after_seconds=0.0):
    """First empty stretch on a layer that ends after *after_seconds*.

    Returns ``(gap_start, gap_end)`` or None. Same detection as the empty
    track area menu (ShowTimelineMenu): a leading gap before the first item
    counts, and a gap containing *after_seconds* is found.
    """
    found_start = 0.0
    for item in layer_items(layer_number):
        left_edge, right_edge = item_span(item.data)
        if left_edge > found_start + EPSILON and left_edge > after_seconds:
            return found_start, left_edge
        found_start = max(found_start, right_edge)
    return None


def list_gaps(layer_number, after_seconds=0.0):
    """Every empty stretch between items on a layer that ends after *after_seconds*."""
    gaps = []
    found_start = 0.0
    for item in layer_items(layer_number):
        left_edge, right_edge = item_span(item.data)
        if left_edge > found_start + EPSILON and left_edge > after_seconds:
            gaps.append((found_start, left_edge))
        found_start = max(found_start, right_edge)
    return gaps


def close_gap(gap_start, gap_end, layer_number):
    """Remove Gap: shift every item after *gap_start* left by the gap size.

    Returns the moved items.
    """
    gap_size = gap_end - gap_start
    moved = []
    for item in Clip.filter(layer=layer_number) + Transition.filter(layer=layer_number):
        if item.data.get("position", 0.0) > gap_start:
            item.data["position"] -= gap_size
            item.save()
            moved.append(item)
    return moved


def close_all_gaps(found_start, layer_number):
    """Remove All Gaps from *found_start* on: pack the layer, moving overlapping items together.

    Returns the moved items.
    """
    clips_and_transitions = layer_items(layer_number)

    # Build groups of overlapping clips/transitions so overlapping items move together
    groups = []
    current_group = []
    current_group_start = None
    current_group_end = None
    for item in clips_and_transitions:
        left_edge, right_edge = item_span(item.data)
        if current_group and left_edge <= current_group_end:
            current_group.append(item)
            current_group_end = max(current_group_end, right_edge)
        else:
            if current_group:
                groups.append((current_group_start, current_group_end, current_group))
            current_group = [item]
            current_group_start = left_edge
            current_group_end = right_edge
    if current_group:
        groups.append((current_group_start, current_group_end, current_group))

    # Track the end of the last processed group (after shifting) and cumulative offset
    last_end = found_start
    total_offset = 0.0
    modified_items = []
    for group_start, group_end, group_items in groups:
        # Skip groups that end before the first detected gap
        if group_end <= found_start:
            last_end = max(last_end, group_end)
            continue

        # Calculate where this group would start after prior shifts
        shifted_start = group_start - total_offset

        # If there is still a gap, close it and increase the total offset
        if shifted_start > last_end:
            gap_size = shifted_start - last_end
            total_offset += gap_size
            shifted_start -= gap_size

        # Shift the entire overlapping group together
        if total_offset > EPSILON:
            for item in group_items:
                item.data["position"] -= total_offset
                modified_items.append(item)

        last_end = group_end - total_offset

    for item in modified_items:
        item.save()
    return modified_items


def close_gap_at(layer_number, start, duration):
    """Ripple after removing [start, start + duration) from a layer.

    Items that started after *start* move left by the room actually freed:
    the removed length, but never further than the next item's start (a
    clip that overlapped the removed one, e.g. across a crossfade, lands where
    the removed clip began instead of before it). Returns the moved items.
    """
    later = [item for item in layer_items(layer_number) if item.data.get("position", 0.0) > start + EPSILON]
    if not later or duration <= EPSILON:
        return []
    shift = min(float(duration), min(item.data.get("position", 0.0) for item in later) - start)
    if shift <= EPSILON:
        return []
    for item in later:
        item.data["position"] -= shift
        item.save()
    return later


def shift_after(layer_number, from_seconds, amount, exclude_ids=()):
    """Move every item on a layer starting at/after *from_seconds* by *amount* seconds.

    Positive opens room (insert edit), negative closes it. Returns the moved items.
    """
    exclude = set(exclude_ids or ())
    moved = []
    if abs(amount) <= EPSILON:
        return moved
    for item in layer_items(layer_number):
        if item.id in exclude:
            continue
        if item.data.get("position", 0.0) >= from_seconds - EPSILON:
            item.data["position"] = max(0.0, item.data.get("position", 0.0) + amount)
            item.save()
            moved.append(item)
    return moved


# ---------------------------------------------------------------------------
# Repeat (loop / ping-pong) state
# ---------------------------------------------------------------------------

def repeat_is_active(clip_data):
    """True while Clip > Speed > Repeat is applied to a clip.

    The presence of ``repeat_cache`` is not enough: project updates merge
    keys, so undoing a repeat (or Speed > Reset) leaves the cache behind. A
    live repeat always moved the clip's in/out away from the cached ones.
    """
    cache = clip_data.get("repeat_cache") if isinstance(clip_data, dict) else None
    if not isinstance(cache, dict) or not cache:
        return False
    try:
        return (abs(float(cache.get("start", 0.0)) - float(clip_data.get("start", 0.0))) > EPSILON
                or abs(float(cache.get("end", 0.0)) - float(clip_data.get("end", 0.0))) > EPSILON)
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Align
# ---------------------------------------------------------------------------

def aligned_positions(datas, align_right, to_seconds=None):
    """New positions that line up the starts (or ends) of clip/transition dicts.

    By default the starts go to the earliest start and the ends to the latest
    end (Clip menu > Align Left / Right). Returns ``{id: position}``.
    """
    spans = {d.get("id"): item_span(d) for d in datas if isinstance(d, dict)}
    if not spans:
        return {}
    if to_seconds is None:
        target = max(e for _s, e in spans.values()) if align_right else min(s for s, _e in spans.values())
    else:
        target = float(to_seconds)
    out = {}
    for item_id, (start, end) in spans.items():
        out[item_id] = start + (target - end) if align_right else target
    return out


# ---------------------------------------------------------------------------
# Add to Timeline (the dialog's placement, shared with add_clips_to_timeline_tool)
# ---------------------------------------------------------------------------

FADE_IN, FADE_OUT, FADE_IN_OUT = "Fade In", "Fade Out", "Fade In & Out"
ZOOM_RANDOM, ZOOM_IN, ZOOM_OUT = "Random", "Zoom In", "Zoom Out"


def transition_files():
    """Every built-in transition image (common first, then extra), as absolute paths."""
    from classes import info
    out = []
    for group in ("common", "extra"):
        folder = os.path.join(info.PATH, "transitions", group)
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for filename in names:
            if filename[0] == "." or "thumbs.db" in filename.lower():
                continue
            out.append(os.path.join(folder, filename))
    return out


def _point_json(x, y, interpolation):
    return json.loads(openshot.Point(x, y, interpolation).Json())


_TRANSITION_READERS = {}
_TRANSITION_READERS_LOCK = threading.Lock()


def transition_reader_json(path):
    """Reader JSON of a transition image, cached per path (rendering an SVG takes ~1 s)."""
    with _TRANSITION_READERS_LOCK:
        cached = _TRANSITION_READERS.get(path)
    if cached is None:
        cached = json.loads(openshot.QtImageReader(path).Json())
        with _TRANSITION_READERS_LOCK:
            _TRANSITION_READERS[path] = cached
    return copy.deepcopy(cached)


def place_files(entries, start_position, track_num, **options):
    """Place files back to back on one track: the Add to Timeline dialog's accept().

    Takes the same options as :func:`plan_placement`. Returns ``(clip_ids,
    transition_ids)``. The caller owns the undo group.
    """
    clip_ids, transition_ids = [], []
    for step in plan_placement(entries, start_position, track_num, **options):
        clip_id, transition_id = insert_placement_step(step)
        clip_ids.append(clip_id)
        if transition_id:
            transition_ids.append(transition_id)
    return clip_ids, transition_ids


def insert_placement_step(step):
    """Save one planned clip (and the transition leading into it). Returns their ids."""
    transition_id = None
    if step.get("transition"):
        tran = Transition()
        tran.data = step["transition"]
        tran.save()
        transition_id = tran.data.get("id")
    clip = Clip()
    clip.data = step["clip"]
    clip.save()
    return clip.data.get("id"), transition_id


def clip_json(path):
    """libopenshot's default clip JSON for a media file (reads the file; rotation comes from its metadata)."""
    return json.loads(openshot.Clip(path).Json())


def paints_with_qt(path):
    """True when libopenshot reads *path* by painting it with Qt (stills, SVG titles and transitions).

    Qt painting (fonts in particular) must not run on a plain Python thread:
    QFontCache's mutex against the GIL deadlocks the app. Video and audio go
    through FFmpeg and are safe off the GUI thread.
    """
    from classes.image_types import is_image
    return is_image({"path": str(path)}) or str(path).lower().endswith(".svg")


def plan_placement(entries, start_position, track_num, fade=None, fade_length=2.0,
                   transition_path=None, random_transitions=None, transition_length=2.0,
                   image_length=10.0, zoom=None, transition_first_clip=True,
                   read_clip_json=clip_json, read_transition_json=None):
    """Build the clips (and transitions) the Add to Timeline dialog would place, without saving.

    read_clip_json / read_transition_json let a caller on a worker thread send
    the reads that paint with Qt (see :func:`paints_with_qt`) to the GUI thread.

    Reads the media through libopenshot (slow: call it off the GUI thread when
    you can) and returns ``[{"clip": data, "transition": data or None}]`` for
    :func:`insert_placement_step`.

    entries             -- ``{"file": File, "start": s or None, "end": s or None}`` in order;
                           start/end override the file's own in/out (source seconds).
    fade                -- None, FADE_IN, FADE_OUT or FADE_IN_OUT (only without a transition);
                           clips overlap by *fade_length* so the fades cross.
    transition_path     -- a transition image; clips overlap by *transition_length*.
    random_transitions  -- pick a random image from this list for every transition instead.
    zoom                -- None, ZOOM_RANDOM, ZOOM_IN or ZOOM_OUT.
    transition_first_clip -- the dialog also wipes the first clip in over what is below it.

    """
    from classes import frame_time as ft
    from classes.clip_placement import apply_audio_only_clip_overrides
    from classes.clip_utils import project_fps_fraction

    # Frame-exact like the dialog since #181: positions snap to frames, keyframes use frame_time.
    fps = project_fps_fraction()
    fps_float = float(fps)
    position = start_position
    steps = []

    for index, entry in enumerate(entries):
        file = entry["file"]
        filename = os.path.basename(file.data["path"])

        # Create clip object for this file
        new_clip = read_clip_json(file.absolute_path())
        new_clip["position"] = ft.snap(float(position), fps)
        new_clip["layer"] = track_num
        new_clip["file_id"] = file.id
        new_clip["title"] = file.data.get("name", filename)
        new_clip["reader"] = file.data

        # Audio-only media must not composite video (cover-art MP3s
        # otherwise paint an opaque frame over every lower layer)
        apply_audio_only_clip_overrides(
            new_clip, file.data,
            constant_interpolation=openshot.CONSTANT,
            scale_none=openshot.SCALE_NONE,
        )

        # Skip any clips that are missing a 'reader' attribute
        if not new_clip.get("reader"):
            continue

        # Source in/out: the entry's override, else the file's own (sub-clips), else all of it
        start_time = 0
        end_time = new_clip["reader"]["duration"]
        if 'start' in file.data:
            start_time = file.data['start']
            new_clip["start"] = start_time
        if 'end' in file.data:
            end_time = file.data['end']
        if entry.get("start") is not None:
            start_time = float(entry["start"])
            new_clip["start"] = start_time
        if entry.get("end") is not None:
            end_time = float(entry["end"])

        new_clip["duration"] = new_clip["reader"]["duration"]
        if file.data["media_type"] == "image":
            end_time = entry.get("end") if entry.get("end") is not None else image_length
        new_clip["end"] = end_time

        # Adjust Fade of Clips (if no transition is chosen)
        if not transition_path and not random_transitions:
            if fade is not None:
                # Overlap this clip with the previous one (if any)
                position = max(start_position, new_clip["position"] - fade_length)
                new_clip["position"] = position

            if fade in (FADE_IN, FADE_IN_OUT):
                new_clip['alpha']["Points"].append(
                    _point_json(ft.keyframe_x(start_time, fps), 0.0, openshot.BEZIER))
                new_clip['alpha']["Points"].append(_point_json(
                    min(ft.keyframe_x(start_time + fade_length, fps), ft.keyframe_x(end_time, fps)),
                    1.0, openshot.BEZIER))

            if fade in (FADE_OUT, FADE_IN_OUT):
                new_clip['alpha']["Points"].append(_point_json(
                    max(ft.keyframe_x(end_time - fade_length, fps), ft.keyframe_x(start_time, fps)),
                    1.0, openshot.BEZIER))
                new_clip['alpha']["Points"].append(
                    _point_json(ft.keyframe_x(end_time, fps), 0.0, openshot.BEZIER))

        # Adjust zoom amount
        if zoom is not None:
            if zoom == ZOOM_RANDOM:
                animate_start_x = random.uniform(-0.5, 0.5)
                animate_end_x = random.uniform(-0.15, 0.15)
                animate_start_y = random.uniform(-0.5, 0.5)
                animate_end_y = random.uniform(-0.15, 0.15)
                start_scale = random.uniform(0.5, 1.5)
                end_scale = random.uniform(0.85, 1.15)
            else:
                animate_start_x = animate_end_x = animate_start_y = animate_end_y = 0.0
                start_scale, end_scale = (1.0, 1.25) if zoom == ZOOM_IN else (1.25, 1.0)

            first_frame = ft.keyframe_x(start_time, fps)
            last_frame = ft.keyframe_x(end_time, fps)
            new_clip["gravity"] = openshot.GRAVITY_CENTER
            for key, (v0, v1) in (("scale_x", (start_scale, end_scale)),
                                  ("scale_y", (start_scale, end_scale)),
                                  ("location_x", (animate_start_x, animate_end_x)),
                                  ("location_y", (animate_start_y, animate_end_y))):
                new_clip[key]["Points"].append(_point_json(first_frame, v0, openshot.BEZIER))
                new_clip[key]["Points"].append(_point_json(last_frame, v1, openshot.BEZIER))

        if (transition_path or random_transitions) and (index > 0 or transition_first_clip):
            # Add transition for this clip
            path = random.choice(random_transitions) if random_transitions else transition_path

            brightness = openshot.Keyframe()
            brightness.AddPoint(1, 1.0, openshot.BEZIER)
            brightness.AddPoint(
                round(min(transition_length, end_time - start_time) * fps_float) + 1,
                -1.0, openshot.BEZIER)
            contrast = openshot.Keyframe(3.0)

            transitions_data = {
                "layer": track_num,
                "title": "Transition",
                "type": "Mask",
                "start": 0,
                "end": min(transition_length, end_time - start_time),
                "brightness": json.loads(brightness.Json()),
                "contrast": json.loads(contrast.Json()),
                "reader": (read_transition_json or transition_reader_json)(path),
                "replace_image": False,
            }

            # Overlap this clip with the previous one (if any)
            position = max(start_position, position - transition_length)
            transitions_data["position"] = position
            new_clip["position"] = position
        else:
            transitions_data = None

        steps.append({"clip": new_clip, "transition": transitions_data})

        # Increment position by length of clip
        position += (end_time - start_time)

    return steps
