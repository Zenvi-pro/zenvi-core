"""When a saved analysis may stand in for a new import of the same video."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index.reuse import durations_match, restored_metadata, reusable_analysis  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

FP = "c" * 64
ANALYSIS = {
    "analyzed": True, "provider": "gemini-flash-lite", "short_summary": "a street at night",
    "chapters": [{"start": 0, "end": 5, "title": "Street"}], "transcript_cues": [], "moments": [],
    "index": {"status": "ready", "index_id": "zenvi-OLD", "video_id": "OLDFILE", "index_name": "zenvi-OLD"},
    "twelvelabs": {"status": "ready", "index_id": "zenvi-OLD", "video_id": "OLDFILE"},
}


@pytest.fixture
def shelf(tmp_path):
    s = Shelf(str(tmp_path / "idx"))
    s.save_v1_index(FP, ANALYSIS, duration=30.0, media_type="video", project_id="OLD", file_id="OLDFILE")
    return s


# -- when it is reusable ---------------------------------------------------------------
def test_the_same_content_is_reused(shelf):
    got = reusable_analysis(shelf, FP, media_type="video", duration=30.0)
    assert got and got["short_summary"] == "a street at night"


def test_content_never_indexed_is_not_reusable(shelf):
    assert reusable_analysis(shelf, "d" * 64, media_type="video", duration=30.0) is None
    assert reusable_analysis(shelf, "not-a-hash", media_type="video", duration=30.0) is None


@pytest.mark.parametrize("duration", [0.0, None, 12.0, 90.0, 31.0])
def test_a_file_with_another_duration_is_a_different_file(shelf, duration):
    assert reusable_analysis(shelf, FP, media_type="video", duration=duration) is None


@pytest.mark.parametrize("duration", [29.6, 30.0, 30.4])
def test_a_probe_that_differs_by_a_hair_still_matches(shelf, duration):
    assert reusable_analysis(shelf, FP, media_type="video", duration=duration)


def test_another_kind_of_media_is_never_reused(shelf):
    assert reusable_analysis(shelf, FP, media_type="audio", duration=30.0) is None


def test_stills_ignore_duration(tmp_path):
    s = Shelf(str(tmp_path / "idx"))
    s.save_v1_index(FP, dict(ANALYSIS, media_type="image"), duration=0.0, media_type="image")
    assert reusable_analysis(s, FP, media_type="image", duration=0.0)
    assert reusable_analysis(s, FP, media_type="image", duration=60.0)


def test_an_analysis_with_no_usable_content_is_not_reused(tmp_path):
    s = Shelf(str(tmp_path / "idx"))
    s.save_v1_index(FP, {"analyzed": True}, duration=30.0)  # analyzed flag only, nothing in it
    assert reusable_analysis(s, FP, media_type="video", duration=30.0) is None


def test_a_layer_that_is_not_ready_is_not_reused(shelf):
    shelf.set_layer(FP, "v1_index", version=1, status="failed")
    assert reusable_analysis(shelf, FP, media_type="video", duration=30.0) is None


def test_a_layer_from_a_different_version_is_not_reused(shelf):
    shelf.set_layer(FP, "v1_index", version=2, status="ready")
    assert reusable_analysis(shelf, FP, media_type="video", duration=30.0) is None


@pytest.mark.parametrize("saved,current,kind,expected", [
    (30, 30, "video", True), (30, 30.4, "video", True), (3600, 3630, "video", True),
    (3600, 3700, "video", False), (30, 0, "video", False), (0, 30, "video", False),
    ("x", 30, "video", False), (30, None, "audio", False), (0, 0, "image", True),
])
def test_duration_rule(saved, current, kind, expected):
    assert durations_match(saved, current, kind) is expected


# -- labelling it for the new file -------------------------------------------------------
def test_a_reused_analysis_is_labelled_for_the_new_file_and_project():
    got = restored_metadata(ANALYSIS, index_name="zenvi-NEW", file_id="NEWFILE", media_type="video")
    for block in (got["index"], got["twelvelabs"]):
        assert block["status"] == "ready"
        assert block["video_id"] == "NEWFILE"
        assert block["index_id"] == "zenvi-NEW" and block["index_name"] == "zenvi-NEW"
        assert block["restored"] is True
    assert got["analyzed"] is True and got["media_type"] == "video"
    assert got["short_summary"] == "a street at night"


def test_labelling_never_modifies_the_saved_copy():
    before = {"index": dict(ANALYSIS["index"]), "chapters": list(ANALYSIS["chapters"])}
    got = restored_metadata(ANALYSIS, index_name="zenvi-NEW", file_id="NEWFILE", media_type="video")
    got["chapters"].append({"start": 9, "end": 10})
    got["index"]["video_id"] = "mutated"
    assert ANALYSIS["index"] == before["index"] and ANALYSIS["chapters"] == before["chapters"]


def test_old_errors_and_skips_do_not_leak_into_a_restored_analysis():
    stale = dict(ANALYSIS, error="old failure", skip_reason="too long", skip_code="signin")
    got = restored_metadata(stale, index_name="zenvi-NEW", file_id="F", media_type="video")
    assert not {"error", "skip_reason", "skip_code"} & set(got)


def test_a_restored_analysis_reads_as_indexed_in_the_badge():
    from classes.indexing_status import SUCCESS, derive_indexing_status

    got = restored_metadata(ANALYSIS, index_name="zenvi-NEW", file_id="NEWFILE", media_type="video")
    assert derive_indexing_status(got).state == SUCCESS
    from classes.twelvelabs_match import twelvelabs_is_indexed

    assert twelvelabs_is_indexed(got["index"])
