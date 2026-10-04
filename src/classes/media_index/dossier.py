"""
 @file
 @brief Everything the index knows about a file (or a stretch of it) as compact text for an agent.

 The dossier is how an agent gets "heavy context" without looking at frames: a header with the
 file's facts, then each shot in time order with what happens, what is said, how the camera moves,
 how it looks and sounds. It is bounded: past ``max_chars`` it stops and says where to continue.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from classes.media_index.library import FileIndex


def _t(sec: float) -> str:
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def _look_line(profile: Optional[Dict[str, Any]], extras: Optional[Dict[str, Any]]) -> str:
    if not profile or not profile.get("present"):
        return ""
    bits = [f"luma {profile.get('avg_luma', 0):.2f}", f"warmth {profile.get('warm_cool', 0):+.2f}"]
    sat = ((extras or {}).get("saturation") or {}).get("mean")
    if sat is not None:
        bits.append(f"sat {sat:.2f}")
    if profile.get("contrast_span") is not None:
        bits.append(f"contrast {profile['contrast_span']:.2f}")
    palette = [c["hex"] for c in ((extras or {}).get("palette") or [])[:3]]
    if palette:
        bits.append("palette " + " ".join(palette))
    return ", ".join(bits)


def _speaker(sentence: Dict[str, Any]) -> str:
    who = sentence.get("speaker")
    return f"{who}: " if who else ""


def header(fi: FileIndex) -> List[str]:
    lines = [f"FILE {fi.name or fi.file_id or fi.sha[:10]} ({fi.media_type}, {_t(fi.duration)} long"
             + (f", {fi.orientation}" if fi.orientation else "") + f", {len(fi.shots)} shots)"]
    audio = fi.audio or {}
    loud = (audio.get("loudness") or {})
    bits = []
    if loud.get("integrated_lufs") is not None:
        bits.append(f"loudness {loud['integrated_lufs']:.1f} LUFS")
    tempo = audio.get("tempo")
    if tempo:
        bits.append(f"tempo {tempo['bpm']:.0f} BPM ({len(tempo.get('beats') or [])} beats)")
    if audio.get("silence_ranges"):
        bits.append("silent " + ", ".join(f"{_t(a)}-{_t(b)}" for a, b in audio["silence_ranges"][:4]))
    if bits:
        lines.append("AUDIO " + "; ".join(bits))
    file_look = _look_line((fi.look_file or {}).get("profile"), (fi.look_file or {}).get("extras"))
    if file_look:
        lines.append("LOOK " + file_look)
    for w in fi.warnings:
        lines.append("WARNING " + w.split(":")[0])
    missing = [name for name in ("structure", "look", "audio", "speech", "watch", "vectors") if not fi.layers.get(name)]
    if missing:
        lines.append("NOT INDEXED YET: " + ", ".join(missing))
    return lines


def shot_lines(fi: FileIndex, shot: Dict[str, Any]) -> str:
    w = shot.get("watch") or {}
    head = f"[{_t(shot['start'])}-{_t(shot['end'])}] shot {shot['id']}"
    tags = [x for x in (w.get("shot_type"), (shot.get("motion") or {}).get("class"), w.get("mood")) if x and x != "unknown"]
    if tags:
        head += " (" + ", ".join(tags) + ")"
    if shot.get("black"):
        head += " BLACK"
    parts = [head + (": " + w["description"] if w.get("description") else "")]
    if w.get("actions"):
        parts.append("  does: " + "; ".join(w["actions"]))
    labels = list(dict.fromkeys(o["label"] for o in w.get("objects") or [] if o.get("label")))
    if labels:
        parts.append("  sees: " + ", ".join(labels))
    texts = list(dict.fromkeys(o["text"] for o in w.get("on_screen_text") or [] if o.get("text")))
    if texts:
        parts.append("  text: " + "; ".join(f'"{x}"' for x in texts))
    sounds = list(dict.fromkeys(o["label"] for o in w.get("sound_events") or [] if o.get("label")))
    if sounds:
        parts.append("  hears: " + ", ".join(sounds))
    said = [s for s in fi.sentences if s["end"] > shot["start"] and s["start"] < shot["end"]]
    if said:
        parts.append("  says: " + " ".join(f"{_speaker(s)}\"{s['text']}\"" for s in said))
    look = _look_line(shot.get("look"), shot.get("look_extras"))
    if look:
        parts.append("  look: " + look)
    return "\n".join(parts)


def build_dossier(fi: FileIndex, start: Optional[float] = None, end: Optional[float] = None, max_chars: int = 6000) -> Dict[str, Any]:
    """The dossier text for [start, end) of *fi* (the whole file when both are None)."""
    lo = 0.0 if start is None else float(start)
    hi = float(fi.duration or (fi.shots[-1]["end"] if fi.shots else 0.0)) if end is None else float(end)
    lines = header(fi)
    chosen = fi.shots_between(lo, hi)
    used = sum(len(x) + 1 for x in lines)
    shown = 0
    next_start = None
    for shot in chosen:
        block = shot_lines(fi, shot)
        if used + len(block) + 1 > max_chars and shown > 0:
            next_start = shot["start"]
            lines.append(f"... {len(chosen) - shown} more shots; continue from {_t(shot['start'])} (start={shot['start']:.1f})")
            break
        lines.append(block)
        used += len(block) + 1
        shown += 1
    if not chosen:
        lines.append("(no analysed shots in this range)")
    return {"text": "\n".join(lines), "shots_shown": shown, "shots_in_range": len(chosen), "next_start": next_start}
