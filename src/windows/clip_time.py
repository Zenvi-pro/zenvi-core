"""
 @file
 @brief Utility functions for constraining clip timing to its source media
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2018 OpenShot Studios, LLC
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

from classes.app import get_app


def clamp_timing_to_media(clip_data, existing_clip=None):
    """Clamp timing-related clip values to the bounds of its reader.

    If less than two time keyframes exist, reset the clip duration to the
    reader's video length and ensure the end trim does not exceed that
    duration. When multiple time keyframes are present, clamp all time values
    to the reader's frame range but leave the clip's duration unchanged. In
    all cases, start/end trims remain within the available duration.

    :param dict clip_data: The clip data to modify.
    :param existing_clip: Optional clip instance to use for missing reader info.
    :return: The mutated ``clip_data`` dict.
    :rtype: dict
    """
    reader = clip_data.get("reader")
    if not reader and existing_clip and getattr(existing_clip, "data", None):
        reader = existing_clip.data.get("reader")
    if not reader:
        return clip_data

    # Populate missing timing fields from the existing clip
    if existing_clip and getattr(existing_clip, "data", None):
        for key in ("start", "end", "duration"):
            if key not in clip_data and key in existing_clip.data:
                clip_data[key] = existing_clip.data.get(key)

    # Determine the FPS of the reader's media (falling back to project FPS)
    project_fps = get_app().project.get("fps")
    reader_fps = reader.get("fps") or project_fps
    reader_fps_float = float(reader_fps.get("num", 0)) / float(reader_fps.get("den", 1))

    video_length = float(reader.get("video_length", 0))
    if reader.get("has_single_image"):
        max_duration = float(reader.get("duration", 0))
    else:
        # Convert reader frames to seconds using the reader's own FPS
        max_duration = video_length / reader_fps_float if reader_fps_float else 0

    time_data = clip_data.get("time")
    points = time_data.get("Points") if isinstance(time_data, dict) else None

    if points and len(points) > 1:
        # Clamp time keyframes within reader bounds
        for point in points:
            co = point.get("co", {})
            x = co.get("X")
            y = co.get("Y")
            if x is not None:
                co["X"] = round(x)
            if y is None:
                continue
            y = round(y)
            if y < 1:
                co["Y"] = 1
            elif y > video_length:
                co["Y"] = video_length
            else:
                co["Y"] = y
        # Leave duration/end as-is for multi-point curves
    else:
        # No time curve or a single keyframe: reset to full reader duration
        clip_data["duration"] = max_duration

    # Constrain end and start trims within the clip duration
    clip_data["end"] = min(clip_data.get("end", clip_data.get("duration", 0)), clip_data.get("duration", 0))
    clip_data["start"] = max(0.0, min(clip_data.get("start", 0.0), clip_data["end"]))

    return clip_data
