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

    Clamping rules
    --------------
    • Time curve Y values are clamped in PROJECT-FRAME space.
      max_y_project = reader.video_length (SOURCE frames) * (project_fps / reader_fps)
    • For multi-point time curves, only clamp point coords (do not rescale or stretch the curve).
      For zero/one time point, reset duration to the reader’s full duration (in seconds).
    • Start/End trims are clamped to the available media duration (seconds).
    """
    reader = clip_data.get("reader")
    if not reader and existing_clip and getattr(existing_clip, "data", None):
        reader = existing_clip.data.get("reader")
    if not reader:
        return clip_data

    # If we have an existing clip, fill any missing timing fields from it
    if existing_clip and getattr(existing_clip, "data", None):
        for key in ("start", "end", "duration"):
            if key not in clip_data and key in existing_clip.data:
                clip_data[key] = existing_clip.data.get(key)

    # Project FPS (used for X and clamped Y domain)
    proj_fps = get_app().project.get("fps") or {"num": 30, "den": 1}
    proj_fps_f = float(proj_fps.get("num", 30)) / float(proj_fps.get("den", 1))

    # Reader FPS (SOURCE fps). Parse robustly; if missing, try live libopenshot reader.
    def _parse_src_fps_from_reader(r):
        vf = (r.get("video_fps") or r.get("fps") or {}) if isinstance(r, dict) else {}
        num = vf.get("num") or vf.get("Num") or r.get("video_fps_num") or r.get("fps_num")
        den = vf.get("den") or vf.get("Den") or r.get("video_fps_den") or r.get("fps_den")
        try:
            return float(num) / float(den) if (num and den) else 0.0
        except Exception:
            return 0.0

    src_fps_f = _parse_src_fps_from_reader(reader)

    if not src_fps_f or src_fps_f <= 0:
        # Fallback to the live libopenshot clip (more authoritative)
        clip_id = clip_data.get("id") or (existing_clip and existing_clip.data.get("id"))
        try:
            c = get_app().window.timeline_sync.timeline.GetClip(clip_id) if clip_id else None
        except Exception:
            c = None
        if c:
            try:
                info = c.Reader().info
                # Prefer video_fps if present, else fps
                if hasattr(info, "video_fps") and getattr(info.video_fps, "den", 0):
                    src_fps_f = float(info.video_fps.num) / float(info.video_fps.den)
                elif hasattr(info, "fps") and getattr(info.fps, "den", 0):
                    src_fps_f = float(info.fps.num) / float(info.fps.den)
            except Exception:
                pass

    # As a last resort, do NOT silently fall back to project fps unless there is truly no source fps.
    if not src_fps_f or src_fps_f <= 0:
        src_fps_f = proj_fps_f  # unavoidable fallback

    # Reader length in SOURCE frames (ground truth frame count)
    try:
        video_len_src = int(float(reader.get("video_length", 0)))
    except Exception:
        video_len_src = 0

    # Max duration in seconds (for start/end trims)
    if reader.get("has_single_image"):
        max_duration_sec = float(reader.get("duration", 0))
    else:
        max_duration_sec = (video_len_src / src_fps_f) if src_fps_f else 0.0

    # Max Y bound in PROJECT FRAMES (scale source frames into project domain)
    if reader.get("has_single_image"):
        max_y_project = max(1, int(round(float(reader.get("duration", 0)) * proj_fps_f)))
    else:
        scale = (proj_fps_f / src_fps_f) if src_fps_f else 1.0
        # Use floor/int to match historic behavior (old code used int(...) on endpoints)
        max_y_project = max(1, int(video_len_src * scale))

    # Clamp time-curve points (project-frame domain)
    time_data = clip_data.get("time")
    points = time_data.get("Points") if isinstance(time_data, dict) else None

    if isinstance(points, list) and len(points) > 1:
        for point in points:
            co = point.get("co", {})
            # X is project frames
            if "X" in co and co["X"] is not None:
                co["X"] = int(round(co["X"]))
            # Y is project frames (not source frames)
            if "Y" in co and co["Y"] is not None:
                y = int(round(co["Y"]))
                if y < 1:
                    co["Y"] = 1
                elif y > max_y_project:
                    co["Y"] = max_y_project
                else:
                    co["Y"] = y
        # Keep duration/end unchanged for multi-point curves
    else:
        # Zero or one time point → reset duration to full media duration
        clip_data["duration"] = float(max_duration_sec)

    # --- Clamp start/end trims in SECONDS domain ---
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
    # Keep duration consistent with start/end
    clip_data["duration"] = float(end_sec - start_sec)

    return clip_data
