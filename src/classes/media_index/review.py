"""
 @file
 @brief The audit of a finished edit: what still needs doing, with measured evidence.

 Pure logic over a plain description of the timeline (``TimelineClip`` list), the footage index and a few
 measurements the caller took (live colour of each clip, the rendered mix's loudness). Nothing here touches
 Qt, the project or the network, so every rule is tested on small hand-built timelines.

 Each finding is ``{"id", "layer", "status", "summary", "evidence", "fix"}`` with status ``ok`` (checked, fine),
 ``needs`` (act on it), ``info`` (worth knowing, no action required by default) or ``unknown`` (could not be
 measured: index the footage, render the mix). The audit never edits anything and never decides *how* to fix
 a problem: ``fix`` names tools the agent may choose from. Whether a layer is wanted at all depends on the
 ``Brief`` (form, vibe, bans); with no form given, expectations that depend on it are reported as ``info``.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from classes.media_index import quality as Q

# ---- story ------------------------------------------------------------------------------------
LENGTH_TOLERANCE = 0.10          # within 10 % of the requested length is on target
HOOK_MIN_HIGHLIGHT = 0.45
HOOK_MIN_POTENTIAL = 0.40
FAST_CUT_SECONDS = 0.5
FAST_CUT_SHARE = 0.25            # more than this share of shots under FAST_CUT_SECONDS reads as frantic
MONOTONE_CV = 0.20               # shot lengths varying less than this (std / mean) feel mechanical
MIN_SHOTS_FOR_PACING = 6
SLOW_MEDIAN_SHORT_FORM = 8.0     # a short-form edit whose median shot is this long is slow
REPEAT_OVERLAP = 0.5             # source ranges overlapping this much (of the shorter) are the same moment
VARIETY_TOP_SHARE = 0.8

# ---- picture ----------------------------------------------------------------------------------
LUMA_SPREAD = 0.22               # p90 - p10 of clip brightness across the edit
WARMTH_SPREAD = 0.10
OUTLIER_MIN = 0.15               # a clip this far from the median brightness stands out (a whole-edit spread can hide one)
WARM_OUTLIER_MIN = 0.08

# ---- sound ------------------------------------------------------------------------------------
MUSIC_MIN_COVERAGE = 0.70
MARGIN_TARGET_DB = 10.0          # speech should sit at least this far above a music bed (prompt: duck 12-18 dB)
MARGIN_NEEDS_DB = 8.0
LEVEL_JUMP_DB = 6.0
DEAD_AIR_SECONDS = 2.0
LOUDNESS_TOLERANCE_LU = 3.0
TRUE_PEAK_MAX_DB = -1.0
BEAT_TOLERANCE = 0.10            # a cut within this many seconds of a beat counts as on the beat
BEAT_MIN_FRACTION = 0.40
MIN_CUTS_FOR_BEATS = 6
SILENCE_FLOOR_DB = -50.0
GAIN_SAMPLE_SECONDS = 0.25       # how finely an automated volume curve is read over an overlap

# ---- forms ------------------------------------------------------------------------------------
_MUSIC_FORMS = ("vlog", "tiktok", "reel", "short", "montage", "trailer", "recap", "highlight", "music video", "film",
                "movie", "scene", "ad", "promo", "commercial", "travel")
_DIALOGUE_FORMS = ("podcast", "interview", "tutorial", "lecture", "talk", "webinar", "news")
_BEAT_FORMS = ("montage", "trailer", "reel", "tiktok", "music video", "highlight", "recap", "promo")
LOUDNESS_TARGETS = {"podcast": -16.0, "film": -23.0, "movie": -23.0, "broadcast": -23.0}
DEFAULT_LOUDNESS = -14.0         # online video (YouTube, TikTok, Reels, Shorts)
FORM_MAX_SECONDS = {"tiktok": 180.0, "reel": 90.0, "short": 60.0}


@dataclass
class TimelineClip:
    id: str
    name: str = ""
    file_id: str = ""
    sha: str = ""
    layer: int = 1
    kind: str = "video"                      # video | image | audio | title | caption
    start: float = 0.0                       # timeline seconds
    end: float = 0.0
    src_in: float = 0.0                      # source seconds
    src_out: float = 0.0
    speed: float = 1.0
    role: Optional[str] = None               # speech | music | sfx | ambient | unknown (audio-bearing clips)
    gain_db: Optional[float] = None          # None = automated volume
    has_audio: bool = False
    effects: List[str] = field(default_factory=list)
    generated: bool = False
    gain_fn: Optional[Callable[[float], float]] = None   # gain in dB at a timeline second, for clips with automated (e.g. ducked) volume
    windows: List[Tuple[float, float]] = field(default_factory=list)   # timeline seconds in which this clip's voice is actually speaking

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)

    def gain_over(self, lo: float, hi: float) -> float:
        """The gain (dB) this clip really has over [lo, hi): the set level, or for automated volume the power average of the curve."""
        if self.gain_fn is None:
            return float(self.gain_db or 0.0)
        steps = max(1, int((hi - lo) / GAIN_SAMPLE_SECONDS))
        points = [self.gain_fn(lo + (hi - lo) * (i + 0.5) / steps) for i in range(steps)]
        return round(10.0 * math.log10(statistics.fmean(10 ** (g / 10.0) for g in points)), 2)

    def to_source(self, t: float) -> float:
        return self.src_in + (t - self.start) * self.speed

    def to_timeline(self, s: float) -> float:
        return self.start + (s - self.src_in) / (self.speed or 1.0)


@dataclass
class Brief:
    form: str = ""                           # "YouTube vlog", "TikTok", "short film", "podcast"...
    target_seconds: Optional[float] = None
    vibe: str = ""
    wants_music: Optional[bool] = None       # an explicit yes/no from the user wins over the form's default
    wants_captions: Optional[bool] = None
    wants_grade: Optional[bool] = None


@dataclass
class ProjectInfo:
    duration: float = 0.0
    width: int = 1920
    height: int = 1080


Provider = Callable[[TimelineClip], Any]


def finding(fid: str, layer: str, status: str, summary: str, evidence: Optional[Dict[str, Any]] = None,
            fix: Sequence[str] = ()) -> Dict[str, Any]:
    return {"id": fid, "layer": layer, "status": status, "summary": summary, "evidence": evidence or {}, "fix": list(fix)}


def _has(form: str, words: Sequence[str]) -> bool:
    f = (form or "").lower()
    return any(w in f for w in words)


_SHORT_NOT_FORM = re.compile(r"\bshorts?[\s-]+(film|movie|story|stories|documentary|doc|drama|comedy)\b")
_SHORT_WORD = re.compile(r"\bshorts?\b|\bshort-form\b")


def is_short_form(form: str) -> bool:
    """TikTok, Reels, YouTube Shorts and short-form video; not a 'short film'."""
    f = (form or "").lower()
    if _has(f, ("tiktok", "reel")):
        return True
    return bool(_SHORT_WORD.search(f)) and not _SHORT_NOT_FORM.search(f)


def music_expected(brief: Brief) -> Optional[bool]:
    if brief.wants_music is not None:
        return brief.wants_music
    if not brief.form:
        return None
    if _has(brief.form, _DIALOGUE_FORMS) and not _has(brief.form, ("vlog",)):
        return False
    return _has(brief.form, _MUSIC_FORMS)


def captions_expected(brief: Brief) -> Optional[bool]:
    if brief.wants_captions is not None:
        return brief.wants_captions
    return True if is_short_form(brief.form) else (None if not brief.form else False)


def vertical_expected(brief: Brief) -> bool:
    return is_short_form(brief.form)


def loudness_target(brief: Brief) -> float:
    for key, value in LOUDNESS_TARGETS.items():
        if _has(brief.form, (key,)):
            return value
    return DEFAULT_LOUDNESS


# ============================ helpers ============================
def visible_shots(clips: Sequence[TimelineClip]) -> List[Dict[str, Any]]:
    """What the viewer sees, in order: the topmost picture clip at each moment, runs of one clip merged."""
    pics = [c for c in clips if c.kind in ("video", "image") and c.length > 0]
    if not pics:
        return []
    cuts = sorted({round(t, 4) for c in pics for t in (c.start, c.end)})
    shots: List[Dict[str, Any]] = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        mid = (a + b) / 2.0
        top = max((c for c in pics if c.start <= mid < c.end), key=lambda c: (c.layer, c.start), default=None)
        if top is None:
            continue
        if shots and shots[-1]["clip"] is top and abs(shots[-1]["end"] - a) < 1e-3:
            shots[-1]["end"] = b
        else:
            shots.append({"clip": top, "start": a, "end": b})
    return shots


def _intervals_union(intervals: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for a, b in sorted(i for i in intervals if i[1] > i[0]):
        if out and a <= out[-1][1] + 1e-6:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _covered(intervals: Sequence[Tuple[float, float]], lo: float, hi: float) -> float:
    return sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in _intervals_union(intervals))


def source_level_db(clip: TimelineClip, index: Any, lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
    """Average level (dB, as indexed) of the audible part of a clip's source range; None when not indexed."""
    windows = (getattr(index, "audio", None) or {}).get("windows") or []
    a = clip.src_in if lo is None else lo
    b = clip.src_out if hi is None else hi
    mine = [w for w in windows if w["end"] > a and w["start"] < b and float(w.get("rms_db", -120.0)) > SILENCE_FLOOR_DB]
    if not mine:
        return None
    power = statistics.fmean(10 ** (float(w["rms_db"]) / 10.0) for w in mine)
    return round(10.0 * math.log10(power), 2)


def _shot_at(index: Any, t: float) -> Optional[Dict[str, Any]]:
    return index.shot_at(t) if index is not None and hasattr(index, "shot_at") else None


# ============================ story ============================
def story_findings(clips: Sequence[TimelineClip], project: ProjectInfo, brief: Brief, index_for: Provider,
                   take_groups: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    shots = visible_shots(clips)
    duration = project.duration or (shots[-1]["end"] if shots else 0.0)
    if not shots:
        return [finding("picture_present", "story", "needs", "There is no picture on the timeline yet.", {}, ["add_clips_to_timeline_tool"])]

    target = brief.target_seconds
    if target:
        off = (duration - target) / target
        out.append(finding("length", "story", "ok" if abs(off) <= LENGTH_TOLERANCE else "needs",
                           f"{duration:.1f} s against a target of {target:.0f} s ({off * 100:+.0f}%).",
                           {"duration": round(duration, 2), "target": target, "off_pct": round(off * 100, 1)},
                           [] if abs(off) <= LENGTH_TOLERANCE else ["trim_clips_tool", "set_clip_speed_tool", "remove_gaps_tool"]))
    cap = next((v for k, v in FORM_MAX_SECONDS.items() if (is_short_form(brief.form) if k == "short" else _has(brief.form, (k,)))), None)
    if cap and duration > cap:
        out.append(finding("form_length", "delivery", "needs", f"A {brief.form} should stay under {cap:.0f} s; this is {duration:.0f} s.",
                           {"duration": round(duration, 2), "limit": cap}, ["trim_clips_tool"]))

    lengths = [s["end"] - s["start"] for s in shots]
    median = statistics.median(lengths)
    evidence = {"shots": len(lengths), "median_seconds": round(median, 2), "min_seconds": round(min(lengths), 2), "max_seconds": round(max(lengths), 2)}
    if len(lengths) >= MIN_SHOTS_FOR_PACING:
        cv = statistics.pstdev(lengths) / statistics.fmean(lengths)
        fast = sum(1 for x in lengths if x < FAST_CUT_SECONDS) / len(lengths)
        evidence.update(variation=round(cv, 2), share_under_half_second=round(fast, 2))
        if fast > FAST_CUT_SHARE:
            out.append(finding("pacing", "story", "needs", f"{fast * 100:.0f}% of the shots are under {FAST_CUT_SECONDS} s: it will feel frantic.", evidence,
                               ["trim_clips_tool", "remove_clip_tool"]))
        elif is_short_form(brief.form) and median > SLOW_MEDIAN_SHORT_FORM:
            out.append(finding("pacing", "story", "needs", f"The median shot is {median:.1f} s: slow for a {brief.form}.", evidence, ["trim_clips_tool", "set_clip_speed_tool"]))
        elif cv < MONOTONE_CV:
            out.append(finding("pacing", "story", "info", "Every shot is about the same length; varying them (long, short, long) reads as more alive.", evidence,
                               ["trim_clips_tool"]))
        else:
            out.append(finding("pacing", "story", "ok", "Shot lengths vary naturally.", evidence))
    else:
        out.append(finding("pacing", "story", "info", f"Only {len(lengths)} shot(s): too few to judge pacing.", evidence))

    first = shots[0]
    idx = index_for(first["clip"])
    sh = _shot_at(idx, first["clip"].to_source(first["start"]))
    if sh is None:
        out.append(finding("hook", "story", "unknown", "The opening shot is not indexed, so its pull cannot be judged.", {"clip": first["clip"].id}, ["index_status_tool"]))
    else:
        q = sh.get("quality") or {}
        inferred = q.get("inferred") or {}
        highlight, potential = q.get("highlight"), inferred.get("hook_potential")
        weak = (potential is not None and potential < HOOK_MIN_POTENTIAL) or (potential is None and highlight is not None and highlight < HOOK_MIN_HIGHLIGHT)
        bad = [f for f in q.get("flags", []) if f in ("black", "blurry", "shaky", "dark")]
        status = "needs" if weak or bad else "ok"
        out.append(finding("hook", "story", status,
                           "The opening shot is weak." if status == "needs" else "The opening shot has pull.",
                           {"clip": first["clip"].id, "highlight": highlight, "hook_potential": potential, "flags": bad},
                           ["get_project_overview_tool", "search_footage_tool", "move_clips_tool"] if status == "needs" else []))

    weak_rows = []
    for s in shots:
        c = s["clip"]
        sh = _shot_at(index_for(c), c.to_source((s["start"] + s["end"]) / 2.0))
        flags = [f for f in ((sh or {}).get("quality") or {}).get("flags", []) if f in ("black", "blurry", "shaky", "dark", "bright", "audio_clipping", "audio_rumble")]
        if flags:
            weak_rows.append({"clip": c.id, "name": c.name, "at": round(s["start"], 2), "flags": flags})
    hard = [r for r in weak_rows if set(r["flags"]) & {"black", "blurry", "shaky"}]
    out.append(finding("weak_shots", "story", "needs" if hard else ("info" if weak_rows else "ok"),
                       f"{len(hard)} shot(s) are blurry, shaky or black." if hard else (f"{len(weak_rows)} shot(s) have minor issues." if weak_rows else "No weak shots found in the indexed footage."),
                       {"shots": weak_rows[:12]}, ["search_footage_tool", "trim_clips_tool", "remove_clip_tool"] if hard else []))

    repeats = []
    by_file: Dict[str, List[TimelineClip]] = {}
    for s in shots:
        by_file.setdefault(s["clip"].sha or s["clip"].file_id, []).append(s["clip"])
    for key, cs in by_file.items():
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                overlap = min(a.src_out, b.src_out) - max(a.src_in, b.src_in)
                shorter = min(a.src_out - a.src_in, b.src_out - b.src_in)
                if shorter > 0 and overlap / shorter >= REPEAT_OVERLAP and a is not b:
                    repeats.append({"clips": [a.id, b.id], "name": a.name, "source_overlap_seconds": round(overlap, 2)})
    used = {(c.file_id, ) for c in clips}
    similar = [g for g in take_groups if sum(1 for m in g["members"] if (m["file_id"],) in used) >= 2]
    out.append(finding("repeats", "story", "needs" if repeats else ("info" if similar else "ok"),
                       "The same moment is used more than once." if repeats else ("Some shots are near-duplicates of each other." if similar else "No repeated moments."),
                       {"repeated": repeats[:8], "similar_groups": len(similar)}, ["remove_clip_tool", "search_footage_tool"] if repeats else []))

    types = [((_shot_at(index_for(s["clip"]), s["clip"].to_source(s["start"])) or {}).get("watch") or {}).get("shot_type") for s in shots]
    types = [t for t in types if t and t != "unknown"]
    if len(types) >= MIN_SHOTS_FOR_PACING:
        top = max(set(types), key=types.count)
        share = types.count(top) / len(types)
        out.append(finding("variety", "story", "info" if share > VARIETY_TOP_SHARE else "ok",
                           f"{share * 100:.0f}% of the shots are '{top}': mix in other framings." if share > VARIETY_TOP_SHARE else "Good mix of shot types.",
                           {"top": top, "share": round(share, 2)}, ["search_footage_tool"] if share > VARIETY_TOP_SHARE else []))
    return out


# ============================ picture ============================
def _percentile(values: Sequence[float], p: float) -> float:
    xs = sorted(values)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def picture_findings(clips: Sequence[TimelineClip], brief: Brief, looks: Dict[str, Dict[str, Any]], index_for: Provider) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    pics: List[TimelineClip] = []
    for s in visible_shots(clips):
        if s["clip"] not in pics:                    # a clip split by an overlay is still one clip
            pics.append(s["clip"])
    measured = [(c, looks[c.id]) for c in pics if (looks.get(c.id) or {}).get("present")]
    if len(measured) >= 2:
        lumas = [float(p["avg_luma"]) for _, p in measured]
        warms = [float(p.get("warm_cool") or 0.0) for _, p in measured]
        ls = _percentile(lumas, 0.9) - _percentile(lumas, 0.1)
        ws = _percentile(warms, 0.9) - _percentile(warms, 0.1)
        ml, mw = statistics.median(lumas), statistics.median(warms)
        outliers = [{"clip": c.id, "name": c.name, "luma": round(float(p["avg_luma"]), 3), "warmth": round(float(p.get("warm_cool") or 0.0), 3)}
                    for c, p in measured if abs(float(p["avg_luma"]) - ml) > OUTLIER_MIN or abs(float(p.get("warm_cool") or 0.0) - mw) > WARM_OUTLIER_MIN]
        uneven = ls > LUMA_SPREAD or ws > WARMTH_SPREAD or bool(outliers)
        out.append(finding("consistency", "picture", "needs" if uneven else "ok",
                           "Clips differ noticeably in brightness or warmth." if uneven else "Brightness and warmth are consistent across the clips.",
                           {"measured_clips": len(measured), "luma_spread": round(ls, 3), "warmth_spread": round(ws, 3), "median_luma": round(ml, 3),
                            "median_warmth": round(mw, 3), "outliers": outliers[:10]},
                           ["harmonize_look_tool", "apply_color_tool", "match_color_to_reference_tool"] if uneven else []))
    else:
        out.append(finding("consistency", "picture", "unknown" if pics else "ok",
                           "Fewer than two clips were measured." if pics else "No picture to measure.", {"measured_clips": len(measured)}, ["analyze_frame_colors_tool"] if pics else []))

    problems = []
    for c, p in measured:
        ex = Q.exposure_of(p)
        if ex and ex["problems"]:
            problems.append({"clip": c.id, "name": c.name, "problems": ex["problems"], "luma": ex["avg_luma"]})
    out.append(finding("exposure", "picture", "needs" if problems else ("ok" if measured else "unknown"),
                       f"{len(problems)} clip(s) have exposure problems." if problems else ("No exposure problems found." if measured else "Exposure was not measured."),
                       {"clips": problems[:12]}, ["apply_color_tool", "color_grade_clip_tool"] if problems else []))

    risky = []
    for c in pics:
        idx = index_for(c)
        pipe = getattr(idx, "pipeline", None) or {}
        if pipe.get("hdr") and not any(e in ("ColorGrade", "LUT") for e in c.effects):
            risky.append({"clip": c.id, "name": c.name, "why": "HDR footage on an SDR timeline without a grade or LUT"})
    if risky:
        out.append(finding("colour_pipeline", "picture", "needs", f"{len(risky)} clip(s) are HDR and not converted: they will look washed out or clipped.",
                           {"clips": risky[:10]}, ["color_grade_clip_tool", "list_luts_tool"]))

    graded = [c for c in pics if any(e in ("ColorGrade", "LUT") for e in c.effects)]
    wants = brief.wants_grade if brief.wants_grade is not None else bool(brief.vibe)
    if not pics:
        pass
    elif graded:
        out.append(finding("style", "picture", "ok", f"{len(graded)} of {len(pics)} clips carry a grade.", {"graded": len(graded), "clips": len(pics)}))
    else:
        out.append(finding("style", "picture", "needs" if wants else "info",
                           "No grade is applied" + (f", but a look was asked for ({brief.vibe})." if brief.vibe else "; fine if the footage is already consistent and a natural look is wanted."),
                           {"graded": 0, "clips": len(pics), "vibe": brief.vibe}, ["apply_look_tool", "apply_color_tool", "harmonize_look_tool"]))
    return out


# ============================ sound ============================
def _bed_clips(clips: Sequence[TimelineClip]) -> List[TimelineClip]:
    return [c for c in clips if c.role in ("music", "sfx") and c.length > 0]


def sound_findings(clips: Sequence[TimelineClip], project: ProjectInfo, brief: Brief, index_for: Provider,
                   mix: Optional[Dict[str, Any]] = None, speech_windows: Optional[Dict[str, List[Tuple[float, float]]]] = None
                   ) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    shots = visible_shots(clips)
    duration = project.duration or (shots[-1]["end"] if shots else 0.0)
    beds = [c for c in _bed_clips(clips) if c.role == "music"]
    speech = [c for c in clips if c.role == "speech" and c.length > 0]
    want_music = music_expected(brief)
    coverage = _covered([(c.start, c.end) for c in beds], 0.0, duration) / duration if duration > 0 else 0.0
    if want_music is False:
        out.append(finding("music", "sound", "ok" if not beds else "info", "Music is not wanted here." if not beds else "Music is present although this form does not need it.", {"coverage": round(coverage, 2)}))
    elif coverage >= MUSIC_MIN_COVERAGE:
        out.append(finding("music", "sound", "ok", f"Music covers {coverage * 100:.0f}% of the edit.", {"coverage": round(coverage, 2), "beds": len(beds)}))
    else:
        status = "needs" if want_music else "info"
        out.append(finding("music", "sound", status, "There is no music bed." if not beds else f"Music covers only {coverage * 100:.0f}% of the edit.",
                           {"coverage": round(coverage, 2), "beds": len(beds), "expected": want_music},
                           ["find_music_tool", "audition_music_tool", "stock_music"] if status == "needs" or want_music is None else []))

    margins, unknown_levels = [], 0
    for sp in speech:
        for w0, w1 in (sp.windows or [(sp.start, sp.end)]):           # only while someone is speaking: the gaps are where the music comes back up
            for bed in beds:
                lo, hi = max(w0, bed.start, sp.start), min(w1, bed.end, sp.end)
                if hi - lo < 0.5:
                    continue
                s_level = source_level_db(sp, index_for(sp), sp.to_source(lo), sp.to_source(hi))
                b_level = source_level_db(bed, index_for(bed), bed.to_source(lo), bed.to_source(hi))
                if s_level is None or b_level is None:
                    unknown_levels += 1
                    continue
                margin = (s_level + sp.gain_over(lo, hi)) - (b_level + bed.gain_over(lo, hi))
                margins.append({"speech": sp.id, "bed": bed.id, "from": round(lo, 2), "to": round(hi, 2), "margin_db": round(margin, 1),
                                "automated_gain": sp.gain_fn is not None or bed.gain_fn is not None})
    if margins:
        worst = min(margins, key=lambda m: m["margin_db"])
        bad = [m for m in margins if m["margin_db"] < MARGIN_NEEDS_DB]
        out.append(finding("dialogue_margin", "sound", "needs" if bad else "ok",
                           f"Music sits only {worst['margin_db']:.0f} dB under the voice at {worst['from']:.0f}-{worst['to']:.0f} s (aim for {MARGIN_TARGET_DB:.0f} dB or more)." if bad
                           else f"Speech stays {worst['margin_db']:.0f} dB or more above the music.",
                           {"worst_db": worst["margin_db"], "overlaps": margins[:10], "target_db": MARGIN_TARGET_DB},
                           ["balance_mix_tool", "duck_under_speech_tool", "set_clip_volume_tool"] if bad else []))
    elif speech and beds:
        out.append(finding("dialogue_margin", "sound", "unknown", "Speech and music overlap but their levels are not indexed.", {"unindexed_overlaps": unknown_levels},
                           ["index_status_tool", "analyze_timeline_audio_tool"]))

    levels = [(c, source_level_db(c, index_for(c))) for c in sorted(speech, key=lambda c: c.start)]
    jumps = []
    for (a, la), (b, lb) in zip(levels, levels[1:]):
        if la is not None and lb is not None:
            diff = (lb + b.gain_over(b.start, b.end)) - (la + a.gain_over(a.start, a.end))
            if abs(diff) > LEVEL_JUMP_DB:
                jumps.append({"from_clip": a.id, "to_clip": b.id, "at": round(b.start, 2), "jump_db": round(diff, 1)})
    if len(speech) >= 2:
        out.append(finding("level_jumps", "sound", "needs" if jumps else "ok",
                           f"{len(jumps)} jump(s) in voice level between clips." if jumps else "Voice levels are even between clips.",
                           {"jumps": jumps[:10]}, ["balance_mix_tool", "set_clip_volume_tool"] if jumps else []))

    overlaps = [(a.id, b.id) for i, a in enumerate(speech) for b in speech[i + 1:] if min(a.end, b.end) - max(a.start, b.start) > 0.05]
    if overlaps:
        out.append(finding("speech_overlap", "sound", "needs", "Two voices play at once.", {"clips": overlaps[:8]}, ["move_clips_tool", "trim_clips_tool"]))

    if duration > 0:
        covered = _intervals_union([(c.start, c.end) for c in clips if c.has_audio and c.role not in (None, "silent") and c.length > 0])
        gaps, t = [], 0.0
        for a, b in covered:
            if a - t > DEAD_AIR_SECONDS:
                gaps.append([round(t, 2), round(a, 2)])
            t = max(t, b)
        if duration - t > DEAD_AIR_SECONDS:
            gaps.append([round(t, 2), round(duration, 2)])
        out.append(finding("dead_air", "sound", "needs" if gaps else "ok", f"{len(gaps)} stretch(es) with no sound at all." if gaps else "No dead air.",
                           {"gaps": gaps[:10]}, ["stock_music", "add_clip_to_timeline_tool", "remove_gaps_tool"] if gaps else []))

    target = loudness_target(brief)
    if mix and mix.get("integrated_lufs") is not None:
        lufs, peak = float(mix["integrated_lufs"]), mix.get("true_peak_db")
        off = lufs - target
        loud_bad = abs(off) > LOUDNESS_TOLERANCE_LU
        peak_bad = peak is not None and float(peak) > TRUE_PEAK_MAX_DB
        out.append(finding("loudness", "sound", "needs" if loud_bad or peak_bad else "ok",
                           f"The mix measures {lufs:.1f} LUFS (target {target:.0f})" + (f" with peaks at {float(peak):.1f} dB." if peak is not None else "."),
                           {"integrated_lufs": round(lufs, 1), "target_lufs": target, "off_lu": round(off, 1), "true_peak_db": peak, "lra": mix.get("lra")},
                           ["balance_mix_tool", "set_clip_volume_tool"] if loud_bad or peak_bad else []))
    else:
        out.append(finding("loudness", "sound", "unknown", "The mix was not rendered, so its loudness is unknown.", {"target_lufs": target}, ["review_edit_tool"]))

    cuts = [s["start"] for s in shots[1:]]
    tempo_beds = [(b, (getattr(index_for(b), "audio", None) or {}).get("tempo")) for b in beds]
    tempo_beds = [(b, t) for b, t in tempo_beds if t and t.get("beats")]
    if tempo_beds and len(cuts) >= MIN_CUTS_FOR_BEATS:
        bed, tempo = max(tempo_beds, key=lambda bt: bt[0].length)
        beats = [bed.to_timeline(x) for x in tempo["beats"] if bed.src_in <= x <= bed.src_out]
        beats = [x for x in beats if bed.start <= x <= bed.end]
        if beats:
            on = sum(1 for c in cuts if min(abs(c - b) for b in beats) <= BEAT_TOLERANCE)
            frac = on / len(cuts)
            drives = _has(brief.form, _BEAT_FORMS) or is_short_form(brief.form)
            out.append(finding("beat_sync", "sound", "ok" if frac >= BEAT_MIN_FRACTION else ("needs" if drives else "info"),
                               f"{frac * 100:.0f}% of the cuts land on a beat ({tempo['bpm']:.0f} BPM)." + ("" if frac >= BEAT_MIN_FRACTION else " Cutting on the beat makes it feel tight."),
                               {"cuts": len(cuts), "on_beat": on, "fraction": round(frac, 2), "bpm": tempo["bpm"], "tolerance_seconds": BEAT_TOLERANCE},
                               ["sync_cuts_to_beats_tool"] if frac < BEAT_MIN_FRACTION else []))
    return out


# ============================ text and delivery ============================
def text_findings(clips: Sequence[TimelineClip], brief: Brief, captions: Optional[Sequence[Tuple[float, float]]],
                  speech_ranges: Sequence[Tuple[float, float]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    total = sum(b - a for a, b in _intervals_union(speech_ranges))
    want = captions_expected(brief)
    if total > 0.5:
        covered = _covered(captions or [], 0.0, 1e9) if captions else 0.0
        inside = sum(_covered(captions or [], a, b) for a, b in _intervals_union(speech_ranges))
        frac = inside / total
        if want is False and not captions:
            out.append(finding("captions", "text", "ok", "Captions are not wanted here.", {}))
        elif frac >= 0.8:
            out.append(finding("captions", "text", "ok", f"Captions cover {frac * 100:.0f}% of the speech.", {"coverage": round(frac, 2)}))
        else:
            status = "needs" if want else "info"
            out.append(finding("captions", "text", status, "Speech has no captions." if not captions else f"Captions cover only {frac * 100:.0f}% of the speech.",
                               {"coverage": round(frac, 2), "speech_seconds": round(total, 1), "caption_seconds": round(covered, 1)},
                               ["add_captions_tool"]))
    titles = [c for c in clips if c.kind == "title"]
    out.append(finding("opening_title", "text", "ok" if any(t.start < 3.0 for t in titles) else "info",
                       "An opening title is on screen early." if any(t.start < 3.0 for t in titles) else "There is no opening title (optional).",
                       {"titles": len(titles)}, [] if any(t.start < 3.0 for t in titles) else ["add_title_tool"]))
    return out


def delivery_findings(project: ProjectInfo, brief: Brief) -> List[Dict[str, Any]]:
    if not vertical_expected(brief):
        return []
    portrait = project.height > project.width
    return [finding("format", "delivery", "ok" if portrait else "needs",
                    "The project is vertical." if portrait else f"A {brief.form} should be vertical (9:16); the project is {project.width}x{project.height}.",
                    {"width": project.width, "height": project.height}, [] if portrait else ["set_project_profile_tool"])]


# ============================ the whole audit ============================
_ORDER = {"needs": 0, "unknown": 1, "info": 2, "ok": 3}


def review(clips: Sequence[TimelineClip], project: ProjectInfo, brief: Brief, *, index_for: Provider,
           looks: Optional[Dict[str, Dict[str, Any]]] = None, mix: Optional[Dict[str, Any]] = None,
           captions: Optional[Sequence[Tuple[float, float]]] = None, take_groups: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """The audit: findings per layer, what needs work first, and what could not be measured."""
    speech_ranges = [(c.start, c.end) for c in clips if c.role == "speech" and c.length > 0]
    findings: List[Dict[str, Any]] = []
    findings += story_findings(clips, project, brief, index_for, take_groups)
    findings += picture_findings(clips, brief, looks or {}, index_for)
    findings += sound_findings(clips, project, brief, index_for, mix)
    findings += text_findings(clips, brief, captions, speech_ranges)
    findings += delivery_findings(project, brief)
    layers: Dict[str, List[Dict[str, Any]]] = {}
    for f in findings:
        layers.setdefault(f["layer"], []).append(f)
    needs = sorted((f for f in findings if f["status"] == "needs"), key=lambda f: _ORDER[f["status"]])
    unknown = [f["id"] for f in findings if f["status"] == "unknown"]
    return {"layers": layers, "needs": [f["id"] for f in needs], "unknown": unknown,
            "counts": {s: sum(1 for f in findings if f["status"] == s) for s in ("needs", "info", "unknown", "ok")}}
