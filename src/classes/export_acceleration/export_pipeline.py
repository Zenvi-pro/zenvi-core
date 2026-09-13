"""Pipelined export: overlap Timeline.GetFrame with FFmpegWriter.WriteFrame.

Phase 3a: one composite thread + one encoder thread + bounded queue.
Phase 3b: N composite workers on cloned Timeline instances + reorder buffer.

Timeline construction and GetFrame stay on worker threads (not the UI thread).
Progress is reported via an optional callback (Qt signal from the dialog).
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from classes.export_acceleration.export_tuning import (
    ExportPipelineProfile,
    get_export_pipeline_profile,
)
from classes.logger import log


class PipelineCancelled(Exception):
    """Raised when export is cancelled mid-flight."""


@dataclass
class _CompositedFrame:
    frame_number: int
    frame_obj: Any


def _clone_timeline(project_data: dict, video_settings: dict, audio_settings: dict, cache_bytes: int):
    """Build an export Timeline owned entirely by the calling thread."""
    import openshot

    fps = video_settings.get("fps") or {}
    fps_num = int(fps.get("num", 30))
    fps_den = int(fps.get("den", 1) or 1)
    width = int(video_settings.get("width", 1920))
    height = int(video_settings.get("height", 1080))
    sample_rate = int(audio_settings.get("sample_rate", 48000) or 48000)
    channels = int(audio_settings.get("channels", 2) or 2)
    channel_layout = int(audio_settings.get("channel_layout", openshot.LAYOUT_STEREO))

    timeline = openshot.Timeline(
        width,
        height,
        openshot.Fraction(fps_num, fps_den),
        sample_rate,
        channels,
        channel_layout,
    )
    timeline.info.sample_rate = sample_rate
    timeline.info.channels = channels
    timeline.info.channel_layout = channel_layout
    timeline.info.has_audio = bool(sample_rate and channels)
    timeline.info.has_video = True
    timeline.info.width = width
    timeline.info.height = height
    timeline.info.fps.num = fps_num
    timeline.info.fps.den = fps_den

    # Hold a Python reference for the lifetime of this timeline.
    cache = openshot.CacheMemory(int(cache_bytes))
    timeline.SetCache(cache)
    timeline.SetJson(json.dumps(project_data))
    timeline.Open()
    timeline.SetMaxSize(width, height)
    try:
        timeline.ApplyMapperToClips()
    except Exception:
        pass
    return timeline, cache


def run_pipelined_export(
    *,
    writer: Any,
    project_data: dict,
    video_settings: dict,
    audio_settings: dict,
    start_frame: int,
    end_frame: int,
    is_cancelled: Callable[[], bool],
    on_progress: Optional[Callable[[int, float], None]] = None,
    profile: Optional[ExportPipelineProfile] = None,
    existing_timeline: Any = None,
    existing_cache: Any = None,
) -> dict:
    """Composite and encode with overlapped threads.

    If *existing_timeline* is provided (dialog path), Phase 3a uses it on a
    single composite worker to avoid rebuilding. Phase 3b clones additional
    timelines from *project_data* when workers > 1.

    Returns metrics dict: {elapsed_sec, frames, fps, profile}.
    """
    width = int(video_settings.get("width", 1920))
    height = int(video_settings.get("height", 1080))
    fps_dict = video_settings.get("fps") or {}
    frame_rate = float(fps_dict.get("num", 30)) / float(fps_dict.get("den", 1) or 1)

    if profile is None:
        # Parallel composite (3b) is opt-in via settings; default to overlap-only.
        enable_parallel = bool(video_settings.get("_enable_parallel_composite", False))
        profile = get_export_pipeline_profile(
            width,
            height,
            frame_rate,
            enable_parallel_composite=enable_parallel,
        )

    start_frame = int(start_frame)
    end_frame = int(end_frame)
    if end_frame < start_frame:
        raise ValueError("end_frame must be >= start_frame")

    total_frames = end_frame - start_frame + 1
    pending: queue.Queue = queue.Queue(maxsize=profile.max_pending_frames)
    error_box: list[BaseException] = []
    cancel_event = threading.Event()
    next_frame_lock = threading.Lock()
    next_frame_to_assign = start_frame
    started_at = time.perf_counter()

    # Keep strong refs so SWIG does not free CacheMemory under C++.
    keep_alive: list[Any] = []

    def mark_error(exc: BaseException) -> None:
        if not error_box:
            error_box.append(exc)
        cancel_event.set()

    def check_cancel() -> None:
        if cancel_event.is_set() or is_cancelled():
            cancel_event.set()
            raise PipelineCancelled()

    def composite_worker(worker_id: int, timeline: Any) -> None:
        nonlocal next_frame_to_assign
        try:
            while True:
                check_cancel()
                with next_frame_lock:
                    frame_number = next_frame_to_assign
                    if frame_number > end_frame:
                        return
                    next_frame_to_assign += 1
                frame_obj = timeline.GetFrame(frame_number)
                pending.put(_CompositedFrame(frame_number, frame_obj))
        except PipelineCancelled:
            return
        except Exception as exc:
            mark_error(exc)

    # Build worker timelines
    timelines: list[Any] = []
    workers = max(1, int(profile.composite_workers))

    if existing_timeline is not None and workers == 1:
        timelines.append(existing_timeline)
        if existing_cache is not None:
            keep_alive.append(existing_cache)
    else:
        for i in range(workers):
            if i == 0 and existing_timeline is not None:
                timelines.append(existing_timeline)
                if existing_cache is not None:
                    keep_alive.append(existing_cache)
            else:
                tl, cache = _clone_timeline(
                    project_data, video_settings, audio_settings, profile.cache_bytes
                )
                timelines.append(tl)
                keep_alive.append(cache)
                keep_alive.append(tl)

    composite_threads = [
        threading.Thread(
            target=composite_worker,
            args=(i, timelines[i]),
            name=f"zenvi-export-composite-{i}",
            daemon=True,
        )
        for i in range(len(timelines))
    ]

    def encoder_loop() -> None:
        expected = start_frame
        reorder: dict[int, Any] = {}
        processed = 0
        last_progress_at = time.perf_counter()
        try:
            while expected <= end_frame:
                if cancel_event.is_set() or is_cancelled():
                    cancel_event.set()
                    return
                if error_box:
                    return
                if expected in reorder:
                    frame_obj = reorder.pop(expected)
                else:
                    try:
                        item = pending.get(timeout=0.5)
                    except queue.Empty:
                        # Exit if all compositors died and nothing is pending.
                        if not any(t.is_alive() for t in composite_threads) and pending.empty():
                            if expected <= end_frame and not error_box:
                                mark_error(RuntimeError("export pipeline stalled before completion"))
                            return
                        continue
                    if item.frame_number != expected:
                        reorder[item.frame_number] = item.frame_obj
                        continue
                    frame_obj = item.frame_obj
                writer.WriteFrame(frame_obj)
                processed += 1
                expected += 1
                now = time.perf_counter()
                if on_progress and (
                    processed == 1 or processed == total_frames or now - last_progress_at >= 0.5
                ):
                    elapsed = max(now - started_at, 0.001)
                    on_progress(expected - 1, processed / elapsed)
                    last_progress_at = now
        except Exception as exc:
            mark_error(exc)

    encode_thread = threading.Thread(
        target=encoder_loop, name="zenvi-export-encode", daemon=True
    )

    log.info(
        "Starting pipelined export: workers=%s pending=%s cache_mb=%.0f profile=%s",
        len(timelines),
        profile.max_pending_frames,
        profile.cache_bytes / (1024 * 1024),
        profile.name,
    )

    for thread in composite_threads:
        thread.start()
    encode_thread.start()

    # Watch for UI cancel
    while encode_thread.is_alive():
        if is_cancelled():
            cancel_event.set()
        encode_thread.join(timeout=0.25)

    for thread in composite_threads:
        thread.join(timeout=2.0)

    # Close cloned timelines (not the caller's existing_timeline)
    for i, tl in enumerate(timelines):
        if existing_timeline is not None and tl is existing_timeline:
            continue
        try:
            tl.Close()
        except Exception:
            log.debug("Failed closing cloned export timeline %s", i, exc_info=True)

    if error_box:
        raise error_box[0]
    if cancel_event.is_set() or is_cancelled():
        raise PipelineCancelled()

    elapsed = max(time.perf_counter() - started_at, 0.001)
    return {
        "elapsed_sec": elapsed,
        "frames": total_frames,
        "fps": total_frames / elapsed,
        "profile": profile.name,
        "workers": len(timelines),
    }
