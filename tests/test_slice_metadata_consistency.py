"""Tests for slice metadata consistency — source times must not leak across segments."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.ai_metadata_utils import (  # noqa: E402
    apply_metadata_to_clip_data,
    get_effective_ai_metadata,
    materialize_clip_ai_metadata,
    merge_basic_clip_props,
    scene_source_time,
)


def _root_ai():
    return {
        "analyzed": True,
        "tags": {"objects": ["ball"], "scenes": ["court"], "activities": []},
        "scene_descriptions": [
            {"time": 5.0, "description": "warm up"},
            {"time": 11.0, "description": "serve"},
            {"time": 20.0, "description": "rally at net"},
            {"time": 25.0, "description": "winner"},
            {"time": 40.0, "description": "celebration"},
        ],
        "twelvelabs": {"status": "ready", "index_id": "i", "video_id": "v"},
    }


def test_slice_keep_both_no_source_leak():
    """Right segment must not include scenes before the cut point."""
    root = _root_ai()
    cut = 20.0

    left = materialize_clip_ai_metadata(root, 0.0, cut, rebased=True)
    right = materialize_clip_ai_metadata(root, cut, 60.0, rebased=True, exclusive_start=True)

    left_sources = [scene_source_time(s, left) for s in left["scene_descriptions"]]
    right_sources = [scene_source_time(s, right) for s in right["scene_descriptions"]]

    assert left_sources == [5.0, 11.0, 20.0]
    assert right_sources == [25.0, 40.0]
    assert max(left_sources) <= cut + 1e-3
    assert min(right_sources) >= cut - 1e-3
    assert 11.0 not in right_sources


def test_slice_rebased_times_start_at_zero_for_right():
    root = _root_ai()
    right = materialize_clip_ai_metadata(root, 20.0, 60.0, rebased=True, exclusive_start=True)
    times = [float(s["time"]) for s in right["scene_descriptions"]]
    assert times[0] == 5.0  # source 25 -> local 5
    assert all(t >= 0.0 for t in times)


def test_stale_rebased_clip_metadata_is_recomputed():
    """Persisted rebased scenes must not be trusted when source_window mismatches."""
    root = _root_ai()
    stale = materialize_clip_ai_metadata(root, 0.0, 60.0, rebased=True)
    # Simulate buggy right clip that inherited left-segment rebased scenes.
    clip_data = {"start": 20.0, "end": 60.0, "file_id": "f1"}
    fixed = get_effective_ai_metadata(
        {"ai_metadata": root},
        clip_data=clip_data,
        clip_ai_metadata=stale,
        root_ai_metadata=root,
        rebased=True,
    )
    sources = [scene_source_time(s, fixed) for s in fixed["scene_descriptions"]]
    assert 11.0 not in sources
    assert min(sources) >= 20.0


def test_apply_metadata_to_clip_data_clears_inherited_stale():
    root = _root_ai()
    clip_data = {
        "start": 20.0,
        "end": 60.0,
        "ai_metadata": materialize_clip_ai_metadata(root, 0.0, 20.0, rebased=True),
    }
    apply_metadata_to_clip_data(clip_data, root, rebased=True)
    sources = [scene_source_time(s, clip_data["ai_metadata"]) for s in clip_data["ai_metadata"]["scene_descriptions"]]
    assert 11.0 not in sources
    assert min(sources) >= 20.0


def test_second_slice_on_left_segment():
    root = _root_ai()
    first = materialize_clip_ai_metadata(root, 0.0, 20.0, rebased=True)
    clip_data = {"start": 0.0, "end": 15.0, "ai_metadata": first}
    second = get_effective_ai_metadata(
        {"ai_metadata": root},
        clip_data=clip_data,
        clip_ai_metadata=first,
        root_ai_metadata=root,
        rebased=True,
    )
    sources = [scene_source_time(s, second) for s in second["scene_descriptions"]]
    assert sources == [5.0, 11.0]
    assert 20.0 not in sources


def test_adjacent_segments_source_monotonic():
    root = _root_ai()
    cut = 20.0
    left = materialize_clip_ai_metadata(root, 0.0, cut, rebased=True)
    right = materialize_clip_ai_metadata(root, cut, 60.0, rebased=True, exclusive_start=True)
    left_max_src = max(scene_source_time(s, left) for s in left["scene_descriptions"])
    right_min_src = min(scene_source_time(s, right) for s in right["scene_descriptions"])
    assert left_max_src <= cut + 1e-3
    assert right_min_src >= cut - 1e-3
    assert right_min_src >= left_max_src - 1e-3


def test_merge_basic_clip_props_preserves_ai_metadata():
    from classes.ai_metadata_utils import merge_basic_clip_props

    root = _root_ai()
    meta = materialize_clip_ai_metadata(root, 0.0, 20.0, rebased=True)
    old = {
        "id": "c1",
        "layer": 1000000,
        "position": 5.0,
        "start": 0.0,
        "end": 20.0,
        "duration": 20.0,
        "title": "Left segment",
        "ai_metadata": meta,
        "effects": [{"id": "fx1"}],
    }
    moved = merge_basic_clip_props(old, {
        "id": "c1",
        "layer": 2000000,
        "position": 12.0,
        "start": 0.0,
        "end": 20.0,
        "duration": 20.0,
    })
    assert moved["layer"] == 2000000
    assert moved["position"] == 12.0
    assert moved["ai_metadata"] is meta
    assert moved["effects"] == [{"id": "fx1"}]
    assert moved["title"] == "Left segment"
    scenes = moved["ai_metadata"]["scene_descriptions"]
    assert scenes[0]["time"] == 5.0
    assert scenes[0]["source_time"] == 5.0


def test_override_merge_updates_layer_for_slice():
    """Slice must use the visually moved track, not stale project layer."""
    stored = {"id": "c1", "layer": 1, "position": 0.0, "start": 0.0, "end": 60.0, "duration": 60.0}
    override = {"layer": 3, "position": 10.0}
    merged = merge_basic_clip_props(stored, {**stored, **override})
    assert merged["layer"] == 3
    assert merged["position"] == 10.0
    assert merged["start"] == 0.0


if __name__ == "__main__":
    test_slice_keep_both_no_source_leak()
    test_slice_rebased_times_start_at_zero_for_right()
    test_stale_rebased_clip_metadata_is_recomputed()
    test_apply_metadata_to_clip_data_clears_inherited_stale()
    test_second_slice_on_left_segment()
    test_adjacent_segments_source_monotonic()
    test_merge_basic_clip_props_preserves_ai_metadata()
    test_override_merge_updates_layer_for_slice()
    print("test_slice_metadata_consistency: ok")
