"""Helpers shared by the timeline-edit tools (targeting, tracks, overlap checks, receipts)."""

from __future__ import annotations

from classes.editor_tools._base import (
    ToolError,
    describe_clip,
    get_app,
    is_locked,
    layers,
    playhead_seconds,
    project_fps,
    resolve_clip,
    resolve_clips,
    resolve_layer,
    timeline_ui,
    ui_track_number,
)
from classes.logger import log

EPS = 1e-6


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def frame_seconds() -> float:
    return 1.0 / project_fps()


def snap(seconds: float) -> float:
    fps = project_fps()
    return round(float(seconds) * fps) / fps


def tolerance() -> float:
    """Half a frame: anything closer is the same instant on the timeline."""
    return 0.5 / project_fps()


def r3(value) -> float:
    return round(float(value), 3)


def time_arg(seconds: float) -> float:
    """-1 (or any negative) means the playhead."""
    return playhead_seconds() if seconds is None or float(seconds) < 0 else float(seconds)


# ---------------------------------------------------------------------------
# Clips
# ---------------------------------------------------------------------------

def span(data: dict) -> tuple:
    position = float(data.get("position") or 0.0)
    return position, position + max(0.0, float(data.get("end") or 0.0) - float(data.get("start") or 0.0))


def targets(timeline_clip_ids=None, clip_query="", track="", scope=""):
    """Clips a batch tool acts on, de-duplicated, in timeline order."""
    clips = resolve_clips(timeline_clip_ids or [], clip_query, track, scope)
    seen, out = set(), []
    for c in clips:
        if c.id not in seen:
            seen.add(c.id)
            out.append(c)
    out.sort(key=lambda c: (float(c.data.get("position") or 0.0), int(c.data.get("layer") or 0)))
    return out


def target(timeline_clip_id="", clip_query="", track=""):
    return resolve_clip(timeline_clip_id, clip_query, track)


def title(clip) -> str:
    return str(clip.data.get("title") or clip.id)


def reader_of(clip) -> dict:
    reader = clip.data.get("reader")
    if isinstance(reader, dict) and reader:
        return reader
    from classes.query import File
    f = File.get(id=str(clip.data.get("file_id") or ""))
    return dict(f.data) if f and isinstance(f.data, dict) else {}


def media_has_audio(clip) -> bool:
    reader = reader_of(clip)
    has_audio = reader.get("has_audio")
    has_audio = True if has_audio is None else bool(has_audio)
    try:
        channels = int(reader.get("channels")) if reader.get("channels") is not None else None
    except (TypeError, ValueError):
        channels = None
    return has_audio and (channels is None or channels > 0)


def media_has_video(clip) -> bool:
    reader = reader_of(clip)
    has_video = reader.get("has_video")
    return True if has_video is None else bool(has_video)


def is_image(clip_or_data) -> bool:
    data = clip_or_data.data if hasattr(clip_or_data, "data") else clip_or_data
    reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
    return bool(reader.get("has_single_image")) or reader.get("media_type") == "image"


def keyframe_value(kf, default=-1.0) -> float:
    """First point's value of a constant keyframe such as has_audio (-1 auto, 0 off, 1 on)."""
    pts = kf.get("Points") if isinstance(kf, dict) else None
    if not pts:
        return float(default)
    try:
        return float(pts[0]["co"]["Y"])
    except (KeyError, TypeError, ValueError, IndexError):
        return float(default)


def clip_summary(clip_or_data) -> dict:
    """describe_clip() for a QueryObject or a plain clip dict."""
    if isinstance(clip_or_data, dict):
        class _Tmp:
            pass
        tmp = _Tmp()
        tmp.id = clip_or_data.get("id")
        tmp.data = clip_or_data
        return describe_clip(tmp)
    return describe_clip(clip_or_data)


def all_clip_ids() -> set:
    from classes.query import Clip
    return {c.id for c in Clip.filter()}


def fresh(clip_id):
    from classes.query import Clip
    return Clip.get(id=clip_id)


# ---------------------------------------------------------------------------
# Tracks
# ---------------------------------------------------------------------------

def track_numbers_bottom_up() -> list:
    """Layer numbers in UI order: index 0 = UI track 1 (bottom)."""
    return sorted(int(t.get("number") or 0) for t in layers())


def track_name(layer_num: int) -> str:
    ui = ui_track_number(layer_num)
    return f"track {ui}" if ui else f"layer {layer_num}"


def require_unlocked(layer_numbers, what="") -> None:
    locked = sorted({int(n) for n in layer_numbers if is_locked(int(n))})
    if locked:
        names = ", ".join(track_name(n) for n in locked)
        verb, pronoun = ("is", "it") if len(locked) == 1 else ("are", "them")
        raise ToolError(f"{names} {verb} locked{(' (' + what + ')') if what else ''}; "
                        f"unlock {pronoun} first (Track menu > Unlock Track)")


def default_layer() -> int:
    """Same default as add_clip_to_timeline_tool: the selected track, else the bottom track."""
    from classes.clip_placement import default_underlay_layer_number
    from classes.query import Track
    try:
        selected = list(getattr(get_app().window, "selected_tracks", []) or [])
    except Exception:
        selected = []
    if selected and isinstance(selected[0], str):
        t = Track.get(id=selected[0])
        if t and isinstance(t.data, dict):
            return int(t.data.get("number") or 0)
    return int(default_underlay_layer_number(layers()))


def remap_layers(source_layers, dest_track) -> dict:
    """Map source layers onto *dest_track*, keeping their relative stacking.

    The top-most source track lands on the destination and the others keep
    their UI-track offsets below/above it (what paste does). Raises when a
    mapped track does not exist.
    """
    order = track_numbers_bottom_up()
    dest = resolve_layer(dest_track)
    sources = sorted({int(n) for n in source_layers})
    top = max(sources, key=lambda n: order.index(n) if n in order else -1)
    offset = order.index(dest) - order.index(top)
    out = {}
    for n in sources:
        idx = order.index(n) + offset
        if idx < 0 or idx >= len(order):
            raise ToolError(f"moving these clips' {len(sources)} tracks onto {track_name(dest)} needs a "
                            "track that does not exist; add one first (add_track_tool)")
        out[n] = order[idx]
    return out


def transitions_between(clips) -> list:
    """Transitions lying where two of *clips* overlap on one track (crossfades inside a selection).

    A clip's incoming or outgoing crossfade overlaps only that clip among the
    selected ones: it belongs to the neighbour and is not included.
    """
    from classes.query import Transition
    tol = tolerance()
    spans = {c.id: (int(c.data.get("layer") or 0),) + span(c.data) for c in clips}
    layer_set = {v[0] for v in spans.values()}
    out = []
    for tr in Transition.filter():
        layer = int(tr.data.get("layer") or 0)
        if layer not in layer_set:
            continue
        t0, t1 = span(tr.data)
        under = [cid for cid, (lay, s, e) in spans.items() if lay == layer and s < t1 - tol and e > t0 + tol]
        if len(under) >= 2:
            out.append(tr)
    return out


# ---------------------------------------------------------------------------
# Overlaps
# ---------------------------------------------------------------------------

def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def new_overlaps(changes: dict, new_items=()) -> list:
    """Clip overlaps a planned edit would create or grow on the same track.

    changes   -- {clip_id: (new_layer, new_start, new_end)} for existing clips.
    new_items -- [(label, layer, start, end)] for clips about to be created.
    Overlaps that already existed (a crossfade pair) are fine unless they grow.
    Transitions never count: they overlap clips by design.
    """
    from classes.query import Clip
    tol = tolerance()
    before = {}
    for c in Clip.filter():
        s, e = span(c.data)
        before[c.id] = (int(c.data.get("layer") or 0), s, e, title(c))
    after = dict(before)
    for cid, (layer, s, e) in changes.items():
        after[cid] = (int(layer), s, e, before.get(cid, (0, 0, 0, cid))[3])

    hits = []
    checked = set()
    for cid in changes:
        layer, s, e, name = after[cid]
        for oid, (olayer, os_, oe, oname) in after.items():
            if oid == cid or olayer != layer or (oid, cid) in checked:
                continue
            checked.add((cid, oid))
            amount = _overlap(s, e, os_, oe)
            if amount <= tol:
                continue
            bl, bs, be, _n = before[cid]
            obl, obs, obe, _o = before[oid]
            was = _overlap(bs, be, obs, obe) if bl == obl else 0.0
            if amount > was + tol:
                hits.append({"timeline_clip_id": cid, "title": name, "overlaps": oid, "overlaps_title": oname,
                             "track": ui_track_number(layer), "seconds": r3(amount)})
    for label, layer, s, e in new_items:
        for oid, (olayer, os_, oe, oname) in after.items():
            if olayer != int(layer):
                continue
            amount = _overlap(s, e, os_, oe)
            if amount > tol:
                hits.append({"timeline_clip_id": label, "title": label, "overlaps": oid, "overlaps_title": oname,
                             "track": ui_track_number(int(layer)), "seconds": r3(amount)})
    return hits


def check_overlaps(hits: list, allow_overlap: bool, what: str, ripple_hint: bool = True) -> list:
    if hits and not allow_overlap:
        first = hits[0]
        more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
        options = "pass allow_overlap=true to overlap anyway"
        if ripple_hint:
            options = "pass ripple=true to push the later clips along, or " + options
        raise ToolError(
            f"{what} would overlap {first['overlaps_title']!r} ({first['overlaps']}) on track {first['track']} "
            f"by {first['seconds']} s{more}; {options}")
    return hits


# ---------------------------------------------------------------------------
# Applying edits
# ---------------------------------------------------------------------------

def save_clip(clip, **fields) -> None:
    """Write fields into a clip through the update manager (joins the tool's undo step)."""
    clip.data.update(fields)
    clip.save()


def extend_timeline() -> None:
    """Grow the project length to fit the clips (untracked, like every UI add/move path)."""
    try:
        extend = getattr(timeline_ui(), "_extend_timeline_to_fit_items", None)
    except ToolError:
        return
    if callable(extend):
        try:
            extend()
        except Exception:
            log.warning("could not extend the timeline to fit its clips", exc_info=True)


def refresh() -> None:
    from classes.editor_tools._base import refresh_preview
    refresh_preview()


def shifted_receipt(items) -> list:
    return sorted({str(i.id) for i in items})
