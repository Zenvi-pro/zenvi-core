"""Tools that change the edit using what the index knows: cut to the beat, balance the mix, keep the brief.

Every tool here validates before it touches anything and is one undo step. The decisions are made by the pure
planners in ``media_index`` (``beatsync``, ``mixplan``); this module only describes the timeline to them and
applies what they return through the editor's own operations.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from classes.editor_tools._base import ToolError, array, boolean, enum, get_app, integer, mapping, number, obj, ok, on_main, string, th
from classes.editor_tools._registry import editor_tool
from classes.editor_tools.media_index_tools_review import MAX_MIX_SECONDS, build_timeline, measure_looks, render_timeline_mix
from classes.editor_tools.timeline_edit_common import extend_timeline, frame_seconds, refresh, require_unlocked, save_clip, snap, tolerance
from classes.media_index import audition, beatsync, harmonize, library, mixplan, review as R
from classes.media_index.store import default_shelf

BRIEF_KEY = "edit_brief"
BRIEF_MAX_BYTES = 64 * 1024


def _index_provider():
    shelf = default_shelf()
    cache: Dict[str, Any] = {}

    def index_for(c: R.TimelineClip) -> Any:
        if c.sha not in cache:
            cache[c.sha] = library.get_file_index(shelf, c.sha, file_id=c.file_id, name=c.name) if c.sha else None
        return cache[c.sha]
    return index_for


# ============================ sync_cuts_to_beats_tool ============================
@editor_tool(
    "sync_cuts_to_beats_tool",
    covers=("index.beat_sync",),
    label="Cut to the beat",
    schema=obj({
        "music_clip_id": string("Timeline clip id of the music to cut to (default: the longest music clip with a detected tempo).", ""),
        "on": enum(["beat", "downbeat"], "'beat' = every beat, 'downbeat' = only the first beat of each bar (a calmer, more musical cut).",
                   "beat"),
        "max_shift_seconds": number("The furthest a cut may move to reach a beat. Larger moves change what shows more.", 0.25,
                                    minimum=0.05, maximum=1.0),
        "include_speech": boolean("Also move cuts next to speaking clips (they are left alone by default: a move could land mid-word).", False),
        "dry_run": boolean("Only report which cuts would move; change nothing.", False),
    }),
)
def sync_cuts_to_beats(music_clip_id="", on="beat", max_shift_seconds=0.25, include_speech=False, dry_run=False):
    """Nudge the cuts between neighbouring picture clips onto the beat of the music, so the edit feels tight.

    For each cut the earlier clip's out point and the later clip's in point shift together by the same small amount
    (to the nearest beat within max_shift_seconds, on a whole frame), so the picture on each side stays continuous, no
    other edge moves and the length does not change. A cut stays where it is, with the reason, when a clip has no spare
    source to give, would become too short, has its speed changed, overlaps its neighbour (a transition) or speaks
    (unless include_speech). Works on every track. One undo step. Use dry_run to see the plan first.
    """
    from classes.clip_utils import clip_time_bounds

    clips, _project, objs = build_timeline()
    index_for = _index_provider()
    beds = [c for c in clips if c.role == "music" and c.length > 0]
    if music_clip_id:
        bed = next((c for c in beds if c.id == str(music_clip_id)), None)
        if bed is None:
            raise ToolError(f"{music_clip_id!r} is not a music clip on the timeline (analyze_timeline_audio_tool lists them)")
    else:
        with_tempo = [c for c in beds if ((getattr(index_for(c), "audio", None) or {}).get("tempo") or {}).get("beats")]
        if not with_tempo:
            raise ToolError("no music with a detected tempo is on the timeline: add music and let it finish indexing, or the track has no steady beat")
        bed = max(with_tempo, key=lambda c: c.length)
    audio = getattr(index_for(bed), "audio", None) or {}
    tempo = audio.get("tempo") or {}
    if not tempo.get("beats"):
        raise ToolError(f"{bed.name!r} has no steady tempo, so there is no beat to cut to")
    source_beats = ((audio.get("music") or {}).get("downbeats") if on == "downbeat" else None) or tempo["beats"]
    if on == "downbeat" and not (audio.get("music") or {}).get("downbeats"):
        source_beats = tempo["beats"][::4]
    beats = sorted(t for t in (bed.to_timeline(x) for x in source_beats if bed.src_in <= x <= bed.src_out) if bed.start <= t <= bed.end)
    if not beats:
        raise ToolError("none of the music's beats fall inside the part of it that is on the timeline")

    cut_clips = []
    for c in clips:
        if c.kind not in ("video", "image") or c.length <= 0:
            continue
        clip_obj = objs.get(c.id)
        max_src: Optional[float] = None
        if c.kind == "video" and clip_obj is not None:
            longest, _frames = clip_time_bounds(clip_obj.data)
            max_src = float(longest) if longest else None
        cut_clips.append(beatsync.CutClip(c.id, c.layer, c.start, c.end, c.src_in, c.src_out, c.speed, max_src, speech=(c.role == "speech")))
    plan = beatsync.plan_beat_sync(cut_clips, beats, max_shift=float(max_shift_seconds), frame=frame_seconds(), include_speech=bool(include_speech))
    summary_bits = f"{plan['cuts']} cut(s): {len(plan['moves'])} to move, {plan['already_on_beat']} already on a beat, {len(plan['skipped'])} left as they are"
    receipt = dict(cuts=plan["cuts"], already_on_beat=plan["already_on_beat"], moves=plan["moves"], skipped=plan["skipped"][:20],
                   music_clip_id=bed.id, bpm=tempo.get("bpm"), on=on)
    if dry_run or not plan["moves"]:
        return ok(("Would move " if dry_run else "Nothing to move: ") + summary_bits + ".", changed=False, dry_run=bool(dry_run), **receipt)

    final: Dict[str, Dict[str, float]] = {}
    for c in cut_clips:
        obj_ = objs[c.id]
        final[c.id] = {"start": float(obj_.data.get("start") or 0.0), "end": float(obj_.data.get("end") or 0.0), "position": float(obj_.data.get("position") or 0.0)}
    for m in plan["moves"]:
        final[m["earlier"]]["end"] = m["earlier_source_out"]
        final[m["later"]]["start"] = m["later_source_in"]
        final[m["later"]]["position"] = m["later_position"]
    touched = {m["earlier"] for m in plan["moves"]} | {m["later"] for m in plan["moves"]}
    require_unlocked({int(objs[i].data.get("layer") or 0) for i in touched})
    for cid in touched:                                   # validate every result before the first write
        f = final[cid]
        if f["end"] - f["start"] < frame_seconds() - tolerance() or f["position"] < -tolerance():
            raise ToolError(f"the plan would leave clip {cid} with no picture or before 0 s; nothing was changed")
    for cid in sorted(touched, key=lambda i: final[i]["position"]):
        f = final[cid]
        save_clip(objs[cid], start=f["start"], end=f["end"], duration=f["end"] - f["start"], position=snap(f["position"]))
    extend_timeline()
    refresh()
    return ok(f"Moved {len(plan['moves'])} cut(s) onto the {on}: " + summary_bits + ".", changed=True, **receipt)


# ============================ balance_mix_tool ============================
def _is_error(text: Any) -> bool:
    return str(text or "").lstrip().startswith("Error")


@editor_tool(
    "balance_mix_tool",
    covers=("index.balance_mix",),
    label="Balance the mix",
    background_safe=True,
    schema=obj({
        "even_out_voices": boolean("Bring every speaking clip to the same level (to the median voice, or speech_target_db).", True),
        "speech_target_db": number("The level (dB, as measured: -20 is a typical voice) to bring voices to; omit to use the median voice.", None,
                                   minimum=-60, maximum=0),
        "duck_music": boolean("Duck music and sound-effect beds under the voice, with volume keyframes, restoring them in the gaps.", True),
        "set_loudness": boolean("Render the mix, measure it, and raise or lower everything together to the platform's loudness target "
                                "(YouTube/TikTok/Reels about -14 LUFS, podcasts -16, film -23) without pushing peaks over -1 dB.", True),
        "form": string("What this is ('YouTube vlog', 'podcast', 'film'...), to pick the loudness target. '' = -14 LUFS (online video).", ""),
        "target_lufs": number("Override the loudness target (LUFS).", None, minimum=-40, maximum=-5),
        "dry_run": boolean("Only report what would change; change nothing.", False),
    }),
)
def balance_mix(even_out_voices=True, speech_target_db=None, duck_music=True, set_loudness=True, form="", target_lufs=None, dry_run=False):
    """Make the sound of the edit sit right in one pass: even out the voices, duck the music under them, then set the
    overall level to the target loudness. It reads the measured levels of every clip (as indexed, plus the gain set),
    applies the changes with the editor's own volume and ducking operations (volume curves, no re-encoding, one undo step),
    renders the mix to measure its real loudness and peaks, and reports before and after. Levels that cannot be raised
    without distortion are reported with what to do instead (compressor, lower the loudest clip). Run review_edit_tool
    afterwards to confirm.
    """
    clips, project, _objs = build_timeline()
    audio_clips = [c for c in clips if c.has_audio and c.length > 0 and c.role not in (None, "silent")]
    if not audio_clips:
        raise ToolError("the timeline has no audio to balance")
    index_for = _index_provider()
    speech = [c for c in audio_clips if c.role == "speech"]
    beds = [c for c in audio_clips if c.role in ("music", "sfx")]
    result: Dict[str, Any] = {"voices": None, "ducking": None, "loudness": None}

    items = [{"id": c.id, "level_db": R.source_level_db(c, index_for(c)), "gain_db": c.gain_db} for c in speech]
    voices = mixplan.speech_adjustments(items, target_db=speech_target_db) if even_out_voices and speech else {"target_db": None, "adjust": [], "left_alone": []}
    result["voices"] = voices
    duck_plan: List[Dict[str, Any]] = []
    if duck_music and speech and beds:
        # How deep each bed must go to sit a margin under the quietest voice it plays with, using the levels that will be heard
        # (source level plus gain, after the voices have been evened out). Unknown levels fall back to the editor's own estimate.
        delta = {a["id"]: a["delta_db"] for a in voices["adjust"]}
        for bed in beds:
            heard = []
            for sp in speech:
                lo, hi = max(sp.start, bed.start), min(sp.end, bed.end)
                level = R.source_level_db(sp, index_for(sp))
                if hi - lo >= 0.5 and level is not None:
                    heard.append(level + sp.gain_over(lo, hi) + delta.get(sp.id, 0.0))
            bed_src = R.source_level_db(bed, index_for(bed))
            depth = mixplan.duck_depth(heard, None if bed_src is None else bed_src + bed.gain_over(bed.start, bed.end))
            duck_plan.append({"bed": bed.id, "name": bed.name, **depth})
        result["ducking"] = duck_plan
    elif duck_music and beds and not speech:
        result["ducking"] = "no speech on the timeline: the music is the soundtrack and stays at its level"
    target = float(target_lufs) if target_lufs is not None else R.loudness_target(R.Brief(form=str(form or "")))
    if dry_run:
        return ok(f"Would adjust {len(voices['adjust'])} voice(s)" + (", duck the music" if duck_plan and any(d["duck_db"] is not None or d["why"] == "levels unknown" for d in duck_plan) else "") +
                  (f" and set the loudness to {target:g} LUFS" if set_loudness else "") + ".", changed=False, dry_run=True, target_lufs=target, **result)

    errors: List[str] = []

    def apply_voices_and_ducking() -> None:
        handlers = th()
        for a in voices["adjust"]:
            out = handlers.set_clip_volume(timeline_clip_id=a["id"], level_db=str(a["delta_db"]), mode="scale")
            if _is_error(out):
                errors.append(f"voice {a['id']}: {out}")
        reports = []
        for step in duck_plan:
            if step["duck_db"] is None and step["why"] != "levels unknown":
                continue                                           # already far enough under the voice: leave it
            out = handlers.duck_under_speech(bed_clip_ids=step["bed"], speech_clip_ids="auto",
                                             duck_db="auto" if step["duck_db"] is None else str(step["duck_db"]))
            if _is_error(out):
                errors.append(f"ducking {step['bed']}: {out}")
            else:
                reports.append(str(out)[:300])
        if reports:
            result["ducking_report"] = " | ".join(reports)[:900]

    if voices["adjust"] or any(s["duck_db"] is not None or s["why"] == "levels unknown" for s in duck_plan):
        on_main(apply_voices_and_ducking)

    if set_loudness:
        from classes.media_index import audio as au
        end = min(project.duration, MAX_MIX_SECONDS)
        before = _measure_mix(end, au, errors)
        corr = mixplan.master_correction(before.get("integrated_lufs") if before else None, target, before.get("true_peak_db") if before else None)
        result["loudness"] = {"before": before, "target_lufs": target, **corr}
        if abs(corr["delta_db"]) > 0.05:
            def apply_master() -> None:
                handlers = th()
                for c in audio_clips:
                    out = handlers.set_clip_volume(timeline_clip_id=c.id, level_db=str(corr["delta_db"]), mode="scale")
                    if _is_error(out):
                        errors.append(f"level {c.id}: {out}")
            on_main(apply_master)
            result["loudness"]["after"] = _measure_mix(end, au, errors)
    changed = bool(voices["adjust"]) or bool(result.get("ducking_report")) or bool(result["loudness"] and abs(result["loudness"].get("delta_db", 0.0)) > 0.05)
    bits = []
    if voices["adjust"]:
        bits.append(f"evened out {len(voices['adjust'])} voice(s) to {voices['target_db']:.0f} dB")
    if result.get("ducking_report"):
        bits.append("ducked the music under the voice")
    lo = result["loudness"]
    if lo and lo.get("after"):
        bits.append(f"loudness {lo['before'].get('integrated_lufs'):.1f} -> {lo['after'].get('integrated_lufs'):.1f} LUFS (target {target:g})")
    elif lo and lo.get("peak_warning"):
        bits.append(lo["peak_warning"])
    return ok(("Balanced the mix: " + "; ".join(bits) + ".") if bits else "The mix needed no changes.", changed=changed, errors=errors or None,
              target_lufs=target, **result)


def _measure_mix(end: float, au: Any, errors: List[str]) -> Optional[Dict[str, Any]]:
    path, why = render_timeline_mix(0.0, end)
    if not path:
        errors.append("could not render the mix to measure it: " + why)
        return None
    try:
        return au.measure_loudness(path)
    finally:
        try:
            os.remove(path)
            os.rmdir(os.path.dirname(path))
        except OSError:
            pass


# ============================ the edit brief ============================
def _clean_brief(value: Any) -> Dict[str, Any]:
    """The brief as plain JSON, or ToolError. Keys are lower-cased names; None values mean 'cleared'."""
    if not isinstance(value, dict):
        raise ToolError("the brief must be a JSON object")
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"the brief must be plain JSON: {exc}")
    if len(text.encode("utf-8")) > BRIEF_MAX_BYTES:
        raise ToolError(f"the brief is over {BRIEF_MAX_BYTES // 1024} KB; keep the plan short and put detail in files")
    return json.loads(text)


def read_brief() -> Dict[str, Any]:
    stored = get_app().project.get(BRIEF_KEY) or {}
    return {k: v for k, v in stored.items() if v is not None} if isinstance(stored, dict) else {}


@editor_tool(
    "set_edit_brief_tool",
    covers=("index.brief",),
    label="Save the edit brief",
    schema=obj({
        "brief": mapping("What to remember about this edit, as a JSON object. Suggested keys: form ('YouTube vlog'), target_seconds, vibe, "
                         "bans (['no stock', 'no AI']), delivery ('YouTube 1080p'), beat_map (the plan: a list of {label, start, end, notes}), "
                         "scenes (cards: {id, summary, mood, look, music}), music (the chosen track and why), look_target, notes, done (what is finished)."),
        "replace": boolean("Replace the whole brief instead of updating the keys you give (a key set to null is removed either way).", False),
    }, required=["brief"]),
)
def set_edit_brief(brief, replace=False):
    """Save the plan and intent of this edit inside the project, so a long job (a vlog, a whole film) survives a long
    conversation, a restart or another agent picking it up. It is stored with the project (saved with it, never an undo
    step, shared by the Zenvi Assistant and Claude Code). Update it as you go: mark scenes done, note decisions.
    review_edit_tool and get_edit_brief_tool read it.
    """
    new = _clean_brief(brief)
    old = read_brief()
    merged = dict(new) if replace else {**old, **new}
    merged = {k: v for k, v in merged.items() if v is not None}
    if len(json.dumps(merged, ensure_ascii=False).encode("utf-8")) > BRIEF_MAX_BYTES:
        raise ToolError(f"the brief would be over {BRIEF_MAX_BYTES // 1024} KB; keep the plan short")
    clear = {k: None for k in old if k not in merged}

    def write() -> None:
        get_app().updates.update_untracked([BRIEF_KEY], {**clear, **merged})
    on_main(write)
    return ok(f"Saved the edit brief ({len(merged)} item(s)).", brief=merged, removed=sorted(clear))


@editor_tool(
    "get_edit_brief_tool",
    covers=("index.brief",),
    label="Read the edit brief",
    schema=obj({}),
    read_only=True,
)
def get_edit_brief():
    """Read the edit brief saved with this project: the intent, plan and progress someone (or you, earlier) recorded
    with set_edit_brief_tool. Read it first when picking up an edit you did not start or after a long break.
    """
    brief = read_brief()
    if not brief:
        return ok("No edit brief is saved for this project yet.", brief={})
    return ok(f"Edit brief with {len(brief)} item(s).", brief=brief)


# ============================ harmonize_look_tool ============================
@editor_tool(
    "harmonize_look_tool",
    covers=("index.harmonize",),
    label="Match the clips' look",
    background_safe=True,
    schema=obj({
        "reference": string("The look to match everything to: a timeline clip id, or 'median' (default) = the clip whose look is closest "
                            "to all the others, so the group is pulled toward its own centre.", "median"),
        "tolerance": number("How far a clip's look may be from the reference before it is matched (0.15 = visibly different; the same "
                            "scene twice is about 0.02).", 0.15, minimum=0.02, maximum=1.0),
        "max_clips": integer("Most clips to match in one call (the furthest from the reference first).", 12, minimum=1, maximum=40),
        "measure_limit": integer("Most clips whose look is measured (an even sample when there are more).", 24, minimum=2, maximum=80),
        "dry_run": boolean("Only measure and report which clips would be matched; change nothing.", False),
    }),
)
def harmonize_look(reference="median", tolerance=0.15, max_clips=12, measure_limit=24, dry_run=False):
    """Make the clips of an edit look like they belong together. It measures how every picture clip looks as it plays
    (grade included), picks the reference look (the median clip unless you name one), finds the clips that stray beyond
    the tolerance and matches them to it with the editor's own colour matching (it solves the grade, applies it as one
    undo step, re-measures and recovers if a result goes wrong), then re-measures and reports each clip's distance
    before and after. It only evens out clips relative to each other: for a creative look apply it afterwards with
    apply_look_tool or color_grade_clip_tool, or name a clip that already has the look as the reference.
    """
    from classes import color_agent as ca

    clips, _project, objs = build_timeline()
    order: List[str] = []
    for shot in R.visible_shots(clips):
        if shot["clip"].id not in order:
            order.append(shot["clip"].id)
    if len(order) < 2:
        raise ToolError("a timeline needs at least two picture clips to match them to each other")
    looks, considered = measure_looks(clips, objs, max_clips=int(measure_limit))
    if len(looks) < 2:
        raise ToolError("fewer than two clips could be measured (are they rendering?): try again, or use match_color_to_reference_tool")
    try:
        plan = harmonize.plan_harmonize({k: v for k, v in looks.items()}, [c for c in order if c in looks], ca.look_profile_distance,
                                        reference=str(reference or "median"), tolerance=float(tolerance), max_clips=int(max_clips))
    except ValueError as exc:
        raise ToolError(str(exc))
    names = {c.id: c.name for c in clips}
    ref = plan["reference"]
    receipt = dict(reference=ref, reference_name=names.get(ref), tolerance=plan["tolerance"], distances=plan["distances"],
                   to_match=plan["to_match"], left_out=plan["left_out"], within_tolerance=plan["within_tolerance"],
                   unmeasured=plan["unmeasured"], measured=len(looks), considered=considered)
    if dry_run or not plan["to_match"]:
        said = (f"Would match {len(plan['to_match'])} clip(s) to {names.get(ref) or ref}" if plan["to_match"]
                else f"All {len(looks)} measured clips already look alike (within {plan['tolerance']:g} of {names.get(ref) or ref})")
        return ok(said + ".", changed=False, dry_run=bool(dry_run), **receipt)

    out = th().match_color_to_reference(clipIds=",".join(plan["to_match"]), referenceClipId=ref)
    if _is_error(out):
        raise ToolError(f"the colour match failed: {str(out)[:300]}")
    again, _n = measure_looks([c for c in clips if c.id in set(plan["to_match"])], objs, max_clips=len(plan["to_match"]) + 1)
    after: Dict[str, Any] = {}
    for cid in plan["to_match"]:
        d = ca.look_profile_distance(again[cid], looks[ref]) if cid in again else None
        after[cid] = round(float(d), 4) if d is not None else None
    improved = [cid for cid in plan["to_match"] if after.get(cid) is not None and after[cid] < plan["distances"][cid]]
    closer = ", ".join(f"{names.get(cid) or cid} {plan['distances'][cid]:.2f} -> {after[cid]:.2f}" for cid in improved[:4] if after.get(cid) is not None)
    return ok(f"Matched {len(plan['to_match'])} clip(s) to {names.get(ref) or ref}; {len(improved)} now look closer" + (f" ({closer})" if closer else "") + ".",
              changed=True, after=after, improved=improved, matcher=str(out)[:400], **receipt)


# ============================ audition_music_tool ============================
@editor_tool(
    "audition_music_tool",
    covers=("index.audition",),
    label="Audition music",
    schema=obj({
        "candidates": array(mapping("One sound to audition.", properties={
            "id": string("The sound's id (Freesound id from search_freesound_music_tool)."),
            "name": string("A name to show in the result."),
            "preview_url": string("The preview MP3 URL from the search result (https, freesound.org only)."),
            "duration": number("The full sound's length in seconds (the preview may be shorter).")},
            additionalProperties=False, required=["id", "preview_url"]),
            "The sounds to compare (at most 12), from search_freesound_music_tool's results."),
        "bpm_min": number("Slowest tempo you want.", None, minimum=0),
        "bpm_max": number("Fastest tempo you want.", None, minimum=0),
        "seconds_needed": number("How long the music must play (the whole edit, or the section it will cover).", None, minimum=0),
        "energy": enum(["", "low", "medium", "high", "building"], "The energy you want.", ""),
    }, required=["candidates"]),
    read_only=True,
)
def audition_music(candidates, bpm_min=None, bpm_max=None, seconds_needed=None, energy=""):
    """Listen before you choose: download each candidate's preview, analyse it (tempo, beats, energy curve, sections,
    phrase points) and rank the sounds by how well they fit the music you need, with the measured reason for each.
    Free (previews cost nothing, the analysis is local) and nothing is imported. Then place the winner with
    stock_music(query=..., sound_id=<id>, preview_url=<its preview_url>, start_seconds=<a phrase point>, ...) so it
    starts and ends on a phrase. Tempo and energy are measured on the preview; sections may be incomplete for long sounds.
    """
    cleaned = [c for c in (candidates or []) if isinstance(c, dict)]
    if not cleaned:
        raise ToolError("give at least one candidate (id and preview_url from search_freesound_music_tool)")
    result = audition.audition(cleaned, bpm_min=bpm_min, bpm_max=bpm_max, seconds=seconds_needed, energy=str(energy or ""))
    ranked = result["ranked"]
    if not ranked:
        raise ToolError("none of the candidates could be analysed: " + "; ".join(f"{f['name']}: {f['error']}" for f in result["failed"][:3]))
    best = ranked[0]
    summary = (f"Best fit: {best['name']} ({best['bpm']:.0f} BPM)" if best.get("bpm") else f"Best fit: {best['name']} (no steady tempo)") + \
        (f"; {sum(1 for r in ranked if r['fits'])} of {len(ranked)} fit" if bpm_min or bpm_max or seconds_needed or energy else "") + "."
    return ok(summary, ranked=ranked, failed=result["failed"], analysed=result["analysed"],
              use_best=dict(sound_id=best["id"], preview_url=best["preview_url"], start_seconds=(best["phrase_points"] or [0.0])[0]))
