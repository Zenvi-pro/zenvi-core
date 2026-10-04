"""
 @file
 @brief What is under, above and around a clip, and what an edit to it would move.

 Pure arithmetic over the timeline as plain data (``review.TimelineClip`` plus track and transition facts); nothing here
 reads the project or changes it. ``clip_context`` describes the clip's surroundings. ``predict_edit`` answers "if I delete,
 trim or lengthen it with the clips after it closing up (a ripple), which clips shift, by how much, and what stops lining
 up" without doing it. Links (the video and audio of one recording) and sync-locked tracks are read from the fields the pro
 editing tools add (``link_group_id`` on a clip, ``sync_locked`` on a track); where they are absent the answer says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from classes.media_index.review import BEAT_TOLERANCE, TimelineClip

PICTURE_KINDS = ("video", "image", "title", "caption")
OPAQUE = 0.95                  # a picture clip this opaque hides what is under it
SAME_SPOT = 0.05               # seconds: clips this close are touching
SPEECH_EDGE = 0.15
MAX_LISTED = 12


@dataclass
class Track:
    layer: int
    label: str = ""
    locked: bool = False
    sync_locked: Optional[bool] = None       # None = the project has no such setting


@dataclass
class Transition:
    id: str
    layer: int
    start: float
    end: float
    title: str = ""


def overlap(a0: float, a1: float, b0: float, b1: float) -> Tuple[float, float]:
    return (max(a0, b0), min(a1, b1))


def _merge(spans: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for a, b in sorted(s for s in spans if s[1] > s[0]):
        if out and a <= out[-1][1] + 1e-6:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _brief(c: TimelineClip) -> Dict[str, Any]:
    return {"id": c.id, "name": c.name, "layer": c.layer, "kind": c.kind, "start": round(c.start, 3), "end": round(c.end, 3)}


def _find(clips: Sequence[TimelineClip], clip_id: str) -> TimelineClip:
    for c in clips:
        if c.id == clip_id:
            return c
    raise KeyError(clip_id)


def sync_locked_layers(tracks: Mapping[int, Track], target_layer: int) -> Tuple[List[int], bool]:
    """Layers whose later clips move when the target ripples, and whether the project has the setting at all.

    Without the setting only the target's own track moves. With it, every track that is sync-locked (a track without a value
    counts as locked, as in the pro editing tools) moves, plus the target's own.
    """
    configured = any(t.sync_locked is not None for t in tracks.values())
    if not configured:
        return [target_layer], False
    layers = {target_layer} | {layer for layer, t in tracks.items() if t.sync_locked is not False and not t.locked}
    return sorted(layers), True


def clip_context(clip_id: str, clips: Sequence[TimelineClip], tracks: Mapping[int, Track] = (), transitions: Sequence[Transition] = (),
                 opacity: Optional[Mapping[str, float]] = None, links: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Everything around one clip. *links* maps clip id to link group id, or is None when the project has no links."""
    tracks = dict(tracks) if not isinstance(tracks, dict) else tracks
    opacity = opacity or {}
    target = _find(clips, clip_id)
    others = [c for c in clips if c.id != target.id]

    def stack(candidates: Sequence[TimelineClip], above: bool) -> List[Dict[str, Any]]:
        rows = []
        for c in candidates:
            lo, hi = overlap(target.start, target.end, c.start, c.end)
            if hi - lo <= 0.01 or c.kind not in PICTURE_KINDS or (c.layer > target.layer) != above or c.layer == target.layer:
                continue
            row = _brief(c)
            row.update(overlap=[round(lo, 3), round(hi, 3)], opacity=round(float(opacity.get(c.id, 1.0)), 2), hides=bool(opacity.get(c.id, 1.0) >= OPAQUE and c.kind != "caption"))
            rows.append(row)
        rows.sort(key=lambda r: -r["layer"])             # the top of the stack first
        return rows[:MAX_LISTED]

    above, below = stack(others, True), stack(others, False)
    hidden = _merge([(r["overlap"][0], r["overlap"][1]) for r in above if r["hides"]]) if target.kind in PICTURE_KINDS else []
    hidden_seconds = sum(b - a for a, b in hidden)
    same = sorted((c for c in others if c.layer == target.layer), key=lambda c: c.start)
    prev = max((c for c in same if c.start < target.start), key=lambda c: c.end, default=None)
    nxt = min((c for c in same if c.start >= target.start), key=lambda c: c.start, default=None)

    def seam(left: TimelineClip, right: TimelineClip) -> Dict[str, Any]:
        gap = right.start - left.end
        tr = next((t for t in transitions if t.layer == target.layer and t.start <= right.start + 0.5 and t.end >= left.end - 0.5), None)
        kind = ("transition: " + (tr.title or tr.id)) if tr else ("overlap" if gap < -SAME_SPOT else "gap" if gap > SAME_SPOT else "hard cut")
        return {"gap_seconds": round(gap, 3), "kind": kind}

    before = None if prev is None else {**_brief(prev), **seam(prev, target)}
    after = None if nxt is None else {**_brief(nxt), **seam(target, nxt)}
    sound = []
    for c in others:
        lo, hi = overlap(target.start, target.end, c.start, c.end)
        if c.has_audio and hi - lo > 0.01:
            sound.append({**_brief(c), "role": c.role, "level_db": None if c.gain_db is None else round(c.gain_db, 1), "overlap": [round(lo, 3), round(hi, 3)],
                          "automated_volume": c.gain_db is None})
    track = tracks.get(target.layer)
    out: Dict[str, Any] = {
        "clip": {**_brief(target), "role": target.role, "has_audio": target.has_audio, "effects": target.effects,
                 "opacity": round(float(opacity.get(target.id, 1.0)), 2)},
        "track": {"layer": target.layer, "label": track.label if track else "", "locked": bool(track.locked) if track else False,
                  "sync_locked": track.sync_locked if track else None},
        "above": above, "below": below,
        "visible": {"hidden_by_clips_above": [[round(a, 3), round(b, 3)] for a, b in hidden], "hidden_seconds": round(hidden_seconds, 3),
                    "fully_hidden": bool(target.length > 0 and hidden_seconds >= 0.98 * target.length)} if target.kind in PICTURE_KINDS else None,
        "before": before, "after": after, "sound_with": sound[:MAX_LISTED],
    }
    if links is None:
        out["links"] = {"available": False, "note": "this project has no clip links (the pro editing tools add them); only overlaps, neighbours and sound are shown"}
    else:
        group = links.get(target.id)
        partners = [_brief(c) for c in others if group and links.get(c.id) == group]
        out["links"] = {"available": True, "group": group or None, "partners": partners}
    return out


def predict_edit(clip_id: str, op: str, seconds: float, clips: Sequence[TimelineClip], tracks: Mapping[int, Track] = (),
                 links: Optional[Mapping[str, str]] = None, beats: Sequence[float] = (), speech: Sequence[Tuple[float, float]] = ()) -> Dict[str, Any]:
    """What would move if *op* (delete, trim_end, trim_start, lengthen) were done to a clip with the clips after it closing up.

    ``seconds`` is how much to trim or lengthen. Nothing is changed. *beats* and *speech* are timeline times: the beat grid
    of the music and the windows where a voice speaks; they say which cuts would stop landing on a beat or start landing
    inside speech.
    """
    if op not in ("delete", "trim_end", "trim_start", "lengthen"):
        raise ValueError(f"unknown edit {op!r}")
    tracks = dict(tracks) if not isinstance(tracks, dict) else tracks
    target = _find(clips, clip_id)
    if op != "delete" and seconds <= 0:
        raise ValueError("say how many seconds to trim or lengthen")
    if op in ("trim_end", "trim_start") and seconds >= target.length:
        raise ValueError("that would remove the whole clip: use delete")
    shift = {"delete": -target.length, "trim_end": -seconds, "trim_start": -seconds, "lengthen": seconds}[op]
    layers, configured = sync_locked_layers(tracks, target.layer)
    locked = [t.layer for t in tracks.values() if t.locked]
    refused = target.layer in locked
    moving: List[TimelineClip] = []
    for c in clips:
        if refused or c.id == target.id or c.layer not in layers or c.layer in locked:
            continue
        if c.start >= target.end - SAME_SPOT:
            moving.append(c)
    moved_ids = {c.id for c in moving}
    shifted = [{**_brief(c), "from": round(c.start, 3), "to": round(c.start + shift, 3), "shift": round(shift, 3)} for c in sorted(moving, key=lambda c: c.start)]
    stay_put = [_brief(c) for c in clips if c.id != target.id and c.id not in moved_ids and c.start < target.end and c.end > target.start
                and (c.layer not in layers or c.layer in locked)]
    split_links: List[Dict[str, Any]] = []
    if links is not None:
        changed = moved_ids | ({target.id} if op == "delete" else set())
        for c in clips:
            group = links.get(c.id)
            if not group or c.id not in changed:
                continue
            for partner in clips:
                if partner.id != c.id and links.get(partner.id) == group and partner.id not in changed:
                    split_links.append({"clip": c.id, "partner": partner.id, "partner_name": partner.name})
    picture_cuts = [c.start for c in moving if c.kind in PICTURE_KINDS]
    lost_beats = gained_speech = 0
    for t in picture_cuts:
        on_beat_now = bool(beats) and min(abs(t - b) for b in beats) <= BEAT_TOLERANCE
        on_beat_later = bool(beats) and min(abs(t + shift - b) for b in beats) <= BEAT_TOLERANCE
        lost_beats += int(on_beat_now and not on_beat_later)
        inside_now = any(a + SPEECH_EDGE < t < z - SPEECH_EDGE for a, z in speech)
        inside_later = any(a + SPEECH_EDGE < t + shift < z - SPEECH_EDGE for a, z in speech)
        gained_speech += int(inside_later and not inside_now)
    notes: List[str] = []
    if refused:
        notes.append("this clip's track is locked: the edit would be refused until the track is unlocked")
    if not configured:
        notes.append("the project has no sync-lock setting: only this clip's own track is predicted to move")
    if stay_put:
        notes.append(f"{len(stay_put)} clip(s) on other tracks keep their place and may no longer line up with what changes here")
    if split_links:
        notes.append(f"{len(split_links)} linked clip(s) would be left behind: their picture and sound would go out of sync")
    if lost_beats:
        notes.append(f"{lost_beats} cut(s) that land on a beat now would no longer land on one (sync_cuts_to_beats_tool can fix that)")
    if gained_speech:
        notes.append(f"{gained_speech} cut(s) would land in the middle of speech")
    return {"edit": op, "seconds": round(abs(shift), 3), "shift": round(shift, 3), "clip": _brief(target), "shifted": shifted[:MAX_LISTED * 4],
            "shifted_count": len(shifted), "stay_put": stay_put[:MAX_LISTED], "split_links": split_links[:MAX_LISTED],
            "cuts_off_the_beat": lost_beats, "cuts_into_speech": gained_speech, "tracks_that_move": [] if refused else layers, "sync_lock_known": configured, "refused_locked_track": refused, "notes": notes}
