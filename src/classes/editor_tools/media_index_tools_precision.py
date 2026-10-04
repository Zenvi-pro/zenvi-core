"""Precision tools: the exact frame of a cut, the exact edges of a voice, retakes, where the subject is, media health, edit style
and what surrounds a clip. Each works on a range or a file the agent is about to act on, computes it on demand from the original
media (measured), and keeps the answer on the shelf so asking again is free. Nothing here changes the project.
"""

from __future__ import annotations

from typing import Any, Dict, List

from classes.editor_tools._base import boolean, get_app, obj, ok
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.media_files import FILE_TARGET, _display_name, resolve_files
from classes.editor_tools.media_index_tools import _all_files
from classes.media_index import health
from classes.media_index.facts import technical_of
from classes.media_index.probe import probe_media
from classes.media_index.store import default_shelf, sha_of

MAX_FILES = 80


def _project_facts() -> Dict[str, Any]:
    project = get_app().project
    fps = project.get("fps") or {}
    try:
        rate = float(fps.get("num") or 30) / float(fps.get("den") or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        rate = 0.0
    return {"width": int(project.get("width") or 0), "height": int(project.get("height") or 0), "fps": rate}


def _media_files(file_ids=None, file_query=""):
    if file_ids or file_query:
        return resolve_files(file_ids, file_query)
    return [f for f in _all_files() if str(f.data.get("media_type") or "video") in ("video", "audio")]


def _technical(f, shelf) -> Dict[str, Any]:
    """The technical facts of a file: from the shelf when indexed after they were kept, else a quick local probe."""
    sha = sha_of(f.data.get("fingerprint"))
    saved = ((shelf.manifest(sha).get("source") or {}).get("technical") if sha else None) or {}
    if saved:
        return saved
    from classes.path_utils import absolute_media_path
    probe = probe_media(absolute_media_path(f.data.get("path")) or str(f.data.get("path") or ""))
    return technical_of(probe) if probe.get("ok") else {}


# ============================ check_media_health_tool ============================
@editor_tool(
    "check_media_health_tool",
    covers=("index.health",),
    label="Check media health",
    schema=obj({
        **FILE_TARGET,
        "only_problems": boolean("Leave out files with nothing worth knowing.", True),
    }),
    read_only=True,
)
def check_media_health(file_ids=None, file_query="", only_problems=True):
    """Find what about the project's footage will cause trouble before editing: a variable frame rate (typical of
    phone clips: audio and picture drift apart), interlaced or HDR footage, clips whose frame rate does not fit the
    project, clips too small or in the wrong orientation for it, and heavy files that will play back slowly. Each
    issue says how serious it is and what to do about it. Reads the file's technical facts (kept with the index, or a
    quick local look at the file); changes nothing.
    """
    shelf = default_shelf()
    project = _project_facts()
    files = _media_files(file_ids, file_query)
    rows: List[Dict[str, Any]] = []
    for f in files[:MAX_FILES]:
        tech = _technical(f, shelf)
        sha = sha_of(f.data.get("fingerprint"))
        camera = ((shelf.manifest(sha).get("source") or {}).get("camera") if sha else None) or {}
        issues = health.file_issues(tech, project) if tech else []
        rows.append({"file_id": str(f.id), "name": _display_name(f.data), "fps": (tech.get("video") or {}).get("fps"),
                     "orientation": (tech.get("video") or {}).get("orientation"), "camera": camera or None, "issues": issues,
                     "checked": bool(tech)})
    rollup = health.project_summary(rows, project)
    shown = [r for r in rows if any(i["severity"] != "info" for i in r["issues"])] if only_problems else rows
    head = (f"{rollup['with_problems']} of {rollup['files']} file(s) have something worth fixing" if rollup["with_problems"]
            else f"no problems found in {rollup['files']} file(s)")
    if rollup.get("mixed_frame_rates"):
        head += "; mixed frame rates"
    return ok(head, changed=False, files=shown, rollup=rollup, project={"width": project["width"], "height": project["height"], "fps": project["fps"]},
              truncated=max(0, len(files) - MAX_FILES) or None)
