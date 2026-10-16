"""
 @file
 @brief The exact edges of a voice, the pauses inside it, and takes of the same line said more than once.

 Word times from a recogniser are good to a few tenths of a second and say nothing about where the sound really starts or
 stops. ``voice_edges`` reads the audio of a range, finds the first and last instant the voice is above the room's noise, and
 reports the pauses between. ``retake_groups`` reads the transcript and groups sentences that are the same line (or a false
 start of it) said again, with measured facts about each take (fillers, completeness, level, what follows). Both are measured:
 they say what the audio and the transcript contain, and leave the choice of take to the editor.
"""

from __future__ import annotations

import difflib
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from classes.ffmpeg_cli import run_ffmpeg
from classes.speech.word_ranges import FILLER_PRESETS, normalize_word

SAMPLE_RATE = 16000
FRAME_SECONDS = 0.025
HOP_SECONDS = 0.010
MAX_RANGE_SECONDS = 180.0
MIN_CONTRAST_DB = 8.0          # the voice must stand this far above the quietest parts of the range, or there is no voice to find
THRESHOLD_SHARE = 0.35         # of the way from the noise floor to the voice level
MIN_VOICED_SECONDS = 0.08      # a burst shorter than this (the analysis frame already smears a click to about 50 ms) is not speech
BRIDGE_SECONDS = 0.12          # gaps shorter than this are inside a word
MIN_PAUSE_SECONDS = 0.25
SEARCH_BEFORE = 0.35           # where to look for the onset and the end around the recogniser's first and last word
SEARCH_AFTER = 0.45


# ============================ the voice, from the audio ============================
def read_audio(path: str, start: float, end: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Mono float samples of [start, end) of *path*'s audio. Raises RuntimeError when ffmpeg cannot decode it."""
    start = max(0.0, float(start))
    length = min(MAX_RANGE_SECONDS, max(0.05, float(end) - start))
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    proc = run_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError((proc.stderr or b"could not decode the audio").decode("utf-8", "replace")[-200:])
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)


def frame_db(samples: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Loudness (dB, relative) of every ``HOP_SECONDS`` step, from ``FRAME_SECONDS`` frames."""
    frame, hop = int(FRAME_SECONDS * sr), int(HOP_SECONDS * sr)
    if samples.size < frame:
        return np.zeros(0)
    count = 1 + (samples.size - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
    power = np.mean(samples[idx].astype(np.float64) ** 2, axis=1) + 1e-12
    return 10.0 * np.log10(power)


def _runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """(first, last+1) of each stretch of True."""
    out: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, on in enumerate(mask):
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def voiced_spans(db: np.ndarray, hop: float = HOP_SECONDS) -> Tuple[List[Tuple[float, float]], Dict[str, float]]:
    """Stretches of voice in a loudness curve, in seconds from its start, and what was measured to find them."""
    if db.size < 5:
        return [], {"floor_db": 0.0, "voice_db": 0.0, "threshold_db": 0.0, "contrast_db": 0.0}
    floor, level = float(np.percentile(db, 10)), float(np.percentile(db, 95))
    facts = {"floor_db": round(floor, 1), "voice_db": round(level, 1), "contrast_db": round(level - floor, 1)}
    if level - floor < MIN_CONTRAST_DB:
        return [], {**facts, "threshold_db": round(floor, 1)}
    threshold = floor + max(MIN_CONTRAST_DB / 2, THRESHOLD_SHARE * (level - floor))
    facts["threshold_db"] = round(threshold, 1)
    runs = _runs(db >= threshold)
    merged: List[List[int]] = []
    for a, b in runs:
        if merged and (a - merged[-1][1]) * hop < BRIDGE_SECONDS:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    spans = [(a * hop, b * hop) for a, b in merged if (b - a) * hop >= MIN_VOICED_SECONDS]
    return spans, facts


def voice_edges(samples: np.ndarray, offset: float = 0.0, sr: int = SAMPLE_RATE, words: Optional[Sequence[Dict[str, Any]]] = None,
                within: Optional[Tuple[float, float]] = None) -> Dict[str, Any]:
    """Where the voice starts and stops in *samples* (which begin at *offset* seconds of the file), and the pauses inside it.

    With *words* (the recogniser's, with ``startSec``/``endSec``) the search is held to the neighbourhood of the first and last
    word, so a door slamming or music before the line is not taken for it, and the result also says how far the recogniser's
    timing was from the sound. Times are in the file's own seconds. ``found`` is False when nothing stands out from the noise.
    """
    db = frame_db(samples, sr)
    lo_t, hi_t = 0.0, db.size * HOP_SECONDS
    word_start = word_end = None
    if words:
        word_start, word_end = float(words[0]["startSec"]) - offset, float(words[-1]["endSec"]) - offset
        lo_t, hi_t = max(0.0, word_start - SEARCH_BEFORE), min(db.size * HOP_SECONDS, word_end + SEARCH_AFTER)
    window = db[int(lo_t / HOP_SECONDS):int(hi_t / HOP_SECONDS)]
    spans, facts = voiced_spans(window)
    if not spans:
        return {"found": False, **facts, "method": "energy"}
    base = lo_t + FRAME_SECONDS / 2          # a frame's loudness belongs to its middle
    spans = [(a + base, b + base) for a, b in spans]
    if within is not None:                   # the audio read had extra around the range for context: only voice in the range counts
        spans = [(a, b) for a, b in spans if offset + b > within[0] and offset + a < within[1]]
        if not spans:
            return {"found": False, **facts, "method": "energy"}
    pauses = [[round(offset + a, 3), round(offset + b, 3)] for (_, a), (b, _) in zip(spans[:-1], spans[1:]) if b - a >= MIN_PAUSE_SECONDS]
    out: Dict[str, Any] = {"found": True, "start": round(offset + spans[0][0], 3), "end": round(offset + spans[-1][1], 3),
                           "pauses": pauses, "spans": [[round(offset + a, 3), round(offset + b, 3)] for a, b in spans], "method": "energy", **facts}
    if word_start is not None and word_end is not None:
        out["recogniser_start"], out["recogniser_end"] = round(offset + word_start, 3), round(offset + word_end, 3)
        out["start_offset"] = round(out["start"] - out["recogniser_start"], 3)
        out["end_offset"] = round(out["end"] - out["recogniser_end"], 3)
    return out


# ============================ takes of the same line ============================
TERMINAL = (".", "?", "!")
MAX_RETAKE_GAP = 120.0
MIN_TOKENS = 4
SIMILARITY = 0.75
PREFIX_SIMILARITY = 0.85
MAX_SENTENCES = 2500


def tokens(text: str) -> List[str]:
    return [t for t in (normalize_word(w) for w in str(text or "").split()) if t]


def same_line(a: Sequence[str], b: Sequence[str], threshold: float = SIMILARITY) -> Optional[str]:
    """Why two token lists are one line said twice ('repeat' or 'false_start'), or None."""
    if len(a) < MIN_TOKENS or len(b) < MIN_TOKENS:
        return None
    if difflib.SequenceMatcher(None, a, b).ratio() >= threshold:
        return "repeat"
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= MIN_TOKENS - 1 and len(short) < len(long_) and difflib.SequenceMatcher(None, short, long_[:len(short)]).ratio() >= PREFIX_SIMILARITY:
        return "false_start"
    return None


def fillers_in(words: Sequence[Dict[str, Any]], start: float, end: float, preset: str = "um_uh") -> List[Dict[str, Any]]:
    bag = FILLER_PRESETS.get(preset, frozenset())
    return [{"word": str(w.get("text") or "").strip(), "start": round(float(w["startSec"]), 3), "end": round(float(w["endSec"]), 3)} for w in words
            if start - 1e-6 <= float(w["startSec"]) and float(w["endSec"]) <= end + 1e-6 and normalize_word(str(w.get("text") or "")) in bag]


def retake_groups(sentences: Sequence[Dict[str, Any]], words: Sequence[Dict[str, Any]] = (), *, threshold: float = SIMILARITY,
                  level_db: Optional[Callable[[float, float], Optional[float]]] = None) -> List[Dict[str, Any]]:
    """Groups of sentences that are the same line said more than once, each take described by what can be measured about it.

    A group has ``takes`` in time order. Each take carries its fillers, whether it ended like a finished sentence, how fast it
    was spoken, its level, and the pause after it. ``likely_best`` names the take with the fewest fillers that finished its
    sentence, the later one on a tie, and says why: a starting point for the editor's choice, not a verdict.
    """
    rows = [s for s in sentences[:MAX_SENTENCES] if tokens(s.get("text"))]
    toks = [tokens(s["text"]) for s in rows]
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    reason: Dict[Tuple[int, int], str] = {}
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if float(rows[j]["start"]) - float(rows[i]["end"]) > MAX_RETAKE_GAP:
                break
            si, sj = rows[i].get("speaker"), rows[j].get("speaker")
            if si and sj and si != sj:
                continue
            why = same_line(toks[i], toks[j], threshold)
            if why:
                parent[find(j)] = find(i)
                reason[(i, j)] = why
    groups: Dict[int, List[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    out: List[Dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda k: float(rows[k]["start"]))
        takes = []
        for n, k in enumerate(members):
            s = rows[k]
            a, b = float(s["start"]), float(s["end"])
            nxt = float(rows[k + 1]["start"]) if k + 1 < len(rows) else None
            spoken = [w for w in words if a - 1e-6 <= float(w["startSec"]) and float(w["endSec"]) <= b + 1e-6]
            fillers = fillers_in(words, a, b)
            take = {"index": n + 1, "start": round(a, 3), "end": round(b, 3), "seconds": round(b - a, 2), "text": str(s["text"]),
                    "words": len(spoken) or len(toks[k]), "fillers": fillers, "complete": str(s["text"]).rstrip().endswith(TERMINAL),
                    "words_per_second": round((len(spoken) or len(toks[k])) / max(0.1, b - a), 2), "pause_after": None if nxt is None else round(max(0.0, nxt - b), 2)}
            if level_db is not None:
                lv = level_db(a, b)
                take["level_db"] = None if lv is None else round(lv, 1)
            takes.append(take)
        finished = [t for t in takes if t["complete"]] or takes
        fewest = min(len(t["fillers"]) for t in finished)
        best = [t for t in finished if len(t["fillers"]) == fewest][-1]
        kinds = {reason.get((i, j)) for i in members for j in members if (i, j) in reason}
        out.append({"line": takes[-1]["text"], "kind": "false_start" if kinds == {"false_start"} else "repeat", "takes": takes,
                    "likely_best": best["index"],
                    "why": (f"take {best['index']} has {'no' if not fewest else fewest} filler word(s)" + (" and finishes its sentence" if best["complete"] else "")
                            + (", and is the later of the equally clean takes" if sum(1 for t in finished if len(t["fillers"]) == fewest) > 1 else ""))})
    out.sort(key=lambda g: g["takes"][0]["start"])
    return out
