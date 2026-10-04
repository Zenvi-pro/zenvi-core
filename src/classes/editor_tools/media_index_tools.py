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
        (found if fi and (fi.shots or fi.rows) else missing).append(fi if fi and (fi.shots or fi.rows) else _display_name(f.data))
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
                           "(seconds), exclude_black (default true)."),
        "file_ids": array({"type": "string"}, "Only search these project files (default: every indexed file)."),
        "limit": integer("Most results to return.", 10, minimum=1, maximum=50),
        "cursor": integer("Where to continue: the `next` of the previous result.", 0, minimum=0),
    }),
    read_only=True,
)
def search_footage(query="", look_for="", reference_file_id="", reference_start=None, reference_end=None, match="picture",
                   filters=None, file_ids=None, limit=10, cursor=0):
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
                            filters=filters or {}, limit=int(limit), offset=int(cursor))
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
        ready = {layer: bool(sha) and shelf.layer_ready(sha, layer, version=v) for layer, v in S.LAYER_VERSIONS.items()}
        na = [layer for layer in LAYER_ORDER if not ready[layer] and sha
              and (shelf.layer(sha, layer) or {}).get("status") == S.NOT_APPLICABLE]
        missing = [layer for layer in LAYER_ORDER if not ready[layer] and layer not in na]
        if only_incomplete and not missing:
            continue
        rows.append({"file_id": str(f.id), "name": _display_name(f.data), "media_type": kind,
                     "ready": [layer for layer in LAYER_ORDER if ready[layer]], "missing": missing, "not_applicable": na,
                     "searchable": ready["vectors"] or ready["structure"]})
    log.debug("index_status: %d files", len(rows))
    complete = sum(1 for r in rows if not r["missing"])
    return ok(f"{complete} of {len(rows)} files fully indexed", files=rows)


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
