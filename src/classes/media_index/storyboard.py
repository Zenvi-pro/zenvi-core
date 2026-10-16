"""
 @file
 @brief A video as its shots: which shots a page of the storyboard holds, and the one instant that stands for each.

 Pure functions (no Qt, no ffmpeg) so the paging and the picture times are tested without a file.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from classes.media_index import contact
from classes.media_index.library import FileIndex


def page(shots: List[Dict[str, Any]], start: Optional[float], per_page: int = contact.MAX_TILES) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """(the shots of this page, the start to ask for the next page or None) for shots at or after *start*."""
    later = [s for s in shots if start is None or float(s["end"]) > float(start)]
    here, rest = later[:per_page], later[per_page:]
    return here, (float(rest[0]["start"]) if rest else None)


def keyframe_time(shot: Dict[str, Any], frame: float = 0.0) -> float:
    """The instant that stands for a shot: its middle, but never past its last frame."""
    start, end = float(shot["start"]), float(shot["end"])
    last = max(start, end - (frame if frame > 0 else 0.04))
    return round(min(last, (start + end) / 2.0), 3)


MAX_LIST_SHOTS = 300      # a whole feature-length cut fits one call as one-line rows; longer files continue with next_start


def _clock(sec: float) -> str:
    sec = max(0.0, float(sec))
    return f"{int(sec // 60)}:{sec % 60:05.2f}"


def list_line(fi: FileIndex, shot: Dict[str, Any], max_chars: int = 110, look: bool = False) -> str:
    """One row for a shot, with its own exact time: ``S12 0:05.50-0:05.90 (0.4s) close, handheld: what happens ...``.

    A row per shot is what lets an agent keep every cut, however short, instead of merging a run of quick shots into one range.
    """
    watch = shot.get("watch") or {}
    kind = ", ".join(x for x in (watch.get("shot_type"), camera(shot)) if x and x != "unknown")
    said = " ".join(f'"{s["text"]}"' for s in fi.sentences if s["end"] > shot["start"] and s["start"] < shot["end"])
    text = str(watch.get("description") or "").strip().split(". ")[0].rstrip(".")
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    line = f"S{shot['id']} {_clock(shot['start'])}-{_clock(shot['end'])} ({float(shot['end']) - float(shot['start']):.1f}s)"
    line += (" " + kind if kind else "") + opens(shot) + (": " + text if text else "") + unsure(watch)
    if shot.get("black"):
        line += " [black]"
    if look:
        line += look_tag(shot)
    return line + (f" says {said}" if said else "")


def opens(shot: Dict[str, Any]) -> str:
    """`` [fade in]`` when a shot is not reached by a plain cut ("cut" is the rule, so it is not repeated on every row)."""
    kind = str((shot.get("opens_with") or {}).get("kind") or "")
    return f" [{kind} in]" if kind and kind != "hard" else ""


def cut_summary(fi: FileIndex, on_beat: float = 0.06) -> str:
    """One line: how the shots are joined, and how many cuts land on a beat (a cut is on a beat when within *on_beat* seconds)."""
    kinds: Dict[str, int] = {}
    for shot in fi.shots[1:]:
        kind = str((shot.get("opens_with") or {}).get("kind") or "hard")
        kinds[kind] = kinds.get(kind, 0) + 1
    if not kinds:
        return ""
    text = "CUTS " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
    tempo = (fi.audio or {}).get("tempo") or {}
    beats = [float(b) for b in tempo.get("beats") or []]
    if beats and fi.shots[1:]:
        hits = sum(1 for shot in fi.shots[1:] if min(abs(float(shot["start"]) - b) for b in beats) <= on_beat)
        text += f"; {hits} of {len(fi.shots) - 1} land on a beat ({float(tempo.get('bpm') or 0):.0f} BPM)"
    return text


_HEDGE = re.compile(r"\b(may|might|appears?|seems?|possibly|probably|likely|looks? like|resembl\w+|unclear|perhaps|or object|indistinct|blurry|unidentified)\b", re.I)


def unsure(watch: Dict[str, Any]) -> str:
    """`` [unsure]`` when the description itself hedges ("resembling a mouth", "a package or object"): look at that shot's picture."""
    return " [unsure]" if _HEDGE.search(str(watch.get("description") or "")) else ""


def camera(shot: Dict[str, Any], min_confidence: float = 0.3) -> str:
    """The camera move with its speed when it was measured with some confidence: ``pan left 0.26/s``, ``zoom_in 0.26/s``.

    Speeds are fractions of the frame a second (a pan of 0.26/s crosses a quarter of the picture in a second).
    """
    motion = shot.get("motion") or {}
    kind = str(motion.get("class") or "")
    if float(motion.get("confidence") or 0.0) < min_confidence:
        return kind
    if kind == "pan" and motion.get("direction"):
        return f"pan {motion['direction']} {abs(float(motion.get('pan_speed') or 0.0)):.2f}/s"
    if kind == "tilt" and motion.get("direction"):
        return f"tilt {motion['direction']} {abs(float(motion.get('tilt_speed') or 0.0)):.2f}/s"
    if kind in ("zoom_in", "zoom_out"):
        return f"{kind} {abs(float(motion.get('zoom_rate') or 0.0)):.2f}/s"
    return kind


def look_tag(shot: Dict[str, Any]) -> str:
    """`` | luma 0.30 #0b1116 #ede9e9 #413e3b``: how bright the shot is and its three main colours (measured)."""
    profile = shot.get("look") or {}
    if not profile.get("present"):
        return ""
    palette = " ".join(c["hex"] for c in ((shot.get("look_extras") or {}).get("palette") or [])[:3] if c.get("hex"))
    return f" | luma {float(profile.get('avg_luma') or 0.0):.2f}" + (" " + palette if palette else "")


def repeats_line(fi: FileIndex, threshold: float = 0.85, max_pairs: int = 12) -> str:
    """Pairs of shots, not next to each other, whose pictures look nearly the same (the same set, the same prop): worth reusing or matching.

    Measured on the saved picture vectors (free). 0.85 is about the top 2% of pairs in a 90 s video; the same two fish at the start
    and end score 0.86 and 0.87. Empty when the picture layer was not built.
    """
    rows, matrix = fi.image_rows, fi.image_matrix
    if matrix is None or not len(rows) or not fi.shots:
        return ""
    n = len(fi.shots)
    index = {int(s["id"]): k for k, s in enumerate(fi.shots)}
    total = np.zeros((n, matrix.shape[1]), dtype=np.float32)
    for row, vec in zip(rows, matrix):
        if int(row.get("shot", -1)) in index:
            total[index[int(row["shot"])]] += vec / (np.linalg.norm(vec) + 1e-9)
    norms = np.linalg.norm(total, axis=1, keepdims=True)
    total = total / np.maximum(norms, 1e-9)
    sim = total @ total.T
    pairs = [(float(sim[i, j]), i, j) for i in range(n) for j in range(i + 3, n) if norms[i] > 0 and norms[j] > 0 and sim[i, j] >= threshold]
    pairs.sort(reverse=True)
    if not pairs:
        return ""
    ids = [int(s["id"]) for s in fi.shots]
    return "REPEATS (near-identical pictures): " + ", ".join(f"S{ids[i]}~S{ids[j]} {v:.2f}" for v, i, j in sorted(pairs[:max_pairs], key=lambda p: p[1]))


def music_line(fi: FileIndex) -> str:
    """The song's sections (steady, build, peak...) with the shots they cover, from the audio analysis."""
    sections = ((fi.audio or {}).get("music") or {}).get("sections") or []
    if not sections or not fi.shots:
        return ""
    parts = []
    for sec in sections:
        inside = [int(s["id"]) for s in fi.shots if float(s["start"]) >= float(sec["start"]) - 1e-6 and float(s["start"]) < float(sec["end"]) - 1e-6]
        span = f" S{inside[0]}-S{inside[-1]}" if inside else ""
        parts.append(f"{_clock(sec['start'])[:-1]}-{_clock(sec['end'])[:-1]} {sec.get('label', '?')}{span}")
    return "MUSIC " + "; ".join(parts)


def rhythm_line(fi: FileIndex) -> str:
    """How fast the picture cuts: the average and median shot, how many are under a second, and whether it speeds up or slows down."""
    lengths = [float(s["end"]) - float(s["start"]) for s in fi.shots]
    if len(lengths) < 6:
        return ""
    third = len(lengths) // 3
    parts = [float(np.mean(lengths[:third])), float(np.mean(lengths[third:2 * third])), float(np.mean(lengths[2 * third:]))]
    return (f"RHYTHM average shot {float(np.mean(lengths)):.1f}s (median {float(np.median(lengths)):.1f}s, shortest {min(lengths):.1f}s, longest {max(lengths):.1f}s); "
            f"{sum(1 for x in lengths if x < 1.0)} of {len(lengths)} under a second; first, middle, last third average {parts[0]:.1f}s, {parts[1]:.1f}s, {parts[2]:.1f}s")


def speech_line(fi: FileIndex) -> str:
    """Names the language and size of the transcript when it was made in another language than the machine's (a song's lyrics)."""
    if not fi.speech_retried or not fi.speech_words:
        return ""
    return (f"SPEECH {fi.speech_language or fi.speech_retried}: {fi.speech_words} words, transcribed in that language "
            "(a machine transcription of singing or speech: it may contain mistakes)")
