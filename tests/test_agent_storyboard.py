"""Storyboard dHash + coverage floor (no openshot)."""

from __future__ import annotations

from classes.agent_tools.storyboard import (
    dhash64,
    hamming64,
    select_storyboard_indexes,
    should_keep,
)


def _solid(val: int) -> bytes:
    return bytes([val] * 72)


def _checker() -> bytes:
    out = bytearray(72)
    for row in range(8):
        for col in range(9):
            out[row * 9 + col] = 255 if (row + col) % 2 else 0
    return bytes(out)


def test_identical_pair_collapses():
    a = dhash64(_solid(128))
    assert not should_keep(a, a, threshold=10)


def test_checkerboard_differs_from_solid():
    a = dhash64(_solid(128))
    b = dhash64(_checker())
    assert hamming64(a, b) > 10
    assert should_keep(b, a, threshold=10)


def test_coverage_floor_inserts_middle_on_static():
    # Long static sequence: fingerprints identical → only first/last would stay
    # without coverage floor; floor should insert midpoints.
    fp = dhash64(_solid(100))
    n = 20
    fingerprints = [fp] * n
    timestamps = [i * 1.0 for i in range(n)]  # 19s span
    idxs = select_storyboard_indexes(
        fingerprints, timestamps, coverage_floor_sec=2.0, max_tiles=36,
    )
    assert idxs[0] == 0
    assert idxs[-1] == n - 1
    assert len(idxs) > 2
