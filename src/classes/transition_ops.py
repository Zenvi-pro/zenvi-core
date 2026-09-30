"""
 @file
 @brief Transition (Mask) rules shared by the timeline and the agent tools.

 The timeline's drop, drag, Reverse and Properties edits and the Zenvi
 Assistant's transition tools apply the same rules: how a Mask transition is
 built, how its brightness/contrast keyframes follow a duration change, how it
 is reversed and which way it faces. Pure data in, data out (project fps passed
 in), so the rules run headless in the unit tests; ``TimelineView`` keeps thin
 wrappers with the historical method names.

 A Mask transition wipes from the clip below to the one above along a grayscale
 image: ``brightness`` animates the wipe (1 -> -1 = the default direction, the
 lower layer fading out), ``contrast`` is the edge hardness (0-20; 3 = soft).
 Keyframe X values are 1-based frames of the transition itself.
"""

import copy
import os

from classes import info
from classes.logger import log

# The editor's default Mask curve (Timeline.addTransition / add_missing_transition).
DEFAULT_BRIGHTNESS = (1.0, -1.0)
DEFAULT_CONTRAST = 3.0
BEZIER = 0

TRANSITION_EXTENSIONS = (".svg", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
                         ".mp4", ".mov", ".webm", ".mkv", ".avi")


def _point(x, y, interpolation=BEZIER):
    """One libopenshot keyframe point, shaped like ``openshot.Keyframe.AddPoint`` JSON."""
    return {
        "co": {"X": float(x), "Y": float(y)},
        "handle_left": {"X": 0.5, "Y": 1.0},
        "handle_right": {"X": 0.5, "Y": 0.0},
        "handle_type": 0,
        "interpolation": int(interpolation),
    }


def frames_for(duration, fps_float):
    """Whole frames in *duration* seconds (at least 0)."""
    return max(0, int(round(max(0.0, float(duration or 0.0)) * float(fps_float))))


def duration_of(transition_data):
    try:
        return max(0.0, float(transition_data.get("end", 0.0) or 0.0)
                   - float(transition_data.get("start", 0.0) or 0.0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


# ---------------------------------------------------------------------------
# Keyframe rules
# ---------------------------------------------------------------------------

def scale_keyframes(keyframe, factor):
    """Scale the X values of keyframe points (frame 1 stays put)."""
    for point in keyframe.get("Points", []):
        if "co" in point and "X" in point["co"] and point["co"]["X"] != 1:
            point["co"]["X"] = round((point["co"]["X"] - 1) * factor) + 1


def anchor_endpoint_keyframes(transition_data, total_frames):
    """Keep static transition endpoint keyframes anchored to the transition edges."""
    if total_frames <= 0 or not isinstance(transition_data, dict):
        return
    last_frame = int(total_frames) + 1
    for prop in ("brightness", "contrast"):
        keyframe = transition_data.get(prop)
        points = keyframe.get("Points") if isinstance(keyframe, dict) else None
        if not isinstance(points, list) or len(points) < 2:
            continue
        first = points[0].get("co") if isinstance(points[0], dict) else None
        last = points[-1].get("co") if isinstance(points[-1], dict) else None
        if isinstance(first, dict):
            first["X"] = 1
        if isinstance(last, dict):
            last["X"] = last_frame


def reverse_keyframes(keyframe):
    """Mirror keyframe positions in time, swapping bezier handles."""
    points = keyframe.get("Points", [])
    x_values = [
        point["co"]["X"]
        for point in points
        if isinstance(point.get("co"), dict) and "X" in point["co"]
    ]
    if not x_values:
        return

    # Keyframe X positions are 1-indexed.  Use the actual min/max X values to
    # determine the reflection pivot so we don't lose leading keyframes when the
    # last point is stored at duration + 1.
    pivot = min(x_values) + max(x_values)

    new_points = []
    for point in points:
        new_point = copy.deepcopy(point)
        if isinstance(new_point.get("co"), dict) and "X" in new_point["co"]:
            new_point["co"]["X"] = pivot - point["co"]["X"]
            hl = new_point.pop("handle_left", None)
            hr = new_point.pop("handle_right", None)
            if hr is not None:
                new_point["handle_left"] = hr
            if hl is not None:
                new_point["handle_right"] = hl
        new_points.append(new_point)

    keyframe["Points"] = sorted(new_points, key=lambda p: p.get("co", {}).get("X", 0))


def reverse_transition_data(transition_data):
    """Transition > Reverse Transition: mirror the brightness and contrast curves in time."""
    for prop in ("brightness", "contrast"):
        keyframe = transition_data.get(prop)
        if isinstance(keyframe, dict):
            reverse_keyframes(keyframe)
    return transition_data


def rescale_curves(transition_data, old_duration, new_duration, fps_float):
    """Fit a static mask's curves to a new length (what a drag-trim does), end points pinned."""
    old_frames = frames_for(old_duration, fps_float)
    new_frames = frames_for(new_duration, fps_float)
    if not uses_static_mask(transition_data):
        return
    if old_frames and new_frames and old_frames != new_frames:
        for prop in ("brightness", "contrast"):
            if isinstance(transition_data.get(prop), dict):
                scale_keyframes(transition_data[prop], new_frames / old_frames)
    if new_frames:
        anchor_endpoint_keyframes(transition_data, new_frames)


def default_keyframes(duration, start_value, end_value, contrast_value, fps_float):
    """(brightness, contrast) keyframe JSON for a fresh transition of *duration* seconds."""
    brightness = {"Points": [_point(1, start_value)]}
    if float(start_value) != float(end_value):
        brightness["Points"].append(_point(frames_for(duration, fps_float) + 1, end_value))
    contrast = {"Points": [_point(1, contrast_value)]}
    return brightness, contrast


def direction_of(transition_data):
    """'default' (brightness falls: the lower clip wipes away), 'reversed' (rises), or None."""
    brightness = transition_data.get("brightness") if isinstance(transition_data, dict) else None
    points = brightness.get("Points", []) if isinstance(brightness, dict) else []
    keyed = []
    for point in points:
        co = point.get("co") if isinstance(point, dict) else None
        if not isinstance(co, dict):
            continue
        try:
            keyed.append((float(co.get("X")), float(co.get("Y"))))
        except (TypeError, ValueError):
            continue
    if len(keyed) < 2:
        return None
    keyed.sort(key=lambda k: k[0])
    if keyed[0][1] > keyed[-1][1]:
        return "default"
    if keyed[0][1] < keyed[-1][1]:
        return "reversed"
    return None


# ---------------------------------------------------------------------------
# Mask readers
# ---------------------------------------------------------------------------

def mask_reader(transition_data, fallback_data=None):
    """Reader metadata of a transition payload ({} when none)."""
    for data in (transition_data, fallback_data):
        if isinstance(data, dict):
            for key in ("mask_reader", "reader"):
                reader = data.get(key)
                if isinstance(reader, dict):
                    return reader
    return {}


def uses_static_mask(transition_data, fallback_data=None):
    """True when a transition uses a static single-image mask."""
    from classes.clip_utils import is_single_image_media

    reader = mask_reader(transition_data, fallback_data)
    if "has_single_image" in reader:
        return bool(reader.get("has_single_image"))
    return bool(is_single_image_media(reader))


def reader_changed(transition_data, fallback_data=None):
    """True when the transition reader source changed."""
    if not isinstance(fallback_data, dict):
        return False
    new_reader = mask_reader(transition_data, fallback_data)
    old_reader = mask_reader(fallback_data, None)
    if not new_reader and not old_reader:
        return False
    for key in ("id", "path", "type", "has_single_image", "video_length", "duration"):
        if new_reader.get(key) != old_reader.get(key):
            return True
    return new_reader != old_reader


def set_mask_defaults(transition_data, fps_float, fallback_data=None):
    """Normalize timing/keyframes for static vs animated transition masks."""
    if not isinstance(transition_data, dict):
        return transition_data

    start = float(transition_data.get("start", 0.0) or 0.0)
    end = float(transition_data.get("end", start) or start)
    if end < start:
        end = start
    duration = max(0.0, end - start)

    if uses_static_mask(transition_data, fallback_data):
        transition_data["start"] = 0.0
        transition_data["end"] = duration
        brightness, contrast = default_keyframes(duration, 1.0, -1.0, DEFAULT_CONTRAST, fps_float)
    else:
        transition_data["start"] = start
        transition_data["end"] = end
        brightness, contrast = default_keyframes(duration, 0.0, 0.0, 0.0, fps_float)

    transition_data["duration"] = duration_of(transition_data)
    transition_data["brightness"] = brightness
    transition_data["contrast"] = contrast
    return transition_data


# ---------------------------------------------------------------------------
# Orientation (fade-in on a clip's left edge, fade-out on its right edge)
# ---------------------------------------------------------------------------

def infer_drop_side(transition_data, clips):
    """'left' or 'right': which edge of the most-overlapped clip the transition sits on."""
    if not isinstance(transition_data, dict):
        return None
    try:
        position = float(transition_data.get("position", 0.0))
        start = float(transition_data.get("start", 0.0))
        end = float(transition_data.get("end", 0.0))
    except (TypeError, ValueError):
        return None

    duration = max(0.0, end - start)
    if duration <= 0.0:
        return None

    tran_left = position
    tran_right = position + duration
    tran_mid = (tran_left + tran_right) / 2.0

    best_match = None
    for clip_data in clips:
        if not isinstance(clip_data, dict):
            continue
        try:
            clip_left = float(clip_data.get("position", 0.0))
            clip_duration = max(0.0, float(clip_data.get("end", 0.0)) - float(clip_data.get("start", 0.0)))
        except (TypeError, ValueError):
            continue
        if clip_duration <= 0.0:
            continue

        clip_right = clip_left + clip_duration
        overlap = min(tran_right, clip_right) - max(tran_left, clip_left)
        if overlap <= 0.0:
            continue

        clip_mid = (clip_left + clip_right) / 2.0
        side = "left" if tran_mid <= clip_mid else "right"
        edge_dist = abs(tran_mid - (clip_left if side == "left" else clip_right))
        score = (-overlap, edge_dist)
        if best_match is None or score < best_match[0]:
            best_match = (score, side)

    return best_match[1] if best_match else None


def auto_orient_keyframes(transition_data, clips):
    """Fade in on a left-edge drop; a right-edge drop keeps the default orientation.

    Only flips when the current direction is clearly inferable, so customized or
    non-monotonic curves are never rewritten.
    """
    target_side = infer_drop_side(transition_data, clips)
    if target_side not in ("left", "right"):
        return
    direction = direction_of(transition_data)
    if direction is None:
        return
    current_side = "left" if direction == "default" else "right"
    if current_side == target_side:
        return
    for prop in ("brightness", "contrast"):
        keyframe = transition_data.get(prop)
        if isinstance(keyframe, dict):
            reverse_keyframes(keyframe)


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------

def prepare_update(transition_data, old_data, fps_float, only_basic_props=True,
                   auto_direction=False, layer_clips=None):
    """Project data to save for a transition edit (the core of ``update_transition_data``).

    * only_basic_props on an existing transition (a drag or a trim): keep its
      brightness/contrast curves; for a static mask, rescale them to the new
      length and pin the end points to the transition edges.
    * a changed mask reader (Properties > mask image) resets the curves to the
      defaults for that kind of mask.
    * a brand-new transition with only_basic_props keeps just the basic keys.
    """
    data = transition_data
    old_data = old_data or {}
    old_duration = old_data.get("end", 0.0) - old_data.get("start", 0.0) if old_data else 0.0
    new_duration = data.get("end", 0.0) - data.get("start", 0.0)
    old_frames = round(old_duration * fps_float) if old_duration > 0 else 0
    new_frames = round(new_duration * fps_float) if new_duration > 0 else 0
    static_mask = uses_static_mask(data, old_data)

    if old_data and only_basic_props:
        if "brightness" in old_data:
            data["brightness"] = copy.deepcopy(old_data["brightness"])
        if "contrast" in old_data:
            data["contrast"] = copy.deepcopy(old_data["contrast"])
        if static_mask and old_frames and new_frames and old_frames != new_frames:
            scale = new_frames / old_frames
            for prop in ("brightness", "contrast"):
                if prop in data:
                    scale_keyframes(data[prop], scale)
        if static_mask and new_frames:
            anchor_endpoint_keyframes(data, new_frames)
    elif old_data and reader_changed(data, old_data):
        set_mask_defaults(data, fps_float, old_data)

    if auto_direction and static_mask:
        auto_orient_keyframes(data, layer_clips or [])

    if only_basic_props and not old_data:
        data = {
            "id": transition_data["id"],
            "layer": transition_data["layer"],
            "position": transition_data["position"],
            "start": transition_data["start"],
            "end": transition_data["end"],
            "brightness": transition_data.get("brightness", {}),
            "contrast": transition_data.get("contrast", {}),
        }
    return data


def new_mask_transition(transition_id, reader_json, position, layer, duration, fps_float,
                        title="Transition", resource=None, fade_audio=False):
    """A new Mask transition dict: the timeline's drop / auto-transition defaults."""
    brightness, contrast = default_keyframes(duration, DEFAULT_BRIGHTNESS[0], DEFAULT_BRIGHTNESS[1],
                                             DEFAULT_CONTRAST, fps_float)
    data = {
        "id": transition_id,
        "layer": layer,
        "title": title,
        "type": "Mask",
        "position": position,
        "start": 0,
        "end": duration,
        "brightness": brightness,
        "contrast": contrast,
        "reader": copy.deepcopy(reader_json),
        "replace_image": False,
    }
    if resource:
        data["resource"] = resource
    if fade_audio:
        data["fade_audio_hint"] = True
    return data


# ---------------------------------------------------------------------------
# Catalog (Transitions dock: common, extra and the user folder)
# ---------------------------------------------------------------------------

def transition_groups():
    """[(category, folder)] in the Transitions dock's order."""
    base = os.path.join(info.PATH, "transitions")
    groups = [("common", os.path.join(base, "common")), ("extra", os.path.join(base, "extra"))]
    user_dir = getattr(info, "TRANSITIONS_PATH", "")
    if user_dir:
        groups.append(("user", user_dir))
    return groups


def catalog(categories=("common", "extra", "user")):
    """Every transition image: [{name, key, filename, category, path}] (file system read)."""
    out = []
    for category, folder in transition_groups():
        if category not in categories or not os.path.isdir(folder):
            continue
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            log.debug("cannot list transitions in %s", folder, exc_info=True)
            continue
        for filename in names:
            if filename.startswith(".") or "thumbs.db" in filename.lower():
                continue
            stem, ext = os.path.splitext(filename)
            if ext.lower() not in TRANSITION_EXTENSIONS:
                continue
            out.append({
                "name": stem.replace("_", " ").capitalize(),
                "key": stem.lower(),
                "filename": filename,
                "category": category,
                "path": os.path.join(folder, filename),
            })
    return out


def _norm(text):
    return " ".join(str(text or "").strip().lower().replace("_", " ").replace("-", " ").split())


# What people call the common transitions.
ALIASES = {
    "crossfade": "fade", "cross fade": "fade", "dissolve": "fade", "cross dissolve": "fade",
    "fade": "fade", "mix": "fade",
    "wipe left": "wipe_right_to_left", "wipe right": "wipe_left_to_right",
    "wipe up": "wipe_bottom_to_top", "wipe down": "wipe_top_to_bottom",
    "circle": "circle_in_to_out", "iris": "circle_in_to_out", "iris open": "circle_in_to_out",
    "iris close": "circle_out_to_in", "circle in": "circle_out_to_in", "circle out": "circle_in_to_out",
}


def find_transition(name, entries=None):
    """(entry, candidates): the catalog entry for a name, alias, file name or path.

    Exact key/name match first, then aliases, then a unique substring match.
    entry is None when nothing or several match (candidates lists up to 12 names).
    """
    raw = str(name or "").strip()
    if not raw:
        return None, []
    if os.path.isabs(raw) and os.path.isfile(raw):
        stem = os.path.splitext(os.path.basename(raw))[0]
        return {"name": stem.replace("_", " ").capitalize(), "key": stem.lower(),
                "filename": os.path.basename(raw), "category": "file", "path": raw}, []
    entries = catalog() if entries is None else entries
    wanted = _norm(os.path.splitext(raw)[0] if raw.lower().endswith(TRANSITION_EXTENSIONS) else raw)
    alias = ALIASES.get(wanted)
    if alias:
        wanted = _norm(alias)
    exact = [e for e in entries if _norm(e["key"]) == wanted or _norm(e["name"]) == wanted]
    if exact:
        exact.sort(key=lambda e: ("common", "extra", "user").index(e["category"])
                   if e["category"] in ("common", "extra", "user") else 3)
        return exact[0], []
    partial_matches = [e for e in entries if wanted in _norm(e["key"])]
    if len(partial_matches) == 1:
        return partial_matches[0], []
    return None, [e["key"] for e in partial_matches[:12]]
