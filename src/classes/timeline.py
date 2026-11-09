"""
 @file
 @brief This file contains a timeline object, which listens for updates and syncs a libopenshot timeline object
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
import threading
import openshot  # Python module for libopenshot (required video editing module installed separately)
from qt_api import QTimer

from classes import crash_handler, update_batch
from classes.path_utils import media_paths_equal
from classes.updates import UpdateInterface
from classes.logger import log
from classes.app import get_app


def _payload_reader_path(payload):
    """Absolute-ish media path a clip/effect JSON would open, or None."""
    if not isinstance(payload, dict):
        return None
    reader = payload.get("reader")
    if isinstance(reader, dict) and reader.get("path"):
        return str(reader["path"])
    if payload.get("path"):
        return str(payload["path"])
    return None


def _payload_proxy_path(payload):
    """Optimized-preview path nested on the payload or its reader, or None."""
    if not isinstance(payload, dict):
        return None
    containers = [payload]
    reader = payload.get("reader")
    if isinstance(reader, dict):
        containers.append(reader)
    for container in containers:
        proxy = container.get("proxy_reader")
        if isinstance(proxy, dict) and proxy.get("path"):
            return str(proxy["path"])
    return None


def _payload_fingerprint_sha(payload):
    """sha256 from a clip/effect payload or its nested reader, or None."""
    if not isinstance(payload, dict):
        return None
    for container in (payload, payload.get("reader") if isinstance(payload.get("reader"), dict) else None):
        if not isinstance(container, dict):
            continue
        fingerprint = container.get("fingerprint")
        if isinstance(fingerprint, dict) and fingerprint.get("sha256"):
            return str(fingerprint["sha256"])
    return None


def _project_item_before_update(key):
    """Clip/effect still in the project at *key* when TimelineSync runs.

    TimelineSync is registered at listener index 0, so it runs *before*
    ProjectDataStore commits the mutation and fills ``action.old_values``.
    The project still holds the pre-update item here — that is the baseline
    we must compare against on the live edit path.
    """
    if not key or len(key) < 2 or not isinstance(key[1], dict):
        return None
    item_id = key[1].get("id")
    if not item_id:
        return None
    root = str(key[0]).lower()
    if root not in ("clips", "effects"):
        return None
    try:
        items = get_app().project.get(root) or []
    except Exception:
        log.debug("could not read project %s for media-source check", root, exc_info=True)
        return None
    for item in items:
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    return None


def media_source_changed(action):
    """True when a clip/effect update swaps the file (or proxy) behind the same timeline object.

    ApplyJsonDiff drops frame ranges for property edits, but when only the
    reader path / file_id changes the composite CacheMemory can keep serving
    frames from the previous file — preview shows the old video while a fresh
    export Timeline (SetJson) shows the new one. Proxy apply already clears
    for the same reason; normal relinks and agent B-roll swaps must too.

    Do **not** treat "old_values is missing" as a media change: on every live
    edit TimelineSync sees the action before ProjectDataStore sets old_values,
    and a full clip save always carries ``file_id`` / ``reader.path``. Clearing
    on that would reintroduce the #332 playback-cache starvation.
    """
    if not action or getattr(action, "type", None) != "update":
        return False
    key = getattr(action, "key", None) or []
    if not key or str(key[0]).lower() not in ("clips", "effects"):
        return False
    new = getattr(action, "values", None)
    if not isinstance(new, dict):
        return False

    old = getattr(action, "old_values", None)
    if not isinstance(old, dict):
        old = _project_item_before_update(key)
    if not isinstance(old, dict):
        return False

    new_fid, old_fid = new.get("file_id"), old.get("file_id")
    if new_fid is not None and old_fid is not None and new_fid != old_fid:
        return True

    new_path, old_path = _payload_reader_path(new), _payload_reader_path(old)
    if new_path and old_path and not media_paths_equal(new_path, old_path):
        return True

    # Same path, new bytes on disk (relink / overwrite): fingerprints diverge.
    new_sha, old_sha = _payload_fingerprint_sha(new), _payload_fingerprint_sha(old)
    if new_sha and old_sha and new_sha != old_sha:
        return True

    # Proxy compare only when this update carries a reader (or top-level proxy).
    # A partial property tweak must not look like "proxy removed" just because
    # the new payload omitted reader entirely.
    touches_reader = (
        isinstance(new.get("reader"), dict)
        or "proxy_reader" in new
        or "path" in new
    )
    if not touches_reader:
        return False
    new_proxy, old_proxy = _payload_proxy_path(new), _payload_proxy_path(old)
    if new_proxy and old_proxy:
        return not media_paths_equal(new_proxy, old_proxy)
    return bool(new_proxy) != bool(old_proxy)


class TimelineSync(UpdateInterface):
    """ This class syncs changes from the timeline to libopenshot """

    def __init__(self, window):
        self.app = get_app()
        self.window = window
        project = self.app.project
        # Protect libopenshot timeline operations from concurrent access patterns
        # (e.g. UI cache polling vs. JSON diff application), which can otherwise
        # result in blocking calls and UI unresponsiveness.
        self.timeline_lock = threading.RLock()

        # Get some settings from the project
        fps = project.get("fps")
        width = project.get("width")
        height = project.get("height")
        sample_rate = project.get("sample_rate")
        channels = project.get("channels")
        channel_layout = project.get("channel_layout")

        # Create an instance of a libopenshot Timeline object
        self.timeline = openshot.Timeline(width, height, openshot.Fraction(fps["num"], fps["den"]),
                                          sample_rate, channels, channel_layout)
        # The first Timeline installs libopenshot's CrashHandler, which also
        # traps SIGPIPE; hand SIGPIPE back to Python so a client that resets
        # a local socket cannot end the app (see crash_handler.ignore_sigpipe).
        crash_handler.ignore_sigpipe()
        self.timeline.info.channel_layout = channel_layout
        self.timeline.info.has_audio = True
        self.timeline.info.has_video = True
        self.timeline.info.video_length = 99999
        self.timeline.info.duration = 999.99
        self.timeline.info.sample_rate = sample_rate
        self.timeline.info.channels = channels

        # Open the timeline reader
        with self.timeline_lock:
            self.timeline.Open()

        # Diffs held back while an update batch is open (see classes.update_batch): applied in one call at the end.
        self._batched_diffs = []
        self._batched_property_edit = False
        self._batched_media_source_changed = False
        update_batch.register(self.flush_batch)

        # Add self as listener to project data updates (at the beginning of the list)
        # This listener will receive events before others.
        self.app.updates.add_listener(self, 0)

        # Connect to signal
        self.window.MaxSizeChanged.connect(self.MaxSizeChangedCB)

    def changed(self, action):
        """ This method is invoked by the UpdateManager each time a change happens (i.e UpdateInterface) """

        # Ignore changes that don't affect libopenshot
        if action and len(action.key) >= 1 and action.key[0].lower() in ["files", "history", "markers", "layers", "scale", "profile", "export_settings", "export_overrides", "zenvi_cloud", "edit_brief", "recreation_placed"]:
            return

        if action and action.type != "load" and update_batch.active():
            # An edit is changing many things: keep the diffs in order and apply them once when it ends.
            self._batched_diffs.extend(json.loads(self._preview_payload(action.json(is_array=True))))
            self._batched_property_edit = self._batched_property_edit or action.type == "update"
            self._batched_media_source_changed = (
                self._batched_media_source_changed or media_source_changed(action))
            return
        if action and action.type == "load":
            self._batched_diffs = []        # a load replaces the whole timeline: held-back diffs would be stale
            self._batched_property_edit = False
            self._batched_media_source_changed = False

        with self.timeline_lock:
            # Enter edit mode for property updates — disable caching until the user seeks or plays.
            # Only "update" actions represent manual property edits; structural changes like
            # inserting/deleting clips should not interrupt caching. Also skip during playback
            # so live property tweaks don't kill an in-progress cache fill.
            if action and action.type == "update":
                try:
                    is_playing = self.window.preview_thread.player.Mode() == openshot.PLAYBACK_PLAY
                except Exception:
                    is_playing = False
                if not is_playing:
                    openshot.Settings.Instance().ENABLE_PLAYBACK_CACHING = False

            try:
                proxy_service = getattr(self.window, "proxy_service", None)
                if action.type == "load":
                    # Clear any selections in UI (since we are clearing the timeline)
                    self.window.clearSelections()

                    # Clear any existing clips & effects (free memory)
                    self.timeline.Close()
                    self.timeline.Clear()

                    # This JSON is initially loaded to libopenshot to update the timeline
                    payload = action.json(only_value=True)
                    if proxy_service:
                        payload = proxy_service.rewrite_json_for_preview(payload)
                    self.timeline.SetJson(payload)
                    self.timeline.Open()  # Re-Open the Timeline reader

                    # The timeline's profile changed, so update all clips
                    self.timeline.ApplyMapperToClips()

                    # Always seek back to frame 1
                    self.window.SeekSignal.emit(1)

                    # Refresh current frame (since the entire timeline was updated)
                    if getattr(self.window, "_project_loading", False):
                        self.window._pending_project_open_refresh = True
                    else:
                        self.window.refreshFrameSignal.emit()

                else:
                    # This JSON DIFF is passed to libopenshot to update the timeline
                    self.timeline.ApplyJsonDiff(self._preview_payload(action.json(is_array=True)))
                    # Path/file_id swaps keep the same frame ranges — drop the whole
                    # preview cache or the widget keeps showing the previous file.
                    if media_source_changed(action):
                        self.timeline.ClearAllCache(True)

                    # Refresh the preview so the video widget reflects the change
                    self.window.refreshFrameSignal.emit()

            except Exception as e:
                log.error("Error applying JSON to timeline object in libopenshot: %s. %s" %
                         (e, action.json(is_array=True)))
            finally:
                self._rewarm()  # even a failed load or diff has changed (or cleared) the timeline

            # Cache stays off — re-enabled when the user seeks or starts playback

    def _rewarm(self):
        """Warm the changed timeline again (the warmer does one pass, then idles)."""
        warmer = getattr(self.window, "background_render_manager", None)
        if warmer is not None:
            warmer.invalidate()

    def _preview_payload(self, payload):
        """The diff as libopenshot should read it (proxy readers swapped in when there are proxies)."""
        proxy_service = getattr(self.window, "proxy_service", None)
        return proxy_service.rewrite_json_for_preview(payload) if proxy_service else payload

    def flush_batch(self):
        """Apply every diff held back during an update batch in one libopenshot call, then refresh the preview once."""
        diffs, self._batched_diffs = self._batched_diffs, []
        property_edit, self._batched_property_edit = self._batched_property_edit, False
        media_changed, self._batched_media_source_changed = self._batched_media_source_changed, False
        if not diffs:
            return
        with self.timeline_lock:
            if property_edit:
                try:
                    is_playing = self.window.preview_thread.player.Mode() == openshot.PLAYBACK_PLAY
                except Exception:
                    is_playing = False
                if not is_playing:
                    openshot.Settings.Instance().ENABLE_PLAYBACK_CACHING = False
            try:
                # ApplyJsonDiff drops the edited ranges; a full ClearAllCache starved playback (#332)
                # unless the media behind a clip changed — then the ranges match and the cache lies.
                self.timeline.ApplyJsonDiff(json.dumps(diffs))
                if media_changed:
                    self.timeline.ClearAllCache(True)
                self.window.refreshFrameSignal.emit()
            except Exception as e:
                log.error("Error applying batched JSON to timeline object in libopenshot: %s" % e)
            finally:
                self._rewarm()

    def MaxSizeChangedCB(self, new_size):
        """Callback for max sized change (i.e. max size of video widget)"""
        if not self.window.initialized:
            log.info('Deferring SetMaxSize until main window initialization completes')
            self.window._pending_preview_size = new_size
            QTimer.singleShot(0, self.window._finish_pending_preview_resize)
            return

        if getattr(self.window, "_dock_interaction_active", False):
            self.window._pending_preview_size = new_size
            return

        # Increase based on DPI
        device_pixel_ratio = self.window.devicePixelRatioF()
        scaled_width = round(new_size.width() * device_pixel_ratio)
        scaled_height = round(new_size.height() * device_pixel_ratio)

        if scaled_width < 1 or scaled_height < 1:
            log.info(
                "Skipping preview max size update for invalid size: %sx%s",
                scaled_width,
                scaled_height,
            )
            return

        log.info(f"Adjusting max size of preview image: {scaled_width}x{scaled_height}")

        # Set new max video size (Based on preview widget size and display scaling)
        previous_preview_width = self.timeline.preview_width
        previous_preview_height = self.timeline.preview_height

        with self.timeline_lock:
            self.timeline.SetMaxSize(scaled_width, scaled_height)

        if (
            previous_preview_width != self.timeline.preview_width
            or previous_preview_height != self.timeline.preview_height
        ):
            with self.timeline_lock:
                # Clear timeline preview cache (since our video size has changed)
                self.timeline.ClearAllCache(True)

            if getattr(self.window, "_project_loading", False):
                self.window._pending_project_open_refresh = True
                return

            # Refresh current frame (since the entire timeline was updated)
            self.window.refreshFrameSignal.emit()

    def GetLastFrame(self):
        """Return the last seekable/playable frame on the timeline."""
        try:
            max_frame = max(1, int(self.timeline.GetMaxFrame()))
        except Exception:
            return 1
        return max(1, max_frame - 1)
