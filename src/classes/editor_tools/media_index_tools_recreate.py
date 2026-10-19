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

from classes.editor_tools._base import ToolError, array, boolean, clip_frame_at, enum, get_app, integer, number, obj, ok, on_main, string, th
from classes.editor_tools._registry import REGISTRY, editor_tool
from classes.editor_tools.media_index_tools import _all_files, _index_for, describe_file_shots, project_indexes
from classes.editor_tools.effects_color_looks import _grade_values
from classes.editor_tools.timeline_edit_common import frame_seconds, keyframe_value, snap
from classes.logger import log
from classes.media_index import reference as mref
from classes.media_index.style import real_shots

MAX_PLANS = 6
TRACK_NAME = "Recreation"
SONG_TRACK_NAME = "Song"
_PLANS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
PLACED_KEY = "recreation_placed"     # project key: track name -> ids of the clips this tool placed there; a build removes these and nothing else


def _placed(track: str) -> List[str]:
    stored = get_app().project.get(PLACED_KEY) or {}
    return list(stored.get(track) or []) if isinstance(stored, dict) else []


def _set_placed(track: str, ids: List[str]) -> None:
    """Saved in the project (not undoable, like the edit brief), so a build after a restart still finds its own clips."""
    on_main(lambda: get_app().updates.update_untracked([PLACED_KEY], {track: list(ids)}))

# areas of the check, with how much each counts towards the overall score; the recreation passes when the overall score is at least
# PASS_SCORE and no area is below AREA_FLOOR
AREA_WEIGHT = {"timing": 0.22, "coverage": 0.13, "variety": 0.18, "picture": 0.15, "look": 0.12, "sound": 0.08, "framing": 0.12}
ZOOM_PER_STEP = 0.45             # extra zoom for each step of tightness the reference is tighter than the footage
MAX_ZOOM = 2.2                   # past this a 1080p source is visibly soft in a 720p-1080p project
MIN_ZOOM = 1.08                  # under this a zoom is not worth making
FRAMING_OK = 0.85                # a clip is framed as asked when its zoom reaches this share of the wanted zoom
PASS_SCORE, AREA_FLOOR = 80.0, 60.0
WEAK_RANK = 0.30                 # a placed window ranked in the bottom 30% of the candidates for its shot is a weak pick
LOOK_CLOSE = 0.25                # a clip matches the reference shot's look when the gap in brightness, colour cast, saturation and contrast is under this
SCENE_GAP = 0.30                 # neighbouring reference shots whose looks are this close belong to one scene and are graded alike
RAMP_MIN_STOPS = 0.15            # a within-shot brightness change smaller than this is not worth a ramp
RAMP_MAX_STOPS = 0.8
RAMP_MIN_SECONDS = 0.6
MAX_CUT_ERROR_FRAMES = 6         # one cut further off than this fails the check whatever the average says
TRANSITION_SECONDS = 0.5         # the longest dissolve or fade the build makes
TILE_HEIGHT = 144
TILE_WIDTH = 256
MAX_COMPARE_SHOTS = 8
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
        "max_reuse": integer("Most times the same moment of a source clip may be used. 1 (default): never, except where the reference itself repeats an image.", 1, minimum=1, maximum=3),
        "stock": enum(["auto", "off"], "auto: for shots nothing fits, look for stock clips to suggest (needs sign-in); off: do not.", "auto"),
        "max_stock_searches": integer("Most stock searches to make, for the shots that fit worst.", 10, minimum=0, maximum=25),
    }, required=["reference_file_id"]),
    read_only=True,
)
def plan_recreation(reference_file_id, source_file_ids=[], max_reuse=1, stock="auto", max_stock_searches=10):
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
    ref_file = next((f for f in _all_files() if str(f.id) == ref.file_id), None)
    if ref_file is not None and any(not s.get("watch") for s in ref.shots):
        # The reference's shot types and stock phrases come from its descriptions: made once, best effort.
        _out, why = describe_file_shots(ref_file, ref, [s["id"] for s in ref.shots])
        if why:
            log.info("plan_recreation: no descriptions for the reference: %s", why)
        else:
            ref = _index_for(ref_file) or ref
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
            "song_clip": "", "track_name": TRACK_NAME, "pool_ids": [f.file_id for f in pool]}
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


def _smooth_grades(rows: List[Dict[str, Any]], targets: Dict[int, Dict[str, float]]) -> Dict[int, Dict[str, float]]:
    """Blend each shot's grade with its neighbours' when the reference's own look barely changes between them, so cuts inside one scene do not
    jump from grade to grade; a real change of scene keeps its own grade. Values stay on the same steps so clips can still share a call."""
    q = lambda v, step: round(round(v / step) * step, 3)  # noqa: E731
    out: Dict[int, Dict[str, float]] = {}
    for k, r in enumerate(rows):
        own = targets.get(r["reference_shot"])
        if not own:
            continue
        near = []
        for j in (k - 1, k + 1):
            if 0 <= j < len(rows) and targets.get(rows[j]["reference_shot"]):
                gap = _look_gap(r.get("look") or {}, rows[j].get("look") or {})
                if gap is not None and gap < SCENE_GAP:
                    near.append(targets[rows[j]["reference_shot"]])
        blended = {}
        for key, value in own.items():
            values = [value] + [n.get(key, 1.0 if key == "saturation" else 0.0) for n in near]
            weights = [2.0] + [1.0] * len(near)
            step = {"exposure": 0.1, "saturation": 0.1, "temperature": 0.05, "contrast": 0.1}[key]
            blended[key] = q(sum(v * w for v, w in zip(values, weights)) / sum(weights), step)
        out[r["reference_shot"]] = blended
    return out


def _reference_luma_ramps(ref_file_id: str, rows: List[Dict[str, Any]]) -> Dict[int, float]:
    """{reference shot: change in brightness over the shot, in stops (end minus start)} for shots long enough to show one, measured from a
    few tiny frames of the reference itself. Empty when the reference cannot be read."""
    import numpy as np
    from classes.editor_tools.media_index_tools_precision import _audio_path
    from classes.media_index import contact
    from classes.media_index.probe import probe_media
    from classes.query import File
    f = File.get(id=str(ref_file_id))
    long_rows = [r for r in rows if r["duration"] >= RAMP_MIN_SECONDS]
    if f is None or not long_rows:
        return {}
    times = []
    for r in long_rows:
        pad = min(0.15, r["duration"] * 0.15)
        times += [r["start"] + pad, r["end"] - pad]
    try:
        path = _audio_path(f)
        probe = probe_media(path)
        frames, got = contact.decode_frames_at(path, times, (64, 36), start_time=float((probe or {}).get("start_time") or 0.0))
    except Exception:  # noqa: BLE001 - no ramps is a fine fallback
        return {}
    if len(got) != len(times):
        return {}
    luma = [max(0.02, float(frames[i].mean()) / 255.0) for i in range(len(times))]
    out: Dict[int, float] = {}
    for k, r in enumerate(long_rows):
        stops = 0.8 * float(np.log2(luma[2 * k + 1] / luma[2 * k]))
        if abs(stops) >= RAMP_MIN_STOPS:
            out[r["reference_shot"]] = max(-RAMP_MAX_STOPS, min(RAMP_MAX_STOPS, stops))
    return out


def _ramp_exposure(clip_id: str, stops: float, frames: int) -> bool:
    """Make a clip's Color Grade exposure move by *stops* over its length (centred on the exposure it has), as keyframes on the effect."""
    import copy
    from classes.editor_tools import clip_props_model as cpm
    from classes.editor_tools.effects_color import save_clip_effects
    from classes.query import Clip
    clip = Clip.get(id=clip_id)
    if clip is None or frames < 2:
        return False
    if _applied_grade(clip_id) is None:                                # a shot that needed no grade still needs the effect to ramp
        _call("color_grade_clip_tool", timeline_clip_ids=[clip_id], exposure=0.0, reset=True)
        clip = Clip.get(id=clip_id)
    data = clip.data or {}
    first = clip_frame_at(data, float(data.get("position") or 0.0))           # keyframes count from the source's first frame (clip_frame_at): start on the frame the clip shows
    effects = copy.deepcopy(data.get("effects") or [])
    for effect in effects:
        if effect.get("class_name") == "ColorGrade":
            base = keyframe_value(effect.get("exposure"), 0.0)
            effect["exposure"] = {"Points": [cpm.make_point(first, round(base - stops / 2.0, 3), cpm.BEZIER), cpm.make_point(first + frames - 1, round(base + stops / 2.0, 3), cpm.BEZIER)]}
            save_clip_effects([(clip_id, effects)])
            return True
    return False


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


def _snapshot(track: str, spans: Optional[List[Tuple[float, float]]] = None) -> List[Dict[str, Any]]:
    """The clips this tool placed on the track (all of them, or those in the spans): what a build clears, and what a failed build puts back."""
    from classes import audio_mix as am
    from classes.query import Clip
    rows = []
    for cid in _placed(track):
        c = Clip.get(id=cid)
        if c is None:
            continue
        data = c.data or {}
        start, end = am.clip_timeline_extent(data)
        if spans is None or any(start < hi - 1e-6 and end > lo + 1e-6 for lo, hi in spans):
            rows.append({"id": str(cid), "file_id": str(data.get("file_id") or ""), "position": float(start), "in": float(data.get("start") or 0.0), "out": float(data.get("end") or 0.0)})
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
            data = _call("add_clips_to_timeline_tool", items=[{"file_id": r["file_id"], "source_start": r["in"], "source_end": r["out"]} for r in run],
                         track=track, start_seconds=run[0]["position"], allow_overlap=True)
        except ToolError:
            continue
        _set_placed(track, _placed(track) + [str(c["timeline_clip_id"]) for c in data.get("clips") or []])


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


def _clear_placed(track: str, rows: List[Dict[str, Any]]) -> int:
    """Remove the clips this tool placed (the rows _snapshot picked) from the track and from its record; returns how many were removed."""
    for r in rows:
        gone = th().delete_from_timeline(timeline_clip_id=r["id"])
        if str(gone).startswith("Error"):
            raise ToolError(f"could not clear the {track} track: {gone}")
    gone_ids = {r["id"] for r in rows}
    _set_placed(track, [cid for cid in _placed(track) if cid not in gone_ids])
    return len(rows)


def _wanted_zoom(ref_type: Optional[str], src_type: Optional[str]) -> float:
    """The zoom (1 = none) that makes footage framed as *src_type* read as *ref_type*: only ever in (a wide shot cannot be pulled back)."""
    want, have = mref.tightness(ref_type), mref.tightness(src_type)
    if want is None or have is None or want <= have:
        return 1.0
    zoom = min(MAX_ZOOM, 1.0 + ZOOM_PER_STEP * (want - have))
    return zoom if zoom >= MIN_ZOOM else 1.0


def _source_type_at(by_id: Dict[str, Any], file_id: str, t: float) -> Optional[str]:
    fi = by_id.get(file_id)
    shot = fi.shot_at(t) if fi is not None else None
    return ((shot or {}).get("watch") or {}).get("shot_type")


def _subject_zoom_values(clip_id: str, file_id: str, start: float, end: float, zoom: float) -> Optional[Dict[str, Any]]:
    """Clip properties that zoom into the footage and keep its subject (found by saliency and faces) in shot: crop-to-fill, scale, and the
    shift that puts the subject at the middle, kept inside the picture. None when the picture cannot be read."""
    from classes.editor_tools import clip_props_model as cpm
    from classes.editor_tools.media_index_tools_precision import _frame_aspect
    from classes.editor_tools.project_export_profiles import GRAVITY_CENTER, SCALE_CROP, _source_aspect
    from classes.media_index import framing
    from classes.path_utils import absolute_media_path
    from classes.query import Clip, File
    clip, f = Clip.get(id=clip_id), File.get(id=file_id)
    if clip is None or f is None:
        return None
    path = absolute_media_path(f.data.get("path")) or str(f.data.get("path") or "")
    try:
        found = framing.framing_of(path, start, end, 1.0 / zoom, "x", samples=3)
        xs = sorted(r["x"] for r in found["samples"])
        ys = sorted(r["y"] for r in found["samples"])
        sx, sy = xs[len(xs) // 2], ys[len(ys) // 2]
    except Exception:  # noqa: BLE001 - an unreadable frame means a centred zoom, not a failed build
        sx = sy = 0.5
        found = {"confidence": 0.0, "sure": False}
    source_aspect, frame_aspect = _source_aspect(clip.data or {}) or 0.0, _frame_aspect("project")
    vis_x = min(1.0, frame_aspect / source_aspect) if source_aspect else 1.0           # share of the source's width / height left after crop-to-fill
    vis_y = min(1.0, source_aspect / frame_aspect) if source_aspect else 1.0
    fx = 0.5 + (sx - 0.5) / vis_x
    fy = 0.5 + (sy - 0.5) / vis_y
    reach = (zoom - 1.0) / 2.0
    shift_x = max(-reach, min(reach, -(fx - 0.5) * zoom))
    shift_y = max(-reach, min(reach, -(fy - 0.5) * zoom))

    def curve(value: float) -> Dict[str, Any]:
        return {"Points": [cpm.make_point(1, round(float(value), 4), cpm.BEZIER)]}

    return {"values": {"scale": SCALE_CROP, "gravity": GRAVITY_CENTER, "scale_x": curve(zoom), "scale_y": curve(zoom),
                       "location_x": curve(shift_x), "location_y": curve(shift_y)},
            "zoom": round(zoom, 2), "subject": [round(sx, 3), round(sy, 3)], "sure": bool(found.get("sure"))}


def _applied_zoom(clip_id: str) -> float:
    from classes.query import Clip
    clip = Clip.get(id=clip_id)
    return keyframe_value((clip.data or {}).get("scale_x") if clip else None, 1.0)


def _fade_alpha(clip_id: str, frames: int, direction: str, total_frames: int) -> None:
    """Fade a clip's picture in from black or out to black over *frames*, as opacity keyframes (kept alongside any fade it already has)."""
    from classes.editor_tools import clip_props, clip_props_model as cpm
    from classes.query import Clip
    clip = Clip.get(id=clip_id)
    if clip is None or total_frames < 3:
        return
    frames = max(1, min(frames, total_frames - 1))
    first = clip_frame_at(clip.data or {}, float((clip.data or {}).get("position") or 0.0))   # keyframes count from the source's first frame: the fade sits on the frames the clip shows
    last = first + total_frames - 1
    existing = {int(float(p["co"]["X"])): float(p["co"]["Y"]) for p in ((clip.data or {}).get("alpha") or {}).get("Points", []) if len(((clip.data or {}).get("alpha") or {}).get("Points", [])) > 1}
    points = dict(existing) if existing else {first: 1.0, last: 1.0}
    if direction == "in":
        points.update({first: 0.0, first + frames: 1.0})
    else:
        points.update({last - frames: 1.0, last: 0.0})

    def apply() -> None:
        with clip_props._transaction():
            clip_props.save_clip_values(clip_id, {"alpha": {"Points": [cpm.make_point(x, y, cpm.BEZIER) for x, y in sorted(points.items())]}})

    on_main(apply, timeout=BATCH_HOP_TIMEOUT)


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
        "transitions": boolean("Make the reference's dissolves (crossfades) and fades (through black) at the same cuts, kept to the reference's cut times.", True),
        "ramps": boolean("Let each clip's brightness follow the reference shot's own change over its length (a fade up, a flare), as keyframes, and grade the shots of one scene alike.", True),
        "framing": boolean("Zoom into footage that is framed wider than the reference shot (a close-up made from a wide shot), keeping the subject centred.", True),
    }),
    background_safe=True,
)
def build_recreation(plan_id="", only_shots=[], overrides=[], grade=True, song=True, framing=True, ramps=True, transitions=True):
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
        spans = None if wanted is None else [(snap(r["start"]), snap(r["end"])) for r in todo]
        snapshot = _snapshot(plan["track_name"], spans)
        replaced = _clear_placed(plan["track_name"], snapshot)           # a rebuild replaces the clips this tool placed, even if the plan was lost; the user's own clips stay

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
        _set_placed(plan["track_name"], _placed(plan["track_name"]) + list(placed_ids.values()))

        graded = 0
        ramped = 0
        if grade:
            targets = {r["reference_shot"]: _frame_grade(r.get("look"), _source_look(by_id, _effective(r, plan))) for r in todo}
            targets = _smooth_grades(todo, targets)
            groups: Dict[Tuple, List[str]] = {}
            for r in todo:
                values = targets.get(r["reference_shot"])
                if values and r["reference_shot"] in placed_ids:
                    groups.setdefault(tuple(sorted(values.items())), []).append(placed_ids[r["reference_shot"]])
            for params, ids in groups.items():
                _call("color_grade_clip_tool", timeline_clip_ids=ids, reset=True, **dict(params))
                graded += len(ids)
            if ramps:
                steps = _reference_luma_ramps(plan["reference_file_id"], [r for r in todo if r["reference_shot"] in placed_ids])
                fps_now = 1.0 / frame
                for shot_id, stops in steps.items():
                    if _ramp_exposure(placed_ids[shot_id], stops, int(round(slot_len[shot_id] * fps_now))):
                        ramped += 1

        zoomed = []
        if framing:
            from classes.editor_tools import clip_props
            pending = []
            for r in todo:
                shot_id = r["reference_shot"]
                c = _effective(r, plan)
                zoom = _wanted_zoom(r.get("shot_type"), _source_type_at(by_id, c["file_id"], float(c["in"]) + slot_len[shot_id] / 2.0))
                if zoom > 1.0 and shot_id in placed_ids:
                    values = _subject_zoom_values(placed_ids[shot_id], c["file_id"], starts[shot_id], starts[shot_id] + slot_len[shot_id], zoom)
                    if values:
                        pending.append((placed_ids[shot_id], values["values"]))
                        zoomed.append({"reference_shot": shot_id, "zoom": values["zoom"], "subject": values["subject"], "sure": values["sure"]})

            def apply_framing() -> None:
                with clip_props._transaction():
                    for clip_id, values in pending:
                        clip_props.save_clip_values(clip_id, values)
            if pending:
                on_main(apply_framing, timeout=BATCH_HOP_TIMEOUT)
        plan["zoomed"] = {z["reference_shot"]: z for z in zoomed} if wanted is None else {**plan.get("zoomed", {}), **{z["reference_shot"]: z for z in zoomed}}

        made_transitions, skipped_transitions = 0, 0
        if transitions:
            order = [r["reference_shot"] for r in plan["rows"]]
            before = dict(zip(order[1:], order))                    # the slot before each one (shot ids skip over split pieces)
            for r in todo:
                kind = r.get("opens_with")
                prev = before.get(r["reference_shot"])
                prev_id, cur_id = placed_ids.get(prev), placed_ids.get(r["reference_shot"])
                if kind not in ("dissolve", "fade") or not prev_id or not cur_id:
                    continue
                span = min(TRANSITION_SECONDS, 0.4 * min(slot_len[r["reference_shot"]], slot_len.get(prev, TRANSITION_SECONDS)))
                if span < 3 * frame:
                    skipped_transitions += 1
                    continue
                try:
                    if kind == "dissolve":
                        _call("add_transitions_between_clips_tool", timeline_clip_ids=[prev_id, cur_id], transition="fade", duration_seconds=round(span, 3),
                              method="handles", audio_crossfade=False, replace_existing=True)
                    else:
                        half = max(1, int(round(span / 2.0 / frame)))
                        _fade_alpha(prev_id, half, "out", int(round(slot_len[prev] / frame)))
                        _fade_alpha(cur_id, half, "in", int(round(slot_len[r["reference_shot"]] / frame)))
                    made_transitions += 1
                except ToolError as exc:
                    skipped_transitions += 1                                   # e.g. no spare footage around the cut for a crossfade
                    log.info("no transition at shot %s: %s", r["reference_shot"], exc)

        song_note = ""
        if song and not plan["song_clip"]:
            ref = by_id.get(plan["reference_file_id"])
            if ref is not None:
                try:
                    _ensure_track(SONG_TRACK_NAME)
                    _clear_placed(SONG_TRACK_NAME, _snapshot(SONG_TRACK_NAME))     # a new plan must not stack another copy of the song on the last one
                    wanted_end = max(float(r["end"]) for r in plan["rows"])
                    usable_end = _inside_file(plan["reference_file_id"], wanted_end, 0.0)        # the editor's usable end of the file, which the index can overrun
                    added = _call("add_clips_to_timeline_tool", items=[{"file_id": plan["reference_file_id"], "source_start": 0.0, "source_end": round(min(wanted_end, usable_end), 3)}],
                                  track=SONG_TRACK_NAME, start_seconds=0.0, allow_overlap=True)
                    plan["song_clip"] = str((added.get("clips") or [{}])[0].get("timeline_clip_id") or "")
                    if plan["song_clip"]:
                        _set_placed(SONG_TRACK_NAME, _placed(SONG_TRACK_NAME) + [plan["song_clip"]])
                        _call("set_clip_audio_video_tool", timeline_clip_ids=[plan["song_clip"]], video="off")
                        song_note = f" Song: the reference's audio on track '{SONG_TRACK_NAME}' (picture off)."
                except ToolError as exc:                                  # the clips are the valuable part: say what failed, keep them
                    song_note = f" The song was NOT added ({exc}); add the reference's audio yourself."
        if song and plan["song_clip"] and placed_ids:                # the footage goes silent only once the song is on the timeline
            _call("set_clip_audio_video_tool", timeline_clip_ids=list(placed_ids.values()), audio="off")

        return placed_ids, replaced, graded, song_note, zoomed, ramped, (made_transitions, skipped_transitions)

    placed_ids, replaced, graded, song_note, zoomed, ramped, (made_t, skipped_t) = _in_one_batch(run)

    unfilled = [r["reference_shot"] for r in plan["rows"] if not _effective(r, plan)]
    from classes.editor_tools import playback_health
    playback_note = playback_health.start_preview_copies({_effective(r, plan)["file_id"] for r in todo}, len(placed_ids))
    layer_now = _layer_of(plan["track_name"])
    playback_note += playback_health.stacked_tracks_note(layer_now, min(snap(r["start"]) for r in todo), max(snap(r["end"]) for r in todo))
    return ok(f"Placed {len(placed_ids)} shot(s) on track '{plan['track_name']}' (replaced {replaced}), graded {graded} ({ramped} with a brightness ramp), zoomed in on {len(zoomed)} to match the reference's framing, {made_t} dissolve/fade transition(s) ({skipped_t} skipped).{song_note}"
              + (f" Unfilled: {unfilled}." if unfilled else "") + playback_note,
              plan_id=plan["id"], placed=len(placed_ids), replaced=replaced, graded=graded, ramped=ramped, zoomed=zoomed, transitions=made_t, transitions_skipped=skipped_t, unfilled=unfilled,
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
            values = _grade_values(effect)
            points = (effect.get("exposure") or {}).get("Points") if isinstance(effect.get("exposure"), dict) else None
            if points and len(points) > 1:                              # a ramp: judge the look on its average
                values["exposure"] = round(sum(float(p["co"]["Y"]) for p in points) / len(points), 4)
            return values
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


def _moment_keys(file_id: str, start: float, length: float) -> List[Tuple[str, int]]:
    return [(file_id, k) for k in range(int(start / mref.BUCKET_SECONDS), int((start + length) / mref.BUCKET_SECONDS) + 1)]


def _swap_fix(shot_id: int, scored: List[tuple], pool: List[Any], covering: Any, moments: Counter, prefer: str, next_to: set,
              proposed: Dict[int, str]) -> Optional[str]:
    """The build_recreation_tool call that swaps this shot's clip for the best other window ("look": the one whose look is closest; "any": the
    best overall). It skips the moment already used, any moment already used, and the file of a neighbouring shot, and it books its own
    pick in *moments* and *proposed* so the next suggestion in the same pass sees it."""
    length = covering.src_out - covering.src_in
    best, best_key = None, -1.0
    for fidx, w_in, _w_out, _src, score, parts in scored:
        f = pool[fidx]
        if f.file_id in next_to:
            continue                                                      # the same file as a neighbouring shot would be a back-to-back repeat
        if f.file_id == covering.file_id and abs(w_in - covering.src_in) < 0.5:
            continue                                                      # the moment it already uses
        if any(moments[key] >= 1 for key in _moment_keys(f.file_id, w_in, length)):
            continue                                                      # footage already shown (the swap must be a fresh moment)
        key = (2.0 * parts["look"] + score) if prefer == "look" else score
        if key > best_key:
            best, best_key = (f.file_id, w_in), key
    if best is None:
        return None
    for key in _moment_keys(covering.file_id, covering.src_in, length):
        moments[key] -= 1
    for key in _moment_keys(best[0], best[1], length):
        moments[key] += 1
    proposed[shot_id] = best[0]
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
        "show_pictures": boolean(f"Attach a picture of the worst shots (reference frame beside the chosen footage, up to {MAX_COMPARE_SHOTS}) so they can be judged by eye.", True),
    }, required=["reference_file_id"]),
    read_only=True,
)
def check_recreation(reference_file_id, plan_id="", tolerance_frames=1, show_pictures=True):
    """Score the timeline against the reference it recreates, and say what to fix. Six areas, 0 to 100 each: timing (every cut and
    length against the reference's, in frames), coverage (a clip in every slot, no overlaps), variety (the same moment or clip not
    reused too often, no clip twice in a row), picture (how well each placed window ranks among the footage for its shot), look (each clip
    graded and close to the reference shot's measured look) and sound (the reference's audio present, the footage's own sound off).
    It passes at 80 overall with no area under 60. failing lists the shots to fix, each with what is wrong and the call that fixes it;
    transitions lists the reference's fades and dissolves (build_recreation_tool makes them). Run it after every build and
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
    skipped: List[int] = []
    if plan:
        from classes.query import Track
        layer_of = next((int((t.data or {}).get("number") or 0) for t in Track.filter() if str((t.data or {}).get("label") or "") == plan["track_name"]), 0)
        if layer_of:
            pictures = [c for c in pictures if c.layer == layer_of]
        slots = [(r["reference_shot"], r["start"], r["end"], r) for r in plan["rows"] if _effective(r, plan)]
        skipped = [r["reference_shot"] for r in plan["rows"] if not _effective(r, plan)]          # left out on purpose, and reported as such
    else:
        slots = [(s["id"], max(float(s["start"]), 0.0), float(s["end"]), None) for s in real_shots(ref.shots)
                 if float(s["end"]) - float(s["start"]) >= mref.MIN_REFERENCE_SHOT and not s.get("black")]
    if not slots:
        raise ToolError("nothing to check: the reference has no analysed shots")
    if not pictures:
        raise ToolError("no recreation clips on the timeline yet: build_recreation_tool places the plan")

    findings: Dict[int, List[str]] = {}
    fixes: Dict[int, str] = {}
    timing, coverage, picture, look_scores, graded = [], [], [], [], 0
    worst_cut = 0.0
    buckets: Counter = Counter()
    placed_files: List[str] = []
    slot_info: Dict[int, Tuple[List[tuple], Any]] = {}
    proposed: Dict[int, str] = {}
    look_misses: List[int] = []
    framing_scores: List[float] = []
    ranks: Dict[int, Optional[float]] = {}
    moments: Counter = Counter()                                    # filled once every slot is read; swaps book into it

    def clip_at(a: float, b: float) -> Any:
        """The picture clip playing the middle of a slot (the middle, not the start, so a dissolve's overlap cannot pick the neighbour)."""
        mid = (snap(a) + snap(b)) / 2.0
        return next((c for c in pictures if c.start <= mid < c.end), None)

    def neighbour_files(shot_id: int) -> set:
        order = [i for i, _a, _b, _r in slots]
        at = {i: proposed.get(i) or getattr(clip_at(a, b), "file_id", None) for i, a, b, _r in slots}
        k = order.index(shot_id)
        return {at[order[j]] for j in (k - 1, k + 1) if 0 <= j < len(order) and at[order[j]]}
    following = {i: r2 for (i, _a, _b, _r), (_i2, _a2, _b2, r2) in zip(slots, slots[1:])}     # the slot after each one (shot ids skip over split pieces)
    for shot_id, a, b, row in slots:
        covering = clip_at(a, b)
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
        next_row = following.get(shot_id)
        if (row or {}).get("opens_with") in ("dissolve", "fade") or (next_row or {}).get("opens_with") in ("dissolve", "fade"):   # a transition overlaps its cut by up to half its length each side
            start_err = max(0.0, start_err - TRANSITION_SECONDS / 2.0 / frame)
            len_err = max(0.0, len_err - TRANSITION_SECONDS / frame)
        timing.append(1.0 if max(start_err, len_err) <= tol + 1e-6 else max(0.0, 1.0 - (max(start_err, len_err) - tol) / 12.0))
        worst_cut = max(worst_cut, start_err, len_err)
        if max(start_err, len_err) > tol + 1e-6:
            shift = snap(a) - covering.start
            findings.setdefault(shot_id, []).append(f"cut {start_err:.0f} frame(s) {'late' if shift < 0 else 'early'}, length {len_err:.0f} frame(s) off")
            if len_err <= tol + 1e-6:                                       # right length, wrong place: slide it
                fixes[shot_id] = f"move_clips_tool(timeline_clip_ids=['{covering.id}'], by_seconds={shift:.3f})"
            else:
                fixes[shot_id] = f"build_recreation_tool(only_shots=[{shot_id}])"
        for k in range(int(covering.src_in / mref.BUCKET_SECONDS), int(max(covering.src_in, covering.src_out - 1e-6) / mref.BUCKET_SECONDS) + 1):
            buckets[(covering.file_id, k)] += 1
        cand = by_id.get(covering.file_id)
        shot = next((s for s in ref.shots if int(s["id"]) == int(shot_id)), None)
        ref_type = ((shot or {}).get("watch") or {}).get("shot_type")
        wanted_zoom = _wanted_zoom(ref_type, _source_type_at(by_id, covering.file_id, (covering.src_in + covering.src_out) / 2.0))
        if mref.tightness(ref_type) is not None:
            applied = _applied_zoom(covering.id)
            if wanted_zoom <= 1.0 or applied >= wanted_zoom * FRAMING_OK:
                framing_scores.append(1.0)
            else:
                framing_scores.append(max(0.0, (applied - 1.0) / max(wanted_zoom - 1.0, 1e-6)))
                findings.setdefault(shot_id, []).append(f"framed wider than the reference ({ref_type}): needs about {wanted_zoom:.1f}x zoom, has {applied:.1f}x")
                fixes.setdefault(shot_id, "build_recreation_tool(framing=true, only_shots=[%d])" % shot_id)
        if cand is not None and shot is not None and cand in pool:
            scored = mref.scored_windows(ref, shot, float(b) - float(a), pool)
            slot_info[shot_id] = (scored, covering)
            rank = mref.rank_of_window(scored, pool.index(cand), covering.src_in, covering.src_out)
            ranks[shot_id] = rank
            if rank is not None:
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
                    if grade:
                        look_misses.append(shot_id)                            # a grade can only move a look so far: the fix is a source whose own look is closer (below)
                    else:
                        fixes.setdefault(shot_id, "build_recreation_tool(grade=true, only_shots=[%d])" % shot_id)

    # picture: how alike each placed window is to its reference shot, measured against every shot/window pair (not just the shot's own options),
    # so a "best of a poor lot" cannot pass
    every_raw = [x[5]["raw_picture"] for sc, _c in slot_info.values() for x in sc if x[5].get("raw_picture") is not None]
    pool_at = {f.file_id: i for i, f in enumerate(pool)}
    row_of = {i: r for i, _a, _b, r in slots}
    best_raw = {sid: max((x[5]["raw_picture"] for x in sc if x[5].get("raw_picture") is not None), default=None) for sid, (sc, _c) in slot_info.items()}
    nothing_like = mref.unmatchable_shots({k: v for k, v in best_raw.items() if v is not None})
    for shot_id, (scored, covering) in slot_info.items():
        mine = [x[5]["raw_picture"] for x in scored if x[0] == pool_at.get(covering.file_id) and x[1] < covering.src_out - 1e-6
                and x[2] > covering.src_in + 1e-6 and x[5].get("raw_picture") is not None]
        fit = mref.picture_fit(every_raw, max(mine)) if mine else None
        if shot_id in nothing_like and (row_of.get(shot_id) or {}).get("repeat_of") is None:
            picture.append(0.15)
            findings.setdefault(shot_id, []).append(f"nothing in the footage looks like this shot (its best match trails the other shots' by {nothing_like[shot_id]:.2f}): "
                                                    "use stock, or leave it out and say so")
            desc = ((row_of.get(shot_id) or {}).get("description") or "").strip()
            if desc:
                query = mref.stock_query_for(desc, (next((x for x in ref.shots if int(x["id"]) == int(shot_id)), None) or {}).get("look"))
                fixes[shot_id] = f"search_stock_video_tool(query='{query}') then build_recreation_tool(overrides=[...], only_shots=[{shot_id}])"
            continue
        if fit is None:                                                # no picture vectors for this file: fall back to how it ranks on look and camera
            rank = ranks.get(shot_id)
            picture.append(0.5 if rank is None else (1.0 if rank >= WEAK_RANK else rank / WEAK_RANK))
        elif (row_of.get(shot_id) or {}).get("repeat_of") is not None or fit >= mref.FIT_MIN:
            picture.append(1.0)
        else:
            picture.append(max(0.0, fit / mref.FIT_MIN) ** 2)
            findings.setdefault(shot_id, []).append(f"does not look like the reference shot (picture match {fit:.2f}; needs {mref.FIT_MIN:.2f})")
            desc = ((row_of.get(shot_id) or {}).get("description") or "").strip()
            if desc:
                query = mref.stock_query_for(desc, (next((x for x in ref.shots if int(x["id"]) == int(shot_id)), None) or {}).get("look"))
                fixes.setdefault(shot_id, f"search_stock_video_tool(query='{query}') then build_recreation_tool(overrides=[...], only_shots=[{shot_id}])")

    reuse_max = max(buckets.values(), default=0)
    uses = Counter(placed_files)
    share = max(uses.values(), default=0) / max(1, len(placed_files))
    back_to_back = sum(1 for x, y in zip(placed_files, placed_files[1:]) if x == y)
    variety_penalty = 0.0
    repeat_slots = {i for i, _a, _b, r in slots if (r or {}).get("repeat_of") is not None}
    reused = 0
    first_seen: set = set()
    for shot_id, a, b, _row in slots:
        c = clip_at(a, b)
        if c is None:
            continue
        keys = [(c.file_id, k) for k in range(int(c.src_in / mref.BUCKET_SECONDS), int(max(c.src_in, c.src_out - 1e-6) / mref.BUCKET_SECONDS) + 1)]
        if any(k in first_seen for k in keys) and shot_id not in repeat_slots:
            reused += 1                                                # footage shown again where the reference does not repeat itself
        first_seen.update(keys)
    if reused:
        variety_penalty += min(0.6, 0.1 * reused)
    if share > 0.25:
        variety_penalty += min(0.3, (share - 0.25) * 2.0)
    variety_penalty += min(0.3, back_to_back / max(1, len(placed_files)) * 2.0)
    overused = {key for key, n in buckets.items() if n > 1}
    moments.update(buckets)
    if overused:
        seen: Counter = Counter()
        for shot_id, a, b, _row in slots:
            c = clip_at(a, b)
            if c is None:
                continue
            hot = [(c.file_id, k) for k in range(int(c.src_in / mref.BUCKET_SECONDS), int(max(c.src_in, c.src_out - 1e-6) / mref.BUCKET_SECONDS) + 1)
                   if (c.file_id, k) in overused]
            if not hot:
                continue
            seen[hot[0]] += 1
            if seen[hot[0]] > 1 and shot_id not in repeat_slots:      # the first showing is fine; a later one should change unless the reference repeats
                name = by_id[c.file_id].name[:24] if c.file_id in by_id else c.file_id
                findings.setdefault(shot_id, []).append(f"same moment of {name} used {buckets[hot[0]]} times")
                info = slot_info.get(shot_id)
                swap = _swap_fix(shot_id, info[0], pool, info[1], moments, "any", neighbour_files(shot_id), proposed) if info else None
                if swap:
                    fixes.setdefault(shot_id, swap)

    for shot_id in look_misses:                                            # after the repeats are settled, so a look swap cannot make a new one
        info = slot_info.get(shot_id)
        swap = _swap_fix(shot_id, info[0], pool, info[1], moments, "look", neighbour_files(shot_id), proposed) if info else None
        fixes.setdefault(shot_id, swap or "build_recreation_tool(grade=true, only_shots=[%d])" % shot_id)

    song_present = any(c.start <= 0.5 and c.length >= 0.8 * max(b for _i, _a, b, _r in slots) for c in ref_clips)
    audible = sum(1 for c in pictures if not _is_muted(c.id))
    sound = 1.0 if song_present else 0.4
    if audible:
        sound -= min(0.5, audible / max(1, len(pictures)))
    areas = {"timing": _grade_score(timing), "coverage": _grade_score(coverage), "variety": round(100.0 * max(0.0, 1.0 - variety_penalty), 1),
             "picture": _grade_score(picture) if picture else 50.0, "look": _grade_score(look_scores) if look_scores else 50.0, "sound": round(100.0 * max(0.0, sound), 1),
             "framing": _grade_score(framing_scores) if framing_scores else 100.0}
    overall = round(sum(areas[k] * w for k, w in AREA_WEIGHT.items()), 1)
    passed = overall >= PASS_SCORE and min(areas.values()) >= AREA_FLOOR and worst_cut <= MAX_CUT_ERROR_FRAMES
    ref_transitions = sorted((s for s in ref.shots if (s.get("opens_with") or {}).get("kind") in ("fade", "dissolve")), key=lambda s: s["start"])
    failing = [{"reference_shot": sid, "time": _mmss(next(a for i, a, _b, _r in slots if i == sid)), "problems": probs, "fix": fixes.get(sid)}
               for sid, probs in sorted(findings.items())][:20]
    sheet_path, sheet_rows = None, []
    if show_pictures and plan and findings:
        worst = sorted(findings, key=lambda sid: (-len(findings[sid]), sid))[:MAX_COMPARE_SHOTS]
        try:
            sheet_path, sheet_rows, _skipped_sheet = _compare_sheet(plan, ref.file_id, sorted(worst))
        except Exception:  # noqa: BLE001 - the numbers are the answer; the picture is a bonus
            log.info("could not draw the picture of the worst shots", exc_info=True)
    head = (f"{'PASS' if passed else 'NOT YET'}: overall {overall} (timing {areas['timing']}, coverage {areas['coverage']}, variety {areas['variety']}, "
            f"picture {areas['picture']}, look {areas['look']}, sound {areas['sound']}, framing {areas['framing']}); {len(findings)} shot(s) to fix.")
    return ok(head + (f" {len(skipped)} shot(s) skipped on purpose: {skipped[:12]}." if skipped else ""), passed=passed, overall=overall, areas=areas, skipped=skipped, failing=failing, failing_total=len(findings), image_path=sheet_path, image_rows=sheet_rows,
              variety={"most_uses_of_one_moment": reuse_max, "clips_twice_in_a_row": back_to_back, "biggest_share_of_one_file": round(share, 2),
                       "files_used": len(uses)},
              graded=graded, slots=len(slots),
              transitions={"reference_fades_and_dissolves": len(ref_transitions), "timeline_transitions": len(transitions),
                           "at": [_mmss(s["start"]) + " " + str((s.get("opens_with") or {}).get("kind")) for s in ref_transitions[:12]]},
              next="Fix the shots in `failing` with build_recreation_tool (overrides/only_shots), then check again. Stop at PASS or after three rounds with no change.")


# ============================ compare ============================


def _tile(f, t: float) -> "Any":
    """One frame of file *f* at time *t*, scaled to fit a TILE_WIDTH x TILE_HEIGHT tile (padded, never stretched)."""
    import numpy as np
    from classes.editor_tools.media_index_tools_precision import _audio_path
    from classes.media_index import contact
    from classes.media_index.probe import probe_media
    path = _audio_path(f)
    probe = probe_media(path)
    video = probe.get("video") or {}
    vw, vh = int(video.get("width") or 0), int(video.get("height") or 0)
    if not vw or not vh:
        raise ToolError(f"could not read the picture of {str(f.data.get('name') or f.id)[:40]}")
    w = min(TILE_WIDTH, max(2, int(round(TILE_HEIGHT * vw / vh / 2)) * 2))
    frames, _got = contact.decode_frames_at(path, [max(0.0, t)], (w, TILE_HEIGHT), start_time=float(probe.get("start_time") or 0.0))
    tile = np.zeros((TILE_HEIGHT, TILE_WIDTH, 3), dtype=np.uint8)
    x = (TILE_WIDTH - w) // 2
    tile[:, x:x + w] = frames[0]
    return tile


def _compare_sheet(plan: Dict[str, Any], reference_file_id: str, wanted: List[int]) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Render the reference-beside-footage picture for *wanted* shots: (png path, rows shown, shots skipped with why). Raises ToolError when
    nothing can be shown."""
    import os
    import tempfile
    import numpy as np
    from classes.media_index import contact
    from classes.query import File
    ref_file = File.get(id=str(reference_file_id))
    rows = {r["reference_shot"]: r for r in plan["rows"]}
    tiles, legend = [], []
    for shot_id in wanted[:MAX_COMPARE_SHOTS]:
        row = rows.get(shot_id)
        c = _effective(row, plan) if row else None
        if row is None or c is None or ref_file is None:
            legend.append({"shot": shot_id, "note": "no footage chosen for this shot" if row else "not a shot of this plan"})
            continue
        src = File.get(id=str(c["file_id"]))
        if src is None:
            legend.append({"shot": shot_id, "note": "the chosen file is no longer in the project"})
            continue
        ref_t = (float(row["start"]) + float(row["end"])) / 2.0
        src_t = float(c["in"]) + float(row["duration"]) / 2.0
        tiles.extend([_tile(ref_file, ref_t), _tile(src, src_t)])
        legend.append({"shot": shot_id, "reference_t": round(ref_t, 2), "source": str(src.data.get("name") or c["file_id"])[:40], "source_t": round(src_t, 2),
                       "described_as": (row.get("description") or "")[:100]})
    if not tiles:
        raise ToolError("nothing to show: " + "; ".join(str(x.get("note")) for x in legend))
    times = [t for entry in legend if "reference_t" in entry for t in (entry["reference_t"], entry["source_t"])]
    picture = contact.sheet(np.stack(tiles), times, 0.0, 2)
    fd, path = tempfile.mkstemp(suffix=".png", prefix="recreation_compare_")
    os.close(fd)
    contact.write_png(path, picture)
    try:
        from classes.agent_tools.output import ImageBlock, attach_images
        from classes.editor_tools.media_index_tools_precision import _sheet_jpeg
        attach_images([ImageBlock(data=_sheet_jpeg(path))])
    except Exception:                                                      # the path in the result still works
        log.info("comparison picture not attached; image_path still works", exc_info=True)
    return path, [e for e in legend if "reference_t" in e], [e for e in legend if "note" in e]


@editor_tool(
    "compare_recreation_tool",
    covers=("index.match",),
    label="Look at recreated shots next to the reference",
    schema=obj({
        "reference_file_id": string("The video being recreated."),
        "shots": array({"type": "integer"}, f"Reference shot numbers to look at, at most {MAX_COMPARE_SHOTS} (the ones check_recreation_tool lists as failing, or the weakest in the plan)."),
        "plan_id": string("The plan (default: the latest for this reference).", ""),
    }, required=["reference_file_id", "shots"]),
    read_only=True,
)
def compare_recreation(reference_file_id, shots, plan_id=""):
    """Pictures for judging a recreation by eye: for each listed reference shot, the reference's frame (left) beside the frame of the footage
    chosen for it (right, from the middle of the chosen window, before any colour grade). Look at them: is it the same kind of scene, subject
    and framing? If not, swap the shot (build_recreation_tool overrides), look for stock, or leave it out and say so. The numbers in
    check_recreation_tool cannot tell you that; the picture can.
    """
    wanted = [int(x) for x in shots][:MAX_COMPARE_SHOTS]
    if not wanted:
        raise ToolError("give the reference shot numbers to look at")
    files, _missing = project_indexes()
    _reference(files, reference_file_id)                                  # raises a plain message when the reference has no index
    plan = _plan(plan_id, reference_file_id)
    if not plan:
        raise ToolError("no plan yet: plan_recreation_tool makes one")
    path, shown, skipped = _compare_sheet(plan, str(reference_file_id), wanted)
    return ok(f"{len(shown)} shot(s): reference on the left, the chosen footage on the right, one row per shot in the order listed.",
              image_path=path, rows=shown, skipped=skipped)
