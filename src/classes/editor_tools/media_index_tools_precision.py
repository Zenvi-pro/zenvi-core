"""Precision tools: the exact frame of a cut, the exact edges of a voice, retakes, where the subject is, media health, edit style
and what surrounds a clip. Each works on a range or a file the agent is about to act on, computes it on demand from the original
media (measured), and keeps the answer on the shelf so asking again is free. Nothing here changes the project.
"""

from __future__ import annotations

from typing import Any, Dict, List

from classes.editor_tools._base import ToolError, boolean, enum, get_app, number, obj, ok, string
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.media_files import FILE_TARGET, _display_name, resolve_files
from classes.editor_tools.media_index_tools import _all_files
from classes.media_index import context, health
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


# ============================ get_clip_context_tool ============================
def _opacity_of(alpha: Any) -> float:
    """The average opacity of a clip from its alpha keyframes (1.0 when it has none)."""
    points = (alpha or {}).get("Points") if isinstance(alpha, dict) else None
    ys = []
    for p in points or []:
        co = p.get("co") if isinstance(p, dict) else None
        try:
            ys.append(float((co or {}).get("Y")))
        except (TypeError, ValueError):
            continue
    return min(1.0, max(0.0, sum(ys) / len(ys))) if ys else 1.0


def _timeline_facts():
    """Clips, tracks, transitions, opacity and links as plain data for ``media_index.context``."""
    from classes.editor_tools.media_index_tools_review import build_timeline
    clips, _project, objs = build_timeline()
    project = get_app().project
    tracks = {}
    for layer in project.get("layers") or []:
        try:
            number_ = int(layer.get("number") or 0)
        except (TypeError, ValueError):
            continue
        tracks[number_] = context.Track(layer=number_, label=str(layer.get("label") or layer.get("name") or ""), locked=bool(layer.get("lock", False)),
                                        sync_locked=(bool(layer["sync_locked"]) if "sync_locked" in layer else None))
    transitions = [context.Transition(id=str(e.get("id") or ""), layer=int(e.get("layer") or 0), start=float(e.get("position") or 0.0),
                                      end=float(e.get("end") or 0.0), title=str(e.get("title") or "")) for e in project.get("effects") or [] if isinstance(e, dict)]
    opacity = {cid: _opacity_of((o.data or {}).get("alpha")) for cid, o in objs.items()}
    groups = {cid: str((o.data or {}).get("link_group_id") or "") for cid, o in objs.items()}
    links = groups if any(groups.values()) else None
    return clips, tracks, transitions, opacity, links


def _timeline_beats(clips):
    """Beat times on the timeline from the longest music clip with a detected tempo, and where voices speak."""
    from classes.editor_tools.media_index_tools_edit import _index_provider
    index_for = _index_provider()
    beds = [c for c in clips if c.role == "music" and c.length > 0 and ((getattr(index_for(c), "audio", None) or {}).get("tempo") or {}).get("beats")]
    beats: List[float] = []
    if beds:
        bed = max(beds, key=lambda c: c.length)
        beats = sorted(t for t in (bed.to_timeline(x) for x in index_for(bed).audio["tempo"]["beats"] if bed.src_in <= x <= bed.src_out) if bed.start <= t <= bed.end)
    speech = [w for c in clips if c.role == "speech" for w in c.windows]
    return beats, speech


@editor_tool(
    "get_clip_context_tool",
    covers=("index.context",),
    label="What is around a clip",
    schema=obj({
        "timeline_clip_id": string("The timeline clip to look at (timeline_clip_id from get_timeline_state_tool)."),
        "if_edit": enum(["", "delete", "trim_end", "trim_start", "lengthen"],
                        "Also predict what would move if the clip were deleted, shortened or lengthened with the clips after it closing up (a ripple). "
                        "Nothing is changed.", ""),
        "seconds": number("How many seconds to trim or lengthen (for trim_end, trim_start, lengthen).", 0.0, minimum=0, maximum=3600),
    }, required=["timeline_clip_id"]),
    read_only=True,
)
def get_clip_context(timeline_clip_id, if_edit="", seconds=0.0):
    """Know what is under, above and around a clip before changing it. Shows the clips stacked over and under it (and the
    parts of it that something opaque above hides), the clip before and after it on its track with the seam between them (hard
    cut, gap, overlap or a transition), the other clips whose sound plays at the same time with their levels, its linked partners
    (the video and audio of one recording) and the track's lock. With if_edit it predicts what a delete, trim or lengthen would
    move if the later clips close up: which clips shift and by how much, which stay put on other tracks, which links would
    split, and which cuts would stop landing on a beat or start landing inside speech. It never changes the project; the edits
    themselves are the editing tools'.
    """
    from classes.media_index import context
    clips, tracks, transitions, opacity, links = _timeline_facts()
    if not any(c.id == str(timeline_clip_id) for c in clips):
        raise ToolError(f"no timeline clip {timeline_clip_id!r} (get_timeline_state_tool lists them)")
    info = context.clip_context(str(timeline_clip_id), clips, tracks, transitions, opacity, links)
    prediction = None
    if if_edit:
        beats, speech = _timeline_beats(clips)
        try:
            prediction = context.predict_edit(str(timeline_clip_id), if_edit, float(seconds), clips, tracks, links, beats, speech)
        except ValueError as exc:
            raise ToolError(str(exc))
    bits = []
    vis = info.get("visible") or {}
    if vis.get("fully_hidden"):
        bits.append("completely hidden by clips above it")
    elif vis.get("hidden_seconds"):
        bits.append(f"{vis['hidden_seconds']:.1f} s hidden by clips above")
    bits.append(f"{len(info['above'])} above, {len(info['below'])} below, {len(info['sound_with'])} playing sound with it")
    if prediction:
        bits.append(f"{if_edit}: {prediction['shifted_count']} clip(s) would shift by {prediction['shift']:+g} s")
    return ok(f"{info['clip']['name'] or timeline_clip_id}: " + "; ".join(bits) + ".", changed=False, context=info, prediction=prediction)
