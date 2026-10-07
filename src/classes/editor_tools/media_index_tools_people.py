"""Tools for people identity: find who is on screen, name people, search and locate by person. Local only, opt-in.

Faces are matched on this computer by two small ONNX models and never uploaded. A person has a name only because the user gave
one. Every match carries a confidence, and "unsure" is reported as unsure. Nothing here returns or logs the numbers a face becomes.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from classes.editor_tools._base import ToolError, array, boolean, integer, number, obj, ok, string
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.media_index_tools import project_indexes
from classes.logger import log
from classes.media_index import handoff, people as pp, people_models as pm, voice_diarize
from classes.media_index.store import default_shelf
from classes.media_index.flags import people_enabled

SCAN_BUDGET_SECONDS = 120.0
MAX_LISTED = 40
MINOR_SECONDS = 2.0


def _need_on() -> None:
    if not people_enabled():
        raise ToolError("people identity is off: turn on 'recognise people' in Preferences (it needs Media Index v2 on too)")


def _fi_by_file(file_ids: Optional[List[str]]):
    files, missing = project_indexes(file_ids)
    return [f for f in files if f.media_type == "video"], missing


def _name_of(reg: Dict[str, Any], pid: Optional[str]) -> Optional[str]:
    p = next((q for q in reg["people"] if q["id"] == pid), None)
    return (p or {}).get("name")


# ============================ status and setup ============================
@editor_tool(
    "people_status_tool",
    covers=("index.people",),
    label="People identity status",
    schema=obj({}),
    read_only=True,
)
def people_status():
    """Say whether people identity is on and ready: the preference, the onnxruntime package, the two models (sizes and licences) and how
    much is stored. Faces and voices are matched on this computer and never uploaded; nobody is named unless the user names them.
    """
    st = pm.status()
    reg = pp.load_registry()
    enabled = people_enabled()
    ready = st["runtime"] and st["faces_ready"]
    todo = []
    if not enabled:
        todo.append("turn on 'recognise people' in Preferences")
    if not st["runtime"]:
        todo.append("install onnxruntime (pip install -r requirements-speech.txt)")
    elif not st["faces_ready"] or not st["voices_ready"]:
        todo.append(f"download the models ({st['download_bytes'] / 1e6:.0f} MB, one time) with setup_people_tool")
    if enabled and ready and not st["voices_ready"]:
        head = f"People identity is ready for faces; voices need the voice model ({st['download_bytes'] / 1e6:.0f} MB, one time): run setup_people_tool."
    else:
        head = "People identity is " + ("ready." if enabled and ready else "not ready: " + "; ".join(todo) + ".")
    return ok(head, changed=False, enabled=enabled, ready=bool(enabled and ready),
              runtime=st["runtime"], models=st["models"], voices_ready=st["voices_ready"], licenses=st["licenses"], attributions=st["attributions"], people=len(reg["people"]), named=sum(1 for p in reg["people"] if p.get("name")),
              stored=pp.has_data(), privacy="faces stay on this computer: never uploaded, never logged, not in shared or exported indexes; erase_people_data_tool removes all of it")


@editor_tool(
    "setup_people_tool",
    covers=("index.people",),
    label="Set up people identity",
    schema=obj({}),
)
def setup_people():
    """Download the people models (faces about 37 MB, voices about 27 MB, one time, each checked against a fixed checksum) so people can be
    recognised on this computer. Only when the user asked for people recognition: it fetches files from the internet (the models only, never any of
    their media). Needs the preference on and the onnxruntime package.
    """
    _need_on()
    if not pm.runtime_available():
        raise ToolError("people identity needs the onnxruntime package: pip install -r requirements-speech.txt")
    out = pm.download()
    if not out["ok"]:
        raise ToolError(out["error"])
    return ok("The people models are installed." if out["installed"] else "The people models were already installed.", changed=bool(out["installed"]),
              installed=out["installed"], models=pm.status()["models"])


# ============================ scanning ============================
def _scan_one(fi) -> Dict[str, Any]:
    from classes.path_utils import absolute_media_path
    try:
        return pp.scan_video(absolute_media_path(fi.path) or fi.path, fi.sha, fi.shots, fi.duration)
    except RuntimeError as exc:
        raise ToolError(str(exc))


def _scan_voices_one(fi) -> Dict[str, Any]:
    """Tell the voices in one file apart from its transcript's word times (nothing to do when it has no speech)."""
    from classes.path_utils import absolute_media_path
    try:
        got = pp.scan_file_voices(absolute_media_path(fi.path) or fi.path, fi.sha, fi.duration, default_shelf())
    except RuntimeError as exc:
        raise ToolError(str(exc))
    return {"voices": len(got["scan"]["speakers"]), **got["assigned"]} if got["had_speech"] else {"voices": 0, "why": "no speech to listen to"}


@editor_tool(
    "scan_people_tool",
    covers=("index.people",),
    label="Find the people in footage",
    schema=obj({
        "file_ids": array({"type": "string"}, "Project files to scan (default: every indexed video or audio file not scanned yet)."),
        "rescan": boolean("Scan again files that already have a scan.", False),
    }),
)
def scan_people(file_ids=None, rescan=False):
    """Look through indexed videos for faces and note where each person is on screen (about half a second at a time), and listen to the
    speech in each file to tell the voices apart, all on this computer. Fast (a 30 s clip takes a couple of seconds). People are unnamed
    until the user names them (name_person_tool). Needs the preference on and the models (people_status_tool says what is missing);
    voices need the voice model and a transcript. It stops after about two minutes and says what is left.
    """
    _need_on()
    st = pm.status()
    if not st["runtime"] or not st["faces_ready"]:
        raise ToolError("people models are not ready: " + ("install onnxruntime (pip install -r requirements-speech.txt)" if not st["runtime"] else "run setup_people_tool first"))
    all_files, missing = project_indexes(file_ids or None)
    started, rows, left = time.time(), [], []
    for fi in all_files:
        is_video = fi.media_type == "video"
        need_faces = is_video and (rescan or not pp.load_scan(fi.sha))
        need_voice = st["voices_ready"] and (rescan or not pp.load_voice_scan(fi.sha))
        if not (need_faces or need_voice):
            continue
        if time.time() - started > SCAN_BUDGET_SECONDS:
            left.append(fi.name)
            continue
        row: Dict[str, Any] = {"file_id": fi.file_id, "name": fi.name}
        try:
            if need_faces:
                got = _scan_one(fi)
                row.update(tracks=len(got["scan"]["tracks"]), frames=got["scan"]["sampled"], **got["assigned"])
            if need_voice:
                row.update(_scan_voices_one(fi))
        except ToolError as exc:
            row["error"] = str(exc)
        except pm.PeopleUnavailable as exc:
            raise ToolError(str(exc))
        except Exception as exc:  # noqa: BLE001 - one file failing must not stop the rest; say which
            log.warning("people scan failed", exc_info=True)
            row["error"] = f"could not scan: {exc}"[:160]
        rows.append(row)
    done = [r for r in rows if "error" not in r]
    return ok(f"Scanned {len(done)} file(s)" + (f", {len(left)} left (run it again)" if left else "") + ".", changed=bool(done), scanned=rows, left=left or None, not_indexed=missing[:12] or None)


# ============================ who is who ============================
def _scans_for(files) -> Dict[str, Dict[str, Any]]:
    return {fi.sha: s for fi in files for s in [pp.load_scan(fi.sha)] if s}


def _voice_scans_for(files) -> Dict[str, Dict[str, Any]]:
    return {fi.sha: s for fi in files for s in [pp.load_voice_scan(fi.sha)] if s}


@editor_tool(
    "list_people_tool",
    covers=("index.people",),
    label="List people",
    schema=obj({
        "include_minor": boolean("Also list people seen in a single brief shot (usually passers-by).", False),
    }),
    read_only=True,
)
def list_people(include_minor=False):
    """The people found so far: id, the name the user gave (or none), how many files and shots they are in and for how many seconds,
    most on-screen first. Unnamed people are only numbers: ask the user who they are, never guess a name.
    """
    _need_on()
    reg = pp.load_registry()
    files, _missing = _fi_by_file(None)
    scans, vscans = _scans_for(files), _voice_scans_for(project_indexes(None)[0])
    rows = pp.person_summary(reg, scans)
    spoken = pp.speaking_seconds(reg, vscans)
    links = pp.voice_face_links(reg, {sha: (scans[sha], vscans[sha]) for sha in scans if sha in vscans})
    for r in rows:
        p = next(q for q in reg["people"] if q["id"] == r["id"])
        r["speaking_seconds"] = round(spoken.get(r["id"], 0.0), 1)
        r["has_face"], r["has_voice"] = bool(p["exemplars"]), bool(p.get("voices"))
        pair = next((k for k in links if r["id"] in (k["voice"], k["face"])), None)
        r["likely_same_as"] = None if pair is None else {"person": pair["face"] if pair["voice"] == r["id"] else pair["voice"], "confidence": pair["confidence"],
                                                         "seen_together_seconds": pair["seconds"]}
    shown = [r for r in rows if include_minor or r["shots"] >= 2 or r["seconds_on_screen"] >= MINOR_SECONDS or r["speaking_seconds"] >= MINOR_SECONDS or r["name"]]
    return ok(f"{len(shown)} people" + (f" ({len(rows) - len(shown)} brief appearances hidden)" if len(rows) > len(shown) else "") + ".", changed=False,
              people=shown[:MAX_LISTED], hidden=len(rows) - len(shown), unnamed=sum(1 for r in shown if not r["name"]),
              note="names come only from the user; an id like P3 is just a label. likely_same_as pairs a voice with a face that is on screen "
                   "while it speaks: if the user agrees they are one person, merge_people_tool joins them")


def _file_track(fi, seconds: float, track_id: str = ""):
    """The face track at a time in a file: (scan, tracks near that time). Raises ToolError saying what is missing."""
    scan = pp.load_scan(fi.sha)
    if not scan:
        raise ToolError(f"{fi.name!r} has no people scan yet: run scan_people_tool")
    reg = pp.load_registry()
    tracks = pp.resolve_tracks(fi.sha, scan, reg)
    if track_id:
        tracks = [t for t in tracks if t["id"] == track_id]
    else:
        tracks = [t for t in tracks if t["start"] - 1.0 <= seconds <= t["end"] + 1.0]
    return reg, tracks


def _describe_track(reg, t) -> Dict[str, Any]:
    mid = t["samples"][len(t["samples"]) // 2]
    return {"track_id": t["id"], "at": [t["start"], t["end"]], "person": t["person"], "name": _name_of(reg, t["person"]), "confidence": t["confidence"], "status": t["status"],
            "possible": t["candidate"], "possible_name": _name_of(reg, t["candidate"]), "box": mid["box"], "face_px": t["best_px"]}


@editor_tool(
    "who_is_this_tool",
    covers=("index.people",),
    label="Who is on screen",
    schema=obj({
        "file_id": string("Project file id."),
        "seconds": number("A time in the file's own seconds.", None, minimum=0),
    }, required=["file_id", "seconds"]),
    read_only=True,
)
def who_is_this(file_id, seconds):
    """Who is on screen, and who is speaking, at a moment of a file: each face found near that time and the voice heard then, with the
    person it matches, the name the user gave, how sure the match is, and 'unsure' with the possible person when it is not sure enough
    to say. Boxes are fractions of the frame.
    """
    _need_on()
    files, _m = _fi_by_file([str(file_id)])
    if not files:
        raise ToolError(f"no indexed video with id {file_id!r}")
    fi = files[0]
    vscan = pp.load_voice_scan(fi.sha)
    if not pp.load_scan(fi.sha) and not vscan:
        raise ToolError(f"{fi.name!r} has no people scan yet: run scan_people_tool")
    reg = pp.load_registry()
    tracks = _file_track(fi, float(seconds))[1] if pp.load_scan(fi.sha) else []
    rows = [_describe_track(reg, t) for t in tracks]
    voice = None
    if vscan:
        v = pp.voice_at(fi.sha, vscan, reg, float(seconds))
        if v:
            voice = {"speaker": v["speaker"], "person": v["person"], "name": _name_of(reg, v["person"]), "confidence": v["confidence"], "status": v["status"],
                     "possible": v["candidate"], "possible_name": _name_of(reg, v["candidate"]), "speaking": [v["start"], v["end"]]}
    if not rows and not voice:
        return ok("No face or voice was found near that time.", changed=False, faces=[], voice=None)
    return ok(f"{len(rows)} face(s)" + (" and a voice" if voice else "") + f" near {float(seconds):.1f} s.", changed=False, faces=rows, voice=voice)


@editor_tool(
    "name_person_tool",
    covers=("index.people",),
    label="Name a person",
    schema=obj({
        "name": string("The name the user gave this person."),
        "person_id": string("The person to name (list_people_tool), like P3.", ""),
        "file_id": string("Or point at a face: the project file...", ""),
        "seconds": number("...and a time it is on screen.", None, minimum=0),
        "track_id": string("When several faces are on screen at that time: which one (from who_is_this_tool).", ""),
        "by": {"type": "string", "enum": ["face", "voice"], "description": "When pointing at a time: name the face on screen (default) or the voice speaking then.", "default": "face"},
    }, required=["name"]),
)
def name_person(name, person_id="", file_id="", seconds=None, track_id="", by="face"):
    """Give a person the name the user told you, by id or by pointing at a face at a time in a file. If that name already belongs to
    someone, the face is added to them (recognised as the same person) instead of making a second person. Only name someone because
    the user said who they are: never infer a name.
    """
    _need_on()
    clean = " ".join(str(name or "").split())
    if not clean:
        raise ToolError("a name is needed")
    reg = pp.load_registry()
    if person_id:
        try:
            out = pp.name_person(str(person_id), clean)
        except KeyError:
            raise ToolError(f"no person {person_id!r} (list_people_tool lists them)")
        return ok(f"{out['id']} is now called {out['name']}.", changed=True, person=out, **({"warning": f"{out['same_name_as']} has the same name: merge_people_tool if they are one person"} if out["same_name_as"] else {}))
    if not file_id or seconds is None:
        raise ToolError("say who: a person_id, or a file_id and the time they are on screen")
    files, _m = _fi_by_file([str(file_id)])
    if not files:
        raise ToolError(f"no indexed video with id {file_id!r}")
    fi = files[0]
    if by == "voice":
        return _name_voice(fi, float(seconds), clean)
    reg, tracks = _file_track(fi, float(seconds), track_id)
    if not tracks:
        raise ToolError("no face was found there: who_is_this_tool shows where faces are")
    if len(tracks) > 1:
        raise ToolError("more than one face is on screen then: pass track_id from " + ", ".join(f"{t['id']} (box {t['samples'][0]['box']})" for t in tracks[:6]))
    t = tracks[0]
    existing = pp.find_people(reg, clean)
    existing = [p for p in existing if (p.get("name") or "").lower() == clean.lower()]
    if existing:
        res = pp.pin_track(fi.sha, t["id"], existing[0]["id"])
        return ok(f"That is {clean} ({existing[0]['id']}), already known; this face was added to them.", changed=True, person=existing[0]["id"], track=res["track"])
    if t["person"] is None:
        pid = pp.pin_track(fi.sha, t["id"], None)["person"]
    else:
        pid = t["person"]
    out = pp.name_person(pid, clean)
    return ok(f"{out['id']} is now called {out['name']}.", changed=True, person=out)


def _name_voice(fi, seconds: float, clean: str) -> str:
    vscan = pp.load_voice_scan(fi.sha)
    if not vscan:
        raise ToolError(f"{fi.name!r} has no voice scan yet: run scan_people_tool (it needs the voice model and a transcript)")
    reg = pp.load_registry()
    v = pp.voice_at(fi.sha, vscan, reg, seconds)
    if v is None:
        raise ToolError("no one is speaking then: who_is_this_tool shows when voices are heard")
    existing = [p for p in pp.find_people(reg, clean) if (p.get("name") or "").lower() == clean.lower()]
    if existing:
        res = pp.pin_speaker(fi.sha, v["speaker"], existing[0]["id"])
        return ok(f"That voice is {clean} ({existing[0]['id']}), already known; it was added to them.", changed=True, person=existing[0]["id"], speaker=res["speaker"])
    pid = v["person"] or pp.pin_speaker(fi.sha, v["speaker"], None)["person"]
    out = pp.name_person(pid, clean)
    return ok(f"{out['id']} (a voice) is now called {out['name']}.", changed=True, person=out)


@editor_tool(
    "merge_people_tool",
    covers=("index.people",),
    label="Merge two people",
    schema=obj({
        "keep": string("The person to keep, like P1."),
        "merge": string("The person who is the same one, to fold into it, like P4."),
    }, required=["keep", "merge"]),
)
def merge_people(keep, merge):
    """Say two people are the same person: the second is folded into the first (their faces, and their name if the first has none).
    Use it when list_people_tool shows one person twice (a different look, angle or light).
    """
    _need_on()
    try:
        out = pp.merge_people(str(keep), str(merge))
    except KeyError as exc:
        raise ToolError(f"no person {exc.args[0]!r} (list_people_tool lists them)")
    except ValueError as exc:
        raise ToolError(str(exc))
    return ok(f"{out['merged']} was folded into {out['kept']}.", changed=True, **out)


@editor_tool(
    "fix_person_track_tool",
    covers=("index.people",),
    label="Correct who a face is",
    schema=obj({
        "file_id": string("Project file id."),
        "person": string("Who it really is: a person id like P2, or 'new' for someone else."),
        "track_id": string("The face (from who_is_this_tool).", ""),
        "speaker_id": string("Or the voice (the speaker id from who_is_this_tool, like S1).", ""),
    }, required=["file_id", "person"]),
)
def fix_person_track(file_id, person, track_id="", speaker_id=""):
    """Correct one face or voice that was matched to the wrong person (or that is someone new): the correction is kept and it is added
    to the right person. Use it when who_is_this_tool names the wrong person.
    """
    _need_on()
    if bool(track_id) == bool(speaker_id):
        raise ToolError("say which one: a track_id (a face) or a speaker_id (a voice)")
    files, _m = project_indexes([str(file_id)])
    if not files:
        raise ToolError(f"no indexed file with id {file_id!r}")
    target = None if str(person).lower() == "new" else str(person)
    try:
        if track_id:
            out = pp.pin_track(files[0].sha, str(track_id), target)
        else:
            out = pp.pin_speaker(files[0].sha, str(speaker_id), target)
    except KeyError as exc:
        raise ToolError(f"no such face, voice or person: {exc.args[0]}")
    return ok(f"That {'face' if track_id else 'voice'} is now {out['person']}.", changed=True, **out)


# ============================ locate a person ============================
@editor_tool(
    "locate_person_tool",
    covers=("index.people_locate",),
    label="Locate a person",
    schema=obj({
        "person": string("A name the user gave or an id like P3."),
        "for_action": {"type": "string", "enum": ["", "blur_object", "highlight_object", "mask_object"],
                       "description": "To blur, highlight or cut the person out: each of the first 5 results comes with a ready handoff for enhance_file_with_comfyui_tool.", "default": ""},
        "file_ids": array({"type": "string"}, "Only these project files (default: all scanned)."),
        "limit": integer("Most results to return.", 20, minimum=1, maximum=100),
    }, required=["person"]),
    read_only=True,
)
def locate_person(person, for_action="", file_ids=None, limit=20):
    """Where a person is on screen, with the face box and a rough whole-person box (estimated from the face, to be checked on a frame),
    and with for_action a ready handoff to the masking tools: 'change this person's clothes', 'blur her', 'replace him'. A possible
    (unsure) match is not included. Face boxes are exact enough to choose a frame; the body box is only a seed.
    """
    _need_on()
    reg = pp.load_registry()
    found = pp.find_people(reg, str(person))
    if not found:
        raise ToolError(f"no person {person!r} (list_people_tool lists them)")
    ids = {p["id"] for p in found}
    files, _m = _fi_by_file(file_ids or None)
    from classes.editor_tools.media_index_tools import _all_files
    projects = {str(f.id): f for f in _all_files()}
    hits: List[Dict[str, Any]] = []
    for fi in files:
        scan = pp.load_scan(fi.sha)
        if not scan:
            continue
        for tr in pp.resolve_tracks(fi.sha, scan, reg):
            if tr["person"] not in ids:
                continue
            mid = tr["samples"][len(tr["samples"]) // 2]
            body = handoff.body_box_from_face(mid["box"])
            hit = {"file_id": fi.file_id, "name": fi.name, "shot_id": tr["shot"], "t": mid["t"], "start": tr["start"], "end": tr["end"], "person": tr["person"],
                   "confidence": tr["confidence"], "face_box": mid["box"], "body_box": body, "box_precision": "face exact enough to pick a frame; body estimated from the face"}
            hits.append(hit)
    hits.sort(key=lambda h: (h["file_id"], h["t"]))
    hits = hits[:int(limit)]
    if for_action:
        for hit in hits[:handoff.MAX_HANDOFFS]:
            fi = next(f for f in files if f.file_id == hit["file_id"])
            video = ((fi.technical or {}).get("video") or {})
            data = projects[hit["file_id"]].data if hit["file_id"] in projects else {}
            width, height = int(data.get("width") or video.get("width") or 0), int(data.get("height") or video.get("height") or 0)
            frames = int(float(data["video_length"])) if data.get("video_length") else None
            seed = {"file_id": hit["file_id"], "label": _name_of(reg, hit["person"]) or "person", "t": hit["t"], "box": hit["body_box"]}
            hit["handoff"] = handoff.mask_handoff(seed, width, height, float(video.get("fps") or 0.0), frames, for_action) or {"unavailable": "no usable box or frame size"}
    who = found[0].get("name") or found[0]["id"]
    return ok(f"{who} is on screen in {len(hits)} place(s)." if hits else f"{who} was not found on screen in the scanned footage.", changed=False, hits=hits,
              note="only footage with a people scan is searched (scan_people_tool); a possible match is left out, so a missing result is not proof of absence")


# ============================ erase ============================
@editor_tool(
    "erase_people_data_tool",
    covers=("index.people",),
    label="Delete all people data",
    schema=obj({
        "confirm": boolean("Must be true: this removes every face scan and every name, and cannot be undone."),
        "include_models": boolean("Also remove the downloaded face models.", False),
    }, required=["confirm"]),
)
def erase_people_data(confirm, include_models=False):
    """Delete everything stored about people: every face scan, every person and name. Cannot be undone. Only when the user asks, with
    confirm=true. Works whether or not the preference is on.
    """
    if confirm is not True:
        raise ToolError("this removes every face scan and every name for good: ask the user, then pass confirm=true")
    out = pp.delete_all(include_models=bool(include_models))
    return ok(f"Deleted {out['scans']} scan(s) and the people list" + (f" and {out['models']} model file(s)" if out["models"] else "") + ".", changed=True, removed=out)


voice_diarize.install()          # speaker labels by voiceprint when people identity is on and ready; the baseline otherwise
