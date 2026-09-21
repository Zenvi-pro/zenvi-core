"""Private Timeline / media frame renderer for agent inspect tools.

Affinity (measured): libopenshot GetFrame + Thumbnail work on a worker
thread under Python 3.11 + offscreen Qt. Inspect stays BACKGROUND_SAFE.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from typing import Any, Callable, Optional

log = logging.getLogger("agent_tools.inspect_render")

_GATE = threading.BoundedSemaphore(1)
_CANCEL = threading.Event()
LONGEST_EDGE = 512
JPEG_QUALITY = 70


class InspectBusy(Exception):
    pass


class InspectCancelled(Exception):
    pass


def request_cancel() -> None:
    _CANCEL.set()


def clear_cancel() -> None:
    _CANCEL.clear()


def _check_cancel() -> None:
    if _CANCEL.is_set():
        raise InspectCancelled("inspect cancelled")


def acquire_gate(*, timeout: float = 0.1) -> bool:
    return _GATE.acquire(timeout=timeout)


def release_gate() -> None:
    try:
        _GATE.release()
    except ValueError:
        pass


def absolute_project_paths(project_data: dict) -> dict:
    data = copy.deepcopy(project_data)
    try:
        from classes.path_utils import absolute_media_path
    except Exception:
        return data

    def _fix(obj: Any) -> None:
        if isinstance(obj, dict):
            path = obj.get("path")
            if isinstance(path, str) and path:
                try:
                    obj["path"] = absolute_media_path(path) or path
                except Exception:
                    pass
            for v in obj.values():
                _fix(v)
        elif isinstance(obj, list):
            for item in obj:
                _fix(item)

    _fix(data.get("files") or [])
    _fix(data.get("clips") or [])
    _fix(data.get("effects") or [])
    return data


def snapshot_project(app) -> dict:
    project = app.project
    try:
        cloned = copy.deepcopy(project)
        data = getattr(cloned, "_data", None)
        if isinstance(data, dict):
            return absolute_project_paths(data)
    except Exception:
        pass
    raw = getattr(project, "_data", None)
    if not isinstance(raw, dict):
        raise RuntimeError("project has no _data dict")
    return absolute_project_paths(copy.deepcopy(raw))


def project_dims(project_data: dict, app=None):
    from fractions import Fraction

    width = int(project_data.get("width") or 1920)
    height = int(project_data.get("height") or 1080)
    fps_obj = project_data.get("fps") or {}
    try:
        fps = Fraction(int(fps_obj.get("num") or 30), int(fps_obj.get("den") or 1))
    except Exception:
        fps = Fraction(30, 1)
    sample_rate = int(project_data.get("sample_rate") or 48000)
    channels = int(project_data.get("channels") or 2)
    channel_layout = int(project_data.get("channel_layout") or 4)
    return width, height, fps, sample_rate, channels, channel_layout


def total_frames_0(project_data: dict, fps) -> int:
    from classes.frame_time import to_frame

    duration = project_data.get("duration")
    try:
        if duration is not None:
            return max(0, to_frame(float(duration), fps))
    except Exception:
        pass
    end = 0.0
    for clip in project_data.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        try:
            pos = float(clip.get("position") or 0)
            start = float(clip.get("start") or 0)
            cend = float(clip.get("end") or start)
            end = max(end, pos + max(0.0, cend - start))
        except Exception:
            continue
    try:
        return max(0, to_frame(end, fps))
    except Exception:
        return 0


def jpeg_bytes_from_qimage(qimage) -> bytes:
    from PyQt5.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    qimage.save(buf, "JPG", JPEG_QUALITY)
    data = bytes(buf.data())
    buf.close()
    return data


def _qimage_from_frame(frame, width: int, height: int):
    from PyQt5.QtGui import QImage

    if hasattr(frame, "GetImage"):
        try:
            img = frame.GetImage()
            if isinstance(img, QImage) and not img.isNull():
                return img
            if img is not None and hasattr(img, "isNull") and not img.isNull():
                return img
        except Exception as exc:
            log.debug("GetImage failed: %s", exc)

    tmp = tempfile.NamedTemporaryFile(prefix="zenvi_inspect_", suffix=".jpg", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        frame.Thumbnail(tmp_path, width, height, "", "", "#000000", True, "jpg", JPEG_QUALITY, 0.0)
        img = QImage(tmp_path)
        return None if img.isNull() else img
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def render_timeline_frames(
    project_data: dict,
    frames_0: list[int],
    *,
    longest_edge: int = LONGEST_EDGE,
    live_timeline=None,
) -> list[dict]:
    import openshot
    from classes.frame_time import to_seconds
    from classes.agent_tools.inspect_overlay import (
        COORDINATE_GRID_NOTE,
        apply_overlay_qimage,
        fit_size,
        format_frame_caption,
    )
    from classes.agent_tools.visible_clips import visible_clips_at

    del live_timeline  # explicitly unused — never GetFrame the live object

    if not acquire_gate(timeout=0.1):
        raise InspectBusy("inspect already running")
    clear_cancel()
    timeline = None
    results: list[dict] = []
    try:
        width, height, fps, sample_rate, channels, channel_layout = project_dims(project_data)
        rw, rh = fit_size(width, height, longest_edge)
        timeline = openshot.Timeline(
            width, height, openshot.Fraction(fps.numerator, fps.denominator),
            sample_rate, channels, channel_layout,
        )
        timeline.info.sample_rate = sample_rate
        timeline.info.channels = channels
        timeline.info.channel_layout = channel_layout
        timeline.SetJson(json.dumps(project_data))
        timeline.Open()
        try:
            timeline.ApplyMapperToClips()
        except Exception:
            pass
        timeline.SetMaxSize(rw, rh)

        for frame_0 in frames_0:
            _check_cancel()
            openshot_frame = int(frame_0) + 1
            try:
                frame = timeline.GetFrame(openshot_frame)
            except Exception as exc:
                log.warning("GetFrame(%s) failed: %s", openshot_frame, exc)
                continue
            if frame is None:
                continue
            qimg = _qimage_from_frame(frame, rw, rh)
            if qimg is None or qimg.isNull():
                continue
            if qimg.width() != rw or qimg.height() != rh:
                from PyQt5.QtCore import Qt
                qimg = qimg.scaled(rw, rh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            seconds = to_seconds(int(frame_0), fps)
            apply_overlay_qimage(qimg, caption=format_frame_caption(int(frame_0), seconds))
            jpeg = jpeg_bytes_from_qimage(qimg)
            results.append({
                "frame": int(frame_0),
                "seconds": float(seconds),
                "jpeg": jpeg,
                "clips": visible_clips_at(project_data, seconds=seconds, fps=fps),
                "width": qimg.width(),
                "height": qimg.height(),
                "coordinateGrid": COORDINATE_GRID_NOTE,
            })
        return results
    finally:
        try:
            if timeline is not None:
                timeline.Close()
        except Exception:
            pass
        release_gate()


def render_media_frames(
    path: str,
    timestamps: list[float],
    *,
    longest_edge: int = LONGEST_EDGE,
    with_overlay: bool = True,
) -> list[dict]:
    import openshot
    from classes.agent_tools.inspect_overlay import (
        COORDINATE_GRID_NOTE,
        apply_overlay_qimage,
        fit_size,
        format_timecode,
    )

    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    if not acquire_gate(timeout=0.1):
        raise InspectBusy("inspect already running")
    clear_cancel()
    reader = None
    results: list[dict] = []
    try:
        reader = openshot.FFmpegReader(path)
        reader.Open()
        info = reader.info
        width = int(getattr(info, "width", 0) or 1280)
        height = int(getattr(info, "height", 0) or 720)
        fps_num = int(getattr(getattr(info, "fps", None), "num", 30) or 30)
        fps_den = int(getattr(getattr(info, "fps", None), "den", 1) or 1)
        rw, rh = fit_size(width, height, longest_edge)
        try:
            reader.SetMaxSize(rw, rh)
        except Exception:
            pass
        for ts in timestamps:
            _check_cancel()
            frame_1 = max(1, int(round(float(ts) * fps_num / fps_den)) + 1)
            try:
                frame = reader.GetFrame(frame_1)
            except Exception as exc:
                log.warning("media GetFrame(%s) failed: %s", frame_1, exc)
                continue
            if frame is None:
                continue
            qimg = _qimage_from_frame(frame, rw, rh)
            if qimg is None or qimg.isNull():
                continue
            if with_overlay:
                apply_overlay_qimage(qimg, caption=format_timecode(float(ts)))
            results.append({
                "timestamp": float(ts),
                "jpeg": jpeg_bytes_from_qimage(qimg),
                "width": qimg.width(),
                "height": qimg.height(),
                "coordinateGrid": COORDINATE_GRID_NOTE,
            })
        return results
    finally:
        try:
            if reader is not None:
                reader.Close()
        except Exception:
            pass
        release_gate()
