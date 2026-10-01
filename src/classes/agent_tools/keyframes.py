"""Agent keyframe writer — one property curve per call, frame-exact X."""

from __future__ import annotations

from fractions import Fraction

ALLOWED_PROPERTIES = frozenset({
    "volume",
    "alpha",
    "location_x",
    "location_y",
    "scale_x",
    "scale_y",
    "rotation",
})


def set_keyframes(
    timeline_clip_id: str = "",
    clip_query: str = "",
    track: str = "",
    property: str = "",
    points=None,
    occurrence: str = "0",
    position_near=None,
    **_kw,
) -> str:
    """Set keyframes for one clip property.

    Each point is ``{frame, value}`` (1-indexed openshot X) or
    ``{seconds, value}`` converted through ``frame_time.keyframe_x``.
    """
    from classes.agent_tools.receipt import ToolReceipt
    from classes.frame_time import keyframe_x
    from classes.tool_handlers import (
        QThread,
        _get_app,
        _resolve_timeline_clip_for_tool,
        _run_on_main_thread,
    )

    prop = str(property or "").strip()
    if prop not in ALLOWED_PROPERTIES:
        return ToolReceipt.refused(
            "set_keyframes_tool",
            f"Error: property must be one of {sorted(ALLOWED_PROPERTIES)} "
            f"(got {property!r}).",
        ).to_json()

    if not isinstance(points, list) or not points:
        return ToolReceipt.refused(
            "set_keyframes_tool",
            "Error: points must be a non-empty array of "
            "{frame|seconds, value} objects.",
        ).to_json()

    resolved = _resolve_timeline_clip_for_tool(
        timeline_clip_id=timeline_clip_id,
        clip_query=clip_query,
        track=track,
        occurrence=occurrence,
        position_near=position_near,
    )
    if getattr(resolved, "error", None):
        return ToolReceipt.error("set_keyframes_tool", resolved.error).to_json()
    if not getattr(resolved, "ok", False) or resolved.clip is None:
        return ToolReceipt.error(
            "set_keyframes_tool",
            resolved.error or "Error: could not resolve a timeline clip.",
        ).to_json()
    clip = resolved.clip

    fps = Fraction(30, 1)
    try:
        from classes.clip_utils import project_fps_fraction
        fps = project_fps_fraction()
    except Exception:
        pass

    built = []
    for i, raw in enumerate(points):
        if not isinstance(raw, dict):
            return ToolReceipt.refused(
                "set_keyframes_tool",
                f"Error: points[{i}] must be an object.",
            ).to_json()
        if "value" not in raw:
            return ToolReceipt.refused(
                "set_keyframes_tool",
                f"Error: points[{i}] needs value.",
            ).to_json()
        try:
            value = float(raw["value"])
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "set_keyframes_tool",
                f"Error: points[{i}].value must be a number.",
            ).to_json()
        if raw.get("frame") is not None:
            try:
                x = int(raw["frame"])
            except (TypeError, ValueError):
                return ToolReceipt.refused(
                    "set_keyframes_tool",
                    f"Error: points[{i}].frame must be an integer.",
                ).to_json()
            if x < 1:
                return ToolReceipt.refused(
                    "set_keyframes_tool",
                    f"Error: points[{i}].frame must be >= 1.",
                ).to_json()
        elif raw.get("seconds") is not None:
            try:
                secs = float(raw["seconds"])
            except (TypeError, ValueError):
                return ToolReceipt.refused(
                    "set_keyframes_tool",
                    f"Error: points[{i}].seconds must be a number.",
                ).to_json()
            x = keyframe_x(secs, fps)
        else:
            return ToolReceipt.refused(
                "set_keyframes_tool",
                f"Error: points[{i}] needs frame or seconds.",
            ).to_json()
        built.append({"co": {"X": x, "Y": value}, "interpolation": 2})

    built.sort(key=lambda p: p["co"]["X"])
    app = _get_app()
    error_box = [None]

    def _do():
        try:
            clip.data = {prop: {"Points": built}}
            clip.save()
        except Exception as exc:
            error_box[0] = str(exc)

    if QThread is not None and QThread.currentThread() is not app.thread():
        _run_on_main_thread(_do)
    else:
        _do()

    if error_box[0]:
        return ToolReceipt.error("set_keyframes_tool", error_box[0]).to_json()

    cid = str(getattr(clip, "id", "") or "")
    return ToolReceipt.applied(
        "set_keyframes_tool",
        f"Set {len(built)} keyframe(s) on {prop} for timeline_clip_id={cid}.",
        clips=[{"id": cid}],
        data={
            "timeline_clip_id": cid,
            "property": prop,
            "points": [{"frame": p["co"]["X"], "value": p["co"]["Y"]} for p in built],
        },
    ).to_json()
