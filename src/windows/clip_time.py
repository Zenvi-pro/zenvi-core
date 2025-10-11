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

import logging

from classes.app import get_app
from classes.frame_utils import project_fps_fraction, video_length_to_project_frames


logger = logging.getLogger(__name__)


def _reader_from_clip(clip_data, existing_clip):
    """Return the reader dict from clip data or the existing clip."""
    reader = clip_data.get("reader")
    if reader or not existing_clip or not getattr(existing_clip, "data", None):
        return reader
    return existing_clip.data.get("reader")


def _merge_timing_from_existing(clip_data, existing_clip):
    """Copy start/end/duration from the existing clip when missing."""
    if not existing_clip or not getattr(existing_clip, "data", None):
        return
    for key in ("start", "end", "duration"):
        if key not in clip_data and key in existing_clip.data:
            clip_data[key] = existing_clip.data.get(key)


def _project_fps_float():
    """Return the project frame rate as a float."""
    fps_fraction = project_fps_fraction()
    try:
        return float(fps_fraction)
    except (TypeError, ValueError):
        return 30.0


def _clip_id_from_data(clip_data, existing_clip):
    """Return the clip ID from the provided data or existing clip."""
    if isinstance(clip_data, dict):
        clip_id = clip_data.get("id")
        if clip_id:
            return clip_id
    if existing_clip and getattr(existing_clip, "data", None):
        return existing_clip.data.get("id")
    return None


def _timeline_clip_instance(clip_id):
    """Look up a live timeline clip instance by its ID."""
    if not clip_id:
        return None
    try:
        timeline_sync = getattr(get_app().window, "timeline_sync", None)
        timeline = getattr(timeline_sync, "timeline", None)
        return timeline.GetClip(clip_id) if timeline else None
    except Exception as exc:
        logger.debug(
            "Unable to locate clip %s on timeline: %s", clip_id, exc, exc_info=True
        )
        return None


def _clip_instance_from_data(clip_data, existing_clip):
    """Return the live clip instance for the given data references."""
    clip_id = _clip_id_from_data(clip_data, existing_clip)
    return _timeline_clip_instance(clip_id)


def _parse_int_or_none(value):
    """Convert value to a rounded int or None if conversion fails."""
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _reader_media_bounds(reader):
    """Return duration seconds and frame length derived from reader data."""
    if not isinstance(reader, dict):
        return None, None

    duration_sec = _float_or_none(reader.get("duration"))

    proj_fps_frac = project_fps_fraction()
    proj_fps_f = float(proj_fps_frac) if proj_fps_frac else None

    total_frames = video_length_to_project_frames(reader, project_fps=proj_fps_frac)

    if duration_sec is None and total_frames and proj_fps_f:
        duration_sec = total_frames / proj_fps_f
    if total_frames is None and duration_sec and proj_fps_f:
        total_frames = int(round(duration_sec * proj_fps_f))

    if duration_sec is not None and duration_sec < 0.0:
        duration_sec = None

    if total_frames is not None and total_frames <= 0:
        total_frames = None

    return duration_sec, total_frames


def _time_curve_length_frames(clip_data, existing_clip):
    """Return the max frame value from the clip's time curve."""
    clip_obj = _clip_instance_from_data(clip_data, existing_clip)
    if clip_obj and getattr(clip_obj, "time", None):
        try:
            length = clip_obj.time.GetLength()
        except Exception as exc:
            logger.debug("Unable to query clip time length: %s", exc, exc_info=True)
        else:
            if length:
                try:
                    return max(1, int(round(float(length))))
                except (TypeError, ValueError):
                    pass

    points = _time_points(clip_data)
    if not isinstance(points, list):
        return None

    max_x = 0
    for point in points:
        co = point.get("co")
        if not isinstance(co, dict):
            continue
        x_val = co.get("X")
        if x_val is None:
            continue
        try:
            max_x = max(max_x, int(round(float(x_val))))
        except (TypeError, ValueError):
            continue

    return max_x or None


def _clamp_basic_clip_timing(clip_data, max_duration_sec):
    """Clamp start/end/duration for clips without time remapping."""
    limit_sec = max_duration_sec if max_duration_sec is not None else None

    start_sec = _float_or_none(clip_data.get("start"))
    if start_sec is None:
        start_sec = 0.0
    if start_sec < 0.0:
        start_sec = 0.0
    if limit_sec is not None and start_sec > limit_sec:
        start_sec = limit_sec

    end_sec = _float_or_none(clip_data.get("end"))
    duration_sec = _float_or_none(clip_data.get("duration"))

    if end_sec is None:
        if duration_sec is not None:
            end_sec = start_sec + duration_sec
        elif limit_sec is not None:
            end_sec = limit_sec
        else:
            end_sec = start_sec

    if limit_sec is not None and end_sec > limit_sec:
        end_sec = limit_sec
    if end_sec < start_sec:
        start_sec = end_sec

    clip_data["start"] = start_sec
    clip_data["end"] = end_sec
    clip_data["duration"] = max(0.0, end_sec - start_sec)


def _is_media_type_image(media_type):
    """Return True when the supplied media type represents an image."""
    if isinstance(media_type, str):
        return media_type.lower() == "image"
    return False


def _dict_has_single_image_flag(data):
    """Return True if dict metadata flags the clip as a single image."""
    if not isinstance(data, dict):
        return False
    if data.get("has_single_image"):
        return True
    return _is_media_type_image(data.get("media_type"))


def _clip_has_single_image(reader, clip_data, existing_clip):
    """Return True when any source indicates a single-image clip."""
    if _dict_has_single_image_flag(reader):
        return True
    if _dict_has_single_image_flag(clip_data):
        return True
    if isinstance(clip_data, dict) and _dict_has_single_image_flag(clip_data.get("reader")):
        return True
    if existing_clip and getattr(existing_clip, "data", None):
        existing_data = existing_clip.data
        if _dict_has_single_image_flag(existing_data):
            return True
        if _dict_has_single_image_flag(existing_data.get("reader")):
            return True
    return False


def _float_or_none(value):
    """Convert value to float or None when conversion fails."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_single_image_timing(clip_data):
    """Ensure single-image timing fields remain consistent."""
    start_sec = _float_or_none(clip_data.get("start"))
    if start_sec is None:
        start_sec = 0.0

    end_sec = _float_or_none(clip_data.get("end"))
    duration_sec = _float_or_none(clip_data.get("duration"))

    if duration_sec is None and end_sec is not None:
        duration_sec = end_sec - start_sec
    if end_sec is None and duration_sec is not None:
        end_sec = start_sec + duration_sec
    if end_sec is None:
        end_sec = start_sec
    if duration_sec is None:
        duration_sec = end_sec - start_sec

    if start_sec < 0.0:
        start_sec = 0.0
    if end_sec < start_sec:
        end_sec = start_sec

    duration_sec = end_sec - start_sec

    clip_data["start"] = start_sec
    clip_data["end"] = end_sec
    clip_data["duration"] = duration_sec


def _time_points(clip_data):
    """Return the list of time keyframe points from clip data."""
    time_data = clip_data.get("time")
    return time_data.get("Points") if isinstance(time_data, dict) else None


def _clamp_time_points(points, max_y_project):
    """Clamp keyframe coordinates to positive axes and given Y limit."""
    if not isinstance(points, list) or not max_y_project:
        return

    for point in points:
        co = point.get("co", {})
        if "X" in co and co["X"] is not None:
            co["X"] = int(round(co["X"]))
        if "Y" in co and co["Y"] is not None:
            y = int(round(co["Y"]))
            if y < 1:
                co["Y"] = 1
            elif y > max_y_project:
                co["Y"] = max_y_project
            else:
                co["Y"] = y


def _clamp_multi_time_start(clip_data, max_duration_sec):
    """Clamp clip start for multi-point time curves to available range."""
    start_sec = _float_or_none(clip_data.get("start"))
    if start_sec is None:
        start_sec = 0.0
    if start_sec < 0.0:
        start_sec = 0.0
    if max_duration_sec is not None and start_sec > max_duration_sec:
        start_sec = max_duration_sec
    clip_data["start"] = start_sec


def clamp_timing_to_media(clip_data, existing_clip=None):
    """Keep clip timing values inside the available media or time-curve bounds."""

    reader = _reader_from_clip(clip_data, existing_clip)

    _merge_timing_from_existing(clip_data, existing_clip)

    if _clip_has_single_image(reader, clip_data, existing_clip):
        _normalize_single_image_timing(clip_data)
        return clip_data

    points = _time_points(clip_data)
    multi_point_time = isinstance(points, list) and len(points) > 1

    reader_duration, reader_frames = _reader_media_bounds(reader)

    if multi_point_time:
        max_frames = _time_curve_length_frames(clip_data, existing_clip) or reader_frames
        if reader_frames:
            _clamp_time_points(points, reader_frames)

        proj_fps_f = _project_fps_float()
        max_duration_sec = None
        if max_frames and proj_fps_f:
            max_duration_sec = max_frames / proj_fps_f
        elif reader_duration is not None:
            max_duration_sec = reader_duration

        _clamp_multi_time_start(clip_data, max_duration_sec)
        return clip_data

    _clamp_basic_clip_timing(clip_data, reader_duration)
    if reader_frames:
        _clamp_time_points(points, reader_frames)

    return clip_data


def clip_time_bounds(clip_data, existing_clip=None):
    """Return max duration and frame count allowed for the clip."""
    reader = _reader_from_clip(clip_data, existing_clip) or {}

    if _clip_has_single_image(reader, clip_data, existing_clip):
        duration = _float_or_none((clip_data or {}).get("duration"))
        if duration is None:
            duration = _float_or_none(reader.get("duration")) or 0.0
        proj_fps_f = _project_fps_float()
        max_frames = max(1, int(round(duration * proj_fps_f))) if duration and proj_fps_f else 1
        return duration if duration else 0.0, max_frames

    points = _time_points(clip_data)
    multi_point_time = isinstance(points, list) and len(points) > 1

    proj_fps_f = _project_fps_float()
    reader_duration, reader_frames = _reader_media_bounds(reader)

    if multi_point_time:
        max_frames = _time_curve_length_frames(clip_data, existing_clip) or reader_frames or 1
        if proj_fps_f:
            max_duration = max_frames / proj_fps_f
        elif reader_duration is not None:
            max_duration = reader_duration
        else:
            max_duration = 0.0
        return max_duration, max_frames

    max_frames = reader_frames or 1
    if reader_duration is not None:
        max_duration = reader_duration
    elif proj_fps_f and max_frames:
        max_duration = max_frames / proj_fps_f
    else:
        max_duration = 0.0

    return max_duration, max_frames
