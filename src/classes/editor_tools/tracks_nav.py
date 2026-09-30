"""Tracks, markers, playback, timeline view and selection.

Workstream: tracks-nav. The track and marker rules live in
``classes.track_ops`` (shared with the main window's menu handlers); playback,
seeking, zoom, modes and selection drive the editor's own actions and
signals, exactly like the buttons and shortcuts do.

Threading: track and marker edits run on the GUI thread (one undo step each).
``play_tool`` and ``seek_playhead_tool`` are background-safe: they emit the
player signals on the GUI thread, then wait on the worker thread until the
preview player reports the requested state, so the receipt says what really
happened. View and selection changes are UI state and add no undo steps.
"""

from __future__ import annotations

import time
from typing import Iterable, List, Optional, Tuple

from classes import track_ops
from classes.editor_tools._base import (
    ToolError,
    array,
    boolean,
    describe_clip,
    enum,
    fps_fraction,
    frame_to_seconds,
    get_app,
    integer,
    is_locked,
    layers,
    nullable,
    number,
    obj,
    ok,
    on_main,
    playhead_seconds,
    project,
    project_fps,
    refresh_preview,
    resolve_clip,
    resolve_layer,
    seconds_to_frame,
    snap_seconds,
    string,
    th,
)
from classes.editor_tools._registry import editor_tool
from classes.logger import log

# How long the worker thread waits for the preview player to confirm a change.
PLAYER_SETTLE_SECONDS = 3.0

TRANSPORT_ACTIONS = ("toggle", "play", "pause", "stop", "fast_forward", "rewind",
                     "step_forward", "step_back")
SEEK_TARGETS = ("", "start", "end", "next_marker", "previous_marker", "next_edit", "previous_edit")
SELECT_MODES = ("replace", "add", "remove", "all", "none", "ripple")
TRACK_REF = "UI track number (1 = bottom), track name, track id (L3) or layer number"


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------

def _num(value, default=None):
    """A real number from a Qt/libopenshot getter; anything else -> default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


def _timecode(seconds: float) -> str:
    """The ruler's timecode for a timeline time: HH:MM:SS,FF."""
    from classes.time_parts import secondsToTime
    num, den = fps_fraction()
    t = secondsToTime(max(0.0, float(seconds)), num, den)
    return "%s:%s:%s,%s" % (t["hour"], t["min"], t["sec"], t["frame"])


def _when(seconds: float) -> dict:
    seconds = max(0.0, float(seconds))
    return {"seconds": round(seconds, 3), "frame": seconds_to_frame(seconds), "timecode": _timecode(seconds)}


def _timeline_duration() -> float:
    try:
        return float(project().get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _item_end(data: dict) -> float:
    try:
        return float(data.get("position") or 0.0) + max(
            0.0, float(data.get("end") or 0.0) - float(data.get("start") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _content_end() -> float:
    """End of the last clip or transition, in timeline seconds (0 = empty timeline)."""
    items = list(project().get("clips") or []) + list(project().get("effects") or [])
    return max([_item_end(d) for d in items if isinstance(d, dict)] or [0.0])


def _last_frame() -> int:
    """The last playable frame (what End and Next Marker stop at)."""
    try:
        value = _num(get_app().window.timeline_sync.GetLastFrame())
        if value is not None:
            return max(1, int(value))
    except Exception:
        log.debug("GetLastFrame unavailable", exc_info=True)
    # timeline_sync.GetLastFrame(): one frame before the end of the last clip.
    return max(1, int(round(_content_end() * project_fps())) - 1)


def _playback_play_mode() -> int:
    try:
        import openshot
        return int(getattr(openshot, "PLAYBACK_PLAY", 0))
    except Exception:
        return 0


def _player_state() -> dict:
    """Playhead and transport state as the preview player reports it."""
    player = get_app().window.preview_thread.player
    frame = int(_num(player.Position(), 1) or 1)
    speed = _num(player.Speed(), 0)
    playing = player.Mode() == _playback_play_mode() and speed != 0
    state = {"playing": bool(playing), "speed": speed if playing else 0}
    state.update(_when(frame_to_seconds(frame)))
    state["frame"] = frame
    return state


def _qt_event_loop() -> bool:
    return th().QThread is not None


def _on_gui_thread() -> bool:
    QThread = th().QThread
    if QThread is None:
        return True
    try:
        return QThread.currentThread() is get_app().thread()
    except Exception:
        return True


def _state_matches(state: dict, expect: dict) -> bool:
    """*expect*: playing / speed must be equal; frame within expect['tolerance'] frames."""
    for key, want in expect.items():
        if key == "tolerance":
            continue
        if key == "frame":
            if abs(int(state["frame"]) - int(want)) > int(expect.get("tolerance", 0)):
                return False
        elif state.get(key) != want:
            return False
    return True


def _await_state(expect: dict, timeout: float = PLAYER_SETTLE_SECONDS) -> Tuple[str, dict]:
    """Wait (off the GUI thread) until the player state matches *expect*.

    Returns ("confirmed" | "timeout" | "unconfirmed", state). "unconfirmed":
    called on the GUI thread, which must not block while the player catches up.
    """
    state = _player_state()
    if _state_matches(state, expect):
        return "confirmed", state
    if not _qt_event_loop():
        return "timeout", state       # headless: nothing changes later
    if _on_gui_thread():
        return "unconfirmed", state
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.02)
        state = _player_state()
        if _state_matches(state, expect):
            return "confirmed", state
    return "timeout", state


def _unconfirmed(receipt: dict, summary: str, status: str) -> str:
    if status == "unconfirmed":
        receipt["confirmed"] = False
        summary += " (requested; the player had not confirmed it yet)"
    return summary


def _visible_range() -> Optional[Tuple[float, float]]:
    """Timeline seconds currently visible (from the zoom slider), or None."""
    slider = getattr(get_app().window, "sliderZoomWidget", None)
    sp = getattr(slider, "scrollbar_position", None)
    if not isinstance(sp, (list, tuple)) or len(sp) < 2:
        return None
    left, right = _num(sp[0]), _num(sp[1])
    duration = _timeline_duration()
    if left is None or right is None or right <= left or duration <= 0:
        return None
    return (round(left * duration, 3), round(right * duration, 3))


def _view_width_px() -> Optional[float]:
    slider = getattr(get_app().window, "sliderZoomWidget", None)
    sp = getattr(slider, "scrollbar_position", None)
    if isinstance(sp, (list, tuple)) and len(sp) >= 4:
        width = _num(sp[3])
        if width and width > 0:
            return float(width)
    return None


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

def _tracks() -> List[dict]:
    """The track stack for receipts, bottom first."""
    from classes.track_display import build_track_stack
    locks, counts = {}, {}
    for layer in layers():
        try:
            locks[int(layer.get("number") or 0)] = bool(layer.get("lock"))
        except (TypeError, ValueError):
            continue
    for clip in project().get("clips") or []:
        try:
            n = int(clip.get("layer") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        counts[n] = counts.get(n, 0) + 1
    return [{"track": e["ui_track"], "name": e["label"], "layer": e["layer_number"],
             "track_id": e["track_id"], "locked": locks.get(e["layer_number"], False),
             "clips": counts.get(e["layer_number"], 0)}
            for e in build_track_stack(layers())]


def _track_row(layer: int) -> dict:
    for row in _tracks():
        if row["layer"] == int(layer):
            return row
    return {"track": None, "name": "", "layer": int(layer), "track_id": "", "locked": False, "clips": 0}


def _track_title(row: dict, capital: bool = False) -> str:
    """How the model should read a track: 'track 2 "B-roll"' or 'track 2'."""
    word = "Track" if capital else "track"
    if row.get("name"):
        return '%s %s "%s"' % (word, row.get("track"), row["name"])
    return "%s %s" % (word, row.get("track"))


def _name_clash(name: str, exclude_layer: Optional[int] = None) -> str:
    if not name:
        return ""
    for row in _tracks():
        if row["layer"] != exclude_layer and row["name"].strip().lower() == name.lower():
            return ("another track (%s) already has this name; refer to tracks by number when "
                    "names repeat" % _track_title(row))
    return ""


@editor_tool(
    "add_track_tool",
    label="Add track",
    schema=obj({
        "position": enum(list(track_ops.TRACK_POSITIONS),
                         "Where the new track goes: 'top' (above every track, like the Add Track "
                         "button), 'bottom' (below every track), or 'above' / 'below' the track in "
                         "relative_to (Track menu > Add Track Above / Below).", "top"),
        "relative_to": string("For position 'above' / 'below': the existing track, as a " + TRACK_REF + ".", ""),
        "name": string("Name shown in the new track's header, e.g. 'Music'. Empty = the default "
                       "'Track N'.", ""),
        "count": integer("How many tracks to add in this one step. With a name they are called "
                         "'<name> 1' .. '<name> N' from bottom to top.", 1, minimum=1, maximum=10),
    }),
    covers=("track.add",),
)
def add_track(position="top", relative_to="", name="", count=1):
    """Add an empty track (a timeline lane) for music, voiceover, B-roll, titles or overlays.

    Works with nothing selected. Tracks higher in the stack cover the ones below
    them in the video. position: 'top' (default: above every track, like the
    Add Track button), 'bottom' (below every track), or 'above' / 'below' the
    track given in relative_to. Track numbers count from the bottom (track 1 =
    bottom), so adding a track below track 2 turns the old track 2 into track 3:
    the result's `tracks` list is the new numbering. count adds several tracks
    in one undo step.

    Examples: "add a music track" -> name='Music'; "add a track above the
    voiceover" -> position='above', relative_to='Voiceover'; "add two tracks at
    the bottom" -> position='bottom', count=2. Rename or lock a track with
    update_track_tool; remove one with remove_track_tool.
    """
    name = (name or "").strip()
    relative_to = (relative_to or "").strip()
    relative_layer = None
    if position in ("above", "below"):
        if not relative_to:
            raise ToolError("position='%s' needs relative_to: the track to add it %s (%s)"
                            % (position, position, TRACK_REF))
        relative_layer = resolve_layer(relative_to)
    elif relative_to:
        raise ToolError("relative_to only applies to position 'above' or 'below'; "
                        "got position='%s'" % position)
    warning = _name_clash(name)

    created_ids = []
    upward = position in ("top", "above")
    for i in range(count):
        if count > 1 and name:
            # Name the block bottom-to-top whichever way it grows.
            label = "%s %d" % (name, (i + 1) if upward else (count - i))
        else:
            label = name
        if not created_ids:
            layer = track_ops.insert_track(position, relative_layer, label)
        else:
            # Keep the block together: each next track goes next to the previous new one
            # (looked up by id: a renumbering may have moved it).
            previous = track_ops.get_track(track_id=created_ids[-1])
            layer = track_ops.insert_track("above" if upward else "below",
                                           int(previous.data.get("number") or 0), label)
        created_ids.append(track_ops.get_track(layer=layer).id)

    rows = _tracks()
    added = [r for r in rows if r["track_id"] in created_ids]
    where = {"top": "at the top", "bottom": "at the bottom"}.get(
        position, "%s %s" % (position, relative_to))
    if len(added) == 1:
        summary = "Added %s %s." % (_track_title(added[0]), where)
    else:
        summary = "Added %d tracks (%s) %s." % (len(added), ", ".join(_track_title(r) for r in added), where)
    receipt = {"added": added, "tracks": rows}
    if warning:
        receipt["warning"] = warning
    return ok(summary + " Track numbers count from the bottom; see tracks.", **receipt)


@editor_tool(
    "update_track_tool",
    label="Rename or lock track",
    schema=obj({
        "track": string("Track to change: " + TRACK_REF + "."),
        "name": nullable(string("New name for the track header. Empty string = back to the "
                                "default 'Track N'. Omit to keep the current name.")),
        "lock": nullable(boolean("true locks the track, false unlocks it. Omit to leave it as is.")),
    }, required=["track"]),
    covers=("track.rename", "track.lock"),
)
def update_track(track, name=None, lock=None):
    """Rename a track and/or lock or unlock it (Track menu > Rename Track / Lock Track / Unlock Track).

    A locked track's clips cannot be moved, trimmed, sliced, deleted or edited
    until it is unlocked; renaming a locked track is fine. Pass only what should
    change. Examples: "rename track 2 to B-roll" -> track='2', name='B-roll';
    "lock the voiceover track" -> track='Voiceover', lock=true; "unlock
    everything on track 1" -> track='1', lock=false. One undo step; asking for
    the state a track already has changes nothing.
    """
    if name is None and lock is None:
        raise ToolError("nothing to change: pass name and/or lock")
    layer = resolve_layer(track)
    target = track_ops.get_track(layer=layer)
    new_name = None if name is None else name.strip()
    warning = _name_clash(new_name or "", exclude_layer=layer)

    changed = []
    if new_name is not None and track_ops.rename_track(target.id, new_name):
        changed.append("name")
    if lock is not None and track_ops.set_track_lock(target.id, bool(lock)):
        changed.append("lock")

    row = _track_row(layer)
    parts = []
    if "name" in changed:
        parts.append("renamed to '%s'" % new_name if new_name else "name cleared")
    if "lock" in changed:
        parts.append("locked" if row["locked"] else "unlocked")
    if parts:
        summary = "Track %s %s." % (row["track"], " and ".join(parts))
    else:
        already = []
        if new_name is not None:
            already.append("already has that name")
        if lock is not None:
            already.append("is already " + ("locked" if row["locked"] else "unlocked"))
        summary = "Nothing changed: %s %s." % (_track_title(row), " and ".join(already))
    receipt = {"track": row, "changed": changed, "tracks": _tracks()}
    if warning and "name" in changed:
        receipt["warning"] = warning
    return ok(summary, **receipt)


@editor_tool(
    "remove_track_tool",
    label="Remove track",
    schema=obj({
        "track": string("Track lane to remove: " + TRACK_REF + "."),
        "with_clips": boolean("The track still has clips or transitions: true deletes them together "
                              "with the lane (Track menu > Remove Track); false refuses, so tidying up "
                              "empty lanes never deletes clips by accident.", False),
    }, required=["track"]),
    covers=("track.remove",),
)
def remove_track(track, with_clips=False):
    """Remove a track lane from the timeline (Track menu > Remove Track).

    It never deletes clips unless with_clips=true: a track that still holds
    clips or transitions is refused with how many it has. with_clips=true
    deletes the lane together with its clips and transitions, in one undo
    step (undo brings everything back). To delete the clips but keep the lane
    use delete_from_timeline_tool with scope='track'. Locked tracks and the last
    remaining track cannot be removed. The tracks above move down one number;
    the result's `tracks` list is the new numbering. Example: "remove the empty
    track" -> track='3'.
    """
    layer = resolve_layer(track)
    row = _track_row(layer)
    if is_locked(layer):
        raise ToolError("%s is locked; unlock it first (update_track_tool lock=false)" % _track_title(row))
    if len(layers()) <= 1:
        raise ToolError("the project must keep at least one track")
    contents = track_ops.track_contents(layer)
    if (contents["clips"] or contents["transitions"]) and not with_clips:
        raise ToolError("%s still has %d clip(s) and %d transition(s); pass with_clips=true to delete "
                        "them with the track, or move them to another track first"
                        % (_track_title(row), len(contents["clips"]), len(contents["transitions"])))

    win = get_app().window
    target = track_ops.get_track(layer=layer)
    forget = getattr(win, "deselect_removed_item", None)
    if not callable(forget):
        def forget(item_id, item_type):
            win.removeSelection(item_id, item_type)
    removed = track_ops.remove_track(target.id, on_item_removed=forget)

    selected_tracks = getattr(win, "selected_tracks", None)
    if isinstance(selected_tracks, list) and target.id in selected_tracks:
        win.selected_tracks = []
    try:
        # A deleted clip may still be referenced by the preview's transform handles.
        win.videoPreview.clearTransformState()
    except Exception:
        log.debug("clearTransformState skipped", exc_info=True)
    refresh_preview()

    n_clips, n_trans = len(removed["clips"]), len(removed["transitions"])
    extra = " with %d clip(s) and %d transition(s)" % (n_clips, n_trans) if (n_clips or n_trans) else ""
    return ok("Removed %s%s. One undo restores it." % (_track_title(row), extra),
              removed_track=row, deleted_clip_ids=removed["clips"],
              deleted_transition_ids=removed["transitions"], tracks=_tracks())


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def _marker_rows() -> List[dict]:
    rows = []
    for m in track_ops.markers_in_time_order():
        row = {"marker_id": m["id"], "name": m["name"], "color": m["color"]}
        row.update(_when(m["position"]))
        rows.append(row)
    return rows


def _marker_label(row: dict) -> str:
    name = " '%s'" % row["name"] if row.get("name") else ""
    return "marker%s at %s" % (name, row["timecode"])


def _check_marker_time(seconds: float) -> float:
    seconds = snap_seconds(max(0.0, float(seconds)))
    duration = _timeline_duration()
    if duration and seconds > duration + 1e-6:
        raise ToolError("%.3f s is past the end of the timeline (%.3f s long)" % (seconds, duration))
    return seconds


def _markers_matching(ref: str) -> List[dict]:
    """Markers whose id equals *ref*, else whose name equals it (case-insensitive)."""
    ref = (ref or "").strip()
    rows = _marker_rows()
    by_id = [r for r in rows if r["marker_id"] == ref]
    if by_id:
        return by_id
    return [r for r in rows if r["name"].strip().lower() == ref.lower()] if ref else []


def _marker_listing(rows: Optional[List[dict]] = None, limit: int = 12) -> str:
    rows = _marker_rows() if rows is None else rows
    if not rows:
        return "there are no markers"
    text = ", ".join("%s (%s)" % (r["name"] or "unnamed", r["timecode"]) for r in rows[:limit])
    return "markers: " + text + (" ..." if len(rows) > limit else "")


_COLOR_NOTE = ("color is stored with the marker and shown by list_markers_tool (the timeline "
               "draws every marker with the same icon).")


@editor_tool(
    "add_marker_tool",
    label="Add marker",
    schema=obj({
        "position_seconds": nullable(number("Timeline seconds for the marker (0:32 = 32, 1:05 = 65). "
                                            "Omit to mark the playhead position.", minimum=0)),
        "name": string("Marker name, e.g. 'Drop', 'Chorus', 'Cut here'. Used to find it again.", ""),
        "color": enum(list(track_ops.MARKER_COLORS), "Marker color tag; " + _COLOR_NOTE,
                      track_ops.DEFAULT_MARKER_COLOR),
    }),
    covers=("marker.add",),
)
def add_marker(position_seconds=None, name="", color=track_ops.DEFAULT_MARKER_COLOR):
    """Add a timeline marker at a time, with a name (the editor's Add Marker / M key).

    position_seconds is in timeline seconds (0:32 = 32, 1:05 = 65); omit it to
    mark wherever the playhead is. The time snaps to the nearest frame. Name
    markers so they can be found again: seek_playhead_tool(marker='Drop')
    jumps to one, list_markers_tool lists them all. Several markers may share a
    time. Returns the new marker_id. Example: "mark the drop at 0:32" ->
    position_seconds=32, name='Drop'.
    """
    seconds = _check_marker_time(playhead_seconds() if position_seconds is None else position_seconds)
    name = (name or "").strip()
    same_frame = [r for r in _marker_rows() if r["frame"] == seconds_to_frame(seconds)]
    marker = track_ops.add_marker(seconds, name, color)
    row = next((r for r in _marker_rows() if r["marker_id"] == marker.id), {"marker_id": marker.id})
    receipt = {"marker": row, "marker_id": marker.id, "markers": len(_marker_rows())}
    if same_frame:
        receipt["also_at_this_time"] = same_frame
    return ok("Added %s (id %s)." % (_marker_label(row), marker.id), **receipt)


@editor_tool(
    "update_marker_tool",
    label="Edit marker",
    schema=obj({
        "marker": string("Marker to change: its marker_id (from list_markers_tool) or its exact name."),
        "name": nullable(string("New name. Empty string clears it. Omit to keep the name.")),
        "color": enum([""] + list(track_ops.MARKER_COLORS),
                      "New color tag; empty = keep. " + _COLOR_NOTE, ""),
        "position_seconds": nullable(number("Move the marker to this timeline time (seconds). "
                                            "Omit to keep it where it is.", minimum=0)),
    }, required=["marker"]),
    covers=("marker.add",),
)
def update_marker(marker, name=None, color="", position_seconds=None):
    """Rename, recolor or move an existing timeline marker.

    marker is the marker_id from list_markers_tool or the marker's exact name
    (a name several markers share is refused with their ids). Pass only what
    should change. Example: "rename the marker at 0:32 to Chorus" -> first
    list_markers_tool, then marker='<id>', name='Chorus'; "move the Drop marker
    to 0:40" -> marker='Drop', position_seconds=40. One undo step.
    """
    if name is None and not color and position_seconds is None:
        raise ToolError("nothing to change: pass name, color and/or position_seconds")
    matches = _markers_matching(marker)
    if not matches:
        raise ToolError("no marker with id or name %r; %s" % (marker, _marker_listing()))
    if len(matches) > 1:
        raise ToolError("%d markers are named %r (%s); pass one marker_id"
                        % (len(matches), marker, ", ".join("%s at %s" % (r["marker_id"], r["timecode"])
                                                            for r in matches)))
    seconds = None if position_seconds is None else _check_marker_time(position_seconds)
    marker_id = matches[0]["marker_id"]
    changed = track_ops.update_marker(marker_id, name=None if name is None else name.strip(),
                                      color=color or None, position_seconds=seconds)
    row = next(r for r in _marker_rows() if r["marker_id"] == marker_id)
    if not changed:
        return ok("The %s already looks like that; nothing changed." % _marker_label(row),
                  marker=row, changed=[])
    return ok("Updated the %s (%s)." % (_marker_label(row), ", ".join(changed)), marker=row, changed=changed)


@editor_tool(
    "remove_marker_tool",
    label="Remove marker",
    schema=obj({
        "markers": array({"type": "string"}, "Markers to remove, each a marker_id (from "
                         "list_markers_tool) or an exact marker name."),
        "at_seconds": nullable(number("Remove the marker at this timeline time (the nearest marker "
                                      "within half a second).", minimum=0)),
        "all": boolean("Remove every marker on the timeline.", False),
    }),
    covers=("marker.remove",),
)
def remove_marker(markers=None, at_seconds=None, all=False):
    """Remove timeline markers (Marker menu > Remove Marker). It never deletes clips, tracks or anything else.

    Target one way: markers = ids or exact names (a name several markers share
    is refused with their ids), at_seconds = the marker at that time (nearest
    within 0.5 s), or all=true. One undo step brings them back. Example:
    "remove the Drop marker" -> markers=['Drop']; "clear all markers" ->
    all=true.
    """
    markers = [str(m).strip() for m in (markers or []) if str(m).strip()]
    modes = sum([bool(markers), at_seconds is not None, bool(all)])
    if modes == 0:
        raise ToolError("say which markers: markers (ids or names), at_seconds, or all=true")
    if modes > 1:
        raise ToolError("use only one of markers, at_seconds or all")
    rows = _marker_rows()
    if not rows:
        raise ToolError("the timeline has no markers")

    if all:
        targets = rows
    elif at_seconds is not None:
        nearest = min(rows, key=lambda r: abs(r["seconds"] - float(at_seconds)))
        if abs(nearest["seconds"] - float(at_seconds)) > 0.5:
            raise ToolError("no marker within 0.5 s of %.3f s; the nearest is the %s"
                            % (float(at_seconds), _marker_label(nearest)))
        targets = [r for r in rows if r["frame"] == nearest["frame"]]
    else:
        targets = []
        for ref in markers:
            found = _markers_matching(ref)
            if not found:
                raise ToolError("no marker with id or name %r; %s" % (ref, _marker_listing(rows)))
            if len(found) > 1:
                raise ToolError("%d markers are named %r (%s); pass their marker_ids"
                                % (len(found), ref, ", ".join("%s at %s" % (r["marker_id"], r["timecode"])
                                                               for r in found)))
            if found[0] not in targets:
                targets.append(found[0])

    removed = track_ops.remove_markers([r["marker_id"] for r in targets])
    what = targets[0] if len(targets) == 1 else None
    summary = ("Removed the %s." % _marker_label(what)) if what else "Removed %d markers." % len(removed)
    return ok(summary, removed=targets, remaining=len(_marker_rows()))


@editor_tool(
    "list_markers_tool",
    label="List markers",
    schema=obj({}),
    read_only=True,
    covers=("marker.navigate",),
)
def list_markers():
    """List the timeline's markers in time order: marker_id, name, color, seconds, frame and timecode.

    Use it to find a marker to jump to (seek_playhead_tool marker=... or
    to='next_marker'), rename (update_marker_tool) or remove
    (remove_marker_tool). Read-only.
    """
    rows = _marker_rows()
    if not rows:
        return ok("The timeline has no markers.", markers=[])
    return ok("%d marker(s): %s." % (len(rows), _marker_listing(rows)[len("markers: "):]), markers=rows)


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def _at_end_error(state: dict) -> ToolError:
    return ToolError("nothing to play from %s: the playhead is at the end of the timeline (content "
                     "ends at %s). Play from the start with seek_playhead_tool to='start', play=true"
                     % (state["timecode"], _timecode(_content_end())))


def _min_frame() -> int:
    try:
        value = _num(get_app().window.preview_thread.timeline.GetMinFrame())
        if value is not None:
            return max(1, int(value))
    except Exception:
        log.debug("GetMinFrame unavailable", exc_info=True)
    return 1


def _start_transport(action: str, speed, frames: int) -> dict:
    """Validate against the live player and fire the editor's own transport actions (GUI thread)."""
    win = get_app().window
    state = _player_state()
    if action == "toggle":
        action = "pause" if state["playing"] else "play"
    moving = action in ("play", "fast_forward", "rewind")
    if moving and _content_end() <= 0:
        raise ToolError("the timeline is empty: there is nothing to play")

    if action == "play":
        wanted = 1 if speed is None else speed
        if state["playing"] and state["speed"] == wanted:
            return {"action": action, "expect": {"playing": True}, "changed": False}
        if not win.should_play(0 if wanted == 1 else wanted):
            raise _at_end_error(state)
        if not state["playing"]:
            win.actionPlay_trigger()          # the Play button: normal speed
        if wanted != 1 or state["playing"]:
            win.SpeedSignal.emit(float(wanted))
        return {"action": action, "expect": {"playing": True, "speed": wanted}, "changed": True}

    if action == "pause":
        if not state["playing"]:
            return {"action": action, "expect": {"playing": False}, "changed": False}
        win.actionPlay_trigger()
        return {"action": action, "expect": {"playing": False}, "changed": True}

    if action == "stop":
        target = _min_frame()
        win.actionJumpStart_trigger()
        win.PauseSignal.emit()
        return {"action": action, "expect": {"playing": False, "frame": target, "tolerance": 1},
                "changed": state["playing"] or state["frame"] != target}

    if action in ("fast_forward", "rewind"):
        # The L / J keys: one speed step faster forward / backward (skipping 0).
        current = state["speed"] if state["playing"] else 0
        if action == "fast_forward":
            wanted = current + 1 if current + 1 != 0 else 2
        else:
            wanted = current - 1 if current - 1 != 0 else -1
        if not win.should_play(wanted):
            if wanted > 0:
                raise _at_end_error(state)
            raise ToolError("cannot rewind from %s: the playhead is at the start" % state["timecode"])
        if action == "fast_forward":
            win.actionFastForward_trigger()
        else:
            win.actionRewind_trigger()
        return {"action": action, "expect": {"playing": True, "speed": wanted}, "changed": True}

    # step_forward / step_back (Right / Left arrow): pause and move by N frames.
    delta = frames if action == "step_forward" else -frames
    target = max(1, state["frame"] + delta)
    if target == state["frame"]:
        raise ToolError("already at the first frame; nothing to step back to")
    win.step_frames(delta)
    return {"action": action, "expect": {"playing": False, "frame": target, "tolerance": 1},
            "changed": True}


def _transport_summary(action: str, state: dict, changed: bool) -> str:
    at = state["timecode"]
    if action == "stop":
        return "Stopped: paused at the start (%s)." % at
    if action.startswith("step"):
        return "Stepped to %s (frame %d), paused." % (at, state["frame"])
    if state["playing"]:
        speed = state["speed"]
        how = "backwards at %gx" % abs(speed) if speed < 0 else "at %gx" % speed
        return ("Already playing %s (%s)." if not changed else "Playing %s from %s.") % (how, at)
    return ("Already paused at %s." if not changed else "Paused at %s.") % at


@editor_tool(
    "play_tool",
    label="Play / pause",
    schema=obj({
        "action": enum(list(TRANSPORT_ACTIONS),
                       "What to do. play / pause (always pass one of these for a plain 'play' or "
                       "'pause'); toggle = the Space bar; stop = pause and return to the start; "
                       "fast_forward / rewind = the L / J keys (one speed step faster forward / "
                       "backward); step_forward / step_back = pause and move by `frames`.", "toggle"),
        "speed": nullable(integer("For action='play': playback speed, 1 = normal, 2 or 4 = fast, "
                                  "-1 = backwards. Omit for normal speed.", minimum=-16, maximum=16)),
        "frames": integer("For step_forward / step_back: how many frames to move.", 1,
                          minimum=1, maximum=10000),
    }),
    background_safe=True,
    covers=("playback.transport",),
)
def play(action="toggle", speed=None, frames=1):
    """Control preview playback: play, pause, stop, fast-forward, rewind, play at a speed, or step frames.

    Always pass action: 'play' plays from the playhead at normal speed (does
    nothing if it already is), 'pause' pauses, 'toggle' flips it like the
    Space bar, 'stop' pauses and returns to the start. 'fast_forward' / 'rewind'
    change speed one step like the L / J keys; action='play' with speed=2 plays
    at double speed, speed=-1 backwards. 'step_forward' / 'step_back' pause and
    move `frames` frames. The result is the player's real state (playing,
    speed, playhead time). To play from a time or marker, call
    seek_playhead_tool with play=true instead ("play from the start" ->
    seek_playhead_tool to='start', play=true).
    """
    if speed is not None and action not in ("play", "toggle"):
        raise ToolError("speed only applies to action='play'")
    if speed == 0:
        raise ToolError("speed 0 is a pause; use action='pause'")
    if frames != 1 and action not in ("step_forward", "step_back"):
        raise ToolError("frames only applies to step_forward / step_back")
    if speed is not None and action == "toggle":
        action = "play"

    plan = on_main(_start_transport, action, speed, int(frames))
    status, state = _await_state(plan["expect"])
    if status == "timeout":
        raise ToolError("asked the player to %s, but after %.0f s it reports %s at %s, speed %s "
                        "(is the preview loaded?)" % (plan["action"].replace("_", " "), PLAYER_SETTLE_SECONDS,
                                                      "playing" if state["playing"] else "paused",
                                                      state["timecode"], state["speed"]))
    receipt = dict(state, action=plan["action"], changed=plan["changed"])
    summary = _unconfirmed(receipt, _transport_summary(plan["action"], state, plan["changed"]), status)
    return ok(summary, **receipt)


# ---------------------------------------------------------------------------
# Seeking
# ---------------------------------------------------------------------------

def _pick_adjacent(candidates: Iterable[float], current_seconds: float, current_frame: int,
                   direction: int, last_frame: Optional[int]):
    """The nearest stop in *direction* that actually moves the playhead: (seconds, frame) or None."""
    remaining = sorted(set(candidates), reverse=direction < 0)
    while True:
        target = track_ops.adjacent_position(remaining, current_seconds, direction)
        if target is None:
            return None
        frame = track_ops.seconds_to_seek_frame(target, last_frame)
        if frame != current_frame:
            return target, frame
        remaining = [p for p in remaining if p != target]


def _clip_edges(layer: Optional[int]) -> List[float]:
    edges = []
    for clip in project().get("clips") or []:
        if not isinstance(clip, dict):
            continue
        if layer is not None and int(clip.get("layer") or 0) != int(layer):
            continue
        edges.extend([float(clip.get("position") or 0.0), _item_end(clip)])
    return edges


def _center_if_offscreen(frame: int) -> None:
    visible = _visible_range()
    seconds = frame_to_seconds(frame)
    if visible is None or visible[0] <= seconds <= visible[1]:
        return
    win = get_app().window
    try:
        # Tell the timeline where the playhead is going before centering on it
        # (the player reports the new frame a few milliseconds later).
        win.timeline.movePlayhead(int(frame))
        win.actionCenterOnPlayhead_trigger()
    except Exception:
        log.debug("center on playhead skipped", exc_info=True)


def _start_seek(seconds, frame, marker: str, to: str, track: str) -> dict:
    """Resolve the target against the live project and seek like the editor does (GUI thread)."""
    win = get_app().window
    state = _player_state()
    duration = _timeline_duration()
    note = ""
    matched = {}

    moving = {"was_playing": state["playing"], "speed": state["speed"]}
    if to == "start":
        target = _min_frame()
        win.actionJumpStart_trigger()
        return dict(moving, frame=target, target="start", matched={}, note="")
    if to == "end":
        target = _last_frame()
        win.actionJumpEnd_trigger()
        if _content_end() <= 0:
            note = "the timeline is empty"
        return dict(moving, frame=target, target="end", matched={}, note=note)

    last = _last_frame()
    if seconds is not None or frame is not None:
        if frame is not None:
            seconds = frame_to_seconds(frame)
        if duration and seconds > duration + 1e-6:
            raise ToolError("%.3f s is past the end of the timeline (%.3f s long; content ends at %s)"
                            % (seconds, duration, _timecode(_content_end())))
        target = int(frame) if frame is not None else seconds_to_frame(seconds)
        if seconds > _content_end() + 1e-6:
            note = "past the end of the last clip (%s): the preview shows black" % _timecode(_content_end())
        label = "time"
    elif marker:
        rows = _markers_matching(marker)
        if not rows:
            ref = marker.strip().lower()
            rows = [r for r in _marker_rows() if ref and ref in r["name"].lower()]
        if not rows:
            raise ToolError("no marker with id or name %r; %s" % (marker, _marker_listing()))
        if len(rows) > 1:
            note = "%d markers match %r (%s); went to the first" % (
                len(rows), marker, ", ".join(r["timecode"] for r in rows))
        matched = rows[0]
        target = track_ops.seconds_to_seek_frame(matched["seconds"])
        label = "marker"
    else:
        direction = 1 if to.startswith("next") else -1
        if to.endswith("marker"):
            selected = (list(getattr(win, "selected_clips", None) or []),
                        list(getattr(win, "selected_transitions", None) or []),
                        list(getattr(win, "selected_effects", None) or []))
            nothing_selected = not any(selected)
            stops = track_ops.navigation_positions(*selected, last_frame=last if nothing_selected else None)
            what = "marker" if nothing_selected else "marker, selected-item edge or keyframe"
        else:
            layer = resolve_layer(track) if track else None
            stops = _clip_edges(layer)
            what = "clip edge" + (" on that track" if track else "")
        found = _pick_adjacent(stops, state["seconds"], state["frame"], direction, last)
        if found is None:
            raise ToolError("no %s %s %s; %s" % (what, "after" if direction > 0 else "before",
                                                 state["timecode"], _marker_listing()))
        target = found[1]
        label = to
        if to.endswith("marker"):
            matched = next((r for r in _marker_rows() if r["frame"] == target), {})

    win.SeekSignal.emit(int(target))
    _center_if_offscreen(int(target))
    return dict(moving, frame=int(target), target=label, matched=matched, note=note)


def _start_play_here() -> dict:
    win = get_app().window
    state = _player_state()
    if state["playing"]:
        return {"changed": False}
    if _content_end() <= 0:
        raise ToolError("the timeline is empty: there is nothing to play")
    if not win.should_play():
        raise _at_end_error(state)
    win.actionPlay_trigger()
    return {"changed": True}


@editor_tool(
    "seek_playhead_tool",
    label="Move playhead",
    schema=obj({
        "seconds": nullable(number("Go to this timeline time in seconds (0:32 = 32, 1:05 = 65).", minimum=0)),
        "frame": nullable(integer("Go to this 1-based timeline frame.", minimum=1)),
        "marker": string("Go to the marker with this name or marker_id (see list_markers_tool).", ""),
        "to": enum(SEEK_TARGETS,
                   "Go to: 'start' / 'end' of the timeline (Home / End); 'next_marker' / "
                   "'previous_marker' (the editor's Next / Previous Marker: markers, the timeline "
                   "start and end, and the edges and keyframes of selected clips); 'next_edit' / "
                   "'previous_edit' (the nearest clip start or end: a cut point).", ""),
        "track": string("For to='next_edit' / 'previous_edit': only clip edges on this track "
                        "(" + TRACK_REF + ").", ""),
        "play": boolean("Start playback from the new position (e.g. 'play from the start' = "
                        "to='start', play=true).", False),
    }),
    background_safe=True,
    covers=("playback.seek", "marker.navigate"),
)
def seek_playhead(seconds=None, frame=None, marker="", to="", track="", play=False):
    """Move the playhead (go to a time, frame, marker, the start/end, or the next/previous marker or cut).

    Give exactly one of: seconds (timeline seconds: "go to 1:05" -> 65), frame,
    marker (a name like 'Drop' or a marker_id), or to = 'start' | 'end' |
    'next_marker' | 'previous_marker' | 'next_edit' | 'previous_edit'. The
    preview shows the frame there and the Properties panel follows it.
    play=true starts playback from the new position. The result has the
    playhead frame, seconds and timecode (HH:MM:SS,FF as on the ruler), and
    whether it is playing. Examples: "jump to the drop" -> marker='Drop';
    "play from the start" -> to='start', play=true; "next cut on track 1" ->
    to='next_edit', track='1'.
    """
    marker = (marker or "").strip()
    track = (track or "").strip()
    given = [n for n, v in (("seconds", seconds is not None), ("frame", frame is not None),
                            ("marker", bool(marker)), ("to", bool(to))) if v]
    if not given:
        raise ToolError("say where to go: seconds, frame, marker, or to=start/end/next_marker/"
                        "previous_marker/next_edit/previous_edit")
    if len(given) > 1:
        raise ToolError("give only one of seconds, frame, marker or to (got %s)" % ", ".join(given))
    if track and to not in ("next_edit", "previous_edit"):
        raise ToolError("track only applies to to='next_edit' / 'previous_edit'")

    plan = on_main(_start_seek, seconds, frame, marker, to, track)
    if plan["was_playing"]:
        # Seeking while playing keeps playing: accept a couple of seconds of travel.
        slack = int(round(project_fps() * 2 * max(1.0, abs(float(plan["speed"] or 1)))))
    else:
        slack = 1
    status, state = _await_state({"frame": plan["frame"], "tolerance": slack})
    if status == "timeout":
        raise ToolError("asked the player to go to frame %d, but after %.0f s it is at frame %d (%s)"
                        % (plan["frame"], PLAYER_SETTLE_SECONDS, state["frame"], state["timecode"]))
    started = False
    if play:
        started = on_main(_start_play_here)["changed"]
        status, state = _await_state({"playing": True})
        if status == "timeout":
            raise ToolError("moved to %s but playback did not start within %.0f s"
                            % (state["timecode"], PLAYER_SETTLE_SECONDS))
    where = state["timecode"]
    if plan["matched"].get("name"):
        where += " (marker '%s')" % plan["matched"]["name"]
    summary = "Playhead at %s, frame %d%s." % (where, state["frame"],
                                                ", playing" if state["playing"] else "")
    receipt = dict(state, target=plan["target"], started_playing=started)
    if plan["matched"]:
        receipt["marker"] = plan["matched"]
    if plan["note"]:
        receipt["note"] = plan["note"]
        summary += " Note: %s." % plan["note"]
    return ok(_unconfirmed(receipt, summary, status), **receipt)


# ---------------------------------------------------------------------------
# Timeline view: zoom and modes
# ---------------------------------------------------------------------------

_MODE_ACTIONS = (("snapping", "actionSnappingTool"), ("razor", "actionRazorTool"),
                 ("timing", "actionTimingTool"))


def _modes() -> dict:
    win = get_app().window
    out = {}
    for key, attr in _MODE_ACTIONS:
        try:
            value = getattr(win, attr).isChecked()
        except Exception:
            value = None
        out[key] = value if isinstance(value, bool) else None
    return out


def _zoom_state() -> dict:
    win = get_app().window
    slider = getattr(win, "sliderZoomWidget", None)
    scale = _num(getattr(slider, "zoom_factor", None))
    if scale is None:
        scale = _num(project().get("scale"))
    width = _view_width_px()
    out = {"seconds_per_100px": round(float(scale), 4) if scale else None,
           "visible_seconds": round(float(scale) * width / 100.0, 3) if scale and width else None}
    visible = _visible_range()
    if visible:
        out["visible_range"] = list(visible)
    return out


def _show_range(start: float, end: float) -> None:
    """Zoom and scroll so [start, end] fills the timeline, like dragging the zoom slider's handles."""
    slider = get_app().window.sliderZoomWidget
    duration = _timeline_duration()
    if duration <= 0:
        raise ToolError("the timeline has no length yet")
    sp = list(getattr(slider, "scrollbar_position", None) or [0.0, 1.0, 0.0, 0.0])
    while len(sp) < 4:
        sp.append(0.0)
    left = max(0.0, min(1.0, start / duration))
    right = max(left, min(1.0, end / duration))
    min_width = _num(getattr(slider, "min_distance", None), 0.0) or 0.0
    if right - left < min_width:
        right = min(1.0, left + min_width)
        left = max(0.0, right - min_width)
    slider.scrollbar_position = [left, right, sp[2], sp[3]]
    slider.delayed_resize_callback()
    slider.update()


@editor_tool(
    "set_timeline_view_tool",
    label="Timeline view",
    schema=obj({
        "zoom": enum(["", "in", "out", "fit"],
                     "'in' / 'out' = zoom one step like the = / - keys (repeat with steps); 'fit' = show "
                     "every clip, from 0 to the end of the last clip.", ""),
        "steps": integer("How many zoom steps for zoom 'in' / 'out'.", 1, minimum=1, maximum=10),
        "visible_seconds": nullable(number("Zoom so this many seconds fill the timeline width, "
                                           "centered on the playhead.", minimum=0.1)),
        "start_seconds": nullable(number("Show exactly this range: its start (timeline seconds). "
                                         "Give end_seconds too.", minimum=0)),
        "end_seconds": nullable(number("End of the range to show (timeline seconds).", minimum=0)),
        "center_on_playhead": boolean("Scroll so the playhead is in the middle (Center on Playhead).", False),
        "snapping": nullable(boolean("true: dragged clips snap to clip edges, the playhead and "
                                     "markers; false: free dragging.")),
        "razor": nullable(boolean("true: clicking a clip slices it (Razor tool); false: normal clicks.")),
        "timing": nullable(boolean("true: dragging a clip edge retimes (speeds up / slows down) the "
                                   "clip instead of trimming it; false: normal trimming.")),
    }),
    covers=("timeline.zoom", "timeline.modes"),
)
def set_timeline_view(zoom="", steps=1, visible_seconds=None, start_seconds=None, end_seconds=None,
                      center_on_playhead=False, snapping=None, razor=None, timing=None):
    """Change how the timeline is shown: zoom in/out/fit, show N seconds or a time range, center on the playhead, and turn snapping, razor or timing mode on or off.

    Pick at most one zoom: zoom='in' / 'out' (steps = how many), zoom='fit'
    (every clip in view), visible_seconds (e.g. 10 = ten seconds across), or
    start_seconds + end_seconds (show that range). center_on_playhead scrolls
    to the playhead. snapping / razor / timing switch the timeline toolbar's
    modes: they only change what mouse drags and clicks do for the person
    editing, not the project. Nothing here is an undo step. Examples: "zoom to
    fit" -> zoom='fit'; "show me 0:30 to 1:00" -> start_seconds=30,
    end_seconds=60; "turn off snapping" -> snapping=false.
    """
    ranged = start_seconds is not None or end_seconds is not None
    zooms = [n for n, v in (("zoom", bool(zoom)), ("visible_seconds", visible_seconds is not None),
                            ("start_seconds/end_seconds", ranged)) if v]
    if len(zooms) > 1:
        raise ToolError("use only one of %s" % ", ".join(zooms))
    if ranged:
        if start_seconds is None or end_seconds is None:
            raise ToolError("give both start_seconds and end_seconds")
        if end_seconds <= start_seconds:
            raise ToolError("end_seconds must be after start_seconds")
        duration = _timeline_duration()
        if duration and start_seconds >= duration:
            raise ToolError("start_seconds is past the end of the timeline (%.3f s long)" % duration)
    if steps != 1 and zoom not in ("in", "out"):
        raise ToolError("steps only applies to zoom 'in' / 'out'")
    modes = {k: v for k, v in (("snapping", snapping), ("razor", razor), ("timing", timing)) if v is not None}
    if not zooms and not center_on_playhead and not modes:
        raise ToolError("nothing to change: pass zoom, visible_seconds, start_seconds/end_seconds, "
                        "center_on_playhead or a mode")

    win = get_app().window
    done = []
    if zoom in ("in", "out"):
        trigger = win.actionTimelineZoomIn_trigger if zoom == "in" else win.actionTimelineZoomOut_trigger
        for _ in range(int(steps)):
            trigger()
        done.append("zoomed %s %d step%s" % (zoom, steps, "" if steps == 1 else "s"))
    elif zoom == "fit":
        end = _content_end()
        if end <= 0:
            end = _timeline_duration()
            done.append("the timeline is empty; showing all of it")
        else:
            done.append("fit every clip (0 to %s)" % _timecode(end))
        _show_range(0.0, end * 1.02)
    elif visible_seconds is not None:
        duration = _timeline_duration()
        span = min(float(visible_seconds), duration) if duration else float(visible_seconds)
        middle = playhead_seconds()
        start = max(0.0, middle - span / 2.0)
        if duration:
            start = max(0.0, min(start, duration - span))
        _show_range(start, start + span)
        done.append("showing %g s around the playhead" % span)
    elif ranged:
        end = min(float(end_seconds), _timeline_duration() or float(end_seconds))
        _show_range(float(start_seconds), end)
        done.append("showing %s to %s" % (_timecode(start_seconds), _timecode(end)))

    if center_on_playhead:
        win.actionCenterOnPlayhead_trigger()
        done.append("centered on the playhead")

    before = _modes()
    for key, value in modes.items():
        attr = dict(_MODE_ACTIONS)[key]
        action = getattr(win, attr)
        if before.get(key) is value:
            done.append("%s already %s" % (key, "on" if value else "off"))
            continue
        action.setChecked(bool(value))
        getattr(win, attr + "_trigger")(bool(value))
        done.append("%s %s" % (key, "on" if value else "off"))

    state = {"zoom": _zoom_state(), "modes": _modes()}
    return ok("Timeline view: %s." % "; ".join(done), **state)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _selection_pairs() -> List[Tuple[str, str]]:
    """The editor's current selection as (id, type) pairs, in selection order."""
    win = get_app().window
    items = getattr(win, "selected_items", None)
    if isinstance(items, list):
        return [(str(s.get("id")), str(s.get("type"))) for s in items
                if isinstance(s, dict) and s.get("id")]
    out = []
    for kind, attr in (("clip", "selected_clips"), ("transition", "selected_transitions"),
                       ("effect", "selected_effects")):
        ids = getattr(win, attr, None)
        if isinstance(ids, list):
            out.extend((str(i), kind) for i in ids)
    return out


def _describe_selection(pairs: Optional[List[Tuple[str, str]]] = None) -> dict:
    from classes.query import Clip, Effect, Transition
    pairs = _selection_pairs() if pairs is None else pairs
    clips, transitions, effects = [], [], []
    for item_id, kind in pairs:
        if kind == "clip":
            clip = Clip.get(id=item_id)
            if clip:
                row = describe_clip(clip)
                row["locked"] = is_locked(row["layer"])
                clips.append(row)
        elif kind == "transition":
            tran = Transition.get(id=item_id)
            if tran:
                data = tran.data or {}
                layer = int(data.get("layer") or 0)
                transitions.append({"transition_id": item_id, "track": _track_row(layer)["track"],
                                    "layer": layer, "position": round(float(data.get("position") or 0.0), 3),
                                    "end": round(_item_end(data), 3)})
        elif kind == "effect":
            effect = Effect.get(id=item_id)
            if effect:
                effects.append({"effect_id": item_id, "class_name": str(effect.data.get("class_name") or ""),
                                "timeline_clip_id": str((effect.parent or {}).get("id") or "")})
    return {"clips": clips, "transitions": transitions, "effects": effects,
            "count": len(clips) + len(transitions) + len(effects)}


def _ui_select(item_id: str, kind: str, clear_existing: bool = False) -> None:
    """Select one item the way a click does (Ctrl+click when not clearing)."""
    win = get_app().window
    try:
        win.timeline.AddSelectionJS(item_id, kind, clear_existing)
    except Exception:
        log.debug("timeline selection call failed", exc_info=True)
    if (item_id, kind) not in _selection_pairs():
        # Web timeline backends answer asynchronously; keep the editor's selection right now.
        win.addSelection(item_id, kind, clear_existing)


def _ui_deselect(item_id: str, kind: str) -> None:
    win = get_app().window
    deselect = getattr(win.timeline, "_deselect_timeline_item", None)
    try:
        if callable(deselect):
            deselect(item_id, kind)
    except Exception:
        log.debug("timeline deselect call failed", exc_info=True)
    if (item_id, kind) in _selection_pairs():
        win.removeSelection(item_id, kind)


def _ui_clear() -> None:
    win = get_app().window
    try:
        win.timeline.ClearAllSelections()
    except Exception:
        log.debug("ClearAllSelections failed", exc_info=True)
    if _selection_pairs():
        win.clearSelections()


def _overlaps(data: dict, start: Optional[float], end: Optional[float]) -> bool:
    if start is None:
        return True
    position = float(data.get("position") or 0.0)
    item_end = _item_end(data)
    if end is None or end <= start:
        return position <= start < item_end
    return position < end and item_end > start


def _resolve_targets(timeline_clip_ids, transition_ids, effect_ids, clip_query, track,
                     start_seconds, end_seconds, include_transitions) -> List[Tuple[str, str]]:
    from classes.query import Clip, Effect, Transition
    targets: List[Tuple[str, str]] = []

    def add(pair):
        if pair not in targets:
            targets.append(pair)

    for cid in timeline_clip_ids:
        if not Clip.get(id=cid):
            raise ToolError("no timeline clip with id %r" % cid)
        add((cid, "clip"))
    for tid in transition_ids:
        if not Transition.get(id=tid):
            raise ToolError("no transition with id %r" % tid)
        add((tid, "transition"))
    for eid in effect_ids:
        if not Effect.get(id=eid):
            raise ToolError("no effect with id %r on any clip" % eid)
        add((eid, "effect"))

    if clip_query:
        add((str(resolve_clip(clip_query=clip_query, track=track).id), "clip"))
    elif track or start_seconds is not None:
        layer = resolve_layer(track) if track else None
        found = []
        for clip in Clip.filter():
            data = clip.data or {}
            if layer is not None and int(data.get("layer") or 0) != layer:
                continue
            if _overlaps(data, start_seconds, end_seconds):
                found.append((float(data.get("position") or 0.0), int(data.get("layer") or 0), clip.id, "clip"))
        if include_transitions:
            for tran in Transition.filter():
                data = tran.data or {}
                if layer is not None and int(data.get("layer") or 0) != layer:
                    continue
                if _overlaps(data, start_seconds, end_seconds):
                    found.append((float(data.get("position") or 0.0), int(data.get("layer") or 0),
                                  tran.id, "transition"))
        if not found:
            where = ("on track %s" % track) if track else "on any track"
            if start_seconds is not None:
                where += " between %s and %s" % (_timecode(start_seconds),
                                                 _timecode(end_seconds if end_seconds is not None else start_seconds))
            raise ToolError("nothing to select %s" % where)
        for _pos, _layer, item_id, kind in sorted(found):
            add((str(item_id), kind))
    return targets


@editor_tool(
    "select_timeline_items_tool",
    label="Select on timeline",
    schema=obj({
        "mode": enum(list(SELECT_MODES),
                     "'replace' (default) = select exactly these; 'add' / 'remove' = add them to / "
                     "take them out of the current selection; 'all' = every clip and transition "
                     "(Ctrl+A); 'none' = clear the selection (Ctrl+Shift+A); 'ripple' = these (or the "
                     "current selection) plus everything after them on the same track (Alt+A).",
                     "replace"),
        "timeline_clip_ids": array({"type": "string"}, "Timeline clip ids (list_clips_tool / "
                                   "get_timeline_state_tool)."),
        "transition_ids": array({"type": "string"}, "Transition ids."),
        "effect_ids": array({"type": "string"}, "Effect ids from a clip's effects; the Properties "
                            "panel then shows that effect."),
        "clip_query": string("Describe one clip instead of ids (file name or content, e.g. 'the "
                             "interview clip'); track narrows it.", ""),
        "track": string("Every clip on this track (" + TRACK_REF + "); with start/end only the clips "
                        "in that range.", ""),
        "start_seconds": nullable(number("Clips overlapping the range start_seconds..end_seconds "
                                         "(timeline seconds). Without end_seconds: clips under that "
                                         "moment.", minimum=0)),
        "end_seconds": nullable(number("End of the time range.", minimum=0)),
        "include_transitions": boolean("With track or a time range: also select the transitions "
                                       "there.", False),
        "show_properties": boolean("Also bring the Properties panel to the front, like "
                                   "double-clicking a clip.", False),
    }),
    covers=("selection.select",),
)
def select_timeline_items(mode="replace", timeline_clip_ids=None, transition_ids=None, effect_ids=None,
                          clip_query="", track="", start_seconds=None, end_seconds=None,
                          include_transitions=False, show_properties=False):
    """Select clips, transitions or effects on the timeline, as clicking, Ctrl+click, Ctrl+A or Alt+A would.

    The selection is what the Properties panel shows and what other tools use
    for scope='selected'. Say what to select with ids, clip_query, track (every
    clip on it) and/or a time range (start_seconds..end_seconds, on every track
    or on `track`). mode: 'replace' (default), 'add', 'remove', 'all', 'none',
    'ripple'. Returns the resulting selection. Selecting is not an undo step
    and does not change the project. Examples: "select every clip on track 1"
    -> track='1'; "select the clips between 0:10 and 0:20" ->
    start_seconds=10, end_seconds=20; "deselect everything" -> mode='none'.
    """
    timeline_clip_ids = [str(i).strip() for i in (timeline_clip_ids or []) if str(i).strip()]
    transition_ids = [str(i).strip() for i in (transition_ids or []) if str(i).strip()]
    effect_ids = [str(i).strip() for i in (effect_ids or []) if str(i).strip()]
    clip_query = (clip_query or "").strip()
    track = (track or "").strip()
    has_target = bool(timeline_clip_ids or transition_ids or effect_ids or clip_query or track
                      or start_seconds is not None)
    if end_seconds is not None and start_seconds is None:
        raise ToolError("end_seconds needs start_seconds")
    if start_seconds is not None and end_seconds is not None and end_seconds < start_seconds:
        raise ToolError("end_seconds must not be before start_seconds")
    if mode in ("all", "none") and has_target:
        raise ToolError("mode='%s' takes no targets; use mode='replace' with track/ids to select part "
                        "of the timeline" % mode)
    if mode in ("replace", "add", "remove") and not has_target:
        raise ToolError("mode='%s' needs something to select: ids, clip_query, track or a time range"
                        % mode)

    targets = _resolve_targets(timeline_clip_ids, transition_ids, effect_ids, clip_query, track,
                               start_seconds, end_seconds, include_transitions) if has_target else []
    win = get_app().window
    before = _selection_pairs()

    if mode == "none":
        _ui_clear()
    elif mode == "all":
        from classes.query import Clip, Transition
        if not (Clip.filter() or Transition.filter()):
            raise ToolError("the timeline has no clips or transitions to select")
        try:
            win.timeline.SelectAll()
        except Exception:
            log.debug("SelectAll failed", exc_info=True)
        if not _selection_pairs():
            win.clearSelections()
            for item in list(Clip.filter()) + list(Transition.filter()):
                win.addSelection(item.id, "clip" if isinstance(item, Clip) else "transition", False)
    elif mode == "replace":
        for index, (item_id, kind) in enumerate(targets):
            _ui_select(item_id, kind, clear_existing=(index == 0))
    elif mode == "add":
        for item_id, kind in targets:
            if (item_id, kind) not in _selection_pairs():
                _ui_select(item_id, kind)
    elif mode == "remove":
        for item_id, kind in targets:
            _ui_deselect(item_id, kind)
    else:  # ripple
        seeds = targets or [p for p in before if p[1] in ("clip", "transition")]
        if not seeds:
            raise ToolError("mode='ripple' needs clips: pass ids/track, or select something first")
        for item_id, kind in seeds:
            if kind in ("clip", "transition"):
                if (item_id, kind) not in _selection_pairs():
                    _ui_select(item_id, kind)
                win.timeline.addRippleSelection(item_id, kind)

    if show_properties:
        try:
            win.actionProperties_trigger()
        except Exception:
            log.debug("could not show the Properties panel", exc_info=True)

    selection = _describe_selection()
    parts = []
    for key, noun in (("clips", "clip"), ("transitions", "transition"), ("effects", "effect")):
        n = len(selection[key])
        if n:
            parts.append("%d %s%s" % (n, noun, "" if n == 1 else "s"))
    summary = "Selection: %s." % (", ".join(parts) if parts else "nothing")
    if mode == "remove" and not any(p in before for p in targets):
        summary += " (none of those were selected)"
    return ok(summary, mode=mode, selection=selection)


@editor_tool(
    "get_playhead_and_selection_tool",
    label="Read playhead and selection",
    schema=obj({}),
    read_only=True,
    covers=("selection.select", "playback.seek"),
)
def get_playhead_and_selection():
    """Read what the editor is showing right now: the playhead, playback, the selection and the timeline view.

    Returns the playhead (frame, seconds, timecode as on the ruler), whether
    the preview is playing and at what speed, the selected clips /
    transitions / effects (ids, track, times, locked), the zoom (seconds
    visible, visible range) and whether snapping, razor and timing modes are
    on, plus where the content ends and the markers just before and after the
    playhead. Use it for "what's selected?", "where is the playhead?" or before
    acting on the selection. Read-only.
    """
    state = _player_state()
    markers = _marker_rows()
    before = [r for r in markers if r["frame"] < state["frame"]]
    after = [r for r in markers if r["frame"] > state["frame"]]
    selection = _describe_selection()
    receipt = {
        "playhead": {k: state[k] for k in ("frame", "seconds", "timecode")},
        "playback": {"playing": state["playing"], "speed": state["speed"]},
        "selection": selection,
        "view": {"zoom": _zoom_state(), "modes": _modes()},
        "timeline": {"content_end": _when(_content_end()), "duration": round(_timeline_duration(), 3),
                     "markers": len(markers),
                     "previous_marker": before[-1] if before else None,
                     "next_marker": after[0] if after else None},
    }
    return ok("Playhead at %s (frame %d), %s; %d item(s) selected."
              % (state["timecode"], state["frame"], "playing" if state["playing"] else "paused",
                 selection["count"]), **receipt)
