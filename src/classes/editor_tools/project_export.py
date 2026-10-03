"""Project format and delivery: profiles (social presets), project files, preferences, views, EDL/XML, export presets and renders.

Workstream: project-export. This module holds the project lifecycle (info, new,
open, save, recent projects, recovery), EDL / Final Cut Pro XML interchange,
media collect / reclaim and cache / history resets. Profiles, rendering,
preferences and the editor layout live in ``project_export_<topic>.py``.

Every tool reuses the editor's own code path (``MainWindow.new_project``,
``open_project(interactive=False)``, ``save_project(raise_errors=True)``, the
importers and exporters with a path) so nothing here opens a modal dialog.
"""

from __future__ import annotations

import copy
import os
import time
from datetime import datetime

from classes.editor_tools._base import (
    ToolError, boolean, enum, get_app, integer, obj, ok, on_main, string, th,
)
from classes.editor_tools._registry import editor_tool

# Opening / importing a large project runs the editor's loader on the GUI thread.
_LOAD_TIMEOUT = 600


# ---------------------------------------------------------------------------
# Shared helpers (the other project_export_* modules import these)
# ---------------------------------------------------------------------------

def window():
    win = getattr(get_app(), "window", None)
    if win is None:
        raise ToolError("the editor window is not ready yet")
    return win


def project_path() -> str:
    return getattr(get_app().project, "current_filepath", None) or ""


def is_dirty() -> bool:
    try:
        return bool(get_app().project.needs_save())
    except Exception:
        return False


def normalize_path(path: str) -> str:
    path = os.path.expandvars(os.path.expanduser(str(path or "").strip()))
    return os.path.abspath(path) if path else ""


def timeline_end_seconds() -> float:
    end = 0.0
    for c in get_app().project.get("clips") or []:
        try:
            pos = float(c.get("position") or 0.0)
            end = max(end, pos + max(0.0, float(c.get("end") or 0.0) - float(c.get("start") or 0.0)))
        except (TypeError, ValueError):
            continue
    return round(end, 3)


def undo_depth() -> tuple:
    updates = get_app().updates
    try:
        undo = len({a.transaction for a in updates.actionHistory})
        redo = len({a.transaction for a in updates.redoHistory})
        return undo, redo
    except Exception:
        return 0, 0


def project_snapshot() -> dict:
    """What get_project_info_tool reports (also used in other receipts)."""
    from classes.project_profile import aspect_text, orientation_of

    app = get_app()
    proj = app.project
    fps = proj.get("fps") or {"num": 30, "den": 1}
    num, den = int(fps.get("num") or 30), int(fps.get("den") or 1)
    width, height = int(proj.get("width") or 0), int(proj.get("height") or 0)
    dar = proj.get("display_ratio") or {"num": width, "den": height}
    par = proj.get("pixel_ratio") or {"num": 1, "den": 1}
    path = project_path()
    undo, redo = undo_depth()
    clips = proj.get("clips") or []
    files = [f for f in (proj.get("files") or []) if not f.get("zenvi_subclip")]
    layers = proj.get("layers") or []
    return {
        "path": path or None,
        "name": os.path.splitext(os.path.basename(path))[0] if path else "Untitled Project",
        "saved": bool(path),
        "unsaved_changes": is_dirty(),
        "profile": proj.get("profile") or "",
        "width": width,
        "height": height,
        "fps": round(num / float(den or 1), 3),
        "fps_num": num,
        "fps_den": den,
        "aspect": aspect_text(dar.get("num") or width or 1, dar.get("den") or height or 1),
        "pixel_ratio": f"{par.get('num', 1)}:{par.get('den', 1)}",
        "orientation": orientation_of(width, height),
        "sample_rate": int(proj.get("sample_rate") or 0),
        "channels": int(proj.get("channels") or 0),
        "channel_layout": _channel_layout_name(proj.get("channel_layout")),
        "duration_seconds": timeline_end_seconds(),
        "counts": {
            "files": len(files),
            "clips": len(clips),
            "tracks": len(layers),
            "locked_tracks": sum(1 for layer in layers if layer.get("lock")),
            "transitions": len(proj.get("effects") or []),
            "markers": len(proj.get("markers") or []),
            "clip_effects": sum(len(c.get("effects") or []) for c in clips),
        },
        "undo_steps": undo,
        "redo_steps": redo,
    }


# libopenshot ChannelLayout values
CHANNEL_LAYOUTS = {"mono": (4, 1), "stereo": (3, 2), "surround": (7, 3), "5.1": (1551, 6), "7.1": (1599, 8)}


def _channel_layout_name(value) -> str:
    for name, (code, _channels) in CHANNEL_LAYOUTS.items():
        if value == code:
            return name
    return str(value or "")


def _require_clean(discard_unsaved: bool, save_first: bool, action: str) -> None:
    """Refuse to throw away unsaved work unless the caller said what to do with it."""
    if not is_dirty() or discard_unsaved:
        return
    if not save_first:
        raise ToolError(
            f"the current project has unsaved changes; {action} would discard them. Ask the user, "
            "then call again with save_first=true (keep them) or discard_unsaved=true (lose them)")
    if not project_path():
        raise ToolError(
            "the current project has unsaved changes and has never been saved; call "
            "save_project_tool with a file_path first, or pass discard_unsaved=true")


def save_to(path: str) -> None:
    """Save the project through the editor's save path (history, recovery zip, media relocation)."""
    try:
        window().save_project(path, raise_errors=True)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not save the project to {path}: {exc}") from exc
    if not os.path.isfile(path):
        raise ToolError(f"saving reported success but {path} does not exist")


def _recent_projects() -> list:
    """Recent projects, most recent first."""
    items = get_app().get_settings().get("recent_projects") or []
    return [p for p in reversed(list(items)) if p]


# ---------------------------------------------------------------------------
# Project info / lifecycle
# ---------------------------------------------------------------------------

@editor_tool(
    "get_project_info_tool",
    label="Read project info",
    schema=obj({}),
    read_only=True,
    covers=("project.info",),
)
def get_project_info():
    """Read the project's file path, profile (size, fps, aspect, orientation), audio settings, length and counts.

    Use it before changing the format ("is this vertical?", "what fps is this?"),
    before saving/opening (it says whether there are unsaved changes), and to
    confirm a profile change. Returns path (null when never saved), name,
    unsaved_changes, profile name, width, height, fps (+fps_num/fps_den),
    aspect ("16:9"), orientation (landscape/portrait/square), sample_rate,
    channels, channel_layout, duration_seconds (end of the last clip), counts of
    files/clips/tracks/transitions/markers/effects and undo/redo depth.
    """
    info = project_snapshot()
    where = info["path"] or "not saved yet"
    return ok(f"Project '{info['name']}' ({where}): {info['width']}x{info['height']} {info['aspect']} "
              f"{info['orientation']} at {info['fps']} fps, {info['duration_seconds']}s of timeline, "
              f"{info['counts']['clips']} clips on {info['counts']['tracks']} tracks"
              f"{', unsaved changes' if info['unsaved_changes'] else ''}.", **info)


@editor_tool(
    "new_project_tool",
    label="New project",
    schema=obj({
        "discard_unsaved": boolean("Throw away unsaved changes in the current project. Only after the user agreed.", False),
        "save_first": boolean("Save the current project to its existing file first (it must have been saved before).", False),
        "profile": string("Optional video profile or social preset for the new project, e.g. 'instagram_reel', "
                          "'youtube_4k', 'FHD 1080p 30 fps' (see list_project_profiles_tool). Empty = default profile.", ""),
    }),
    background_safe=True,
    covers=("project.new",),
)
def new_project(discard_unsaved=False, save_first=False, profile=""):
    """Start a new, empty project (File > New Project), optionally in a given format.

    Refuses while the current project has unsaved changes unless save_first=true
    (saves to its current file) or discard_unsaved=true (the user agreed to lose
    them) -- ask the user which. The new project has no undo history.
    Example: {"save_first": true, "profile": "tiktok"} for "start a new TikTok".
    """
    from classes.editor_tools import project_export_profiles as profiles

    _require_clean(discard_unsaved, save_first, "starting a new project")
    record = profiles.resolve_profile_request(profile) if (profile or "").strip() else None
    previous = project_path() or None
    saved_to = None
    if save_first and is_dirty():
        saved_to = project_path()
        save_to(saved_to)

    def _new():
        window().new_project()
        if record:
            app = get_app()
            with th()._ignore_history(app):
                from classes.project_profile import apply_profile_values
                apply_profile_values(record)
            app.project.has_unsaved_changes = False
            window().refreshFrameSignal.emit()

    on_main(_new, timeout=120)
    info = project_snapshot()
    return ok(f"Started a new project ({info['width']}x{info['height']} at {info['fps']} fps"
              f"{', saved the previous one first' if saved_to else ''}).",
              previous_path=previous, saved_previous_to=saved_to, profile=info["profile"],
              width=info["width"], height=info["height"], fps=info["fps"])


@editor_tool(
    "open_project_tool",
    label="Open project",
    schema=obj({
        "file_path": string("Path of a .zvn project (legacy .osp/.flow also open). Leave empty when using recent_index.", ""),
        "recent_index": integer("Open a recent project instead: 1 = most recent (see list_recent_projects_tool). 0 = use file_path.",
                                0, minimum=0, maximum=10),
        "discard_unsaved": boolean("Throw away unsaved changes in the current project. Only after the user agreed.", False),
        "save_first": boolean("Save the current project to its existing file before opening the other one.", False),
    }),
    background_safe=True,
    covers=("project.open",),
)
def open_project(file_path="", recent_index=0, discard_unsaved=False, save_first=False):
    """Open a saved project file and wait until it is loaded (File > Open Project / Recent Projects).

    Refuses while the current project has unsaved changes unless save_first=true
    or discard_unsaved=true (ask the user). Never shows a dialog: media that
    cannot be found stays in the project and is listed in missing_media.
    Reopening the current file with discard_unsaved=true reverts to the last
    save. Undo history becomes the one stored in the opened file.
    """
    from classes import info

    if recent_index:
        recent = _recent_projects()
        if recent_index > len(recent):
            raise ToolError(f"there are only {len(recent)} recent projects")
        file_path = recent[recent_index - 1]
    path = normalize_path(file_path)
    if not path:
        raise ToolError("give file_path (a .zvn project) or recent_index")
    if not path.lower().endswith(info.ALL_PROJECT_EXTS):
        raise ToolError(f"{os.path.basename(path)} is not a project file (.zvn, .osp or .flow); "
                        "use import_files_tool for media or import_project_file_tool for EDL/XML")
    if not os.path.isfile(path):
        raise ToolError(f"project file not found: {path}")
    _require_clean(discard_unsaved, save_first, "opening another project")
    saved_to = None
    if save_first and is_dirty():
        saved_to = project_path()
        save_to(saved_to)

    def _open():
        return window().open_project(path, interactive=False)

    try:
        loaded = on_main(_open, timeout=_LOAD_TIMEOUT)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not open {path}: {exc}") from exc
    if not loaded:
        raise ToolError(f"{path} was not opened")
    missing = list(getattr(get_app().project, "last_missing_media", []) or [])
    snap = project_snapshot()
    note = f" {len(missing)} media file(s) are missing (listed)." if missing else ""
    return ok(f"Opened '{snap['name']}': {snap['width']}x{snap['height']} at {snap['fps']} fps, "
              f"{snap['counts']['clips']} clips.{note}",
              path=path, saved_previous_to=saved_to, missing_media=missing[:50],
              missing_count=len(missing), profile=snap["profile"], counts=snap["counts"],
              duration_seconds=snap["duration_seconds"])


@editor_tool(
    "save_project_tool",
    label="Save project",
    schema=obj({
        "file_path": string("Where to save. Empty = the project's current file (Save). A new path = Save As "
                            "(.zvn is added when there is no project extension; missing folders are created).", ""),
        "overwrite": boolean("Allow replacing an existing file that is not this project's current file.", False),
    }),
    background_safe=True,
    covers=("project.save", "project.save_as"),
)
def save_project(file_path="", overwrite=False):
    """Save the project (File > Save Project) or save it under a new path (Save Project As).

    Empty file_path saves to the current file; a never-saved project needs a
    file_path. Writes undo history (capped by the history-limit preference) and a
    recovery copy, like the editor's own save. Refuses to overwrite another
    existing file unless overwrite=true. Failures are reported as errors.
    """
    from classes import info

    current = project_path()
    path = normalize_path(file_path) if (file_path or "").strip() else current
    if not path:
        raise ToolError("the project has never been saved; give file_path, e.g. ~/Movies/My Edit.zvn")
    if os.path.isdir(path):
        raise ToolError(f"{path} is a folder; give a file path such as {os.path.join(path, 'My Edit.zvn')}")
    if not path.lower().endswith(info.ALL_PROJECT_EXTS):
        path += info.PROJECT_EXT
    same_file = bool(current) and os.path.normcase(path) == os.path.normcase(current)
    if os.path.exists(path) and not same_file and not overwrite:
        raise ToolError(f"{path} already exists; pass overwrite=true to replace it")
    folder = os.path.dirname(path)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"cannot create folder {folder}: {exc}") from exc
    save_to(path)
    try:
        s = get_app().get_settings()
        s.setDefaultPath(s.actionType.SAVE, path)
    except Exception:
        pass
    size = os.path.getsize(path)
    verb = "Saved" if same_file or not current else "Saved as"
    return ok(f"{verb} {path} ({size // 1024} KB).", path=path, previous_path=current or None,
              save_as=not same_file, size_bytes=size)


# ---------------------------------------------------------------------------
# Recent projects
# ---------------------------------------------------------------------------

@editor_tool(
    "list_recent_projects_tool",
    label="List recent projects",
    schema=obj({}),
    read_only=True,
    covers=("project.recent",),
)
def list_recent_projects():
    """List File > Recent Projects, most recent first, with whether each file still exists.

    Use the index with open_project_tool(recent_index=N) ("open my last
    project"), or forget_recent_projects_tool to tidy the list.
    """
    out = []
    for i, path in enumerate(_recent_projects(), start=1):
        exists = os.path.isfile(path)
        out.append({
            "index": i, "path": path, "name": os.path.splitext(os.path.basename(path))[0],
            "exists": exists,
            "modified": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="minutes")
            if exists else None,
        })
    if not out:
        return ok("No recent projects.", projects=[])
    return ok(f"{len(out)} recent project(s); most recent: {out[0]['name']}.", projects=out,
              current_path=project_path() or None)


@editor_tool(
    "forget_recent_projects_tool",
    label="Forget recent projects",
    schema=obj({
        "file_path": string("Forget only this project path. Empty = every entry (or only missing ones with missing_only).", ""),
        "missing_only": boolean("Only forget entries whose file no longer exists.", False),
    }),
    covers=("project.recent",),
)
def forget_recent_projects(file_path="", missing_only=False):
    """Remove entries from File > Recent Projects (Clear Recent Projects). Never deletes any file.

    Empty file_path clears the whole list; missing_only=true drops only entries
    whose file is gone. Not an undo step (it is an editor preference).
    """
    from classes.path_utils import comparable_local_path

    recent = list(get_app().get_settings().get("recent_projects") or [])
    if not recent:
        raise ToolError("the recent projects list is already empty")
    if (file_path or "").strip():
        key = comparable_local_path(normalize_path(file_path))
        keep = [p for p in recent if comparable_local_path(p) != key]
        if len(keep) == len(recent):
            raise ToolError(f"{file_path} is not in the recent projects list")
    elif missing_only:
        keep = [p for p in recent if os.path.isfile(p)]
        if len(keep) == len(recent):
            return ok("Every recent project still exists; nothing forgotten.", forgotten=[], remaining=len(recent))
    else:
        keep = []
    forgotten = [p for p in recent if p not in keep]

    def _apply():
        s = get_app().get_settings()
        s.set("recent_projects", keep)
        s.save()
        window().load_recent_menu()

    on_main(_apply)
    return ok(f"Forgot {len(forgotten)} recent project(s); {len(keep)} remain.",
              forgotten=forgotten, remaining=len(keep))


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def _recovery_versions() -> list:
    from classes.project_recovery import recovery_files_for

    now = time.time()
    out = []
    for i, (stamp, path) in enumerate(recovery_files_for(project_path()), start=1):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
        out.append({
            "version": i, "path": path, "saved_at": datetime.fromtimestamp(stamp).isoformat(timespec="seconds"),
            "minutes_ago": round((now - stamp) / 60.0, 1), "size_bytes": size,
        })
    return out


@editor_tool(
    "list_recovery_versions_tool",
    label="List recovery versions",
    schema=obj({}),
    read_only=True,
    covers=("project.recovery",),
)
def list_recovery_versions():
    """List File > Recovery: the automatic copies saved each time this project was saved, newest first.

    Use before restore_recovery_version_tool ("go back to the version from an
    hour ago"). A project that was never saved has none.
    """
    if not project_path():
        return ok("The project has never been saved, so it has no recovery versions.", versions=[])
    versions = _recovery_versions()
    if not versions:
        return ok("No recovery versions exist for this project yet.", versions=[], project_path=project_path())
    return ok(f"{len(versions)} recovery version(s); newest saved {versions[0]['minutes_ago']} min ago.",
              versions=versions, project_path=project_path())


@editor_tool(
    "restore_recovery_version_tool",
    label="Restore recovery version",
    schema=obj({
        "version": integer("Which copy from list_recovery_versions_tool: 1 = newest.", 1, minimum=1),
        "discard_unsaved": boolean("Throw away unsaved changes in the open project. Only after the user agreed.", False),
    }),
    background_safe=True,
    covers=("project.recovery",),
)
def restore_recovery_version(version=1, discard_unsaved=False):
    """Replace the project file with one of its recovery copies and reopen it (File > Recovery).

    The current project file is kept first as '<name>-<time>-backup.zvn' next to
    it, so nothing is lost. Refuses while there are unsaved changes unless
    discard_unsaved=true. Undo history becomes the restored file's history.
    """
    if not project_path():
        raise ToolError("the project has never been saved, so it has no recovery versions")
    versions = _recovery_versions()
    if not versions:
        raise ToolError("no recovery versions exist for this project yet")
    if version > len(versions):
        raise ToolError(f"there are only {len(versions)} recovery versions")
    _require_clean(discard_unsaved, False, "restoring a recovery version")
    chosen = versions[version - 1]
    try:
        restored, backup = window().restore_recovery_file(chosen["path"])
    except Exception as exc:
        raise ToolError(f"could not restore {chosen['path']}: {exc}") from exc

    def _open():
        return window().open_project(restored, interactive=False)

    try:
        loaded = on_main(_open, timeout=_LOAD_TIMEOUT)
    except Exception as exc:
        raise ToolError(f"restored the file but could not reopen it: {exc}") from exc
    if not loaded:
        raise ToolError(f"restored {restored} but it did not open")
    snap = project_snapshot()
    return ok(f"Restored the version saved {chosen['minutes_ago']} min ago ({snap['counts']['clips']} clips); "
              f"the previous file was kept as {os.path.basename(backup) if backup else 'nothing (it was missing)'}.",
              path=restored, restored_from=chosen["path"], backup_of_previous=backup, counts=snap["counts"])


# ---------------------------------------------------------------------------
# EDL / Final Cut Pro XML interchange
# ---------------------------------------------------------------------------

def _format_of(path: str, fmt: str) -> str:
    fmt = (fmt or "auto").lower()
    if fmt != "auto":
        return fmt
    ext = os.path.splitext(path)[1].lower()
    if ext == ".edl":
        return "edl"
    if ext in (".xml", ".fcpxml"):
        return "fcpxml"
    raise ToolError(f"cannot tell the format of {os.path.basename(path)}; pass format='edl' or 'fcpxml'")


def _edl_media_paths(path: str) -> list:
    from classes.importers import edl
    from classes.path_utils import absolute_path_from_export

    folder = os.path.dirname(os.path.abspath(path))
    found = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for r in edl.source_regex.findall(line) + edl.clip_name_regex.findall(line):
                found.append(absolute_path_from_export(r.strip(), folder))
    return found


def _media_resolvable(path: str) -> bool:
    from windows.views.find_file import find_missing_file
    resolved, _modified, skipped = find_missing_file(path, prompt=False)
    return bool(resolved) and not skipped


@editor_tool(
    "import_project_file_tool",
    label="Import EDL / XML",
    schema=obj({
        "file_path": string("Path of an .edl (Edit Decision List) or Final Cut Pro .xml file."),
        "format": enum(["auto", "edl", "fcpxml"], "File format; auto = from the extension.", "auto"),
    }, required=["file_path"]),
    background_safe=True,
    covers=("project.import_edl", "project.import_fcpxml"),
)
def import_project_file(file_path, format="auto"):
    """Import an EDL or Final Cut Pro XML timeline into new tracks of this project (File > Import Project).

    Clips keep their positions, trims and opacity/volume (and, from XML,
    position/scale/rotation) keyframes; media not already in the project is
    added. Media that cannot be found is skipped and listed (no dialog). An EDL
    becomes one new track "EDL Import"; XML creates one track per source track.
    One undo step removes everything it added.
    """
    path = normalize_path(file_path)
    if not os.path.isfile(path):
        raise ToolError(f"file not found: {path}")
    fmt = _format_of(path, format)
    if fmt == "edl":
        media = _edl_media_paths(path)
        if not media:
            raise ToolError(f"{os.path.basename(path)} has no clips with a media path (FROM CLIP NAME / SOURCE FILE)")
        if not any(_media_resolvable(m) for m in media):
            raise ToolError("none of the media in this EDL can be found: " + ", ".join(sorted(set(media))[:5]))
    else:
        from xml.dom import minidom
        try:
            doc = minidom.parse(path)
        except Exception as exc:
            raise ToolError(f"{os.path.basename(path)} is not valid XML: {exc}") from exc
        try:
            if not doc.getElementsByTagName("clipitem"):
                raise ToolError(f"{os.path.basename(path)} has no clips (no <clipitem>); is it a Final Cut Pro 7 XML?")
        finally:
            doc.unlink()

    def _import():
        if fmt == "edl":
            from classes.importers.edl import import_edl
            return import_edl(path, prompt=False)
        from classes.importers.final_cut_pro import import_xml
        return import_xml(path, prompt=False)

    try:
        summary = on_main(_import, timeout=_LOAD_TIMEOUT)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"importing {os.path.basename(path)} failed: {exc}") from exc
    summary = summary or {}
    clip_ids = list(summary.get("clip_ids") or [])
    missing = sorted(set(summary.get("missing") or []))
    if not clip_ids:
        raise ToolError("no clip could be imported" + (": missing media " + ", ".join(missing[:5]) if missing else ""))
    tracks = summary.get("track_numbers") or ([summary["track_number"]] if summary.get("track_number") else [])
    return ok(f"Imported {len(clip_ids)} clip(s) from {os.path.basename(path)} onto {len(tracks)} new track(s)"
              f"{f'; {len(missing)} missing media skipped' if missing else ''}.",
              format=fmt, timeline_clip_ids=clip_ids, layers=tracks, missing_media=missing)


@editor_tool(
    "export_project_file_tool",
    label="Export EDL / XML",
    schema=obj({
        "format": enum(["fcpxml", "edl"], "fcpxml = Final Cut Pro XML (Premiere, Resolve, FCP 7); edl = CMX EDL, one file per track.",
                       "fcpxml"),
        "file_path": string("Output file. Empty = next to the project (or in Downloads) named after the project.", ""),
        "overwrite": boolean("Allow replacing existing output file(s).", False),
    }),
    background_safe=True,
    covers=("project.export_edl", "project.export_fcpxml"),
)
def export_project_file(format="fcpxml", file_path="", overwrite=False):
    """Export the timeline as a Final Cut Pro XML or EDL file for another editor (File > Export Project).

    Use for "send this edit to Premiere/Resolve" or "give me an EDL". Not a
    video render (that is export_video_tool). EDL holds one track per file, so
    each non-empty track is written as '<name>-<track>.edl'. Changes nothing in
    the project.
    """
    from classes import info

    if not (get_app().project.get("clips") or []):
        raise ToolError("the timeline has no clips to export")
    ext = ".xml" if format == "fcpxml" else ".edl"
    path = normalize_path(file_path)
    if not path:
        base = os.path.splitext(os.path.basename(project_path()))[0] if project_path() else "Untitled Project"
        folder = os.path.dirname(project_path()) if project_path() else info.DOWNLOADS_PATH
        path = os.path.join(folder, base + ext)
    if os.path.isdir(path):
        raise ToolError(f"{path} is a folder; give a file name")
    if not path.lower().endswith(ext):
        path += ext
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    if not overwrite:
        if format == "fcpxml" and os.path.exists(path):
            raise ToolError(f"{path} already exists; pass overwrite=true to replace it")
        if format == "edl":
            stem = os.path.basename(path)[:-len(ext)] + "-"
            clash = [n for n in os.listdir(folder) if n.startswith(stem) and n.endswith(ext)]
            if clash:
                raise ToolError(f"{clash[0]} already exists in {folder}; pass overwrite=true to replace it")
    try:
        if format == "fcpxml":
            from classes.exporters.final_cut_pro import export_xml
            written = [export_xml(path)]
        else:
            from classes.exporters.edl import export_edl
            written = export_edl(path)
    except Exception as exc:
        raise ToolError(f"export failed: {exc}") from exc
    written = [w for w in written if w and os.path.isfile(w)]
    if not written:
        raise ToolError("nothing was written (no track has clips)")
    return ok(f"Exported the timeline as {'Final Cut Pro XML' if format == 'fcpxml' else 'EDL'}: "
              + ", ".join(os.path.basename(w) for w in written) + ".",
              format=format, files=written, sizes=[os.path.getsize(w) for w in written])


# ---------------------------------------------------------------------------
# Media collect / reclaim
# ---------------------------------------------------------------------------

def _media_changes(before_files, after_files, before_clips, after_clips):
    # Diffs exactly the fields media_collect.repoint_media writes (file path /
    # original_path, clip reader path); a new field there needs adding here.
    by_id = {f.get("id"): f for f in before_files}
    file_changes = []
    for f in after_files:
        old = by_id.get(f.get("id")) or {}
        if f.get("path") != old.get("path") or f.get("original_path") != old.get("original_path"):
            file_changes.append((f.get("id"), {"path": f.get("path"), "original_path": f.get("original_path")}))
    clip_by_id = {c.get("id"): c for c in before_clips}
    clip_changes = []
    for c in after_clips:
        old = clip_by_id.get(c.get("id")) or {}
        if (c.get("reader") or {}).get("path") != (old.get("reader") or {}).get("path"):
            clip_changes.append((c.get("id"), {"reader": c.get("reader")}))
    return file_changes, clip_changes


@editor_tool(
    "consolidate_project_media_tool",
    label="Collect / reclaim media",
    schema=obj({
        "action": enum(["collect", "reclaim"],
                       "collect = copy media stored outside the project into its assets folder (for handoff/archive); "
                       "reclaim = delete asset copies whose original still exists unchanged, pointing back at the original."),
    }, required=["action"]),
    background_safe=True,
    covers=("project.collect_media", "project.reclaim_media"),
)
def consolidate_project_media(action):
    """Collect external media into the project's assets folder, or reclaim duplicate copies (File > Collect / Reclaim).

    The project must be saved. collect copies files (the originals stay) and
    repoints the project at the copies -- one undo step points back at the
    originals. reclaim deletes copies that are byte-identical to their recorded
    original and repoints at the original; it cannot be undone (the copies are
    gone), so it adds no undo step. Nothing is deleted if its original is missing.
    """
    from classes import info
    from classes.media_collect import (
        collect_media_into_project, commit_reclaim, find_reclaimable_media, repoint_media,
    )

    proj = get_app().project
    path = project_path()
    if not path:
        raise ToolError("save the project first (save_project_tool); media is collected next to the project file")
    files = copy.deepcopy(proj.get("files") or [])
    clips = copy.deepcopy(proj.get("clips") or [])
    before_files, before_clips = copy.deepcopy(files), copy.deepcopy(clips)

    def _apply(file_changes, clip_changes):
        app = get_app()
        updates = app.updates

        def _write():
            for fid, values in file_changes:
                updates.update(["files", {"id": fid}], values)
            for cid, values in clip_changes:
                updates.update(["clips", {"id": cid}], values)

        if action == "reclaim":
            with th()._ignore_history(app):
                _write()
        else:
            _write()

    if action == "collect":
        done, other, errors = collect_media_into_project(files, clips, path, app_root=info.PATH)
        file_changes, clip_changes = _media_changes(before_files, files, before_clips, clips)
        if file_changes or clip_changes:
            on_main(lambda: _apply(file_changes, clip_changes))
    else:
        moves, other, errors = find_reclaimable_media(files, path)
        live = {"files": copy.deepcopy(files), "clips": copy.deepcopy(clips)}

        def _repoint(m):
            repoint_media(files, clips, m)
            fc, cc = _media_changes(live["files"], files, live["clips"], clips)
            live["files"], live["clips"] = copy.deepcopy(files), copy.deepcopy(clips)
            if fc or cc:
                on_main(lambda: _apply(fc, cc))

        # Saved before any copy is deleted: the project on disk never points at
        # removed media, and a failed save deletes nothing.
        done, commit_errors = commit_reclaim(moves, _repoint, lambda: save_to(path))
        if commit_errors and not done:
            raise ToolError(commit_errors[0])
        errors = errors + commit_errors
        file_changes, clip_changes = _media_changes(before_files, files, before_clips, clips)
    verb = "Copied" if action == "collect" else "Removed"
    summary = (f"{verb} {len(done)} file(s); {'skipped' if action == 'collect' else 'kept'} {len(other)}"
               f"{f'; {len(errors)} error(s)' if errors else ''}.")
    if not done and not file_changes:
        summary = ("Nothing to collect: all media is already in the project folder or unavailable."
                   if action == "collect" else "Nothing to reclaim: no asset copy has an identical original.")
    return ok(summary, action=action, files=done[:100], count=len(done), errors=errors[:20],
              files_repointed=len(file_changes), clips_repointed=len(clip_changes),
              undoable=action == "collect" and bool(file_changes or clip_changes))


# ---------------------------------------------------------------------------
# Caches and history
# ---------------------------------------------------------------------------

@editor_tool(
    "reset_caches_and_history_tool",
    label="Reset caches / history",
    schema=obj({
        "target": enum(["playback_cache", "waveforms", "undo_history"],
                       "playback_cache = drop cached preview frames (Ctrl+Shift+Esc; fixes stale previews); "
                       "waveforms = drop stored waveform data from files and clips (Edit > Clear > Waveform); "
                       "undo_history = forget every undo/redo step (Edit > Clear > History)."),
        "confirm": boolean("Must be true for undo_history: it cannot be undone. Ask the user first.", False),
    }, required=["target"]),
    covers=("project.clear_cache", "project.clear_waveforms", "project.reset_history"),
)
def reset_caches_and_history(target, confirm=False):
    """Clear the playback frame cache, the stored waveform data, or the undo history.

    playback_cache is harmless (frames re-render). waveforms is one undo step
    (waveforms are regenerated when shown again). undo_history empties undo and
    redo for this session and marks the project unsaved; it needs confirm=true.
    Never deletes clips or media.
    """
    app = get_app()
    if target == "playback_cache":
        on_main(lambda: window().actionClearAllCache_trigger())
        return ok("Cleared the playback cache; preview frames will re-render.", target=target)
    if target == "waveforms":
        from classes.waveform import clear_waveform_data
        files_cleared, clips_cleared = clear_waveform_data()
        if not (files_cleared or clips_cleared):
            return ok("No file or clip has stored waveform data; nothing cleared.", target=target,
                      files=0, clips=0)
        try:
            window().actionClearWaveformData.setEnabled(False)
        except Exception:
            pass
        return ok(f"Dropped waveform data from {files_cleared} file(s) and {clips_cleared} clip(s).",
                  target=target, files=files_cleared, clips=clips_cleared)
    if not confirm:
        raise ToolError("clearing the undo history cannot be undone; ask the user, then pass confirm=true")
    undo, redo = undo_depth()
    if not (undo or redo):
        return ok("The undo history is already empty.", target=target, undo_steps_cleared=0)
    app.project.has_unsaved_changes = True
    app.updates.reset()
    return ok(f"Cleared the undo history ({undo} undo and {redo} redo step(s)).", target=target,
              undo_steps_cleared=undo, redo_steps_cleared=redo)


# The topic modules register their tools on import (they import the helpers above).
from classes.editor_tools import (  # noqa: E402,F401
    project_export_prefs,
    project_export_profiles,
    project_export_render,
    project_export_views,
)
