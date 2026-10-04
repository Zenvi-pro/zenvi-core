"""Media index tools: search the footage by what happens, what is said and how it looks.

These read the saved per-file index (``classes.media_index``): one index per file content, so the
same footage is searchable in every project. Nothing here mutates the project.
"""

from __future__ import annotations

import base64

import numpy as np

from classes.editor_tools._base import ToolError, array, boolean, enum, integer, mapping, number, obj, ok, string
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.media_files import _display_name, resolve_files
from classes.logger import log
from classes.media_index import library, schema as S, search as msearch
from classes.media_index.dossier import build_dossier
from classes.media_index.store import default_shelf, sha_of

QUERY_LIMIT_CHARS = 500
LAYER_ORDER = ("structure", "look", "audio", "speech", "watch", "vectors")
CLOUD_LAYERS = ("watch", "vectors")


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


def embed_query(text: str):
    """Unit vector of a search phrase from the cloud, or raises ToolError with what to do."""
    from classes.api_client import get_backend_client
    out = get_backend_client().v2_embed([{"kind": "text", "id": "q", "text": text}], dims=S.EMBED_DIMS, task_type="RETRIEVAL_QUERY")
    if out.get("auth"):
        raise ToolError("sign in to Zenvi to search by description (searching with filters or a reference clip works signed out)")
    if out.get("unsupported"):
        raise ToolError("this backend has no media index v2 search yet; use search_clips_tool")
    vectors = out.get("vectors") or []
    if out.get("error") or not vectors or not vectors[0]:
        raise ToolError("could not embed the query: " + str(out.get("error") or "no vector returned"))
    return np.frombuffer(base64.b64decode(vectors[0]), dtype="<f2").astype(np.float32)


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
        "limit": integer("Most results to return.", 10, minimum=1, maximum=50),
        "cursor": integer("Where to continue: the `next` of the previous result.", 0, minimum=0),
    }),
    read_only=True,
)
def search_footage(query="", look_for="", reference_file_id="", reference_start=None, reference_end=None, match="picture",
                   filters=None, file_ids=None, sort="relevance", captured_after="", captured_before="", place_id=-1, limit=10, cursor=0):
    """Search every indexed file in the project and return the best moments, each with the file, the in and out
    seconds (snapped to the real cut, or to the spoken sentence), where in it the match peaks and why.

    It searches shot descriptions, what people say, and the pictures themselves, then ranks them together, so
    "the dog on the beach" and "where he says thank you" both work. Add filters for measured facts (camera
    move, shot type, colour, duration, talking or not). A reference file or range finds similar footage, by
    picture or by colour. Use the returned file_id/start/end directly with the clip tools. Files not yet
    indexed are listed in `not_indexed`; call index_status_tool to see progress.
    """
    query = str(query or "").strip()[:QUERY_LIMIT_CHARS]
    files, missing = project_indexes(file_ids)
    files = restrict_files(files, captured_after, captured_before, int(place_id))
    if not files:
        raise ToolError("no indexed footage yet" + (f" ({len(missing)} file(s) still indexing or not indexed)" if missing else "")
                        + "; index_status_tool shows progress")
    qv = embed_query(query) if query else None
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
    result = msearch.search(files, query_vector=qv, reference_vector=ref_vec, reference_look=ref_look, look_for=look_for or "",
                            filters=filters or {}, limit=int(limit), offset=int(cursor), sort=str(sort or "relevance"))
    hits = [_nonempty({**h, "sha": None, "scores": h["scores"]}) for h in result["hits"]]
    summary = f"{result['total']} match(es)" + (f", showing {len(hits)}" if len(hits) < result["total"] else "")
    if not hits:
        summary = "no matches" + (" (a weak match is treated as none; try other words or fewer filters)" if result["ranked"] or query else "")
    return ok(summary, hits=hits, total=result["total"], next=result["next"], not_indexed=missing[:20])


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
    tempo), speech (transcript), watch (what happens, objects, text) and vectors (search). Use it to know
    whether search_footage_tool can already see a file, and what is still being worked on.
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
        older = [layer for layer in LAYER_ORDER if ready[layer] and not shelf.layer_ready(sha, layer, version=S.LAYER_VERSIONS[layer])]
        stale = [layer for layer in older if layer not in CLOUD_LAYERS]            # free: refreshed on the next index run
        older_cloud = [layer for layer in older if layer in CLOUD_LAYERS]         # paid: kept as is, newer fields read as unknown
        na = [layer for layer in LAYER_ORDER if not ready[layer] and sha
              and (shelf.layer(sha, layer) or {}).get("status") == S.NOT_APPLICABLE]
        missing = [layer for layer in LAYER_ORDER if not ready[layer] and layer not in na]
        if only_incomplete and not missing:
            continue
        rows.append({"file_id": str(f.id), "name": _display_name(f.data), "media_type": kind,
                     "ready": [layer for layer in LAYER_ORDER if ready[layer]], "missing": missing, "not_applicable": na, "stale": stale, "older_cloud": older_cloud,
                     "searchable": ready["vectors"] or ready["structure"]})
    log.debug("index_status: %d files", len(rows))
    complete = sum(1 for r in rows if not r["missing"])
    return ok(f"{complete} of {len(rows)} files fully indexed", files=rows)


@editor_tool(
    "match_reference_tool",
    covers=("index.match",),
    label="Match reference",
    schema=obj({
        "reference_file_id": string("The file to recreate (a reference scene, an anime clip, a rough cut).", ""),
        "reference_start": number("Start second of the part to recreate (default: the beginning).", None, minimum=0),
        "reference_end": number("End second of the part to recreate (default: the end).", None, minimum=0),
        "candidate_file_ids": array({"type": "string"}, "Only use these project files as source footage (default: every "
                                    "indexed file except the reference)."),
        "per_shot": integer("Candidates to return for each reference shot.", 3, minimum=1, maximum=8),
        "filters": mapping("Same facts as search_footage_tool's filters (camera, shot_type, look, orientation, "
                           "min_duration...), applied to the candidates."),
    }),
    read_only=True,
)
def match_reference(reference_file_id="", reference_start=None, reference_end=None, candidate_file_ids=None, per_shot=3,
                    filters=None):
    """Plan a recreation of a reference, shot by shot, from the footage in the project. For each shot of the
    reference it finds the project footage that looks most like it (its own saved frames against every other
    file's pictures, no upload) and returns ready cut windows, in/out seconds of exactly the reference shot's
    length, centred on the best moment and kept inside the source shot. status: matched = a candidate is long
    enough; short_only = only too-short footage exists (see short_by); no_match = nothing similar, with a
    stock_query to search stock footage for that gap. Place the windows in order, then check with
    inspect_timeline_tool. Black shots and flashes under 0.25 s are skipped.
    """
    from classes.media_index import reference as mref
    files, missing = project_indexes()
    ref = next((f for f in files if f.file_id == str(reference_file_id)), None)
    if ref is None:
        raise ToolError(f"reference file {reference_file_id!r} has no saved index yet; index_status_tool shows progress")
    wanted = {str(x) for x in candidate_file_ids} if candidate_file_ids else None
    pool = [f for f in files if wanted is None or f.file_id in wanted]
    if not [f for f in pool if f.sha != ref.sha]:
        raise ToolError("no other indexed footage to match against; import and index the source clips first")
    out = mref.match_reference(ref, pool, reference_start, reference_end, per_shot=int(per_shot), filters=filters or {})
    if not out["total"]:
        raise ToolError("the reference has no analysed shots in that range")
    gaps = [r["reference_shot"] for r in out["shots"] if r["status"] != "matched"]
    return ok(f"{out['matched']} of {out['total']} reference shots have matching footage" + (f"; gaps: shots {gaps}" if gaps else ""),
              reference_file_id=str(reference_file_id), shots=out["shots"], matched=out["matched"], total=out["total"],
              not_indexed=missing[:20])


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
    }, required=["what"]),
    read_only=True,
)
def locate_in_footage(what, kind="auto", file_ids=None, start=None, end=None, limit=30):
    """Find where an object or on-screen text appears, with when (seconds in the file) and where in the frame.
    box is [x, y, width, height] as 0-1 fractions from the top-left. It is a ROUGH box from the indexing pass:
    good for choosing where to look, point-masking or a region prompt, but not pixel-exact; refine it on a
    frame (inspect_media_tool, then the masking or tracking effect) before masking, blurring, removing or
    replacing the thing. For a generative replace or removal, pass the file, time and region to the
    generation tool that is available.
    """
    from classes.media_index import reference as mref
    files, missing = project_indexes(file_ids)
    if not files:
        raise ToolError("no indexed footage yet; index_status_tool shows progress")
    hits = mref.locate(files, what, kind=kind, start=start, end=end, limit=int(limit))
    summary = f"{len(hits)} place(s) where {what!r} appears" if hits else f"{what!r} was not found in the indexed footage"
    note = ("Boxes are rough. Only what the indexing pass noticed is listed, so a missing result is not proof the "
            "thing is absent: try search_footage_tool with a description, or inspect_media_tool on the footage.")
    return ok(summary, hits=hits, note=note, not_indexed=missing[:20])


@editor_tool(
    "view_audio_tool",
    covers=("index.audio_view",),
    label="View audio",
    schema=obj({
        "file_id": string("Project file id.", ""),
        "file_query": string("Describe the file instead of an id (its name).", ""),
        "start": number("Start second (default: the beginning).", None, minimum=0),
        "end": number("End second (default: 90 s after start at most).", None, minimum=0),
    }),
    read_only=True,
)
def view_audio(file_id="", file_query="", start=None, end=None):
    """Draw a spectrogram of a stretch of a file's audio (up to 90 s) as a PNG you can look at: time across,
    frequency up (logarithmic, 20 Hz to 11 kHz), brightness = loudness in dBFS, with labelled axes and a legend.
    Use it to see what the audio is doing: steady tones and hum are horizontal lines, speech is stacked bands
    that move, drums and impacts are vertical bars, music has regular repeating structure, silence is dark.
    The time axis starts at 0 for the start of the strip. Returns the image path to open.
    """
    from classes.media_index import spectro
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
    from classes.path_utils import absolute_media_path
    path = absolute_media_path(f.data.get("path")) or str(f.data.get("path") or "")
    out = spectro.make_spectrogram(path, sha, shelf, lo, hi, duration, has_audio=source.get("has_audio") is not False)
    if not out["ok"]:
        raise ToolError(out["error"])
    return ok(f"spectrogram of {_display_name(f.data)} {out['start']:.1f}-{out['end']:.1f} s", file_id=str(f.id),
              image_path=out["path"], start=out["start"], end=out["end"], cached=out["cached"],
              axes="x = seconds from the strip start, y = Hz (log), colour = dBFS")


# ---------------------------------------------------------------------------
# The older search tools, answered from the local index when media-index-v2 is on
# ---------------------------------------------------------------------------

def _mmss(sec):
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _window_text(h):
    return (f"start_seconds={h['start']:.3f} end_seconds={h['end']:.3f} peak_seconds={h['peak']:.3f} "
            f"({_mmss(h['start'])}–{_mmss(h['end'])})")


def _v2_files():
    """Indexed files that can be searched by meaning, or None when the preference is off or nothing is indexed yet."""
    from classes.media_index.flags import v2_enabled
    if not v2_enabled():
        return None, 0
    files, missing = project_indexes()
    files = [f for f in files if f.layers.get(S.LAYER_VECTORS)]
    return (files or None), len(missing)


def legacy_search_clips(query, k, look_for="", nth=0):
    """search_clips_tool's answer from the local index, in its own line format; None to use the original search."""
    files, missing = _v2_files()
    if files is None:
        return None
    try:
        qv = embed_query(query)
    except ToolError as exc:
        return f"Error: {exc}"
    res = msearch.search(files, query_vector=qv, look_for=look_for or "", limit=60)
    hits = res["hits"]
    if not hits:
        return (f"No index matches for '{query}' in this project's index ({len(files)} indexed media item(s)). "
                "Try a more specific description, or check indexing finished.")
    grouped = {}
    for h in hits:
        grouped.setdefault(h["file_id"], []).append(h)
    lines = [f"Found {len(hits)} match(es) across {len(grouped)} project media item(s) (local index):"]
    for shown, (fid, group) in enumerate(grouped.items()):
        if shown >= k and nth == 0:
            break
        chrono = sorted(group, key=lambda x: x["start"])
        name = group[0]["name"]
        if nth > 0:
            h = chrono[min(nth - 1, len(chrono) - 1)]
            lines.append(f"  • {name} media_bin_file_id={fid} — occurrence #{nth} {_window_text(h)}")
        elif len(chrono) == 1:
            lines.append(f"  • {name} media_bin_file_id={fid} media_type=video — {_window_text(chrono[0])} (rank=1)")
        else:
            lines.append(f"  • {name} media_bin_file_id={fid} — {len(chrono)} occurrences:")
            best = group[0]
            order = {id(h): n for n, h in enumerate(chrono, 1)}
            for h in sorted(group[:8], key=lambda x: x["start"]):
                lines.append(f"      {order[id(h)]}. {_window_text(h)}{'  <-- best match' if h is best else ''}")
            lines.append("      Place the best match unless the user asked for a different one (e.g. 'the 2nd time').")
    if missing:
        lines.append(f"Note: {missing} file(s) are not in the local index yet (index_status_tool).")
    lines.append(
        "To PLACE a moment: place_moment(file_id=..., start_seconds=<keep in>, end_seconds=<keep out>, query=<same "
        "description>, track=..., position_seconds=...). Watch+trim+place are built in — do not call "
        "watch_clip_window_tool or convert to frames. To SLICE an already-placed clip: slice_moment(query, "
        "clip_query=... or timeline_clip_id=...).")
    return "\n".join(lines)


def legacy_search_in_clip(query, k, file_id, clip_start, clip_end, clip_name, look_for=""):
    """search_clip_scenes_tool's answer for one clip's trimmed range from the local index; None to use the original."""
    files, _ = _v2_files()
    if files is None or not any(f.file_id == str(file_id) for f in files):
        return None
    try:
        qv = embed_query(query)
    except ToolError as exc:
        return f"Error: {exc}"
    mine = [f for f in files if f.file_id == str(file_id)]
    res = msearch.search(mine, query_vector=qv, look_for=look_for or "", limit=60)
    inside = [h for h in res["hits"] if h["end"] > clip_start and h["start"] < clip_end][:k]
    if not inside:
        return None        # nothing in this range by meaning: let the scene-description fallback answer
    lines = [f"Index matches in '{clip_name}' ({_mmss(clip_start)} - {_mmss(clip_end)}):"]
    for rank, h in enumerate(inside, 1):
        s, e = max(h["start"], clip_start), min(h["end"], clip_end)
        peak = min(max(h["peak"], s), e)
        lines.append(f"- keep {_mmss(s - clip_start)}-{_mmss(e - clip_start)} peak {_mmss(peak - clip_start)} (rank={rank}, overlap=1.00)")
        if h.get("why"):
            lines.append(f"  transcript: {h['why'][:180]}")
    return "\n".join(lines)
