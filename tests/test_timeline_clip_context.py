"""Unit tests for timeline clip context and effective metadata."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.ai_metadata_utils import (  # noqa: E402
    build_tags_preview,
    collect_scene_descriptions_for_baked_segment,
    get_effective_ai_metadata,
)
from classes.timeline_clip_context import (  # noqa: E402
    resolve_parent_file_id,
)


def _sample_ai():
    return {
        "analyzed": True,
        "tags": {
            "objects": ["cat", "dog", "car"],
            "scenes": ["beach", "city"],
            "activities": ["running"],
            "mood": [],
            "quality": {},
        },
        "scene_descriptions": [
            {"time": 5.0, "description": "A cat on the beach"},
            {"time": 50.0, "description": "A car in the city"},
        ],
        "twelvelabs": {
            "status": "ready",
            "index_id": "idx-1",
            "video_id": "vid-1",
        },
    }


def test_get_effective_metadata_preserves_twelvelabs():
    file_data = {"ai_metadata": _sample_ai(), "start": 0.0, "end": 120.0}
    eff = get_effective_ai_metadata(
        file_data,
        clip_data={"start": 0.0, "end": 10.0},
        rebased=True,
    )
    assert eff.get("twelvelabs", {}).get("video_id") == "vid-1"
    assert eff.get("analyzed") is True


def test_get_effective_metadata_filters_scenes():
    file_data = {"ai_metadata": _sample_ai(), "start": 0.0, "end": 120.0}
    eff = get_effective_ai_metadata(
        file_data,
        clip_data={"start": 0.0, "end": 10.0},
        rebased=True,
    )
    scenes = eff.get("scene_descriptions") or []
    assert len(scenes) == 1
    assert scenes[0]["description"] == "A cat on the beach"
    assert scenes[0]["time"] == 5.0
    assert scenes[0].get("source_time") == 5.0


def test_resolve_parent_file_subclip():
    subclip = {
        "zenvi_subclip": True,
        "parent_file_id": "root-file",
        "path": "/tmp/video.mp4",
    }
    assert resolve_parent_file_id(subclip, file_id="sub-1") == "root-file"


def test_build_tags_preview_windowed():
    file_data = {"ai_metadata": _sample_ai(), "start": 0.0, "end": 120.0}
    eff = get_effective_ai_metadata(
        file_data,
        clip_data={"start": 0.0, "end": 10.0},
        rebased=True,
    )
    preview = build_tags_preview(eff)
    assert "cat" in preview.lower() or "beach" in preview.lower()
    assert "car" not in preview.lower()


def test_collect_baked_segment_offsets():
    ai = _sample_ai()
    baked = collect_scene_descriptions_for_baked_segment(ai, 0.0, 10.0, baked_offset=30.0)
    assert len(baked) == 1
    assert baked[0]["time"] == 35.0


if __name__ == "__main__":
    test_get_effective_metadata_preserves_twelvelabs()
    test_get_effective_metadata_filters_scenes()
    test_resolve_parent_file_subclip()
    test_build_tags_preview_windowed()
    test_collect_baked_segment_offsets()
    print("test_timeline_clip_context: ok")
