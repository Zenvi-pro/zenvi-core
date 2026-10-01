"""
 @file
 @brief Project Files rules shared by the Files dock, File Properties and the agent tools.

 * Image sequences: detect a numbered frame set from one frame (no prompt), count
   its frames, find the first frame in a folder.
 * File Properties: rescale an image sequence to a new frame rate, set a file's in/out
   frames, relink to a new path without losing the Zenvi keys, then save the file and
   refresh the clips that use it.
 * Remove from Project: cancel the file's jobs, delete its clips, delete the file.

 Detection and counting touch the file system (any thread); the save/remove
 helpers mutate the project through ``classes.query`` (GUI thread), so callers
 decide the undo grouping.
"""

import copy
import glob
import os
import re
from fractions import Fraction

from classes.logger import log

def get_app():
    """The running app (looked up per call, so tests can stand one in)."""
    from classes import app
    return app.get_app()


SEQUENCE_EXTENSIONS = ["png", "jpg", "jpeg", "tif", "svg"]
_SEQUENCE_NAME_RE = re.compile(r"(.*[^\d])?(0*)(\d+)\.(%s)$" % "|".join(SEQUENCE_EXTENSIONS), re.I)

# Frame rates people say as decimals.
_NTSC_RATES = {23.976: (24000, 1001), 23.98: (24000, 1001), 29.97: (30000, 1001),
               47.952: (48000, 1001), 59.94: (60000, 1001), 119.88: (120000, 1001)}


# ---------------------------------------------------------------------------
# Image sequences
# ---------------------------------------------------------------------------

def detect_image_sequence(file_path):
    """Sequence details when *file_path* is one frame of a numbered image set, else None.

    The detection half of ``FilesModel.get_image_sequence_details`` (which adds the
    once-per-folder rule and the "Import as sequence?" prompt). Returns
    {folder_path, base_name, fixlen, digits, extension, pattern, path} where path is
    the libopenshot pattern (``frame_%04d.png``).
    """
    dir_name, file_name = os.path.split(str(file_path or ""))
    match = _SEQUENCE_NAME_RE.findall(file_name)
    if not match:
        return None

    base_name = match[0][0]
    fixlen = match[0][1] > ""
    number = int(match[0][2])
    digits = len(match[0][1] + match[0][2])
    extension = match[0][3]
    full_base_name = os.path.join(dir_name, base_name)

    # Check for images which the file names have the different length
    fixlen = fixlen or not (
        glob.glob("%s%s.%s" % (glob.escape(full_base_name), "[0-9]" * (digits + 1), extension))
        or glob.glob("%s%s.%s" % (glob.escape(full_base_name), "[0-9]" * ((digits - 1) if digits > 1 else 3),
                                  extension))
    )

    # Check for previous or next image
    for x in range(max(0, number - 100), min(number + 101, 50000)):
        if x != number and os.path.exists(
                "%s%s.%s" % (full_base_name, str(x).rjust(digits, "0") if fixlen else str(x), extension)):
            break  # found one!
    else:
        return None

    zero_pattern = "%%0%sd" % digits if fixlen else "%d"
    pattern = "%s%s.%s" % (base_name, zero_pattern, extension)
    return {
        "folder_path": dir_name,
        "base_name": base_name,
        "fixlen": fixlen,
        "digits": digits,
        "extension": extension,
        "pattern": pattern,
        "path": os.path.join(dir_name, pattern),
    }


def sequence_frame_numbers(details):
    """Sorted frame numbers on disk for a detect_image_sequence() result."""
    folder = details.get("folder_path") or ""
    base = details.get("base_name") or ""
    ext = details.get("extension") or ""
    if details.get("fixlen"):
        rx = re.compile(r"^%s(\d{%d})\.%s$" % (re.escape(base), int(details.get("digits") or 1), re.escape(ext)))
    else:
        rx = re.compile(r"^%s(\d+)\.%s$" % (re.escape(base), re.escape(ext)))
    numbers = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for name in names:
        m = rx.match(name)
        if m:
            numbers.append(int(m.group(1)))
    return sorted(numbers)


def first_sequence_frame(folder):
    """(first_frame_path, details) of the largest numbered image set in *folder*, or (None, None)."""
    try:
        names = sorted(n for n in os.listdir(folder) if not n.startswith("."))
    except OSError:
        return None, None
    best = (0, None, None)
    seen_patterns = set()
    for name in names:
        if not _SEQUENCE_NAME_RE.match(name):
            continue
        details = detect_image_sequence(os.path.join(folder, name))
        if not details or details["path"] in seen_patterns:
            continue
        seen_patterns.add(details["path"])
        count = len(sequence_frame_numbers(details))
        if count > best[0]:
            best = (count, os.path.join(folder, name), details)
    return best[1], best[2]


def fps_fraction(value):
    """A frame rate given as a number (24, 23.976, 29.97, 12.5) -> (num, den)."""
    rate = float(value)
    if rate <= 0:
        raise ValueError("frame rate must be positive")
    for known, frac in _NTSC_RATES.items():
        if abs(rate - known) < 0.005:
            return frac
    f = Fraction(rate).limit_denominator(1001)
    return f.numerator, f.denominator


# ---------------------------------------------------------------------------
# File Properties (the dialog's accept, minus the widgets)
# ---------------------------------------------------------------------------

def is_image_sequence(file_data):
    return "%" in str((file_data or {}).get("path") or "")


def apply_sequence_fps(file_data, fps_num, fps_den):
    """Play an image sequence at a new frame rate: fps, timebase, duration and in/out rescale."""
    fps = file_data.get("fps") or {"num": 30, "den": 1}
    original = float(fps["num"]) / float(fps["den"])
    new = float(fps_num) / float(fps_den)
    file_data["fps"] = {"num": int(fps_num), "den": int(fps_den)}
    file_data["video_timebase"] = {"num": int(fps_den), "den": int(fps_num)}
    fps_diff = original / new
    file_data["duration"] = float(file_data.get("duration") or 0.0) * fps_diff
    if "start" in file_data:
        file_data["start"] = float(file_data["start"]) * fps_diff
    if "end" in file_data:
        file_data["end"] = float(file_data["end"]) * fps_diff
    return fps_diff


def set_in_out_frames(file_data, start_frame, end_frame):
    """File in/out from 1-based frame numbers (end inclusive), as the dialog's Start/End Frame."""
    fps = file_data.get("fps") or {"num": 30, "den": 1}
    fps_float = float(fps["num"]) / float(fps["den"])
    file_data["start"] = (int(start_frame) - 1) / fps_float
    # End frames are inclusive, so convert to the time *after* the last frame
    file_data["end"] = int(end_frame) / fps_float


def relinked_file_data(old_data, reader_data, media_type, fingerprint=None):
    """The file record after pointing it at new media.

    The reader's keys (path, duration, size, fps, codecs...) replace the old ones;
    everything Zenvi keeps on a file (name, tags, AI metadata, sub-clip in/out)
    is kept. In/out are clamped to the new media's duration. *fingerprint* is the
    new media's (classes.media_fingerprint), or None when it has none: the
    optimized preview is kept only when it proves the media is the same content,
    since preview playback would otherwise keep showing the old footage.
    """
    data = copy.deepcopy(old_data or {})
    data.update(copy.deepcopy(reader_data or {}))
    data["id"] = (old_data or {}).get("id", data.get("id"))
    old_fp = (old_data or {}).get("fingerprint")
    old_sha = old_fp.get("sha256") if isinstance(old_fp, dict) else None
    if fingerprint:
        data["fingerprint"] = fingerprint
    else:
        data.pop("fingerprint", None)
    if not (old_sha and fingerprint and old_sha == fingerprint.get("sha256")):
        data.pop("proxy_reader", None)
    if media_type:
        data["media_type"] = media_type
    duration = float(data.get("duration") or 0.0)
    if "end" in data and duration > 0 and float(data["end"]) > duration:
        data["end"] = duration
    if "start" in data and "end" in data and float(data["start"]) >= float(data["end"]):
        data.pop("start", None)
        data.pop("end", None)
    return data


def save_file_and_sync_clips(file_obj, removed_keys=()):
    """Save a changed file and update every clip that uses it (reader, duration, clamped end).

    Project updates merge into the stored record, so keys the new record no longer
    has (a cleared in/out, a stale fingerprint) are deleted explicitly via
    *removed_keys*. Returns the ids of the clips updated. Joins the caller's undo
    transaction.
    """
    from classes.query import Clip

    file_obj.save()
    for key in removed_keys or ():
        if file_obj.key:
            get_app().updates.delete(list(file_obj.key) + [key])
    window = getattr(get_app(), "window", None)
    try:
        window.FileUpdated.emit(file_obj.id)
    except Exception:
        log.debug("FileUpdated signal unavailable", exc_info=True)

    fps = file_obj.data.get("fps") or {"num": 30, "den": 1}
    fps_float = float(fps["num"]) / float(fps["den"] or 1)
    updated = []
    for clip in Clip.filter(file_id=file_obj.id):
        clip.data["reader"] = copy.deepcopy(file_obj.data)
        clip.data["duration"] = file_obj.data["duration"]
        if clip.data["end"] > clip.data["duration"]:
            clip.data["end"] = clip.data["duration"]
        clip.save()
        updated.append(clip.id)
        try:
            # Update the timeline thumbnail
            window.ThumbnailUpdated.emit(clip.id, (clip.data["start"] * fps_float) + 1)
        except Exception:
            log.debug("ThumbnailUpdated signal unavailable", exc_info=True)
    return updated


# ---------------------------------------------------------------------------
# Remove from Project
# ---------------------------------------------------------------------------

def remove_files_from_project(files):
    """Remove files and every clip that uses them (Project Files > Remove from Project).

    Cancels the files' generation and optimized-preview jobs first. Never touches the
    media on disk. Returns (removed_file_ids, removed_clip_ids). Joins the caller's
    undo transaction.
    """
    from classes.query import Clip

    window = getattr(get_app(), "window", None)
    removed_files, removed_clips = [], []
    for f in files:
        if not f:
            continue
        file_id = f.data.get("id") or f.id

        # Cancel queued/running generation and optimize jobs tied to this file
        queue = getattr(window, "generation_queue", None)
        if queue:
            queue.cancel_jobs_for_file(file_id)
        proxy = getattr(window, "proxy_service", None)
        if proxy and proxy.get_active_job_for_file(file_id):
            proxy.cancel_job(file_id)

        for c in Clip.filter(file_id=file_id):
            # Clear selected clips (and update properties and transform handles - to prevent crashes)
            window.removeSelection(c.id, "clip")
            window.emit_selection_signal()
            window.show_property_timeout()
            removed_clips.append(c.id)
            c.delete()

        # Remove file (after clips are deleted)
        removed_files.append(file_id)
        f.delete()
    return removed_files, removed_clips
