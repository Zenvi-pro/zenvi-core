"""Unit tests for clip placement trim + underlay defaults."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.clip_placement import (
    DEFAULT_WATCH_QUERY,
    compute_clip_trim_bounds,
    default_underlay_layer_number,
    file_looks_like_image,
    placement_watch_query,
    should_watch_placement,
    source_window_for_file,
)


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


def test_file_window_prefers_start_end_over_parent_duration():
    start, end = source_window_for_file({"start": 12.0, "end": 18.0, "duration": 30.0})
    assert abs(start - 12.0) < 1e-9
    assert abs(end - 18.0) < 1e-9


def test_trim_subclip_does_not_exceed_file_end():
    src_start, src_end = source_window_for_file({"start": 12.0, "end": 18.0, "duration": 30.0})
    start, end = compute_clip_trim_bounds(
        src_end - src_start, trim_start=0.0, trim_dur=10.0, file_start=src_start,
    )
    assert start >= 12.0 - 1e-9
    assert end <= 18.0 + 1e-9


def test_watch_query_prefers_prompt_then_summary():
    data = {
        "name": "clip.mp4",
        "ai_metadata": {"prompt": "a cat waving", "short_summary": "pet clip"},
    }
    assert placement_watch_query(data) == "a cat waving"
    assert placement_watch_query(data, query="handshake") == "handshake"


def test_watch_query_uses_layout_region_before_filename():
    data = {"path": "/tmp/out.webm", "ai_metadata": {"transparent": True}}
    assert placement_watch_query(data, extra="lower_third") == "lower third"


def test_watch_query_falls_back_to_default():
    assert placement_watch_query({}) == DEFAULT_WATCH_QUERY


def test_file_looks_like_image_by_ext_and_media_type():
    assert file_looks_like_image({"path": "/x.png"})
    assert file_looks_like_image({"media_type": "image", "path": "/x"})
    assert not file_looks_like_image({"path": "/x.mp4"})


def test_should_watch_short_unindexed_video():
    assert should_watch_placement(window_sec=5.0) is True
    assert should_watch_placement(window_sec=5.0, is_audio=True) is False
    assert should_watch_placement(window_sec=5.0, is_image=True) is False
    assert should_watch_placement(window_sec=5.0, skip_explicit_times=True) is False


def test_should_watch_skips_already_watched_subclip_unless_query():
    assert should_watch_placement(
        window_sec=6.0, is_already_watched_subclip=True, explicit_query=False,
    ) is False
    assert should_watch_placement(
        window_sec=6.0, is_already_watched_subclip=True, explicit_query=True,
    ) is True


def test_should_watch_skips_long_untrimmed_file():
    assert should_watch_placement(window_sec=120.0) is False
    assert should_watch_placement(window_sec=6.0) is True
