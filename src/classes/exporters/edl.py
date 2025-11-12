"""
 @file
 @brief This file is used to generate an EDL (edit decision list) export
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

import os
from operator import itemgetter

from PyQt5.QtWidgets import QFileDialog

from classes import info
from classes.app import get_app
from classes.logger import log
from classes.path_utils import relative_export_path, absolute_media_path
from classes.query import Clip, Track, File
from classes.time_parts import secondsToTimecode


def _is_drop_frame(fps_num, fps_den):
    """Return True if FPS corresponds to a common drop-frame rate."""
    if fps_den == 0:
        return False
    fps = float(fps_num) / float(fps_den)
    return any(abs(fps - rate) < 0.01 for rate in (29.97, 59.94))


def _clip_media_path(clip):
    file_id = clip.data.get("file_id")
    if file_id:
        file_obj = File.get(id=file_id)
        if file_obj:
            path = file_obj.absolute_path()
            if path:
                return path

    reader_path = clip.data.get("reader", {}).get("path")
    if reader_path:
        resolved = absolute_media_path(reader_path)
        if resolved:
            return resolved
        return reader_path
    return ""


def export_edl():
    """Export EDL File"""
    app = get_app()
    _ = app._tr

    # EDL Export format
    edl_string = "%03d  %-9s%-6s%-9s%11s %11s %11s %11s\n"

    # Get FPS info
    fps_num = get_app().project.get("fps").get("num", 24)
    fps_den = get_app().project.get("fps").get("den", 1)
    fps_float = float(fps_num / fps_den)

    # Get EDL path
    recommended_path = app.project.current_filepath or ""
    if not recommended_path:
        recommended_path = os.path.join(info.HOME_PATH, "%s.edl" % _("Untitled Project"))
    else:
        recommended_path = recommended_path.replace(".osp", ".edl")
    file_path = QFileDialog.getSaveFileName(app.window, _("Export EDL..."), recommended_path,
                                            _("Edit Decision List (*.edl)"))[0]
    if not file_path:
        return

    # Append .edl if needed
    if not file_path.endswith(".edl"):
        file_path = "%s.edl" % file_path

    export_root = os.path.dirname(os.path.abspath(file_path))

    # Get filename with no extension
    file_name_with_ext = os.path.basename(file_path)
    file_name = os.path.splitext(file_name_with_ext)[0]

    all_tracks = get_app().project.get("layers")
    track_count = len(all_tracks)
    for track in reversed(sorted(all_tracks, key=itemgetter('number'))):
        existing_track = Track.get(number=track.get("number"))
        if not existing_track:
            # Log error and fail silently, and continue
            log.error('No track object found with number: %s' % track.get("number"))
            continue

        # Track name
        track_name = track.get("label") or "TRACK %s" % track_count
        clips_on_track = sorted(Clip.filter(layer=track.get("number")), key=lambda c: c.data.get('position', 0.0))
        if not clips_on_track:
            continue

        # Generate EDL File (1 per track - limitation of EDL format)
        # TODO: Improve and move this into its own class
        with open("%s-%s.edl" % (file_path.replace(".edl", ""), track_name), 'w', encoding="utf8") as f:
            # Add Header
            f.write("TITLE: %s - %s\n" % (file_name, track_name))
            f.write("FCM: %s\n\n" % ("DROP FRAME" if _is_drop_frame(fps_num, fps_den) else "NON-DROP FRAME"))

            # Loop through each track
            export_position = 0.0
            event_index = 1

            # Loop through clips on this track
            for clip in clips_on_track:
                clip_position = clip.data.get('position', 0.0)
                clip_start = clip.data.get('start', 0.0)
                clip_end = clip.data.get('end', clip_start)
                clip_duration = clip_end - clip_start

                # Do we need a blank clip?
                if clip_position > export_position:
                    clip_start_time = secondsToTimecode(0.0, fps_num, fps_den)
                    clip_end_time = secondsToTimecode(clip_position - export_position, fps_num, fps_den)
                    timeline_start_time = secondsToTimecode(export_position, fps_num, fps_den)
                    timeline_end_time = secondsToTimecode(clip_position, fps_num, fps_den)

                    f.write(edl_string % (
                        event_index, "BL"[:9], "V"[:6], "C",
                        clip_start_time, clip_end_time,
                        timeline_start_time, timeline_end_time))
                    event_index += 1
                    export_position = clip_position

                # Format clip start/end and timeline start/end values (i.e. 00:00:00:00)
                clip_start_time = secondsToTimecode(clip_start, fps_num, fps_den)
                clip_end_time = secondsToTimecode(clip_end, fps_num, fps_den)
                timeline_start_time = secondsToTimecode(clip_position, fps_num, fps_den)
                timeline_end_time = secondsToTimecode(clip_position + clip_duration, fps_num, fps_den)

                has_video = clip.data.get("reader", {}).get("has_video", False)
                has_audio = clip.data.get("reader", {}).get("has_audio", False)
                if not has_video and not has_audio:
                    continue
                reel_name = clip.data.get("reel") or "AX"
                if has_video:
                    # Video Track
                    f.write(edl_string % (
                            event_index, reel_name[:9], "V"[:6], "C",
                            clip_start_time, clip_end_time,
                            timeline_start_time, timeline_end_time))
                if has_audio:
                    # Audio Track
                    f.write(edl_string % (
                            event_index, reel_name[:9], "A"[:6], "C",
                            clip_start_time, clip_end_time,
                            timeline_start_time, timeline_end_time))
                f.write("* FROM CLIP NAME: %s\n" % clip.data.get('title'))
                media_path = _clip_media_path(clip)
                relative_media = relative_export_path(media_path, export_root)
                if relative_media:
                    f.write("* SOURCE FILE: %s\n" % relative_media)

                # Add opacity data (if any)
                alpha_points = clip.data.get('alpha', {}).get('Points', [])
                if len(alpha_points) > 1:
                    # Loop through Points (remove duplicates)
                    keyframes = {}
                    for point in alpha_points:
                        keyframeTime = (point.get('co', {}).get('X', 1.0) - 1) / fps_float
                        keyframeValue = point.get('co', {}).get('Y', 0.0) * 100.0
                        keyframes[keyframeTime] = keyframeValue
                    # Write keyframe values to EDL
                    for opacity_time in sorted(keyframes.keys()):
                        opacity_value = keyframes.get(opacity_time)
                        f.write("* OPACITY LEVEL AT %s IS %0.2f%%  (REEL AX)\n" % (secondsToTimecode(opacity_time, fps_num, fps_den), opacity_value))

                # Add volume data (if any)
                volume_points = clip.data.get('volume', {}).get('Points', [])
                if len(volume_points) > 1:
                    # Loop through Points (remove duplicates)
                    keyframes = {}
                    for point in volume_points:
                        keyframeTime = (point.get('co', {}).get('X', 1.0) - 1) / fps_float
                        keyframeValue = (point.get('co', {}).get('Y', 0.0) * 99.0) - 99 # Scaling 0-1 to -99-0
                        keyframes[keyframeTime] = keyframeValue
                    # Write keyframe values to EDL
                    for volume_time in sorted(keyframes.keys()):
                        volume_value = keyframes.get(volume_time)
                        f.write("* AUDIO LEVEL AT %s IS %0.2f DB  (REEL AX A1)\n" % (secondsToTimecode(volume_time, fps_num, fps_den), volume_value))

                # Update export position
                export_position = max(export_position, clip_position + clip_duration)
                event_index += 1
                f.write("\n")

            # Update counters
            track_count -= 1
