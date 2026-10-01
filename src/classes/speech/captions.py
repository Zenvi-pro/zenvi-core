"""Caption phrasing + SRT/VTT round-trip (frame-snapped)."""

from __future__ import annotations

import re
from typing import Any, Sequence

from classes.frame_time import snap, to_frame, to_seconds


def _timeline_sec(word: dict, edge: str, fps) -> float:
    """A word's start or end on the timeline (*edge* is "Start" or "End")."""
    sec = word.get(f"timeline{edge}Sec")
    if sec is not None:
        return float(sec)
    frame = word.get(f"{edge.lower()}Frame")
    if frame is not None:
        return float(to_seconds(int(frame), fps))
    return float(word.get(f"{edge.lower()}Sec") or 0)


def phrase_words(
    words: Sequence[dict],
    *,
    max_words: int = 8,
    max_chars: int = 42,
    max_gap_sec: float = 0.8,
    fps,
) -> list[dict[str, Any]]:
    """Group word rows into caption cues.

    Input words need ``text``, ``startSec``/``endSec`` (source) and preferably
    ``startFrame``/``endFrame`` (timeline). Output cues use timeline frames.
    """
    cues: list[dict[str, Any]] = []
    if not words:
        return cues

    bucket: list[dict] = []

    def _flush():
        nonlocal bucket
        if not bucket:
            return
        text = " ".join(str(w.get("text") or "").strip() for w in bucket).strip()
        if not text:
            bucket = []
            return
        s0 = bucket[0]
        s1 = bucket[-1]
        start_f = int(s0.get("startFrame") if s0.get("startFrame") is not None
                      else to_frame(_timeline_sec(s0, "Start", fps), fps))
        end_f = int(s1.get("endFrame") if s1.get("endFrame") is not None
                    else to_frame(_timeline_sec(s1, "End", fps), fps))
        if end_f <= start_f:
            end_f = start_f + 1
        cues.append({
            "text": text,
            "startFrame": start_f,
            "endFrame": end_f,
            "startSec": to_seconds(start_f, fps),
            "endSec": to_seconds(end_f, fps),
            "wordIndices": [int(w["index"]) for w in bucket if "index" in w],
        })
        bucket = []

    for w in words:
        text = str(w.get("text") or "").strip()
        if not text:
            continue
        if not bucket:
            bucket = [w]
            continue
        prev = bucket[-1]
        # Timeline time: source time restarts in each clip and skips cut words.
        gap = _timeline_sec(w, "Start", fps) - _timeline_sec(prev, "End", fps)
        chars = len(" ".join(str(x.get("text") or "") for x in bucket) + " " + text)
        if gap > max_gap_sec or len(bucket) >= max_words or chars > max_chars:
            _flush()
            bucket = [w]
        else:
            bucket.append(w)
    _flush()
    return cues


def _ts_srt(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def cues_to_srt(cues: Sequence[dict], *, fps) -> str:
    lines: list[str] = []
    for i, cue in enumerate(cues, start=1):
        if "startSec" in cue:
            t0 = float(cue["startSec"])
            t1 = float(cue["endSec"])
        else:
            t0 = to_seconds(int(cue["startFrame"]), fps)
            t1 = to_seconds(int(cue["endFrame"]), fps)
        if t1 <= t0:
            t1 = t0 + to_seconds(1, fps)
        lines.append(str(i))
        lines.append(f"{_ts_srt(t0)} --> {_ts_srt(t1)}")
        lines.append(str(cue.get("text") or "").strip())
        lines.append("")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def cues_to_vtt(cues: Sequence[dict], *, fps) -> str:
    def _ts(sec: float) -> str:
        srt = _ts_srt(sec).replace(",", ".")
        return srt

    out = ["WEBVTT", ""]
    for cue in cues:
        if "startSec" in cue:
            t0 = float(cue["startSec"])
            t1 = float(cue["endSec"])
        else:
            t0 = to_seconds(int(cue["startFrame"]), fps)
            t1 = to_seconds(int(cue["endFrame"]), fps)
        out.append(f"{_ts(t0)} --> {_ts(t1)}")
        out.append(str(cue.get("text") or "").strip())
        out.append("")
    return "\n".join(out)


_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _parse_ts(h, m, s, frac) -> float:
    milli = int((frac + "000")[:3])
    return int(h) * 3600 + int(m) * 60 + int(s) + milli / 1000.0


def parse_srt_or_vtt(text: str, *, fps) -> list[dict[str, Any]]:
    """Parse SRT or VTT into frame-snapped cues."""
    body = text.lstrip("\ufeff")
    if body.lstrip().upper().startswith("WEBVTT"):
        body = re.sub(r"^WEBVTT[^\n]*\n", "", body, count=1, flags=re.IGNORECASE)
    cues: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", body.strip())
    for block in blocks:
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        # Skip numeric index line
        if lines[0].isdigit() and len(lines) >= 2:
            lines = lines[1:]
        if not lines:
            continue
        m = _TIME_RE.search(lines[0])
        if not m:
            continue
        t0 = _parse_ts(*m.groups()[0:4])
        t1 = _parse_ts(*m.groups()[4:8])
        t0 = snap(t0, fps)
        t1 = snap(t1, fps)
        if t1 <= t0:
            t1 = to_seconds(to_frame(t0, fps) + 1, fps)
        caption = " ".join(lines[1:]).strip()
        if not caption:
            continue
        cues.append({
            "text": caption,
            "startSec": t0,
            "endSec": t1,
            "startFrame": to_frame(t0, fps),
            "endFrame": to_frame(t1, fps),
        })
    return cues
