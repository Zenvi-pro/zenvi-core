"""Before/after timeline snapshots for mutation receipts.

Pure data: no Qt / libopenshot. Callers pass clip dicts already loaded from
the project store. Frame numbers come from ``frame_time`` so receipts report
the same grid Phase 2 writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Optional

from classes.agent_tools.receipt import (
    MUTATION_CLIP_LIMIT,
    SHIFT_GROUP_MINIMUM,
    ToolReceipt,
)
from classes.frame_time import duration_frames, to_frame, to_seconds


@dataclass(frozen=True)
class ClipPlacement:
    clip_id: str
    track: int
    start_frame: int
    duration_frames: int
    position: float
    start: float
    end: float
    layer: int = 0
    title: str = ""

    def same_place(self, other: "ClipPlacement") -> bool:
        return (
            self.track == other.track
            and self.start_frame == other.start_frame
            and self.duration_frames == other.duration_frames
        )


@dataclass
class TimelineSnapshot:
    placements: dict[str, ClipPlacement]
    track_ids: list[str]
    markers: dict[str, dict]


def _clip_fields(clip: dict, fps: Fraction) -> ClipPlacement:
    cid = str(clip.get("id") or "")
    position = float(clip.get("position") or 0.0)
    start = float(clip.get("start") or 0.0)
    end = float(clip.get("end") if clip.get("end") is not None else start)
    layer = int(clip.get("layer") or 0)
    track = int(clip.get("_track_index") if clip.get("_track_index") is not None else layer)
    start_frame = to_frame(position, fps)
    dur = duration_frames(start, end, fps)
    title = str(clip.get("title") or clip.get("label") or "")
    return ClipPlacement(
        clip_id=cid,
        track=track,
        start_frame=start_frame,
        duration_frames=dur,
        position=position,
        start=start,
        end=end,
        layer=layer,
        title=title,
    )


def timeline_snapshot(
    clips: Iterable[dict],
    *,
    fps: Fraction | float | int,
    track_ids: Optional[list[str]] = None,
    markers: Optional[Iterable[dict]] = None,
    layer_to_track: Optional[dict[int, int]] = None,
) -> TimelineSnapshot:
    """Build a snapshot from raw project clip dicts."""
    rate = fps if isinstance(fps, Fraction) else Fraction(fps)
    placements: dict[str, ClipPlacement] = {}
    for raw in clips:
        clip = dict(raw)
        layer = int(clip.get("layer") or 0)
        if layer_to_track is not None and "_track_index" not in clip:
            clip["_track_index"] = layer_to_track.get(layer, layer)
        placement = _clip_fields(clip, rate)
        if placement.clip_id:
            placements[placement.clip_id] = placement
    marker_map = {}
    for m in markers or []:
        mid = str(m.get("id") or "")
        if mid:
            marker_map[mid] = dict(m)
    return TimelineSnapshot(
        placements=placements,
        track_ids=list(track_ids or []),
        markers=marker_map,
    )


def _clip_dict(
    placement: ClipPlacement,
    *,
    requested_frame: Optional[int] = None,
    landed_frame: Optional[int] = None,
) -> dict:
    out = {
        "id": placement.clip_id,
        "track": placement.track,
        "layer": placement.layer,
        "position": placement.position,
        "start": placement.start,
        "end": placement.end,
        "durationFrames": placement.duration_frames,
        "landedFrame": landed_frame if landed_frame is not None else placement.start_frame,
        "title": placement.title,
    }
    if requested_frame is not None:
        out["requestedFrame"] = requested_frame
    return out


def mutation_result(
    before: TimelineSnapshot,
    after: TimelineSnapshot,
    *,
    tool: str,
    summary: str,
    status: str = "applied",
    touched: Optional[Iterable[str]] = None,
    requested_frames: Optional[dict[str, int]] = None,
    extra_notes: Optional[list[str]] = None,
    warnings: Optional[list[str]] = None,
    watch_suggested: Optional[dict] = None,
    data: Any = None,
    undo_steps: int = 1,
) -> ToolReceipt:
    """Diff two snapshots into a ToolReceipt (Palmier mutationResult shape + frames)."""
    touched_set = set(touched or [])
    changed: set[str] = set(cid for cid in touched_set if cid in after.placements)
    changed.update(cid for cid in after.placements if cid not in before.placements)

    pure_shifts: dict[str, tuple[int, int]] = {}
    for cid, placement in after.placements.items():
        prior = before.placements.get(cid)
        if prior is None or prior.same_place(placement) or cid in changed:
            continue
        if prior.track == placement.track and prior.duration_frames == placement.duration_frames:
            pure_shifts[cid] = (prior.start_frame, placement.start_frame - prior.start_frame)
        else:
            changed.add(cid)

    shifts: list[dict] = []
    grouped: dict[str, list[str]] = {}
    for cid in pure_shifts:
        key = f"{after.placements[cid].track}|{pure_shifts[cid][1]}"
        grouped.setdefault(key, []).append(cid)
    for ids in grouped.values():
        if len(ids) >= SHIFT_GROUP_MINIMUM:
            first_from, delta = pure_shifts[ids[0]]
            shifts.append({
                "track": after.placements[ids[0]].track,
                "fromFrame": min(pure_shifts[i][0] for i in ids),
                "by": delta,
                "count": len(ids),
            })
        else:
            changed.update(ids)
    shifts.sort(key=lambda s: (s["track"], s["fromFrame"]))

    removed = sorted(cid for cid in before.placements if cid not in after.placements)

    created_tracks: list[dict] = []
    before_tracks = set(before.track_ids)
    for i, tid in enumerate(after.track_ids):
        if tid not in before_tracks:
            created_tracks.append({"index": i, "id": tid})

    removed_markers = sorted(mid for mid in before.markers if mid not in after.markers)
    marker_changes = [
        after.markers[mid]
        for mid in after.markers
        if mid not in before.markers or after.markers[mid] != before.markers.get(mid)
    ]

    req = requested_frames or {}
    clip_rows = []
    for cid in sorted(changed):
        placement = after.placements.get(cid)
        if placement is None:
            continue
        clip_rows.append(
            _clip_dict(
                placement,
                requested_frame=req.get(cid),
                landed_frame=placement.start_frame,
            )
        )

    clips_note = None
    if len(clip_rows) > MUTATION_CLIP_LIMIT:
        clips_note = f"Showing {MUTATION_CLIP_LIMIT} of {len(clip_rows)} changed clips."
        clip_rows = clip_rows[:MUTATION_CLIP_LIMIT]

    if status == "unchanged":
        undo_steps = 0
    if status in ("error", "refused"):
        undo_steps = 0

    return ToolReceipt(
        status=status,  # type: ignore[arg-type]
        tool=tool,
        summary=summary,
        clips=clip_rows,
        clipsNote=clips_note,
        shifted=shifts,
        removedClipIds=removed,
        createdTracks=created_tracks,
        markers=marker_changes,
        removedMarkerIds=removed_markers,
        warnings=list(warnings or []),
        notes=list(extra_notes or []),
        watchSuggested=watch_suggested,
        undoSteps=undo_steps,
        data=data,
    )


def requested_vs_landed(
    requested_seconds: float,
    landed_seconds: float,
    fps: Fraction | float | int,
) -> dict[str, int]:
    """Helper for placement tools that know both requested and landed times."""
    rate = fps if isinstance(fps, Fraction) else Fraction(fps)
    return {
        "requestedFrame": to_frame(requested_seconds, rate),
        "landedFrame": to_frame(landed_seconds, rate),
        "requestedSeconds": float(to_seconds(to_frame(requested_seconds, rate), rate)),
        "landedSeconds": float(to_seconds(to_frame(landed_seconds, rate), rate)),
    }
