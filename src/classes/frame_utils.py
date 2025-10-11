"""
 @file
 @brief Utilities for working with frame counts across differing frame rates.
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

from __future__ import annotations

from fractions import Fraction
from typing import Any, Mapping, Optional

from classes.app import get_app


def _coerce_mapping(candidate: Any) -> Mapping[str, Any]:
    """Return a dict-like view of clip/file metadata."""
    if isinstance(candidate, Mapping):
        return candidate
    data = getattr(candidate, "data", None)
    if isinstance(data, Mapping):
        return data
    return {}


def _fps_fraction(fps_value: Any) -> Optional[Fraction]:
    """Convert fps metadata to a Fraction, when possible."""
    if fps_value is None:
        return None
    if isinstance(fps_value, Fraction):
        return fps_value
    if isinstance(fps_value, (int, float)):
        if fps_value > 0:
            return Fraction(fps_value).limit_denominator(1000000)
        return None
    if isinstance(fps_value, Mapping):
        fps_num = fps_value.get("num")
        fps_den = fps_value.get("den")
    else:
        fps_num = getattr(fps_value, "num", None)
        fps_den = getattr(fps_value, "den", None)
    try:
        fps_num = int(fps_num)
        fps_den = int(fps_den)
    except (TypeError, ValueError):
        fps_num = fps_den = None
    if fps_num and fps_den:
        try:
            return Fraction(fps_num, fps_den)
        except ZeroDivisionError:
            return None
    to_float = getattr(fps_value, "ToFloat", None)
    if callable(to_float):
        fps_float = to_float()
        if fps_float and fps_float > 0:
            return Fraction(fps_float).limit_denominator(1000000)
    return None


def _coerce_int(value: Any) -> Optional[int]:
    """Convert the supplied value to a rounded int, if feasible."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return int(round(number))


def _coerce_float(value: Any) -> Optional[float]:
    """Convert the supplied value to a positive float."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def project_fps_fraction() -> Optional[Fraction]:
    """Return the current project's FPS as a Fraction."""
    app = get_app()
    project = getattr(app, "project", None) if app else None
    fps_meta = None
    if hasattr(project, "get"):
        try:
            fps_meta = project.get("fps")
        except TypeError:
            fps_meta = None
    elif isinstance(project, Mapping):
        fps_meta = project.get("fps")
    return _fps_fraction(fps_meta) or Fraction(30, 1)


def video_length_to_project_frames(
    media: Any = None,
    *,
    video_length: Any = None,
    fps: Any = None,
    duration: Any = None,
    project_fps: Any = None,
) -> Optional[int]:
    """Return the number of project frames required to play the supplied media.

    Arguments may be provided either directly or via a clip/file metadata dict.
    """
    metadata = _coerce_mapping(media)
    if video_length is None:
        video_length = metadata.get("video_length")
    frames = _coerce_int(video_length)

    if fps is None:
        fps = metadata.get("fps")
    source_fps = _fps_fraction(fps)

    if duration is None:
        duration = metadata.get("duration")
    duration_value = _coerce_float(duration)

    if frames is None and duration_value is not None and source_fps:
        frames = int(round(duration_value * float(source_fps)))

    if frames is None:
        return None

    project_fraction = _fps_fraction(project_fps) or project_fps_fraction()

    if not source_fps or not project_fraction:
        return max(frames, 1)

    scaled = Fraction(frames) * project_fraction / source_fps
    try:
        scaled_value = int(round(float(scaled)))
    except (TypeError, ValueError):
        return max(frames, 1)
    return max(scaled_value, 1)
