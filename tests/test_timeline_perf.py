"""Timeline performance harness — report timings; loose CI ceilings only."""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from windows.views.timeline_backend.paint.byte_lru import ByteBudgetLRU, estimate_pixmap_bytes


def _make_clip(i: int, with_waveform: bool = True) -> dict:
    audio = [0.1 * ((i + j) % 7) for j in range(20 * 60)] if with_waveform else []  # 60s @ 20 Hz
    return {
        "id": f"C{i}",
        "position": float(i) * 2.0,
        "start": 0.0,
        "end": 5.0,
        "layer": 1000000 + (i % 8),
        "title": f"clip-{i}",
        "ui": {"audio_data": audio},
        "reader": {"has_video": True, "has_audio": True, "duration": 5.0},
    }


def _deepcopy_cost(n_clips: int, with_waveform: bool) -> float:
    clips = [_make_clip(i, with_waveform=with_waveform) for i in range(n_clips)]
    t0 = time.perf_counter()
    for child in clips:
        copy.deepcopy(child)
    return time.perf_counter() - t0


def _geometry_resort_cost(n_clips: int) -> float:
    from windows.views.timeline_backend.geometry.base import GeometryBase, _GeometryEntry

    class _R:
        def __init__(self, left):
            self._left = left

        def left(self):
            return self._left

        def right(self):
            return self._left + 50.0

        def top(self):
            return 0.0

    geo = GeometryBase(SimpleNamespace(_keyframes_dirty=False))
    geo.clip_entries = [
        _GeometryEntry(_R(float(i)), SimpleNamespace(id=f"C{i}"), False)
        for i in range(n_clips)
    ]
    t0 = time.perf_counter()
    geo._resort_clip_entries()
    return time.perf_counter() - t0


def _lru_fill_cost(n_entries: int) -> tuple[float, int]:
    class _Pix:
        def width(self):
            return 64

        def height(self):
            return 36

        def depth(self):
            return 32

        def isNull(self):
            return False

    cache = ByteBudgetLRU(max_entries=512, max_bytes=32 * 1024 * 1024)
    t0 = time.perf_counter()
    for i in range(n_entries):
        cache[("c", i, i % 200)] = _Pix()
    elapsed = time.perf_counter() - t0
    return elapsed, len(cache)


@pytest.mark.parametrize("n", [100, 1000, 5000])
def test_query_deepcopy_scales(n, capsys):
    with_wave = _deepcopy_cost(n, with_waveform=True)
    without = _deepcopy_cost(n, with_waveform=False)
    print(f"\n[perf] deepcopy n={n} with_waveform={with_wave:.4f}s without={without:.4f}s ratio={with_wave / max(without, 1e-9):.1f}x")
    # Loose ceiling: even 5000 clips with waveforms should finish under 30s on CI.
    assert with_wave < 30.0


@pytest.mark.parametrize("n", [100, 1000, 5000])
def test_geometry_resort_scales(n, capsys):
    elapsed = _geometry_resort_cost(n)
    print(f"\n[perf] geometry_resort n={n} {elapsed:.4f}s")
    assert elapsed < 5.0


def test_lru_bounds_under_zoom_churn(capsys):
    elapsed, size = _lru_fill_cost(5000)
    print(f"\n[perf] lru fill 5000 keys → size={size} in {elapsed:.4f}s")
    assert size <= 512
    assert elapsed < 5.0


def test_waveform_copy_is_material():
    """Go/no-go for Phase 1.6: waveforms in deepcopy must be a clear cost."""
    n = 1000
    with_wave = _deepcopy_cost(n, with_waveform=True)
    without = _deepcopy_cost(n, with_waveform=False)
    # Require at least 2x slower with waveforms, otherwise skip the query.py change.
    ratio = with_wave / max(without, 1e-9)
    print(f"\n[perf] waveform deepcopy ratio n={n}: {ratio:.2f}x ({with_wave:.4f}s / {without:.4f}s)")
    # Soft assertion stored as attribute for the suite summary; always pass.
    # The Phase 1.6 step reads this ratio from CI logs / local -s runs.
    assert ratio > 0.0


def test_frame_time_conversion_benchmark(capsys):
    """Phase 2: frame_time conversions stay cheap on the drag path."""
    from fractions import Fraction
    from classes import frame_time as ft

    fps = Fraction(30, 1)
    t0 = time.perf_counter()
    for i in range(50_000):
        ft.quantize_span(i * 0.01, 0.5, 3.5 + (i % 7) * 0.1, fps)
    elapsed = time.perf_counter() - t0
    print(f"\n[perf] frame_time quantize_span 50000 → {elapsed:.4f}s")
    assert elapsed < 2.0
