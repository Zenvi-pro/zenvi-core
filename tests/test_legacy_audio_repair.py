"""Projects saved before the cover-art fix must be repaired on load.

apply_audio_only_clip_overrides only runs when a clip is *added*, so a project
whose music clip was placed by an older build still carries has_video=True and
still blacks out every lower layer.
"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from classes.clip_placement import (  # noqa: E402
    apply_audio_only_clip_overrides,
    repair_audio_only_project_data,
)


def _legacy_project():
    return {
        "files": [
            {"id": "F1", "path": "/m/theme.mp3", "media_type": "audio",
             "has_audio": True, "has_video": True},          # cover art
            {"id": "F2", "path": "/m/broll.mp4", "media_type": "video",
             "has_audio": True, "has_video": True},
        ],
        "clips": [
            {"id": "C1", "file_id": "F1", "layer": 5,
             "reader": {"id": "F1", "has_video": True}},
            {"id": "C2", "file_id": "F2", "layer": 1,
             "reader": {"id": "F2", "has_video": True}},
        ],
    }


def _repair(data):
    repair_audio_only_project_data(data, constant_interpolation=2, scale_none=3)
    return data


def _hidden(clip):
    points = clip.get("has_video", {}).get("Points", [])
    return bool(points) and points[0]["co"]["Y"] == 0.0


def test_a_legacy_music_clip_stops_painting_a_frame():
    data = _repair(_legacy_project())
    music = data["clips"][0]
    assert _hidden(music)
    assert music["scale"] == 3
    assert music["reader"]["has_video"] is False


def test_the_file_flag_is_corrected_too():
    data = _repair(_legacy_project())
    assert data["files"][0]["has_video"] is False


def test_the_video_clip_is_untouched():
    data = _repair(_legacy_project())
    video = data["clips"][1]
    assert "has_video" not in video or not _hidden(video)
    assert "scale" not in video
    assert video["reader"]["has_video"] is True


def test_a_clip_matched_only_by_reader_id_is_repaired():
    """Older writes did not always carry file_id."""
    data = _legacy_project()
    del data["clips"][0]["file_id"]
    assert _hidden(_repair(data)["clips"][0])


def test_a_project_with_no_audio_is_left_alone():
    data = {"files": [{"id": "F2", "path": "/m/b.mp4", "media_type": "video",
                       "has_audio": True, "has_video": True}],
            "clips": [{"id": "C2", "file_id": "F2", "reader": {"id": "F2"}}]}
    before = repr(data)
    assert repr(_repair(data)) == before


def test_repair_is_idempotent():
    once = _repair(_legacy_project())
    twice = _repair(_repair(_legacy_project()))
    assert once == twice


@pytest.mark.parametrize("data", [{}, {"files": []}, {"clips": []},
                                  {"files": [None, "junk"], "clips": [None]}])
def test_malformed_project_data_is_survivable(data):
    _repair(data)


def test_an_orphan_clip_is_skipped():
    data = _legacy_project()
    data["clips"][0]["file_id"] = "GONE"
    data["clips"][0]["reader"]["id"] = "GONE"
    assert not _hidden(_repair(data)["clips"][0])


def test_the_repair_matches_what_add_clip_writes():
    """One source of truth: repair must equal the add-time override."""
    data = _repair(_legacy_project())
    fresh = {"reader": {"id": "F1", "has_video": True}}
    apply_audio_only_clip_overrides(
        fresh, data["files"][0], constant_interpolation=2, scale_none=3,
    )
    assert data["clips"][0]["has_video"] == fresh["has_video"]
    assert data["clips"][0]["scale"] == fresh["scale"]
