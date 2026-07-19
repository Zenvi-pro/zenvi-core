"""Tests for project TwelveLabs index helper (no Qt app required)."""

from __future__ import annotations

from classes.project_tl_index import (
    build_project_index_name,
    map_search_hit_to_file,
)


def test_build_project_index_name():
    assert build_project_index_name("proj-uuid-1") == "zenvi-proj-uuid-1"
    assert build_project_index_name("") == "zenvi-videos"


def test_map_search_hit_to_file():
    video_map = {
        "vid-9": {"file_id": "file-9", "name": "talk.mp4"},
    }
    fid, name = map_search_hit_to_file(
        {"video_id": "vid-9", "filename": "x.mp4"},
        video_map,
    )
    assert fid == "file-9"
    assert name == "talk.mp4"

    fid2, name2 = map_search_hit_to_file({"video_id": "missing", "filename": "x.mp4"}, video_map)
    assert fid2 == ""
    assert name2 == "x.mp4"
