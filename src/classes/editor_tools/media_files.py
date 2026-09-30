"""Project Files: find, rename/tag/relink, image sequences, removal, sub-clips, optimized previews.

Workstream: media-files. Transitions live in ``media_files_transitions``.

The rules are the editor's own: ``classes.project_files`` holds the File
Properties "accept" core, image-sequence detection and Remove from Project
(shared with the dialog and the Files dock menu); optimized previews go
through the window's ``ProxyService`` like the Preview > Optimize menu.

Threading: listing is read-only (worker thread). Relinking, sequence import,
file removal and proxies are background-safe: disk checks and media probes run
on the worker thread and only the project edit hops to the GUI thread, so each
call is still one undo step. Sub-clips are a plain project edit (GUI thread).
"""

from __future__ import annotations

import copy
import glob
import os
import time

from classes import project_files
from classes.editor_tools._base import (
    ToolError,
    array,
    boolean,
    enum,
    get_app,
    integer,
    mapping,
    nullable,
    number,
    obj,
    ok,
    on_main,
    project_fps,
    refresh_preview,
    string,
    ui_track_number,
)
from classes.editor_tools._registry import editor_tool
from classes.logger import log

# Long GUI hops: probing a media file / an image sequence can take a while on slow volumes.
HOP_TIMEOUT = 120
FILE_KINDS = ("video", "audio", "image", "image_sequence", "title")
MAX_FOLDER_SCAN = 20000

FILE_TARGET = {
    "file_ids": array({"type": "string"},
                      "Project file ids (file_id / media_bin_file_id from list_project_files_tool)."),
    "file_query": string("Name one file instead of ids: its name, file name or a tag "
                         "('intro.mp4', 'the drone shot').", ""),
}


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------

def _files():
    from classes.query import File
    return list(File.filter())


def _abs_path(path):
    from classes.path_utils import absolute_media_path
    try:
        return absolute_media_path(path) or str(path or "")
    except Exception:
        return str(path or "")


def _base_name(data):
    return os.path.basename(str(data.get("path") or ""))


def _display_name(data):
    return str(data.get("name") or _base_name(data) or data.get("id") or "?")


def tags_of(data) -> list:
    """The file's tags (a comma separated string in project data) as a list."""
    out, seen = [], set()
    for part in str((data or {}).get("tags") or "").split(","):
        tag = part.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def _tags_string(tags) -> str:
    return ", ".join(tags)


def kind_of(data) -> str:
    """video | audio | image | image_sequence | title (an SVG title)."""
    path = str(data.get("path") or "")
    if "%" in path:
        return "image_sequence"
    if path.lower().endswith(".svg") and "emoji" not in path.lower():
        return "title"
    media_type = str(data.get("media_type") or "").lower()
    return media_type if media_type in ("video", "audio", "image") else "video"


def media_missing(data) -> bool:
    """True when the file's media is not on disk (file system read)."""
    path = _abs_path(data.get("path"))
    if not path:
        return True
    if "%" in path:
        details = {"folder_path": os.path.dirname(path)}
        folder = details["folder_path"]
        if not os.path.isdir(folder):
            return True
        stem = os.path.basename(path).split("%", 1)[0]
        ext = os.path.splitext(path)[1]
        return not glob.glob(os.path.join(glob.escape(folder), glob.escape(stem) + "*" + ext))
    return not os.path.exists(path)


def _clip_usage() -> dict:
    """file_id -> [timeline clip ids], in timeline order."""
    from classes.query import Clip
    usage = {}
    clips = sorted(Clip.filter(), key=lambda c: (float(c.data.get("position") or 0.0),
                                                 int(c.data.get("layer") or 0)))
    for c in clips:
        usage.setdefault(str(c.data.get("file_id") or ""), []).append(c.id)
    return usage


def _proxy_service():
    return getattr(get_app().window, "proxy_service", None)


def _proxy_state(f) -> str:
    service = _proxy_service()
    try:
        state = service.get_proxy_state(f) if service else "none"
    except Exception:
        log.debug("proxy state unavailable", exc_info=True)
        return "none"
    return state if isinstance(state, str) else "none"


def _fps_parts(data):
    fps = data.get("fps") or {}
    try:
        num, den = int(fps.get("num") or 0), int(fps.get("den") or 1)
    except (TypeError, ValueError):
        num, den = 0, 1
    return num, den or 1


def _source_window(data):
    from classes.clip_placement import source_window_for_file
    return source_window_for_file(data)


# libopenshot / FFmpeg channel layout masks.
_LAYOUT_NAMES = {0: "unknown", 3: "stereo", 4: "mono", 7: "surround_3", 11: "2.1", 51: "quad", 63: "5.1",
                 1551: "5.1", 1599: "7.1"}


def describe_file(f, usage=None, full=False, check_missing=True) -> dict:
    """What the model needs about one project file."""
    data = f.data if isinstance(f.data, dict) else {}
    clip_ids = (usage or {}).get(str(f.id), [])
    start, end = _source_window(data)
    kind = kind_of(data)
    still = kind in ("image", "title")
    out = {
        "file_id": str(f.id),
        "name": _display_name(data),
        "file_name": _base_name(data),
        "kind": kind,
        # A still has no length of its own (libopenshot reports 3600 s); on the timeline it
        # lasts the Image Length preference unless trimmed.
        "duration": None if still else round(max(0.0, end - start), 3),
        "tags": tags_of(data),
        "clip_count": len(clip_ids),
    }
    if ("start" in data or "end" in data) and not still:
        out["source_in"] = round(start, 3)
        out["source_out"] = round(end, 3)
    if clip_ids:
        out["timeline_clip_ids"] = clip_ids[:20]
    if data.get("zenvi_subclip") or data.get("parent_file_id"):
        out["subclip"] = True
        out["parent_file_id"] = str(data.get("parent_file_id") or "")
    if check_missing:
        out["missing"] = media_missing(data)
    if out["kind"] == "video":
        out["optimized_preview"] = _proxy_state(f)
    if full:
        num, den = _fps_parts(data)
        out.update({
            "path": _abs_path(data.get("path")),
            "media_duration": None if still else round(float(data.get("duration") or 0.0), 3),
            "width": data.get("width"),
            "height": data.get("height"),
            "fps": f"{num}/{den}",
            "fps_value": round(num / den, 3) if num else None,
            "frames": int(data.get("video_length") or 0) if data.get("video_length") else None,
            "has_video": bool(data.get("has_video")),
            "has_audio": bool(data.get("has_audio")),
            "video_codec": data.get("vcodec") or "",
            "audio_codec": data.get("acodec") or "",
            "sample_rate": data.get("sample_rate"),
            "channels": data.get("channels"),
            "channel_layout": (_LAYOUT_NAMES.get(int(data.get("channel_layout") or 0), str(data.get("channel_layout")))
                               if data.get("has_audio") else "none"),
            "video_bit_rate": data.get("video_bit_rate"),
            "audio_bit_rate": data.get("audio_bit_rate"),
            "interlaced": bool(data.get("interlaced_frame")),
            "display_ratio": "{}:{}".format((data.get("display_ratio") or {}).get("num", ""),
                                            (data.get("display_ratio") or {}).get("den", "")),
            "pixel_ratio": "{}:{}".format((data.get("pixel_ratio") or {}).get("num", ""),
                                          (data.get("pixel_ratio") or {}).get("den", "")),
            "file_size": data.get("file_size"),
            "analyzed": bool((data.get("ai_metadata") or {}).get("analyzed"))
            if isinstance(data.get("ai_metadata"), dict) else False,
        })
        proxy = data.get("proxy_reader")
        if isinstance(proxy, dict) and proxy.get("path"):
            out["optimized_preview_path"] = _abs_path(proxy.get("path"))
    else:
        out["path"] = _abs_path(data.get("path"))
    return out


def _query_score(f, q_lower, words):
    data = f.data if isinstance(f.data, dict) else {}
    name = _display_name(data).lower()
    base = _base_name(data).lower()
    stem = os.path.splitext(base)[0]
    tags = [t.lower() for t in tags_of(data)]
    if q_lower in (str(f.id).lower(), name, base, stem) or q_lower in tags:
        return 3
    if q_lower in name or q_lower in base:
        return 2
    corpus = " ".join([name, base] + tags)
    if words and all(w in corpus for w in words):
        return 1
    return 0


def seed_missing_keys(obj, defaults: dict) -> None:
    """Write absent keys with their neutral default, outside undo history.

    Project updates merge into the stored record and an undo merges the old
    record back, so a key an edit ADDS survives its undo (a renamed file would
    keep the new name). Seeding the key first with a value that looks the same
    as "absent" (a file's name = its file name, no tags = "") lets the edit's
    one undo step restore it exactly. Call after validation, right before the edit.
    """
    missing = {k: v for k, v in defaults.items() if k not in obj.data}
    if not missing or not obj.key:
        return
    app = get_app()
    from classes.editor_tools._base import th
    with th()._ignore_history(app):
        app.updates.update(list(obj.key), copy.deepcopy(missing))
    obj.data.update(copy.deepcopy(missing))


def file_key_defaults(data) -> dict:
    """Neutral values for the keys the file tools may add to a file record."""
    start, end = _source_window(data) if ("start" in data or "end" in data) else (
        0.0, float(data.get("duration") or 0.0))
    return {"name": _base_name(data), "tags": "", "start": start, "end": end}


def resolve_files(file_ids=None, file_query="") -> list:
    """Files by id, or the one file a description names. Raises ToolError."""
    from classes.query import File
    if file_ids:
        out, seen = [], set()
        for fid in file_ids:
            fid = str(fid).strip()
            f = File.get(id=fid)
            if not f:
                raise ToolError(f"no project file with id={fid!r} (list_project_files_tool lists them)")
            if f.id not in seen:
                seen.add(f.id)
                out.append(f)
        return out
    q = str(file_query or "").strip()
    if not q:
        raise ToolError("say which file: file_ids or file_query")
    words = q.lower().split()
    scored = [(s, f) for f in _files() for s in [_query_score(f, q.lower(), words)] if s]
    if not scored:
        raise ToolError(f"no project file matches {q!r} (list_project_files_tool lists them)")
    best = max(s for s, _ in scored)
    top = [f for s, f in scored if s == best]
    if len(top) > 1:
        names = ", ".join(f"{_display_name(f.data)!r} (file_id={f.id})" for f in top[:8])
        raise ToolError(f"{q!r} matches {len(top)} files: {names}; pass file_ids")
    return top


# ---------------------------------------------------------------------------
# list_project_files_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "list_project_files_tool",
    label="List project files",
    schema=obj({
        "media_type": enum(["", *FILE_KINDS],
                           "Only this kind: video, audio, image, image_sequence or title (SVG titles). "
                           "'' = every kind.", ""),
        "query": string("Search text matched against the file name, display name and tags, like the Project "
                        "Files search box (every word must match). '' = no text filter.", ""),
        "tags": array({"type": "string"}, "Only files that have ALL of these tags (case-insensitive)."),
        "usage": enum(["", "unused", "used"],
                      "'unused' = files no timeline clip uses (what 'remove unused files' removes), "
                      "'used' = files on the timeline, '' = both.", ""),
        "missing_only": boolean("Only files whose media is missing on disk (moved or deleted; relink them "
                                "with update_project_files_tool).", False),
        "file_ids": array({"type": "string"}, "Only these file ids (e.g. to read full media info)."),
        "detail": enum(["summary", "full"],
                       "'full' adds media info: size, fps, frames, codecs, sample rate, channels, bit rates, "
                       "interlacing, aspect ratios, file size, optimized-preview path.", "summary"),
        "limit": integer("Most files to return.", 100, minimum=1, maximum=1000),
    }),
    read_only=True,
    covers=("files.list_filter", "files.properties"),
)
def list_project_files(media_type="", query="", tags=None, usage="", missing_only=False, file_ids=None,
                       detail="summary", limit=100):
    """List or filter the Project Files (the media bin): by kind, name/tag search, usage and missing media.

    Use it to answer "what footage do I have", "show my b-roll", "which files are unused",
    "which files are missing", or to get file ids and full media info (resolution, fps,
    duration, codecs, audio channels) before editing. Each file reports its kind, duration,
    tags, how many timeline clips use it (with their ids), whether its media is missing on
    disk, and for videos the optimized-preview state (none/queued/running/ready/missing).
    Sub-clips made with Split Clip / create_subclips_tool are listed too (with source_in/out).
    Read-only. Example: usage="unused" before remove_files_from_project_tool(scope="unused").
    """
    usage_map = _clip_usage()
    wanted_tags = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]
    q = str(query or "").strip().lower()
    words = q.split()
    only_ids = {str(i).strip() for i in (file_ids or []) if str(i).strip()}
    files = _files()
    if only_ids:
        known = {str(f.id) for f in files}
        unknown = sorted(only_ids - known)
        if unknown:
            raise ToolError(f"no project file with id(s) {', '.join(unknown)}")
    matched = []
    for f in files:
        data = f.data if isinstance(f.data, dict) else {}
        if only_ids and str(f.id) not in only_ids:
            continue
        if media_type and kind_of(data) != media_type:
            continue
        if words and _query_score(f, q, words) == 0:
            continue
        if wanted_tags:
            have = {t.lower() for t in tags_of(data)}
            if not all(t in have for t in wanted_tags):
                continue
        used = bool(usage_map.get(str(f.id)))
        if (usage == "unused" and used) or (usage == "used" and not used):
            continue
        if missing_only and not media_missing(data):
            continue
        matched.append(f)
    shown = [describe_file(f, usage_map, full=(detail == "full")) for f in matched[:int(limit)]]
    filters = [x for x in (media_type, q and f"'{q}'", wanted_tags and f"tags {wanted_tags}", usage,
                           missing_only and "missing") if x]
    summary = (f"{len(matched)} of {len(files)} project files match" + (f" ({', '.join(map(str, filters))})"
                                                                         if filters else "") + ".")
    if len(matched) > len(shown):
        summary += f" Showing the first {len(shown)}."
    return ok(summary, files=shown, matched=len(matched), total_files=len(files))


# ---------------------------------------------------------------------------
# update_project_files_tool
# ---------------------------------------------------------------------------

def _probe_reader(path):
    """libopenshot's reader JSON for a media path (image sequence pattern allowed)."""
    import json
    import openshot

    clip = openshot.Clip(path)
    try:
        if not clip or clip.info.duration <= 0.0:
            raise ToolError(f"libopenshot cannot read {path!r} as media")
        return json.loads(clip.Reader().Json())
    finally:
        try:
            clip.Close()
        except Exception:
            pass


def _find_in_folder(folder, file_name):
    """First file called *file_name* under *folder* (breadth-first, bounded)."""
    direct = os.path.join(folder, file_name)
    if os.path.isfile(direct):
        return direct
    scanned = 0
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        scanned += len(names)
        if file_name in names:
            return os.path.join(root, file_name)
        if scanned > MAX_FOLDER_SCAN:
            break
    return None


def _relink_target(data, path="", search_folder=""):
    """(new_media_path, sequence_details_or_None) for one file, or raise ToolError."""
    was_sequence = project_files.is_image_sequence(data)
    old_path = _abs_path(data.get("path"))
    if search_folder:
        folder = os.path.expanduser(search_folder)
        if not os.path.isdir(folder):
            raise ToolError(f"search_folder {search_folder!r} is not a folder")
        if was_sequence:
            first = None
            stem = os.path.basename(old_path).split("%", 1)[0]
            for root, _dirs, names in os.walk(folder):
                cands = sorted(n for n in names if n.startswith(stem))
                for n in cands:
                    details = project_files.detect_image_sequence(os.path.join(root, n))
                    if details and os.path.basename(details["path"]) == os.path.basename(old_path):
                        first = os.path.join(root, n)
                        break
                if first:
                    break
            if not first:
                return None, None
            return first, project_files.detect_image_sequence(first)
        found = _find_in_folder(folder, os.path.basename(old_path))
        return (found, None) if found else (None, None)

    new_path = os.path.abspath(os.path.expanduser(str(path)))
    if was_sequence:
        if os.path.isdir(new_path):
            first, details = project_files.first_sequence_frame(new_path)
            if not first:
                raise ToolError(f"no numbered image sequence in {path!r}")
            return first, details
        details = project_files.detect_image_sequence(new_path)
        if not details:
            raise ToolError(f"{path!r} is not a frame of a numbered image sequence (this file is a sequence)")
        return new_path, details
    if not os.path.isfile(new_path):
        raise ToolError(f"no file at {path!r}")
    return new_path, None


def _relinked(data, media_path, details):
    """New file data after pointing *data* at *media_path* (probes the media; worker thread)."""
    from classes.image_types import get_media_type
    from classes.media_fingerprint import fingerprint

    probe_path = details["path"] if details else media_path
    if probe_path.lower().endswith(".svg"):
        # Qt renders SVGs (fonts included): only on the GUI thread.
        reader = on_main(_probe_reader, probe_path, timeout=HOP_TIMEOUT)
    else:
        reader = _probe_reader(probe_path)
    media_type = "video" if details else get_media_type(reader)
    new = project_files.relinked_file_data(data, reader, media_type)
    if details:
        # The sequence reader counts 25 fps; keep the frame rate the file had.
        num, den = _fps_parts(data)
        start, end = new.pop("start", None), new.pop("end", None)
        new["fps"] = dict(reader.get("fps") or {"num": 25, "den": 1})
        project_files.apply_sequence_fps(new, num or 25, den or 1)
        if start is not None and end is not None:
            new["start"], new["end"] = start, min(end, new["duration"])
    old_fp = (data.get("fingerprint") or {}).get("sha256") if isinstance(data.get("fingerprint"), dict) else None
    fp = fingerprint(media_path) if not details else None
    if fp:
        new["fingerprint"] = fp
    else:
        new.pop("fingerprint", None)
    same = None if not (old_fp and fp) else (old_fp == fp.get("sha256"))
    return new, same


@editor_tool(
    "update_project_files_tool",
    label="Update project files",
    schema=obj({
        **FILE_TARGET,
        "name": string("New display name (one file). '' = keep.", ""),
        "tags": nullable(array({"type": "string"}, "Replace the file(s) tags with exactly these. "
                                                   "[] clears them; leave out to keep.")),
        "add_tags": array({"type": "string"}, "Tags to add to every chosen file ('b-roll', 'intro')."),
        "remove_tags": array({"type": "string"}, "Tags to remove from every chosen file."),
        "path": string("Relink one file to this media path (the file moved or was replaced). For an image "
                       "sequence: a frame of the new sequence or its folder. '' = keep.", ""),
        "search_folder": string("Relink every chosen file to the file with the same name found in this folder "
                                "(searched recursively). '' = no search.", ""),
        "source_start_seconds": nullable(number("New in-point, in seconds of the source media (File "
                                                "Properties > Start Frame). One file.", minimum=0)),
        "source_end_seconds": nullable(number("New out-point, in seconds of the source media (File "
                                              "Properties > End Frame). One file.", minimum=0)),
        "reset_in_out": boolean("Clear the file's in/out so it spans the whole media again.", False),
        "fps": number("New frame rate for IMAGE SEQUENCES only (e.g. 24, 12, 29.97); rescales its duration. "
                      "0 = keep.", 0.0, minimum=0, maximum=240),
        "rename_clips": boolean("With name: also relabel the timeline clips that use the file.", False),
    }),
    background_safe=True,
    covers=("files.properties",),
)
def update_project_files(file_ids=None, file_query="", name="", tags=None, add_tags=None, remove_tags=None,
                         path="", search_folder="", source_start_seconds=None, source_end_seconds=None,
                         reset_in_out=False, fps=0.0, rename_clips=False):
    """Change project files (File Properties): rename, tag, relink moved media, set in/out, set sequence fps.

    Use for "rename the clip to Intro" (with rename_clips=true the timeline clips get the label
    too), "tag these as b-roll" (add_tags on many file_ids at once), "I moved my footage to
    ~/Movies/shoot, fix the missing files" (search_folder, or path for one file), "only use
    0:05-0:20 of this file" (source_start/end_seconds, source seconds), "play this image
    sequence at 12 fps" (fps; image sequences only). Timeline clips that use a changed file
    are refreshed (their end is clamped to the new length, as the dialog does). Relinking
    keeps the name, tags, AI metadata, in/out and optimized preview, and reports
    same_content=false when the new media is a different file. One undo step. Does not
    trim timeline clips (that is a timeline edit) and never deletes anything.
    Example: file_ids=["A1","B2"], add_tags=["b-roll"].
    """
    from classes.query import Clip

    files = resolve_files(file_ids, file_query)
    name = str(name or "").strip()
    add = [str(t).strip() for t in (add_tags or []) if str(t).strip()]
    drop = {str(t).strip().lower() for t in (remove_tags or []) if str(t).strip()}
    replace = None if tags is None else [str(t).strip() for t in tags if str(t).strip()]
    in_out = source_start_seconds is not None or source_end_seconds is not None
    relink = bool(str(path or "").strip() or str(search_folder or "").strip())
    if not (name or add or drop or replace is not None or relink or in_out or reset_in_out or fps):
        raise ToolError("nothing to change: pass name, tags/add_tags/remove_tags, path or search_folder, "
                        "source_start/end_seconds, reset_in_out or fps")
    single = [label for label, used in (("name", name), ("path", str(path or "").strip()),
                                        ("source_start/end_seconds", in_out)) if used]
    if single and len(files) > 1:
        raise ToolError(f"{' and '.join(single)} apply to one file; {len(files)} were chosen")
    if path and search_folder:
        raise ToolError("pass path or search_folder, not both")
    if in_out and reset_in_out:
        raise ToolError("pass source_start/end_seconds or reset_in_out, not both")
    if rename_clips and not name:
        raise ToolError("rename_clips needs a name")
    if fps:
        not_seq = [_display_name(f.data) for f in files if not project_files.is_image_sequence(f.data)]
        if not_seq:
            raise ToolError(f"only image sequences can change frame rate; {', '.join(not_seq)} "
                            "keep the rate their media was recorded at")
        fps_num, fps_den = project_files.fps_fraction(fps)

    # Relink targets and probes (disk + libopenshot) off the GUI thread.
    relinked, not_found = {}, []
    if relink:
        for f in files:
            media_path, details = _relink_target(f.data, path=path, search_folder=search_folder)
            if not media_path:
                not_found.append(_display_name(f.data))
                continue
            relinked[f.id] = (media_path,) + _relinked(f.data, media_path, details)
        if not relinked:
            raise ToolError(f"no media named like {', '.join(not_found)} under {search_folder!r}")

    # Build every new record, then decide whether anything changes (a no-op adds no undo step).
    plans = []
    for f in files:
        old = copy.deepcopy(f.data)
        new = copy.deepcopy(relinked[f.id][1]) if f.id in relinked else copy.deepcopy(f.data)
        if name:
            new["name"] = name
        if replace is not None or add or drop:
            current = replace if replace is not None else tags_of(new)
            merged, seen = [], set()
            for t in list(current) + add:
                if t.lower() not in seen and t.lower() not in drop:
                    seen.add(t.lower())
                    merged.append(t)
            if merged or "tags" in new:
                new["tags"] = _tags_string(merged)
        if fps:
            project_files.apply_sequence_fps(new, fps_num, fps_den)
        if reset_in_out:
            new.pop("start", None)
            new.pop("end", None)
        if in_out:
            num, den = _fps_parts(new)
            rate = num / den if num else project_fps()
            media_len = float(new.get("duration") or 0.0)
            cur_start, cur_end = _source_window(new)
            start = float(source_start_seconds) if source_start_seconds is not None else cur_start
            end = float(source_end_seconds) if source_end_seconds is not None else cur_end
            if media_len > 0 and end > media_len + 1.0 / rate:
                raise ToolError(f"source_end_seconds {end:.3f} is past the end of the media ({media_len:.3f}s)")
            end = min(end, media_len) if media_len > 0 else end
            if end - start < 1.0 / rate:
                raise ToolError(f"the in-point ({start:.3f}s) must be before the out-point ({end:.3f}s)")
            project_files.set_in_out_frames(new, int(round(start * rate)) + 1, max(1, int(round(end * rate))))
        if new != old or (rename_clips and name):
            plans.append((f, new))

    receipt_files = []
    for f in files:
        entry = {"file_id": f.id}
        if f.id in relinked:
            entry["path"] = relinked[f.id][0]
            entry["same_content"] = relinked[f.id][2]
        receipt_files.append(entry)
    if not plans:
        return ok("No change: the file(s) already have those settings.", changed=False, files=receipt_files,
                  not_found=not_found)

    def _apply():
        updated_clips, renamed_clips = [], []
        for f, new in plans:
            seed_missing_keys(f, {k: v for k, v in file_key_defaults(f.data).items() if k in new})
            removed_keys = [k for k in f.data if k not in new]
            f.data = new
            updated_clips += project_files.save_file_and_sync_clips(f, removed_keys)
            if rename_clips and name:
                for c in Clip.filter(file_id=f.id):
                    if c.data.get("title") != name:
                        c.data["title"] = name
                        c.save()
                        renamed_clips.append(c.id)
        refresh_preview()
        return updated_clips, renamed_clips

    updated_clips, renamed_clips = on_main(_apply, timeout=HOP_TIMEOUT)
    for entry in receipt_files:
        f = next((f for f, _ in plans if f.id == entry["file_id"]), None)
        if f is not None:
            entry.update({"name": _display_name(f.data), "tags": tags_of(f.data)})
            s, e = _source_window(f.data)
            entry["duration"] = round(e - s, 3)
            if "start" in f.data:
                entry["source_in"], entry["source_out"] = round(s, 3), round(e, 3)
            if fps:
                entry["fps"] = f"{f.data['fps']['num']}/{f.data['fps']['den']}"
    what = [w for w, used in (("renamed", name), ("tagged", add or drop or replace is not None),
                              ("relinked", relinked), ("in/out set", in_out or reset_in_out),
                              ("frame rate set", fps)) if used]
    summary = f"Updated {len(plans)} file(s): {', '.join(what)}."
    if updated_clips:
        summary += f" Refreshed {len(set(updated_clips))} timeline clip(s)."
    if not_found:
        summary += f" Not found under the folder: {', '.join(not_found)}."
    if any(e.get("same_content") is False for e in receipt_files):
        summary += " Note: the new media is not the same file as before (content differs)."
    return ok(summary, changed=True, files=receipt_files, updated_clip_ids=sorted(set(updated_clips)),
              renamed_clip_ids=renamed_clips, not_found=not_found)


# ---------------------------------------------------------------------------
# import_image_sequence_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "import_image_sequence_tool",
    label="Import image sequence",
    schema=obj({
        "path": string("A folder of numbered frames (frame_0001.png, frame_0002.png...) or any one frame of "
                       "the sequence. png, jpg, jpeg, tif and svg frames."),
        "fps": number("Frames per second to play the sequence at (24, 30, 12, 23.976...). 0 = the project "
                      "frame rate (what the Import Files prompt uses).", 0.0, minimum=0, maximum=240),
        "name": string("Display name in Project Files. '' = the pattern file name.", ""),
        "skip_indexing": boolean("Skip the background AI analysis of the new file.", False),
    }, required=["path"]),
    background_safe=True,
    covers=("files.import_sequence",),
)
def import_image_sequence(path, fps=0.0, name="", skip_indexing=False):
    """Import a folder of numbered images as ONE image-sequence file (an animation/video clip).

    Use for "import these frames as a sequence at 24 fps", renders from Blender/After
    Effects, stop-motion or time-lapse photo folders. The frames play at `fps` (the project
    rate when 0). It only adds the file to Project Files: place it with
    add_clip_to_timeline_tool(file_id=...). To import photos as separate stills (a
    slideshow), use import_files_tool instead. Returns the file id, pattern, frame count,
    fps and duration. One undo step.
    Example: path="~/renders/intro/", fps=24.
    """
    from classes.query import File

    raw = os.path.abspath(os.path.expanduser(str(path).strip()))
    if os.path.isdir(raw):
        first, details = project_files.first_sequence_frame(raw)
        if not first:
            raise ToolError(f"no numbered image sequence (name0001.png, name0002.png...) in {path!r}")
    elif os.path.isfile(raw):
        first, details = raw, project_files.detect_image_sequence(raw)
        if not details:
            raise ToolError(f"{os.path.basename(raw)!r} has no neighbouring numbered frames; import a single "
                            "image with import_files_tool")
    else:
        raise ToolError(f"no file or folder at {path!r}")
    frames = project_files.sequence_frame_numbers(details)
    if len(frames) < 2:
        raise ToolError(f"{details['pattern']} has only {len(frames)} frame(s); a sequence needs at least 2")

    existing = File.get(path=details["path"])
    if existing:
        return ok(f"That sequence is already in the project as {_display_name(existing.data)!r}.",
                  file_id=existing.id, changed=False, path=details["path"])

    if fps:
        num, den = project_files.fps_fraction(fps)
    else:
        p = get_app().project.get("fps") or {"num": 30, "den": 1}
        num, den = int(p.get("num") or 30), int(p.get("den") or 1)
    seq_info = dict(details, fps={"num": num, "den": den}, length_multiplier=1)

    def _import():
        model = get_app().window.files_model
        before = {f.id for f in File.filter()}
        added = model.add_files([first], image_seq_details=seq_info, quiet=True,
                                prevent_recent_folder=True, skip_indexing=bool(skip_indexing))
        new = next((f for f in (added or []) if f.id not in before), None)
        if new is None:
            new = File.get(path=details["path"])
            if new is None or new.id in before:
                return None
        if name:
            new.data["name"] = str(name).strip()
            new.save()
        return new.id

    file_id = on_main(_import, timeout=HOP_TIMEOUT)
    if not file_id:
        raise ToolError(f"libopenshot could not open {details['pattern']} as an image sequence")
    f = File.get(id=file_id)
    data = f.data
    return ok(f"Imported {len(frames)} frames of {details['pattern']} as one {num / den:g} fps sequence "
              f"({float(data.get('duration') or 0):.2f}s). Place it with add_clip_to_timeline_tool.",
              file_id=file_id, name=_display_name(data), path=details["path"], frames=len(frames),
              first_frame=frames[0], last_frame=frames[-1], fps=f"{num}/{den}",
              duration=round(float(data.get("duration") or 0.0), 3),
              width=data.get("width"), height=data.get("height"))


# ---------------------------------------------------------------------------
# remove_files_from_project_tool
# ---------------------------------------------------------------------------

@editor_tool(
    "remove_files_from_project_tool",
    label="Remove files from project",
    schema=obj({
        **FILE_TARGET,
        "scope": enum(["", "unused", "missing"],
                      "Instead of ids/query: 'unused' = every file no timeline clip uses, 'missing' = every "
                      "file whose media is gone from disk.", ""),
        "confirm_remove_clips": boolean("Required when a chosen file is used on the timeline: its timeline "
                                        "clips are removed with it. Ask the user first.", False),
    }),
    background_safe=True,
    covers=("files.remove",),
)
def remove_files_from_project(file_ids=None, file_query="", scope="", confirm_remove_clips=False):
    """Remove files from Project Files (Remove from Project). The media on disk is never deleted.

    Use for "remove unused files" (scope="unused": safe, no clip is touched), "clean up the
    missing files" (scope="missing"), or "remove intro.mp4 from the project". A file that is
    used on the timeline takes its timeline clips with it, so the call is refused until
    confirm_remove_clips=true; confirm with the user first. Refused when such a clip sits on
    a locked track. Cancels the file's AI generation and optimize jobs. One undo step
    restores the files and their clips. To delete clips but keep the file use
    delete_from_timeline_tool; this tool never deletes clips of other files.
    """
    from classes.query import Clip

    if scope and (file_ids or str(file_query or "").strip()):
        raise ToolError("pass scope or file_ids/file_query, not both")
    usage = _clip_usage()
    if scope == "unused":
        files = [f for f in _files() if not usage.get(str(f.id))]
        if not files:
            return ok("Every project file is used on the timeline; nothing to remove.", removed_file_ids=[],
                      changed=False)
    elif scope == "missing":
        files = [f for f in _files() if media_missing(f.data)]
        if not files:
            return ok("No project file is missing its media; nothing to remove.", removed_file_ids=[],
                      changed=False)
    else:
        files = resolve_files(file_ids, file_query)

    used = {f.id: usage.get(str(f.id), []) for f in files if usage.get(str(f.id))}
    if used:
        clips = [Clip.get(id=cid) for ids in used.values() for cid in ids]
        from classes.editor_tools._base import is_locked
        locked = sorted({ui_track_number(int(c.data.get("layer") or 0)) or int(c.data.get("layer") or 0)
                         for c in clips if c and is_locked(int(c.data.get("layer") or 0))})
        if locked:
            raise ToolError(f"clips of these files sit on locked track(s) {locked}; unlock them first")
        if not confirm_remove_clips:
            names = ", ".join(f"{_display_name(f.data)!r} ({len(used[f.id])} clip(s))" for f in files if f.id in used)
            raise ToolError(f"{names} {'is' if len(used) == 1 else 'are'} used on the timeline; removing "
                            f"{'it' if len(used) == 1 else 'them'} also deletes those "
                            f"{sum(len(v) for v in used.values())} timeline clip(s). Ask the user, then call "
                            "again with confirm_remove_clips=true (or remove only unused files).")

    names = {f.id: _display_name(f.data) for f in files}

    def _remove():
        removed = project_files.remove_files_from_project(files)
        refresh_preview()
        return removed

    removed_files, removed_clips = on_main(_remove, timeout=HOP_TIMEOUT)
    kept_subclips = [f.id for f in _files() if str(f.data.get("parent_file_id") or "") in set(removed_files)]
    summary = f"Removed {len(removed_files)} file(s) from the project"
    summary += f" and {len(removed_clips)} timeline clip(s) that used them." if removed_clips else "."
    summary += " The media files stay on disk."
    return ok(summary, changed=True, removed_file_ids=removed_files,
              removed_names=[names.get(i, i) for i in removed_files], removed_clip_ids=removed_clips,
              subclips_kept=kept_subclips)


# ---------------------------------------------------------------------------
# create_subclips_tool (Split Clip dialog, explicit times)
# ---------------------------------------------------------------------------

def _compact_time(seconds, num, den, hours, minutes):
    from classes import time_parts
    t = time_parts.secondsToTime(max(0.0, seconds), num, den)
    h, m, s, fr = int(t["hour"]), int(t["min"]), int(t["sec"]), int(t["frame"])
    if hours:
        return f"{h:02d}:{m:02d}:{s:02d};{fr:02d}"
    if minutes:
        return f"{m:02d}:{s:02d};{fr:02d}"
    return f"{s:02d};{fr:02d}"


@editor_tool(
    "create_subclips_tool",
    label="Create sub-clips",
    schema=obj({
        "file_id": string("The project file to cut sub-clips from (list_project_files_tool)."),
        "subclips": array(
            mapping("One sub-clip.", properties={
                "source_start_seconds": number("In-point in seconds of the source media.", minimum=0),
                "source_end_seconds": number("Out-point in seconds of the source media.", minimum=0),
                "name": string("Name in Project Files. '' = '<file> (start to end)' like Split Clip."),
            }, required=["source_start_seconds", "source_end_seconds"], additionalProperties=False),
            "Sub-clips to create, each with its own in/out and optional name."),
    }, required=["file_id", "subclips"]),
    covers=("files.subclip",),
)
def create_subclips(file_id, subclips):
    """Cut several named sub-clips out of one project file in one call (Project Files > Split Clip).

    Use when the user gives explicit source times: "make sub-clips 0:05-0:12 'Hook' and
    1:10-1:30 'Demo' from interview.mp4". Each sub-clip is a new Project Files entry that
    plays only its range (times in seconds of the source media); AI scene descriptions are
    trimmed to the range. Nothing is placed on the timeline: put them there with
    add_clip_to_timeline_tool(file_id=<new id>). To find a moment by description instead of
    times use split_file_add_clip_tool(query=...). One undo step for all sub-clips.
    """
    from classes.ai_metadata_utils import adjust_scene_descriptions_for_subclip
    from classes.query import File

    src = File.get(id=str(file_id).strip())
    if not src:
        raise ToolError(f"no project file with id={file_id!r}")
    if not subclips:
        raise ToolError("give at least one sub-clip with source_start_seconds and source_end_seconds")
    num, den = _fps_parts(src.data)
    rate = num / den if num else project_fps()
    media_start, media_end = _source_window(src.data)
    if kind_of(src.data) == "image" or src.data.get("has_single_image"):
        raise ToolError("a still image has no time range to split")
    plans = []
    for i, sc in enumerate(subclips):
        s, e = float(sc["source_start_seconds"]), float(sc["source_end_seconds"])
        if e <= s:
            raise ToolError(f"sub-clip {i + 1}: source_end_seconds must be after source_start_seconds")
        if s < media_start - 1e-3 or e > media_end + 1.0 / rate:
            raise ToolError(f"sub-clip {i + 1}: {s:.2f}-{e:.2f}s is outside the file's "
                            f"{media_start:.2f}-{media_end:.2f}s")
        # Snap to the file's frames, like the dialog's Start/End buttons.
        s = round(s * rate) / rate
        e = min(round(e * rate) / rate, media_end)
        if e - s < 1.0 / rate:
            raise ToolError(f"sub-clip {i + 1} is shorter than one frame")
        plans.append((s, e, str(sc.get("name") or "").strip()))

    base = os.path.splitext(_base_name(src.data))[0] or _display_name(src.data)

    def _create():
        ids = []
        for s, e, sub_name in plans:
            new_file = File()
            new_file.data = copy.deepcopy(src.data)
            new_file.data.pop("name", None)
            new_file.data.pop("id", None)
            new_file.id = None
            new_file.key = None
            new_file.type = "insert"
            new_file.data["start"] = s
            new_file.data["end"] = e
            if isinstance(new_file.data.get("ai_metadata"), dict):
                new_file.data["ai_metadata"] = adjust_scene_descriptions_for_subclip(
                    new_file.data["ai_metadata"], s, e)
            if not sub_name:
                hours = s >= 3600 or e >= 3600
                minutes = hours or s >= 60 or e >= 60
                sub_name = (f"{base} ({_compact_time(s, num or 30, den, hours, minutes)} to "
                            f"{_compact_time(e, num or 30, den, hours, minutes)})")
            new_file.data["name"] = sub_name
            new_file.save()
            ids.append((new_file.id, sub_name, s, e))
        return ids

    created = on_main(_create)
    return ok(f"Created {len(created)} sub-clip(s) of {_display_name(src.data)!r}. Place one with "
              f"add_clip_to_timeline_tool(file_id=...).",
              subclips=[{"file_id": i, "name": n, "source_in": round(s, 3), "source_out": round(e, 3),
                         "duration": round(e - s, 3)} for i, n, s, e in created],
              parent_file_id=src.id)


# ---------------------------------------------------------------------------
# manage_optimized_previews_tool (Preview > Optimize)
# ---------------------------------------------------------------------------

PROXY_ACTIONS = ("optimize", "status", "link", "unlink", "delete", "cancel", "clear_all")
ACTIVE_PROXY_STATES = ("queued", "running", "canceling")


def _proxy_row(service, f):
    data = f.data if isinstance(f.data, dict) else {}
    row = {"file_id": f.id, "name": _display_name(data), "state": _proxy_state(f)}
    job = service.get_active_job_for_file(f.id) if service else None
    if isinstance(job, dict):
        row["progress"] = int(job.get("progress") or 0)
    proxy = data.get("proxy_reader")
    if isinstance(proxy, dict) and proxy.get("path"):
        row["proxy_path"] = _abs_path(proxy.get("path"))
        if isinstance(proxy.get("width"), int):
            row["proxy_size"] = f"{proxy.get('width')}x{proxy.get('height')}"
    return row


@editor_tool(
    "manage_optimized_previews_tool",
    label="Optimized previews",
    schema=obj({
        "action": enum(list(PROXY_ACTIONS),
                       "optimize = build a small preview copy for smooth playback (runs in the background); "
                       "status = report progress/state; link = use existing optimized copies from a folder "
                       "(or proxy_path for one file); unlink = stop using the copy (kept on disk); delete = "
                       "unlink and delete the copy from disk; cancel = stop a running optimize job; "
                       "clear_all = unlink and delete every optimized copy in this project's assets folder "
                       "(Edit > Clear > Optimized Videos; needs confirm).", "optimize"),
        **FILE_TARGET,
        "all_videos": boolean("Act on every video file in the project instead of file_ids/file_query.", False),
        "folder": string("For action=link: the folder that holds the optimized copies (matched by file name).",
                         ""),
        "proxy_path": string("For action=link with one file: the exact optimized video to use.", ""),
        "wait_seconds": number("For optimize: wait up to this long for the jobs to finish before answering "
                               "(0 = return at once; poll with action=status).", 0.0, minimum=0, maximum=1800),
        "confirm": boolean("Required for clear_all (deletes files from the assets folder).", False),
    }),
    background_safe=True,
    covers=("files.proxy",),
)
def manage_optimized_previews(action="optimize", file_ids=None, file_query="", all_videos=False, folder="",
                              proxy_path="", wait_seconds=0.0, confirm=False):
    """Optimized previews (proxies): smooth playback of heavy media (4K, high bit rate) while editing.

    "Optimize the 4K clip for smooth playback" -> action=optimize (a background job builds a
    small H.264 copy, at most the Optimized Preview Resolution preference, 720p by default;
    the preview plays the copy, exports always use the original). Poll with action=status
    or pass wait_seconds. link/unlink/delete/cancel mirror Preview > Optimize; clear_all is
    Edit > Clear > Optimized Videos. Videos only. Linking/unlinking is one undo step; a
    finished optimize job links its copy in its own undo step (as the menu does). Never
    deletes timeline clips or original media.
    """
    service = _proxy_service()
    if service is None or not hasattr(service, "create_for_files"):
        raise ToolError("optimized previews are not available (the editor window is not ready)")

    if action == "clear_all":
        if not confirm:
            raise ToolError("clear_all deletes every optimized copy in this project's assets folder; ask the "
                            "user, then call again with confirm=true")
        proxy_root, unlinked = on_main(service.unlink_internal_project_proxies, timeout=HOP_TIMEOUT)
        if not proxy_root:
            return ok("This project has no optimized copies in its assets folder.", changed=False, deleted=0)
        deleted, failed = service.delete_proxy_paths(unlinked)
        deleted += service.purge_proxy_root(proxy_root)
        return ok(f"Deleted {deleted} optimized file(s); unlinked {len(unlinked)} project file(s).",
                  changed=bool(unlinked), deleted=deleted, unlinked=len(unlinked), failed=failed,
                  folder=proxy_root)

    if all_videos:
        if file_ids or str(file_query or "").strip():
            raise ToolError("pass all_videos or file_ids/file_query, not both")
        files = [f for f in _files() if kind_of(f.data) == "video"]
        if not files:
            raise ToolError("the project has no video files")
    elif action == "status" and not file_ids and not str(file_query or "").strip():
        files = [f for f in _files() if kind_of(f.data) == "video"]
    else:
        files = resolve_files(file_ids, file_query)
        not_video = [_display_name(f.data) for f in files if kind_of(f.data) != "video"]
        if not_video:
            raise ToolError(f"only video files can be optimized; not videos: {', '.join(not_video)}")

    if action == "status":
        rows = [_proxy_row(service, f) for f in files]
        active = sum(1 for r in rows if r["state"] in ACTIVE_PROXY_STATES)
        ready = sum(1 for r in rows if r["state"] == "ready")
        return ok(f"{len(rows)} video file(s): {ready} optimized, {active} in progress.", files=rows)

    if action == "optimize":
        missing = [_display_name(f.data) for f in files if media_missing(f.data)]
        if missing:
            raise ToolError(f"media missing on disk: {', '.join(missing)}; relink first")
        before = {f.id: _proxy_state(f) for f in files}
        todo = [f for f in files if before[f.id] not in ACTIVE_PROXY_STATES + ("ready",)]
        if not todo:
            return ok("Already optimized or in progress; nothing started.", changed=False,
                      files=[_proxy_row(service, f) for f in files])
        on_main(service.create_for_files, todo, timeout=HOP_TIMEOUT)
        deadline = time.monotonic() + float(wait_seconds or 0.0)
        while wait_seconds and time.monotonic() < deadline:
            if not any(service.get_active_job_for_file(f.id) for f in todo):
                break
            time.sleep(0.5)
        from classes.query import File
        fresh = [File.get(id=f.id) or f for f in files]
        rows = [_proxy_row(service, f) for f in fresh]
        running = [r for r in rows if r["state"] in ACTIVE_PROXY_STATES]
        failed = [r["name"] for r in rows if r["file_id"] in {f.id for f in todo}
                  and r["state"] == "none" and not service.get_active_job_for_file(r["file_id"])]
        if wait_seconds and failed and not running:
            raise ToolError(f"optimizing failed for {', '.join(failed)} (see the status bar/log)")
        summary = (f"Optimizing {len(todo)} video(s) in the background."
                   if running else f"Optimized {len(todo)} video(s).")
        if running and wait_seconds:
            summary += f" Still running after {wait_seconds:g}s; check with action=status."
        return ok(summary, changed=True, started=[f.id for f in todo], files=rows)

    if action == "cancel":
        active = [f for f in files if service.get_active_job_for_file(f.id)]
        if not active:
            return ok("No optimize job is running for those files.", changed=False)
        on_main(service.cancel_for_files, active, timeout=HOP_TIMEOUT)
        return ok(f"Canceled optimizing {len(active)} video(s).", changed=True, canceled=[f.id for f in active])

    if action in ("unlink", "delete"):
        linked = [f for f in files if isinstance(f.data.get("proxy_reader"), dict)]
        if not linked:
            return ok("Those files have no optimized preview linked.", changed=False)
        paths = on_main(lambda: service.remove_for_files(linked, show_status=False), timeout=HOP_TIMEOUT)
        if action == "unlink":
            return ok(f"Unlinked the optimized preview of {len(linked)} file(s); playback uses the original "
                      "media. The optimized copies stay on disk.", changed=True, unlinked=[f.id for f in linked],
                      kept_paths=[p for p in paths if p])
        deleted, failed = service.delete_proxy_paths(paths)
        summary = f"Unlinked {len(linked)} file(s) and deleted {deleted} optimized copy/copies from disk."
        if failed:
            summary += f" Could not delete: {', '.join(failed)}."
        return ok(summary, changed=True, unlinked=[f.id for f in linked], deleted=deleted, failed=failed)

    # link
    if proxy_path:
        if len(files) != 1:
            raise ToolError("proxy_path links one file; choose exactly one")
        p = os.path.abspath(os.path.expanduser(proxy_path))
        if not os.path.isfile(p):
            raise ToolError(f"no file at {proxy_path!r}")
        try:
            reader = service._reader_json_for_path(p, files[0].id)
        except Exception as exc:
            raise ToolError(f"libopenshot cannot open {proxy_path!r}: {exc}") from None
        matches = [(files[0], reader, p, "")]
    else:
        if not folder:
            raise ToolError("action=link needs folder (or proxy_path for one file)")
        d = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(d):
            raise ToolError(f"{folder!r} is not a folder")
        matches = service.match_existing_in_folder(files, d)
        if not any(r and not (r or {}).get("missing") and not err for _f, r, _p, err in matches):
            names = ", ".join(_display_name(f.data) for f in files)
            raise ToolError(f"no optimized copy for {names} in {folder!r}")
        # Only link real matches; a "missing" placeholder would mark the file as optimized-but-missing.
        matches = [m for m in matches if m[1] and not m[1].get("missing") and not m[3]]
    matched, _missing, invalid = on_main(service.link_matches, matches, timeout=HOP_TIMEOUT)
    return ok(f"Linked {matched} optimized preview(s).", changed=bool(matched),
              linked=[{"file_id": f.id, "proxy_path": p} for f, _r, p, _e in matches], invalid=invalid)


# The transition tools of this workstream register from their own module.
from classes.editor_tools import media_files_transitions  # noqa: E402,F401
