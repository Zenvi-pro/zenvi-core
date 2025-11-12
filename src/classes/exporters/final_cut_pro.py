"""
 @file
 @brief This file is used to generate a Final Cut Pro export
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
from uuid import uuid1
from xml.dom import minidom

from PyQt5.QtWidgets import QFileDialog

from classes import info
from classes.app import get_app
from classes.logger import log
from classes.path_utils import absolute_media_path, relative_export_path, normalize_path
from classes.query import Clip, Track, File

TICKS_PER_FRAME = 254016000000


def _set_text(node, value):
    """Safely set the text content of a DOM node."""
    if not node:
        return
    if node.firstChild:
        node.firstChild.nodeValue = str(value)
    else:
        node.appendChild(node.ownerDocument.createTextNode(str(value)))


def _is_ntsc_rate(fps_num, fps_den):
    return fps_den == 1001 and fps_num in (24000, 30000, 60000)


def _seconds_to_frames(value, fps_num, fps_den):
    if value is None:
        value = 0.0
    return int(round(float(value) * fps_num / fps_den))


def _format_timebase(fps_num, fps_den):
    fps_value = float(fps_num) / float(fps_den)
    text = ("{0:.3f}".format(fps_value)).rstrip('0').rstrip('.')
    return text or "0"


def _clip_file_info(clip):
    """Return (File object, merged metadata dict, absolute file path)."""
    file_obj = None
    file_data = {}
    file_id = clip.data.get("file_id")
    if file_id:
        file_obj = File.get(id=file_id)
        if file_obj and file_obj.data:
            file_data = dict(file_obj.data)

    reader_data = clip.data.get("reader", {}) or {}
    merged_data = dict(reader_data)
    merged_data.update(file_data)

    file_path = ""
    if file_obj:
        file_path = file_obj.absolute_path()
    if not file_path:
        file_path = absolute_media_path(reader_data.get("path"))
    if not file_path:
        file_path = reader_data.get("path", "")

    return file_obj, merged_data, file_path


def _apply_rate_settings(rate_nodes, timebase_value, ntsc_value):
    for rate_node in rate_nodes:
        timebase_nodes = rate_node.getElementsByTagName("timebase")
        if timebase_nodes:
            _set_text(timebase_nodes[0], timebase_value)
        ntsc_nodes = rate_node.getElementsByTagName("ntsc")
        if ntsc_nodes:
            _set_text(ntsc_nodes[0], ntsc_value)


def createEffect(xmldoc, name, node, points, scale):
    """Create the XML filter with keyframes"""
    # Find correct effect
    for effectNode in node.getElementsByTagName('effect'):
        effectName = effectNode.getElementsByTagName("name")[0].childNodes[0].nodeValue
        if effectName == name:
            parameterNode = effectNode.getElementsByTagName('parameter')[0]

            # Loop through Points (remove duplicates)
            keyframes = {}
            for point in points:
                keyframeTime = point.get('co', {}).get('X', 1)
                keyframeValue = point.get('co', {}).get('Y', 1) * scale
                keyframes[keyframeTime] = keyframeValue

            # Loop through Points
            first_value = None
            for keyframeTime in sorted(keyframes.keys()):
                keyframeValue = keyframes.get(keyframeTime)
                if first_value is None:
                    first_value = keyframeValue

                # Create keyframe element for each point
                keyframeNode = xmldoc.createElement("keyframe")
                parameterNode.appendChild(keyframeNode)
                whenNode = xmldoc.createElement("when")
                whenNode.appendChild(xmldoc.createTextNode(str(keyframeTime)))
                keyframeNode.appendChild(whenNode)
                valueNode = xmldoc.createElement("value")
                valueNode.appendChild(xmldoc.createTextNode(str(keyframeValue)))
                keyframeNode.appendChild(valueNode)


def export_xml():
    """Export final cut pro XML file"""
    app = get_app()
    _ = app._tr

    # Get FPS info
    fps_num = get_app().project.get("fps").get("num", 24)
    fps_den = get_app().project.get("fps").get("den", 1)
    fps_float = float(fps_num / fps_den)
    timebase_value = _format_timebase(fps_num, fps_den)
    ntsc_value = "TRUE" if _is_ntsc_rate(fps_num, fps_den) else "FALSE"

    # Get path
    recommended_path = get_app().project.current_filepath or ""
    if not recommended_path:
        recommended_path = os.path.join(info.HOME_PATH, "%s.xml" % _("Untitled Project"))
    else:
        recommended_path = recommended_path.replace(".osp", ".xml")
    file_path = QFileDialog.getSaveFileName(app.window, _("Export XML..."), recommended_path,
                                            _("Final Cut Pro (*.xml)"))[0]
    if not file_path:
        # User canceled dialog
        return

    # Append .xml if needed
    if not file_path.endswith(".xml"):
        file_path = "%s.xml" % file_path

    export_folder = os.path.dirname(os.path.abspath(file_path))

    # Get filename with no path
    file_name = os.path.basename(file_path)

    # Determine max frame (based on clips)
    duration = 0.0
    all_clips = Clip.filter()
    for clip in all_clips:
        clip_last_frame = (clip.data.get("position") or 0.0) + ((clip.data.get("end") or 0.0) - (clip.data.get("start") or 0.0))
        if clip_last_frame > duration:
            # Set max length of timeline
            duration = clip_last_frame
    duration_frames = _seconds_to_frames(duration, fps_num, fps_den)

    # XML template path
    xmldoc = minidom.parse(os.path.join(info.RESOURCES_PATH, 'export-project-template.xml'))

    # Set Project Details
    _set_text(xmldoc.getElementsByTagName("name")[0], file_name)
    _set_text(xmldoc.getElementsByTagName("uuid")[0], str(uuid1()))
    _set_text(xmldoc.getElementsByTagName("duration")[0], duration_frames)
    _set_text(xmldoc.getElementsByTagName("width")[0], app.project.get("width"))
    _set_text(xmldoc.getElementsByTagName("height")[0], app.project.get("height"))
    _set_text(xmldoc.getElementsByTagName("samplerate")[0], app.project.get("sample_rate"))
    sequence_node = xmldoc.getElementsByTagName("sequence")[0]
    if app.project.get("id"):
        sequence_node.setAttribute("id", app.project.get("id"))
    for childNode in xmldoc.getElementsByTagName("timebase"):
        _set_text(childNode, timebase_value)
    for ntsc_node in xmldoc.getElementsByTagName("ntsc"):
        _set_text(ntsc_node, ntsc_value)

    # Get parent nodes
    parentAudioNode = xmldoc.getElementsByTagName("audio")[0]
    parentVideoNode = xmldoc.getElementsByTagName("video")[0]
    num_output_channels = parentAudioNode.getElementsByTagName("numOutputChannels")
    if num_output_channels:
        project_channels = app.project.get("channels")
        if project_channels is None:
            project_channels = 2
        _set_text(num_output_channels[0], project_channels)

    # Loop through tracks
    all_tracks = get_app().project.get("layers")
    audio_track_index = 1
    for track in sorted(all_tracks, key=itemgetter('number')):
        existing_track = Track.get(number=track.get("number"))
        if not existing_track:
            # Log error and fail silently, and continue
            log.error('No track object found with number: %s' % track.get("number"))
            continue

        # Track details
        track_locked = track.get("lock", False)
        clips_on_track = sorted(Clip.filter(layer=track.get("number")), key=lambda c: c.data.get('position', 0.0))
        if not clips_on_track:
            continue

        has_video = any(c.data.get("reader", {}).get("has_video") for c in clips_on_track)
        has_audio = any(c.data.get("reader", {}).get("has_audio") for c in clips_on_track)

        videoTrackNode = None
        audioTrackNode = None
        audioTrackNumber = None

        if has_video:
            trackTemplateDoc = minidom.parse(os.path.join(info.RESOURCES_PATH, 'export-track-video-template.xml'))
            videoTrackNode = trackTemplateDoc.getElementsByTagName('track')[0]
            parentVideoNode.appendChild(videoTrackNode)
            if track_locked:
                _set_text(videoTrackNode.getElementsByTagName("locked")[0], "TRUE")

        if has_audio:
            trackTemplateDoc = minidom.parse(os.path.join(info.RESOURCES_PATH, 'export-track-audio-template.xml'))
            audioTrackNode = trackTemplateDoc.getElementsByTagName('track')[0]
            parentAudioNode.appendChild(audioTrackNode)
            audioTrackNumber = audio_track_index
            audio_track_index += 1
            output_nodes = audioTrackNode.getElementsByTagName("outputchannelindex")
            if output_nodes:
                _set_text(output_nodes[0], audioTrackNumber)
            if track_locked:
                _set_text(audioTrackNode.getElementsByTagName("locked")[0], "TRUE")

        # Loop through clips on this track
        for clip in clips_on_track:
            clip_reader = clip.data.get("reader", {}) or {}
            clip_duration_frames = _seconds_to_frames((clip.data.get('end') or 0.0) - (clip.data.get('start') or 0.0), fps_num, fps_den)
            if clip_duration_frames <= 0:
                continue

            clip_in_frames = _seconds_to_frames(clip.data.get('start') or 0.0, fps_num, fps_den)
            clip_out_frames = clip_in_frames + clip_duration_frames
            timeline_in_frames = _seconds_to_frames(clip.data.get('position') or 0.0, fps_num, fps_den)
            timeline_out_frames = timeline_in_frames + clip_duration_frames

            _, merged_data, abs_media_path = _clip_file_info(clip)
            display_name = merged_data.get("name") or os.path.basename(merged_data.get("path", "") or clip.data.get('title') or "")
            clip_title = clip.data.get('title') or display_name or os.path.basename(abs_media_path or "") or "Clip"
            sample_rate = merged_data.get("sample_rate") or merged_data.get("audio_sample_rate") or app.project.get("sample_rate") or 48000
            channel_count = merged_data.get("channels") or merged_data.get("channel_count") or 2
            try:
                sample_rate = int(sample_rate)
            except (TypeError, ValueError):
                sample_rate = 48000
            try:
                channel_count = int(channel_count)
            except (TypeError, ValueError):
                channel_count = 2
            relative_media_path = relative_export_path(abs_media_path, export_folder)

            if clip_reader.get("has_video") and videoTrackNode:
                clipTemplateDoc = minidom.parse(os.path.join(info.RESOURCES_PATH, 'export-clip-video-template.xml'))
                clipNode = clipTemplateDoc.getElementsByTagName('clipitem')[0]
                videoTrackNode.appendChild(clipNode)

                clipNode.setAttribute('id', clip.data.get('id'))
                clip_file_node = clipNode.getElementsByTagName("file")[0]
                if clip.data.get('file_id'):
                    clip_file_node.setAttribute('id', clip.data.get('file_id'))
                clip_names = clipNode.getElementsByTagName("name")
                if clip_names:
                    _set_text(clip_names[0], clip_title)
                if len(clip_names) > 1:
                    _set_text(clip_names[1], clip_title)
                _set_text(clipNode.getElementsByTagName("start")[0], timeline_in_frames)
                _set_text(clipNode.getElementsByTagName("end")[0], timeline_out_frames)
                _set_text(clipNode.getElementsByTagName("in")[0], clip_in_frames)
                _set_text(clipNode.getElementsByTagName("out")[0], clip_out_frames)
                _set_text(clipNode.getElementsByTagName("duration")[0], clip_duration_frames)
                _set_text(clipNode.getElementsByTagName("pproTicksIn")[0], clip_in_frames * TICKS_PER_FRAME)
                _set_text(clipNode.getElementsByTagName("pproTicksOut")[0], clip_out_frames * TICKS_PER_FRAME)
                _apply_rate_settings(clipNode.getElementsByTagName("rate"), timebase_value, ntsc_value)

                file_path_nodes = clip_file_node.getElementsByTagName("pathurl")
                if file_path_nodes:
                    _set_text(file_path_nodes[0], normalize_path(relative_media_path or abs_media_path or clip_title))
                file_name_nodes = clip_file_node.getElementsByTagName("name")
                if file_name_nodes:
                    _set_text(file_name_nodes[0], display_name)
                file_duration_nodes = clip_file_node.getElementsByTagName("duration")
                if file_duration_nodes:
                    file_duration_value = merged_data.get("duration")
                    if isinstance(file_duration_value, (int, float)):
                        _set_text(file_duration_nodes[0], _seconds_to_frames(file_duration_value, fps_num, fps_den))
                    else:
                        _set_text(file_duration_nodes[0], clip_duration_frames)
                width_nodes = clip_file_node.getElementsByTagName("width")
                if width_nodes:
                    _set_text(width_nodes[0], merged_data.get("width") or app.project.get("width"))
                height_nodes = clip_file_node.getElementsByTagName("height")
                if height_nodes:
                    _set_text(height_nodes[0], merged_data.get("height") or app.project.get("height"))
                samplerate_nodes = clip_file_node.getElementsByTagName("samplerate")
                if samplerate_nodes:
                    _set_text(samplerate_nodes[0], sample_rate)
                channel_nodes = clip_file_node.getElementsByTagName("channelcount")
                if channel_nodes:
                    _set_text(channel_nodes[0], channel_count)

                createEffect(xmldoc, "Opacity", clipNode, clip.data.get('alpha', {}).get('Points', []), 100.0)

            if clip_reader.get("has_audio") and audioTrackNode:
                clipTemplateDoc = minidom.parse(os.path.join(info.RESOURCES_PATH, 'export-clip-audio-template.xml'))
                clipAudioNode = clipTemplateDoc.getElementsByTagName('clipitem')[0]
                audioTrackNode.appendChild(clipAudioNode)

                clipAudioNode.setAttribute('id', "%s-audio" % clip.data.get('id'))
                if channel_count == 1:
                    clipAudioNode.setAttribute('premiereChannelType', 'mono')
                elif channel_count == 2:
                    clipAudioNode.setAttribute('premiereChannelType', 'stereo')
                else:
                    clipAudioNode.setAttribute('premiereChannelType', 'multichannel')

                audio_file_node = clipAudioNode.getElementsByTagName("file")[0]
                if clip.data.get('file_id'):
                    audio_file_node.setAttribute('id', clip.data.get('file_id'))
                audio_name_nodes = clipAudioNode.getElementsByTagName("name")
                if audio_name_nodes:
                    _set_text(audio_name_nodes[0], clip_title)
                if len(audio_name_nodes) > 1:
                    _set_text(audio_name_nodes[1], clip_title)
                _set_text(clipAudioNode.getElementsByTagName("start")[0], timeline_in_frames)
                _set_text(clipAudioNode.getElementsByTagName("end")[0], timeline_out_frames)
                _set_text(clipAudioNode.getElementsByTagName("in")[0], clip_in_frames)
                _set_text(clipAudioNode.getElementsByTagName("out")[0], clip_out_frames)
                _set_text(clipAudioNode.getElementsByTagName("duration")[0], clip_duration_frames)
                _set_text(clipAudioNode.getElementsByTagName("pproTicksIn")[0], clip_in_frames * TICKS_PER_FRAME)
                _set_text(clipAudioNode.getElementsByTagName("pproTicksOut")[0], clip_out_frames * TICKS_PER_FRAME)
                _apply_rate_settings(clipAudioNode.getElementsByTagName("rate"), timebase_value, ntsc_value)

                audio_path_nodes = audio_file_node.getElementsByTagName("pathurl")
                if audio_path_nodes:
                    _set_text(audio_path_nodes[0], normalize_path(relative_media_path or abs_media_path or clip_title))
                audio_file_names = audio_file_node.getElementsByTagName("name")
                if audio_file_names:
                    _set_text(audio_file_names[0], display_name)
                samplerate_nodes = audio_file_node.getElementsByTagName("samplerate")
                if samplerate_nodes:
                    _set_text(samplerate_nodes[0], sample_rate)
                channel_nodes = audio_file_node.getElementsByTagName("channelcount")
                if channel_nodes:
                    _set_text(channel_nodes[0], channel_count)

                sourcetrack_nodes = clipAudioNode.getElementsByTagName("sourcetrack")
                if sourcetrack_nodes and audioTrackNumber is not None:
                    track_index_nodes = sourcetrack_nodes[0].getElementsByTagName("trackindex")
                    if track_index_nodes:
                        _set_text(track_index_nodes[0], audioTrackNumber)

                track_index_nodes = clipAudioNode.getElementsByTagName("trackindex")
                if track_index_nodes and audioTrackNumber is not None:
                    _set_text(track_index_nodes[0], audioTrackNumber)

                createEffect(xmldoc, "Audio Levels", clipAudioNode, clip.data.get('volume', {}).get('Points', []), 1.0)

    try:
        file = open(os.fsencode(file_path), "wb")  # wb needed for windows support
        file.write(bytes(xmldoc.toxml(), 'UTF-8'))
        file.close()
    except IOError as inst:
        log.error("Error writing XML export: {}".format(str(inst)))
    finally:
        # Free up DOM memory
        xmldoc.unlink()
