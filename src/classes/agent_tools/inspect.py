"""inspect_timeline_tool handler."""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("agent_tools.inspect")

_DEFAULT_FRAMES = 6
_MAX_FRAMES = 12


def _clamp_max_frames(raw) -> tuple[int, list[str]]:
    warnings: list[str] = []
    try:
        n = int(raw) if raw is not None else _DEFAULT_FRAMES
    except (TypeError, ValueError):
        n = _DEFAULT_FRAMES
    if n < 1:
        n = 1
    if n > _MAX_FRAMES:
        warnings.append(f"max_frames clamped from {n} to {_MAX_FRAMES}")
        n = _MAX_FRAMES
    return n, warnings


def _find_clip(project_data: dict, clip_id: str) -> Optional[dict]:
    for clip in project_data.get("clips") or []:
        if isinstance(clip, dict) and str(clip.get("id")) == clip_id:
            return clip
    return None


def _range_error(sf: int, ef: int, total: int):
    """A refusal when frames [sf, ef) are not all inside the project, else None."""
    from classes.agent_tools.receipt import ToolReceipt

    if sf < 0 or (total > 0 and ef > total):
        return ToolReceipt.refused(
            "inspect_timeline_tool",
            f"Error: frames [{sf}, {ef}) out of range [0, {total}).",
        )
    return None


def inspect_timeline(
    start_frame=None,
    end_frame=None,
    max_frames=None,
    timeline_clip_id="",
    start=None,
    end=None,
    overview=False,
    **_kw,
):
    """See the composited timeline at given frames (0-index) or watchSuggested window."""
    from classes.agent_tools.inspect_overlay import COORDINATE_GRID_NOTE
    from classes.agent_tools.inspect_render import (
        InspectBusy,
        InspectCancelled,
        project_dims,
        render_timeline_frames,
        snapshot_project,
        total_frames_0,
    )
    from classes.agent_tools.output import ImageBlock, ToolOutput, trim_to_budget
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.visible_clips import mid_bin_frames

    try:
        from classes.app import get_app
        app = get_app()
    except Exception as exc:
        return ToolReceipt.error("inspect_timeline_tool", str(exc))

    try:
        project_data = snapshot_project(app)
    except Exception as exc:
        return ToolReceipt.error("inspect_timeline_tool", f"snapshot failed: {exc}")

    width, height, fps, *_rest = project_dims(project_data)
    total = total_frames_0(project_data, fps)
    warnings: list[str] = []
    overview = bool(overview)
    mode = "overview" if overview else "frames"

    if timeline_clip_id and (start is not None or end is not None):
        clip = _find_clip(project_data, str(timeline_clip_id))
        if clip is None:
            return ToolReceipt.refused(
                "inspect_timeline_tool", f"Error: Unknown timeline_clip_id '{timeline_clip_id}'.",
            )
        try:
            t0 = float(start if start is not None else clip.get("position") or 0)
            t1 = float(
                end if end is not None else (
                    float(clip.get("position") or 0)
                    + max(0.0, float(clip.get("end") or 0) - float(clip.get("start") or 0))
                )
            )
        except (TypeError, ValueError):
            return ToolReceipt.refused(
                "inspect_timeline_tool", "Error: Invalid start/end for watchSuggested window.",
            )
        if t1 <= t0:
            return ToolReceipt.refused(
                "inspect_timeline_tool", "Error: end must be greater than start.",
            )
        from classes.frame_time import to_frame
        start_frame = to_frame(t0, fps)
        end_frame = to_frame(t1, fps)
        if end_frame <= start_frame:
            end_frame = start_frame + 1

    if start_frame is None and not overview:
        return ToolReceipt.refused(
            "inspect_timeline_tool",
            "Error: Provide start_frame, or timeline_clip_id+start+end, or overview=true.",
        )

    if total <= 0 and not (project_data.get("clips") or []):
        return ToolReceipt.refused(
            "inspect_timeline_tool", "Error: Timeline is empty — nothing to render.",
        )

    max_n, w = _clamp_max_frames(max_frames)
    warnings.extend(w)

    if overview:
        if start_frame is not None and end_frame is not None:
            sf, ef = int(start_frame), int(end_frame)
        elif start_frame is not None:
            sf, ef = int(start_frame), max(int(start_frame) + 1, total)
        else:
            sf, ef = 0, max(1, total)
        if ef <= sf:
            return ToolReceipt.refused(
                "inspect_timeline_tool", "Error: end_frame must be greater than start_frame.",
            )
        if (out_of_range := _range_error(sf, ef, total)) is not None:
            return out_of_range
        frames_0 = mid_bin_frames(sf, ef, min(36, max(6, ef - sf)))
    elif end_frame is None or int(end_frame or 0) == 0:
        sf = int(start_frame)
        if sf < 0 or (total > 0 and sf >= total):
            return ToolReceipt.refused(
                "inspect_timeline_tool",
                f"Error: start_frame {sf} out of range [0, {total}).",
            )
        frames_0 = [sf]
    else:
        sf, ef = int(start_frame), int(end_frame)
        if ef <= sf:
            return ToolReceipt.refused(
                "inspect_timeline_tool", "Error: end_frame must be greater than start_frame.",
            )
        if (out_of_range := _range_error(sf, ef, total)) is not None:
            return out_of_range
        frames_0 = mid_bin_frames(sf, ef, max_n)

    try:
        rendered = render_timeline_frames(project_data, frames_0, live_timeline=None)
    except InspectBusy:
        return ToolReceipt.refused("inspect_timeline_tool", "Error: inspect already running")
    except InspectCancelled:
        return ToolReceipt.error("inspect_timeline_tool", "Error: inspect cancelled")
    except Exception as exc:
        log.error("inspect_timeline failed: %s", exc, exc_info=True)
        return ToolReceipt.error("inspect_timeline_tool", str(exc))

    if not rendered:
        return ToolReceipt.error(
            "inspect_timeline_tool", "Error: Failed to render timeline frames.",
        )

    images = [ImageBlock(data=r["jpeg"]) for r in rendered]
    data_frames = [
        {"frame": r["frame"], "seconds": r["seconds"], "clips": r["clips"]}
        for r in rendered
    ]
    data_overview = None
    if mode == "overview" and len(rendered) > 1:
        sheet_out = _overview_sheet(rendered, time_key="seconds")
        if sheet_out is not None:
            images = [ImageBlock(data=sheet_out["jpeg"])]
            data_overview = {"tileTimestamps": sheet_out["timestamps"]}

    images, budget_warns = trim_to_budget(images)
    warnings.extend(budget_warns)
    if not images:
        return ToolReceipt.error(
            "inspect_timeline_tool",
            "Error: inspect image budget exceeded before first frame.",
        )
    if mode != "overview" and len(images) < len(data_frames):
        data_frames = data_frames[: len(images)]

    data: dict[str, Any] = {
        "coordinateGrid": COORDINATE_GRID_NOTE,
        "width": rendered[0].get("width"),
        "height": rendered[0].get("height"),
        "fps": float(fps),
        "totalFrames": total,
        "mode": mode,
        "requestedFrames": list(frames_0),
        "renderedFrames": [f["frame"] for f in data_frames],
        "frames": data_frames if mode != "overview" else [],
    }
    if data_overview is not None:
        data["overview"] = data_overview

    return ToolOutput(
        receipt=ToolReceipt.applied(
            "inspect_timeline_tool",
            f"Rendered {len(images)} inspect image(s).",
            undo_steps=0,
            warnings=warnings,
            data=data,
        ),
        images=images,
    )


def _overview_sheet(rendered: list[dict], *, time_key: str) -> Optional[dict]:
    try:
        from qt_api import QImage
        from classes.agent_tools.inspect_render import jpeg_bytes_from_qimage
        from classes.agent_tools.storyboard import (
            compose_sheet_qimage,
            dhash64,
            gray_8x9_from_qimage,
            select_storyboard_indexes,
        )
    except Exception:
        return None

    tiles, timestamps, fingerprints = [], [], []
    for r in rendered:
        img = QImage()
        img.loadFromData(r["jpeg"], "JPG")
        if img.isNull():
            continue
        tiles.append(img)
        timestamps.append(float(r[time_key]))
        try:
            fingerprints.append(dhash64(gray_8x9_from_qimage(img)))
        except Exception:
            fingerprints.append(None)
    if not tiles:
        return None
    idxs = select_storyboard_indexes(fingerprints, timestamps)
    sheet = compose_sheet_qimage(
        [tiles[i] for i in idxs], [timestamps[i] for i in idxs],
    )
    if sheet is None:
        return None
    return {
        "jpeg": jpeg_bytes_from_qimage(sheet),
        "timestamps": [timestamps[i] for i in idxs],
    }
