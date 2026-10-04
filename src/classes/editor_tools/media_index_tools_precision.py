"""Precision tools: the exact frame of a cut, the exact edges of a voice, retakes, where the subject is, media health, edit style
and what surrounds a clip. Each works on a range or a file the agent is about to act on, computes it on demand from the original
media (measured), and keeps the answer on the shelf so asking again is free. Nothing here changes the project.
"""

from __future__ import annotations

from typing import Any, Dict, List

from classes.editor_tools._base import ToolError, array, boolean, enum, get_app, integer, number, obj, ok, on_main, string
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.media_files import FILE_TARGET, _display_name, resolve_files
from classes.editor_tools.media_index_tools import _all_files, _index_for
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


# ============================ get_voice_edges_tool ============================
def _audio_path(f) -> str:
    from classes.path_utils import absolute_media_path
    return absolute_media_path(f.data.get("path")) or str(f.data.get("path") or "")


def _speech_layer(shelf, sha: str) -> Dict[str, Any]:
    if not sha or not shelf.layer_ready(sha, "speech"):
        return {}
    return shelf.read_json(sha, "speech.json") or {}


@editor_tool(
    "get_voice_edges_tool",
    covers=("index.voice",),
    label="Exact voice edges",
    schema=obj({
        **FILE_TARGET,
        "start_seconds": number("Start of the stretch to look at, in the file's own seconds (a sentence from search_footage_tool, or a clip's in point).", None, minimum=0),
        "end_seconds": number("End of the stretch.", None, minimum=0),
    }, required=["start_seconds", "end_seconds"]),
    read_only=True,
)
def get_voice_edges(start_seconds, end_seconds, file_ids=None, file_query=""):
    """Find the exact instants a voice starts and stops in a stretch of a file, to cut right where it stops. Word times from
    transcription are only good to a few tenths of a second; this reads the sound itself and returns when the voice first rises
    above the room's noise and when it last falls back, the pauses inside it, how far the transcript's timing was off, and how
    sure it is (the contrast between the voice and the noise). Cut a little after the end it gives, not on it, so the last
    sound is not clipped. Measured from the original audio; kept on the shelf, so asking again is free.
    """
    from classes.media_index import voice
    if start_seconds is None or end_seconds is None or float(end_seconds) - float(start_seconds) < 0.2:
        raise ToolError("give start_seconds and end_seconds, at least 0.2 s apart")
    start, end = float(start_seconds), float(end_seconds)
    if end - start > voice.MAX_RANGE_SECONDS:
        raise ToolError(f"look at {int(voice.MAX_RANGE_SECONDS)} seconds or less at a time")
    f = resolve_files(file_ids, file_query)[0]
    sha = sha_of(f.data.get("fingerprint"))
    shelf = default_shelf()
    name = f"voice_{int(round(start * 1000))}_{int(round(end * 1000))}.json"
    saved = shelf.read_json(sha, name) if sha else None
    if isinstance(saved, dict) and saved.get("found") is not None:
        result = saved
        cached = True
    else:
        layer = _speech_layer(shelf, sha)
        words = [w for w in layer.get("words") or [] if float(w["endSec"]) > start and float(w["startSec"]) < end]
        pad = voice.SEARCH_BEFORE + 0.5
        lo = max(0.0, start - pad)
        try:
            samples = voice.read_audio(_audio_path(f), lo, end + pad)
        except RuntimeError as exc:
            raise ToolError(f"could not read the audio: {exc}")
        result = voice.voice_edges(samples, offset=lo, words=words or None, within=(start, end))
        result["range"] = [round(start, 3), round(end, 3)]
        result["transcript"] = " ".join(str(w.get("text") or "").strip() for w in words)[:200] or None
        if sha:
            shelf.write_json(sha, name, result)
        cached = False
    if not result.get("found"):
        return ok("No voice stands out from the noise in that stretch.", changed=False, file_id=str(f.id), voice=result, cached=cached)
    note = (f" (the transcript had it {result['start_offset']:+.2f} s / {result['end_offset']:+.2f} s off)" if "start_offset" in result else "")
    return ok(f"Voice from {result['start']:.2f} s to {result['end']:.2f} s{note}; {len(result['pauses'])} pause(s) inside.", changed=False, file_id=str(f.id),
              voice=result, cached=cached, advice="cut 0.05 to 0.1 s after the end, not on it")


# ============================ find_retakes_tool ============================
def _level_fn(fi):
    windows = ((getattr(fi, "audio", None) or {}).get("windows")) or []

    def level(a: float, b: float):
        import math
        mine = [w for w in windows if w["end"] > a and w["start"] < b and float(w.get("rms_db", -120.0)) > -80.0]
        if not mine:
            return None
        return 10.0 * math.log10(sum(10 ** (float(w["rms_db"]) / 10.0) for w in mine) / len(mine))
    return level


@editor_tool(
    "find_retakes_tool",
    covers=("index.retakes",),
    label="Find retakes",
    schema=obj({
        **FILE_TARGET,
        "min_similarity": number("How alike two sentences must be to count as the same line (0 to 1).", 0.75, minimum=0.5, maximum=1.0),
    }),
    read_only=True,
)
def find_retakes(file_ids=None, file_query="", min_similarity=0.75):
    """Find lines that were said more than once (retakes and false starts) in the transcripts of the project's files, so the best
    take can be chosen and the others cut. Each group lists the takes in order with what can be measured about each: filler
    words (um, uh), whether it finished its sentence, how fast it was said, its level, and the pause after it; and which take
    looks best and why (the one with the fewest fillers that finished its sentence, the later on a tie). That is a starting point:
    listen to or look at the candidates before cutting. Uses the local transcripts, no cloud.
    """
    from classes.media_index import voice
    shelf = default_shelf()
    files = _media_files(file_ids, file_query)
    out: List[Dict[str, Any]] = []
    scanned = 0
    for f in files:
        sha = sha_of(f.data.get("fingerprint"))
        layer = _speech_layer(shelf, sha)
        sentences = layer.get("sentences") or []
        if len(sentences) < 2:
            continue
        scanned += 1
        fi = _index_for(f, shelf)
        for g in voice.retake_groups(sentences, layer.get("words") or [], threshold=float(min_similarity), level_db=_level_fn(fi) if fi else None):
            out.append({"file_id": str(f.id), "name": _display_name(f.data), **g})
    out = out[:100]
    head = (f"{len(out)} line(s) said more than once across {scanned} file(s) with a transcript" if out
            else f"no retakes found in {scanned} file(s) with a transcript")
    return ok(head, changed=False, groups=out, files_with_transcript=scanned,
              note="likely_best is a heuristic over measured facts (fillers, finished sentence, order); check it before cutting")


# ============================ refine_cut_tool ============================
def _refined(f, shelf, sha: str, probe: Dict[str, Any], near: float, radius: float) -> Dict[str, Any]:
    from classes.media_index import refine
    name = f"refine_{int(round(near * 1000))}_{int(round(radius * 1000))}.json"
    saved = shelf.read_json(sha, name) if sha else None
    if isinstance(saved, dict) and saved.get("kind"):
        return {**saved, "cached": True}
    try:
        result = refine.refine_cut(_audio_path(f), probe, near, radius)
    except RuntimeError as exc:
        raise ToolError(f"could not read the picture: {exc}")
    if sha:
        shelf.write_json(sha, name, result)
    return {**result, "cached": False}


@editor_tool(
    "refine_cut_tool",
    covers=("index.refine",),
    label="Exact cut frame",
    schema=obj({
        **FILE_TARGET,
        "near_seconds": number("Roughly where the cut is, in the file's own seconds (a boundary from search_footage_tool or get_segment_dossier_tool).", None, minimum=0),
        "shot_id": integer("Instead of near_seconds: a shot id from the dossier; both its start and its end are made exact.", None, minimum=0),
        "radius_seconds": number("How far either side of the hint to look.", 0.5, minimum=0.1, maximum=2.0),
    }),
    read_only=True,
)
def refine_cut(file_ids=None, file_query="", near_seconds=None, shot_id=None, radius_seconds=0.5):
    """Find the exact frame of a cut. The index finds boundaries to about a tenth of a second; this decodes the stretch around
    one at every frame (with each frame's real timestamp, so variable frame rate is safe) and returns the first frame of the
    new shot for a hard cut, or the whole span and centre of a dissolve or a fade through black. A flash is not a cut and
    comes back as none. It tells you how far the hint was off. Run it only for the cut you are about to make: it is computed on
    demand and kept on the shelf. Measured from the original file.
    """
    if near_seconds is None and shot_id is None:
        raise ToolError("give near_seconds (roughly where the cut is) or a shot_id")
    f = resolve_files(file_ids, file_query)[0]
    if str(f.data.get("media_type") or "video") != "video":
        raise ToolError("only video has cuts")
    sha = sha_of(f.data.get("fingerprint"))
    shelf = default_shelf()
    probe = probe_media(_audio_path(f))
    if not probe.get("ok") or not probe.get("video"):
        raise ToolError("could not read the picture of this file")
    radius = float(radius_seconds)
    if shot_id is not None:
        structure = (shelf.read_json(sha, "structure.json") if sha else None) or {}
        shot = next((s for s in structure.get("shots") or [] if int(s["id"]) == int(shot_id)), None)
        if shot is None:
            raise ToolError(f"no shot {shot_id} in this file's index (get_segment_dossier_tool lists the shots)")
        edges = {}
        last = max(int(s["id"]) for s in structure["shots"])
        for label, at, skip in (("start", float(shot["start"]), int(shot_id) == 0), ("end", float(shot["end"]), int(shot_id) == last)):
            edges[label] = {"kind": "file_edge", "t": round(at, 3), "note": "the start or end of the file"} if skip else _refined(f, shelf, sha, probe, at, radius)
        bits = [f"{k} {v['t']:.3f} s" + (f" (frame {v['frame']})" if "frame" in v and v.get("kind") != "file_edge" else "") for k, v in edges.items() if "t" in v]
        return ok(f"Shot {shot_id}: " + ", ".join(bits) + ".", changed=False, file_id=str(f.id), shot_id=int(shot_id), edges=edges)
    result = _refined(f, shelf, sha, probe, float(near_seconds), radius)
    if result["kind"] == "none":
        return ok(f"No cut near {float(near_seconds):.2f} s: {result.get('reason', 'nothing changes like a cut')}.", changed=False, file_id=str(f.id), cut=result)
    what = {"hard": "cut", "dissolve": "dissolve", "fade": "fade through black"}[result["kind"]]
    frame = f" (frame {result['frame']} at {result['fps']:g} fps)" if "frame" in result else ""
    span = f", from {result['start']:.3f} s to {result['end']:.3f} s" if "start" in result else ""
    return ok(f"The {what} is at {result['t']:.3f} s{frame}{span}; the hint was {abs(result['hint_error']):.2f} s {'early' if result['hint_error'] > 0 else 'late'}.",
              changed=False, file_id=str(f.id), cut=result)


# ============================ framing: where the subject is, and reframing to keep it in shot ============================
ASPECTS = {"9:16": 9 / 16, "4:5": 4 / 5, "1:1": 1.0, "3:4": 3 / 4, "2:3": 2 / 3, "16:9": 16 / 9, "21:9": 21 / 9}


def _frame_aspect(text: str) -> float:
    text = str(text or "project").strip().lower()
    if text in ("", "project"):
        project = _project_facts()
        if not project["width"] or not project["height"]:
            raise ToolError("the project has no size yet")
        return project["width"] / project["height"]
    if text in ASPECTS:
        return ASPECTS[text]
    try:
        w, _, h = text.partition(":")
        value = float(w) / float(h)
    except (TypeError, ValueError, ZeroDivisionError):
        raise ToolError(f"aspect must be 'project' or one of {', '.join(ASPECTS)} (or w:h)")
    if not 0.2 <= value <= 5.0:
        raise ToolError("that aspect is outside 1:5 to 5:1")
    return value


def _framing_cached(f, shelf, sha: str, path: str, start: float, end: float, fraction: float, axis: str) -> Dict[str, Any]:
    from classes.media_index import framing
    name = f"framing_{int(round(start * 1000))}_{int(round(end * 1000))}_{int(round(fraction * 1000))}_{axis}.json"
    saved = shelf.read_json(sha, name) if sha else None
    if isinstance(saved, dict) and saved.get("samples"):
        return {**saved, "cached": True}
    try:
        result = framing.framing_of(path, start, end, fraction, axis)
    except RuntimeError as exc:
        raise ToolError(f"could not read the picture: {exc}")
    if sha:
        shelf.write_json(sha, name, result)
    return {**result, "cached": False}


def _window_of(source_aspect: float, frame_aspect: float):
    """(axis, fraction of the picture a crop window covers) for filling a frame of another shape, or None when the shapes match."""
    from classes.media_index import framing
    if abs(source_aspect - frame_aspect) / frame_aspect < framing.SAME_SHAPE:
        return None
    return ("x", frame_aspect / source_aspect) if source_aspect > frame_aspect else ("y", source_aspect / frame_aspect)


@editor_tool(
    "get_framing_tool",
    covers=("index.framing",),
    label="Where the subject is",
    schema=obj({
        **FILE_TARGET,
        "start_seconds": number("Start of the stretch, in the file's own seconds.", None, minimum=0),
        "end_seconds": number("End of the stretch.", None, minimum=0),
        "aspect": string("The frame shape to fill: 'project' (default), 9:16, 4:5, 1:1, 3:4, 2:3, 16:9, 21:9 or w:h.", "project"),
    }, required=["start_seconds", "end_seconds"]),
    read_only=True,
)
def get_framing(start_seconds, end_seconds, file_ids=None, file_query="", aspect="project"):
    """Find where the subject of a shot is, and where a crop window of another shape (a vertical 9:16 from landscape footage,
    say) should sit to keep it in shot. It reads a few frames, finds the part of the picture that draws the eye, and returns
    the window's position over time, whether the subject moves, and a confidence. It is an attention heuristic, not an
    understanding of the picture: a small subject against bright clutter can be missed, and when confidence is low it says so
    (check those shots by eye). Faces count extra when the people index is on. The result includes the values to give the
    clip (set_clip_properties_tool, or reframe_to_subject_tool to do it for you). Measured from the original; kept on the shelf.
    """
    from classes.media_index import framing
    if float(end_seconds) - float(start_seconds) < 0.2:
        raise ToolError("give a stretch at least 0.2 s long")
    f = resolve_files(file_ids, file_query)[0]
    if str(f.data.get("media_type") or "video") not in ("video", "image"):
        raise ToolError("only pictures have a subject to frame")
    probe = probe_media(_audio_path(f))
    video = probe.get("video") or {}
    if not video.get("width") or not video.get("height"):
        raise ToolError("could not read the picture of this file")
    source_aspect = video["width"] / video["height"]
    frame_aspect = _frame_aspect(aspect)
    window = _window_of(source_aspect, frame_aspect)
    if window is None:
        return ok("The footage already has the shape of that frame: nothing to reframe.", changed=False, file_id=str(f.id), source_aspect=round(source_aspect, 3))
    axis, fraction = window
    sha = sha_of(f.data.get("fingerprint"))
    result = _framing_cached(f, default_shelf(), sha, _audio_path(f), float(start_seconds), float(end_seconds), fraction, axis)
    place = framing.offsets(result, source_aspect, frame_aspect)
    prop = place["property"]
    suggestion = {"properties": {"scale": "Crop", "gravity": "Center", prop: place["static"]}}
    if place.get("keyframes"):
        suggestion["keyframes"] = {prop: place["keyframes"], "note": "the subject moves: set these with set_clip_properties_tool(at_seconds=...) on the timeline, or let reframe_to_subject_tool do it"}
    sure = "" if result["sure"] else " Not sure: nothing stands out clearly, so check this shot by eye."
    direction = "across" if axis == "x" else "down"
    moving = ""
    if result["moves"]:
        moving = " (the subject moves: " + ", ".join("%.0f%%" % (r["window_center"] * 100) for r in result["samples"]) + ")"
    head = "Keep the window at %.0f%% of the way %s the picture%s; confidence %.2f.%s" % (result["center"] * 100, direction, moving, result["confidence"], sure)
    return ok(head, changed=False, file_id=str(f.id), framing=result, placement=place, suggestion=suggestion, source_aspect=round(source_aspect, 3), frame_aspect=round(frame_aspect, 3))


@editor_tool(
    "reframe_to_subject_tool",
    covers=("index.reframe",),
    label="Reframe to the subject",
    schema=obj({
        "timeline_clip_ids": array({"type": "string"}, "Clips to reframe (default: every full-frame picture clip whose shape differs from the project's)."),
        "aspect": string("The frame shape to fill: 'project' (default), 9:16, 4:5, 1:1, 3:4, 2:3, 16:9, 21:9 or w:h.", "project"),
        "dry_run": boolean("Only say what each clip would get; change nothing.", False),
    }),
)
def reframe_to_subject(timeline_clip_ids=None, aspect="project", dry_run=False):
    """Fill the frame with footage of another shape (landscape clips in a vertical project, say) and keep the subject in shot,
    instead of cutting people in half with a centre crop. For each full-frame picture clip it sets crop-to-fill and positions
    the picture on the subject (a fixed position, or keyframes across the clip when the subject moves), all in one undo step.
    Clips moved or resized as picture-in-picture, titles, and clips on locked tracks are left alone, and every clip reports
    its confidence: where nothing stands out it still centres but says to check it by eye. Use get_framing_tool first to
    look at one shot, or dry_run to see the plan.
    """
    from classes.editor_tools import clip_props, clip_props_model as cpm
    from classes.editor_tools._base import is_locked, refresh_preview
    from classes.editor_tools.project_export_profiles import GRAVITY_CENTER, SCALE_CROP, _constant, _source_aspect
    from classes.media_index import framing
    from classes.query import Clip, File
    frame_aspect = _frame_aspect(aspect)
    wanted = {str(x) for x in timeline_clip_ids} if timeline_clip_ids else None
    shelf = default_shelf()
    plans: List[Any] = []
    report: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for clip in Clip.filter():
        if wanted is not None and str(clip.id) not in wanted:
            continue
        data = clip.data if isinstance(clip.data, dict) else {}
        reader = data.get("reader") or {}
        path = str(reader.get("path") or "")
        if not reader.get("has_video", True) or reader.get("media_type") == "audio" or path.lower().endswith(".svg"):
            continue
        source_aspect = _source_aspect(data)
        window = _window_of(source_aspect, frame_aspect) if source_aspect else None
        if window is None:
            continue
        if not (_constant(data.get("scale_x"), 1.0) and _constant(data.get("scale_y"), 1.0) and _constant(data.get("location_x"), 0.0) and _constant(data.get("location_y"), 0.0)):
            skipped.append({"timeline_clip_id": str(clip.id), "reason": "resized or moved (picture-in-picture) clip kept"})
            continue
        if is_locked(int(data.get("layer") or 0)):
            skipped.append({"timeline_clip_id": str(clip.id), "reason": "track is locked"})
            continue
        axis, fraction = window
        file_obj = File.get(id=str(data.get("file_id") or ""))
        sha = sha_of((file_obj.data if file_obj else {}).get("fingerprint"))
        from classes.path_utils import absolute_media_path
        media_path = absolute_media_path(path) or path
        start, end = float(data.get("start") or 0.0), float(data.get("end") or 0.0)
        result = _framing_cached(file_obj, shelf, sha, media_path, start, end, fraction, axis)
        place = framing.offsets(result, source_aspect, frame_aspect)
        prop = place["property"]
        ct = cpm.ClipTime.of(data)
        if place.get("keyframes"):                  # X is a 1-based clip frame that counts the trimmed-off source frames
            points = [cpm.make_point(ct.from_source_seconds(kf["t"]), kf["value"], cpm.BEZIER) for kf in place["keyframes"]]
            curve = {"Points": sorted(points, key=lambda p: float(p["co"]["X"]))}
        else:
            curve = {"Points": [cpm.make_point(1, place["static"], cpm.BEZIER)]}
        values = {"scale": SCALE_CROP, "gravity": GRAVITY_CENTER, prop: curve}
        plans.append((str(clip.id), values))
        report.append({"timeline_clip_id": str(clip.id), "name": str(data.get("title") or ""), "property": prop, "value": place["static"],
                       "moving": bool(result["moves"]), "keyframes": len(place.get("keyframes") or []), "confidence": result["confidence"], "sure": result["sure"],
                       "clamped": place["clamped"]})
    if not plans:
        return ok("Nothing to reframe: no full-frame picture clip has a shape different from that frame.", changed=False, skipped=skipped, clips=[])
    unsure = [r for r in report if not r["sure"]]
    note = f" {len(unsure)} clip(s) had no clear subject and are centred: check them by eye." if unsure else ""
    if dry_run:
        return ok(f"Would reframe {len(plans)} clip(s) to keep the subject in shot.{note}", changed=False, dry_run=True, clips=report, skipped=skipped)

    def apply() -> None:
        with clip_props._transaction():
            for clip_id, values in plans:
                clip_props.save_clip_values(clip_id, values)
        refresh_preview()

    on_main(apply)
    return ok(f"Reframed {len(plans)} clip(s) to keep the subject in shot (crop to fill, positioned on the subject).{note}", changed=True, clips=report, skipped=skipped)


# ============================ get_edit_style_tool ============================
@editor_tool(
    "get_edit_style_tool",
    covers=("index.style",),
    label="Style of an edit",
    schema=obj({
        **FILE_TARGET,
        "compare_to_timeline": boolean("Also measure the timeline being built and say where it differs from this reference, and what to do.", True),
    }),
    read_only=True,
)
def get_edit_style(file_ids=None, file_query="", compare_to_timeline=True):
    """Read the style of a finished video in numbers, so "make it like this" has something to match: how fast it cuts (shot
    lengths, cuts a minute, whether it speeds up), how it changes shots (hard cuts, dissolves, fades), how much of it is
    still, panning or handheld, how many cuts land on the beat, how much is speech and how fast it is spoken, how loud it is,
    its look, and how much text is on screen. With compare_to_timeline it measures the timeline being built the same way and
    lists where the two differ by enough to matter, each with what to do and which tool does it (the colour match itself is
    match_reference_tool). It reports measurements; deciding what to copy is yours. The reference must be indexed.
    """
    from classes.media_index import style
    shelf = default_shelf()
    f = resolve_files(file_ids, file_query)[0]
    fi = _index_for(f, shelf)
    if fi is None or not fi.shots:
        raise ToolError("that file has no shot index yet (index_status_tool shows what is still being worked on)")
    reference = style.edit_style(fi)
    yours, gaps = None, []
    if compare_to_timeline:
        clips, _tracks, transitions, _opacity, _links = _timeline_facts()
        pictures = [c for c in clips if c.kind in ("video", "image")]
        if pictures:
            beats, _speech = _timeline_beats(clips)
            yours = style.timeline_style(pictures, beats, max(c.end for c in clips), transitions=len(transitions))
            gaps = style.compare_style(reference, yours)
    pacing = reference.get("pacing") or {}
    head = (f"{_display_name(f.data)}: {pacing.get('average_shot', '?')} s average shot, {pacing.get('cuts_per_minute', '?')} cuts a minute, {pacing.get('shape', 'steady')} pace"
            + (f"; {len(gaps)} way(s) your timeline differs." if yours else "."))
    return ok(head, changed=False, file_id=str(f.id), reference=reference, timeline=yours, gaps=gaps)
