"""Pure timeline edit operations (no Qt, no libopenshot).

All new Phase 7 NLE math lives here. UI and agent handlers call these
functions and apply the returned patches — never a second copy of roll/slide
math in a tool.

Receipt schema version: ``edit_ops/v1``. Same keys on every path.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from classes import frame_time as ft

RECEIPT_VERSION = "edit_ops/v1"
NUDGE_DEFAULT_FRAMES = 8
NUDGE_MAX_FRAMES = 12
STATUS_APPLIED = "applied"
STATUS_NO_OP = "no_op"
STATUS_REFUSED = "refused"
STATUS_RECOVERED = "recovered"

_CLIP_KEYS = ("id", "position", "start", "end", "layer", "link_group_id")


def new_link_group_id() -> str:
    return str(uuid.uuid4())


def receipt(
    *,
    ok: bool,
    op: str,
    changed_ids: Sequence[str] | None = None,
    shifted_ids: Sequence[str] | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    no_op: bool = False,
    warnings: Sequence[str] | None = None,
    status: str = STATUS_APPLIED,
    error: str = "",
    undo: str = "one step",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable edit_ops/v1 receipt (same keys on success and failure)."""
    if no_op and status == STATUS_APPLIED:
        status = STATUS_NO_OP
    if not ok and status == STATUS_APPLIED:
        status = STATUS_REFUSED
    out: dict[str, Any] = {
        "ok": bool(ok),
        "op": str(op),
        "version": RECEIPT_VERSION,
        "changed_ids": list(changed_ids or []),
        "shifted_ids": list(shifted_ids or []),
        "before": dict(before or {}),
        "after": dict(after or {}),
        "no_op": bool(no_op),
        "warnings": list(warnings or []),
        "undo": undo if ok and not no_op else "none",
        "status": status,
        "error": str(error or ""),
    }
    if extra:
        out.update(dict(extra))
    return out


def clip_snapshot(clip: Mapping[str, Any]) -> dict[str, Any]:
    """Compact before/after fields for receipts."""
    out: dict[str, Any] = {}
    for key in _CLIP_KEYS:
        if key in clip:
            out[key] = clip[key]
    try:
        out["duration_frames"] = ft.duration_frames(
            float(clip.get("start", 0.0) or 0.0),
            float(clip.get("end", 0.0) or 0.0),
            24,  # display only; real duration uses caller fps in ops
        )
    except Exception:
        pass
    return out


def snapshots_by_id(clips: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for clip in clips:
        cid = str(clip.get("id") or "")
        if cid:
            out[cid] = clip_snapshot(clip)
    return out


def _require_fps(fps: Any) -> Any:
    # Validate via frame_time
    ft.to_frame(0.0, fps)
    return fps


def _as_clip(clip: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(clip)
    if not str(data.get("id") or "").strip():
        raise ValueError("clip id is required")
    data["id"] = str(data["id"])
    data["position"] = float(data.get("position", 0.0) or 0.0)
    data["start"] = float(data.get("start", 0.0) or 0.0)
    data["end"] = float(data.get("end", 0.0) or 0.0)
    try:
        data["layer"] = int(data.get("layer") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid layer: {data.get('layer')!r}") from exc
    if data["end"] <= data["start"]:
        raise ValueError("clip end must be greater than start")
    return data


def _quantize(clip: MutableMapping[str, Any], fps: Any) -> None:
    pos, start, end = ft.quantize_span(
        float(clip["position"]),
        float(clip["start"]),
        float(clip["end"]),
        fps,
    )
    clip["position"] = pos
    clip["start"] = start
    clip["end"] = end


def layer_is_locked(layers: Sequence[Mapping[str, Any]], layer_num: int) -> bool:
    for layer in layers or ():
        try:
            if int(layer.get("number") or 0) == int(layer_num):
                return bool(layer.get("lock", False))
        except (TypeError, ValueError):
            continue
    return False


def layer_is_sync_locked(layers: Sequence[Mapping[str, Any]], layer_num: int) -> bool:
    """Missing sync_locked defaults to True (plan default-on)."""
    for layer in layers or ():
        try:
            if int(layer.get("number") or 0) == int(layer_num):
                if "sync_locked" not in layer:
                    return True
                return bool(layer.get("sync_locked"))
        except (TypeError, ValueError):
            continue
    return True


def partners_for(
    clip_id: str,
    clips: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return other clips sharing link_group_id with *clip_id*."""
    by_id = {str(c.get("id") or ""): _as_clip(c) for c in clips if c.get("id")}
    subject = by_id.get(str(clip_id))
    if not subject:
        return []
    group = str(subject.get("link_group_id") or "").strip()
    if not group:
        return []
    return [
        c for cid, c in by_id.items()
        if cid != subject["id"] and str(c.get("link_group_id") or "") == group
    ]


def link_clips(clip_ids: Sequence[str], clips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Assign one link_group_id to the given clips. Returns patches + receipt."""
    ids = [str(i).strip() for i in clip_ids if str(i).strip()]
    if len(ids) < 2:
        return receipt(
            ok=False,
            op="link",
            error="link requires at least two clipIds",
            status=STATUS_REFUSED,
        )
    by_id = {str(c.get("id") or ""): dict(c) for c in clips if c.get("id")}
    missing = [i for i in ids if i not in by_id]
    if missing:
        return receipt(
            ok=False,
            op="link",
            error=f"unknown clipIds: {', '.join(missing)}",
            status=STATUS_REFUSED,
        )
    before = snapshots_by_id(by_id[i] for i in ids)
    # Reuse an existing group id if any member already has one.
    group = ""
    for i in ids:
        g = str(by_id[i].get("link_group_id") or "").strip()
        if g:
            group = g
            break
    if not group:
        group = new_link_group_id()
    patches = []
    for i in ids:
        patch = dict(by_id[i])
        patch["link_group_id"] = group
        patches.append(patch)
    after = snapshots_by_id(patches)
    return {
        **receipt(
            ok=True,
            op="link",
            changed_ids=ids,
            before=before,
            after=after,
            status=STATUS_APPLIED,
            extra={"link_group_id": group, "patches": patches},
        ),
    }


def unlink_clips(clip_ids: Sequence[str], clips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ids = [str(i).strip() for i in clip_ids if str(i).strip()]
    if not ids:
        return receipt(ok=False, op="unlink", error="clipIds required", status=STATUS_REFUSED)
    by_id = {str(c.get("id") or ""): dict(c) for c in clips if c.get("id")}
    missing = [i for i in ids if i not in by_id]
    if missing:
        return receipt(
            ok=False,
            op="unlink",
            error=f"unknown clipIds: {', '.join(missing)}",
            status=STATUS_REFUSED,
        )
    before = snapshots_by_id(by_id[i] for i in ids)
    patches = []
    for i in ids:
        patch = dict(by_id[i])
        patch.pop("link_group_id", None)
        patches.append(patch)
    after = snapshots_by_id(patches)
    return {
        **receipt(
            ok=True,
            op="unlink",
            changed_ids=ids,
            before=before,
            after=after,
            status=STATUS_APPLIED,
            extra={"patches": patches},
        ),
    }


def list_link_groups(clips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[str]] = {}
    for clip in clips:
        cid = str(clip.get("id") or "")
        group = str(clip.get("link_group_id") or "").strip()
        if not cid or not group:
            continue
        groups.setdefault(group, []).append(cid)
    return receipt(
        ok=True,
        op="list_links",
        status=STATUS_APPLIED,
        undo="none",
        extra={"groups": groups},
    )


def resolve_nudge_frames(
    frames: int | None,
    *,
    soft: bool = False,
    default: int = NUDGE_DEFAULT_FRAMES,
    cap: int = NUDGE_MAX_FRAMES,
) -> int:
    """Soft phrases → capped small deltas (Phase 6 'a little sunnier' lesson)."""
    if frames is None:
        value = default if soft else default
    else:
        try:
            value = int(frames)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"frames must be an int, got {frames!r}") from exc
    if value == 0:
        raise ValueError("frames must be non-zero")
    if soft:
        sign = 1 if value > 0 else -1
        value = sign * min(abs(value), abs(cap))
    return value


def _refuse_locked(
    op: str,
    clip: Mapping[str, Any],
    layers: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    layer = int(clip.get("layer") or 0)
    if layer_is_locked(layers, layer):
        return receipt(
            ok=False,
            op=op,
            before=snapshots_by_id([clip]),
            after=snapshots_by_id([clip]),
            error=f"track {layer} is locked",
            status=STATUS_REFUSED,
        )
    return None


def slip(
    clip: Mapping[str, Any],
    *,
    delta_frames: int,
    fps: Any,
    media_duration: float | None = None,
    layers: Sequence[Mapping[str, Any]] = (),
    partners: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Slide source in/out; timeline position and duration stay fixed."""
    fps = _require_fps(fps)
    subject = _as_clip(clip)
    refused = _refuse_locked("slip", subject, layers)
    if refused:
        return refused
    try:
        delta = int(delta_frames)
    except (TypeError, ValueError) as exc:
        return receipt(ok=False, op="slip", error=str(exc), status=STATUS_REFUSED)
    if delta == 0:
        return receipt(
            ok=True,
            op="slip",
            changed_ids=[subject["id"]],
            before=snapshots_by_id([subject]),
            after=snapshots_by_id([subject]),
            no_op=True,
        )

    dur_f = ft.duration_frames(subject["start"], subject["end"], fps)
    start_f = ft.to_frame(subject["start"], fps) + delta
    if start_f < 0:
        return receipt(
            ok=False,
            op="slip",
            error="slip would move start before media begin",
            status=STATUS_REFUSED,
            before=snapshots_by_id([subject]),
            after=snapshots_by_id([subject]),
        )
    end_f = start_f + dur_f
    if media_duration is not None:
        max_f = ft.to_frame(float(media_duration), fps)
        if end_f > max_f:
            return receipt(
                ok=False,
                op="slip",
                error="slip would move end past media duration",
                status=STATUS_REFUSED,
                before=snapshots_by_id([subject]),
                after=snapshots_by_id([subject]),
            )

    before_clips = [subject] + [_as_clip(p) for p in partners]
    before = snapshots_by_id(before_clips)
    patches = []
    for item in before_clips:
        patch = dict(item)
        s_f = ft.to_frame(patch["start"], fps) + delta
        e_f = s_f + ft.duration_frames(patch["start"], patch["end"], fps)
        patch["start"] = ft.to_seconds(s_f, fps)
        patch["end"] = ft.to_seconds(e_f, fps)
        patch["position"] = ft.snap(patch["position"], fps)
        patches.append(patch)
    after = snapshots_by_id(patches)
    return {
        **receipt(
            ok=True,
            op="slip",
            changed_ids=[p["id"] for p in patches],
            before=before,
            after=after,
            extra={"patches": patches, "delta_frames": delta},
        ),
    }


def _find_neighbors(
    clip: Mapping[str, Any],
    clips: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    layer = int(clip.get("layer") or 0)
    pos = float(clip.get("position", 0.0) or 0.0)
    start = float(clip.get("start", 0.0) or 0.0)
    end = float(clip.get("end", 0.0) or 0.0)
    right_edge = pos + (end - start)
    same = [
        _as_clip(c)
        for c in clips
        if str(c.get("id") or "") != str(clip.get("id") or "")
        and int(c.get("layer") or 0) == layer
    ]
    left = None
    right = None
    for other in same:
        o_pos = float(other["position"])
        o_right = o_pos + (float(other["end"]) - float(other["start"]))
        if abs(o_right - pos) < 1e-6 or (o_right <= pos + 1e-6 and (left is None or o_right > float(left["position"]) + (float(left["end"]) - float(left["start"])))):
            if abs(o_right - pos) < 1e-4:
                left = other
        if abs(o_pos - right_edge) < 1e-4:
            right = other
    return left, right


def roll(
    left_clip: Mapping[str, Any],
    right_clip: Mapping[str, Any],
    *,
    delta_frames: int,
    fps: Any,
    layers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Move the shared cut: left out and right in together. Sequence length unchanged."""
    fps = _require_fps(fps)
    left = _as_clip(left_clip)
    right = _as_clip(right_clip)
    if left["id"] == right["id"]:
        return receipt(ok=False, op="roll", error="roll needs two different clips", status=STATUS_REFUSED)
    if left["layer"] != right["layer"]:
        return receipt(ok=False, op="roll", error="roll clips must share a track", status=STATUS_REFUSED)
    for item in (left, right):
        refused = _refuse_locked("roll", item, layers)
        if refused:
            return refused
    try:
        delta = int(delta_frames)
    except (TypeError, ValueError) as exc:
        return receipt(ok=False, op="roll", error=str(exc), status=STATUS_REFUSED)
    if delta == 0:
        return receipt(
            ok=True,
            op="roll",
            changed_ids=[left["id"], right["id"]],
            before=snapshots_by_id([left, right]),
            after=snapshots_by_id([left, right]),
            no_op=True,
        )

    # Ensure left is earlier on the timeline.
    if float(left["position"]) > float(right["position"]):
        left, right = right, left

    left_end_f = ft.to_frame(left["end"], fps) + delta
    left_start_f = ft.to_frame(left["start"], fps)
    if left_end_f <= left_start_f:
        return receipt(ok=False, op="roll", error="roll would empty the left clip", status=STATUS_REFUSED)

    right_start_f = ft.to_frame(right["start"], fps) + delta
    right_end_f = ft.to_frame(right["end"], fps)
    if right_start_f < 0 or right_start_f >= right_end_f:
        return receipt(ok=False, op="roll", error="roll would empty the right clip", status=STATUS_REFUSED)

    # Right position moves with the cut so the join stays abutting.
    right_pos_f = ft.to_frame(right["position"], fps) + delta
    if right_pos_f < 0:
        return receipt(ok=False, op="roll", error="roll would move right clip before 0", status=STATUS_REFUSED)

    before = snapshots_by_id([left, right])
    left_patch = dict(left)
    right_patch = dict(right)
    left_patch["end"] = ft.to_seconds(left_end_f, fps)
    left_patch["start"] = ft.to_seconds(left_start_f, fps)
    left_patch["position"] = ft.snap(left_patch["position"], fps)
    right_patch["start"] = ft.to_seconds(right_start_f, fps)
    right_patch["end"] = ft.to_seconds(right_end_f, fps)
    right_patch["position"] = ft.to_seconds(right_pos_f, fps)
    patches = [left_patch, right_patch]
    after = snapshots_by_id(patches)
    return {
        **receipt(
            ok=True,
            op="roll",
            changed_ids=[left["id"], right["id"]],
            before=before,
            after=after,
            extra={"patches": patches, "delta_frames": delta},
        ),
    }


def slide(
    clip: Mapping[str, Any],
    clips: Sequence[Mapping[str, Any]],
    *,
    delta_frames: int,
    fps: Any,
    layers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Move clip; neighbors absorb. Sequence length unchanged."""
    fps = _require_fps(fps)
    subject = _as_clip(clip)
    refused = _refuse_locked("slide", subject, layers)
    if refused:
        return refused
    try:
        delta = int(delta_frames)
    except (TypeError, ValueError) as exc:
        return receipt(ok=False, op="slide", error=str(exc), status=STATUS_REFUSED)
    if delta == 0:
        return receipt(
            ok=True,
            op="slide",
            changed_ids=[subject["id"]],
            before=snapshots_by_id([subject]),
            after=snapshots_by_id([subject]),
            no_op=True,
        )

    left, right = _find_neighbors(subject, clips)
    if left is None and right is None:
        return receipt(
            ok=False,
            op="slide",
            error="no adjacent clips to absorb the slide",
            status=STATUS_REFUSED,
            before=snapshots_by_id([subject]),
            after=snapshots_by_id([subject]),
        )
    for neighbor in (left, right):
        if neighbor is None:
            continue
        refused = _refuse_locked("slide", neighbor, layers)
        if refused:
            return refused

    before_list = [subject] + [n for n in (left, right) if n is not None]
    before = snapshots_by_id(before_list)

    subject_patch = dict(subject)
    new_pos_f = ft.to_frame(subject["position"], fps) + delta
    if new_pos_f < 0:
        return receipt(ok=False, op="slide", error="slide would move clip before 0", status=STATUS_REFUSED)
    subject_patch["position"] = ft.to_seconds(new_pos_f, fps)

    patches = [subject_patch]
    if left is not None:
        left_patch = dict(left)
        # Grow/shrink left's out point so its right edge meets the new subject position.
        left_dur_span = float(left["end"]) - float(left["start"])
        left_right_edge = float(left["position"]) + left_dur_span
        # New right edge should be subject's new position.
        new_left_end_f = ft.to_frame(left["end"], fps) + delta
        left_start_f = ft.to_frame(left["start"], fps)
        if new_left_end_f <= left_start_f:
            return receipt(ok=False, op="slide", error="slide would empty the left neighbor", status=STATUS_REFUSED)
        left_patch["end"] = ft.to_seconds(new_left_end_f, fps)
        patches.append(left_patch)
        _ = left_right_edge  # documented invariant; abut checked via frames

    if right is not None:
        right_patch = dict(right)
        # Sliding the subject right (+delta): right neighbor loses frames from
        # its in-point and its position advances so its out stays put.
        right_start_f = ft.to_frame(right["start"], fps) + delta
        right_end_f = ft.to_frame(right["end"], fps)
        right_pos_f = ft.to_frame(right["position"], fps) + delta
        if right_start_f < 0 or right_start_f >= right_end_f:
            return receipt(ok=False, op="slide", error="slide would empty the right neighbor", status=STATUS_REFUSED)
        if right_pos_f < 0:
            return receipt(ok=False, op="slide", error="slide would move right neighbor before 0", status=STATUS_REFUSED)
        right_patch["start"] = ft.to_seconds(right_start_f, fps)
        right_patch["end"] = ft.to_seconds(right_end_f, fps)
        right_patch["position"] = ft.to_seconds(right_pos_f, fps)
        patches.append(right_patch)

    after = snapshots_by_id(patches)
    return {
        **receipt(
            ok=True,
            op="slide",
            changed_ids=[p["id"] for p in patches],
            before=before,
            after=after,
            extra={"patches": patches, "delta_frames": delta},
        ),
    }


def ripple_trim(
    clip: Mapping[str, Any],
    clips: Sequence[Mapping[str, Any]],
    *,
    edge: str,
    delta_frames: int,
    fps: Any,
    layers: Sequence[Mapping[str, Any]] = (),
    partners: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Trim one edge; shift downstream on same track and sync-locked tracks."""
    fps = _require_fps(fps)
    edge = str(edge or "").strip().lower()
    if edge not in ("in", "out", "left", "right", "start", "end"):
        return receipt(ok=False, op="ripple_trim", error="edge must be in/out", status=STATUS_REFUSED)
    trim_out = edge in ("out", "right", "end")
    subject = _as_clip(clip)
    refused = _refuse_locked("ripple_trim", subject, layers)
    if refused:
        return refused
    try:
        delta = int(delta_frames)
    except (TypeError, ValueError) as exc:
        return receipt(ok=False, op="ripple_trim", error=str(exc), status=STATUS_REFUSED)
    if delta == 0:
        return receipt(
            ok=True,
            op="ripple_trim",
            changed_ids=[subject["id"]],
            before=snapshots_by_id([subject]),
            after=snapshots_by_id([subject]),
            no_op=True,
        )

    start_f = ft.to_frame(subject["start"], fps)
    end_f = ft.to_frame(subject["end"], fps)
    pos_f = ft.to_frame(subject["position"], fps)
    subject_patch = dict(subject)
    if trim_out:
        new_end_f = end_f + delta
        if new_end_f <= start_f:
            return receipt(ok=False, op="ripple_trim", error="trim would empty the clip", status=STATUS_REFUSED)
        subject_patch["end"] = ft.to_seconds(new_end_f, fps)
        shift = delta
        cut_frame = pos_f + (end_f - start_f)  # old right edge
    else:
        new_start_f = start_f + delta
        if new_start_f < 0 or new_start_f >= end_f:
            return receipt(ok=False, op="ripple_trim", error="trim would empty the clip", status=STATUS_REFUSED)
        # Trimming in from the left shortens duration; position stays, content shifts.
        # Ripple shift is -delta for a positive delta (media start later → shorter → pull up).
        subject_patch["start"] = ft.to_seconds(new_start_f, fps)
        shift = -delta
        cut_frame = pos_f

    partner_patches = []
    for partner in partners:
        p = _as_clip(partner)
        refused = _refuse_locked("ripple_trim", p, layers)
        if refused:
            return refused
        p_patch = dict(p)
        if trim_out:
            p_end = ft.to_frame(p["end"], fps) + delta
            p_start = ft.to_frame(p["start"], fps)
            if p_end <= p_start:
                return receipt(ok=False, op="ripple_trim", error="trim would empty a linked partner", status=STATUS_REFUSED)
            p_patch["end"] = ft.to_seconds(p_end, fps)
        else:
            p_start = ft.to_frame(p["start"], fps) + delta
            p_end = ft.to_frame(p["end"], fps)
            if p_start < 0 or p_start >= p_end:
                return receipt(ok=False, op="ripple_trim", error="trim would empty a linked partner", status=STATUS_REFUSED)
            p_patch["start"] = ft.to_seconds(p_start, fps)
        partner_patches.append(p_patch)

    # Downstream shift: same layer, or other sync-locked layers, clips starting at/after cut.
    shifted = []
    subject_layer = subject["layer"]
    for other in clips:
        oid = str(other.get("id") or "")
        if not oid or oid == subject["id"] or oid in {p["id"] for p in partner_patches}:
            continue
        o = _as_clip(other)
        o_layer = o["layer"]
        if o_layer != subject_layer and not layer_is_sync_locked(layers, o_layer):
            continue
        if o_layer != subject_layer and not layer_is_sync_locked(layers, subject_layer):
            continue
        # Sync-lock: both tracks must be sync-locked for cross-track shift.
        if o_layer != subject_layer:
            if not (layer_is_sync_locked(layers, subject_layer) and layer_is_sync_locked(layers, o_layer)):
                continue
        o_pos_f = ft.to_frame(o["position"], fps)
        if o_pos_f < cut_frame:
            continue
        if layer_is_locked(layers, o_layer):
            return receipt(
                ok=False,
                op="ripple_trim",
                error=f"downstream track {o_layer} is locked",
                status=STATUS_REFUSED,
            )
        patch = dict(o)
        patch["position"] = ft.to_seconds(o_pos_f + shift, fps)
        shifted.append(patch)

    patches = [subject_patch] + partner_patches + shifted
    before = snapshots_by_id([subject] + [_as_clip(p) for p in partners] + [
        _as_clip(c) for c in clips
        if str(c.get("id") or "") in {p["id"] for p in shifted}
    ])
    after = snapshots_by_id(patches)
    return {
        **receipt(
            ok=True,
            op="ripple_trim",
            changed_ids=[subject["id"]] + [p["id"] for p in partner_patches],
            shifted_ids=[p["id"] for p in shifted],
            before=before,
            after=after,
            extra={"patches": patches, "delete_ids": [], "delta_frames": delta, "edge": "out" if trim_out else "in"},
        ),
    }


def lift(
    clip_ids: Sequence[str],
    clips: Sequence[Mapping[str, Any]],
    *,
    layers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Delete clips, leave a hole. Sequence length unchanged."""
    ids = [str(i).strip() for i in clip_ids if str(i).strip()]
    if not ids:
        return receipt(ok=False, op="lift", error="clipIds required", status=STATUS_REFUSED)
    by_id = {str(c.get("id") or ""): _as_clip(c) for c in clips if c.get("id")}
    missing = [i for i in ids if i not in by_id]
    if missing:
        return receipt(ok=False, op="lift", error=f"unknown clipIds: {', '.join(missing)}", status=STATUS_REFUSED)
    for i in ids:
        refused = _refuse_locked("lift", by_id[i], layers)
        if refused:
            return refused
    before = snapshots_by_id(by_id[i] for i in ids)
    return {
        **receipt(
            ok=True,
            op="lift",
            changed_ids=ids,
            before=before,
            after={},
            extra={"patches": [], "delete_ids": ids},
        ),
    }


def extract(
    clip_ids: Sequence[str],
    clips: Sequence[Mapping[str, Any]],
    *,
    fps: Any,
    layers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Delete clips and close the gap (ripple). Sequence shortens."""
    fps = _require_fps(fps)
    ids = [str(i).strip() for i in clip_ids if str(i).strip()]
    if not ids:
        return receipt(ok=False, op="extract", error="clipIds required", status=STATUS_REFUSED)
    by_id = {str(c.get("id") or ""): _as_clip(c) for c in clips if c.get("id")}
    missing = [i for i in ids if i not in by_id]
    if missing:
        return receipt(ok=False, op="extract", error=f"unknown clipIds: {', '.join(missing)}", status=STATUS_REFUSED)

    # Process earliest-first; for v1 support one contiguous extract per call (same layer).
    targets = [by_id[i] for i in ids]
    for t in targets:
        refused = _refuse_locked("extract", t, layers)
        if refused:
            return refused
    layers_set = {t["layer"] for t in targets}
    if len(layers_set) != 1:
        return receipt(
            ok=False,
            op="extract",
            error="extract v1 requires all clipIds on one track",
            status=STATUS_REFUSED,
        )
    layer = targets[0]["layer"]
    targets_sorted = sorted(targets, key=lambda c: float(c["position"]))
    # Merge span from first left edge to last right edge among deleted.
    span_start_f = min(ft.to_frame(t["position"], fps) for t in targets_sorted)
    span_end_f = max(
        ft.to_frame(t["position"], fps) + ft.duration_frames(t["start"], t["end"], fps)
        for t in targets_sorted
    )
    gap_f = span_end_f - span_start_f
    if gap_f <= 0:
        return receipt(ok=False, op="extract", error="extract span is empty", status=STATUS_REFUSED)

    delete_ids = [t["id"] for t in targets_sorted]
    shifted = []
    for other in clips:
        oid = str(other.get("id") or "")
        if not oid or oid in delete_ids:
            continue
        o = _as_clip(other)
        o_layer = o["layer"]
        if o_layer != layer:
            if not (layer_is_sync_locked(layers, layer) and layer_is_sync_locked(layers, o_layer)):
                continue
        o_pos_f = ft.to_frame(o["position"], fps)
        if o_pos_f < span_end_f:
            continue
        if layer_is_locked(layers, o_layer):
            return receipt(
                ok=False,
                op="extract",
                error=f"downstream track {o_layer} is locked",
                status=STATUS_REFUSED,
            )
        patch = dict(o)
        patch["position"] = ft.to_seconds(o_pos_f - gap_f, fps)
        shifted.append(patch)

    before = snapshots_by_id(list(targets_sorted) + [
        _as_clip(c) for c in clips if str(c.get("id") or "") in {p["id"] for p in shifted}
    ])
    after = snapshots_by_id(shifted)
    return {
        **receipt(
            ok=True,
            op="extract",
            changed_ids=delete_ids,
            shifted_ids=[p["id"] for p in shifted],
            before=before,
            after=after,
            extra={"patches": shifted, "delete_ids": delete_ids, "gap_frames": gap_f},
        ),
    }


def set_track_sync_lock(
    layers: Sequence[Mapping[str, Any]],
    *,
    track: int,
    sync_locked: bool,
) -> dict[str, Any]:
    """Return a patched layers list with sync_locked set on *track*."""
    found = False
    patched = []
    for layer in layers or ():
        item = dict(layer)
        try:
            num = int(item.get("number") or 0)
        except (TypeError, ValueError):
            patched.append(item)
            continue
        if num == int(track):
            item["sync_locked"] = bool(sync_locked)
            # Default lock field preserved; sync_locked is independent.
            if "lock" not in item:
                item["lock"] = False
            found = True
        elif "sync_locked" not in item:
            item["sync_locked"] = True
        patched.append(item)
    if not found:
        return receipt(
            ok=False,
            op="set_track_sync_lock",
            error=f"track {track} not found",
            status=STATUS_REFUSED,
        )
    return {
        **receipt(
            ok=True,
            op="set_track_sync_lock",
            status=STATUS_APPLIED,
            extra={"layers": patched, "track": int(track), "sync_locked": bool(sync_locked)},
        ),
    }


def ensure_layers_sync_defaults(layers: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return layers with sync_locked defaulted to True when missing."""
    out = []
    for layer in layers or ():
        item = dict(layer)
        if "sync_locked" not in item:
            item["sync_locked"] = True
        out.append(item)
    return out


def invert_patches(
    before: Mapping[str, Mapping[str, Any]],
    after_patches: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build inverse patches from a before snapshot map (for property tests)."""
    inverse = []
    for patch in after_patches:
        cid = str(patch.get("id") or "")
        if cid and cid in before:
            inv = dict(patch)
            snap = before[cid]
            for key in ("position", "start", "end", "layer", "link_group_id"):
                if key in snap:
                    inv[key] = snap[key]
                elif key == "link_group_id":
                    inv.pop("link_group_id", None)
            inverse.append(inv)
    return inverse


def clone_project_clips(clips: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [copy.deepcopy(dict(c)) for c in clips]
