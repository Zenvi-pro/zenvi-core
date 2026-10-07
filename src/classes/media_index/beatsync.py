"""
 @file
 @brief Move the cuts of an edit onto the beat of its music, without moving anything else.

 Pure planning over plain clip records. A *cut* is where one clip ends and the next on the same track begins.
 To move it to a beat, the earlier clip's out point and the later clip's in point both shift by the same amount,
 so the picture on each side of the cut stays continuous, every other edge stays where it is, and the total
 length does not change. A cut is left alone (with the reason) when the source has no spare footage to give,
 when a clip would become too short, when the clip carries speech (a cut there could land mid-word), when
 the clip's speed is changed, or when the clips overlap (a transition).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_MAX_SHIFT = 0.25
DEFAULT_MIN_LENGTH = 0.3


@dataclass
class CutClip:
    id: str
    layer: int
    start: float                     # timeline seconds
    end: float
    src_in: float                    # source seconds
    src_out: float
    speed: float = 1.0
    max_src: Optional[float] = None  # length of the source; None = unlimited (a still image)
    speech: bool = False

    @property
    def length(self) -> float:
        return self.end - self.start


def _nearest(beats: Sequence[float], t: float) -> Optional[float]:
    if not beats:
        return None
    i = bisect.bisect_left(beats, t)
    options = [beats[j] for j in (i - 1, i) if 0 <= j < len(beats)]
    return min(options, key=lambda b: abs(b - t))


def plan_beat_sync(clips: Sequence[CutClip], beats: Sequence[float], *, max_shift: float = DEFAULT_MAX_SHIFT,
                   min_length: float = DEFAULT_MIN_LENGTH, frame: float = 1.0 / 30.0, include_speech: bool = False
                   ) -> Dict[str, Any]:
    """Which cuts move where. Returns {"moves", "skipped", "already_on_beat", "cuts"}; the input is not changed."""
    tol = frame / 2.0
    ordered = sorted(beats)
    state: Dict[str, CutClip] = {c.id: replace(c) for c in clips}
    by_layer: Dict[int, List[str]] = {}
    for c in sorted(clips, key=lambda c: (c.layer, c.start)):
        by_layer.setdefault(c.layer, []).append(c.id)

    moves: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    on_beat = cuts = 0
    for layer, ids in by_layer.items():
        for a_id, b_id in zip(ids[:-1], ids[1:]):
            a, b = state[a_id], state[b_id]
            gap = b.start - a.end
            if gap < -tol:
                skipped.append({"between": [a.id, b.id], "at": round(b.start, 3), "why": "the clips overlap (a transition): left alone"})
                continue
            if gap > tol:
                continue                                    # a gap, not a cut
            cuts += 1
            at = a.end
            beat = _nearest(ordered, at)
            if beat is None or abs(beat - at) > max_shift + 1e-9:
                skipped.append({"between": [a.id, b.id], "at": round(at, 3), "why": f"no beat within {max_shift:.2f} s"})
                continue
            target = round(beat / frame) * frame
            delta = target - at
            if abs(delta) <= tol:
                on_beat += 1
                continue
            if abs(a.speed - 1.0) > 1e-6 or abs(b.speed - 1.0) > 1e-6:
                skipped.append({"between": [a.id, b.id], "at": round(at, 3), "why": "a clip's speed is changed: left alone"})
                continue
            if not include_speech and (a.speech or b.speech):
                skipped.append({"between": [a.id, b.id], "at": round(at, 3), "why": "a speaking clip: a cut there could land mid-word"})
                continue
            if delta > 0:                                   # cut later: A runs longer (needs source after it), B loses its head
                if a.max_src is not None and a.src_out + delta > a.max_src + tol:
                    skipped.append({"between": [a.id, b.id], "at": round(at, 3), "why": "the earlier clip has no spare source after its end"})
                    continue
                if b.length - delta < min_length:
                    skipped.append({"between": [a.id, b.id], "at": round(at, 3), "why": "the later clip would become too short"})
                    continue
            else:                                           # cut earlier: A loses its tail, B starts sooner (needs source before it)
                if a.length + delta < min_length:
                    skipped.append({"between": [a.id, b.id], "at": round(at, 3), "why": "the earlier clip would become too short"})
                    continue
                if b.src_in + delta < -tol:
                    skipped.append({"between": [a.id, b.id], "at": round(at, 3), "why": "the later clip has no spare source before its start"})
                    continue
            a.end += delta
            a.src_out += delta
            b.start += delta
            b.src_in += delta
            moves.append({"layer": layer, "from": round(at, 3), "to": round(target, 3), "delta": round(delta, 3), "earlier": a.id, "later": b.id,
                          "earlier_source_out": round(a.src_out, 4), "later_source_in": round(b.src_in, 4), "later_position": round(b.start, 4)})
    return {"moves": moves, "skipped": skipped, "already_on_beat": on_beat, "cuts": cuts}
