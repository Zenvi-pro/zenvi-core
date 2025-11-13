"""
 @file
 @brief This file is used to import a Final Cut Pro XML file
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

import json
import os
from operator import itemgetter
from urllib.parse import unquote, urlparse
from xml.dom import minidom

import openshot
from PyQt5.QtWidgets import QFileDialog

from classes import info
from classes.app import get_app
from classes.logger import log
from classes.image_types import get_media_type
from classes.path_utils import absolute_path_from_export, absolute_media_path
from classes.query import Clip, Track, File
from windows.views.find_file import find_missing_file


def _pathurl_to_path(path_url, base_folder):
    """Convert a Final Cut pathurl value into a filesystem path."""
    if not path_url:
        return ""
    parsed = urlparse(path_url)
    if parsed.scheme and parsed.scheme.lower() == "file":
        netloc = parsed.netloc
        path = parsed.path or ""
        if netloc and netloc.lower() not in ("", "localhost"):
            path = "/%s%s" % (netloc, path)
        path = unquote(path)
        if len(path) > 2 and path[0] == "/" and path[2:3] == ":":
            # Windows drive letter encoded as /C:/
            path = path[1:]
        return os.path.normpath(path)

    if path_url.startswith("@"):
        return absolute_media_path(path_url)

    normalized = unquote(path_url)
    return absolute_path_from_export(normalized, base_folder)


def _extract_path_from_file_node(file_node, file_lookup, base_folder):
    """Extract pathurl information, supporting referenced file nodes."""
    if not file_node:
        return ""

    path_nodes = file_node.getElementsByTagName("pathurl")
    if path_nodes and path_nodes[0].childNodes:
        return _pathurl_to_path(path_nodes[0].childNodes[0].nodeValue, base_folder)

    # Follow references to shared file definitions
    file_id = file_node.getAttribute("id")
    referenced_node = file_lookup.get(file_id)
    if referenced_node is not None and referenced_node is not file_node:
        ref_paths = referenced_node.getElementsByTagName("pathurl")
        if ref_paths and ref_paths[0].childNodes:
            return _pathurl_to_path(ref_paths[0].childNodes[0].nodeValue, base_folder)
    return ""

def _clip_merge_key(path, start, end, position):
    """Return a hashable key for matching audio/video clip pairs."""
    if not path:
        return None
    normalized_path = os.path.normcase(os.path.abspath(path))
    return (
        normalized_path,
        round(float(start or 0.0), 4),
        round(float(end or 0.0), 4),
        round(float(position or 0.0), 4)
    )


def _xml_interp_to_point(value):
    """Map Final Cut interpolation data to OpenShot constants."""
    if value is None:
        return openshot.LINEAR
    try:
        numeric = int(float(value))
    except (ValueError, TypeError):
        numeric = None

    if numeric == 0:
        return openshot.LINEAR
    if numeric == 1:
        return openshot.BEZIER
    if numeric == 2:
        return openshot.CONSTANT

    text = str(value).strip().lower()
    if text.startswith("lin"):
        return openshot.LINEAR
    if text.startswith("bez"):
        return openshot.BEZIER
    if text.startswith("const") or text.startswith("hold"):
        return openshot.CONSTANT

    return openshot.LINEAR


def import_xml():
    """Import final cut pro XML file"""
    app = get_app()
    _ = app._tr

    # Get FPS info
    fps_num = app.project.get("fps").get("num", 24)
    fps_den = app.project.get("fps").get("den", 1)
    fps_float = float(fps_num / fps_den)

    # Get XML path
    recommended_path = app.project.current_filepath or ""
    if not recommended_path:
        recommended_path = info.HOME_PATH
    else:
        recommended_path = os.path.dirname(recommended_path)
    file_path = QFileDialog.getOpenFileName(app.window, _("Import XML..."), recommended_path,
                                            _("Final Cut Pro (*.xml)"), _("Final Cut Pro (*.xml)"))[0]

    if not file_path or not os.path.exists(file_path):
        # User canceled dialog
        return

    # Parse XML file
    xmldoc = minidom.parse(file_path)
    xml_folder = os.path.dirname(os.path.abspath(file_path))

    # Build lookup for shared <file> nodes
    file_lookup = {}
    for file_element in xmldoc.getElementsByTagName("file"):
        file_id = file_element.getAttribute("id")
        if file_id:
            file_lookup[file_id] = file_element

    # Get video tracks
    video_tracks = []
    for video_element in xmldoc.getElementsByTagName("video"):
        for video_track in video_element.getElementsByTagName("track"):
            video_tracks.append(video_track)
    audio_tracks = []
    for audio_element in xmldoc.getElementsByTagName("audio"):
        for audio_track in audio_element.getElementsByTagName("track"):
            audio_tracks.append(audio_track)

    # Loop through tracks
    track_index = 0
    imported_clip_map = {}

    for track_list, track_type in ((video_tracks, "video"), (audio_tracks, "audio")):
        is_audio_track_list = (track_type == "audio")
        for track_element in track_list:
            # Get clipitems on this track (if any)
            clips_on_track = track_element.getElementsByTagName("clipitem")
            if not clips_on_track:
                continue

            # Get # of tracks
            track_index += 1
            all_tracks = app.project.get("layers")
            track_number = list(reversed(sorted(all_tracks, key=itemgetter('number'))))[0].get("number") + 1000000

            # Prepare to create track lazily (only if clips remain after merging)
            track = None
            is_locked = False
            if track_element.getElementsByTagName("locked")[0].childNodes[0].nodeValue == "TRUE":
                is_locked = True

            def ensure_track():
                nonlocal track
                if track is None:
                    track = Track()
                    track.data = {"number": track_number, "y": 0, "label": "XML Import %s" % track_index, "lock": is_locked}
                    track.save()

            # Loop through clips
            for clip_element in clips_on_track:
                # Get clip path (handles shared file nodes)
                file_elements = clip_element.getElementsByTagName("file")
                if not file_elements:
                    continue
                clip_path = _extract_path_from_file_node(file_elements[0], file_lookup, xml_folder)
                if not clip_path:
                    continue

                clip_path, is_modified, is_skipped = find_missing_file(clip_path)
                if is_skipped:
                    continue

                # Check for this path in our existing project data
                file = File.get(path=clip_path)

                # Load filepath in libopenshot clip object (which will try multiple readers to open it)
                clip_obj = openshot.Clip(clip_path)

                if not file:
                    # Get the JSON for the clip's internal reader
                    try:
                        reader = clip_obj.Reader()
                        file_data = json.loads(reader.Json())

                        # Determine media type
                        file_data["media_type"] = get_media_type(file_data)

                        # Save new file to the project data
                        file = File()
                        file.data = file_data

                        # Save file
                        file.save()
                    except Exception:
                        log.warning('Error building File object for %s' % clip_path, exc_info=1)

                if (file.data["media_type"] == "video" or file.data["media_type"] == "image"):
                    # Determine thumb path
                    thumb_path = os.path.join(info.THUMBNAIL_PATH, "%s.png" % file.data["id"])
                else:
                    # Audio file
                    thumb_path = os.path.join(info.PATH, "images", "AudioThumbnail.png")

                # Create Clip object
                clip = Clip()
                clip_start_value = float(clip_element.getElementsByTagName("in")[0].childNodes[0].nodeValue) / fps_float
                clip_end_value = float(clip_element.getElementsByTagName("out")[0].childNodes[0].nodeValue) / fps_float
                clip_position_value = float(clip_element.getElementsByTagName("start")[0].childNodes[0].nodeValue) / fps_float

                clip.data = json.loads(clip_obj.Json())
                clip.data["file_id"] = file.id
                clip.data["title"] = clip_element.getElementsByTagName("name")[0].childNodes[0].nodeValue
                clip.data["layer"] = track_number
                clip.data["image"] = thumb_path
                clip.data["position"] = clip_position_value
                clip.data["start"] = clip_start_value
                clip.data["end"] = clip_end_value

                alpha_points = []
                volume_points = []
                # Loop through clip's effects
                for effect_element in clip_element.getElementsByTagName("effect"):
                    effectid = effect_element.getElementsByTagName("effectid")[0].childNodes[0].nodeValue
                    keyframes = effect_element.getElementsByTagName("keyframe")
                    if effectid == "opacity":
                        for keyframe_element in keyframes:
                            keyframe_time = float(keyframe_element.getElementsByTagName("when")[0].childNodes[0].nodeValue)
                            keyframe_value = float(keyframe_element.getElementsByTagName("value")[0].childNodes[0].nodeValue) / 100.0
                            interp_nodes = keyframe_element.getElementsByTagName("interpolation")
                            interp_value = interp_nodes[0].childNodes[0].nodeValue if interp_nodes and interp_nodes[0].childNodes else None
                            alpha_points.append(
                                {
                                    "co": {
                                        "X": round(keyframe_time),
                                        "Y": keyframe_value
                                    },
                                    "interpolation": _xml_interp_to_point(interp_value)
                                }
                            )
                    elif effectid == "audiolevels":
                        for keyframe_element in keyframes:
                            keyframe_time = float(keyframe_element.getElementsByTagName("when")[0].childNodes[0].nodeValue)
                            keyframe_value = float(keyframe_element.getElementsByTagName("value")[0].childNodes[0].nodeValue)
                            if keyframe_value > 5.0:
                                keyframe_value = keyframe_value / 100.0
                            keyframe_value = max(0.0, min(1.0, keyframe_value))
                            interp_nodes = keyframe_element.getElementsByTagName("interpolation")
                            interp_value = interp_nodes[0].childNodes[0].nodeValue if interp_nodes and interp_nodes[0].childNodes else None
                            volume_points.append(
                                {
                                    "co": {
                                        "X": round(keyframe_time),
                                        "Y": keyframe_value
                                    },
                                    "interpolation": _xml_interp_to_point(interp_value)
                                }
                            )

                merge_key = _clip_merge_key(clip_path, clip_start_value, clip_end_value, clip_position_value)

                if is_audio_track_list and merge_key in imported_clip_map:
                    existing_clip = imported_clip_map[merge_key]
                    if volume_points:
                        existing_clip.data["volume"] = {"Points": volume_points}
                    existing_clip.save()
                    continue

                ensure_track()

                if alpha_points:
                    clip.data["alpha"] = {"Points": alpha_points}
                if volume_points:
                    clip.data["volume"] = {"Points": volume_points}
                # Save clip
                clip.save()

                if not is_audio_track_list and merge_key:
                    imported_clip_map[merge_key] = clip

            # Update the preview and reselect current frame in properties
            app.window.refreshFrameSignal.emit()
            app.window.propertyTableView.select_frame(app.window.preview_thread.player.Position())

    # Free up DOM memory
    xmldoc.unlink()
