"""
 @file
 @brief This file contains re-time keyframe logic (for Time->Fast/Slow menu, Timing mode on timeline)
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2025 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

import json
import openshot
from classes.app import get_app


def retime_clip(clip, new_end, new_position=None, direction=1):
    """Retimes a clip and uniformly rescales ALL keyframes' X (including 'time').
       - X and Y are in PROJECT frames.
       - Flip 'time'.Y for reverse.
    """
    proj_fps = get_app().project.get("fps") or {"num": 30, "den": 1}
    pfps = float(proj_fps.get("num", 30)) / float(proj_fps.get("den", 1))

    # Seconds domain
    start_s = float(clip.data["start"])
    old_end_s = float(clip.data["end"])
    req_end_s = float(new_end)
    new_dur_s = req_end_s - start_s
    if new_dur_s <= 0:
        return False

    # Frame snapping and derived X domain
    new_dur_frames = max(1, int(round(new_dur_s * pfps)))
    new_dur_s = new_dur_frames / pfps
    new_end_s = start_s + new_dur_s

    start_x = int(round(start_s * pfps)) + 1
    old_end_x = int(round(old_end_s * pfps))
    new_end_x = start_x + new_dur_frames

    old_len = max(1, old_end_x - start_x)
    scale = float(new_end_x - start_x) / float(old_len)

    # Helpers to iterate and scale all keyframe lists
    def _scale_points_x(points, clamp_to_new=True):
        if not isinstance(points, list):
            return
        for p in points:
            co = p.get("co", {})
            x = co.get("X")
            if x is None:
                continue
            if x >= start_x:
                dx = x - start_x
                nx = start_x + dx * scale
                nx = int(round(nx))
                if clamp_to_new:
                    if nx < start_x:
                        nx = start_x
                    elif nx > new_end_x:
                        nx = new_end_x
                co["X"] = nx

    def _visit_all_keyframe_dicts(clip_dict):
        for k, v in clip_dict.items():
            if isinstance(v, dict) and isinstance(v.get("Points"), list):
                yield ("clip", k, v["Points"])
        for obj in (clip_dict.get("objects") or {}).values():
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, dict) and isinstance(v.get("Points"), list):
                        yield ("object", k, v["Points"])
        for eff in clip_dict.get("effects", []) or []:
            if isinstance(eff, dict):
                for k, v in eff.items():
                    if isinstance(v, dict) and isinstance(v.get("Points"), list):
                        yield ("effect", k, v["Points"])

    for _scope, key, points in _visit_all_keyframe_dicts(clip.data):
        _scale_points_x(points, clamp_to_new=True)

    # Build or update the time curve orientation
    time_points = None
    if isinstance(clip.data.get("time"), dict):
        time_points = clip.data["time"].get("Points")

    if not isinstance(time_points, list) or len(time_points) < 2:
        y0 = start_x
        y1 = int(round(old_end_s * pfps))
        if direction == -1:
            y0, y1 = y1, y0
        p0 = openshot.Point(start_x, y0, openshot.LINEAR)
        p1 = openshot.Point(new_end_x, y1, openshot.LINEAR)
        clip.data["time"] = {"Points": [json.loads(p0.Json()), json.loads(p1.Json())]}
        time_points = clip.data["time"]["Points"]
    else:
        if direction == -1 and len(time_points) >= 2:
            y_start = int(round(time_points[0]["co"]["Y"]))
            y_end = int(round(time_points[-1]["co"]["Y"]))
            for p in time_points:
                co = p.get("co", {})
                y = co.get("Y")
                if y is None:
                    continue
                co["Y"] = int(round(y_start + y_end - y))

    if time_points and len(time_points) >= 1:
        time_points.sort(key=lambda p: int(round(p.get("co", {}).get("X", 0))))
        time_points[-1]["co"]["X"] = int(new_end_x)
        if time_points[0]["co"]["X"] < start_x:
            time_points[0]["co"]["X"] = int(start_x)

    clip.data["duration"] = float(new_dur_s)
    clip.data["end"] = float(new_end_s)
    if new_position is not None:
        clip.data["position"] = float(int(round(float(new_position) * pfps)) / pfps)

    return True
