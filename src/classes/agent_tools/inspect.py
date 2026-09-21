"""inspect_timeline_tool and inspect_media_tool handlers."""

from __future__ import annotations

import logging
import os
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
        warnings.append(f"maxFrames clamped from {n} to {_MAX_FRAMES}")
        n = _MAX_FRAMES
    return n, warnings


def _find_clip(project_data: dict, clip_id: str) -> Optional[dict]:
    for clip in project_data.get("clips") or []:
        if isinstance(clip, dict) and str(clip.get("id")) == clip_id:
            return clip
    return None


def inspect_timeline(
    startFrame=None,
    endFrame=None,
    maxFrames=None,
    clipId="",
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

    if clipId and (start is not None or end is not None):
        clip = _find_clip(project_data, str(clipId))
        if clip is None:
            return ToolReceipt.refused(
                "inspect_timeline_tool", f"Error: Unknown clipId '{clipId}'.",
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
        startFrame = to_frame(t0, fps)
        endFrame = to_frame(t1, fps)
        if endFrame <= startFrame:
            endFrame = startFrame + 1

    if startFrame is None and not overview:
        return ToolReceipt.refused(
            "inspect_timeline_tool",
            "Error: Provide startFrame, or clipId+start+end, or overview=true.",
        )

    if total <= 0 and not (project_data.get("clips") or []):
        return ToolReceipt.refused(
            "inspect_timeline_tool", "Error: Timeline is empty — nothing to render.",
        )

    max_n, w = _clamp_max_frames(maxFrames)
    warnings.extend(w)

    if overview:
        if startFrame is not None and endFrame is not None:
            sf, ef = int(startFrame), int(endFrame)
        elif startFrame is not None:
            sf, ef = int(startFrame), max(int(startFrame) + 1, total)
        else:
            sf, ef = 0, max(1, total)
        if ef <= sf:
            return ToolReceipt.refused(
                "inspect_timeline_tool", "Error: endFrame must be greater than startFrame.",
            )
        frames_0 = mid_bin_frames(sf, ef, min(36, max(6, ef - sf)))
    elif endFrame is None or int(endFrame or 0) == 0:
        sf = int(startFrame)
        if sf < 0 or (total > 0 and sf >= total):
            return ToolReceipt.refused(
                "inspect_timeline_tool",
                f"Error: startFrame {sf} out of range [0, {total}).",
            )
        frames_0 = [sf]
    else:
        sf, ef = int(startFrame), int(endFrame)
        if ef <= sf:
            return ToolReceipt.refused(
                "inspect_timeline_tool", "Error: endFrame must be greater than startFrame.",
            )
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


def inspect_media(
    fileId="",
    clipId="",
    start=None,
    end=None,
    maxFrames=None,
    overview=False,
    **_kw,
):
    """See frames from a library file (or a storyboard overview). Visual only."""
    from classes.agent_tools.inspect_overlay import COORDINATE_GRID_NOTE
    from classes.agent_tools.inspect_render import (
        InspectBusy,
        InspectCancelled,
        render_media_frames,
    )
    from classes.agent_tools.output import ImageBlock, ToolOutput, trim_to_budget
    from classes.agent_tools.receipt import ToolReceipt
    from classes.agent_tools.visible_clips import mid_bin_seconds
    from classes.query import File

    file_id = str(fileId or "").strip()
    if not file_id:
        return ToolReceipt.refused("inspect_media_tool", "Error: fileId is required.")

    fobj = File.get(id=file_id)
    if not fobj:
        return ToolReceipt.refused(
            "inspect_media_tool", f"Error: Unknown fileId '{file_id}'.",
        )
    file_data = fobj.data if isinstance(fobj.data, dict) else {}
    try:
        path = fobj.absolute_path() if hasattr(fobj, "absolute_path") else (
            file_data.get("path") or ""
        )
    except Exception:
        path = file_data.get("path") or ""
    path = str(path or "")
    if not path or not os.path.isfile(path):
        return ToolReceipt.refused(
            "inspect_media_tool",
            f"Error: Media file not on disk: {os.path.basename(path) or file_id}",
        )

    has_video = file_data.get("has_video")
    has_audio = file_data.get("has_audio")
    media_type = str(file_data.get("media_type") or file_data.get("type") or "").lower()
    is_image = media_type in ("image", "photo") or path.lower().endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff")
    )
    is_audio = (has_video is False and has_audio) or media_type == "audio"

    warnings: list[str] = []
    max_n, w = _clamp_max_frames(maxFrames)
    warnings.extend(w)

    try:
        duration = float(file_data.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    data: dict[str, Any] = {
        "fileId": file_id,
        "coordinateGrid": COORDINATE_GRID_NOTE,
        "mode": "overview" if overview else "frames",
    }
    if duration:
        data["duration"] = duration

    if is_audio and not is_image:
        data["channels"] = file_data.get("channels")
        data["sample_rate"] = file_data.get("sample_rate")
        data["hasAudio"] = True
        data["hasVideo"] = False
        return ToolOutput(
            receipt=ToolReceipt.applied(
                "inspect_media_tool",
                "Audio file — metadata only (no frames).",
                undo_steps=0,
                data=data,
            ),
            images=[],
        )

    try:
        t0 = float(start) if start is not None else 0.0
    except (TypeError, ValueError):
        t0 = 0.0
    try:
        t1 = float(end) if end is not None else (duration if duration > 0 else 0.0)
    except (TypeError, ValueError):
        t1 = duration

    if is_image:
        timestamps = [0.0]
    else:
        if t1 <= t0:
            if duration > 0:
                t0, t1 = 0.0, duration
            else:
                return ToolReceipt.refused(
                    "inspect_media_tool", "Error: Invalid time range.",
                )
        if overview:
            n = min(48, max(12, int((t1 - t0) * 2) or 12))
            timestamps = mid_bin_seconds(t0, t1, n)
        else:
            timestamps = mid_bin_seconds(t0, t1, max_n)

    try:
        rendered = render_media_frames(path, timestamps, with_overlay=not overview)
    except InspectBusy:
        return ToolReceipt.refused("inspect_media_tool", "Error: inspect already running")
    except InspectCancelled:
        return ToolReceipt.error("inspect_media_tool", "Error: inspect cancelled")
    except FileNotFoundError:
        return ToolReceipt.refused(
            "inspect_media_tool",
            f"Error: Media file not on disk: {os.path.basename(path)}",
        )
    except Exception as exc:
        log.error("inspect_media failed: %s", exc, exc_info=True)
        return ToolReceipt.error("inspect_media_tool", str(exc))

    if not rendered:
        return ToolReceipt.error("inspect_media_tool", "Error: Failed to extract frames.")

    images = [ImageBlock(data=r["jpeg"]) for r in rendered]
    data["frameTimestamps"] = [r["timestamp"] for r in rendered]
    data["width"] = rendered[0].get("width")
    data["height"] = rendered[0].get("height")

    if overview and len(rendered) > 1:
        sheet = _overview_sheet(rendered, time_key="timestamp")
        if sheet is None:
            return ToolReceipt.error("inspect_media_tool", "Error: Overview failed.")
        images = [ImageBlock(data=sheet["jpeg"])]
        data["overview"] = {"tileTimestamps": sheet["timestamps"]}
        data.pop("frameTimestamps", None)
        data["mode"] = "overview"

    images, budget_warns = trim_to_budget(images)
    warnings.extend(budget_warns)

    if clipId:
        mapping = _timeline_mapping(str(clipId), file_id)
        if mapping:
            data["timelineMapping"] = mapping

    return ToolOutput(
        receipt=ToolReceipt.applied(
            "inspect_media_tool",
            f"Rendered {len(images)} inspect image(s).",
            undo_steps=0,
            warnings=warnings,
            data=data,
        ),
        images=images,
    )


def _overview_sheet(rendered: list[dict], *, time_key: str) -> Optional[dict]:
    try:
        from PyQt5.QtGui import QImage
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


def _timeline_mapping(clip_id: str, file_id: str) -> Optional[dict]:
    try:
        from classes.app import get_app
        from classes.clip_utils import project_fps_fraction
        from classes.frame_time import to_frame

        app = get_app()
        clips = app.project.get("clips") or []
        fps = project_fps_fraction()
        for clip in clips:
            data = clip if isinstance(clip, dict) else getattr(clip, "data", {})
            if not isinstance(data, dict) or str(data.get("id")) != clip_id:
                continue
            reader = data.get("reader") if isinstance(data.get("reader"), dict) else {}
            ref = str(data.get("file_id") or reader.get("id") or "")
            if ref and ref != file_id:
                return None
            position = float(data.get("position") or 0)
            start = float(data.get("start") or 0)
            end = float(data.get("end") or start)
            return {
                "clipId": clip_id,
                "startFrame": to_frame(position, fps),
                "endFrame": to_frame(position + max(0.0, end - start), fps),
                "fps": float(fps),
                "note": "times are project frames (0-index)",
            }
    except Exception:
        return None
    return None
