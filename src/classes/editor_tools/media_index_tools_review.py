"""Edit review and project survey tools: what a finished edit still needs, and what footage there is to cut.

``review_edit_tool`` measures the *timeline* (the result), not the sources: live colour after grading, the
rendered mix's loudness, where cuts fall against the music's beats. The rules live in ``media_index.review``;
this module only describes the timeline to them and reports back. Nothing here changes the project.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from classes.editor_tools._base import ToolError, boolean, enum, integer, number, obj, ok, string
from classes.editor_tools._registry import REGISTRY, editor_tool
from classes.editor_tools.media_files import _display_name, resolve_files
from classes.editor_tools.media_index_tools import _index_for, project_indexes
from classes.logger import log
from classes.media_index import audio_cloud, library, musicfit, quality, review as R, schema as S, trip
from classes.media_index.store import default_shelf, sha_of

MEASURE_BUDGET_SECONDS = 40.0
MAX_MIX_SECONDS = 1800.0
PLACE_DECIMALS = 1           # about 11 km: enough to name a city, not a street, when places reach the model


# ============================ describing the timeline ============================
def _file_kind(file_data: Optional[Dict[str, Any]], clip_data: Dict[str, Any]) -> str:
    kind = str((file_data or {}).get("media_type") or "video")
    path = str(((clip_data.get("reader") or {}).get("path")) or (file_data or {}).get("path") or "").lower()
    if kind == "title" or path.endswith(".svg"):
        return "title"
    return kind if kind in ("video", "audio", "image") else "video"


def build_timeline() -> Tuple[List[R.TimelineClip], R.ProjectInfo, Dict[str, Any]]:
    """The timeline as plain data for the audit, plus the live clip objects by id (for colour measurement)."""
    from classes import audio_mix as am
    from classes.query import Clip, File
    from classes import tool_handlers as th

    app = th._get_app()
    entries, _layers, _fps = th._collect_timeline_audio(app, None)
    audio_by_id = {e["id"]: e for e in entries}
    get_file: Any = File.get                     # its signature is not typed for keyword lookups
    file_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    clips: List[R.TimelineClip] = []
    objs: Dict[str, Any] = {}
    for clip_obj in Clip.filter():
        data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        fid = str(data.get("file_id") or "")
        if fid not in file_cache:
            f = get_file(id=fid) if fid else None
            file_cache[fid] = f.data if f is not None and isinstance(f.data, dict) else None
        fdata = file_cache[fid]
        tl_start, tl_end = am.clip_timeline_extent(data)
        src_in, src_out = float(data.get("start") or 0.0), float(data.get("end") or 0.0)
        tl_len = tl_end - tl_start
        speed = (src_out - src_in) / tl_len if tl_len > 0.01 and src_out > src_in else 1.0
        entry = audio_by_id.get(str(clip_obj.id))
        level = entry["level"] if entry else None
        gain_fn: Optional[Callable[[float], float]] = None
        if entry is not None and level is None:          # automated volume (ducking, fades): read the curve where it matters
            points, fps_info = am.curve_points(data), entry["fps"]

            def curve_gain(t: float, _data=data, _points=points, _fps=fps_info) -> float:
                return am.gain_to_db(am.evaluate_volume_curve(_points, am.timeline_to_source_frame(t, _data, _fps)))

            gain_fn = curve_gain
        clips.append(R.TimelineClip(
            id=str(clip_obj.id), name=str(data.get("title") or (fdata or {}).get("name") or os.path.basename(str((fdata or {}).get("path") or "")) or "clip"),
            file_id=fid, sha=sha_of((fdata or {}).get("fingerprint")), layer=int(data.get("layer") or 0), kind=_file_kind(fdata, data),
            start=float(tl_start), end=float(tl_end), src_in=src_in, src_out=src_out, speed=float(speed),
            role=entry["role"] if entry else None, gain_db=(am.gain_to_db(level) if level is not None else None),
            has_audio=entry is not None, effects=[str(e.get("class_name") or "") for e in (data.get("effects") or []) if isinstance(e, dict)],
            gain_fn=gain_fn, windows=[(float(a), float(b)) for a, b in (entry["windows"] if entry else [])]))
        objs[str(clip_obj.id)] = clip_obj
    project = th._get_app().project
    width, height = int(project.get("width") or 1920), int(project.get("height") or 1080)
    return clips, R.ProjectInfo(duration=max((c.end for c in clips), default=0.0), width=width, height=height), objs


def caption_intervals(objs: Dict[str, Any]) -> List[Tuple[float, float]]:
    """Timeline [start, end) of every caption cue on every clip that carries a Caption effect."""
    from classes import caption_cues
    from classes.editor_tools.titles_text_captions import caption_effects, caption_to_timeline

    out: List[Tuple[float, float]] = []
    for clip_obj in objs.values():
        data = clip_obj.data if isinstance(clip_obj.data, dict) else {}
        for effect in caption_effects(data):
            for cue in caption_cues.parse_caption_text(str(effect.get("caption_text") or "")):
                out.append((caption_to_timeline(data, cue.start), caption_to_timeline(data, cue.end)))
    return out


def measure_looks(clips: List[R.TimelineClip], objs: Dict[str, Any], *, max_clips: int, budget: float = MEASURE_BUDGET_SECONDS,
                  profile_fn: Optional[Any] = None) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """The look of each picture clip *as it plays* (effects included), a few frames each, within a time budget.

    Returns (profiles by clip id, how many clips were considered). More clips than *max_clips* are sampled evenly,
    always including the first and the last.
    """
    if profile_fn is None:
        from classes import tool_handlers as th
        project = th._get_app().project
        fps_info = project.get("fps") or {"num": 30, "den": 1}
        fps = float(fps_info.get("num", 30)) / float(fps_info.get("den", 1) or 1)

        def default_profile(data: Dict[str, Any]) -> Dict[str, Any]:
            return th._profile_clip_dense(data, fps, with_jpegs=False, max_samples=3)

        profile_fn = default_profile
    seen: List[R.TimelineClip] = []
    for s in R.visible_shots(clips):
        if s["clip"] not in seen:
            seen.append(s["clip"])
    if len(seen) > max_clips:
        step = (len(seen) - 1) / float(max_clips - 1) if max_clips > 1 else 0
        seen = [seen[round(i * step)] for i in range(max_clips)]
    looks: Dict[str, Dict[str, Any]] = {}
    deadline = time.monotonic() + budget
    for c in seen:
        if time.monotonic() > deadline:
            break
        clip_obj = objs.get(c.id)
        if clip_obj is None:
            continue
        try:
            profile = (profile_fn(dict(clip_obj.data)) or {}).get("profile") or {}
            if profile.get("present"):
                looks[c.id] = profile
        except Exception as exc:  # noqa: BLE001 - one unreadable clip must not sink the audit
            log.debug("look of %s not measured: %s", c.id, exc)
    return looks, len(seen)


def render_timeline_mix(start: float, end: float) -> Tuple[Optional[str], str]:
    """Render [start, end) of the timeline's audio to a temporary file. Returns (path, "") or (None, why)."""
    folder = tempfile.mkdtemp(prefix="zenvi_mix_")
    path = os.path.join(folder, "mix.mp3")
    from classes import tool_handlers as th

    saved = None
    try:
        settings = th._get_app().get_settings()
        saved = settings.getDefaultPath(settings.actionType.EXPORT)
    except Exception:
        settings = None
    try:
        out = REGISTRY["export_video_tool"].func(preset="MP3", export_type="audio_only", start=float(start), end=float(end),
                                                 output_path=path, overwrite=True)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:200]
    finally:
        if settings is not None and saved:
            try:
                settings.setDefaultPath(settings.actionType.EXPORT, saved)      # an analysis render must not move the user's export folder
            except Exception:
                pass
    if str(out).startswith("Error") or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return None, str(out)[:300]
    return path, ""


def _brief(form: str, target_seconds: Optional[float], vibe: str, wants_music: str, wants_captions: str, wants_grade: str) -> R.Brief:
    def yn(v: str) -> Optional[bool]:
        return {"yes": True, "no": False}.get(str(v or "").lower())
    return R.Brief(form=str(form or ""), target_seconds=float(target_seconds) if target_seconds else None, vibe=str(vibe or ""),
                   wants_music=yn(wants_music), wants_captions=yn(wants_captions), wants_grade=yn(wants_grade))


# ============================ review_edit_tool ============================
@editor_tool(
    "review_edit_tool",
    covers=("index.review",),
    label="Review edit",
    schema=obj({
        "form": string("What this edit is: 'YouTube vlog', 'TikTok', 'Instagram Reel', 'short film', 'montage', 'trailer', 'podcast', "
                       "'tutorial'... Sets what is expected (vertical, captions, music, loudness target). '' = judge only what can be "
                       "measured and report expectations as notes.", ""),
        "target_seconds": number("The length it should be, in seconds (0 = no target).", None, minimum=0),
        "vibe": string("The look or feel asked for ('warm golden hour', 'moody noir', 'bright and clean'). A vibe makes an ungraded "
                       "edit a problem.", ""),
        "wants_music": enum(["", "yes", "no"], "Override the form's default: 'yes' = music is wanted, 'no' = it is not.", ""),
        "wants_captions": enum(["", "yes", "no"], "Override the form's default for burned-in captions.", ""),
        "wants_grade": enum(["", "yes", "no"], "Override: 'yes' = a colour grade is wanted even without a named vibe.", ""),
        "render_mix": boolean("Render the timeline's audio to measure its true loudness and peaks (a few seconds; refused while "
                              "another export runs).", True),
        "measure_colour": boolean("Measure how each clip actually looks now (after any grade), a few frames each.", True),
        "max_clips_measured": integer("Most clips whose colour is measured (an even sample when there are more).", 24, minimum=2, maximum=80),
    }),
    read_only=True,
)
def review_edit(form="", target_seconds=None, vibe="", wants_music="", wants_captions="", wants_grade="", render_mix=True,
                measure_colour=True, max_clips_measured=24):
    """Audit the edit on the timeline right now and say what still needs doing, with numbers.

    Picture: do the clips match in brightness and warmth after grading, any exposure or HDR problems, is a grade
    applied. Story: length against the target, pacing, how strong the opening shot is, blurry or shaky shots used,
    the same moment used twice. Sound: music present, how far the voice sits above the music as placed, jumps in
    voice level, dead air, true loudness and peaks against the platform target, whether cuts land on the beat.
    Text and delivery: captions over the speech, an opening title, vertical format for social forms.
    Each finding is ok, needs, info or unknown (not measured: index the footage or render the mix) with evidence
    and tools that could fix it. Run it after assembling and before exporting, fix what 'needs' work, run it again.
    It changes nothing and does not decide how to fix a problem: that is yours.
    """
    clips, project, objs = build_timeline()
    if not clips:
        raise ToolError("the timeline is empty; assemble the edit first")
    brief = _brief(form, target_seconds, vibe, wants_music, wants_captions, wants_grade)
    shelf = default_shelf()
    cache: Dict[str, Any] = {}

    def index_for(c: R.TimelineClip) -> Any:
        if c.sha not in cache:
            cache[c.sha] = library.get_file_index(shelf, c.sha, file_id=c.file_id, name=c.name) if c.sha else None
        return cache[c.sha]

    looks, measured_of = ({}, 0)
    if measure_colour:
        looks, measured_of = measure_looks(clips, objs, max_clips=int(max_clips_measured))
    mix = None
    mix_note = ""
    if render_mix and any(c.has_audio for c in clips):
        end = min(project.duration, MAX_MIX_SECONDS)
        path, why = render_timeline_mix(0.0, end)
        if path:
            from classes.media_index import audio as au
            mix = au.measure_loudness(path)
            try:
                os.remove(path)
                os.rmdir(os.path.dirname(path))
            except OSError:
                pass
        else:
            mix_note = why
    files = [f for f in (index_for(c) for c in clips if c.sha) if f is not None]
    result = R.review(clips, project, brief, index_for=index_for, looks=looks, mix=mix, captions=caption_intervals(objs),
                      take_groups=quality.take_groups(files))
    needs, unknown = result["needs"], result["unknown"]
    summary = (f"{len(needs)} thing(s) need work: {', '.join(needs[:6])}" if needs else "Nothing needs work") + \
              (f"; {len(unknown)} could not be measured: {', '.join(unknown[:5])}" if unknown else "") + "."
    return ok(summary, needs=needs, unknown=unknown, counts=result["counts"], layers=result["layers"],
              measured={"colour_clips": len(looks), "colour_clips_considered": measured_of, "mix_rendered": mix is not None,
                        "mix_note": mix_note or None, "duration": round(project.duration, 2), "clips": len(clips)},
              how_to_read="Findings marked measured come from the file or the rendered mix; the model's opinions (interest, "
                          "hook) are labelled inferred in get_segment_dossier_tool and never override a measured defect.")


# ============================ get_project_overview_tool ============================
def _bucket(value: Optional[float], cuts: Tuple[float, float], names: Tuple[str, str, str]) -> str:
    if value is None:
        return "unknown"
    return names[0] if value < cuts[0] else (names[1] if value < cuts[1] else names[2])


def survey(files: List[Any], not_indexed: List[str], *, top: int = 8, precise_places: bool = False) -> Dict[str, Any]:
    """The project's footage at a glance (pure over loaded file indexes)."""
    firsts: Dict[str, Any] = {}
    for f in files:
        firsts.setdefault(f.sha, f)                  # the same footage imported twice: the first file stands for it, as in search
    unique = list(firsts.values())
    videos = [f for f in unique if f.media_type == "video"]
    audios = [f for f in unique if f.media_type == "audio"]
    images = [f for f in unique if f.media_type == "image"]

    moments = []
    for f in unique:
        for s in f.shots:
            q = s.get("quality") or {}
            if s.get("black") or (q.get("inferred") or {}).get("usable") is False:
                continue
            w = s.get("watch") or {}
            moments.append({"file_id": f.file_id, "name": f.name, "start": round(s["start"], 2), "end": round(s["end"], 2),
                            "highlight": q.get("highlight", 0.0), "why": (q.get("inferred") or {}).get("highlight_reason") or w.get("description", "")[:100],
                            "flags": q.get("flags", [])})
    moments.sort(key=lambda m: -float(m["highlight"] or 0.0))

    groups = quality.take_groups(unique)
    capture = [{"file_id": f.file_id, "captured_at": f.captured_at or None, "gps": f.gps, "duration": f.duration} for f in unique]
    outline = trip.trip_outline(capture)
    places = []
    from classes.media_index import gazetteer
    for p in outline["places"]:
        named = gazetteer.describe(p["lat"], p["lon"])
        row = {"id": p["id"], "files": len(p["file_ids"]), "minutes": p["minutes"]}
        if named:
            row["name"] = named["label"]                     # a name, not coordinates, is what reaches the model
        if precise_places or not named:
            row.update(lat=round(p["lat"], 5 if precise_places else PLACE_DECIMALS), lon=round(p["lon"], 5 if precise_places else PLACE_DECIMALS))
        places.append(row)
    names = {f.file_id: f.name for f in unique}

    clusters: Dict[str, List[str]] = {}
    for f in videos:
        prof = (f.look_file or {}).get("profile") or {}
        if prof.get("present"):
            key = f"{_bucket(prof.get('avg_luma'), (0.3, 0.6), ('dark', 'mid', 'bright'))} / {_bucket(prof.get('warm_cool'), (-0.04, 0.04), ('cool', 'neutral', 'warm'))}"
            clusters.setdefault(key, []).append(f.file_id)
    music = []
    for f in audios:
        m = (f.audio or {}).get("music") or {}
        tempo = (f.audio or {}).get("tempo") or {}
        music.append({"file_id": f.file_id, "name": f.name, "seconds": round(f.duration, 1), "bpm": tempo.get("bpm"),
                      "energy_arc": m.get("arc", [])[:12], "sections": [(x["label"], x["start"]) for x in m.get("sections", [])][:8],
                      "described": bool(f.music_desc)})
    return {
        "totals": {"files": len(unique), "videos": len(videos), "audio": len(audios), "images": len(images),
                   "video_minutes": round(sum(f.duration for f in videos) / 60.0, 1), "not_indexed": len(not_indexed),
                   "usable_minutes": round(sum((s["end"] - s["start"]) for f in videos for s in f.shots
                                               if not s.get("black") and (s.get("quality") or {}).get("flags", []) is not None
                                               and not set((s.get("quality") or {}).get("flags", [])) & {"blurry", "black"}) / 60.0, 1)},
        "trip": {"days": [{"day": d["day"], "date": d["date"], "minutes": d["minutes"], "clips": len(d["file_ids"]), "places": d["place_ids"],
                           "first": names.get(d["file_ids"][0], "")} for d in outline["days"]],
                 "places": places, "undated_clips": len(outline["undated"]),
                 "place_precision": "exact" if precise_places else "named places carry a city name only; others about 11 km (city level)",
                 "place_names": gazetteer.ATTRIBUTION},
        "top_moments": moments[:top],
        "take_groups": {"groups": len(groups), "examples": [{"best": g["best"], "also": [m for m in g["members"] if m != g["best"]][:3]} for g in groups[:4]]},
        "speech": [{"file_id": f.file_id, "name": f.name, "sentences": len(f.sentences)} for f in unique if f.sentences][:12],
        "music": music[:10],
        "looks": {k: {"files": len(v), "ids": v[:6]} for k, v in clusters.items()},
        "orientation": {o: sum(1 for f in videos if f.orientation == o) for o in ("landscape", "portrait", "square") if any(f.orientation == o for f in videos)},
        "not_indexed": not_indexed[:15],
    }


@editor_tool(
    "get_project_overview_tool",
    covers=("index.overview",),
    label="Survey footage",
    schema=obj({
        "top_moments": integer("How many of the best moments to list.", 8, minimum=0, maximum=30),
        "precise_places": boolean("Report place coordinates to street level (default: about 11 km, enough to name a city). "
                                  "Coordinates reach the model, so keep the default unless exact spots matter.", False),
    }),
    read_only=True,
)
def get_project_overview(top_moments=8, precise_places=False):
    """Everything about the project's footage in one call: how much there is and of what kind, when and where it was
    shot (days and places, from the clips' own tags), the best moments by highlight score, near-duplicate takes, which
    clips have speech, the music files with their energy and sections, the look clusters (dark/cool, bright/warm...),
    orientation mix and what is not indexed yet. Start here before planning a vlog, montage or film: it says what you
    have to cut with, then use search_footage_tool and get_segment_dossier_tool for detail.
    """
    files, missing = project_indexes()
    if not files and not missing:
        raise ToolError("the project has no media files yet")
    return ok(f"{len(files)} indexed file(s)" + (f", {len(missing)} not indexed yet" if missing else ""),
              **survey(files, missing, top=int(top_moments), precise_places=bool(precise_places)))


# ============================ analyze_music_tool ============================
def music_profile_of(fi: Any) -> Dict[str, Any]:
    return musicfit.profile_from_audio(fi.audio or {}, fi.duration)


def music_fit(profile, *, bpm_min=None, bpm_max=None, seconds=None, energy=""):
    return musicfit.music_fit(profile, bpm_min=bpm_min, bpm_max=bpm_max, seconds=seconds, energy=energy)


@editor_tool(
    "analyze_music_tool",
    covers=("index.music",),
    label="Analyze music",
    schema=obj({
        "file_id": string("Project file id of the music.", ""),
        "file_query": string("Describe the file instead of an id (its name).", ""),
        "describe": boolean("Also get genre, mood, instruments, vocals and what it suits, per section, from the cloud (once per "
                            "file, then kept; needs sign-in).", False),
        "bpm_min": number("For a fit check: slowest tempo you want.", None, minimum=0),
        "bpm_max": number("For a fit check: fastest tempo you want.", None, minimum=0),
        "seconds_needed": number("For a fit check: how long the music must play.", None, minimum=0),
        "energy": enum(["", "low", "medium", "high", "building"], "For a fit check: the energy you want.", ""),
    }),
    read_only=True,
)
def analyze_music(file_id="", file_query="", describe=False, bpm_min=None, bpm_max=None, seconds_needed=None, energy=""):
    """Understand a music file as an editor would: tempo and beats, loudness, the energy curve, its sections (intro,
    build, peak, break, outro), bars and phrase points (good places to cut or end), and, on request, what it is: genre,
    mood, instruments, vocals, what moments it suits. Pass bpm_min/bpm_max, seconds_needed or energy to get a fit verdict
    for your edit, with the measured reason for each line. Phrase points are where to loop or end the track so it does
    not stop mid-phrase; cutting your picture on downbeats makes it feel tight.
    """
    f = resolve_files([file_id] if file_id else None, file_query)[0]
    fi = _index_for(f)
    if fi is None or not fi.layers.get(S.LAYER_AUDIO):
        raise ToolError(f"{_display_name(f.data)!r} has no audio analysis yet; index_status_tool shows progress")
    profile = music_profile_of(fi)
    out: Dict[str, Any] = {"file_id": str(f.id), "profile": profile}
    if describe or fi.music_desc:
        desc = fi.music_desc
        if describe and not desc:
            from classes.api_client import get_backend_client
            from classes.media_index.probe import probe_media
            shelf = default_shelf()
            res = audio_cloud.describe_music(get_backend_client(), fi.path, probe_media(fi.path), fi.sha, shelf, file_id=str(f.id),
                                             sections=profile["sections"])
            if res.get("auth"):
                raise ToolError("sign in to Zenvi to describe music (the local analysis above works signed out)")
            if res.get("unsupported"):
                raise ToolError("this backend has no media index v2 audio description yet")
            if res.get("error"):
                raise ToolError("could not describe the music: " + str(res["error"]))
            desc = res["windows"]
            library.clear_cache()
        out["description"] = desc
        out["description_kind"] = "inferred (the model's reading; the measured profile is authoritative)"
    if bpm_min or bpm_max or seconds_needed or energy:
        out["fit"] = music_fit(profile, bpm_min=bpm_min, bpm_max=bpm_max, seconds=seconds_needed, energy=energy)
    bits = [f"{profile['bpm']:.0f} BPM" if profile["bpm"] else "no steady tempo", f"{profile['seconds']:.0f} s"]
    if profile["sections"]:
        bits.append("sections: " + ", ".join(s["label"] for s in profile["sections"]))
    return ok(f"{_display_name(f.data)}: " + "; ".join(bits), **out)


# ============================ listen_tool ============================
@editor_tool(
    "listen_tool",
    covers=("index.listen",),
    label="Listen",
    schema=obj({
        "start": number("Start second of the timeline to listen to (default: the beginning).", None, minimum=0),
        "end": number("End second (default: the end, at most 30 minutes).", None, minimum=0),
        "file_id": string("Listen to one project file's audio instead of the timeline mix.", ""),
        "form": string("What this is ('YouTube vlog', 'TikTok'...), so the listener knows what to expect.", ""),
        "vibe": string("The intended feel ('warm and upbeat').", ""),
    }),
    read_only=True,
)
def listen(start=None, end=None, file_id="", form="", vibe=""):
    """Get a second opinion on how the audio sounds, from a model that listens to the rendered mix (about 6 s and a
    few cents of audio per minute). It reliably catches gross faults: speech buried under music, distortion, noise,
    dead air that looks unintended, jarring jumps at cuts. It is NOT a judge of fine loudness balance: it can call a
    well-balanced mix too loud, so trust review_edit_tool's measured voice-over-music margin and loudness for levels.
    Findings come with timestamps and are the model's reading (inferred). Needs sign-in; run review_edit_tool first.
    """
    from classes.api_client import get_backend_client

    speech_ranges: Optional[List[List[float]]] = None
    cleanup: Optional[str] = None
    if file_id:
        f = resolve_files([file_id], "")[0]
        from classes.path_utils import absolute_media_path
        src = absolute_media_path(f.data.get("path")) or str(f.data.get("path") or "")
        if not os.path.isfile(src):
            raise ToolError(f"{_display_name(f.data)!r} is missing on disk")
        lo, hi = float(start or 0.0), float(end) if end is not None else None
        duration = float(f.data.get("duration") or 0.0)
        hi = min(hi if hi is not None else duration, duration or 1e9, MAX_MIX_SECONDS)
        folder = tempfile.mkdtemp(prefix="zenvi_listen_")
        proxy = os.path.join(folder, "audio.aac")
        ok_, err = audio_cloud.make_audio_proxy(src, proxy, lo, hi)
        if not ok_:
            raise ToolError("could not prepare the audio: " + err)
        length, label, cleanup = hi - lo, _display_name(f.data), folder
    else:
        clips, project, _objs = build_timeline()
        if not any(c.has_audio for c in clips):
            raise ToolError("the timeline has no audio to listen to")
        lo = float(start or 0.0)
        hi = min(float(end) if end is not None else project.duration, project.duration, MAX_MIX_SECONDS)
        if hi - lo < audio_cloud.MIN_WINDOW:
            raise ToolError("that range is too short to listen to")
        path, why = render_timeline_mix(lo, hi)
        if not path:
            raise ToolError("could not render the mix: " + why)
        folder = os.path.dirname(path)
        proxy = os.path.join(folder, "audio.aac")
        ok_, err = audio_cloud.make_audio_proxy(path, proxy)
        if not ok_:
            raise ToolError("could not prepare the audio: " + err)
        speech_ranges = [[round(max(0.0, c.start - lo), 2), round(min(hi, c.end) - lo, 2)] for c in clips
                         if c.role == "speech" and c.end > lo and c.start < hi]
        length, label, cleanup = hi - lo, "the timeline mix", folder
    try:
        context: Dict[str, Any] = {"form": form, "vibe": vibe}
        if speech_ranges is not None:
            context["speech_ranges"] = speech_ranges
        result = audio_cloud.listen(get_backend_client(), proxy, length, context)
    finally:
        if cleanup:
            for name in os.listdir(cleanup):
                try:
                    os.remove(os.path.join(cleanup, name))
                except OSError:
                    pass
            try:
                os.rmdir(cleanup)
            except OSError:
                pass
    if result.get("auth"):
        raise ToolError("sign in to Zenvi to use listen_tool")
    if result.get("unsupported"):
        raise ToolError("this backend has no media index v2 listening yet")
    if result.get("error"):
        raise ToolError("could not listen: " + str(result["error"]))
    findings = result.get("findings") or []
    for f_ in findings:                                  # times were relative to the rendered range
        f_["start_in_timeline"] = round(f_["start"] + (0.0 if file_id else lo), 2)
    bad = [f_ for f_ in findings if f_["kind"] != "good"]
    return ok(f"heard {len(bad)} problem(s) in {label} ({lo:.0f}-{lo + length:.0f} s): " + (result.get("summary") or ""),
              findings=findings, overall=result.get("overall"), listener_summary=result.get("summary"), kind="inferred",
              reliability="catches gross faults (buried speech, clipping, noise, dead air); does not judge fine loudness balance")
