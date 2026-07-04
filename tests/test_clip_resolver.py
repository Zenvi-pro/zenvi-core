"""Unit tests for tag/query timeline clip resolution."""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.clip_resolver import (  # noqa: E402
    MIN_CONFIDENCE,
    _score_clip_against_query,
    resolve_clip_pair,
    resolve_timeline_clip,
    search_timeline_placements,
)
from classes.timeline_clip_context import TimelineClipContext  # noqa: E402


def _make_context(
    clip_id,
    title,
    *,
    position=0.0,
    layer=1000000,
    file_name="",
    ai=None,
    source_start=0.0,
    source_end=10.0,
    file_id=None,
):
    fid = file_id or f"file-{clip_id}"
    eff = ai or {}
    return TimelineClipContext(
        timeline_clip_id=clip_id,
        file_id=fid,
        parent_file_id=fid,
        source_path=f"/tmp/{file_name or title}.mp4",
        source_start=source_start,
        source_end=source_end,
        timeline_position=position,
        timeline_duration=source_end - source_start,
        timeline_end=position + (source_end - source_start),
        layer=layer,
        ui_track=1 if layer == 1000000 else 2,
        title=title,
        file_name=file_name or f"{title}.mp4",
        effective_metadata=eff,
        tags_preview="dog" if "dog" in str(eff) else "",
    )


def _make_row(clip_id, title, *, position=0.0, layer=1000000, file_name="", ai=None):
    clip = MagicMock()
    clip.id = clip_id
    data = {
        "id": clip_id,
        "title": title,
        "position": position,
        "start": 0.0,
        "end": 10.0,
        "layer": layer,
        "file_id": f"file-{clip_id}",
    }
    file_data = {"name": file_name or f"{title}.mp4"} if file_name or title else None
    return clip, data, file_data, ai


def _mock_app_window():
    mock_app = MagicMock()
    mock_app.get_app.return_value.project.get.return_value = [
        {"number": 1000000, "id": "t1"},
        {"number": 2000000, "id": "t2"},
    ]
    mock_app.get_app.return_value.window = MagicMock()
    return mock_app


def _resolver_env(query_mapping=None):
    mock_query = MagicMock()
    if query_mapping is not None:
        mock_query.Clip.get.side_effect = lambda id=None, **kw: query_mapping.get(
            str(id or kw.get("id", ""))
        )
    return patch.dict(
        sys.modules,
        {"classes.query": mock_query, "classes.app": _mock_app_window()},
    )


def _patch_contexts(contexts):
    return patch(
        "classes.clip_resolver.enumerate_timeline_contexts",
        return_value=contexts,
    )


def test_score_substring_and_tag_overlap():
    score = _score_clip_against_query(
        {"title": "Beach Interview", "layer": 1000000},
        {"name": "interview.mp4"},
        {
            "tags": {"objects": ["dog"], "scenes": ["beach"]},
            "description": "person talks on the beach",
        },
        "beach interview",
    )
    assert score >= MIN_CONFIDENCE


def test_score_ignores_scenes_outside_trim():
    ai = {
        "analyzed": True,
        "tags": {"objects": ["car"], "scenes": []},
        "scene_descriptions": [{"time": 50.0, "description": "car on highway"}],
    }
    score = _score_clip_against_query(
        {"title": "Trimmed", "start": 0.0, "end": 10.0},
        {"name": "clip.mp4", "duration": 120.0},
        ai,
        "car highway",
    )
    assert score == 0.0


def test_score_includes_scenes_inside_trim():
    ai = {
        "analyzed": True,
        "tags": {"objects": ["cat"], "scenes": []},
        "scene_descriptions": [{"time": 5.0, "description": "cat playing"}],
    }
    score = _score_clip_against_query(
        {"title": "Trimmed", "start": 0.0, "end": 10.0},
        {"name": "clip.mp4", "duration": 120.0},
        ai,
        "cat",
    )
    assert score >= MIN_CONFIDENCE


def test_timeline_clip_id_takes_priority():
    mock_clip = MagicMock()
    mock_clip.id = "clip-abc"
    with _resolver_env({"clip-abc": mock_clip}):
        result = resolve_timeline_clip(timeline_clip_id="clip-abc", clip_query="ignored")
    assert result.ok
    assert result.clip is mock_clip


def test_single_clip_shortcut_without_query():
    ctx = _make_context("only", "Solo Clip")
    with _patch_contexts([ctx]):
        with _resolver_env({"only": MagicMock(id="only")}):
            result = resolve_timeline_clip(clip_query="")
    assert result.ok
    assert result.clip.id == "only"


def test_ambiguous_query_returns_candidates():
    ai_dog = {"analyzed": True, "tags": {"objects": ["dog"]}, "description": "a dog runs"}
    ai_cat = {"analyzed": True, "tags": {"objects": ["dog"]}, "description": "another dog plays"}
    contexts = [
        _make_context("a", "Clip A", position=0.0, ai=ai_dog),
        _make_context("b", "Clip B", position=12.0, ai=ai_cat),
    ]
    with _patch_contexts(contexts):
        with _resolver_env():
            result = resolve_timeline_clip(clip_query="dog")
    assert not result.ok
    assert len(result.candidates) >= 2


def test_same_file_same_track_two_placements():
    ai = {"analyzed": True, "tags": {"objects": ["tennis"]}, "description": "tennis"}
    shared = "file-shared"
    contexts = [
        _make_context("a", "A", position=0.0, ai=ai, file_id=shared, source_end=30.0),
        _make_context("b", "B", position=30.0, ai=ai, file_id=shared, source_start=60.0, source_end=90.0),
    ]
    with _patch_contexts(contexts):
        with _resolver_env({"a": MagicMock(id="a"), "b": MagicMock(id="b")}):
            amb = resolve_timeline_clip(clip_query="tennis")
            second = resolve_timeline_clip(clip_query="tennis", occurrence=2)
    assert not amb.ok
    assert second.ok
    assert second.clip.id == "b"


def test_prefer_track_hard_filter():
    ai = {"analyzed": True, "tags": {"objects": ["dog"]}, "description": "dog"}
    contexts = [
        _make_context("a", "A", layer=1000000, ai=ai),
        _make_context("b", "B", layer=2000000, ai=ai),
    ]
    with _patch_contexts(contexts):
        with _resolver_env({"b": MagicMock(id="b")}):
            result = resolve_timeline_clip(clip_query="dog", track="2")
    assert result.ok
    assert result.clip.id == "b"


def test_position_near_disambiguates():
    ai = {"analyzed": True, "tags": {"objects": ["ball"]}, "description": "ball"}
    contexts = [
        _make_context("a", "A", position=0.0, ai=ai, source_end=20.0),
        _make_context("b", "B", position=40.0, ai=ai, source_end=20.0),
    ]
    with _patch_contexts(contexts):
        with _resolver_env({"b": MagicMock(id="b")}):
            result = resolve_timeline_clip(clip_query="ball", position_near=45.0)
    assert result.ok
    assert result.clip.id == "b"


def test_resolve_clip_pair_by_adjacent_queries():
    ai_beach = {"analyzed": True, "tags": {"scenes": ["beach"]}, "description": "waves on sand"}
    ai_city = {"analyzed": True, "tags": {"scenes": ["city"]}, "description": "downtown skyline"}
    contexts = [
        _make_context("beach", "Beach", position=0.0, ai=ai_beach, source_end=10.0),
        _make_context("city", "City", position=10.0, ai=ai_city, source_end=10.0),
    ]
    with _patch_contexts(contexts):
        with _resolver_env({
            "beach": MagicMock(id="beach"),
            "city": MagicMock(id="city"),
        }):
            result = resolve_clip_pair(clip_a_query="beach", clip_b_query="city")
    assert result.ok
    assert result.clip_a.id == "beach"
    assert result.clip_b.id == "city"


def test_pair_same_file_sequential_trims():
    ai = {"analyzed": True, "tags": {"objects": ["scene"]}, "description": "scene"}
    shared = "file-x"
    contexts = [
        _make_context(
            "a", "A", position=0.0, ai=ai, file_id=shared,
            source_start=0.0, source_end=30.0,
        ),
        _make_context(
            "b", "B", position=30.0, ai=ai, file_id=shared,
            source_start=30.0, source_end=60.0,
        ),
    ]
    with _patch_contexts(contexts):
        with _resolver_env({
            "a": MagicMock(id="a"),
            "b": MagicMock(id="b"),
        }):
            result = resolve_clip_pair()
    assert result.ok
    assert result.clip_a.id == "a"
    assert result.clip_b.id == "b"


def test_resolve_clip_pair_by_explicit_ids():
    clip_a = MagicMock()
    clip_a.id = "id-a"
    clip_b = MagicMock()
    clip_b.id = "id-b"
    with _resolver_env({"id-a": clip_a, "id-b": clip_b}):
        result = resolve_clip_pair(clip_a_id="id-a", clip_b_id="id-b")
    assert result.ok
    assert result.clip_a is clip_a
    assert result.clip_b is clip_b


def test_playhead_single_clip_fallback():
    ctx = _make_context("at-playhead", "Playhead Clip", position=5.0)
    with _patch_contexts([ctx]):
        with patch("classes.clip_resolver._playhead_position", return_value=7.0):
            with _resolver_env({"at-playhead": MagicMock(id="at-playhead")}):
                result = resolve_timeline_clip(clip_query="")
    assert result.ok
    assert result.clip.id == "at-playhead"


def test_same_file_id_on_two_tracks_is_ambiguous():
    shared_file = "file-shared"
    ai = {"analyzed": True, "tags": {"objects": ["dog"]}, "description": "a dog runs"}
    contexts = [
        _make_context("a", "Clip A", position=0.0, layer=1000000, ai=ai, file_id=shared_file),
        _make_context("b", "Clip B", position=12.0, layer=2000000, ai=ai, file_id=shared_file),
    ]
    with _patch_contexts(contexts):
        with patch("classes.clip_resolver._playhead_position", return_value=0.0):
            with _resolver_env():
                result = resolve_timeline_clip(clip_query="dog")
    assert not result.ok
    assert "multiple tracks" in result.error.lower()
    assert len(result.candidates) >= 2


def test_search_timeline_placements_ranked():
    ai = {"analyzed": True, "tags": {"objects": ["tennis"]}, "description": "tennis"}
    contexts = [
        _make_context("c1", "One", ai=ai, source_end=30.0),
        _make_context("c2", "Two", position=30.0, ai=ai, source_start=60.0, source_end=90.0),
    ]
    with _patch_contexts(contexts):
        hits = search_timeline_placements("tennis")
    assert len(hits) == 2


if __name__ == "__main__":
    test_score_substring_and_tag_overlap()
    test_score_ignores_scenes_outside_trim()
    test_score_includes_scenes_inside_trim()
    test_timeline_clip_id_takes_priority()
    test_single_clip_shortcut_without_query()
    test_ambiguous_query_returns_candidates()
    test_same_file_same_track_two_placements()
    test_prefer_track_hard_filter()
    test_position_near_disambiguates()
    test_resolve_clip_pair_by_adjacent_queries()
    test_pair_same_file_sequential_trims()
    test_resolve_clip_pair_by_explicit_ids()
    test_playhead_single_clip_fallback()
    test_same_file_id_on_two_tracks_is_ambiguous()
    test_search_timeline_placements_ranked()
    print("test_clip_resolver: ok")
