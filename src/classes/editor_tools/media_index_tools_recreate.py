"""Recreate a reference video from the project's own footage: plan every shot, build it, check it against the reference, fix, repeat.

A recreation done by eye went wrong in four ways (found on a real run): 85 shots were placed by length alone so six clips were reused
about forty times, nothing was graded shot by shot, the stock footage the prompt allowed could not be found, and nothing measured the
result, so the agent could not know it had failed. These three tools give it the loop a coder has: `plan_recreation_tool` chooses a
source window for every shot at once (and a stock suggestion where nothing fits), `build_recreation_tool` places the plan at the
reference's exact cut times with each clip graded towards the reference shot, and `check_recreation_tool` scores the timeline against
the reference and names the shots to fix and how.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from classes.editor_tools._base import ToolError, array, boolean, enum, integer, number, obj, ok, on_main, string, th
from classes.editor_tools._registry import REGISTRY, editor_tool
from classes.editor_tools.media_index_tools import _all_files, project_indexes
from classes.editor_tools.effects_color_looks import _grade_values
from classes.editor_tools.timeline_edit_common import frame_seconds, keyframe_value, snap
from classes.media_index import reference as mref

MAX_PLANS = 6
TRACK_NAME = "Recreation"
SONG_TRACK_NAME = "Song"
_PLANS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

# areas of the check, with how much each counts towards the overall score; the recreation passes when the overall score is at least
# PASS_SCORE and no area is below AREA_FLOOR
AREA_WEIGHT = {"timing": 0.25, "coverage": 0.15, "variety": 0.20, "picture": 0.15, "look": 0.15, "sound": 0.10}
PASS_SCORE, AREA_FLOOR = 80.0, 60.0
WEAK_RANK = 0.30                 # a placed window ranked in the bottom 30% of the candidates for its shot is a weak pick
LOOK_CLOSE = 0.25                # a clip matches the reference shot's look when the gap in brightness, colour cast, saturation and contrast is under this
BATCH_HOP_TIMEOUT = 180
BUILD_LEASE_SECONDS = 900.0


def _mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60)}:{seconds % 60:05.2f}"


def _call(tool: str, /, **kwargs) -> Dict[str, Any]:
    """Run another editor tool and return the JSON it answers with ({} when it only answers a sentence). Raises ToolError on an error."""
    out = REGISTRY[tool].func(**kwargs)
    if str(out).startswith("Error"):
        raise ToolError(str(out)[7:] if str(out).startswith("Error: ") else str(out))
    head, _, rest = str(out).partition("\n")
    try:
        return json.loads(rest) if rest else {"summary": head}
    except ValueError:
        return {"summary": head}


def _remember(plan: Dict[str, Any]) -> str:
    plan["id"] = plan.get("id") or "R" + format(int(time.time() * 1000) % (36 ** 5), "x")
    _PLANS[plan["id"]] = plan
    _PLANS.move_to_end(plan["id"])
    while len(_PLANS) > MAX_PLANS:
        _PLANS.popitem(last=False)
    return plan["id"]


def _plan(plan_id: str, reference_file_id: str = "") -> Optional[Dict[str, Any]]:
    if plan_id:
        if plan_id not in _PLANS:
            raise ToolError(f"no plan {plan_id!r} in this session (plan_recreation_tool makes one)")
        return _PLANS[plan_id]
    for plan in reversed(list(_PLANS.values())):
        if not reference_file_id or plan["reference_file_id"] == str(reference_file_id):
            return plan
    return None


def _effective(row: Dict[str, Any], plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The window that plays this shot: a stock or fix override if there is one, else the planned choice."""
    return plan["overrides"].get(row["reference_shot"]) or row.get("choice")


def _reference(files, reference_file_id: str):
    ref = next((f for f in files if f.file_id == str(reference_file_id)), None)
    if ref is None:
        raise ToolError(f"reference file {reference_file_id!r} has no saved index yet; index_status_tool shows progress")
    return ref


def _row_line(row: Dict[str, Any], plan: Dict[str, Any]) -> str:
    c = _effective(row, plan)
    head = f"S{row['reference_shot']} {_mmss(row['start'])} ({row['duration']:.1f}s)"
    if c is None:
        line = f"{head} <- NOTHING FITS"
    else:
        tag = " stock" if row["reference_shot"] in plan["overrides"] and plan["overrides"][row["reference_shot"]].get("stock") else ""
        line = f"{head} <- {c['name'][:28]} {c['in']:.2f}-{c['out']:.2f}{tag}" + (f" score {c['score']:.2f}" if "score" in c else "")
    if row["status"] in ("weak", "no_match", "short_only") and row["reference_shot"] not in plan["overrides"]:
        line += f" [{row['status']}]"
        stock = (row.get("stock") or [])[:1]
        if stock:
            line += f" stock: id {stock[0]['video_id']} ({stock[0]['seconds']:g}s) '{row.get('stock_query', '')[:40]}'"
        elif row.get("stock_query"):
            line += f" search stock: '{row['stock_query'][:48]}'"
    return line


# ============================ plan ============================
@editor_tool(
    "plan_recreation_tool",
    covers=("index.match",),
    label="Plan a recreation",
    schema=obj({
        "reference_file_id": string("The video to recreate (a project file with a saved index)."),
        "source_file_ids": array({"type": "string"}, "Only use these project files as footage (default: every indexed video except the reference).", []),
        "max_reuse": integer("Most times the same moment of a source clip may be used.", 2, minimum=1, maximum=4),
        "stock": enum(["auto", "off"], "auto: for shots nothing fits, look for stock clips to suggest (needs sign-in); off: do not.", "auto"),
        "max_stock_searches": integer("Most stock searches to make, for the shots that fit worst.", 10, minimum=0, maximum=25),
    }, required=["reference_file_id"]),
    read_only=True,
)
def plan_recreation(reference_file_id, source_file_ids=[], max_reuse=2, stock="auto", max_stock_searches=10):
    """Plan a recreation of a reference video, every shot at once, from the project's own footage. For each reference shot it chooses a
    window of your clips (in/out seconds of exactly the shot's length) by how its picture, measured look and camera move compare, and
    hands windows out under limits: the same moment is not reused beyond max_reuse, no clip plays twice in a row, no clip takes more than
    its share. Shots nothing fits are marked weak or unfilled with a stock-search phrase and, when signed in, stock clips to try (bring one
    in with import_stock_media_tool and give it back to build_recreation_tool as an override). Returns plan_id. Nothing is placed yet:
    read the plan, then call build_recreation_tool, then check_recreation_tool and fix what it names.
    """
    from classes.editor_tools.ai_generation_stock import search_stock_for
    files, missing = project_indexes()
    ref = _reference(files, reference_file_id)
    wanted = {str(x) for x in source_file_ids} if source_file_ids else None
    pool = [f for f in files if f.sha != ref.sha and f.shots and (wanted is None or f.file_id in wanted)]
    if not pool:
        raise ToolError("no other indexed video footage to cut from; import and index the clips first (index_status_tool)")
    out = mref.plan_recreation(ref, pool, max_reuse=int(max_reuse))
    if not out["total"]:
        raise ToolError("the reference has no analysed shots to recreate")
    rows = out["shots"]
    stock_ok = None
    if stock == "auto" and int(max_stock_searches) > 0:
        needy = sorted((r for r in rows if r["status"] != "matched"), key=lambda r: (r.get("choice") or {}).get("score", 0.0))
        for r in needy[:int(max_stock_searches)]:
            found = search_stock_for(r["stock_query"], min_seconds=r["duration"], count=2)
            if found is None:
                stock_ok = False
                break
            stock_ok = True
            r["stock"] = found
    plan = {"reference_file_id": str(reference_file_id), "reference_name": ref.name, "rows": rows, "reuse": out["reuse"], "overrides": {},
            "placed": {}, "song_clip": "", "track_name": TRACK_NAME, "pool_ids": [f.file_id for f in pool]}
    plan_id = _remember(plan)
    weak = [r["reference_shot"] for r in rows if r["status"] != "matched"]
    reuse = out["reuse"]
    head = (f"plan {plan_id}: {out['matched']} of {out['total']} shots matched, {out['weak']} weak, {out['unfilled']} unfilled. "
            f"{reuse['files_used']} clips used, most uses of one moment {reuse['most_uses_of_one_moment']}, "
            f"{reuse['same_file_back_to_back']} clips twice in a row.")
    notes = [_row_line(r, plan) for r in rows]
    return ok(head, plan_id=plan_id, notes="\n".join(notes), matched=out["matched"], weak_shots=weak, unfilled=out["unfilled"], reuse=reuse,
              stock_searched=stock_ok, not_indexed=missing[:20],
              next="build_recreation_tool(plan_id) places it; then check_recreation_tool. Weak or unfilled shots: use a stock suggestion or accept a weak match.")


# ============================ build ============================
def _frame_grade(ref_look: Optional[Dict[str, Any]], src_look: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """Colour-grade values that move a source shot's measured look towards the reference shot's (quantised so clips can share a call)."""
    if not ref_look or not src_look or not ref_look.get("present") or not src_look.get("present"):
        return {}
    clamp = lambda v, lo, hi: max(lo, min(hi, v))  # noqa: E731
    luma_r, luma_s = max(0.03, float(ref_look.get("avg_luma") or 0.03)), max(0.03, float(src_look.get("avg_luma") or 0.03))
    sat_r, sat_s = max(0.02, float(ref_look.get("sat_proxy") or 0.02)), max(0.02, float(src_look.get("sat_proxy") or 0.02))
    q = lambda v, step: round(round(v / step) * step, 3)  # noqa: E731
    out = {"exposure": q(clamp(0.8 * math.log2(luma_r / luma_s), -1.5, 1.5), 0.1),
           "saturation": q(clamp(sat_r / sat_s, 0.3, 2.5), 0.1),
           "temperature": q(clamp(2.0 * (float(ref_look.get("warm_cool") or 0.0) - float(src_look.get("warm_cool") or 0.0)), -0.6, 0.6), 0.05),
           "contrast": q(clamp(1.5 * (float(ref_look.get("contrast_span") or 0.0) - float(src_look.get("contrast_span") or 0.0)), -0.5, 0.5), 0.1)}
    return {k: v for k, v in out.items() if abs(v - (1.0 if k == "saturation" else 0.0)) > 1e-6}


def _source_look(files_by_id: Dict[str, Any], choice: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    fi = files_by_id.get(str(choice.get("file_id")))
    if fi is None:
        return None
    mid = (float(choice["in"]) + float(choice["out"])) / 2.0
    shot = fi.shot_at(mid)
    return (shot or {}).get("look")


def _inside_file(file_id: str, start: float, length: float) -> float:
    """*start* moved earlier if needed so a cut of *length* ends inside the file as the editor sees it (the index can run a frame or two past it)."""
    from classes.clip_placement import source_window_for_file
    from classes.query import File
    f = File.get(id=file_id)
    if f is None:
        return start
    lo, hi = source_window_for_file(f.data)
    return max(lo, min(start, hi - length))


def _aligned_window(file_id: str, start: float, length: float) -> float:
    """Where a cut of *length* (a whole number of frames) should start in the file: on the frame grid and ending inside the file as the editor sees it.

    Snapping a window's start and end separately can make it a frame longer than its slot, which overlaps its neighbour; a start on the grid
    plus a length that is a whole number of frames cannot.
    """
    from classes.clip_placement import source_window_for_file
    from classes.query import File
    f = File.get(id=file_id)
    lo, hi = source_window_for_file(f.data) if f is not None else (0.0, 1e9)
    if length > hi - lo + 1e-6:
        raise ToolError(f"{str((f.data or {}).get('name') or file_id)[:40]} is only {hi - lo:.2f} s long, too short for a {length:.2f} s shot")
    frame = frame_seconds()
    st = snap(max(lo, min(start, hi - length)))
    while st + length > hi + 1e-6 and st - frame >= lo - 1e-6:
        st = snap(st - frame)
    return st


def _snapshot(layer: int, spans: Optional[List[Tuple[float, float]]] = None) -> List[Dict[str, Any]]:
    """The clips about to be cleared from the track (all of them, or those in the spans), so a failed build can put them back."""
    from classes import audio_mix as am
    from classes.query import Clip
    rows = []
    for c in Clip.filter():
        data = c.data or {}
        if not layer or int(data.get("layer") or 0) != layer:
            continue
        start, end = am.clip_timeline_extent(data)
        if spans is None or any(start < hi - 1e-6 and end > lo + 1e-6 for lo, hi in spans):
            rows.append({"file_id": str(data.get("file_id") or ""), "position": float(start), "in": float(data.get("start") or 0.0), "out": float(data.get("end") or 0.0)})
    return sorted(rows, key=lambda r: r["position"])


def _restore(rows: List[Dict[str, Any]], track: str) -> None:
    """Put cleared clips back where they were (best effort: their grades are not restored)."""
    runs: List[List[Dict[str, Any]]] = []
    for r in rows:
        if runs and abs(r["position"] - (runs[-1][-1]["position"] + runs[-1][-1]["out"] - runs[-1][-1]["in"])) < 0.02:
            runs[-1].append(r)
        else:
            runs.append([r])
    for run in runs:
        try:
            _call("add_clips_to_timeline_tool", items=[{"file_id": r["file_id"], "source_start": r["in"], "source_end": r["out"]} for r in run],
                  track=track, start_seconds=run[0]["position"], allow_overlap=True)
        except ToolError:
            continue


def _in_one_batch(fn):
    """Run a build as one batch: the timeline and preview update once at the end, not after every tool call it makes (about 25 colour-grade
    calls alone cost over two minutes when each refreshed the timeline). The batch is closed even when the build fails."""
    from classes import update_batch
    on_main(update_batch.begin, BUILD_LEASE_SECONDS, timeout=BATCH_HOP_TIMEOUT, batch=False)
    try:
        return fn()
    finally:
        on_main(update_batch.end, timeout=BATCH_HOP_TIMEOUT, batch=False)


def _layer_of(name: str) -> int:
    from classes.query import Track
    return next((int((t.data or {}).get("number") or 0) for t in Track.filter() if str((t.data or {}).get("label") or "") == name), 0)


def _clip_rows_on(layer: int) -> List[Tuple[str, float, float]]:
    from classes.query import Clip
    from classes import audio_mix as am
    rows = []
    for c in Clip.filter():
        data = c.data or {}
        if layer and int(data.get("layer") or 0) == layer:
            start, end = am.clip_timeline_extent(data)
            rows.append((str(c.id), float(start), float(end)))
    return rows


def _clear_track(name: str, layer: int) -> int:
    """Remove every clip on the named track; returns how many were removed."""
    count = len(_clip_rows_on(layer))
    if count:
        gone = th().delete_clips_on_track(track=name)
        if str(gone).startswith("Error"):
            raise ToolError(f"could not clear the {name} track: {gone}")
    return count


def _clear_slots(layer: int, spans: List[Tuple[float, float]]) -> int:
    """Remove the clips on the track that sit in any of these time spans (a rebuild replaces its own slots)."""
    ids = [cid for cid, a, b in _clip_rows_on(layer) if any(a < hi - 1e-6 and b > lo + 1e-6 for lo, hi in spans)]
    for cid in ids:
        th().delete_from_timeline(timeline_clip_id=cid)
    return len(ids)


def _ensure_track(name: str) -> None:
    from classes.query import Track
    if not any(str((t.data or {}).get("label") or "") == name for t in Track.filter()):
        _call("add_track_tool", position="top", name=name)


@editor_tool(
    "build_recreation_tool",
    covers=("index.match",),
    label="Build a recreation",
    schema=obj({
        "plan_id": string("The plan from plan_recreation_tool (default: the latest one)."),
        "only_shots": array({"type": "integer"}, "Rebuild just these reference shots (their old clips are replaced). Empty = every shot.", []),
        "overrides": array(obj({
            "reference_shot": integer("The reference shot to change.", minimum=0),
            "file_id": string("The project file to use for it (a stock clip you imported, or another of your clips)."),
            "in": number("Start second in that file.", minimum=0),
            "out": number("End second in that file (leave 0 to use the shot's length from `in`).", 0.0, minimum=0),
        }, required=["reference_shot", "file_id", "in"]), "Swap the source of some shots (from a check's fixes, or stock you imported); remembered for later builds.", []),
        "grade": boolean("Grade every clip towards its reference shot's measured look (brightness, colour, contrast).", True),
        "song": boolean("Put the reference's own audio on a Song track (its picture hidden) and silence the footage's own sound.", True),
    }),
    background_safe=True,
)
def build_recreation(plan_id="", only_shots=[], overrides=[], grade=True, song=True):
    """Place a recreation plan on the timeline: each source window at the reference's exact cut time and length, on a Recreation track,
    in one undoable step. Each clip is graded towards its reference shot's measured look (grade), the reference's audio goes under it
    on a Song track and the footage's own sound is switched off (song). With overrides it swaps the source of the named shots; with
    only_shots it rebuilds just those, replacing their clips. Does not do the reference's fades and dissolves (check_recreation_tool
    lists them). Afterwards call check_recreation_tool and fix what it names.
    """
    plan = _plan(plan_id)
    if plan is None:
        raise ToolError("no plan yet: call plan_recreation_tool first")
    for item in overrides or []:
        shot = int(item["reference_shot"])
        row = next((r for r in plan["rows"] if r["reference_shot"] == shot), None)
        if row is None:
            raise ToolError(f"the plan has no reference shot {shot}")
        out = float(item.get("out") or 0.0) or float(item["in"]) + row["duration"]
        name = next((str(f.data.get("name") or f.id) for f in _all_files() if str(f.id) == str(item["file_id"])), str(item["file_id"]))
        plan["overrides"][shot] = {"file_id": str(item["file_id"]), "name": name, "in": float(item["in"]), "out": out, "stock": True}
    wanted = {int(s) for s in only_shots} if only_shots else None
    todo = [r for r in plan["rows"] if _effective(r, plan) and (wanted is None or r["reference_shot"] in wanted)]
    if not todo:
        raise ToolError("nothing to place: every shot in that selection is unfilled (use overrides or a stock clip)")
    def run():
        files, _missing = project_indexes()
        by_id = {f.file_id: f for f in files}
        frame = frame_seconds()

        # check every window first, so a build that cannot work stops before it touches the timeline
        slot_len = {r["reference_shot"]: snap(r["end"]) - snap(r["start"]) for r in todo}
        starts = {r["reference_shot"]: _aligned_window(_effective(r, plan)["file_id"], float(_effective(r, plan)["in"]), slot_len[r["reference_shot"]]) for r in todo}

        _ensure_track(plan["track_name"])
        layer = _layer_of(plan["track_name"])
        spans = None if wanted is None else [(snap(r["start"]), snap(r["end"])) for r in todo]
        snapshot = _snapshot(layer, spans)
        if wanted is None:
            replaced = _clear_track(plan["track_name"], layer)           # a full build starts from an empty Recreation track (it is ours by name)
            plan["placed"] = {}
        else:
            replaced = _clear_slots(layer, spans or [])                  # a rebuild replaces just those shots, even if the plan was lost

        # place in contiguous runs; each clip's length is the difference of the snapped slot boundaries, so rounding never drifts
        runs: List[List[Dict[str, Any]]] = []
        for r in todo:
            if runs and abs(snap(r["start"]) - snap(runs[-1][-1]["end"])) < frame / 2:
                runs[-1].append(r)
            else:
                runs.append([r])
        placed_ids: Dict[int, str] = {}
        try:
            for run in runs:
                items = [{"file_id": _effective(r, plan)["file_id"], "source_start": round(starts[r["reference_shot"]], 3),
                          "source_end": round(starts[r["reference_shot"]] + slot_len[r["reference_shot"]], 3)} for r in run]
                data = _call("add_clips_to_timeline_tool", items=items, track=plan["track_name"], start_seconds=snap(run[0]["start"]), allow_overlap=False)
                for r, clip in zip(run, data.get("clips") or []):
                    placed_ids[r["reference_shot"]] = str(clip["timeline_clip_id"])
        except Exception:
            for clip_id in placed_ids.values():                            # a build that fails puts the timeline back as it was
                th().delete_from_timeline(timeline_clip_id=clip_id)
            _restore(snapshot, plan["track_name"])
            raise
        plan["placed"].update(placed_ids)

        graded = 0
        if grade:
            groups: Dict[Tuple, List[str]] = {}
            for r in todo:
                values = _frame_grade(r.get("look"), _source_look(by_id, _effective(r, plan)))
                if values and r["reference_shot"] in placed_ids:
                    groups.setdefault(tuple(sorted(values.items())), []).append(placed_ids[r["reference_shot"]])
            for params, ids in groups.items():
                _call("color_grade_clip_tool", timeline_clip_ids=ids, reset=True, **dict(params))
                graded += len(ids)

        song_note = ""
        if song and not plan["song_clip"]:
            ref = by_id.get(plan["reference_file_id"])
            if ref is not None:
                try:
                    _ensure_track(SONG_TRACK_NAME)
                    wanted_end = max(float(r["end"]) for r in plan["rows"])
                    usable_end = _inside_file(plan["reference_file_id"], wanted_end, 0.0)        # the editor's usable end of the file, which the index can overrun
                    added = _call("add_clips_to_timeline_tool", items=[{"file_id": plan["reference_file_id"], "source_start": 0.0, "source_end": round(min(wanted_end, usable_end), 3)}],
                                  track=SONG_TRACK_NAME, start_seconds=0.0, allow_overlap=True)
                    plan["song_clip"] = str((added.get("clips") or [{}])[0].get("timeline_clip_id") or "")
                    if plan["song_clip"]:
                        _call("set_clip_audio_video_tool", timeline_clip_ids=[plan["song_clip"]], video="off")
                        song_note = f" Song: the reference's audio on track '{SONG_TRACK_NAME}' (picture off)."
                except ToolError as exc:                                  # the clips are the valuable part: say what failed, keep them
                    song_note = f" The song was NOT added ({exc}); add the reference's audio yourself."
        if song and placed_ids:
            _call("set_clip_audio_video_tool", timeline_clip_ids=list(placed_ids.values()), audio="off")

        return placed_ids, replaced, graded, song_note

    placed_ids, replaced, graded, song_note = _in_one_batch(run)

    unfilled = [r["reference_shot"] for r in plan["rows"] if not _effective(r, plan)]
    return ok(f"Placed {len(placed_ids)} shot(s) on track '{plan['track_name']}' (replaced {replaced}), graded {graded}.{song_note}"
              + (f" Unfilled: {unfilled}." if unfilled else ""),
              plan_id=plan["id"], placed=len(placed_ids), replaced=replaced, graded=graded, unfilled=unfilled,
              next="check_recreation_tool(plan_id) scores it against the reference; fix the shots it names, then check again.")


# ============================ check ============================
def _look_gap(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    """How far two looks are apart in what a colour grade controls (brightness, colour cast, saturation, contrast); 0 = the same."""
    parts = [abs(float(a[k]) - float(b[k])) * w for k, w in (("avg_luma", 1.2), ("warm_cool", 1.4), ("sat_proxy", 1.1), ("contrast_span", 1.0))
             if a.get(k) is not None and b.get(k) is not None]
    return round(sum(parts), 3) if parts else None


def _applied_grade(clip_id: str) -> Optional[Dict[str, float]]:
    """The Color Grade values a timeline clip carries, or None when it has none."""
    from classes.query import Clip
    clip = Clip.get(id=clip_id)
    for effect in ((clip.data or {}).get("effects") or []) if clip is not None else []:
        if effect.get("class_name") == "ColorGrade":
            return _grade_values(effect)
    return None


def _is_muted(clip_id: str) -> bool:
    """True when the clip's own sound is switched off (set_clip_audio_video audio='off' stores has_audio as 0)."""
    from classes.query import Clip
    clip = Clip.get(id=clip_id)
    return clip is not None and keyframe_value((clip.data or {}).get("has_audio")) == 0.0


def _predicted_look(src_look: Optional[Dict[str, Any]], grade: Optional[Dict[str, float]]) -> Optional[Dict[str, Any]]:
    """The look a source shot should have once a grade is applied, using the inverse of the mapping build_recreation_tool grades with."""
    if not src_look:
        return None
    out = dict(src_look)
    if grade:
        out["avg_luma"] = min(1.0, max(0.0, float(src_look.get("avg_luma") or 0.0) * 2.0 ** (float(grade.get("exposure") or 0.0) / 0.8)))
        out["sat_proxy"] = float(src_look.get("sat_proxy") or 0.0) * float(grade.get("saturation") or 0.0)
        out["warm_cool"] = float(src_look.get("warm_cool") or 0.0) + float(grade.get("temperature") or 0.0) / 2.0
        out["contrast_span"] = float(src_look.get("contrast_span") or 0.0) + float(grade.get("contrast") or 0.0) / 1.5
    return out


def _swap_fix(shot_id: int, scored: List[tuple], pool: List[Any], covering: Any, avoid: set, prefer: str, next_to: set = frozenset()) -> Optional[str]:
    """The build_recreation_tool call that swaps this shot's clip for the best other window ("look": the one whose look is closest; "any": the best overall)."""
    best, best_key = None, -1.0
    for fidx, w_in, _w_out, _src, score, parts in scored:
        f = pool[fidx]
        if f.file_id in next_to:
            continue                                                      # the same file as a neighbouring shot would be a back-to-back repeat
        if f.file_id == covering.file_id and abs(w_in - covering.src_in) < 0.5:
            continue                                                      # the moment it already uses
        if any((f.file_id, k) in avoid for k in range(int(w_in / mref.BUCKET_SECONDS), int((w_in + (covering.src_out - covering.src_in)) / mref.BUCKET_SECONDS) + 1)):
            continue                                                      # a moment already used too often
        key = (2.0 * parts["look"] + score) if prefer == "look" else score
        if key > best_key:
            best, best_key = (f.file_id, w_in), key
    if best is None:
        return None
    return f"build_recreation_tool(overrides=[{{reference_shot:{shot_id}, file_id:'{best[0]}', in:{best[1]:.2f}}}], only_shots=[{shot_id}])"


def _grade_score(parts: List[float]) -> float:
    return round(100.0 * sum(parts) / len(parts), 1) if parts else 0.0


@editor_tool(
    "check_recreation_tool",
    covers=("index.match",),
    label="Check a recreation",
    schema=obj({
        "reference_file_id": string("The video being recreated."),
        "plan_id": string("The plan (default: the latest for this reference); gives the swap candidates for weak shots.", ""),
        "tolerance_frames": integer("How far a cut may be from the reference's, in frames.", 1, minimum=0, maximum=12),
    }, required=["reference_file_id"]),
    read_only=True,
)
def check_recreation(reference_file_id, plan_id="", tolerance_frames=1):
    """Score the timeline against the reference it recreates, and say what to fix. Six areas, 0 to 100 each: timing (every cut and
    length against the reference's, in frames), coverage (a clip in every slot, no overlaps), variety (the same moment or clip not
    reused too often, no clip twice in a row), picture (how well each placed window ranks among the footage for its shot), look (each clip
    graded and close to the reference shot's measured look) and sound (the reference's audio present, the footage's own sound off).
    It passes at 80 overall with no area under 60. failing lists the shots to fix, each with what is wrong and the call that fixes it;
    transitions lists the reference's fades and dissolves, which build_recreation_tool does not make. Run it after every build and
    again after fixes; stop when it passes or three rounds change nothing.
    """
    from classes.editor_tools.media_index_tools_precision import _timeline_facts
    files, _missing = project_indexes()
    ref = _reference(files, reference_file_id)
    plan = _plan(plan_id, reference_file_id)
    pool = [f for f in files if f.sha != ref.sha and f.shots]
    by_id = {f.file_id: f for f in files}
    frame, tol = frame_seconds(), int(tolerance_frames)
    clips, _tracks, transitions, _opacity, _links = _timeline_facts()
    ref_clips = [c for c in clips if c.file_id == ref.file_id]
    pictures = [c for c in clips if c.kind in ("video", "image") and c.file_id != ref.file_id]
    if plan:
        from classes.query import Track
        layer_of = next((int((t.data or {}).get("number") or 0) for t in Track.filter() if str((t.data or {}).get("label") or "") == plan["track_name"]), 0)
        if layer_of:
            pictures = [c for c in pictures if c.layer == layer_of]
        slots = [(r["reference_shot"], r["start"], r["end"], r) for r in plan["rows"]]
    else:
        slots = [(s["id"], max(float(s["start"]), 0.0), float(s["end"]), None) for s in ref.shots
                 if float(s["end"]) - float(s["start"]) >= mref.MIN_REFERENCE_SHOT and not s.get("black")]
    if not slots:
        raise ToolError("nothing to check: the reference has no analysed shots")
    if not pictures:
        raise ToolError("no recreation clips on the timeline yet: build_recreation_tool places the plan")

    findings: Dict[int, List[str]] = {}
    fixes: Dict[int, str] = {}
    timing, coverage, picture, look_scores, graded = [], [], [], [], 0
    buckets: Counter = Counter()
    placed_files: List[str] = []
    slot_info: Dict[int, Tuple[List[tuple], Any]] = {}

    def neighbour_files(shot_id: int) -> set:
        order = [i for i, _a, _b, _r in slots]
        at = {i: next((c.file_id for c in pictures if c.start <= snap(a) + frame * 0.5 < c.end), None) for i, a, _b, _r in slots}
        k = order.index(shot_id)
        return {at[order[j]] for j in (k - 1, k + 1) if 0 <= j < len(order) and at[order[j]]}
    for shot_id, a, b, row in slots:
        covering = next((c for c in pictures if c.start <= snap(a) + frame * 0.5 and c.end > snap(a) + frame * 0.5), None)
        if covering is None:
            coverage.append(0.0)
            slot = snap(a)
            nearby = min((c for c in pictures if abs(c.start - slot) <= 1.5), key=lambda c: abs(c.start - slot), default=None)
            if nearby is not None:
                shift = nearby.start - slot
                findings.setdefault(shot_id, []).append(f"no clip at this slot; the clip starting at {_mmss(nearby.start)} is {abs(shift) / frame:.0f} frame(s) "
                                                        f"{'late' if shift > 0 else 'early'}")
                fixes[shot_id] = f"move_clips_tool(timeline_clip_ids=['{nearby.id}'], by_seconds={-shift:.3f})"
            else:
                findings.setdefault(shot_id, []).append("no clip at this slot")
                fixes[shot_id] = f"build_recreation_tool(only_shots=[{shot_id}])"
            continue
        coverage.append(1.0)
        placed_files.append(covering.file_id)
        start_err = abs(covering.start - snap(a)) / frame
        len_err = abs(covering.length - (snap(b) - snap(a))) / frame
        timing.append(1.0 if max(start_err, len_err) <= tol + 1e-6 else max(0.0, 1.0 - (max(start_err, len_err) - tol) / 12.0))
        if max(start_err, len_err) > tol + 1e-6:
            findings.setdefault(shot_id, []).append(f"cut {start_err:.0f} frame(s) off, length {len_err:.0f} frame(s) off")
        for k in range(int(covering.src_in / mref.BUCKET_SECONDS), int(max(covering.src_in, covering.src_out - 1e-6) / mref.BUCKET_SECONDS) + 1):
            buckets[(covering.file_id, k)] += 1
        cand = by_id.get(covering.file_id)
        shot = next((s for s in ref.shots if int(s["id"]) == int(shot_id)), None)
        if cand is not None and shot is not None and cand in pool:
            scored = mref.scored_windows(ref, shot, float(b) - float(a), pool)
            slot_info[shot_id] = (scored, covering)
            rank = mref.rank_of_window(scored, pool.index(cand), covering.src_in, covering.src_out)
            if rank is not None:
                picture.append(1.0 if rank >= WEAK_RANK else rank / WEAK_RANK)
                if rank < WEAK_RANK:
                    better = [x for x in sorted(scored, key=lambda x: -x[4]) if pool[x[0]].file_id != covering.file_id][:1]
                    findings.setdefault(shot_id, []).append(f"weak pick (ranks {int(rank * 100)}% among the footage)")
                    if better:
                        x = better[0]
                        fixes[shot_id] = (f"overrides=[{{reference_shot:{shot_id}, file_id:'{pool[x[0]].file_id}', in:{x[1]:.2f}}}], only_shots=[{shot_id}]")
            src_look = (cand.shot_at((covering.src_in + covering.src_out) / 2.0) or {}).get("look")
            ref_look = (shot or {}).get("look")
            grade = _applied_grade(covering.id)
            graded += 1 if grade else 0
            dist = None
            if ref_look and src_look:
                dist = _look_gap(_predicted_look(src_look, grade) or {}, ref_look)
            if dist is None:
                look_scores.append(0.5)
            else:
                look_scores.append(1.0 if dist < LOOK_CLOSE else max(0.0, 1.0 - (dist - LOOK_CLOSE) / 0.8))
                if dist >= LOOK_CLOSE:
                    findings.setdefault(shot_id, []).append(f"look is {dist:.2f} from the reference's" + (" even with its grade" if grade else " and it is not graded"))
                    # a grade can only move a look so far: when it already has one, the fix is a source whose own look is closer
                    swap = _swap_fix(shot_id, scored, pool, covering, set(), "look", neighbour_files(shot_id)) if grade else None
                    fixes.setdefault(shot_id, swap or "build_recreation_tool(grade=true, only_shots=[%d])" % shot_id)

    reuse_max = max(buckets.values(), default=0)
    uses = Counter(placed_files)
    share = max(uses.values(), default=0) / max(1, len(placed_files))
    back_to_back = sum(1 for x, y in zip(placed_files, placed_files[1:]) if x == y)
    variety_penalty = 0.0
    if reuse_max > 2:
        variety_penalty += min(0.5, 0.15 * (reuse_max - 2))
    if share > 0.25:
        variety_penalty += min(0.3, (share - 0.25) * 2.0)
    variety_penalty += min(0.3, back_to_back / max(1, len(placed_files)) * 2.0)
    overused = {key for key, n in buckets.items() if n > 2}
    if overused:
        seen: Counter = Counter()
        for shot_id, a, _b, _row in slots:
            c = next((c for c in pictures if c.start <= snap(a) + frame * 0.5 < c.end), None)
            if c is None:
                continue
            hot = [(c.file_id, k) for k in range(int(c.src_in / mref.BUCKET_SECONDS), int(max(c.src_in, c.src_out - 1e-6) / mref.BUCKET_SECONDS) + 1)
                   if (c.file_id, k) in overused]
            if not hot:
                continue
            seen[hot[0]] += 1
            if seen[hot[0]] > 2:                                       # the first two uses of a moment are fine; later ones should change
                name = by_id[c.file_id].name[:24] if c.file_id in by_id else c.file_id
                findings.setdefault(shot_id, []).append(f"same moment of {name} used {buckets[hot[0]]} times")
                info = slot_info.get(shot_id)
                swap = _swap_fix(shot_id, info[0], pool, info[1], overused, "any", neighbour_files(shot_id)) if info else None
                if swap:
                    fixes.setdefault(shot_id, swap)

    song_present = any(c.start <= 0.5 and c.length >= 0.8 * max(b for _i, _a, b, _r in slots) for c in ref_clips)
    audible = sum(1 for c in pictures if not _is_muted(c.id))
    sound = 1.0 if song_present else 0.4
    if audible:
        sound -= min(0.5, audible / max(1, len(pictures)))
    areas = {"timing": _grade_score(timing), "coverage": _grade_score(coverage), "variety": round(100.0 * max(0.0, 1.0 - variety_penalty), 1),
             "picture": _grade_score(picture) if picture else 50.0, "look": _grade_score(look_scores) if look_scores else 50.0, "sound": round(100.0 * max(0.0, sound), 1)}
    overall = round(sum(areas[k] * w for k, w in AREA_WEIGHT.items()), 1)
    passed = overall >= PASS_SCORE and min(areas.values()) >= AREA_FLOOR
    ref_transitions = sorted((s for s in ref.shots if (s.get("opens_with") or {}).get("kind") in ("fade", "dissolve")), key=lambda s: s["start"])
    failing = [{"reference_shot": sid, "time": _mmss(next(a for i, a, _b, _r in slots if i == sid)), "problems": probs, "fix": fixes.get(sid)}
               for sid, probs in sorted(findings.items())][:20]
    head = (f"{'PASS' if passed else 'NOT YET'}: overall {overall} (timing {areas['timing']}, coverage {areas['coverage']}, variety {areas['variety']}, "
            f"picture {areas['picture']}, look {areas['look']}, sound {areas['sound']}); {len(findings)} shot(s) to fix.")
    return ok(head, passed=passed, overall=overall, areas=areas, failing=failing, failing_total=len(findings),
              variety={"most_uses_of_one_moment": reuse_max, "clips_twice_in_a_row": back_to_back, "biggest_share_of_one_file": round(share, 2),
                       "files_used": len(uses)},
              graded=graded, slots=len(slots),
              transitions={"reference_fades_and_dissolves": len(ref_transitions), "timeline_transitions": len(transitions),
                           "at": [_mmss(s["start"]) + " " + str((s.get("opens_with") or {}).get("kind")) for s in ref_transitions[:12]]},
              next="Fix the shots in `failing` with build_recreation_tool (overrides/only_shots), then check again. Stop at PASS or after three rounds with no change.")
