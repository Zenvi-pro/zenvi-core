"""Media index tools: search the footage by what happens, what is said and how it looks.

These read the saved per-file index (``classes.media_index``): one index per file content, so the
same footage is searchable in every project. Nothing here mutates the project.
"""

from __future__ import annotations

from classes.editor_tools._base import ToolError, array, boolean, enum, get_app, integer, mapping, number, obj, ok, on_main, string
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.media_files import FILE_TARGET, _display_name, resolve_files
from classes.logger import log
from classes.media_index import library, schema as S, search as msearch
from classes.media_index.dossier import build_dossier
from classes.media_index.store import default_shelf, sha_of

QUERY_LIMIT_CHARS = 500
# What "fully indexed" means. Descriptions (the watch layer) are not part of it: they are made on demand.
LAYER_ORDER = (S.LAYER_STRUCTURE, S.LAYER_LOOK, S.LAYER_AUDIO, S.LAYER_SPEECH, S.LAYER_VECTORS)


def _all_files():
    from classes.query import File
    return list(File.filter())


def _index_for(file_obj, shelf=None):
    data = file_obj.data
    sha = sha_of(data.get("fingerprint"))
    if not sha:
        return None
    from classes.path_utils import absolute_media_path
    return library.get_file_index(shelf or default_shelf(), sha, file_id=str(file_obj.id), name=_display_name(data),
                                  path=absolute_media_path(data.get("path")) or str(data.get("path") or ""),
                                  media_type=str(data.get("media_type") or "video"))


def project_indexes(file_ids=None):
    """(indexes, not_indexed): the project's files that have a saved index, and the names of those that do not."""
    shelf = default_shelf()
    wanted = {str(x) for x in file_ids} if file_ids else None
    found, missing = [], []
    for f in _all_files():
        if wanted is not None and str(f.id) not in wanted:
            continue
        if str(f.data.get("media_type") or "video") not in ("video", "audio", "image"):
            continue
        fi = _index_for(f, shelf)
        has_index = fi is not None and (fi.shots or fi.rows or any(fi.layers.values()))      # a music file has only an audio analysis
        (found if has_index else missing).append(fi if has_index else _display_name(f.data))
    return found, missing


def embed_query(text: str, files):
    """The search phrase as a unit vector from each model the *files* were indexed with: {model: vector}. A model that
    cannot embed it leaves its files out; ToolError, saying what to do, only when no model can."""
    from classes.media_index import embedder as E
    out, failed = {}, None
    for model in sorted({fi.vector_model for fi in files if fi.vector_model}):
        try:
            out[model] = E.get_embedder(model).texts([text], query=True)[0]
        except E.EmbedError as exc:
            log.info("search: %s could not embed the query: %s", model, exc)
            failed = exc
    if out or failed is None:
        return out
    exc = failed
    if exc.flags.get("auth"):
        raise ToolError("sign in to Zenvi to search footage indexed with the cloud model (or set Preferences > Footage search "
                        "model to this computer); filters and a reference clip work signed out")
    if exc.flags.get("credits"):
        raise ToolError("out of credits for the cloud search model: add credits, or set Preferences > Footage search model "
                        "to this computer (free)")
    if exc.flags.get("rate_limited"):
        wait = exc.flags.get("retry_after")
        raise ToolError("too many searches this hour" + (f"; try again in about {int(wait) // 60 + 1} min" if wait else "")
                        + ", or search with filters or a reference clip")
    raise ToolError(f"could not embed the query: {exc}")


def _nonempty(d):
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _bound(text: str, end: bool):
    """A date or date-time as a UTC instant; a date used as an upper bound means the end of that day."""
    from datetime import datetime, timedelta, timezone
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        raise ToolError(f"{raw!r} is not a date (use 2024-05-01 or 2024-05-01T09:00)")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when + timedelta(days=1) if end and "T" not in raw and " " not in raw else when


def restrict_files(files, captured_after: str, captured_before: str, place_id: int):
    """Files shot inside a date window and/or at one place (clips with no capture time or position are left out)."""
    from datetime import datetime, timezone
    from classes.media_index import trip
    if not (captured_after or captured_before) and place_id < 0:
        return files
    lo, hi = _bound(captured_after, False), _bound(captured_before, True)
    keep = []
    for fi in files:
        if not fi.captured_at:
            continue
        try:
            when = datetime.fromisoformat(fi.captured_at)
        except ValueError:
            continue
        when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
        if (lo and when < lo) or (hi and when >= hi):
            continue
        keep.append(fi)
    if place_id >= 0:
        outline = trip.trip_outline([{"file_id": f.file_id, "captured_at": f.captured_at, "gps": f.gps, "duration": f.duration} for f in files])
        at_place = {fid for p in outline["places"] if p["id"] == place_id for fid in p["file_ids"]}
        keep = [fi for fi in keep if fi.file_id in at_place] if (captured_after or captured_before) else [fi for fi in files if fi.file_id in at_place]
    return keep


@editor_tool(
    "search_footage_tool",
    covers=("index.search",),
    label="Search footage",
    schema=obj({
        "query": string("What to find, in plain words: an action ('a dog jumps into a lake'), a line of speech "
                        "('where she says thank you'), a mood or a look ('moody night street'). '' = filters or a "
                        "reference only.", ""),
        "look_for": enum(["", "spoken", "on_screen"], "'spoken' = only what people say, 'on_screen' = only what is "
                         "seen, '' = both.", ""),
        "reference_file_id": string("Find footage that looks like this file (or the range of it below).", ""),
        "reference_start": number("Start second of the reference range in that file.", None, minimum=0),
        "reference_end": number("End second of the reference range in that file.", None, minimum=0),
        "match": enum(["picture", "look"], "With a reference: 'picture' = what it shows, 'look' = its colour and "
                      "tone (grade matching).", "picture"),
        "filters": mapping("Narrow the results by facts: camera (static|pan|tilt|zoom_in|zoom_out|handheld), "
                           "shot_type (wide|medium|close|...), mood, object_label, text_on_screen, speech (true = "
                           "someone talks, false = no talking), look (warm|cool|dark|bright|saturated|muted|"
                           "contrasty|flat), orientation (landscape|portrait|square), min_duration, max_duration "
                           "(seconds), exclude_black (default true), usable_only (true = leave out blurry, black and "
                           "model-judged unusable shots), min_highlight (0-1, how striking the moment is)."),
        "file_ids": array({"type": "string"}, "Only search these project files (default: every indexed file)."),
        "sort": enum(["relevance", "highlight", "chronological"], "'relevance' = best match first, 'highlight' = the most striking "
                     "moments first (for choosing what to use), 'chronological' = in the order it was shot.", "relevance"),
        "captured_after": string("Only footage shot on or after this date or time (2024-05-01 or 2024-05-01T09:00). Clips without a "
                                 "capture time are left out.", ""),
        "captured_before": string("Only footage shot on or before this date (a date means the end of that day).", ""),
        "place_id": integer("Only footage shot at this place (ids come from get_project_overview_tool's trip.places); -1 = anywhere.", -1,
                            minimum=-1),
        "place": string("Only footage shot in a place by name ('Lisbon', 'Tokyo', 'Portugal'); names come from get_project_overview_tool's trip.places.", ""),
        "person": string("Only shots this person is on screen in: a name the user gave (find_tools: list people) or an id like P3. Needs the people "
                         "preference and a people scan. A possible match is left out and counted in `unsure_shots`; shots where only their "
                         "voice is heard (a narrator over other footage) are left out and counted in `heard_only_shots`.", ""),
        "limit": integer("Most results to return.", 10, minimum=1, maximum=50),
        "cursor": integer("Where to continue: the `next` of the previous result.", 0, minimum=0),
    }),
    read_only=True,
)
def search_footage(query="", look_for="", reference_file_id="", reference_start=None, reference_end=None, match="picture",
                   filters=None, file_ids=None, sort="relevance", captured_after="", captured_before="", place_id=-1, place="", person="", limit=10, cursor=0):
    """Search every indexed file in the project and return the best moments, each with the file, the in and out
    seconds (snapped to the real cut, or to the spoken sentence), where in it the match peaks and why.

    It searches shot descriptions, what people say, and the pictures themselves, then ranks them together, so
    "the dog on the beach" and "where he says thank you" both work. Add filters for measured facts (camera
    move, shot type, colour, duration, talking or not). A reference file or range finds similar footage, by
    picture or by colour. Use the returned file_id/start/end directly with the clip tools. Files not yet
    indexed are listed in `not_indexed`; check progress with find_tools: index status.
    """
    query = str(query or "").strip()[:QUERY_LIMIT_CHARS]
    files, missing = project_indexes(file_ids)
    if place:
        files = _files_in_place(files, str(place))
    files = restrict_files(files, captured_after, captured_before, int(place_id))
    if not files:
        raise ToolError("no indexed footage yet" + (f" ({len(missing)} file(s) still indexing or not indexed)" if missing else "")
                        + "; index_status_tool shows progress")
    filters = dict(filters or {})
    person_note = {}
    if person:
        filters["person_shots"], person_note = _person_shots(files, str(person))
    # A file without search vectors yet cannot match words; when none has them, say so instead of embedding the query.
    searchable = [f for f in files if f.vector_model]
    not_searchable = [f.name for f in files if not f.vector_model]
    if query and not searchable:
        raise ToolError(f"none of the {len(files)} indexed file(s) has search vectors yet (they are made on this computer while "
                        "indexing; index_status_tool shows each file). Searching with filters (camera move, colour, duration, "
                        "talking) still works, and get_transcript_tool reads what was said")
    qv = embed_query(query, searchable) if query else None
    if qv is not None:
        not_searchable += [f.name for f in searchable if f.vector_model not in qv]      # their model could not embed the query
    ref_vec = ref_look = None
    if reference_file_id:
        ref = next((f for f in files if f.file_id == str(reference_file_id)), None)
        if ref is None:
            raise ToolError(f"reference file {reference_file_id!r} has no saved index")
        lo, hi = (reference_start, reference_end) if reference_end is not None else (None, None)
        if match == "look":
            ref_look = msearch.reference_look_from(ref, lo, hi)
            if ref_look is None:
                raise ToolError("that reference has no measured look yet")
        else:
            ref_vec = msearch.reference_vector_from(ref, lo if lo is not None else 0.0, hi if hi is not None else ref.duration + 1.0)
            if ref_vec is None:
                raise ToolError("that reference has no picture vectors yet (not fully indexed)")
            ref_vec = {ref.vector_model: ref_vec}
    result = msearch.search(files, query_vector=qv, query_text=query, reference_vector=ref_vec, reference_look=ref_look, look_for=look_for or "",
                            filters=filters or {}, limit=int(limit), offset=int(cursor), sort=str(sort or "relevance"))
    hits = [_nonempty({**h, "sha": None, "scores": h["scores"]}) for h in result["hits"]]
    if person_note:
        for h, raw in zip(hits, result["hits"]):
            h["person_confidence"] = person_note["confidence"].get(raw["sha"], {}).get(raw["shot_id"])
    summary = f"{result['total']} match(es)" + (f", showing {len(hits)}" if len(hits) < result["total"] else "")
    if not hits:
        summary = "no matches" + (" (a weak match is treated as none; try other words or fewer filters)" if result["ranked"] or query else "")
    extra = {"person": person_note["who"], "unsure_shots": person_note["unsure_shots"],
             "heard_only_shots": person_note["heard_only_shots"]} if person_note else {}
    return ok(summary, hits=hits, total=result["total"], next=result["next"], not_indexed=missing[:20],
              **({"not_searchable": not_searchable[:20]} if query and not_searchable else {}), **extra)


def _files_in_place(files, query: str):
    """The files shot at a place whose name contains *query* (case ignored). Raises ToolError listing the names there are."""
    from classes.media_index import gazetteer, trip
    outline = trip.trip_outline([{"file_id": f.file_id, "captured_at": f.captured_at, "gps": f.gps, "duration": f.duration} for f in files])
    want = " ".join(query.lower().split())
    names, hit = [], set()
    for p in outline["places"]:
        d = gazetteer.describe(p["lat"], p["lon"])
        label = d["label"] if d else ""
        if label:
            names.append(label)
        if want and want in label.lower():
            hit.update(p["file_ids"])
    if not hit:
        raise ToolError(f"no footage shot in {query!r}" + (f" (places: {', '.join(dict.fromkeys(names))[:300]})" if names else ": none of the clips has a position that can be named"))
    return [f for f in files if f.file_id in hit]


def _person_shots(files, person: str):
    """({file_id: {shot ids}}, note) for search's person filter. Raises ToolError saying what to do when it cannot be answered."""
    from classes.media_index import people as pp
    from classes.media_index.flags import people_enabled
    if not people_enabled():
        raise ToolError("searching by person needs the people preference turned on (Preferences) and a people scan of the footage")
    reg = pp.load_registry()
    found = pp.find_people(reg, person)
    if not found:
        known = [p["name"] or p["id"] for p in reg["people"] if p.get("name")][:12]
        raise ToolError(f"no person called {person!r}" + (f" (named people: {', '.join(known)})" if known else ": name someone first with name_person_tool, or look with list_people_tool"))
    scans = {fi.sha: s for fi in files for s in [pp.load_scan(fi.sha)] if s}
    unscanned = [fi.name for fi in files if fi.media_type == "video" and fi.sha not in scans]
    by_sha = pp.shots_with(reg, [p["id"] for p in found], scans)
    unsure = 0
    for fi in files:
        scan = scans.get(fi.sha)
        for tr in (pp.resolve_tracks(fi.sha, scan, reg) if scan else []):
            if tr["status"] == "unsure" and tr["candidate"] in {p["id"] for p in found}:
                unsure += 1
    spoken: dict = {}
    for fi in files:
        vscan = pp.load_voice_scan(fi.sha)
        if vscan:
            got = pp.speaking_shots(fi.sha, vscan, reg, [p["id"] for p in found], fi.shots)
            if got:
                spoken[fi.sha] = got
    # Hearing someone is not seeing them (a narrator speaks over other footage): voice-only shots are counted, not returned.
    heard_only = sum(1 for sha, got in spoken.items() for shot_id in got if shot_id not in by_sha.get(sha, {}))
    by_file = {fi.file_id: set(by_sha.get(fi.sha, {})) for fi in files}
    note = {"who": {"ids": [p["id"] for p in found], "name": found[0].get("name"), "not_scanned": unscanned[:12] or None},
            "unsure_shots": unsure, "confidence": by_sha, "heard_only_shots": heard_only}
    return by_file, note


@editor_tool(
    "get_segment_dossier_tool",
    covers=("index.dossier",),
    label="Read footage notes",
    schema=obj({
        "file_id": string("Project file id (list_project_files_tool).", ""),
        "file_query": string("Describe the file instead of an id (its name).", ""),
        "start": number("Start second (default: the beginning).", None, minimum=0),
        "end": number("End second (default: the end).", None, minimum=0),
        "max_chars": integer("Size limit of the notes; a long file stops and says where to continue.", 6000,
                             minimum=500, maximum=20000),
    }),
    read_only=True,
)
def get_segment_dossier(file_id="", file_query="", start=None, end=None, max_chars=6000):
    """Everything the index knows about a file, or a stretch of it, as compact text, without looking at frames:
    each shot in time order with what happens, who says what, camera move, shot type, mood, objects, on-screen
    text, sounds and its measured look (brightness, warmth, saturation, palette), plus the file's loudness,
    tempo and silent stretches. Use it to understand footage before editing it. Layers not indexed yet are named.
    """
    f = resolve_files([file_id] if file_id else None, file_query)[0]
    fi = _index_for(f)
    if fi is None or not (fi.shots or fi.rows):
        raise ToolError(f"{_display_name(f.data)!r} has no saved index yet; index_status_tool shows progress")
    d = build_dossier(fi, start, end, max_chars=int(max_chars))
    return ok(f"notes for {_display_name(f.data)}: {d['shots_shown']} of {d['shots_in_range']} shots", file_id=str(f.id),
              notes=d["text"], next_start=d["next_start"])


@editor_tool(
    "get_look_profile_tool",
    covers=("index.look",),
    label="Read look profile",
    schema=obj({
        "file_id": string("Project file id.", ""),
        "file_query": string("Describe the file instead of an id (its name).", ""),
        "start": number("Start second of a range (default: the whole file).", None, minimum=0),
        "end": number("End second of the range.", None, minimum=0),
        "compare_to_file_id": string("Also report how far this look is from another file's look (0 = same).", ""),
    }),
    read_only=True,
)
def get_look_profile(file_id="", file_query="", start=None, end=None, compare_to_file_id=""):
    """The measured colour look of a file or a range of it, read from the index with no rendering: brightness,
    warmth, green/magenta tint, saturation, contrast, clipping and average colour (0-1 units), plus
    per-shot variation for the whole file and the colour pipeline flags (HDR / log footage). Pass
    compare_to_file_id to get a distance to another file's look, the number the grading tools aim to shrink.
    """
    from classes import color_agent as ca
    f = resolve_files([file_id] if file_id else None, file_query)[0]
    fi = _index_for(f)
    if fi is None or not fi.layers.get(S.LAYER_LOOK):
        raise ToolError(f"{_display_name(f.data)!r} has no measured look yet; index_status_tool shows progress")
    lo, hi = (start, end) if end is not None else (None, None)
    profile = msearch.reference_look_from(fi, lo, hi)
    if profile is None:
        raise ToolError("no measurable picture in that range (black, or audio only)")
    out = {"file_id": str(f.id), "profile": profile, "pipeline": fi.pipeline, "warnings": [w.split(":")[0] for w in fi.warnings]}
    if lo is None:
        lumas = [s["look"]["avg_luma"] for s in fi.shots if (s.get("look") or {}).get("present")]
        warms = [s["look"]["warm_cool"] for s in fi.shots if (s.get("look") or {}).get("present")]
        if lumas:
            out["across_shots"] = {"luma_min": round(min(lumas), 3), "luma_max": round(max(lumas), 3),
                                   "warmth_min": round(min(warms), 3), "warmth_max": round(max(warms), 3), "shots": len(lumas)}
    if compare_to_file_id:
        other = next(iter(resolve_files([compare_to_file_id])))
        ofi = _index_for(other)
        oprof = msearch.reference_look_from(ofi) if ofi else None
        if oprof is None:
            raise ToolError("the comparison file has no measured look yet")
        out["distance_to_compare"] = ca.look_profile_distance(profile, oprof)
    return ok(f"look of {_display_name(f.data)}", **out)


def _not_applicable_layers(shelf, sha, ready) -> list:
    """Layers this file will never have (no speech in a silent clip, no picture in a song)."""
    return [layer for layer in LAYER_ORDER if not ready[layer] and sha
            and (shelf.layer(sha, layer) or {}).get("status") == S.NOT_APPLICABLE]


def missing_layers(file_data, shelf=None) -> list:
    """The index layers a file still lacks (and will not simply never have); [] when it is complete or has no fingerprint to look up."""
    shelf = shelf or default_shelf()
    sha = sha_of(file_data.get("fingerprint"))
    if not sha:
        return []
    ready = {layer: shelf.layer_ready(sha, layer) for layer in S.LAYER_VERSIONS}
    ready[S.LAYER_VECTORS] = _vectors_current(shelf, sha)
    na = _not_applicable_layers(shelf, sha, ready)
    return [layer for layer in LAYER_ORDER if not ready[layer] and layer not in na]


def _vectors_current(shelf, sha) -> bool:
    """The file's search vectors were made by the model the next index run would use (changing the model re-embeds)."""
    from classes.media_index import embedder as E, vectors
    from classes.media_index.job import IndexingJob
    return vectors.is_current(shelf, sha, E.chosen_model(IndexingJob._signed_in()))


@editor_tool(
    "index_status_tool",
    covers=("index.status",),
    label="Index status",
    schema=obj({
        "file_ids": array({"type": "string"}, "Only these project files (default: all video, audio and image files)."),
        "only_incomplete": boolean("Only files that are missing a layer.", False),
    }),
    read_only=True,
)
def index_status(file_ids=None, only_incomplete=False):
    """Which parts of each file's index are ready: structure (cuts, camera motion), look (colour), audio (loudness,
    tempo), speech (transcript) and vectors_local (search; vector_model names the model that made them).
    described_shots counts the shots Gemini has described so far (made on demand by describe_shots_tool, not needed
    for search). Use it to know whether search_footage_tool can already see a file, and what is still being worked on.
    """
    shelf = default_shelf()
    wanted = {str(x) for x in file_ids} if file_ids else None
    rows = []
    for f in _all_files():
        if wanted is not None and str(f.id) not in wanted:
            continue
        kind = str(f.data.get("media_type") or "video")
        if kind not in ("video", "audio", "image"):
            continue
        sha = sha_of(f.data.get("fingerprint"))
        ready = {layer: bool(sha) and shelf.layer_ready(sha, layer) for layer in S.LAYER_VERSIONS}
        stale = [layer for layer in LAYER_ORDER if ready[layer] and layer != S.LAYER_VECTORS
                 and not shelf.layer_ready(sha, layer, version=S.LAYER_VERSIONS[layer])]
        ready[S.LAYER_VECTORS] = bool(sha) and _vectors_current(shelf, sha)      # another model's vectors are not searchable: missing
        na = _not_applicable_layers(shelf, sha, ready)
        missing = [layer for layer in LAYER_ORDER if not ready[layer] and layer not in na]
        if only_incomplete and not missing:
            continue
        rows.append({"file_id": str(f.id), "name": _display_name(f.data), "media_type": kind,
                     "ready": [layer for layer in LAYER_ORDER if ready[layer]], "missing": missing, "not_applicable": na, "stale": stale,
                     "vector_model": (shelf.layer(sha, S.LAYER_VECTORS) or {}).get("model") if sha else None,
                     "described_shots": int((shelf.layer(sha, S.LAYER_WATCH) or {}).get("shots") or 0) if sha else 0,
                     "searchable": ready[S.LAYER_VECTORS] or ready[S.LAYER_STRUCTURE]})
    log.debug("index_status: %d files", len(rows))
    complete = sum(1 for r in rows if not r["missing"])
    return ok(f"{complete} of {len(rows)} files fully indexed", files=rows)


@editor_tool(
    "index_long_file_tool",
    covers=("index.long_file",),
    label="Describe a long file",
    schema=obj({
        **FILE_TARGET,
        "approve": boolean("Say yes: describe every shot now (search does not need it). Leave false to only see what it would involve and cost.", False),
    }),
)
def index_long_file(file_ids=None, file_query="", approve=False):
    """Describe every shot of a file longer than 30 minutes. Every file is searchable without this; describing a long one
    takes several cloud passes and uses credits, so it needs a yes (shorter files: describe_shots_tool). Call with approve=false to see
    how many passes and the most it can cost (a ceiling: the real charge follows what the AI used and is usually lower);
    ask the user; then call with approve=true. Each pass is charged on its own, so an interrupted run never repays what
    already finished.
    """
    from classes.media_index import longfile
    shelf = default_shelf()
    rows, to_queue = [], []
    for f in resolve_files(file_ids, file_query):
        data = f.data
        kind = str(data.get("media_type") or "video")
        duration = float(data.get("duration") or 0.0)
        row = {"file_id": str(f.id), "name": _display_name(data), "media_type": kind, "duration_seconds": round(duration, 1)}
        sha = sha_of(data.get("fingerprint"))
        if kind not in ("video", "audio"):
            row.update(state="not_applicable", note="only video and audio files are described")
        elif not longfile.is_long(duration):
            row.update(state="not_long", note="under 30 minutes: describe its shots with describe_shots_tool")
        elif not sha:
            row.update(state="not_ready", note="the file has no fingerprint yet; wait for import to finish")
        else:
            row.update(longfile.estimate(duration))
            if shelf.layer_ready(sha, S.LAYER_WATCH):
                row.update(state="described", note="already described")
            elif data.get("index_long_ok") and not approve:
                row.update(state="approved", note="already approved; it is queued or running (see index_status_tool)")
            else:
                row.update(state="will_describe" if approve else "needs_approval")
                if approve:
                    to_queue.append(f)
        rows.append(row)

    def queue():
        files_model = get_app().window.files_model
        for f in to_queue:
            f.data["index_long_ok"] = True
            f.save()
            files_model._enqueue_index(str(f.id))

    if to_queue:
        on_main(queue)
    if approve:
        head = f"Queued {len(to_queue)} long file(s) to be described." if to_queue else "Nothing to queue."
    else:
        asking = [r for r in rows if r["state"] == "needs_approval"]
        head = (f"{len(asking)} file(s) need a yes: " + "; ".join(f"{r['name']} ({r['minutes']} min, {r['chunks']} passes, up to {r['max_credits']} credits)" for r in asking)
                if asking else "No long file is waiting for approval.")
    return ok(head, changed=bool(to_queue), files=rows, ceiling_note="max_credits is a flat ceiling per minute; the real charge follows token use")


MAX_DESCRIBE_SHOTS = 300


def describe_file_shots(f, fi, shot_ids):
    """Gemini descriptions for *shot_ids* of one file, made now for the ones that have none: (result, None) or (None, why not)."""
    from classes.api_client import get_backend_client
    from classes.credits_client import check_operation
    from classes.media_index import cloud, embedder as E
    from classes.media_index.job import IndexingJob
    from classes.media_index.probe import probe_media
    from classes.path_utils import absolute_media_path
    if not IndexingJob._signed_in():
        return None, "sign in to Zenvi for shot descriptions (search, pictures and measured facts work without them)"
    by_id = {int(s["id"]): s for s in fi.shots}
    from classes.media_index import longfile
    wanted = [i for i in dict.fromkeys(int(x) for x in shot_ids) if i in by_id]
    if len(wanted) > MAX_DESCRIBE_SHOTS:
        return None, f"at most {MAX_DESCRIBE_SHOTS} shots a call; ask for fewer"
    todo = [i for i in wanted if not by_id[i].get("watch")]
    if todo:
        seconds = cloud.upload_seconds([by_id[i] for i in todo])          # what is uploaded and billed, gaps between runs excluded
        if longfile.is_long(seconds) and not f.data.get("index_long_ok"):
            return None, "that is over 30 minutes of footage to describe: ask for a shorter range, or approve it with index_long_file_tool"
        _, _balance, blocked = check_operation("indexing_per_minute", "shot descriptions", duration_seconds=max(1.0, seconds))
        if blocked:
            return None, blocked
    path = absolute_media_path(f.data.get("path")) or str(f.data.get("path") or "")
    try:
        out = cloud.describe_shots(get_backend_client(), path, probe_media(path), fi.sha, default_shelf(), wanted,
                                   E.get_embedder(fi.vector_model or E.MODEL_LOCAL), file_id=str(f.id))
    except (cloud.Cancelled, E.EmbedError) as exc:
        return None, str(exc)
    if out.get("auth"):
        return None, "sign in to Zenvi for shot descriptions"
    if out.get("error"):
        return None, str(out["error"])
    return out, None


@editor_tool(
    "describe_shots_tool",
    covers=("index.describe",),
    label="Describe shots",
    schema=obj({
        "file_id": string("The video whose shots to describe.", ""),
        "shot_ids": array({"type": "integer"}, "Shot ids, as search_footage_tool and the storyboard shot list give them."),
        "start_seconds": number("Or every shot between these seconds of the file.", None, minimum=0),
        "end_seconds": number("End of that range.", None, minimum=0),
    }),
    read_only=True,
)
def describe_shots(file_id="", shot_ids=None, start_seconds=None, end_seconds=None):
    """Words for shots, from Gemini: what happens, actions, objects, text on screen, sounds, mood, shot type, how interesting and
    usable each is. Search does NOT need this (every file is searchable from its pictures, speech and sound); call it for the shots
    you are about to reason about or write about. Each shot is described once and kept, so asking again is free; new shots use
    credits and need sign-in. The new descriptions also become searchable.
    """
    f = next(iter(resolve_files([file_id])))
    fi = _index_for(f)
    if fi is None or not fi.shots:
        raise ToolError("that file has no shot list yet (index_status_tool shows progress)")
    ids = [int(x) for x in shot_ids or []]
    if start_seconds is not None and end_seconds is not None:
        ids += [int(s["id"]) for s in fi.shots_between(float(start_seconds), float(end_seconds))]
    if not ids:
        raise ToolError("give shot_ids, or start_seconds and end_seconds")
    if len(set(ids)) > MAX_DESCRIBE_SHOTS:
        raise ToolError(f"at most {MAX_DESCRIBE_SHOTS} shots a call; ask for a shorter range")
    out, why = describe_file_shots(f, fi, ids)
    if why or out is None:
        raise ToolError(why or "no descriptions came back")
    shots = [_nonempty({k: s.get(k) for k in ("id", "start", "end", "description", "actions", "on_screen_text", "sound_events", "mood",
                                               "shot_type", "interest", "usable", "usable_reason")}) for s in out["shots"]]
    return ok(f"{len(shots)} shot(s) described ({len(out['described'])} new)", file_id=str(f.id), shots=shots)


@editor_tool(
    "locate_in_footage_tool",
    covers=("index.locate",),
    label="Locate in footage",
    schema=obj({
        "what": string("What to find: an object ('red car', 'torch'), or on-screen text ('OPEN', 'SALE')."),
        "kind": enum(["auto", "object", "text"], "'object' = things seen, 'text' = written words, 'auto' = both.", "auto"),
        "file_ids": array({"type": "string"}, "Only these project files (default: every indexed file)."),
        "start": number("Only after this second of each file.", None, minimum=0),
        "end": number("Only before this second of each file.", None, minimum=0),
        "limit": integer("Most results to return.", 30, minimum=1, maximum=200),
        "for_action": enum(["", "blur_object", "highlight_object", "mask_object"],
                           "To blur, highlight or cut out what you find, say which: each of the first 5 results then comes with a ready 'handoff' "
                           "(the arguments for enhance_file_with_comfyui_tool).", ""),
    }, required=["what"]),
    read_only=True,
)
def locate_in_footage(what, kind="auto", file_ids=None, start=None, end=None, limit=30, for_action=""):
    """Find where an object or on-screen text appears, with when (seconds in the file) and where in the frame.
    box is [x, y, width, height] as 0-1 fractions from the top-left. It is a ROUGH box from the indexing pass:
    good for choosing where to look, point-masking or a region prompt, but not pixel-exact; refine it on a
    frame (view_frames_tool, then the masking or tracking effect) before masking, blurring, removing or
    replacing the thing. For a generative replace or removal, pass the file, time and region to the
    generation tool that is available. With for_action (blur_object, highlight_object or mask_object) each of the first
    5 results carries a handoff: the box in source pixels (grown a little, since it is rough), a point on the object, the seed
    frame and a prompt, ready to pass to enhance_file_with_comfyui_tool (needs the optional ComfyUI AI tools to be ready).
    """
    from classes.media_index import handoff, reference as mref
    files, missing = project_indexes(file_ids)
    if not files:
        raise ToolError("no indexed footage yet; index_status_tool shows progress")
    hits = mref.locate(files, what, kind=kind, start=start, end=end, limit=int(limit))
    if for_action:
        by_file = {str(fi.file_id): fi for fi in files}
        by_project = {str(f.id): f for f in _all_files()}
        for hit in hits[:handoff.MAX_HANDOFFS]:
            fi, pf = by_file.get(str(hit["file_id"])), by_project.get(str(hit["file_id"]))
            video = ((fi.technical or {}).get("video") or {}) if fi else {}
            data = pf.data if pf is not None else {}
            width, height = int(data.get("width") or video.get("width") or 0), int(data.get("height") or video.get("height") or 0)
            fps = float(video.get("fps") or 0.0)
            length = data.get("video_length")
            try:
                frames = int(float(length)) if length else None
            except (TypeError, ValueError):
                frames = None
            hit["handoff"] = handoff.mask_handoff(hit, width, height, fps, frames, for_action) or {"unavailable": "no box for this hit, or the frame size is unknown: "
                                                                                                "use view_frames_tool to choose points on the object"}
    summary = f"{len(hits)} place(s) where {what!r} appears" if hits else f"{what!r} was not found in the indexed footage"
    note = ("Boxes are rough. Only what the indexing pass noticed is listed, so a missing result is not proof the "
            "thing is absent: try search_footage_tool with a description, or view_frames_tool on the footage.")
    return ok(summary, hits=hits, note=note, not_indexed=missing[:20])


@editor_tool(
    "view_audio_tool",
    covers=("index.audio_view",),
    label="View audio",
    schema=obj({
        "file_id": string("Project file id.", ""),
        "file_query": string("Describe the file instead of an id (its name).", ""),
        "start": number("Start second (default: the beginning).", None, minimum=0),
        "end": number("End second (default: 60 s after start).", None, minimum=0),
        "timeline": boolean("Look at the finished timeline's mixed sound instead of a file: start and end are then timeline seconds, the mix is "
                            "rendered (not kept), and the levels of each clip over time come back as numbers.", False),
    }),
    read_only=True,
)
def view_audio(file_id="", file_query="", start=None, end=None, timeline=False):
    """Draw a spectrogram of a stretch of audio as a PNG you can look at: time across, frequency up (logarithmic, 20 Hz to 11 kHz),
    brightness = loudness in dBFS, with labelled axes and a legend. NUMBERS FIRST: loudness, silence, beats, tempo, speech and
    music sections are already in the dossier, search results, analyze_music_tool and review_edit_tool, and answer "find the quiet
    part", "cut on the beat", "where does she speak", "where is the music loud". Use the picture only (1) where
    spectrogramSuggested on analyze_music_tool says a section edge is uncertain and the edit depends on song structure, (2) to
    compare takes or tracks, (3) to check a finished mix: with timeline=true it shows the rendered mix, and the numbers beside
    it give each clip's level over time (does the music duck under the voice, does a cut jump in loudness). Do not use it to find
    words, silence or beats: those are better as numbers. A range must be given; each strip covers at most 60 s and one call returns
    at most 4. Steady tones and hum are horizontal lines, speech is stacked bands that move, drums are vertical bars, silence is dark.
    The time axis of each strip starts at 0 at its start. Strips of a file are kept; a timeline render is not.
    """
    from classes.media_index import spectro
    if timeline:
        return _view_timeline_audio(start, end)
    f = resolve_files([file_id] if file_id else None, file_query)[0]
    sha = sha_of(f.data.get("fingerprint"))
    if not sha:
        raise ToolError(f"{_display_name(f.data)!r} has not been fingerprinted yet (wait for the import to finish)")
    shelf = default_shelf()
    fi = _index_for(f, shelf)
    source = (shelf.manifest(sha).get("source") or {})
    duration = float(source.get("duration") or (fi.duration if fi else 0.0) or f.data.get("duration") or 0.0)
    lo = float(start or 0.0)
    hi = float(end) if end is not None else min(duration or lo + spectro.MAX_SECONDS, lo + spectro.MAX_SECONDS)
    try:
        strips = spectro.plan_strips(lo, hi, duration)
    except ValueError as exc:
        raise ToolError(str(exc))
    from classes.path_utils import absolute_media_path
    path = absolute_media_path(f.data.get("path")) or str(f.data.get("path") or "")
    made = []
    for a, b in strips:
        out = spectro.make_spectrogram(path, sha, shelf, a, b, duration, has_audio=source.get("has_audio") is not False)
        if not out["ok"]:
            raise ToolError(out["error"])
        made.append({"start": out["start"], "end": out["end"], "image_path": out["path"], "cached": out["cached"]})
    head = f"spectrogram of {_display_name(f.data)} {made[0]['start']:.1f}-{made[-1]['end']:.1f} s" + (f" in {len(made)} strips" if len(made) > 1 else "")
    return ok(head, file_id=str(f.id), image_path=made[0]["image_path"], strips=made, start=made[0]["start"], end=made[-1]["end"],
              cached=all(m["cached"] for m in made), axes="x = seconds from each strip's start, y = Hz (log), colour = dBFS",
              numbers_first="loudness, beats and sections are available as numbers (analyze_music_tool, get_segment_dossier_tool)")


def _view_timeline_audio(start, end):
    """The rendered timeline mix as spectrogram strips, with each clip's level over time as numbers."""
    import os
    from classes.editor_tools.media_index_tools_review import build_timeline, render_timeline_mix
    from classes.media_index import review as R, spectro
    if start is None or end is None:
        raise ToolError("give start and end (timeline seconds) for the part of the mix to look at")
    lo, hi = float(start), float(end)
    try:
        strips = spectro.plan_strips(lo, hi)
    except ValueError as exc:
        raise ToolError(str(exc))
    clips, project, _objs = build_timeline()
    hi_clamped = min(hi, project.duration) if project.duration else hi
    if hi_clamped - lo < spectro.MIN_SECONDS:
        raise ToolError("that range is past the end of the timeline")
    path, why = render_timeline_mix(lo, hi_clamped)
    if not path:
        raise ToolError("could not render the mix: " + why)
    made = []
    try:
        import tempfile
        folder = tempfile.mkdtemp(prefix="zenvi_mixview_")
        for a, b in plan_clamped(strips, hi_clamped):
            png = os.path.join(folder, f"mix_{int(a * 1000)}_{int(b * 1000)}.png")
            ok_, err = spectro.draw_strip(path, a - lo, b - lo, png)
            if not ok_:
                raise ToolError(err)
            made.append({"start": a, "end": b, "image_path": png, "cached": False})
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    cache: dict = {}

    def index_for(c):
        if c.sha not in cache:
            cache[c.sha] = library.get_file_index(default_shelf(), c.sha, file_id=c.file_id, name=c.name) if c.sha else None
        return cache[c.sha]

    levels = []
    for c in clips:
        if not c.has_audio or c.end <= lo or c.start >= hi_clamped or c.length <= 0:
            continue
        seconds = []
        t = max(lo, c.start)
        while t < min(hi_clamped, c.end) and len(seconds) < 120:
            t2 = min(t + 1.0, c.end, hi_clamped)
            base = R.source_level_db(c, index_for(c), c.to_source(t), c.to_source(t2))
            seconds.append({"t": round(t, 2), "db": None if base is None else round(base + c.gain_over(t, t2), 1)})
            t = t2
        levels.append({"clip": c.id, "name": c.name, "role": c.role, "per_second": seconds, "volume_automated": c.gain_fn is not None})
    return ok(f"spectrogram of the timeline mix {made[0]['start']:.1f}-{made[-1]['end']:.1f} s with the level of {len(levels)} clip(s) over time",
              image_path=made[0]["image_path"], strips=made, levels=levels[:12], start=made[0]["start"], end=made[-1]["end"], cached=False,
              axes="x = seconds from each strip's start, y = Hz (log), colour = dBFS",
              numbers_first="read the levels as numbers (dB, source level plus the gain set); the picture shows what the mix looks like, not how loud each part is")


def plan_clamped(strips, hi):
    """The planned strips cut to the end of the timeline."""
    return [(a, min(b, hi)) for a, b in strips if a < hi]
