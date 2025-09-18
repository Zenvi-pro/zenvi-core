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


logger = logging.getLogger(__name__)


def _reader_from_clip(clip_data, existing_clip):
    reader = clip_data.get("reader")
    if reader or not existing_clip or not getattr(existing_clip, "data", None):
        return reader
    return existing_clip.data.get("reader")


def _merge_timing_from_existing(clip_data, existing_clip):
    if not existing_clip or not getattr(existing_clip, "data", None):
        return
    for key in ("start", "end", "duration"):
        if key not in clip_data and key in existing_clip.data:
            clip_data[key] = existing_clip.data.get(key)


def _project_fps_float():
    proj_fps = get_app().project.get("fps") or {"num": 30, "den": 1}
    num = float(proj_fps.get("num", 30))
    den = float(proj_fps.get("den", 1)) or 1.0
    return num / den


def _reader_src_fps(reader):
    if not isinstance(reader, dict):
        return 0.0
    vf = reader.get("video_fps") or reader.get("fps") or {}
    num = (
        vf.get("num")
        or vf.get("Num")
        or reader.get("video_fps_num")
        or reader.get("fps_num")
    )
    den = (
        vf.get("den")
        or vf.get("Den")
        or reader.get("video_fps_den")
        or reader.get("fps_den")
    )
    try:
        return float(num) / float(den) if (num and den) else 0.0
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        logger.debug("Invalid reader FPS %s/%s: %s", num, den, exc)
        return 0.0


def _clip_src_fps_from_libopenshot(clip_id):
    if not clip_id:
        return 0.0
    try:
        timeline_sync = getattr(get_app().window, "timeline_sync", None)
        timeline = getattr(timeline_sync, "timeline", None)
        clip = timeline.GetClip(clip_id) if timeline else None
    except Exception as exc:
        logger.debug("Unable to locate clip %s on timeline: %s", clip_id, exc, exc_info=True)
        return 0.0

    if not clip:
        return 0.0

    try:
        info = clip.Reader().info
    except Exception as exc:
        logger.debug("Unable to query reader info for clip %s: %s", clip_id, exc, exc_info=True)
        return 0.0

    for attr in ("video_fps", "fps"):
        fps_obj = getattr(info, attr, None)
        if fps_obj and getattr(fps_obj, "den", 0):
            return float(fps_obj.num) / float(fps_obj.den)
    return 0.0


def _source_fps(reader, clip_data, existing_clip, proj_fps_f):
    src_fps = _reader_src_fps(reader)
    if src_fps > 0:
        return src_fps
    clip_id = clip_data.get("id")
    if not clip_id and existing_clip and getattr(existing_clip, "data", None):
        clip_id = existing_clip.data.get("id")
    src_fps = _clip_src_fps_from_libopenshot(clip_id)
    return src_fps if src_fps > 0 else proj_fps_f


def _reader_video_length(reader):
    try:
        return int(float(reader.get("video_length", 0)))
    except (TypeError, ValueError) as exc:
        logger.debug("Invalid reader video_length %s: %s", reader.get("video_length"), exc)
        return 0


def _is_media_type_image(media_type):
    if isinstance(media_type, str):
        return media_type.lower() == "image"
    return False


def _dict_has_single_image_flag(data):
    if not isinstance(data, dict):
        return False
    if data.get("has_single_image"):
        return True
    return _is_media_type_image(data.get("media_type"))


def _clip_has_single_image(reader, clip_data, existing_clip):
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
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_single_image_timing(clip_data):
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


def _max_duration_seconds(reader, video_len_src, src_fps_f):
    if reader.get("has_single_image"):
        return float(reader.get("duration", 0))
    return (video_len_src / src_fps_f) if src_fps_f else 0.0


def _max_project_frames(reader, video_len_src, src_fps_f, proj_fps_f):
    if reader.get("has_single_image"):
        duration = float(reader.get("duration", 0))
        return max(1, int(round(duration * proj_fps_f)))
    scale = (proj_fps_f / src_fps_f) if src_fps_f else 1.0
    return max(1, int(video_len_src * scale))


def _time_points(clip_data):
    time_data = clip_data.get("time")
    return time_data.get("Points") if isinstance(time_data, dict) else None


def _clamp_time_points(points, max_y_project):
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
    start_sec = float(clip_data.get("start", 0.0))
    if start_sec < 0.0:
        start_sec = 0.0
    if start_sec > max_duration_sec:
        start_sec = max_duration_sec
    clip_data["start"] = start_sec


def _clamp_single_time_values(clip_data, max_duration_sec):
    clip_data["duration"] = float(max_duration_sec)
    start_sec = float(clip_data.get("start", 0.0))
    end_sec = float(clip_data.get("end", start_sec))
    if end_sec > max_duration_sec:
        end_sec = max_duration_sec
    if start_sec < 0.0:
        start_sec = 0.0
    if start_sec > end_sec:
        start_sec = end_sec
    clip_data["start"] = start_sec
    clip_data["end"] = end_sec
    clip_data["duration"] = float(end_sec - start_sec)


def clamp_timing_to_media(clip_data, existing_clip=None):
    """Clamp timing-related clip values to the bounds of its reader.

    Clamping rules
    --------------
    • Time curve Y values are clamped in PROJECT-FRAME space.
      max_y_project = reader.video_length (SOURCE frames) * (project_fps / reader_fps)
    • For multi-point time curves, only clamp point coords (do not rescale or stretch the curve).
      For zero/one time point, reset duration to the reader’s full duration (in seconds).
    • Start/End trims are clamped to the available media duration (seconds).
    """

    reader = _reader_from_clip(clip_data, existing_clip)

    _merge_timing_from_existing(clip_data, existing_clip)

    if _clip_has_single_image(reader, clip_data, existing_clip):
        _normalize_single_image_timing(clip_data)
        return clip_data

    if not reader:
        return clip_data

    proj_fps_f = _project_fps_float()
    src_fps_f = _source_fps(reader, clip_data, existing_clip, proj_fps_f)

    video_len_src = _reader_video_length(reader)
    max_duration_sec = _max_duration_seconds(reader, video_len_src, src_fps_f)
    max_y_project = _max_project_frames(reader, video_len_src, src_fps_f, proj_fps_f)

    points = _time_points(clip_data)
    if isinstance(points, list) and len(points) > 1:
        _clamp_time_points(points, max_y_project)
        _clamp_multi_time_start(clip_data, max_duration_sec)
        return clip_data

    _clamp_single_time_values(clip_data, max_duration_sec)
    return clip_data
