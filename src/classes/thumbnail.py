"""
 @file
 @brief This file has code to generate thumbnail images and HTTP thumbnail server
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
import re
import openshot
import socket
import time
import shutil
import subprocess
from requests import get
from threading import Thread
from classes import info
from classes.ffmpeg_cli import run_ffmpeg
from classes.query import File
from classes.logger import log
from classes.app import get_app
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# Regex for parsing URLs: (examples)
#  http://127.0.0.1:33723/thumbnails/9ATJTBQ71V/1/path/no-cache/
#  http://127.0.0.1:33723/thumbnails/9ATJTBQ71V/1/path/
#  http://127.0.0.1:33723/thumbnails/9ATJTBQ71V/1/path
#  http://127.0.0.1:33723/thumbnails/9ATJTBQ71V/1/
#  http://127.0.0.1:33723/thumbnails/9ATJTBQ71V/1
# Decode at a multiple of the thumbnail size so downscaling keeps detail
# (only honoured by readers that expose SetMaxDecodeSize, libopenshot >= 1.0).
THUMBNAIL_DECODE_SCALE = 3.0

REGEX_THUMBNAIL_URL = re.compile(r"/thumbnails/(?P<file_id>.+?)/(?P<file_frame>\d+)/*(?P<only_path>path)?/*(?P<no_cache>no-cache)?")

# Optimize Preview pre-warms timeline thumbnails while it transcodes a proxy.
# Frames are snapped onto a coarse grid (this many thumbnails per second of
# source media) so that a nearby pre-warmed thumbnail can be served instead of
# decoding the exact frame.
THUMBNAIL_PREWARM_FPS = 4


def ThumbnailFrameStepForFps(fps, target_fps=THUMBNAIL_PREWARM_FPS):
    """Return the coarse thumbnail frame step for a source FPS."""
    fps = float(fps or 0.0)
    target_fps = max(1.0, float(target_fps or 1.0))
    if fps <= 0.0:
        return 1
    return max(1, int(round(fps / target_fps)))


def RoundFrameToThumbnailGrid(frame_number, fps, target_fps=THUMBNAIL_PREWARM_FPS):
    """Round a requested frame to the nearest coarse thumbnail grid frame."""
    frame_number = max(1, int(frame_number or 1))
    step = ThumbnailFrameStepForFps(fps, target_fps=target_fps)
    return max(1, int(round((frame_number - 1) / float(step))) * step + 1)


def ThumbnailPathForFrame(file_id, thumbnail_frame, fingerprint=None):
    """Return the canonical write path for a file/frame thumbnail.

    Zenvi keeps thumbnails in the fingerprint-keyed media cache when the file
    has a fingerprint, and under ``THUMBNAIL_PATH/<id>/<frame>.png`` otherwise
    (see ``classes.media_cache``).
    """
    return preferred_thumbnail_path(str(file_id), int(thumbnail_frame or 1), fingerprint=fingerprint)


def _file_fps(file):
    """Best-effort FPS for a File record (0.0 when unknown)."""
    try:
        fps_data = file.data.get("fps", {}) if isinstance(getattr(file, "data", None), dict) else {}
        fps_num = float(fps_data.get("num", 0.0) or 0.0)
        fps_den = float(fps_data.get("den", 1.0) or 1.0)
        return (fps_num / fps_den) if fps_num > 0.0 and fps_den > 0.0 else 0.0
    except (TypeError, ValueError, AttributeError):
        return 0.0


def GenerateThumbnailFromFrame(frame, thumb_path, width, height, mask, overlay, rotate=0.0):
    """Create a thumbnail image from an already decoded libopenshot Frame."""
    try:
        scale = float(get_app().devicePixelRatio())
    except Exception:
        scale = 1.0
    scale = max(1.0, scale)

    _ensure_thumb_dir(thumb_path)
    frame.Thumbnail(
        thumb_path,
        round(width * scale),
        round(height * scale),
        mask,
        overlay,
        "#000",
        False,
        "png",
        85,
        float(rotate or 0.0),
    )


def GetThumbPath(file_id, thumbnail_frame, clear_cache=False, attempts=1):
    """Get thumbnail path by invoking HTTP thumbnail request"""

    # Clear thumb cache (if requested)
    thumb_cache = ""
    if clear_cache:
        thumb_cache = "no-cache/"

    # Connect to thumbnail server and get image
    thumb_server_details = get_app().window.http_server_thread.server_address
    thumb_address = "http://%s:%s/thumbnails/%s/%s/path/%s" % (
        thumb_server_details[0],
        thumb_server_details[1],
        file_id,
        thumbnail_frame,
        thumb_cache)
    attempts = max(1, int(attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            r = get(thumb_address)
        except Exception:
            log.warning(
                "Thumbnail path request failed file_id=%s frame=%s attempt=%s/%s",
                file_id,
                thumbnail_frame,
                attempt,
                attempts,
                exc_info=1,
            )
            r = None

        if r is not None and r.ok and r.text:
            # Update thumbnail path to real one
            return r.text

        if r is not None:
            log.warning(
                "Thumbnail path request returned empty/miss file_id=%s frame=%s attempt=%s/%s status=%s",
                file_id,
                thumbnail_frame,
                attempt,
                attempts,
                getattr(r, "status_code", "n/a"),
            )

        if attempt < attempts:
            time.sleep(0.05)

    return ''


def resolve_thumbnail_path(file_id, frame, fingerprint=None, thumb_root=None):
    """Locate an existing thumbnail (legacy layouts + fingerprint cache)."""
    from classes.media_cache import resolve_thumbnail_path as _resolve
    return _resolve(file_id, frame, fingerprint=fingerprint, thumb_root=thumb_root)


def preferred_thumbnail_path(file_id, frame, fingerprint=None, thumb_root=None):
    """Canonical path to write a newly generated thumbnail."""
    from classes.media_cache import preferred_thumbnail_path as _preferred
    return _preferred(file_id, frame, fingerprint=fingerprint, thumb_root=thumb_root)

def _ensure_thumb_dir(thumb_path):
    parent_path = os.path.dirname(thumb_path)
    if parent_path and not os.path.exists(parent_path):
        os.makedirs(parent_path, exist_ok=True)


def _thumbnail_timestamp(file_path, thumbnail_frame):
    if thumbnail_frame <= 1:
        return 0.0
    try:
        proc = run_ffmpeg(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            rate = proc.stdout.strip()
            if "/" in rate:
                num, den = rate.split("/", 1)
                fps = float(num) / float(den)
            else:
                fps = float(rate)
            if fps > 0:
                return max(0.0, (thumbnail_frame - 1) / fps)
    except Exception:
        pass
    return 0.0


def _generate_thumbnail_ffmpeg(file_path, thumb_path, thumbnail_frame, width, height):
    """Fallback thumbnail capture when libopenshot cannot read the file."""
    if not file_path or not os.path.isfile(file_path):
        return False

    _ensure_thumb_dir(thumb_path)
    ts = _thumbnail_timestamp(file_path, thumbnail_frame)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )
    tmp_path = thumb_path + ".tmp.png"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(ts),
        "-i", file_path,
        "-frames:v", "1",
        "-vf", vf,
        tmp_path,
    ]
    try:
        proc = run_ffmpeg(cmd, capture_output=True, check=False)
        if proc.returncode != 0 or not os.path.isfile(tmp_path):
            return False
        shutil.move(tmp_path, thumb_path)
        return os.path.isfile(thumb_path)
    except Exception as exc:
        log.warning("ffmpeg thumbnail failed for %s: %s", file_path, exc)
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def _thumb_device_scale():
    """Device pixel ratio used to size thumbnails (1.0 when no app is running)."""
    try:
        return float(get_app().devicePixelRatioF()) or 1.0
    except Exception:
        return 1.0


def _reader_rotation(reader, source_path):
    """Rotation (degrees) the thumbnail must still apply, or 0.0.

    libopenshot >= 1.0 readers apply the source orientation metadata to the
    frames they decode (OpenShot #6037), so rotating again here would turn
    portrait media twice. Older readers hand out unrotated frames and the
    'rotate' metadata still has to be honoured.
    """
    applies_orientation = getattr(reader, "ApplyOrientationMetadata", None)
    if applies_orientation is not None:
        try:
            if applies_orientation():
                return 0.0
        except Exception:
            log.debug("Could not query reader orientation handling for %s", source_path, exc_info=1)
    rotate_data = None
    try:
        if reader.info.metadata.count("rotate"):
            rotate_data = reader.info.metadata["rotate"]
            return float(rotate_data)
    except ValueError as ex:
        log.warning("Could not parse rotation value %s: %s", rotate_data, ex)
    except Exception:
        log.warning("Error reading rotation metadata from %s", source_path, exc_info=1)
    return 0.0


def _thumbnail_reader_attempts():
    """Reader inspection modes to try, cheapest first.

    libopenshot >= 1.0 exposes Clip.CreateReader(path, inspect_reader): a
    lightweight reader first, then an eagerly inspected one (fixes formats
    such as webp that the quick path cannot open). Older libopenshot only has
    the fully inspected Clip reader, so a single attempt is made.
    """
    if hasattr(openshot.Clip, "CreateReader"):
        return (False, True)
    return (True,)


def _create_thumbnail_reader(source_path, inspect_reader):
    """Return (reader, owner). *owner* keeps a legacy Clip alive while used."""
    create_reader = getattr(openshot.Clip, "CreateReader", None)
    if create_reader is not None:
        return create_reader(source_path, inspect_reader), None
    clip = openshot.Clip(source_path)
    return clip.Reader(), clip


def _frame_thumbnail(frame, thumb_path, thumb_width, thumb_height, mask, overlay, rotate):
    """Write *frame* as a PNG thumbnail (cropping to fill on libopenshot >= 1.0)."""
    args = (thumb_path, thumb_width, thumb_height, mask, overlay, "#000", False, "png", 85, float(rotate or 0.0))
    scale_crop = getattr(openshot, "SCALE_CROP", None)
    if scale_crop is not None:
        try:
            frame.Thumbnail(*args, scale_crop)
            return
        except TypeError:
            # libopenshot < 1.0: Thumbnail() has no scale-mode argument
            pass
    frame.Thumbnail(*args)


def _render_with_reader(source_path, source_frame, inspect_reader, thumb_path,
                        thumb_width, thumb_height, mask, overlay):
    """Render one frame of *source_path* to *thumb_path*; True when the file exists."""
    reader = None
    owner = None
    try:
        reader, owner = _create_thumbnail_reader(source_path, inspect_reader)
        if not reader:
            raise RuntimeError("No reader available for thumbnail generation")

        if hasattr(reader, "SetMaxDecodeSize"):
            decode_width = max(thumb_width, round(thumb_width * THUMBNAIL_DECODE_SCALE))
            decode_height = max(thumb_height, round(thumb_height * THUMBNAIL_DECODE_SCALE))
            reader.SetMaxDecodeSize(decode_width, decode_height)
        reader.Open()

        rotate = _reader_rotation(reader, source_path)
        _frame_thumbnail(
            reader.GetFrame(source_frame),
            thumb_path,
            thumb_width,
            thumb_height,
            mask,
            overlay,
            rotate,
        )
        return os.path.isfile(thumb_path)
    finally:
        for handle in (reader, owner):
            if handle is not None:
                try:
                    handle.Close()
                except Exception:
                    pass


def _write_not_found_thumbnail(thumb_path, width=None, height=None):
    """Render the NotFound placeholder (SVG) at the requested thumbnail size."""
    _ensure_thumb_dir(thumb_path)
    scale = _thumb_device_scale()
    width = width or info.LIST_ICON_SIZE.width()
    height = height or info.LIST_ICON_SIZE.height()
    not_found_path = os.path.join(info.IMAGES_PATH, "NotFound.svg")
    try:
        _render_with_reader(
            not_found_path, 1, False, thumb_path,
            round(width * scale), round(height * scale), "", "")
        log.warning("Failed to generate thumbnail, using placeholder: %s", thumb_path)
    except Exception:
        log.warning("Failed to generate placeholder thumbnail from: %s", not_found_path, exc_info=1)


def GenerateThumbnail(file_path, thumb_path, thumbnail_frame, width, height, mask, overlay):
    """Create thumbnail image, and check for rotate metadata (if any)"""
    if not file_path or not os.path.isfile(file_path):
        _write_not_found_thumbnail(thumb_path, width, height)
        log.warning("Failed to generate thumbnail for missing file: %s", file_path)
        return

    _ensure_thumb_dir(thumb_path)
    scale = _thumb_device_scale()
    thumb_width = round(width * scale)
    thumb_height = round(height * scale)

    # Quick reader first, then retry with eager inspection (libopenshot >= 1.0)
    for inspect_reader in _thumbnail_reader_attempts():
        try:
            if _render_with_reader(file_path, thumbnail_frame, inspect_reader, thumb_path,
                                   thumb_width, thumb_height, mask, overlay):
                return
        except Exception as exc:
            log.warning("libopenshot thumbnail failed for %s (inspect=%s): %s",
                        file_path, inspect_reader, exc)

    if _generate_thumbnail_ffmpeg(file_path, thumb_path, thumbnail_frame, width, height):
        return

    # Any failure opening the reader (i.e. file missing or corrupt) use placeholder thumbnail
    _write_not_found_thumbnail(thumb_path, width, height)
    log.warning("Failed to generate thumbnail for missing file: %s", file_path)


class httpThumbnailServer(ThreadingMixIn, HTTPServer):
    """ This class allows to handle requests in separated threads.
        No further content needed, don't touch this. """


class httpThumbnailException(Exception):
    """ Custom exception if server cannot start. This can happen if a port does ot allow a connection
        due to another program or due to a firewall. """


class httpThumbnailServerThread(Thread):
    """ This class runs a HTTP thumbnail server inside a thread
        so we don't block the main thread with handle_request()."""

    def find_free_port(self):
        """Find the first available socket port"""
        s = socket.socket()
        s.bind(('', 0))
        socket_port = s.getsockname()[1]
        s.close()
        return socket_port

    def kill(self):
        self.running = False
        log.info('Shutting down thumbnail server: %s' % str(self.server_address))
        self.thumbServer.shutdown()

    def run(self):
        log.info("Starting thumbnail server listening on %s", self.server_address)
        self.running = True
        self.thumbServer.serve_forever(0.5)

    def __init__(self):
        """ Attempt to find an available port, and bind to that port for our thumbnail HTTP server.
            If not able to bind to localhost or a specific port, return an exception (and quit OpenShot). """
        Thread.__init__(self)
        self.daemon = True
        self.server_address = None
        self.running = False
        self.thumbServer = None

        exceptions = []
        initial_port = self.find_free_port()
        for attempt in range(3):
            try:
                # Configure server address and port for our HTTP thumbnail server
                self.server_address = ('127.0.0.1', initial_port + attempt)
                log.debug("Attempting to start thumbnail server listening on port %s", self.server_address)
                self.thumbServer = httpThumbnailServer(self.server_address, httpThumbnailHandler)
                self.thumbServer.daemon_threads = True
                exceptions.clear()
                break

            except Exception as ex:
                # Silently track each exception
                # Return full list of exceptions (from each attempt, if no attempt is successful)
                exceptions.append(f"{self.server_address} {ex}")

        if exceptions:
            # Return full list of attempts + exceptions if we failed to make a connection
            raise httpThumbnailException("\n".join(exceptions))


class httpThumbnailHandler(BaseHTTPRequestHandler):
    """ This class handles HTTP requests to the HTTP thumbnail server above."""

    def log_message(self, msg_format, *args):
        """ Log message from HTTPServer """
        log.info(msg_format % args)

    def log_error(self, msg_format, *args):
        """ Log error from HTTPServer """
        log.warning(msg_format % args)

    def do_GET(self):
        """ Process each GET request and return a value (image or file path)"""
        mask_path = os.path.join(info.IMAGES_PATH, "mask.png")

        # Parse URL
        url_output = REGEX_THUMBNAIL_URL.match(self.path)
        if url_output and len(url_output.groups()) == 4:
            # Path is expected to have 3 matched components (third is optional though)
            #   /thumbnails/FILE-ID/FRAME-NUMBER/   or
            #   /thumbnails/FILE-ID/FRAME-NUMBER/path/  or
            #   /thumbnails/FILE-ID/FRAME-NUMBER/no-cache/  or
            #   /thumbnails/FILE-ID/FRAME-NUMBER/path/no-cache/
            self.send_response_only(200)
        else:
            self.send_error(404)
            return

        # Get URL parts
        file_id = url_output.group('file_id')
        file_frame = int(url_output.group('file_frame'))
        only_path = url_output.group('only_path')
        no_cache = url_output.group('no_cache')

        try:
            # Look up file data
            file = File.get(id=file_id)

            # Ensure file location is an absolute path
            file_path = file.absolute_path()
        except AttributeError:
            # Couldn't match file ID
            log.debug("No ID match, returning 404")
            self.send_error(404)
            return

        # Send headers
        if not only_path:
            self.send_header('Content-type', 'image/png')
        else:
            self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()

        # Locate thumbnail (fingerprint cache + legacy layouts)
        fingerprint = None
        try:
            fingerprint = file.data.get("fingerprint") if file and isinstance(file.data, dict) else None
        except Exception:
            fingerprint = None
        thumb_path = resolve_thumbnail_path(file_id, file_frame, fingerprint=fingerprint)
        if not thumb_path and not no_cache:
            # Serve the nearest pre-warmed thumbnail (Optimize Preview writes
            # thumbnails on a coarse grid) instead of decoding this exact frame.
            rounded_frame = RoundFrameToThumbnailGrid(file_frame, _file_fps(file))
            if rounded_frame != file_frame:
                thumb_path = resolve_thumbnail_path(file_id, rounded_frame, fingerprint=fingerprint)
        if not thumb_path:
            thumb_path = preferred_thumbnail_path(file_id, file_frame, fingerprint=fingerprint)

        if not os.path.exists(thumb_path) or no_cache:
            # Generate thumbnail (since we can't find it)
            thumb_path = preferred_thumbnail_path(file_id, file_frame, fingerprint=fingerprint)

            # Determine if video overlay should be applied to thumbnail
            overlay_path = ""
            if file.data["media_type"] == "video":
                overlay_path = os.path.join(info.IMAGES_PATH, "overlay.png")

            # Create thumbnail image (sized like the project files list icons)
            thumb_width = info.LIST_ICON_SIZE.width()
            thumb_height = info.LIST_ICON_SIZE.height()
            GenerateThumbnail(
                file_path,
                thumb_path,
                file_frame,
                thumb_width,
                thumb_height,
                mask_path,
                overlay_path)

        # Send message back to client
        if os.path.exists(thumb_path):
            if only_path:
                self.wfile.write(bytes(thumb_path, "utf-8"))
            else:
                with open(thumb_path, 'rb') as f:
                    self.wfile.write(f.read())

        # Pause processing of request (since we don't currently use thread pooling, this allows
        # the threads to be processed without choking the CPU as much
        # TODO: Make HTTPServer work with a limited thread pool and remove this sleep() hack.
        time.sleep(0.01)
