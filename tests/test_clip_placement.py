"""Unit tests for clip placement trim + underlay defaults."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.clip_placement import compute_clip_trim_bounds, default_underlay_layer_number


def test_trim_duration_three_seconds():
    start, end = compute_clip_trim_bounds(30.0, trim_start=0.0, trim_dur=3.0)
    assert abs((end - start) - 3.0) < 1e-9


def test_trim_clamped_to_source():
    start, end = compute_clip_trim_bounds(2.0, trim_start=0.0, trim_dur=5.0)
    assert abs((end - start) - 2.0) < 1e-9


def test_trim_with_start_offset():
    start, end = compute_clip_trim_bounds(10.0, trim_start=4.0, trim_dur=3.0)
    assert abs(start - 4.0) < 1e-9
    assert abs(end - 7.0) < 1e-9


def test_empty_video_track_defaults_to_lowest_layer():
    layers = [
        {"number": 5000000, "label": "1"},
        {"number": 1000000, "label": "5"},
        {"number": 3000000, "label": "mid"},
    ]
    assert default_underlay_layer_number(layers) == 1000000
