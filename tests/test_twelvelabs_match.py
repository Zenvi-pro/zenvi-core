"""Unit tests for TwelveLabs hit overlap and occurrence selection."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.twelvelabs_match import (  # noqa: E402
    clip_overlap,
    select_twelvelabs_match,
)


def test_overlap_filters_outside_clip_window():
    _, _, ratio = clip_overlap(95.0, 105.0, 10.0, 40.0)
    assert ratio == 0.0


def test_overlap_inside_clip_window():
    start, end, ratio = clip_overlap(15.0, 25.0, 10.0, 40.0)
    assert ratio > 0.0
    assert start == 15.0
    assert end == 25.0


def test_occurrence_selects_nth_hit():
    hits = [
        {"start": 12.0, "end": 18.0, "rank": 1, "score": 0.9},
        {"start": 22.0, "end": 28.0, "rank": 2, "score": 0.8},
    ]
    first = select_twelvelabs_match(
        hits, clip_start=10.0, clip_end=40.0, occurrence=1,
    )
    second = select_twelvelabs_match(
        hits, clip_start=10.0, clip_end=40.0, occurrence=2,
    )
    assert first is not None and second is not None
    assert first["start"] == 12.0
    assert second["start"] == 22.0


def test_hit_outside_window_rejected():
    hits = [{"start": 100.0, "end": 110.0, "rank": 1}]
    chosen = select_twelvelabs_match(
        hits, clip_start=10.0, clip_end=40.0, occurrence=1,
    )
    assert chosen is None


if __name__ == "__main__":
    test_overlap_filters_outside_clip_window()
    test_overlap_inside_clip_window()
    test_occurrence_selects_nth_hit()
    test_hit_outside_window_rejected()
    print("test_twelvelabs_match: ok")
