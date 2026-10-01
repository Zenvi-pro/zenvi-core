"""
 @file
 @brief Qt-free keyframe rules shared by the clip menus, the Properties dock and the agent tools
 @author Zenvi

 @section LICENSE

 This file is part of Zenvi, a fork of OpenShot Video Editor, and is released
 under the GNU General Public License version 3 or later.

 One definition of each rule the editor applies to clip keyframes:

 * which keys each Clip menu > Copy > Keyframes item copies,
 * how far the Properties dock lets scale and shear go,
 * what a property falls back to when its last keyframe is removed,
 * and how libopenshot evaluates a keyframe curve at a frame, so reports and
   relative edits see exactly the value playback will show.
 """

from __future__ import annotations

from typing import Any, Optional

# OpenShot interpolation codes (openshot.BEZIER / LINEAR / CONSTANT).
BEZIER, LINEAR, CONSTANT = 0, 1, 2

# Clip menu > Copy > Keyframes groups, in menu order. Scale, Rotation and
# Location carry gravity with them because those values are relative to it.
COPY_KEYFRAME_GROUPS = {
    "all": ("alpha", "gravity", "scale_x", "scale_y", "shear_x", "shear_y", "rotation",
            "location_x", "location_y", "time", "volume"),
    "alpha": ("alpha",),
    "scale": ("gravity", "scale_x", "scale_y"),
    "shear": ("shear_x", "shear_y"),
    "rotation": ("gravity", "rotation"),
    "location": ("gravity", "location_x", "location_y"),
    "time": ("time",),
    "volume": ("volume",),
}

# What the Properties dock writes when the last point of a curve is removed.
_EMPTY_CURVE_DEFAULTS = {
    "alpha": 1.0, "scale_x": 1.0, "scale_y": 1.0, "time": 1.0, "volume": 1.0,
    "origin_x": 0.5, "origin_y": 0.5,
    "location_x": 0.0, "location_y": 0.0, "rotation": 0.0, "shear_x": 0.0, "shear_y": 0.0,
    "has_audio": -1.0, "has_video": -1.0, "channel_filter": -1.0, "channel_mapping": -1.0,
}
_WAVE_COLOR_DEFAULTS = {"red": 0.0, "green": 123.0, "blue": 255.0, "alpha": 255.0}


def default_keyframe_value(property_key: str, channel: Optional[str] = None) -> Optional[float]:
    """Value a clip property falls back to once its last keyframe is removed (None = no default).

    ``channel`` names the red/green/blue/alpha curve of a color property.
    """
    if property_key == "wave_color":
        return _WAVE_COLOR_DEFAULTS.get(str(channel or ""))
    if channel:
        return None
    return _EMPTY_CURVE_DEFAULTS.get(property_key)


def max_transform_multiple(project_width: Any, project_height: Any, is_svg: bool = False) -> float:
    """Largest |scale_x|, |scale_y|, |shear_x|, |shear_y| the Properties dock accepts.

    50x (15x for SVG titles), tightened for large project sizes so a huge
    value cannot make libopenshot allocate an enormous frame.
    """
    max_multiple = 15 if is_svg else 50
    try:
        width, height = float(project_width or 0), float(project_height or 0)
    except (TypeError, ValueError):
        width = height = 0.0
    if width > 0 and height > 0:
        max_multiple = round((2000 * max_multiple) / max(width, height))
    return max_multiple


# ---------------------------------------------------------------------------
# libopenshot keyframe evaluation (Keyframe::GetValue / InterpolateBetween)
# ---------------------------------------------------------------------------

def _xy(point: dict) -> tuple:
    co = point.get("co") or {}
    return float(co.get("X", 0.0)), float(co.get("Y", 0.0))


def _handle(point: dict, side: str, default: tuple) -> tuple:
    h = point.get(side)
    if not isinstance(h, dict):
        return default
    return float(h.get("X", default[0])), float(h.get("Y", default[1]))


def _bezier(left: dict, right: dict, target: float, allowed_error: float = 0.01) -> float:
    lx, ly = _xy(left)
    rx, ry = _xy(right)
    dx, dy = rx - lx, ry - ly
    hr = _handle(left, "handle_right", (0.5, 0.0))
    hl = _handle(right, "handle_left", (0.5, 1.0))
    p0 = (lx, ly)
    p1 = (lx + hr[0] * dx, ly + hr[1] * dy)
    p2 = (lx + hl[0] * dx, ly + hl[1] * dy)
    p3 = (rx, ry)
    t, step = 0.5, 0.25
    y = ly
    for _ in range(64):
        mt = 1.0 - t
        b0, b1, b2, b3 = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
        x = p0[0] * b0 + p1[0] * b1 + p2[0] * b2 + p3[0] * b3
        y = p0[1] * b0 + p1[1] * b1 + p2[1] * b2 + p3[1] * b3
        if abs(target - x) < allowed_error:
            return y
        t = t - step if x > target else t + step
        step /= 2
    return y


def keyframe_value(curve: Any, frame: float, default: float = 0.0) -> float:
    """Value of an OpenShot keyframe dict at a 1-based clip frame, exactly as libopenshot plays it.

    The interpolation of a segment is the RIGHT point's; bezier segments use
    the left point's handle_right and the right point's handle_left (both
    relative to the segment). A plain number is a static value.
    """
    if isinstance(curve, (int, float)) and not isinstance(curve, bool):
        return float(curve)
    points = curve.get("Points") if isinstance(curve, dict) else None
    points = [p for p in (points or []) if isinstance(p, dict) and isinstance(p.get("co"), dict)]
    if not points:
        return float(default)
    # libopenshot's AddPoint overwrites a point at the same X: the one listed last wins.
    by_x = {}
    for p in points:
        by_x[_xy(p)[0]] = p
    points = [by_x[x] for x in sorted(by_x)]
    target = float(frame)
    # lower_bound: first point with X >= target
    index = next((i for i, p in enumerate(points) if _xy(p)[0] >= target), len(points))
    if index == len(points):
        return _xy(points[-1])[1]
    if index == 0 or _xy(points[index])[0] == target:
        return _xy(points[index])[1]
    left, right = points[index - 1], points[index]
    lx, ly = _xy(left)
    rx, ry = _xy(right)
    interpolation = int(right.get("interpolation", BEZIER))
    if interpolation == CONSTANT:
        return ly
    if interpolation == LINEAR or rx == lx:
        return ly + (ry - ly) / (rx - lx) * (target - lx) if rx != lx else ry
    return _bezier(left, right, target)


def curve_plateau(curve: Any, first_frame: float, last_frame: float, default: float = 1.0) -> float:
    """The level a clip otherwise plays a curve at: its highest value across the visible frames.

    Fades aim for this level, so re-applying a fade over an existing one (or
    over ducked volume) restores the clip's normal level instead of a value
    read part-way through the old ramp.
    """
    values = [keyframe_value(curve, first_frame, default), keyframe_value(curve, last_frame, default)]
    points = curve.get("Points") if isinstance(curve, dict) else None
    for p in points or []:
        if isinstance(p, dict) and isinstance(p.get("co"), dict):
            x, y = _xy(p)
            if first_frame <= x <= last_frame:
                values.append(y)
    return max(values)
