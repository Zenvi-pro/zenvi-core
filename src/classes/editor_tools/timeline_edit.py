"""Timeline structure: place many files, move, nudge, trim, slice, ripple, copy/paste, align, gaps, speed, repeat, freeze, separate audio.

Workstream: timeline-edit. Tools register themselves with ``@editor_tool`` from
``classes.editor_tools._registry``; helpers live in ``classes.editor_tools._base``
and ``timeline_edit_common``. The rules the menus also use (gaps, align, ripple,
Add to Timeline placement) live in ``classes.timeline_ops``; speed, repeat,
freeze and audio tools are in ``timeline_edit_time``.
"""

from __future__ import annotations

import copy
import os
import random

from classes import timeline_ops
from classes.editor_tools._base import (
    CLIPS_TARGET,
    ToolError,
    array,
    boolean,
    enum,
    get_app,
    integer,
    layers,
    mapping,
    number,
    obj,
    ok,
    on_main,
    resolve_layer,
    string,
    timeline_ui,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.timeline_edit_common import (
    all_clip_ids,
    check_overlaps,
    clip_summary,
    default_layer,
    extend_timeline,
    frame_seconds,
    fresh,
    is_image,
    new_overlaps,
    r3,
    refresh,
    remap_layers,
    require_unlocked,
    save_clip,
    shifted_receipt,
    snap,
    span,
    targets,
    time_arg,
    title,
    tolerance,
    track_name,
)

_OVERLAP_ARG = boolean(
    "Let the result overlap other clips on the track (the receipt lists what it covers). Off by default: "
    "the tool refuses and says what is in the way.", False)
_RIPPLE_NOTE = "Shift the later clips on the track to follow (close/open the room), like a ripple edit."


def _clips_or_query_given(timeline_clip_ids, clip_query, scope) -> bool:
    return bool(timeline_clip_ids) or bool((clip_query or "").strip()) or bool((scope or "").strip())


def _finish_edit():
    extend_timeline()
    refresh()


# ---------------------------------------------------------------------------
# Add many files (the Add to Timeline dialog)
# ---------------------------------------------------------------------------

_FADES = {"none": None, "in": timeline_ops.FADE_IN, "out": timeline_ops.FADE_OUT,
          "in_out": timeline_ops.FADE_IN_OUT}
_ZOOMS = {"none": None, "in": timeline_ops.ZOOM_IN, "out": timeline_ops.ZOOM_OUT,
          "random": timeline_ops.ZOOM_RANDOM}
_ITEM = mapping(
    "One file to place, with optional source in/out points in seconds of that file.",
    properties={
        "file_id": string("Project file id (list_files_tool). Sub-clip file ids work too."),
        "source_start": number("Source in point in seconds; -1 or omitted = the file's own in point."),
        "source_end": number("Source out point in seconds; -1 or omitted = the file's own out point. "
                             "For a still image this sets how long it stays on screen."),
    },
    additionalProperties=False, required=["file_id"])


def _transition_choice(name):
    """'' -> (None, None); 'random' -> (None, [all]); a name -> (path, None)."""
    key = (name or "").strip().lower().replace(" ", "_").replace("-", "_")
    for ext in (".svg", ".png", ".jpg"):
        if key.endswith(ext):
            key = key[: -len(ext)]
    if key in ("", "none", "cut", "hard_cut"):
        return None, None
    files = timeline_ops.transition_files()
    if not files:
        raise ToolError("no transition images are installed")
    if key == "random":
        return None, files
    if key in ("crossfade", "cross_fade", "dissolve", "cross_dissolve"):
        key = "fade"
    stems = {os.path.splitext(os.path.basename(p))[0].lower(): p for p in files}
    if key in stems:
        return stems[key], None
    near = sorted(s for s in stems if key in s)
    if len(near) == 1:
        return stems[near[0]], None
    sample = near[:8] or sorted(stems)[:8]
    raise ToolError(f"unknown transition {name!r}; use 'fade' (crossfade), 'random', or a name such as "
                    f"{', '.join(sample)} (list_transitions_tool lists all)")


def _fit_lengths(durations, fixed, keep, budget):
    """Water-fill: trim the longest flexible clips to one common cap so the lengths sum to *budget*.

    Clips shorter than the cap keep their length; no clip goes below its *keep* floor.
    Returns the new lengths (frame-snapped) or None when *budget* is unreachable.
    """
    flex = [i for i in range(len(durations)) if not fixed[i]]
    fixed_sum = sum(durations[i] for i in range(len(durations)) if fixed[i])

    def total_at(cap):
        return fixed_sum + sum(min(durations[i], max(cap, keep[i])) for i in flex)

    if total_at(0.0) > budget + tolerance():
        return None
    lo, hi = 0.0, max(durations)
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if total_at(mid) > budget:
            hi = mid
        else:
            lo = mid
    frame = frame_seconds()
    out = list(durations)
    for i in flex:
        target = min(durations[i], max(lo, keep[i]))
        out[i] = durations[i] if target >= durations[i] - 1e-9 else max(frame, int(target / frame) * frame)
    # Hand the frames lost to rounding back to the longest trimmed clips.
    trimmed = sorted((i for i in flex if out[i] < durations[i] - 1e-9), key=lambda i: -out[i])
    while sum(out) < budget - frame / 2:
        grew = False
        for i in trimmed:
            if sum(out) >= budget - frame / 2:
                break
            if out[i] + frame <= durations[i] + 1e-9:
                out[i] += frame
                grew = True
        if not grew:
            break
    return out


@editor_tool(
    "add_clips_to_timeline_tool",
    label="Add clips to timeline",
    schema=obj({
        "file_ids": array({"type": "string"}, "Project file ids to place, in timeline order (list_files_tool). "
                          "Use this OR items.", []),
        "items": array(_ITEM, "Files with their own in/out points, in timeline order. Use this OR file_ids.", []),
        "order": enum(["as_given", "shuffle", "name"], "Order on the timeline: as given, shuffled, or by file name.",
                      "as_given"),
        "start_seconds": number("Timeline time of the first clip; -1 = right after the last clip already on "
                                "the track (0 on an empty track).", -1.0),
        "track": string("Track to fill: UI track number (1 = bottom), name, or layer number. Empty = the "
                        "selected track, else the bottom track.", ""),
        "image_seconds": number("How long each still image stays on screen; 0 = the Default Image Length "
                                "preference.", 0.0, minimum=0),
        "fit_total_seconds": number("Make the whole sequence last exactly this long (e.g. 60 for a 60-second "
                                    "vlog) by trimming the longest clips evenly, keeping the middle of each "
                                    "(heads and tails are usually shaky); 0 = full lengths.", 0.0, minimum=0),
        "min_clip_seconds": number("Shortest a clip may be trimmed to by fit_total_seconds.", 1.0, minimum=0.1),
        "transition": string("Transition between consecutive clips: '' = hard cuts, 'fade' = crossfade, "
                             "'random', or a transition name such as 'wipe_left_to_right' or "
                             "'circle_in_to_out' (list_transitions_tool).", ""),
        "transition_seconds": number("Length of each transition; clips overlap by this much. 0 = the Default "
                                     "Transition Length preference.", 0.0, minimum=0),
        "fade": enum(["none", "in", "out", "in_out"], "Opacity fades on every clip, used instead of a "
                     "transition; clips overlap by fade_seconds so the fades cross.", "none"),
        "fade_seconds": number("Fade length in seconds.", 2.0, minimum=0.04),
        "zoom": enum(["none", "in", "out", "random"], "Slow zoom on every clip (Ken Burns): in = 100% to 125%, "
                     "out = 125% to 100%, random = random zoom and pan.", "none"),
        "allow_overlap": _OVERLAP_ARG,
    }),
    background_safe=True,
    covers=("clip.add_many",),
)
def add_clips_to_timeline(file_ids=[], items=[], order="as_given", start_seconds=-1.0, track="",
                          image_seconds=0.0, fit_total_seconds=0.0, min_clip_seconds=1.0, transition="",
                          transition_seconds=0.0, fade="none", fade_seconds=2.0, zoom="none",
                          allow_overlap=False):
    """Place several project files back to back on one track in one step: the backbone of
    "make a 60-second vlog from these clips", "put all the beach shots on the timeline with
    crossfades", "make a slideshow of these photos, 3 seconds each".

    Works like Project Files > Add to Timeline: files go on one track in the given order (or
    shuffled / by name), starting at start_seconds (default: after the last clip on that track),
    with optional crossfades or other transitions between consecutive clips, opacity fades, and a
    slow zoom. Still images last image_seconds. Give per-file in/out points with items, e.g.
    items=[{"file_id": "F1", "source_start": 4, "source_end": 9}, {"file_id": "F2"}].

    fit_total_seconds trims the sequence to an exact length: the longest clips are cut to one
    common length (never below min_clip_seconds), keeping the middle of each; clips given with
    explicit in/out points are never trimmed; transition overlaps are accounted for. Example:
    8 files, fit_total_seconds=60, transition="fade", transition_seconds=0.5.

    For one file at a precise time use add_clip_to_timeline_tool. The call is one undo step.
    Returns the new timeline clip ids in order with their positions and source in/out.
    """
    from classes.clip_placement import source_window_for_file
    from classes.query import File

    if file_ids and items:
        raise ToolError("give file_ids (whole files) or items (with in/out points), not both")
    specs = [{"file_id": fid} for fid in file_ids] if file_ids else [dict(it) for it in items]
    if not specs:
        raise ToolError("say which files to place: file_ids or items (list_files_tool lists the project files)")
    if fade != "none" and (transition or "").strip():
        raise ToolError("choose a transition or fades, not both (fade applies only without a transition)")

    settings = get_app().get_settings()
    image_len = float(image_seconds or settings.get("default-image-length") or 10.0)
    trans_path, trans_random = _transition_choice(transition)
    has_transition = bool(trans_path or trans_random)
    trans_len = float(transition_seconds or settings.get("default-transition-length") or 1.0)
    overlap = trans_len if has_transition else (float(fade_seconds) if fade != "none" else 0.0)
    tol = tolerance()

    entries = []
    for spec in specs:
        fid = str(spec.get("file_id") or "").strip()
        f = File.get(id=fid) if fid else None
        if not f:
            raise ToolError(f"no project file with id={fid!r} (list_files_tool lists them)")
        name = str(f.data.get("name") or os.path.basename(str(f.data.get("path") or fid)))
        s_arg = spec.get("source_start")
        e_arg = spec.get("source_end")
        s_arg = None if s_arg is None or float(s_arg) < 0 else float(s_arg)
        e_arg = None if e_arg is None or float(e_arg) < 0 else float(e_arg)
        image = f.data.get("media_type") == "image" or bool(f.data.get("has_single_image"))
        if image:
            length = (e_arg - (s_arg or 0.0)) if e_arg is not None else image_len
            if length <= tol:
                raise ToolError(f"{name}: an image needs a positive on-screen length")
            entries.append({"file": f, "name": name, "image": True, "start": None, "length": length,
                            "fixed": e_arg is not None, "lo": 0.0})
            continue
        lo, hi = source_window_for_file(f.data)
        s = lo if s_arg is None else s_arg
        e = hi if e_arg is None else e_arg
        if s < lo - tol or e > hi + tol or e - s <= tol:
            raise ToolError(f"{name}: source range {s:.2f}-{e:.2f} s is outside the file ({lo:.2f}-{hi:.2f} s)")
        s, e = max(lo, snap(s)), min(hi, snap(e))
        entries.append({"file": f, "name": name, "image": False, "start": s, "length": e - s,
                        "fixed": s_arg is not None or e_arg is not None, "lo": s})

    if order == "shuffle":
        random.shuffle(entries)
    elif order == "name":
        entries.sort(key=lambda en: en["name"].lower())

    n = len(entries)
    frame = frame_seconds()
    keep = []
    for i, en in enumerate(entries):
        sides = 0 if n == 1 else (1 if i in (0, n - 1) else 2)
        need = sides * overlap + frame
        if en["length"] < need - tol:
            raise ToolError(f"{en['name']} is {en['length']:.2f} s, too short for {overlap:.2f} s "
                            f"{'transitions' if has_transition else 'fades'} on {sides} side(s); shorten "
                            f"{'transition_seconds' if has_transition else 'fade_seconds'} or leave it out")
        keep.append(max(float(min_clip_seconds), need))

    lengths = [en["length"] for en in entries]
    total = sum(lengths) - (n - 1) * overlap
    fit_note = ""
    trimmed_ids = []
    if fit_total_seconds > 0 and total > fit_total_seconds + tol:
        budget = float(fit_total_seconds) + (n - 1) * overlap
        new_lengths = _fit_lengths(lengths, [en["fixed"] for en in entries], keep, budget)
        if new_lengths is None:
            shortest = sum(en["length"] if en["fixed"] else min(en["length"], k)
                           for en, k in zip(entries, keep)) - (n - 1) * overlap
            raise ToolError(f"cannot fit {n} clips into {fit_total_seconds:.1f} s: with at least "
                            f"{min_clip_seconds:.1f} s each (plus transition overlaps) the shortest possible "
                            f"sequence is {shortest:.1f} s; lower min_clip_seconds, use fewer clips, or raise "
                            "fit_total_seconds")
        for en, new_len in zip(entries, new_lengths):
            if new_len < en["length"] - 1e-9:
                if not en["image"]:
                    en["start"] = snap(en["start"] + (en["length"] - new_len) / 2.0)
                en["length"] = new_len
                trimmed_ids.append(en["file"].id)
        total = sum(en["length"] for en in entries) - (n - 1) * overlap
        fit_note = f", trimmed {len(trimmed_ids)} clip(s) to fit {fit_total_seconds:g} s"
    elif fit_total_seconds > 0:
        fit_note = f", already within {fit_total_seconds:g} s so nothing was trimmed"

    layer = resolve_layer(track) if (track or "").strip() else default_layer()
    require_unlocked([layer])
    start = snap(start_seconds) if start_seconds >= 0 else snap(timeline_ops.track_end(layer))

    planned, position = [], start
    for i, en in enumerate(entries):
        if i > 0:
            position -= overlap
        planned.append((en["name"], layer, position, position + en["length"]))
        position += en["length"]
    check_overlaps(new_overlaps({}, planned), allow_overlap, "Placing these clips", ripple_hint=False)

    core_entries = []
    for en in entries:
        if en["image"]:
            core_entries.append({"file": en["file"], "start": None, "end": en["length"]})
        else:
            core_entries.append({"file": en["file"], "start": en["start"], "end": en["start"] + en["length"]})
    # Reading the media is slow (libopenshot opens every file): video/audio are read here,
    # off the GUI thread, then each clip is inserted in its own short main-thread hop.
    # Stills, SVG titles and transition images are painted by Qt: that part runs on the GUI
    # thread (Qt text rendering off a plain thread deadlocks against the GIL).
    def _read_clip_json(path):
        if timeline_ops.paints_with_qt(path):
            return on_main(timeline_ops.clip_json, path)
        return timeline_ops.clip_json(path)

    def _read_transition_json(path):
        return on_main(timeline_ops.transition_reader_json, path)

    steps = timeline_ops.plan_placement(
        core_entries, start, layer, fade=_FADES[fade], fade_length=float(fade_seconds),
        transition_path=trans_path, random_transitions=trans_random, transition_length=trans_len,
        image_length=image_len, zoom=_ZOOMS[zoom], transition_first_clip=False,
        read_clip_json=_read_clip_json, read_transition_json=_read_transition_json)
    if not steps:
        raise ToolError("the editor placed no clips (the files have no readable media)")

    def _insert(step):
        require_unlocked([layer])
        return timeline_ops.insert_placement_step(step)

    clip_ids, transition_ids = [], []
    for step in steps:
        clip_id, transition_id = on_main(_insert, step)
        clip_ids.append(clip_id)
        if transition_id:
            transition_ids.append(transition_id)
    on_main(_finish_edit)

    placed = [clip_summary(fresh(cid)) for cid in clip_ids if fresh(cid)]
    end = max(p["end"] for p in placed)
    how = f", {os.path.splitext(os.path.basename(trans_path))[0]} transitions" if trans_path else (
        ", random transitions" if trans_random else (f", fade {fade}" if fade != "none" else ""))
    return ok(f"Placed {len(placed)} clip(s) on {track_name(layer)} from {start:.2f} s to {end:.2f} s "
              f"({end - start:.1f} s{how}{fit_note}).",
              track=ui_track_number(layer), start=r3(start), end=r3(end), total_seconds=r3(end - start),
              clips=placed, transition_ids=transition_ids, trimmed_file_ids=trimmed_ids,
              overlap_seconds=r3(overlap))


# ---------------------------------------------------------------------------
# Move / nudge / reorder
# ---------------------------------------------------------------------------

@editor_tool(
    "move_clips_tool",
    label="Move clips",
    schema=obj({
        **CLIPS_TARGET,
        "position_seconds": number("New timeline start of the (first) clip; -1 = keep the time.", -1.0),
        "by_seconds": number("Move by this many seconds (negative = earlier).", 0.0),
        "by_frames": integer("Nudge by this many frames (negative = earlier), like Ctrl+Left/Right.", 0),
        "to_track": string("Destination track: UI track number (1 = bottom), name, or layer number. "
                           "Empty = stay on the same track(s).", ""),
        "after_clip_id": string("Place the clip(s) right after this timeline clip, on its track.", ""),
        "before_clip_id": string("Place the clip(s) right before this timeline clip, on its track.", ""),
        "ripple": boolean("Reorder instead of overwrite: close the hole the clips leave and push the clips at the "
                          "destination later to make room (use this to reorder a sequence).", False),
        "allow_overlap": _OVERLAP_ARG,
    }),
    covers=("clip.move", "clip.nudge"),
)
def move_clips(timeline_clip_ids=[], clip_query="", track="", scope="", position_seconds=-1.0, by_seconds=0.0,
               by_frames=0, to_track="", after_clip_id="", before_clip_id="", ripple=False, allow_overlap=False):
    """Move clips to another time and/or track, nudge them by frames, or reorder a sequence.

    Use for "move the intro to the end", "put the drone shot before the beach clip", "move this
    to track 2", "shift everything on track 1 two seconds later", "nudge it 3 frames left".
    Several clips keep their spacing and their relative tracks. Give exactly one of
    position_seconds, by_seconds, by_frames, after_clip_id or before_clip_id (or only to_track).

    Without ripple the clips are placed over whatever is there; the tool refuses if that would
    cover another clip unless allow_overlap=true. With ripple=true the move is a reorder: the
    hole left behind closes and the clips at the destination slide later to make room, e.g.
    move the first clip to the end of track 1: timeline_clip_ids=[intro], after_clip_id=<last
    clip>, ripple=true. position_seconds counts in the timeline as it is before the move.
    Transitions stay where they are (only clips move). On a crossfaded sequence an insertion
    point falls inside the previous clip's tail and is refused: reorder first, then add the
    transitions. One undo step. Locked tracks are refused.
    """
    from classes.query import Clip, Transition

    if not _clips_or_query_given(timeline_clip_ids, clip_query, scope):
        raise ToolError("say which clips to move: timeline_clip_ids, clip_query, or scope")
    clips = targets(timeline_clip_ids, clip_query, track, scope)
    ids = {c.id for c in clips}
    modes = [position_seconds >= 0, by_seconds != 0, by_frames != 0, bool(after_clip_id.strip()),
             bool(before_clip_id.strip())]
    if sum(modes) > 1:
        raise ToolError("give only one of position_seconds, by_seconds, by_frames, after_clip_id, before_clip_id")
    if not any(modes) and not to_track.strip():
        raise ToolError("nothing to do: give position_seconds, by_seconds, by_frames, after_clip_id, "
                        "before_clip_id or to_track")

    anchor = None
    if after_clip_id.strip() or before_clip_id.strip():
        aid = (after_clip_id or before_clip_id).strip()
        anchor = Clip.get(id=aid)
        if not anchor:
            raise ToolError(f"no timeline clip with id={aid!r}")
        if anchor.id in ids:
            raise ToolError("the anchor clip is one of the clips being moved")
        if to_track.strip():
            raise ToolError("after_clip_id/before_clip_id already choose the track (the anchor's); drop to_track")

    src_layers = {int(c.data.get("layer") or 0) for c in clips}
    if anchor:
        layer_map = remap_layers(src_layers, str(ui_track_number(int(anchor.data.get("layer") or 0))))
    elif to_track.strip():
        layer_map = remap_layers(src_layers, to_track)
    else:
        layer_map = {n: n for n in src_layers}
    require_unlocked(src_layers | set(layer_map.values()))

    tol = tolerance()
    frame = frame_seconds()
    spans = {c.id: span(c.data) for c in clips}
    g_start = min(s for s, _e in spans.values())
    g_end = max(e for _s, e in spans.values())
    length = g_end - g_start

    before = {c.id: (int(c.data.get("layer") or 0), spans[c.id][0]) for c in clips}
    moved_items = {}  # id -> (obj, new_position, new_layer)
    hits = []

    if not ripple:
        if position_seconds >= 0:
            delta = snap(position_seconds) - g_start
        elif by_frames:
            delta = by_frames * frame
        elif by_seconds:
            delta = snap(g_start + by_seconds) - g_start
        elif anchor:
            a_start, a_end = span(anchor.data)
            delta = (a_end - g_start) if after_clip_id.strip() else (a_start - length - g_start)
        else:
            delta = 0.0
        if g_start + delta < -tol:
            raise ToolError(f"that would start the clip(s) at {g_start + delta:.2f} s, before the timeline start")
        changes = {}
        for c in clips:
            s, e = spans[c.id]
            new_layer = layer_map[int(c.data.get("layer") or 0)]
            changes[c.id] = (new_layer, max(0.0, s + delta), max(0.0, s + delta) + (e - s))
            moved_items[c.id] = (c, max(0.0, s + delta), new_layer)
        if all(abs(changes[c.id][1] - spans[c.id][0]) <= tol and changes[c.id][0] == before[c.id][0]
               for c in clips):
            raise ToolError("the clips are already there; nothing moved")
        hits = check_overlaps(new_overlaps(changes), allow_overlap, "Moving the clip(s)")
    else:
        if len(src_layers) != 1 or len(set(layer_map.values())) != 1:
            raise ToolError("ripple moves clips of one track at a time")
        src = next(iter(src_layers))
        dst = next(iter(layer_map.values()))
        between = [c for c in Clip.filter(layer=src)
                   if c.id not in ids and g_start + tol < span(c.data)[0] < g_end - tol]
        if between:
            raise ToolError(f"ripple needs one contiguous run of clips; {title(between[0])!r} sits between them")
        inner_trans = [t for t in Transition.filter(layer=src)
                       if g_start - tol <= span(t.data)[0] and span(t.data)[1] <= g_end + tol]
        moving_ids = ids | {t.id for t in inner_trans}

        # 1. close the hole at the source (positions after the close, by id)
        pos = {}
        objs = {}
        for item in timeline_ops.layer_items(src) + (timeline_ops.layer_items(dst) if dst != src else []):
            objs[item.id] = item
            pos[item.id] = float(item.data.get("position") or 0.0)
        later = [i for i in timeline_ops.layer_items(src) if i.id not in moving_ids and pos[i.id] > g_start + tol]
        freed = min(length, min(pos[i.id] for i in later) - g_start) if later else 0.0
        for i in later:
            pos[i.id] -= freed

        # 2. insertion time in the closed-up timeline
        def closed(t):
            if dst != src or t <= g_start:
                return t
            return max(g_start, t - freed)

        if anchor:
            a_start, a_end = pos[anchor.id], pos[anchor.id] + (span(anchor.data)[1] - span(anchor.data)[0])
            insert_at = a_end if after_clip_id.strip() else a_start
        elif position_seconds >= 0:
            insert_at = closed(snap(position_seconds))
        elif by_frames or by_seconds:
            insert_at = closed(snap(g_start + (by_frames * frame if by_frames else by_seconds)))
        else:
            insert_at = g_start if dst != src else None
            if insert_at is None:
                raise ToolError("the clips are already there; nothing moved")
        insert_at = max(0.0, insert_at)

        # 3. open room at the destination
        for item in timeline_ops.layer_items(dst, transitions=False):
            if item.id in moving_ids:
                continue
            s = pos[item.id]
            e = s + (span(item.data)[1] - span(item.data)[0])
            if s < insert_at - tol and e > insert_at + tol and not allow_overlap:
                raise ToolError(f"{insert_at:.2f} s falls inside {title(item)!r} ({s:.2f}-{e:.2f} s); insert at its "
                                "start or end, slice it first (slice_clips_tool), or pass allow_overlap=true")
        for item in timeline_ops.layer_items(dst):
            if item.id not in moving_ids and pos[item.id] >= insert_at - tol:
                pos[item.id] += length

        # 4. the moved clips (and the transitions between them)
        for item in list(clips) + inner_trans:
            objs[item.id] = item
            pos[item.id] = insert_at + (span(item.data)[0] - g_start)
            moved_items[item.id] = (item, pos[item.id], dst)
        for item_id, obj_ in objs.items():
            if item_id in moved_items:
                continue
            if abs(pos[item_id] - float(obj_.data.get("position") or 0.0)) > 1e-9:
                moved_items[item_id] = (obj_, pos[item_id], int(obj_.data.get("layer") or 0))

    moved, shifted = [], []
    for item_id, (item, new_pos, new_layer) in moved_items.items():
        fields = {"position": snap(new_pos)}
        if int(item.data.get("layer") or 0) != int(new_layer):
            fields["layer"] = int(new_layer)
        save_clip(item, **fields)
        if item_id in ids:
            moved.append({"timeline_clip_id": item_id, "title": title(item),
                          "from": {"track": ui_track_number(before[item_id][0]), "position": r3(before[item_id][1])},
                          "to": {"track": ui_track_number(int(new_layer)), "position": r3(fields["position"])}})
        elif isinstance(item, Clip):
            shifted.append(item_id)
    extend_timeline()
    refresh()
    first = moved[0]["to"] if moved else {}
    summary = (f"Moved {len(moved)} clip(s) to {track_name(layer_map[next(iter(src_layers))])} at "
               f"{first.get('position', 0):.2f} s" + (f"; {len(shifted)} later clip(s) shifted" if shifted else "")
               + (f"; overlaps {len(hits)} clip(s)" if hits else "") + ".")
    return ok(summary, moved=moved, shifted=sorted(shifted), overlaps=hits, ripple=bool(ripple))


# ---------------------------------------------------------------------------
# Trim
# ---------------------------------------------------------------------------

@editor_tool(
    "trim_clips_tool",
    label="Trim clips",
    schema=obj({
        **CLIPS_TARGET,
        "trim_start_seconds": number("Cut this much off the start (negative = extend back into the source).", 0.0),
        "trim_end_seconds": number("Cut this much off the end (negative = extend further into the source).", 0.0),
        "source_start": number("Set the in point: seconds into the source media; -1 = unchanged.", -1.0),
        "source_end": number("Set the out point: seconds into the source media; -1 = unchanged.", -1.0),
        "duration_seconds": number("Set the clip's length on the timeline by moving its out point; 0 = unchanged.",
                                   0.0, minimum=0),
        "ripple": boolean(_RIPPLE_NOTE + " With ripple the clip keeps its start on the timeline.", False),
        "allow_overlap": _OVERLAP_ARG,
    }),
    covers=("clip.trim",),
)
def trim_clips(timeline_clip_ids=[], clip_query="", track="", scope="", trim_start_seconds=0.0,
               trim_end_seconds=0.0, source_start=-1.0, source_end=-1.0, duration_seconds=0.0, ripple=False,
               allow_overlap=False):
    """Change where clips start and end in their source media (drag a clip edge).

    Use for "trim the first two seconds", "cut the last second off every clip on track 1",
    "make this clip 5 seconds long", "start the clip at 0:12 of the video". Times are seconds
    of the source (source_start/source_end) or amounts to cut (trim_start_seconds /
    trim_end_seconds; negative amounts extend the clip into unused source).

    Trimming the start keeps the picture in place: the clip's left edge moves later and a gap
    opens. With ripple=true the clip keeps its start and every later clip on the track moves to
    close (or open) the room. Extending past the media, before 0 s, or over another clip is
    refused with the exact limit (allow_overlap=true overlaps anyway). Still images can be any
    length. To cut a clip in two use slice_clips_tool; to change speed use set_clip_speed_tool.
    One undo step for the whole batch.
    """
    from classes.clip_utils import clip_time_bounds

    if not _clips_or_query_given(timeline_clip_ids, clip_query, scope):
        raise ToolError("say which clips to trim: timeline_clip_ids, clip_query, or scope")
    if trim_start_seconds and source_start >= 0:
        raise ToolError("give trim_start_seconds or source_start, not both")
    if sum([bool(trim_end_seconds), source_end >= 0, duration_seconds > 0]) > 1:
        raise ToolError("give only one of trim_end_seconds, source_end, duration_seconds")
    if not (trim_start_seconds or trim_end_seconds or source_start >= 0 or source_end >= 0 or duration_seconds > 0):
        raise ToolError("nothing to trim: give trim_start_seconds, trim_end_seconds, source_start, source_end "
                        "or duration_seconds")
    clips = targets(timeline_clip_ids, clip_query, track, scope)
    require_unlocked({int(c.data.get("layer") or 0) for c in clips})

    tol = tolerance()
    frame = frame_seconds()
    plans = []
    for c in clips:
        name = title(c)
        s, e = float(c.data.get("start") or 0.0), float(c.data.get("end") or 0.0)
        pos = float(c.data.get("position") or 0.0)
        ns = source_start if source_start >= 0 else s + trim_start_seconds
        if source_end >= 0:
            ne = source_end
        elif duration_seconds > 0:
            ne = ns + duration_seconds
        else:
            ne = e - trim_end_seconds
        ns, ne = snap(ns), snap(ne)
        if ns < -tol:
            raise ToolError(f"{name}: there are only {s:.2f} s of source before its in point, so the start can be "
                            f"extended by at most {s:.2f} s")
        ns = max(0.0, ns)
        if not is_image(c):
            max_len, _frames = clip_time_bounds(c.data)
            if max_len and ne > max_len + tol:
                raise ToolError(f"{name}: the source is {max_len:.2f} s long, so its out point can be at most "
                                f"{max_len:.2f} s (it is {e:.2f} s now)")
            if max_len:
                ne = min(ne, max_len)
        if ne - ns < frame - tol:
            raise ToolError(f"{name}: that leaves {max(0.0, ne - ns):.3f} s; a clip needs at least one frame "
                            f"(delete it with delete_from_timeline_tool instead)")
        new_pos = pos if ripple else pos + (ns - s)
        if new_pos < -tol:
            raise ToolError(f"{name}: extending its start by {s - ns:.2f} s would put it before 0 s on the "
                            "timeline; move it later first or pass ripple=true")
        plans.append((c, s, e, pos, ns, ne, max(0.0, new_pos)))

    plans = [p for p in plans if abs(p[4] - p[1]) > 1e-9 or abs(p[5] - p[2]) > 1e-9]
    if not plans:
        return ok("Nothing changed: the clips already have those in/out points.", changed=False, clips=[])

    hits = []
    if not ripple:
        changes = {c.id: (int(c.data.get("layer") or 0), np_, np_ + (ne - ns)) for c, _s, _e, _p, ns, ne, np_ in plans}
        hits = check_overlaps(new_overlaps(changes), allow_overlap, "The trim")

    results, shifted = [], []
    for c, s, e, pos, ns, ne, np_ in sorted(plans, key=lambda p: -p[3]):
        layer = int(c.data.get("layer") or 0)
        save_clip(c, start=ns, end=ne, duration=ne - ns, position=snap(np_))
        if ripple:
            # Everything that starts after this clip moves, including a clip that
            # overlaps its tail (a crossfade partner) and the transition between them.
            delta = (ne - ns) - (e - s)
            shifted += timeline_ops.shift_after(layer, pos + tolerance(), delta, exclude_ids={c.id})
        results.append({"timeline_clip_id": c.id, "title": title(c),
                        "before": {"position": r3(pos), "source_in": r3(s), "source_out": r3(e), "duration": r3(e - s)},
                        "after": {"position": r3(np_), "source_in": r3(ns), "source_out": r3(ne),
                                  "duration": r3(ne - ns)}})
    results.reverse()
    extend_timeline()
    refresh()
    one = results[0]["after"]
    summary = (f"Trimmed {len(results)} clip(s)" + (f": now {one['source_in']:.2f}-{one['source_out']:.2f} s of the source, "
               f"{one['duration']:.2f} s long" if len(results) == 1 else "")
               + (f"; {len(shifted)} later item(s) rippled" if shifted else "") + ".")
    return ok(summary, clips=results, shifted=shifted_receipt(shifted), overlaps=hits, ripple=bool(ripple))


# ---------------------------------------------------------------------------
# Slice
# ---------------------------------------------------------------------------

@editor_tool(
    "slice_clips_tool",
    label="Slice clips",
    schema=obj({
        "at_seconds": number("Timeline time to cut at; -1 = the playhead.", -1.0),
        "timeline_clip_ids": array({"type": "string"}, "Clips to cut (each must be under at_seconds).", []),
        "clip_query": string("Describe one clip to cut instead of ids.", ""),
        "track": string("Track for clip_query or scope='track': UI track number (1 = bottom), name, or layer number.",
                        ""),
        "scope": enum(["", "selected", "track", "all"], "Instead of ids/query: 'selected' = the selected clips, "
                      "'track' = everything on track under the time, 'all' = everything under the time on every "
                      "unlocked track (a razor cut through the whole timeline).", ""),
        "keep": enum(["both", "left", "right"], "both = cut into two clips; left = keep the part before the cut "
                     "(drop the rest); right = keep the part after it.", "both"),
        "ripple": boolean("With keep='left' or 'right': close the gap the dropped part leaves.", False),
        "include_transitions": boolean("With scope 'track'/'all'/'selected': cut transitions under the time too.",
                                       True),
    }),
    covers=("clip.slice",),
)
def slice_clips(at_seconds=-1.0, timeline_clip_ids=[], clip_query="", track="", scope="", keep="both",
                ripple=False, include_transitions=True):
    """Cut clips at a timeline time (the Slice menu / razor tool).

    Use for "split the clip at 12 seconds", "cut everything at the playhead", "cut off
    everything after 30 s on track 1" (keep='left'), "drop the first part of this clip and close
    the gap" (keep='right', ripple=true). Target one clip (timeline_clip_ids / clip_query), the
    selection, one track, or everything under the time (scope='all'). Explicit clips must lie
    under at_seconds; locked tracks are refused for explicit targets and skipped (and reported)
    for scope='all'. keep='both' returns the new right-hand clip ids. One undo step.
    To shorten a clip without cutting it in two, trim_clips_tool is simpler.
    """
    from classes.query import Clip, Transition
    from classes.editor_tools._base import is_locked
    from windows.views.timeline_backend.enums import MenuSlice

    if keep == "both" and ripple:
        raise ToolError("ripple only applies when one side is kept (keep='left' or 'right')")
    t = snap(time_arg(at_seconds))
    tol = tolerance()

    def inside(data):
        s, e = span(data)
        return s + tol < t < e - tol

    scope = (scope or "").strip().lower()
    skipped = []
    trans = []
    if timeline_clip_ids or (clip_query or "").strip() or scope == "selected":
        clips = targets(timeline_clip_ids, clip_query, track, scope)
        for c in clips:
            if not inside(c.data):
                s, e = span(c.data)
                raise ToolError(f"{title(c)!r} ({s:.2f}-{e:.2f} s) is not under {t:.2f} s; cut inside the clip")
        require_unlocked({int(c.data.get("layer") or 0) for c in clips})
        if scope == "selected" and include_transitions:
            sel = set(getattr(get_app().window, "selected_transitions", []) or [])
            trans = [tr for tr in Transition.filter() if tr.id in sel and inside(tr.data)]
    elif scope in ("track", "all"):
        wanted = {resolve_layer(track)} if scope == "track" else {int(t_.get("number") or 0) for t_ in layers()}
        if scope == "track":
            require_unlocked(wanted)
        clips = [c for c in Clip.filter() if int(c.data.get("layer") or 0) in wanted and inside(c.data)]
        if include_transitions:
            trans = [tr for tr in Transition.filter() if int(tr.data.get("layer") or 0) in wanted and inside(tr.data)]
        for item in [c for c in clips if is_locked(int(c.data.get("layer") or 0))]:
            skipped.append({"timeline_clip_id": item.id, "title": title(item),
                            "track": ui_track_number(int(item.data.get("layer") or 0))})
        clips = [c for c in clips if not is_locked(int(c.data.get("layer") or 0))]
        trans = [tr for tr in trans if not is_locked(int(tr.data.get("layer") or 0))]
        if not clips and not trans:
            where = f" on {track_name(next(iter(wanted)))}" if scope == "track" else ""
            extra = f" ({len(skipped)} clip(s) on locked tracks skipped)" if skipped else ""
            raise ToolError(f"nothing to slice under {t:.2f} s{where}{extra}")
    else:
        raise ToolError("say what to slice: timeline_clip_ids, clip_query, or scope ('selected', 'track', 'all')")

    action = {"both": MenuSlice.KEEP_BOTH, "left": MenuSlice.KEEP_LEFT, "right": MenuSlice.KEEP_RIGHT}[keep]
    before_ids = all_clip_ids()
    before_data = {c.id: (float(c.data.get("start") or 0.0), float(c.data.get("end") or 0.0),
                          float(c.data.get("position") or 0.0)) for c in clips}
    timeline_ui().Slice_Triggered(action, [c.id for c in clips], [tr.id for tr in trans], t, bool(ripple))

    new_ids = all_clip_ids() - before_ids
    pieces = []
    for c in clips:
        now = fresh(c.id)
        right = None
        if keep == "both":
            for nid in sorted(new_ids):
                nd = fresh(nid)
                if nd and nd.data.get("file_id") == c.data.get("file_id") and \
                        int(nd.data.get("layer") or 0) == int(c.data.get("layer") or 0) and \
                        abs(float(nd.data.get("position") or 0.0) - t) <= tol:
                    right = nid
                    new_ids.discard(nid)
                    break
        after = (float(now.data.get("start") or 0.0), float(now.data.get("end") or 0.0),
                 float(now.data.get("position") or 0.0)) if now else None
        pieces.append({"timeline_clip_id": c.id, "title": title(c),
                       "left": c.id if keep in ("both", "left") else None,
                       "right": right if keep == "both" else (c.id if keep == "right" else None),
                       "changed": after != before_data[c.id] or right is not None})
    if clips and not any(p["changed"] for p in pieces):
        raise ToolError(f"the editor did not slice anything at {t:.2f} s")
    refresh()
    return ok(f"Sliced {len(clips)} clip(s)" + (f" and {len(trans)} transition(s)" if trans else "")
              + f" at {t:.2f} s, kept {keep}" + (" and closed the gap" if ripple else "")
              + (f"; skipped {len(skipped)} on locked tracks" if skipped else "") + ".",
              at_seconds=r3(t), keep=keep, pieces=pieces, transition_ids=[tr.id for tr in trans],
              skipped_locked=skipped, ripple=bool(ripple))


# ---------------------------------------------------------------------------
# Gaps (and closing the hole after a delete)
# ---------------------------------------------------------------------------

@editor_tool(
    "remove_gaps_tool",
    label="Remove gaps",
    schema=obj({
        "track": string("Track to close gaps on: UI track number (1 = bottom), name, or layer number. Empty = the one "
                        "track that has gaps (refused when several do).", ""),
        "from_seconds": number("Only close gaps that end after this timeline time (0 = the whole track, including "
                               "empty space before the first clip).", 0.0, minimum=0),
        "only_first": boolean("Close just the first gap at/after from_seconds (Remove Gap) instead of all of them "
                              "(Remove All Gaps).", False),
    }),
    covers=("clip.gaps", "clip.ripple_delete"),
)
def remove_gaps(track="", from_seconds=0.0, only_first=False):
    """Close empty space on a track by sliding the later clips and transitions left. Never deletes
    timeline clips.

    Use for "remove the gaps", "close the gap between the clips", "pack track 1 together", and
    to finish a ripple delete: delete_from_timeline_tool removes a clip and leaves a hole, then
    remove_gaps_tool(track=<its track>, from_seconds=<where it started>, only_first=true) closes
    exactly that hole. Clips that overlap (crossfades) move together, like Track menu > Remove
    All Gaps. Other tracks are not touched, so music or voice-over on another track stays where
    it is. Returns the gaps closed. One undo step; nothing to close is not an error.
    """
    from classes.query import Clip

    if (track or "").strip():
        todo = [resolve_layer(track)]
    else:
        used = sorted({int(c.data.get("layer") or 0) for c in Clip.filter()})
        todo = [n for n in used if timeline_ops.list_gaps(n, from_seconds)]
        if len(todo) > 1:
            raise ToolError(f"{', '.join(track_name(n) for n in todo)} have gaps; say which track (closing gaps on "
                            "several tracks at once would pull them out of sync)")
    require_unlocked(todo)

    closed = []
    moved_ids = set()
    for layer in todo:
        gaps = timeline_ops.list_gaps(layer, from_seconds)
        if not gaps:
            continue
        if only_first:
            gaps = gaps[:1]
            moved = timeline_ops.close_gap(gaps[0][0], gaps[0][1], layer)
        else:
            moved = timeline_ops.close_all_gaps(gaps[0][0], layer)
        moved_ids.update(i.id for i in moved)
        closed.append({"track": ui_track_number(layer), "gaps": [[r3(s), r3(e)] for s, e in gaps],
                       "closed_seconds": r3(sum(e - s for s, e in gaps))})
    if not closed:
        where = f" on {track_name(todo[0])}" if todo else ""
        return ok(f"No gaps to remove{where} after {from_seconds:.2f} s.", changed=False, closed=[])
    refresh()
    total = sum(c["closed_seconds"] for c in closed)
    n_gaps = sum(len(c["gaps"]) for c in closed)
    return ok(f"Closed {n_gaps} gap(s) ({total:.2f} s) on track {closed[0]['track']}; {len(moved_ids)} item(s) moved "
              "left.", closed=closed, moved=sorted(moved_ids))


# ---------------------------------------------------------------------------
# Duplicate (copy / paste)
# ---------------------------------------------------------------------------

@editor_tool(
    "duplicate_clips_tool",
    label="Duplicate clips",
    schema=obj({
        **CLIPS_TARGET,
        "position_seconds": number("Timeline time for the first copy; -1 = right after the copied clips.", -1.0),
        "to_track": string("Track for the copies: UI track number (1 = bottom), name, or layer number. Empty = the "
                           "same track(s).", ""),
        "copies": integer("How many copies, placed back to back.", 1, minimum=1, maximum=50),
        "include_transitions": boolean("Also copy transitions lying within the copied clips' time span.", True),
        "ripple": boolean("Insert the copies: push the clips at the destination later to make room.", False),
        "allow_overlap": _OVERLAP_ARG,
    }),
    background_safe=True,
    covers=("clip.copy_paste",),
)
def duplicate_clips(timeline_clip_ids=[], clip_query="", track="", scope="", position_seconds=-1.0, to_track="",
                    copies=1, include_transitions=True, ripple=False, allow_overlap=False):
    """Copy clips (with their effects, keyframes and the transitions between them) and paste the
    copies at a time and track: Copy + Paste / Duplicate in the editor.

    Use for "duplicate this clip", "copy the intro to the end", "repeat this section three times"
    (copies=3), "put a copy of the logo on track 3 at 20 s". Copies keep the originals' spacing
    and relative tracks, get new ids (and new effect ids), and land right after the originals by
    default. The tool refuses to cover existing clips unless ripple=true (insert, pushing later
    clips) or allow_overlap=true. The system clipboard is not touched. To move instead of copy
    use move_clips_tool; to loop one clip's playback use repeat_clip_tool. One undo step.
    """
    from classes.query import Clip, Transition

    if not _clips_or_query_given(timeline_clip_ids, clip_query, scope):
        raise ToolError("say which clips to duplicate: timeline_clip_ids, clip_query, or scope")
    clips = targets(timeline_clip_ids, clip_query, track, scope)
    src_layers = {int(c.data.get("layer") or 0) for c in clips}
    layer_map = remap_layers(src_layers, to_track) if (to_track or "").strip() else {n: n for n in src_layers}
    dest_layers = set(layer_map.values())
    require_unlocked(dest_layers, "the copies' destination")

    tol = tolerance()
    spans = {c.id: span(c.data) for c in clips}
    g_start = min(s for s, _e in spans.values())
    g_end = max(e for _s, e in spans.values())
    length = g_end - g_start
    base = snap(position_seconds) if position_seconds >= 0 else g_end
    trans = []
    if include_transitions:
        trans = [tr for tr in Transition.filter() if int(tr.data.get("layer") or 0) in src_layers
                 and g_start - tol <= span(tr.data)[0] and span(tr.data)[1] <= g_end + tol]

    planned = []
    for k in range(int(copies)):
        offset = base + k * length - g_start
        for c in clips:
            s, e = spans[c.id]
            planned.append((f"copy {k + 1} of {title(c)}", layer_map[int(c.data.get("layer") or 0)],
                            s + offset, e + offset))

    shifted = []
    hits = []
    if ripple:
        for layer in dest_layers:
            for item in timeline_ops.layer_items(layer, transitions=False):
                s, e = span(item.data)
                if s < base - tol and e > base + tol and not allow_overlap:
                    raise ToolError(f"{base:.2f} s falls inside {title(item)!r} ({s:.2f}-{e:.2f} s); insert at its "
                                    "start or end, slice it first, or pass allow_overlap=true")

        def _open_room():
            require_unlocked(dest_layers, "the copies' destination")
            moved = []
            for layer in dest_layers:
                moved += timeline_ops.shift_after(layer, base, int(copies) * length)
            return moved

        shifted = on_main(_open_room)
    else:
        hits = check_overlaps(new_overlaps({}, planned), allow_overlap, "The copies")

    def _insert_copy(k):
        # One copy per main-thread hop: each new clip makes the preview open its media.
        require_unlocked(dest_layers, "the copies' destination")
        project = get_app().project
        offset = base + k * length - g_start
        batch, batch_trans = [], []
        for c in clips:
            data = copy.deepcopy(c.data)
            data.pop("id", None)
            for effect in data.get("effects") or []:
                if isinstance(effect, dict):
                    effect["id"] = project.generate_id()
            data["position"] = snap(spans[c.id][0] + offset)
            data["layer"] = layer_map[int(c.data.get("layer") or 0)]
            new = Clip()
            new.data = data
            new.save()
            batch.append({"timeline_clip_id": new.id, "copy_of": c.id, "track": ui_track_number(data["layer"]),
                          "position": r3(data["position"]), "end": r3(span(data)[1])})
        for tr in trans:
            data = copy.deepcopy(tr.data)
            data.pop("id", None)
            data["position"] = snap(float(tr.data.get("position") or 0.0) + offset)
            data["layer"] = layer_map[int(tr.data.get("layer") or 0)]
            new = Transition()
            new.data = data
            new.save()
            batch_trans.append(new.id)
        return batch, batch_trans

    created, new_trans = [], []
    for k in range(int(copies)):
        batch, batch_trans = on_main(_insert_copy, k)
        created.append(batch)
        new_trans += batch_trans
    on_main(_finish_edit)
    first = created[0][0]
    return ok(f"Made {int(copies)} cop{'y' if copies == 1 else 'ies'} of {len(clips)} clip(s); the first starts at "
              f"{first['position']:.2f} s on track {first['track']}"
              + (f"; {len([i for i in shifted if isinstance(i, Clip)])} later clip(s) pushed" if shifted else "")
              + (f"; overlaps {len(hits)} clip(s)" if hits else "") + ".",
              copies=created, transition_ids=new_trans, shifted=shifted_receipt(shifted), overlaps=hits)


# ---------------------------------------------------------------------------
# Align
# ---------------------------------------------------------------------------

@editor_tool(
    "align_clips_tool",
    label="Align clips",
    schema=obj({
        **CLIPS_TARGET,
        "edge": enum(["start", "end"], "Line up the clips' starts or their ends.", "start"),
        "to_seconds": number("Timeline time to line the edges up at; -1 = the earliest start / latest end among "
                             "them (Clip menu > Align Left / Right).", -1.0),
        "allow_overlap": _OVERLAP_ARG,
    }),
    covers=("clip.align",),
)
def align_clips(timeline_clip_ids=[], clip_query="", track="", scope="", edge="start", to_seconds=-1.0,
                allow_overlap=False):
    """Line up the starts (or ends) of clips on different tracks: Clip menu > Align Left / Right.

    Use for "start the title and the music together with the video", "make all these clips end
    at the same time", "line these up at 10 seconds". Each clip keeps its track and length; only
    its timeline position changes. By default the starts go to the earliest start (the ends to
    the latest end); to_seconds picks the time. Clips on the same track would pile up, so that
    is refused unless allow_overlap=true. One undo step.
    """
    if not _clips_or_query_given(timeline_clip_ids, clip_query, scope):
        raise ToolError("say which clips to align: timeline_clip_ids, clip_query, or scope")
    clips = targets(timeline_clip_ids, clip_query, track, scope)
    if len(clips) < 2 and to_seconds < 0:
        raise ToolError("aligning needs at least two clips (or to_seconds to line one clip up with a time)")
    require_unlocked({int(c.data.get("layer") or 0) for c in clips})
    positions = timeline_ops.aligned_positions([c.data for c in clips], edge == "end",
                                               snap(to_seconds) if to_seconds >= 0 else None)
    tol = tolerance()
    changes = {}
    for c in clips:
        s, e = span(c.data)
        p = snap(positions[c.id])
        if p < -tol:
            raise ToolError(f"{title(c)!r} would start at {p:.2f} s, before the timeline start")
        if abs(p - s) > tol:
            changes[c.id] = (int(c.data.get("layer") or 0), max(0.0, p), max(0.0, p) + (e - s))
    if not changes:
        return ok(f"The clips' {edge}s are already lined up.", changed=False, moved=[])
    hits = check_overlaps(new_overlaps(changes), allow_overlap, "Aligning", ripple_hint=False)
    moved = []
    for c in clips:
        if c.id in changes:
            old = span(c.data)[0]
            save_clip(c, position=changes[c.id][1])
            moved.append({"timeline_clip_id": c.id, "title": title(c), "from": r3(old), "to": r3(changes[c.id][1])})
    extend_timeline()
    refresh()
    at = changes[next(iter(changes))][1] if edge == "start" else changes[next(iter(changes))][2]
    return ok(f"Aligned the {edge}s of {len(clips)} clip(s) at {at:.2f} s ({len(moved)} moved).",
              edge=edge, at_seconds=r3(at), moved=moved, overlaps=hits)


# Speed, repeat, freeze and audio/video tools register from their own module.
from classes.editor_tools import timeline_edit_time  # noqa: E402,F401
