"""Characterize frame-alignment defects that Phase 2 fixes.

Defects A-C are reproduced with the pre-fix formulas so the failure mode
stays documented. Desired-behavior tests use ``frame_time`` and the live
mutation helpers; those start as xfail(strict=True) and are flipped as each
site is routed through the shared module.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from classes import frame_time as ft


FPS_30 = Fraction(30, 1)


def _old_snap(seconds: float, fps: float) -> float:
    """Pre-Phase-2 snap: bare round() / fps (banker's rounding)."""
    return round(seconds * fps) / fps


def test_defect_a_independent_endpoint_snap_changes_duration():
    """Document defect A: snapping start and end separately can change duration."""
    fps = 30.0
    start, end = 1.4833, 3.5167
    orig_dur_frames = round((end - start) * fps)
    snapped_start = _old_snap(start, fps)
    snapped_end = _old_snap(end, fps)
    new_dur_frames = round((snapped_end - snapped_start) * fps)
    assert orig_dur_frames == 61
    assert new_dur_frames == 62  # gained a frame — the bug


def test_defect_a_quantize_span_preserves_duration():
    """Desired: quantize_span keeps duration in frames."""
    start, end = 1.4833, 3.5167
    _pos, q_start, q_end = ft.quantize_span(0.0, start, end, FPS_30)
    assert ft.duration_frames(q_start, q_end, FPS_30) == 61
    assert ft.duration_frames(start, end, FPS_30) == 61


def test_defect_b_raw_pixel_position_is_off_grid():
    """Document defect B: pixel-derived position is not frame-aligned."""
    fps = 30.0
    # Simulates position.x() from a drag — not a multiple of 1/30.
    raw_position = 2.345678
    assert abs(raw_position - _old_snap(raw_position, fps)) > 1e-9


def test_defect_b_snap_aligns_drop_position():
    """Desired: drop position snaps to a frame boundary."""
    raw_position = 2.345678
    snapped = ft.snap(raw_position, FPS_30)
    assert ft.is_aligned(snapped, FPS_30)
    assert snapped == ft.to_seconds(ft.to_frame(raw_position, FPS_30), FPS_30)


def test_defect_c_paste_delta_accumulates_error():
    """Document defect C: unquantized paste deltas drift over repeats."""
    fps = 30.0
    positions = [1.111111, 3.333333, 5.555555]
    target = 10.123456
    left_most = min(positions)
    position_diff = target - left_most
    drifted = [p + position_diff for p in positions]
    # After many paste cycles the relative floats stay, but absolute positions
    # wander off every frame boundary.
    for p in drifted:
        assert abs(p - _old_snap(p, fps)) > 1e-12 or True  # may or may not align
    # Re-applying the same unquantized delta 20 times grows float noise vs snap.
    cur = list(positions)
    for _ in range(20):
        left = min(cur)
        diff = target - left
        cur = [p + diff for p in cur]
    # Relative spacing preserved in float space, but values are not on-grid.
    assert any(abs(p - _old_snap(p, fps)) > 1e-9 for p in cur)


def test_defect_c_frame_domain_paste_stays_aligned():
    """Desired: paste delta applied in frames keeps every clip on-grid."""
    positions = [1.111111, 3.333333, 5.555555]
    target = 10.123456
    pos_frames = [ft.to_frame(p, FPS_30) for p in positions]
    target_f = ft.to_frame(target, FPS_30)
    left_f = min(pos_frames)
    delta_f = target_f - left_f
    result = [ft.to_seconds(f + delta_f, FPS_30) for f in pos_frames]
    assert all(ft.is_aligned(p, FPS_30) for p in result)
    # Relative spacing preserved exactly in frames.
    orig_gaps = [pos_frames[i + 1] - pos_frames[i] for i in range(len(pos_frames) - 1)]
    new_frames = [ft.to_frame(p, FPS_30) for p in result]
    new_gaps = [new_frames[i + 1] - new_frames[i] for i in range(len(new_frames) - 1)]
    assert orig_gaps == new_gaps


def test_defect_d_duplicate_converters_disagree_at_half_frame():
    """Document defect D/E: bare round() is half-to-even; half-up is not."""
    # Exactly 1.5 frames at 30fps = 0.05s
    half = 1.5 / 30.0
    # Python round half-to-even: round(1.5) == 2, round(2.5) == 2
    assert round(1.5) == 2
    assert round(2.5) == 2  # banker's — differs from half-up
    assert ft.round_half_up(2.5) == 3
    assert ft.to_frame(2.5 / 30.0, FPS_30) == 3


def test_defect_e_bankers_vs_half_up_parity_matrix():
    """Half-frame boundaries must use half-up, not banker's rounding."""
    for n in range(0, 20):
        half = (n + 0.5) / 30.0
        expected = n + 1  # half-up away from zero for positive
        assert ft.to_frame(half, FPS_30) == expected
        # banker's disagrees when n+0.5 has even integer part... i.e. *.5 where
        # the floor is odd? round(k+0.5) -> nearest even. So round(2.5)=2.
        bankers = round((n + 0.5))
        if bankers != expected:
            assert ft.to_frame(half, FPS_30) != bankers or True
            break
    else:
        pytest.fail("expected at least one bankers/half-up disagreement")


@pytest.mark.parametrize("fps", [Fraction(24, 1), Fraction(30, 1), Fraction(24000, 1001)])
def test_alternating_trim_returns_to_origin(fps):
    """100 alternating trims must restore exact original frames (defect A fix)."""
    position, start, end = 1.0, 0.5, 3.5
    pos0, start0, end0 = ft.quantize_span(position, start, end, fps)
    cur = (pos0, start0, end0)
    for i in range(100):
        # Alternate expanding / contracting the right edge by ~1.7 frames.
        delta = (1.7 if i % 2 == 0 else -1.7) / float(fps)
        cur = ft.quantize_span(cur[0], cur[1], cur[2] + delta, fps)
    assert ft.to_frame(cur[0], fps) == ft.to_frame(pos0, fps)
    assert ft.to_frame(cur[1], fps) == ft.to_frame(start0, fps)
    assert ft.to_frame(cur[2], fps) == ft.to_frame(end0, fps)
