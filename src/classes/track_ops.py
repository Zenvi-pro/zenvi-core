"""Track and marker rules behind the Track menu, the Marker menu and marker navigation.

Qt-free: everything works on the project through ``classes.query`` and
``get_app().updates``, so the main window's menu handlers and the agent tools
(``classes.editor_tools.tracks_nav``) share one implementation.

Track numbers are OpenShot layer numbers (higher is drawn on top; UI track 1 is
the lowest number). A new track takes the midpoint of the gap it goes into;
when that gap is too small, every track is renumbered to multiples of
``TRACK_STRIDE`` first. The renumbering is part of the same undo step as the
insert, so undo restores the old numbers and older history entries keep
pointing at tracks that exist.
"""

from __future__ import annotations

import contextlib
from typing import Callable, Iterable, List, Optional

from classes.logger import log

TRACK_STRIDE = 1000000
TRACK_POSITIONS = ("top", "bottom", "above", "below")

# Marker colors are stored the way the Add Marker action stores its default
# ({"icon": "blue.png", "vector": "blue"}).
MARKER_COLORS = ("blue", "red", "green", "yellow", "orange", "purple", "pink", "white")
DEFAULT_MARKER_COLOR = "blue"

# Two navigation stops closer than this (seconds) count as the same place.
NAVIGATION_EPSILON = 0.001


class TrackOpError(ValueError):
    """A track or marker request the project cannot satisfy."""


def _app():
    # Looked up on every call (not imported once) so tests that patch
    # classes.app.get_app reach this module too.
    from classes import app as app_module
    return app_module.get_app()


def _project():
    return _app().project


@contextlib.contextmanager
def _one_undo_step():
    """Group the enclosed mutations into one undo step, joining the caller's step if any."""
    from classes.tool_handlers import _transaction
    with _transaction(_app()) as tid:
        yield tid


def _fps_float() -> float:
    fps = _project().get("fps") or {}
    try:
        num, den = float(fps.get("num") or 30), float(fps.get("den") or 1)
    except (TypeError, ValueError, AttributeError):
        num, den = 30.0, 1.0
    return num / (den or 1.0)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

def sorted_tracks() -> List[dict]:
    """Project layers, bottom of the stack first."""
    layers = _project().get("layers") or []
    return sorted((t for t in layers if isinstance(t, dict)), key=lambda t: int(t.get("number") or 0))


def track_numbers() -> List[int]:
    return [int(t.get("number") or 0) for t in sorted_tracks()]


def get_track(track_id=None, layer=None):
    """The Track query object for a track id or a layer number."""
    from classes.query import Track
    track = None
    if track_id:
        track = Track.get(id=track_id)
    elif layer is not None:
        track = Track.get(number=int(layer))
    if not track:
        raise TrackOpError(f"no track with {'id ' + repr(track_id) if track_id else 'layer ' + str(layer)}")
    return track


def plan_insert(numbers: Iterable[int], position: str, relative_layer: Optional[int] = None):
    """Where a new track goes, by the Add Track rules.

    Returns ``("number", n)`` -- the free layer number to create -- or
    ``("renumber", index)`` when the gap is too small: renumber every track and
    insert at *index* of the bottom-up track list.

    * top    -- above every track (highest number + stride), like the Add Track button.
    * bottom -- below every track.
    * above / below -- next to *relative_layer*, at the midpoint of the gap
      (Track menu > Add Track Above / Below).
    """
    numbers = sorted(int(n) for n in numbers)
    if position not in TRACK_POSITIONS:
        raise TrackOpError(f"position must be one of {', '.join(TRACK_POSITIONS)}")
    if not numbers:
        return ("number", TRACK_STRIDE)
    if position == "top":
        return ("number", numbers[-1] + TRACK_STRIDE)
    if position == "bottom":
        position, relative_layer = "below", numbers[0]
    if relative_layer is None or int(relative_layer) not in numbers:
        raise TrackOpError(f"no track with layer number {relative_layer}")
    relative_layer = int(relative_layer)
    index = numbers.index(relative_layer)
    if position == "above":
        if index + 1 < len(numbers):
            delta = abs(relative_layer - numbers[index + 1])
        else:
            delta = 2 * TRACK_STRIDE
        if delta > 2:
            return ("number", relative_layer + int(round(delta / 2.0)))
        return ("renumber", index + 1)
    # below
    delta = abs(relative_layer - numbers[index - 1]) if index > 0 else relative_layer
    if delta > 2:
        return ("number", relative_layer - int(round(delta / 2.0)))
    return ("renumber", index)


def _create_track(number: int, label: str = "", lock: bool = False) -> int:
    from classes.query import Track
    track = Track()
    track.data = {"number": int(number), "y": 0, "label": str(label or ""), "lock": bool(lock)}
    track.save()
    return int(number)


def renumber_tracks(insert_at: Optional[int] = None, stride: int = TRACK_STRIDE,
                    new_track_label: str = "") -> Optional[int]:
    """Renumber every track to multiples of *stride*; their clips and transitions follow.

    With *insert_at* (an index into the bottom-up track list), a gap is left
    there and a new track created in it; its number is returned.
    """
    from classes.query import Clip, Track, Transition

    updates = _app().updates
    tracks = sorted_tracks()
    log.info("Renumbering %d tracks (stride %d, insert at %s)", len(tracks), stride, insert_at)
    slots: list = list(tracks)
    if insert_at is not None and int(insert_at) < len(slots) + 1:
        slots.insert(int(insert_at), None)

    # Collect every item first: the loop below reuses numbers that other
    # tracks still hold until their turn comes.
    targets = []
    insert_num = None
    for index, layer in enumerate(slots):
        new_number = (index + 1) * stride
        if layer is None:
            insert_num = new_number
            continue
        old_number = int(layer.get("number") or 0)
        track = Track.get(id=layer.get("id")) if layer.get("id") else Track.get(number=old_number)
        if not track:
            log.error("Track number %s not found while renumbering", old_number)
            continue
        targets.append((new_number, track, list(Clip.filter(layer=old_number)),
                        list(Transition.filter(layer=old_number))))

    with _one_undo_step():
        for new_number, track, clips, transitions in targets:
            if int(track.data.get("number") or 0) != new_number:
                updates.update(["layers", {"id": track.id}], {"number": new_number})
            for item in clips:
                updates.update(["clips", {"id": item.id}], {"layer": new_number})
            for item in transitions:
                updates.update(["effects", {"id": item.id}], {"layer": new_number})
        if insert_num is not None:
            _create_track(insert_num, new_track_label)
    return insert_num


def insert_track(position: str = "top", relative_layer: Optional[int] = None, label: str = "") -> int:
    """Create one track at *position* (see :func:`plan_insert`); returns its layer number."""
    kind, value = plan_insert(track_numbers(), position, relative_layer)
    with _one_undo_step():
        if kind == "number":
            return _create_track(value, label)
        number = renumber_tracks(insert_at=value, new_track_label=label)
    if number is None:
        raise TrackOpError("could not insert the track")
    return number


def rename_track(track_id: str, label: str) -> bool:
    """Set a track's name (empty = the default 'Track N'). False when it already had that name."""
    track = get_track(track_id)
    label = str(label or "")
    if str(track.data.get("label") or "") == label:
        return False
    track.data["label"] = label
    track.save()
    return True


def set_track_lock(track_id: str, locked: bool) -> bool:
    """Lock or unlock a track. False when it was already in that state."""
    track = get_track(track_id)
    if bool(track.data.get("lock")) == bool(locked):
        return False
    track.data["lock"] = bool(locked)
    track.save()
    return True


def track_contents(layer: int) -> dict:
    """Ids of the clips and transitions on a track."""
    from classes.query import Clip, Transition
    return {"clips": [c.id for c in Clip.filter(layer=int(layer))],
            "transitions": [t.id for t in Transition.filter(layer=int(layer))]}


def remove_track(track_id: str,
                 on_item_removed: Optional[Callable[[str, str], None]] = None) -> dict:
    """Delete a track with its clips and transitions (Track menu > Remove Track), in one undo step.

    *on_item_removed(item_id, item_type)* runs before each clip/transition is
    deleted, so the caller can drop it from the selection first. Refuses to
    remove the last track. Returns the deleted clip and transition ids.
    """
    from classes.query import Clip, Transition

    track = get_track(track_id)
    if len(_project().get("layers") or []) <= 1:
        raise TrackOpError("You must keep at least 1 track")
    layer = int(track.data.get("number") or 0)
    removed = {"clips": [], "transitions": []}
    with _one_undo_step():
        for clip in Clip.filter(layer=layer):
            if on_item_removed:
                on_item_removed(clip.id, "clip")
            clip.delete()
            removed["clips"].append(clip.id)
        for tran in Transition.filter(layer=layer):
            if on_item_removed:
                on_item_removed(tran.id, "transition")
            tran.delete()
            removed["transitions"].append(tran.id)
        track.delete()
    return removed


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def marker_color(data: dict) -> str:
    vector = str((data or {}).get("vector") or "").strip().lower()
    if vector:
        return vector
    icon = str((data or {}).get("icon") or "").strip().lower()
    return icon[:-4] if icon.endswith(".png") else (icon or DEFAULT_MARKER_COLOR)


def _check_color(color: str) -> str:
    color = str(color or DEFAULT_MARKER_COLOR).strip().lower()
    if color not in MARKER_COLORS:
        raise TrackOpError(f"marker color must be one of {', '.join(MARKER_COLORS)}")
    return color


def add_marker(position_seconds: float, name: str = "", color: str = DEFAULT_MARKER_COLOR):
    """Add a marker at *position_seconds* (Add Marker / M); returns the Marker."""
    from classes.query import Marker
    color = _check_color(color)
    marker = Marker()
    marker.data = {"position": float(position_seconds), "icon": f"{color}.png", "vector": color}
    if name:
        marker.data["name"] = str(name)
    marker.save()
    return marker


def update_marker(marker_id: str, name: Optional[str] = None, color: Optional[str] = None,
                  position_seconds: Optional[float] = None) -> List[str]:
    """Rename, recolor or move a marker. Returns the keys that changed (empty = no-op)."""
    from classes.query import Marker
    marker = Marker.get(id=marker_id)
    if not marker:
        raise TrackOpError(f"no marker with id {marker_id!r}")
    data = marker.data
    changed = []
    if name is not None and str(data.get("name") or "") != str(name):
        data["name"] = str(name)
        changed.append("name")
    if color is not None:
        color = _check_color(color)
        if marker_color(data) != color:
            data["icon"], data["vector"] = f"{color}.png", color
            changed.append("color")
    if position_seconds is not None and abs(float(data.get("position") or 0.0) - float(position_seconds)) > 1e-9:
        data["position"] = float(position_seconds)
        changed.append("position")
    if changed:
        marker.save()
    return changed


def remove_markers(marker_ids: Iterable[str]) -> List[str]:
    """Delete markers by id (Marker menu > Remove Marker), in one undo step."""
    from classes.query import Marker
    removed = []
    with _one_undo_step():
        for marker_id in marker_ids:
            for marker in Marker.filter(id=marker_id):
                marker.delete()
                removed.append(marker.id)
    return removed


def markers_in_time_order() -> List[dict]:
    """Every marker as {id, position, name, color}, earliest first."""
    from classes.query import Marker
    rows = []
    for m in Marker.filter():
        data = m.data or {}
        try:
            position = float(data.get("position") or 0.0)
        except (TypeError, ValueError):
            position = 0.0
        rows.append({"id": str(m.id), "position": position, "name": str(data.get("name") or ""),
                     "color": marker_color(data)})
    rows.sort(key=lambda r: (r["position"], r["id"]))
    return rows


# ---------------------------------------------------------------------------
# Previous / Next Marker navigation
# ---------------------------------------------------------------------------

def _keyframe_times(value, to_seconds, lower, upper, out):
    """Collect keyframe times (strictly inside lower..upper) from a property tree."""
    if isinstance(value, dict):
        points = value.get("Points")
        if isinstance(points, list):
            for point in points:
                try:
                    t = to_seconds(point["co"]["X"])
                except (TypeError, KeyError, ValueError):
                    continue
                if lower < t < upper:
                    out.append(t)
            return
        for child in value.values():
            _keyframe_times(child, to_seconds, lower, upper, out)
    elif isinstance(value, list):
        for child in value:
            _keyframe_times(child, to_seconds, lower, upper, out)


def _object_positions(data: dict, fps_float: float) -> list:
    """Edges and keyframes of a clip or transition, in timeline seconds."""
    frame_duration = 1.0 / fps_float
    start_time = float(data["position"])
    orig_time = start_time - float(data["start"])
    # The last frame of the item is one frame before its end.
    stop_time = orig_time + float(data["end"]) - frame_duration
    positions = [start_time, stop_time]
    for value in data.values():
        _keyframe_times(value, lambda x: (float(x) - 1) / fps_float - float(data["start"]) + start_time,
                        start_time, stop_time, positions)
    return positions


def navigation_positions(selected_clips: Iterable[str] = (), selected_transitions: Iterable[str] = (),
                         selected_effects: Iterable[str] = (), last_frame: Optional[int] = None) -> list:
    """Every place Previous/Next Marker stops at, in timeline seconds (unsorted, unique).

    The timeline start and every marker; with nothing selected also the end
    of the last clip (*last_frame*); otherwise the edges and keyframes of the
    selected clips and transitions, or -- when effects are selected -- their
    parent clips' edges and the effects' keyframes.
    """
    from classes.query import Clip, Effect, Marker, Transition

    selected_clips = list(selected_clips or [])
    selected_transitions = list(selected_transitions or [])
    selected_effects = list(selected_effects or [])
    fps_float = _fps_float()
    frame_duration = 1.0 / fps_float

    positions = [0.0]
    if not (selected_clips or selected_transitions or selected_effects) and last_frame is not None:
        positions.append((int(last_frame) - 1) / fps_float)
    for marker in Marker.filter():
        try:
            positions.append(float(marker.data["position"]))
        except (KeyError, TypeError, ValueError):
            continue

    if selected_effects:
        for effect_id in selected_effects:
            effect = Effect.get(id=effect_id)
            if not effect:
                continue
            parent = effect.parent
            start_time = float(parent["position"])
            orig_time = start_time - float(parent["start"])
            stop_time = orig_time + float(parent["end"]) - frame_duration
            positions.extend([start_time, stop_time])
            for value in effect.data.values():
                _keyframe_times(value, lambda x: (float(x) - 1) / fps_float + orig_time,
                                start_time, stop_time, positions)
    else:
        for clip_id in selected_clips:
            clip = Clip.get(id=clip_id)
            if clip:
                positions.extend(_object_positions(clip.data, fps_float))
        for tran_id in selected_transitions:
            tran = Transition.get(id=tran_id)
            if tran:
                positions.extend(_object_positions(tran.data, fps_float))

    return list(set(positions))


def adjacent_position(positions: Iterable[float], current: float, direction: int) -> Optional[float]:
    """The nearest stop before (direction < 0) or after (direction > 0) *current*, or None."""
    if direction < 0:
        before = [p for p in positions if p < current and abs(p - current) > NAVIGATION_EPSILON]
        return max(before) if before else None
    after = [p for p in positions if p > current and abs(p - current) > NAVIGATION_EPSILON]
    return min(after) if after else None


def seconds_to_seek_frame(seconds: float, last_frame: Optional[int] = None) -> int:
    """Timeline seconds -> the 1-based frame navigation seeks to (clamped to the last frame)."""
    frame = int(round(float(seconds) * _fps_float())) + 1
    if last_frame is not None:
        frame = min(frame, int(last_frame))
    return max(1, frame)
