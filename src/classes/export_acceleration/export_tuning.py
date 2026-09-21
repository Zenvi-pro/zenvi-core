"""Queue and cache sizing for pipelined export (Recordly-inspired)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ExportPipelineProfile:
    name: str
    composite_workers: int
    max_pending_frames: int
    cache_bytes: int


# Pipelined FFmpegWriter.WriteFrame is only validated for MP4-family muxers.
# Other containers (MOV, MKV, AVI, …) stay on the legacy serial path.
_PIPELINE_SAFE_FORMATS = frozenset({"mp4", "m4v"})
_MP4_FASTSTART_FORMATS = frozenset({"mp4", "m4v"})


def _normalize_vformat(vformat: Optional[str]) -> str:
    return (vformat or "").strip().lower().lstrip(".")


def is_pipelined_export_safe(vformat: Optional[str]) -> bool:
    """Return True when overlapping encode is safe for this container."""
    return _normalize_vformat(vformat) in _PIPELINE_SAFE_FORMATS


def uses_mp4_faststart_preset(vformat: Optional[str]) -> bool:
    """mp4_faststart is an MP4 muxing preset; applying it to MOV/etc can crash natively."""
    return _normalize_vformat(vformat) in _MP4_FASTSTART_FORMATS


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def export_cache_bytes(width: int, height: int, *, frame_budget: int = 24) -> int:
    """Size the export CacheMemory from resolution.

    A 4K RGBA frame is ~33 MB; the historical 250 MB cap held only ~7 frames.
    """
    w = max(1, int(width))
    h = max(1, int(height))
    # libopenshot Frame is typically 4 bytes/pixel for the QImage path.
    bytes_per_frame = w * h * 4
    target = bytes_per_frame * max(8, int(frame_budget))
    # Floor 256 MB, soft-cap 2 GB so we do not OOM low-RAM machines.
    return _clamp(target, 256 * 1024 * 1024, 2 * 1024 * 1024 * 1024)


def get_export_pipeline_profile(
    width: int,
    height: int,
    frame_rate: float,
    *,
    hardware_concurrency: Optional[int] = None,
    enable_parallel_composite: bool = True,
) -> ExportPipelineProfile:
    """Choose composite-worker count and pending-frame depth."""
    import os

    cores = hardware_concurrency
    if cores is None:
        cores = os.cpu_count() or 4
    cores = max(1, int(cores))

    pixel_rate = max(1, int(width)) * max(1, int(height)) * max(1.0, float(frame_rate))
    baseline = 1280 * 720 * 30
    relative = pixel_rate / baseline

    if not enable_parallel_composite or cores <= 2:
        workers = 1
        name = "serial-overlap"
    elif relative >= 3 or cores <= 4:
        workers = 2
        name = "parallel-conservative"
    elif cores >= 8 and relative < 1.5:
        workers = min(4, max(2, cores // 3))
        name = "parallel-balanced-plus"
    else:
        workers = min(3, max(2, cores // 4))
        name = "parallel-balanced"

    # Pending frames ≈ ~1.5–2.5 seconds of video, clamped.
    fps = max(1.0, float(frame_rate))
    pending = _clamp(int(round(fps * 2.0)), 24, 120)
    if workers > 1:
        pending = _clamp(pending + workers * 8, 32, 160)

    return ExportPipelineProfile(
        name=name,
        composite_workers=workers,
        max_pending_frames=pending,
        cache_bytes=export_cache_bytes(width, height, frame_budget=max(16, pending // 2)),
    )
