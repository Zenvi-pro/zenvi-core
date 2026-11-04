"""
 @file
 @brief A moment pack: everything known about ONE range of one file, bounded, never a dump of the library.

 Pure functions over a loaded file index. The tool adds the pictures (frames, a spectrogram when it is worth it) and the
 timeline context; this module chooses and trims the facts so a pack stays a size an agent can read in one go.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from classes.media_index import dossier, spectro

MAX_RANGE_SECONDS = 60.0
MAX_SHOTS = 12
MAX_NOTES_CHARS = 2500
MAX_WORDS_CHARS = 1200
DEFAULT_FRAMES = 8
MAX_FRAMES = 12
MAX_TIMELINE_USES = 3


def check_range(start: Optional[float], end: Optional[float], duration: float = 0.0) -> tuple:
    """(start, end) held to the file; raises ValueError saying what to change."""
    if start is None or end is None:
        raise ValueError("give start_seconds and end_seconds: a moment is one range, not a whole file")
    lo, hi = max(0.0, float(start)), float(end)
    if duration:
        hi = min(hi, float(duration))
    if hi - lo < 0.1:
        raise ValueError("that range is empty or outside the file")
    if hi - lo > MAX_RANGE_SECONDS:
        raise ValueError(f"a moment is at most {MAX_RANGE_SECONDS:.0f} s ({hi - lo:.0f} s asked for): pick the part you need, or use search_footage_tool to find it")
    return lo, hi


def shots_in(fi: Any, lo: float, hi: float) -> Dict[str, Any]:
    """The shots touching [lo, hi) with their measured quality and look, trimmed to ``MAX_SHOTS``."""
    chosen = fi.shots_between(lo, hi)
    rows: List[Dict[str, Any]] = []
    for s in chosen[:MAX_SHOTS]:
        q = s.get("quality") or {}
        w = s.get("watch") or {}
        rows.append({"id": s.get("id"), "start": round(float(s["start"]), 3), "end": round(float(s["end"]), 3),
                     "inside": [round(max(lo, float(s["start"])), 3), round(min(hi, float(s["end"])), 3)],
                     "description": w.get("description") or None, "shot_type": w.get("shot_type") or None, "motion": (s.get("motion") or {}).get("class"),
                     "quality": {"flags": q.get("flags") or [], "score": q.get("score"), "sharpness": q.get("sharpness"), "shake": q.get("shake")} if q else None,
                     "look": dossier._look_line(s.get("look"), s.get("look_extras")) or None, "black": bool(s.get("black"))})
    return {"shots": rows, "total": len(chosen), "truncated": max(0, len(chosen) - MAX_SHOTS) or None}


def words_in(words: Sequence[Dict[str, Any]], lo: float, hi: float, max_chars: int = MAX_WORDS_CHARS) -> Dict[str, Any]:
    """The transcript inside [lo, hi): text, the first and last word time, cut at *max_chars* with a note of where it stopped."""
    mine = [w for w in words if float(w["endSec"]) > lo and float(w["startSec"]) < hi]
    if not mine:
        return {"text": "", "count": 0, "first": None, "last": None, "truncated_at": None}
    text, used, last = [], 0, mine[-1]
    cut = None
    for w in mine:
        piece = str(w.get("text") or "").strip()
        if used + len(piece) + 1 > max_chars:
            cut = round(float(w["startSec"]), 3)
            last = mine[len(text) - 1] if text else w
            break
        text.append(piece)
        used += len(piece) + 1
    return {"text": " ".join(text), "count": len(mine), "first": round(float(mine[0]["startSec"]), 3), "last": round(float(last["endSec"]), 3), "truncated_at": cut}


def audio_picture_worth(sections: Sequence[Dict[str, Any]], lo: float, hi: float, duration: float, asked: bool) -> Dict[str, Any]:
    """Whether a spectrogram belongs in the pack: when asked for, or when the range holds a section edge the numbers are unsure of."""
    suggested = spectro.suggest(sections, duration)
    inside = [r for r in (suggested or {}).get("ranges", []) if r[1] > lo and r[0] < hi]
    if asked:
        return {"include": True, "why": "asked for"}
    if inside:
        return {"include": True, "why": "a section edge here is uncertain (gradual build or fade): the picture shows where the change really is"}
    return {"include": False, "why": "the numbers (loudness, beats, words) cover this range; ask for audio_picture if you want to see it"}


def timeline_uses(clips: Sequence[Any], file_id: str, lo: float, hi: float) -> List[Any]:
    """Timeline clips that play some of [lo, hi) of this file, in timeline order, at most ``MAX_TIMELINE_USES``."""
    mine = [c for c in clips if str(c.file_id) == str(file_id) and c.src_out > lo and c.src_in < hi]
    return sorted(mine, key=lambda c: c.start)[:MAX_TIMELINE_USES]


def notes(fi: Any, lo: float, hi: float, max_chars: int = MAX_NOTES_CHARS) -> str:
    """The dossier text for the range without the file header."""
    d = dossier.build_dossier(fi, lo, hi, max_chars=max_chars)
    lines = [ln for ln in d["text"].splitlines() if not ln.startswith(("FILE ", "AUDIO "))]
    return "\n".join(lines)
