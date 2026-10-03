"""Agent/UI handlers for Phase 7 edit_ops (links, sync-lock, trim family).

Pure math lives in ``classes.edit_ops``. Handlers resolve clips, validate,
then open one undo transaction only after validation succeeds.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from classes import edit_ops


def project_fps_fraction():
    """Lazy import — clip_utils pulls openshot; keep handlers importable headless."""
    from classes.clip_utils import project_fps_fraction as _fps
    return _fps()


def _app():
    from classes.app import get_app
    return get_app()


def _all_clip_dicts(app=None) -> list[dict[str, Any]]:
    from classes.query import Clip
    app = app or _app()
    out = []
    for clip in Clip.filter():
        data = dict(clip.data) if isinstance(clip.data, dict) else {}
        data["id"] = str(getattr(clip, "id", "") or data.get("id") or "")
        if data["id"]:
            out.append(data)
    return out


def _layers(app=None) -> list[dict[str, Any]]:
    app = app or _app()
    return list(app.project.get("layers") or [])


def _receipt_json(result: Mapping[str, Any]) -> str:
    # Drop heavy patch bodies from the agent-facing string; keep ids + before/after.
    payload = {
        k: result.get(k)
        for k in (
            "ok", "op", "version", "changed_ids", "shifted_ids",
            "before", "after", "no_op", "warnings", "undo", "status", "error",
            "link_group_id", "delta_frames", "edge", "gap_frames",
            "track", "sync_locked", "edit_mode",
        )
        if k in result or k in (
            "ok", "op", "version", "changed_ids", "shifted_ids",
            "before", "after", "no_op", "warnings", "undo", "status", "error",
        )
    }
    if not payload.get("ok") and payload.get("error"):
        return f"Error: {payload['error']}\nEDIT_OPS_RECEIPT={json.dumps(payload, default=str)}"
    if payload.get("no_op"):
        return f"No change ({payload.get('op')}).\nEDIT_OPS_RECEIPT={json.dumps(payload, default=str)}"
    return f"OK {payload.get('op')} status={payload.get('status')}.\nEDIT_OPS_RECEIPT={json.dumps(payload, default=str)}"


def _apply_patches(app, patches: Sequence[Mapping[str, Any]], delete_ids: Sequence[str] = ()) -> None:
    from classes.query import Clip
    from classes.tool_handlers import _transaction

    with _transaction(app):
        for cid in delete_ids:
            clip = Clip.get(id=str(cid))
            if clip:
                clip.delete()
        for patch in patches:
            cid = str(patch.get("id") or "")
            if not cid:
                continue
            clip = Clip.get(id=cid)
            if not clip:
                continue
            data = dict(clip.data) if isinstance(clip.data, dict) else {}
            for key in ("position", "start", "end", "layer", "link_group_id"):
                if key == "link_group_id":
                    if "link_group_id" in patch:
                        data["link_group_id"] = patch["link_group_id"]
                    else:
                        data.pop("link_group_id", None)
                elif key in patch:
                    data[key] = patch[key]
            clip.data = data
            clip.save()


def _parse_ids(*values) -> list[str]:
    ids: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    ids.extend(str(x).strip() for x in parsed if str(x).strip())
                    continue
            except Exception:
                pass
        for part in text.replace(";", ",").split(","):
            part = part.strip()
            if part:
                ids.append(part)
    # dedupe preserve order
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_frames(frames="", soft=False) -> tuple[int | None, str]:
    soft_flag = _truthy(soft) if not isinstance(soft, bool) else bool(soft)
    if frames is None or str(frames).strip() == "":
        # Empty → default nudge size (same as "a little").
        try:
            return edit_ops.resolve_nudge_frames(None, soft=True), ""
        except ValueError as exc:
            return None, str(exc)
    try:
        raw = int(float(str(frames).strip()))
    except (TypeError, ValueError):
        return None, f"frames must be an int, got {frames!r}"
    try:
        return edit_ops.resolve_nudge_frames(raw, soft=soft_flag), ""
    except ValueError as exc:
        return None, str(exc)


def manage_clip_links(
    action="",
    clipIds="",
    clip_ids="",
    timeline_clip_id="",
    **_kw,
) -> str:
    """link / unlink / list clip link groups. Mutators = one undo."""
    try:
        app = _app()
        act = str(action or _kw.get("mode") or "").strip().lower()
        clips = _all_clip_dicts(app)
        if act in ("list", "get", ""):
            # Default empty action → list (read-only, no undo).
            if act in ("",) and not (clipIds or clip_ids):
                result = edit_ops.list_link_groups(clips)
                return _receipt_json(result)
            if act in ("list", "get"):
                result = edit_ops.list_link_groups(clips)
                return _receipt_json(result)

        ids = _parse_ids(clipIds, clip_ids, timeline_clip_id, _kw.get("clipId"))
        if act == "link":
            result = edit_ops.link_clips(ids, clips)
            if not result.get("ok"):
                return _receipt_json(result)
            _apply_patches(app, result.get("patches") or [])
            # Re-read for after verification
            after_clips = {c["id"]: c for c in _all_clip_dicts(app)}
            result["after"] = edit_ops.snapshots_by_id(after_clips[i] for i in ids if i in after_clips)
            return _receipt_json(result)
        if act == "unlink":
            result = edit_ops.unlink_clips(ids, clips)
            if not result.get("ok"):
                return _receipt_json(result)
            _apply_patches(app, result.get("patches") or [])
            after_clips = {c["id"]: c for c in _all_clip_dicts(app)}
            result["after"] = edit_ops.snapshots_by_id(after_clips[i] for i in ids if i in after_clips)
            return _receipt_json(result)
        return _receipt_json(edit_ops.receipt(
            ok=False, op="manage_clip_links", error="action must be link, unlink, or list",
            status=edit_ops.STATUS_REFUSED,
        ))
    except Exception as exc:
        return f"Error: {exc}"


def set_track_sync_lock(track="", sync_locked="true", **_kw) -> str:
    try:
        app = _app()
        from classes.track_display import normalize_track_or_layer_arg
        from classes.query import Track
        from classes.tool_handlers import _transaction

        layers = _layers(app)
        layer_num, err = normalize_track_or_layer_arg(str(track).strip(), layers)
        if err:
            return err
        flag = _truthy(sync_locked) if not isinstance(sync_locked, bool) else bool(sync_locked)
        # Also accept syncLocked
        if "syncLocked" in _kw and str(_kw.get("sync_locked", "")).strip() == "":
            flag = _truthy(_kw.get("syncLocked"))
        result = edit_ops.set_track_sync_lock(layers, track=int(layer_num), sync_locked=flag)
        if not result.get("ok"):
            return _receipt_json(result)
        tr = Track.get(number=int(layer_num))
        if not tr:
            return _receipt_json(edit_ops.receipt(
                ok=False, op="set_track_sync_lock", error=f"track {layer_num} not found",
                status=edit_ops.STATUS_REFUSED,
            ))
        with _transaction(app):
            data = dict(tr.data) if isinstance(tr.data, dict) else {}
            data["sync_locked"] = flag
            tr.data = data
            tr.save()
        result["after"] = {"track": int(layer_num), "sync_locked": flag}
        return _receipt_json(result)
    except Exception as exc:
        return f"Error: {exc}"


def _resolve_one(**kwargs):
    from classes.tool_handlers import _resolve_timeline_clip_for_tool
    return _resolve_timeline_clip_for_tool(**kwargs)


def slip_clip(
    timeline_clip_id="",
    clipId="",
    clip_query="",
    frames="",
    soft="false",
    track="",
    **_kw,
) -> str:
    try:
        app = _app()
        resolved = _resolve_one(
            timeline_clip_id=timeline_clip_id or clipId or _kw.get("clip_id"),
            clip_query=clip_query,
            track=track,
            **_kw,
        )
        if not resolved.ok:
            return resolved.error or "Error: could not resolve clip"
        delta, err = _parse_frames(frames, soft=_truthy(soft) or _truthy(_kw.get("nudge")))
        if err:
            return f"Error: {err}"
        clip = dict(resolved.clip.data)
        clip["id"] = str(resolved.clip.id)
        partners = edit_ops.partners_for(clip["id"], _all_clip_dicts(app))
        result = edit_ops.slip(
            clip,
            delta_frames=int(delta),
            fps=project_fps_fraction(),
            layers=_layers(app),
            partners=partners,
        )
        if not result.get("ok") or result.get("no_op"):
            return _receipt_json(result)
        _apply_patches(app, result.get("patches") or [])
        after = {c["id"]: c for c in _all_clip_dicts(app)}
        result["after"] = edit_ops.snapshots_by_id(after[i] for i in result["changed_ids"] if i in after)
        return _receipt_json(result)
    except Exception as exc:
        return f"Error: {exc}"


def slide_clip(
    timeline_clip_id="",
    clipId="",
    clip_query="",
    frames="",
    soft="false",
    track="",
    **_kw,
) -> str:
    try:
        app = _app()
        resolved = _resolve_one(
            timeline_clip_id=timeline_clip_id or clipId or _kw.get("clip_id"),
            clip_query=clip_query,
            track=track,
            **_kw,
        )
        if not resolved.ok:
            return resolved.error or "Error: could not resolve clip"
        delta, err = _parse_frames(frames, soft=_truthy(soft) or _truthy(_kw.get("nudge")))
        if err:
            return f"Error: {err}"
        clip = dict(resolved.clip.data)
        clip["id"] = str(resolved.clip.id)
        clips = _all_clip_dicts(app)
        result = edit_ops.slide(
            clip, clips, delta_frames=int(delta), fps=project_fps_fraction(), layers=_layers(app),
        )
        if not result.get("ok") or result.get("no_op"):
            return _receipt_json(result)
        _apply_patches(app, result.get("patches") or [])
        after = {c["id"]: c for c in _all_clip_dicts(app)}
        result["after"] = edit_ops.snapshots_by_id(
            after[i] for i in (result["changed_ids"] + result["shifted_ids"]) if i in after
        )
        return _receipt_json(result)
    except Exception as exc:
        return f"Error: {exc}"


def roll_edit(
    clip_a_id="",
    clip_b_id="",
    timeline_clip_id="",
    frames="",
    soft="false",
    **_kw,
) -> str:
    try:
        app = _app()
        from classes.tool_handlers import _resolve_clip_pair_for_tool
        delta, err = _parse_frames(frames, soft=_truthy(soft) or _truthy(_kw.get("nudge")))
        if err:
            return f"Error: {err}"
        pair = _resolve_clip_pair_for_tool(
            clip_a_id=clip_a_id or _kw.get("clipAId"),
            clip_b_id=clip_b_id or _kw.get("clipBId"),
            clip_a_query=_kw.get("clip_a_query", ""),
            clip_b_query=_kw.get("clip_b_query", ""),
        )
        if not pair.ok:
            # Fallback: resolve one clip and find neighbor on the same track.
            one = _resolve_one(timeline_clip_id=timeline_clip_id or _kw.get("clipId"), **_kw)
            if not one.ok:
                return pair.error or one.error or "Error: could not resolve cut"
            subject = dict(one.clip.data)
            subject["id"] = str(one.clip.id)
            left, right = edit_ops._find_neighbors(subject, _all_clip_dicts(app))
            if left is None and right is None:
                return _receipt_json(edit_ops.receipt(
                    ok=False, op="roll", error="no adjacent clip to roll against",
                    status=edit_ops.STATUS_REFUSED,
                ))
            # Prefer rolling with the right neighbor when both exist.
            if right is not None:
                left_c, right_c = subject, right
            else:
                left_c, right_c = left, subject
        else:
            left_c = dict(pair.clip_a.data)
            left_c["id"] = str(pair.clip_a.id)
            right_c = dict(pair.clip_b.data)
            right_c["id"] = str(pair.clip_b.id)

        result = edit_ops.roll(
            left_c, right_c, delta_frames=int(delta), fps=project_fps_fraction(), layers=_layers(app),
        )
        if not result.get("ok") or result.get("no_op"):
            return _receipt_json(result)
        _apply_patches(app, result.get("patches") or [])
        after = {c["id"]: c for c in _all_clip_dicts(app)}
        result["after"] = edit_ops.snapshots_by_id(after[i] for i in result["changed_ids"] if i in after)
        return _receipt_json(result)
    except Exception as exc:
        return f"Error: {exc}"


def ripple_trim(
    timeline_clip_id="",
    clipId="",
    clip_query="",
    edge="out",
    frames="",
    soft="false",
    track="",
    **_kw,
) -> str:
    try:
        app = _app()
        resolved = _resolve_one(
            timeline_clip_id=timeline_clip_id or clipId or _kw.get("clip_id"),
            clip_query=clip_query,
            track=track,
            **_kw,
        )
        if not resolved.ok:
            return resolved.error or "Error: could not resolve clip"
        delta, err = _parse_frames(frames, soft=_truthy(soft) or _truthy(_kw.get("nudge")))
        if err:
            return f"Error: {err}"
        clip = dict(resolved.clip.data)
        clip["id"] = str(resolved.clip.id)
        partners = edit_ops.partners_for(clip["id"], _all_clip_dicts(app))
        result = edit_ops.ripple_trim(
            clip,
            _all_clip_dicts(app),
            edge=str(edge or "out"),
            delta_frames=int(delta),
            fps=project_fps_fraction(),
            layers=_layers(app),
            partners=partners,
        )
        if not result.get("ok") or result.get("no_op"):
            return _receipt_json(result)
        _apply_patches(app, result.get("patches") or [], result.get("delete_ids") or [])
        after = {c["id"]: c for c in _all_clip_dicts(app)}
        ids = result["changed_ids"] + result["shifted_ids"]
        result["after"] = edit_ops.snapshots_by_id(after[i] for i in ids if i in after)
        return _receipt_json(result)
    except Exception as exc:
        return f"Error: {exc}"


def lift_clips(clipIds="", clip_ids="", timeline_clip_id="", **_kw) -> str:
    try:
        app = _app()
        ids = _parse_ids(clipIds, clip_ids, timeline_clip_id, _kw.get("clipId"))
        if not ids and (_kw.get("clip_query") or timeline_clip_id):
            resolved = _resolve_one(timeline_clip_id=timeline_clip_id, **_kw)
            if not resolved.ok:
                return resolved.error or "Error: could not resolve clip"
            ids = [str(resolved.clip.id)]
        result = edit_ops.lift(ids, _all_clip_dicts(app), layers=_layers(app))
        if not result.get("ok"):
            return _receipt_json(result)
        _apply_patches(app, [], result.get("delete_ids") or [])
        return _receipt_json(result)
    except Exception as exc:
        return f"Error: {exc}"


def extract_clips(clipIds="", clip_ids="", timeline_clip_id="", **_kw) -> str:
    try:
        app = _app()
        ids = _parse_ids(clipIds, clip_ids, timeline_clip_id, _kw.get("clipId"))
        if not ids and (_kw.get("clip_query") or timeline_clip_id):
            resolved = _resolve_one(timeline_clip_id=timeline_clip_id, **_kw)
            if not resolved.ok:
                return resolved.error or "Error: could not resolve clip"
            ids = [str(resolved.clip.id)]
        result = edit_ops.extract(
            ids, _all_clip_dicts(app), fps=project_fps_fraction(), layers=_layers(app),
        )
        if not result.get("ok"):
            return _receipt_json(result)
        _apply_patches(app, result.get("patches") or [], result.get("delete_ids") or [])
        after = {c["id"]: c for c in _all_clip_dicts(app)}
        result["after"] = edit_ops.snapshots_by_id(after[i] for i in result["shifted_ids"] if i in after)
        return _receipt_json(result)
    except Exception as exc:
        return f"Error: {exc}"


def get_edit_mode(**_kw) -> str:
    try:
        app = _app()
        mode = str(app.project.get("edit_mode") or "insert").strip().lower()
        if mode not in ("insert", "overwrite"):
            mode = "insert"
        return _receipt_json(edit_ops.receipt(
            ok=True, op="get_edit_mode", status=edit_ops.STATUS_APPLIED, undo="none",
            extra={"edit_mode": mode},
        ))
    except Exception as exc:
        return f"Error: {exc}"


def set_edit_mode(mode="", **_kw) -> str:
    try:
        app = _app()
        from classes.tool_handlers import _transaction
        value = str(mode or _kw.get("edit_mode") or "").strip().lower()
        if value not in ("insert", "overwrite"):
            return _receipt_json(edit_ops.receipt(
                ok=False, op="set_edit_mode", error="mode must be insert or overwrite",
                status=edit_ops.STATUS_REFUSED,
            ))
        before = str(app.project.get("edit_mode") or "insert")
        if before == value:
            return _receipt_json(edit_ops.receipt(
                ok=True, op="set_edit_mode", no_op=True, extra={"edit_mode": value},
            ))
        # Project-level key via updates API if available
        with _transaction(app):
            if hasattr(app.project, "set"):
                app.project.set("edit_mode", value)
            else:
                app.project["edit_mode"] = value
        return _receipt_json(edit_ops.receipt(
            ok=True, op="set_edit_mode",
            before={"edit_mode": before}, after={"edit_mode": value},
            extra={"edit_mode": value},
        ))
    except Exception as exc:
        return f"Error: {exc}"


def set_speed_ramp(
    timeline_clip_id="",
    clipId="",
    keypoints="",
    frames="",
    **_kw,
) -> str:
    """Apply a Bezier time-curve speed ramp. keypoints JSON list of {seconds, speed}."""
    try:
        import json as _json
        from classes.speed_ramp import apply_speed_ramp_to_clip_data
        from classes.tool_handlers import _transaction

        app = _app()
        resolved = _resolve_one(
            timeline_clip_id=timeline_clip_id or clipId or _kw.get("clip_id"),
            **_kw,
        )
        if not resolved.ok:
            return resolved.error or "Error: could not resolve clip"
        raw = keypoints or _kw.get("points") or _kw.get("ramp")
        if isinstance(raw, str):
            try:
                kps = _json.loads(raw) if raw.strip() else []
            except Exception:
                return "Error: keypoints must be JSON list of {seconds, speed}"
        elif isinstance(raw, list):
            kps = raw
        else:
            return "Error: keypoints required"
        before = dict(resolved.clip.data)
        before["id"] = str(resolved.clip.id)
        try:
            patched = apply_speed_ramp_to_clip_data(before, kps, fps=project_fps_fraction())
        except ValueError as exc:
            return _receipt_json(edit_ops.receipt(
                ok=False, op="set_speed_ramp", error=str(exc),
                status=edit_ops.STATUS_REFUSED,
            ))
        with _transaction(app):
            resolved.clip.data = patched
            resolved.clip.save()
        after = dict(resolved.clip.data)
        after["id"] = str(resolved.clip.id)
        return _receipt_json(edit_ops.receipt(
            ok=True,
            op="set_speed_ramp",
            changed_ids=[after["id"]],
            before=edit_ops.snapshots_by_id([before]),
            after=edit_ops.snapshots_by_id([after]),
            extra={"time_points": len((patched.get("time") or {}).get("Points") or [])},
        ))
    except Exception as exc:
        return f"Error: {exc}"


def create_proxies(file_ids="", fileIds="", **_kw) -> str:
    """Queue Optimize Preview proxies for media-bin files (background)."""
    try:
        from classes.query import File
        app = _app()
        ids = _parse_ids(file_ids, fileIds, _kw.get("file_id"))
        files = []
        for fid in ids:
            f = File.get(id=fid)
            if f:
                files.append(f)
        if not files and not ids:
            # All project files
            files = list(File.filter())
        service = getattr(app.window, "proxy_service", None) if hasattr(app, "window") else None
        if service is None:
            return "Error: proxy service unavailable"
        service.create_for_files(files)
        return _receipt_json(edit_ops.receipt(
            ok=True, op="create_proxies", status=edit_ops.STATUS_APPLIED, undo="none",
            extra={"queued": len(files), "note": "proxies never used on final export"},
        ))
    except Exception as exc:
        return f"Error: {exc}"


def remove_proxies(file_ids="", fileIds="", **_kw) -> str:
    try:
        from classes.query import File
        app = _app()
        ids = _parse_ids(file_ids, fileIds, _kw.get("file_id"))
        files = [File.get(id=i) for i in ids]
        files = [f for f in files if f]
        service = getattr(app.window, "proxy_service", None) if hasattr(app, "window") else None
        if service is None:
            return "Error: proxy service unavailable"
        if hasattr(service, "delete_and_unlink_for_files"):
            service.delete_and_unlink_for_files(files)
        elif hasattr(service, "unlink_for_files"):
            service.unlink_for_files(files)
        else:
            return "Error: proxy service missing unlink API"
        return _receipt_json(edit_ops.receipt(
            ok=True, op="remove_proxies", status=edit_ops.STATUS_APPLIED, undo="none",
            extra={"count": len(files)},
        ))
    except Exception as exc:
        return f"Error: {exc}"


def set_proxy_mode(mode="", **_kw) -> str:
    """Record preferred preview proxy mode on the project (preview|source). Export always source."""
    try:
        app = _app()
        from classes.tool_handlers import _transaction
        value = str(mode or _kw.get("proxy_mode") or "").strip().lower()
        if value not in ("preview", "source", "proxy", "original"):
            return _receipt_json(edit_ops.receipt(
                ok=False, op="set_proxy_mode",
                error="mode must be preview/proxy or source/original",
                status=edit_ops.STATUS_REFUSED,
            ))
        normalized = "preview" if value in ("preview", "proxy") else "source"
        with _transaction(app):
            if hasattr(app.project, "set"):
                app.project.set("proxy_mode", normalized)
            else:
                app.project["proxy_mode"] = normalized
        return _receipt_json(edit_ops.receipt(
            ok=True, op="set_proxy_mode",
            extra={"proxy_mode": normalized, "export": "always source"},
        ))
    except Exception as exc:
        return f"Error: {exc}"


def three_point_edit(**_kw) -> str:
    """v1 placeholder: validate marks; full insert/overwrite lands with UI wiring."""
    source_in = _kw.get("source_in")
    source_out = _kw.get("source_out")
    record_in = _kw.get("record_in")
    record_out = _kw.get("record_out")
    marks = [source_in, source_out, record_in, record_out]
    present = sum(1 for m in marks if m is not None and str(m).strip() != "")
    if present < 3:
        return _receipt_json(edit_ops.receipt(
            ok=False,
            op="three_point",
            error="three-point edit needs source In/Out + record In, or source In + record In/Out",
            status=edit_ops.STATUS_REFUSED,
        ))
    return _receipt_json(edit_ops.receipt(
        ok=False,
        op="three_point",
        error="three_point_edit apply path not yet wired to timeline insert/overwrite",
        status=edit_ops.STATUS_REFUSED,
    ))


# Names registered on AGENT_TOOL_HANDLERS
HANDLER_MAP = {
    "manage_clip_links_tool": manage_clip_links,
    "set_track_sync_lock_tool": set_track_sync_lock,
    "slip_clip_tool": slip_clip,
    "slide_clip_tool": slide_clip,
    "roll_edit_tool": roll_edit,
    "ripple_trim_tool": ripple_trim,
    "lift_clips_tool": lift_clips,
    "extract_clips_tool": extract_clips,
    "get_edit_mode_tool": get_edit_mode,
    "set_edit_mode_tool": set_edit_mode,
    "three_point_edit_tool": three_point_edit,
    "set_speed_ramp_tool": set_speed_ramp,
    "create_proxies_tool": create_proxies,
    "remove_proxies_tool": remove_proxies,
    "set_proxy_mode_tool": set_proxy_mode,
}

DISPLAY_LABELS = {
    "manage_clip_links_tool": "Manage clip links",
    "set_track_sync_lock_tool": "Set track sync-lock",
    "slip_clip_tool": "Slip clip",
    "slide_clip_tool": "Slide clip",
    "roll_edit_tool": "Roll edit",
    "ripple_trim_tool": "Ripple trim",
    "lift_clips_tool": "Lift clips",
    "extract_clips_tool": "Extract clips",
    "get_edit_mode_tool": "Get edit mode",
    "set_edit_mode_tool": "Set edit mode",
    "three_point_edit_tool": "Three-point edit",
    "set_speed_ramp_tool": "Set speed ramp",
    "create_proxies_tool": "Create proxies",
    "remove_proxies_tool": "Remove proxies",
    "set_proxy_mode_tool": "Set proxy mode",
}
