"""
 @file
 @brief The style of an edit in numbers: how fast it cuts, how it moves between shots, how it sounds and looks.

 "Make it like this" needs more than a colour match. From the layers the index already keeps for a finished video this reads
 its pacing (shot lengths, cuts per minute, whether it speeds up), how it changes shots (hard cuts, dissolves, fades), how much
 of it is still, panning or handheld, how many cuts land on the beat, how much of it is speech, how loud it is, its look, and how
 much text is on screen. ``timeline_style`` reads the same things off the timeline being built, and ``compare_style`` says where
 they differ and which tool closes the gap. Everything is measured; what to copy is the editor's choice.
"""

from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional, Sequence

from classes.media_index.review import BEAT_TOLERANCE

PACE_WINDOW = 10.0
SHAPE_SHIFT = 0.25            # the last third cuts this much faster (or slower) than the first third: the edit speeds up (or slows down)
FASTER, SLOWER = 1.4, 0.7     # a timeline whose average shot is this many times the reference's is much slower (or faster)
BEAT_GAP = 0.3                # the reference lands this much more of its cuts on the beat than the timeline
LOUDNESS_GAP = 3.0            # LU
LOOK_GAPS = {"avg_luma": 0.12, "warm_cool": 0.1, "sat_proxy": 0.15}


def _shot_lengths(shots: Sequence[Dict[str, Any]]) -> List[float]:
    return [float(s["end"]) - float(s["start"]) for s in shots if not s.get("black") and float(s["end"]) > float(s["start"])]


def pacing_curve(cut_times: Sequence[float], duration: float, window: float = PACE_WINDOW) -> List[Dict[str, float]]:
    """Cuts per minute in each *window* seconds of the piece."""
    out: List[Dict[str, float]] = []
    t = 0.0
    while t < duration - 1e-6:
        end = min(duration, t + window)
        if duration - end < window / 2 - 1e-6 and end < duration:      # a sliver at the end joins the last full window
            end = duration
        n = sum(1 for c in cut_times if t <= c < end)
        out.append({"from": round(t, 2), "to": round(end, 2), "cuts_per_minute": round(n * 60.0 / max(1e-6, end - t), 1)})
        t = end
    return out


def pace_shape(curve: Sequence[Dict[str, float]]) -> str:
    """'accelerating', 'decelerating' or 'steady', from the first third of the piece against the last third."""
    if len(curve) < 3:
        return "steady"
    third = max(1, len(curve) // 3)
    first = statistics.fmean(c["cuts_per_minute"] for c in curve[:third])
    last = statistics.fmean(c["cuts_per_minute"] for c in curve[-third:])
    base = max(first, last, 1e-6)
    if (last - first) / base >= SHAPE_SHIFT:
        return "accelerating"
    if (first - last) / base >= SHAPE_SHIFT:
        return "decelerating"
    return "steady"


def _on_beat_share(cuts: Sequence[float], beats: Sequence[float]) -> Optional[float]:
    if not cuts or not beats:
        return None
    return round(sum(1 for c in cuts if min(abs(c - b) for b in beats) <= BEAT_TOLERANCE) / len(cuts), 3)


def _share(counter: Dict[str, float], total: float) -> Dict[str, float]:
    return {k: round(v / total, 3) for k, v in sorted(counter.items(), key=lambda kv: -kv[1]) if total > 0}


def edit_style(fi: Any) -> Dict[str, Any]:
    """The style of a finished video from its index (a ``library.FileIndex``)."""
    shots = list(fi.shots or [])
    duration = float(fi.duration or (shots[-1]["end"] if shots else 0.0))
    lengths = _shot_lengths(shots)
    live = [s for s in shots if not s.get("black")]
    cuts = [float(s["start"]) for s in live[1:]]
    out: Dict[str, Any] = {"seconds": round(duration, 2), "shots": len(live), "orientation": fi.orientation or None}
    if lengths:
        out["pacing"] = {"average_shot": round(statistics.fmean(lengths), 2), "median_shot": round(statistics.median(lengths), 2), "shortest": round(min(lengths), 2),
                         "longest": round(max(lengths), 2), "cuts_per_minute": round(len(cuts) * 60.0 / duration, 1) if duration > 0 else None}
        curve = pacing_curve(cuts, duration)
        out["pacing"]["shape"] = pace_shape(curve)
        out["pacing"]["curve"] = curve
    kinds: Dict[str, int] = {}
    for s in live[1:]:
        kind = (s.get("opens_with") or {}).get("kind")
        if kind in ("hard", "dissolve", "fade"):
            kinds[kind] = kinds.get(kind, 0) + 1
    out["transitions"] = kinds
    motion: Dict[str, float] = {}
    for s in live:
        motion[str((s.get("motion") or {}).get("class") or "unknown")] = motion.get(str((s.get("motion") or {}).get("class") or "unknown"), 0.0) + (s["end"] - s["start"])
    out["motion"] = _share(motion, sum(motion.values()))
    types: Dict[str, float] = {}
    for s in live:
        shot_type = str(((s.get("watch") or {}).get("shot_type")) or "")
        if shot_type:
            types[shot_type] = types.get(shot_type, 0.0) + (s["end"] - s["start"])
    if types:
        out["shot_types"] = _share(types, sum(types.values()))
    tempo = (fi.audio or {}).get("tempo") or {}
    out["music"] = {"bpm": round(float(tempo["bpm"]), 1) if tempo.get("bpm") else None, "cut_on_beat": _on_beat_share(cuts, tempo.get("beats") or []),
                    "sections": [{"label": s["label"], "from": s["start"], "to": s["end"]} for s in ((fi.audio or {}).get("music") or {}).get("sections", [])]}
    sentences = fi.sentences or []
    spoken = sum(max(0.0, float(s["end"]) - float(s["start"])) for s in sentences)
    words = sum(len(str(s.get("text") or "").split()) for s in sentences)
    out["speech"] = {"share": round(spoken / duration, 3) if duration > 0 else 0.0, "words_per_minute": round(words / (spoken / 60.0), 0) if spoken > 1 else None}
    out["sound"] = {"loudness_lufs": ((fi.audio or {}).get("loudness") or {}).get("integrated_lufs")}
    profile = (fi.look_file or {}).get("profile") or {}
    out["look"] = {k: profile.get(k) for k in ("avg_luma", "warm_cool", "sat_proxy", "contrast_span", "palette") if profile.get(k) is not None}
    out["look"]["hdr"] = bool((fi.pipeline or {}).get("hdr"))
    with_text = [s for s in live if (s.get("watch") or {}).get("on_screen_text")]
    out["text_on_screen"] = {"shots_with_text": round(len(with_text) / len(live), 3) if live else 0.0,
                             "seen": bool((fi.layers or {}).get("watch"))}
    return out


def timeline_style(pictures: Sequence[Any], beats: Sequence[float], seconds: float, transitions: int = 0) -> Dict[str, Any]:
    """The same measurements for the timeline being built: *pictures* are its picture clips (``review.TimelineClip``)."""
    ordered = sorted((c for c in pictures if c.end > c.start), key=lambda c: c.start)
    lengths = [c.end - c.start for c in ordered]
    cuts = [c.start for c in ordered[1:]]
    out: Dict[str, Any] = {"seconds": round(seconds, 2), "shots": len(ordered)}
    if lengths and seconds > 0:
        out["pacing"] = {"average_shot": round(statistics.fmean(lengths), 2), "median_shot": round(statistics.median(lengths), 2), "shortest": round(min(lengths), 2),
                         "longest": round(max(lengths), 2), "cuts_per_minute": round(len(cuts) * 60.0 / seconds, 1)}
        out["pacing"]["shape"] = pace_shape(pacing_curve(cuts, seconds))
    out["transitions"] = {"count": int(transitions)}
    out["music"] = {"cut_on_beat": _on_beat_share(cuts, beats), "has_beats": bool(beats)}
    return out


def _gap(aspect: str, reference: Any, yours: Any, note: str, advice: str, tools: Sequence[str] = ()) -> Dict[str, Any]:
    return {"aspect": aspect, "reference": reference, "yours": yours, "gap": note, "advice": advice, "tools": list(tools)}


def compare_style(reference: Dict[str, Any], yours: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Where the timeline differs from the reference by enough to matter, with what to do about it."""
    gaps: List[Dict[str, Any]] = []
    ref_p, my_p = reference.get("pacing") or {}, yours.get("pacing") or {}
    if ref_p.get("average_shot") and my_p.get("average_shot"):
        ratio = my_p["average_shot"] / ref_p["average_shot"]
        if ratio >= FASTER:
            gaps.append(_gap("pacing", f"{ref_p['average_shot']} s average shot", f"{my_p['average_shot']} s", f"your shots are {ratio:.1f}x as long",
                             "cut faster: shorten the longest shots, or cut to the music", ("sync_cuts_to_beats_tool", "get_clip_context_tool")))
        elif ratio <= SLOWER:
            gaps.append(_gap("pacing", f"{ref_p['average_shot']} s average shot", f"{my_p['average_shot']} s", f"your shots are {ratio:.1f}x as long",
                             "let shots breathe: use fewer, longer shots, or hold on the strongest ones"))
        if ref_p.get("shape") in ("accelerating", "decelerating") and my_p.get("shape") != ref_p["shape"]:
            gaps.append(_gap("pace shape", ref_p["shape"], my_p.get("shape"), "the reference " + ref_p["shape"] + ", yours does not",
                             "shorten shots toward the end" if ref_p["shape"] == "accelerating" else "lengthen shots toward the end"))
    ref_beat, my_beat = (reference.get("music") or {}).get("cut_on_beat"), (yours.get("music") or {}).get("cut_on_beat")
    if ref_beat is not None and ref_beat >= 0.5 and (my_beat is None or ref_beat - my_beat >= BEAT_GAP):
        gaps.append(_gap("cuts on the beat", f"{ref_beat * 100:.0f}%", None if my_beat is None else f"{my_beat * 100:.0f}%", "the reference cuts to the music, yours less so",
                         "add music with a steady beat and put the cuts on it", ("audition_music_tool", "sync_cuts_to_beats_tool")))
    ref_tr = reference.get("transitions") or {}
    soft = ref_tr.get("dissolve", 0) + ref_tr.get("fade", 0)
    total = sum(ref_tr.values())
    if total and soft / total >= 0.3 and not (yours.get("transitions") or {}).get("count"):
        gaps.append(_gap("transitions", f"{soft} of {total} shot changes are dissolves or fades", "none", "the reference blends between shots, yours cuts",
                         "add dissolves or fades where the reference has them", ("generate_transition_clip_tool",)))
    ref_l = (reference.get("sound") or {}).get("loudness_lufs")
    my_l = (yours.get("sound") or {}).get("loudness_lufs")
    if ref_l is not None and my_l is not None and abs(ref_l - my_l) >= LOUDNESS_GAP:
        gaps.append(_gap("loudness", f"{ref_l:.1f} LUFS", f"{my_l:.1f} LUFS", f"{abs(ref_l - my_l):.0f} LU apart", f"set the mix to about {ref_l:.0f} LUFS", ("balance_mix_tool",)))
    ref_look, my_look = reference.get("look") or {}, yours.get("look") or {}
    for key, limit in LOOK_GAPS.items():
        a, b = ref_look.get(key), my_look.get(key)
        if a is not None and b is not None and abs(a - b) >= limit:
            gaps.append(_gap("look: " + key, round(a, 3), round(b, 3), f"differs by {abs(a - b):.2f}", "match the reference's look", ("match_reference_tool", "harmonize_look_tool")))
    if (reference.get("text_on_screen") or {}).get("shots_with_text", 0) >= 0.3 and not (yours.get("text_on_screen") or {}).get("shots_with_text"):
        gaps.append(_gap("text on screen", f"{reference['text_on_screen']['shots_with_text'] * 100:.0f}% of shots", "none", "the reference puts text on screen, yours does not",
                         "add titles or captions", ("add_captions_tool",)))
    return gaps
