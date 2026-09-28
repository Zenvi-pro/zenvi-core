"""Pure helpers: word indices → source ranges → compacted clip fragments."""

from __future__ import annotations

from typing import Iterable, Sequence

from classes.frame_time import snap, to_frame, to_seconds


FILLER_PRESETS: dict[str, frozenset[str]] = {
    "um_uh": frozenset({"um", "uh", "umm", "uhh", "uhm"}),
    "english_fillers": frozenset({
        "um", "uh", "umm", "uhh", "uhm", "er", "ah", "like", "youknow", "you", "know",
    }),
}


def normalize_word(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def merge_ranges(
    ranges: Sequence[tuple[float, float]],
    *,
    fps,
) -> list[tuple[float, float]]:
    """Merge overlapping / touching source-second ranges (frame-snapped)."""
    cleaned: list[tuple[float, float]] = []
    for a, b in ranges:
        try:
            s, e = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if not (e > s):
            continue
        s = snap(s, fps)
        e = snap(e, fps)
        if e <= s:
            e = to_seconds(to_frame(s, fps) + 1, fps)
        cleaned.append((s, e))
    if not cleaned:
        return []
    cleaned.sort(key=lambda r: r[0])
    out = [cleaned[0]]
    for s, e in cleaned[1:]:
        ps, pe = out[-1]
        if s <= pe + 1e-9:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


def ranges_for_word_indices(
    words: Sequence[dict],
    indices: Iterable[int],
    *,
    fps,
) -> list[tuple[float, float]]:
    """Build merged source ranges covering the given word indices."""
    by_index = {}
    for w in words:
        try:
            idx = int(w["index"])
        except (KeyError, TypeError, ValueError):
            continue
        by_index[idx] = w
    ranges: list[tuple[float, float]] = []
    for raw in indices:
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            continue
        w = by_index.get(idx)
        if w is None:
            continue
        try:
            s = float(w.get("startSec") if "startSec" in w else w.get("sourceStart"))
            e = float(w.get("endSec") if "endSec" in w else w.get("sourceEnd"))
        except (TypeError, ValueError):
            continue
        if e > s:
            ranges.append((s, e))
    return merge_ranges(ranges, fps=fps)


def indices_for_filler_preset(
    words: Sequence[dict],
    preset: str,
) -> list[int]:
    bag = FILLER_PRESETS.get(preset)
    if bag is None:
        return []
    out: list[int] = []
    for w in words:
        try:
            idx = int(w["index"])
        except (KeyError, TypeError, ValueError):
            continue
        token = normalize_word(str(w.get("text") or ""))
        if token in bag:
            out.append(idx)
    return out


def compact_fragments_after_remove(
    *,
    position: float,
    start: float,
    end: float,
    remove_ranges: Sequence[tuple[float, float]],
    fps,
) -> tuple[list[tuple[float, float, float]], float]:
    """Return (fragments, removed_duration_sec).

    Each fragment is ``(timeline_position, source_start, source_end)`` packed
    back-to-back from the original *position* with removed gaps closed.
    """
    clip_start = snap(float(start), fps)
    clip_end = snap(float(end), fps)
    if clip_end <= clip_start:
        return [], 0.0

    merged = merge_ranges(
        [
            (max(clip_start, float(a)), min(clip_end, float(b)))
            for a, b in remove_ranges
            if float(b) > float(a)
        ],
        fps=fps,
    )
    # Invert to kept source intervals.
    kept: list[tuple[float, float]] = []
    cursor = clip_start
    for s, e in merged:
        if s > cursor:
            kept.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < clip_end:
        kept.append((cursor, clip_end))

    original_dur = to_seconds(to_frame(clip_end, fps) - to_frame(clip_start, fps), fps)
    if not kept:
        return [], original_dur

    fragments: list[tuple[float, float, float]] = []
    pos = snap(float(position), fps)
    kept_dur = 0.0
    for s, e in kept:
        # Drop empty / sub-frame fragments.
        if to_frame(e, fps) <= to_frame(s, fps):
            continue
        fragments.append((pos, s, e))
        dur = to_seconds(to_frame(e, fps) - to_frame(s, fps), fps)
        kept_dur += dur
        pos = snap(pos + dur, fps)

    removed = max(0.0, original_dur - kept_dur)
    return fragments, removed
