"""Sliced timeline clips must inherit speech windows without re-indexing."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.ai_metadata_utils import (  # noqa: E402
    clip_metadata_is_valid,
    get_effective_ai_metadata,
    materialize_clip_ai_metadata,
)

ROOT_AI = {
    "analyzed": True,
    "short_summary": "A person talking over music.",
    "sounds": "soft piano bed",
    "transcript": "one two three",
    "chapters": [{"start": 0.0, "end": 30.0, "title": "talk", "summary": "talking"}],
    "transcript_cues": [
        {"start": 1.0, "end": 2.0, "text": "one"},
        {"start": 10.0, "end": 12.0, "text": "two"},
        {"start": 25.0, "end": 26.0, "text": "three"},
    ],
    "moments": [{"start": 11.0, "end": 11.5, "label": "laugh", "detail": "laughs"}],
    "scene_descriptions": [{"description": "a face", "source_time": 11.0, "time": 11.0}],
}


def test_cues_inside_the_trim_window_survive_and_rebase():
    meta = materialize_clip_ai_metadata(ROOT_AI, 8.0, 15.0)
    cues = meta["transcript_cues"]
    assert len(cues) == 1
    assert cues[0]["text"] == "two"
    # Clip-local seconds for display...
    assert cues[0]["start"] == 2.0
    assert cues[0]["end"] == 4.0
    # ...and absolute root-source seconds for the mixer.
    assert cues[0]["source_start"] == 10.0
    assert cues[0]["source_end"] == 12.0


def test_cues_outside_the_trim_window_are_dropped():
    meta = materialize_clip_ai_metadata(ROOT_AI, 8.0, 15.0)
    texts = [c["text"] for c in meta["transcript_cues"]]
    assert "one" not in texts and "three" not in texts


def test_cue_straddling_the_boundary_is_clipped_not_dropped():
    meta = materialize_clip_ai_metadata(ROOT_AI, 11.0, 20.0)
    cue = meta["transcript_cues"][0]
    assert cue["source_start"] == 11.0   # clipped from 10.0
    assert cue["source_end"] == 12.0
    assert cue["start"] == 0.0


def test_moments_are_carried_with_the_same_window_rule():
    meta = materialize_clip_ai_metadata(ROOT_AI, 8.0, 15.0)
    assert [m["label"] for m in meta["moments"]] == ["laugh"]
    assert materialize_clip_ai_metadata(ROOT_AI, 0.0, 5.0)["moments"] == []


def test_has_speech_reflects_the_window_not_the_whole_file():
    assert materialize_clip_ai_metadata(ROOT_AI, 8.0, 15.0)["has_speech"] is True
    # 15-24s of the same file is music only.
    quiet = materialize_clip_ai_metadata(ROOT_AI, 15.0, 24.0)
    assert quiet["transcript_cues"] == []
    assert quiet["has_speech"] is False


def test_unrebased_metadata_keeps_absolute_start_and_end():
    meta = materialize_clip_ai_metadata(ROOT_AI, 8.0, 15.0, rebased=False)
    cue = meta["transcript_cues"][0]
    assert cue["start"] == 10.0 and cue["end"] == 12.0


def test_malformed_cues_do_not_break_materialization():
    root = dict(ROOT_AI, transcript_cues=[None, "junk", {"start": "x"}, {"start": 10.0, "end": 12.0}])
    meta = materialize_clip_ai_metadata(root, 8.0, 15.0)
    assert len(meta["transcript_cues"]) == 1


def test_missing_cues_on_the_root_yield_an_empty_list_not_a_crash():
    root = {k: v for k, v in ROOT_AI.items() if k != "transcript_cues"}
    meta = materialize_clip_ai_metadata(root, 0.0, 30.0)
    assert meta["transcript_cues"] == []
    assert meta["has_speech"] is False


# --------------------------------------------------------------------------
# Migration: cue-less persisted clip metadata must recompute from the root
# --------------------------------------------------------------------------

def test_persisted_metadata_without_cues_is_invalid():
    stale = {
        "analyzed": True,
        "source_window": {"start": 8.0, "end": 15.0},
        "scene_descriptions": [],
    }
    assert clip_metadata_is_valid(stale, 8.0, 15.0) is False


def test_persisted_metadata_with_cues_and_a_matching_window_is_valid():
    fresh = {
        "analyzed": True,
        "source_window": {"start": 8.0, "end": 15.0},
        "transcript_cues": [],
        "scene_descriptions": [],
    }
    assert clip_metadata_is_valid(fresh, 8.0, 15.0) is True
    assert clip_metadata_is_valid(fresh, 0.0, 15.0) is False  # window mismatch


def test_effective_metadata_recomputes_cues_for_stale_clip_data():
    """The end-to-end path a mix tool uses: stale clip data still yields cues."""
    file_data = {"ai_metadata": ROOT_AI}
    clip_data = {
        "start": 8.0,
        "end": 15.0,
        "position": 0.0,
        "ai_metadata": {
            "analyzed": True,
            "source_window": {"start": 8.0, "end": 15.0},
            "scene_descriptions": [],
        },
    }
    effective = get_effective_ai_metadata(
        file_data, clip_data, clip_ai_metadata=clip_data["ai_metadata"]
    )
    assert [c["text"] for c in effective["transcript_cues"]] == ["two"]
    assert effective["has_speech"] is True


def test_unanalyzed_root_yields_empty_metadata():
    assert materialize_clip_ai_metadata({"analyzed": False}, 0.0, 10.0) == {}
    assert get_effective_ai_metadata({"ai_metadata": {}}, {"start": 0.0, "end": 5.0}) == {}
