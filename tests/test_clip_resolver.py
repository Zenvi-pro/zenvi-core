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


def test_timeline_clip_id_takes_priority():
    mock_clip = MagicMock()
    mock_clip.id = "clip-abc"
    with _resolver_env({"clip-abc": mock_clip}):
        result = resolve_timeline_clip(timeline_clip_id="clip-abc", clip_query="ignored")
    assert result.ok
    assert result.clip is mock_clip


def test_single_clip_shortcut_without_query():
    row = _make_row("only", "Solo Clip")
    with patch("classes.clip_resolver._enumerate_timeline_clips", return_value=[row]):
        with _resolver_env():
            result = resolve_timeline_clip(clip_query="")
    assert result.ok
    assert result.clip.id == "only"


def test_ambiguous_query_returns_candidates():
    ai_dog = {"tags": {"objects": ["dog"]}, "description": "a dog runs"}
    ai_cat = {"tags": {"objects": ["dog"]}, "description": "another dog plays"}
    rows = [
        _make_row("a", "Clip A", position=0.0, ai=ai_dog),
        _make_row("b", "Clip B", position=12.0, ai=ai_cat),
    ]
    with patch("classes.clip_resolver._enumerate_timeline_clips", return_value=rows):
        with _resolver_env():
            result = resolve_timeline_clip(clip_query="dog")
    assert not result.ok
    assert len(result.candidates) >= 2


def test_resolve_clip_pair_by_adjacent_queries():
    ai_beach = {"tags": {"scenes": ["beach"]}, "description": "waves on sand"}
    ai_city = {"tags": {"scenes": ["city"]}, "description": "downtown skyline"}
    rows = [
        _make_row("beach", "Beach", position=0.0, ai=ai_beach),
        _make_row("city", "City", position=10.0, ai=ai_city),
    ]
    with patch("classes.clip_resolver._enumerate_timeline_clips", return_value=rows):
        with _resolver_env():
            result = resolve_clip_pair(clip_a_query="beach", clip_b_query="city")
    assert result.ok
    assert result.clip_a.id == "beach"
    assert result.clip_b.id == "city"


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
    row = _make_row("at-playhead", "Playhead Clip", position=5.0)
    with patch("classes.clip_resolver._enumerate_timeline_clips", return_value=[row]):
        with patch("classes.clip_resolver._playhead_position", return_value=7.0):
            with _resolver_env():
                result = resolve_timeline_clip(clip_query="")
    assert result.ok
    assert result.clip.id == "at-playhead"


def test_same_file_id_on_two_tracks_is_ambiguous():
    shared_file = "file-shared"
    ai = {"tags": {"objects": ["dog"]}, "description": "a dog runs"}
    rows = [
        _make_row("a", "Clip A", position=0.0, layer=1000000, ai=ai),
        _make_row("b", "Clip B", position=12.0, layer=2000000, ai=ai),
    ]
    rows[0][1]["file_id"] = shared_file
    rows[1][1]["file_id"] = shared_file
    with patch("classes.clip_resolver._enumerate_timeline_clips", return_value=rows):
        with patch("classes.clip_resolver._playhead_position", return_value=0.0):
            with _resolver_env():
                result = resolve_timeline_clip(clip_query="dog")
    assert not result.ok
    assert "multiple tracks" in result.error.lower()
    assert len(result.candidates) >= 2


if __name__ == "__main__":
    test_score_substring_and_tag_overlap()
    test_timeline_clip_id_takes_priority()
    test_single_clip_shortcut_without_query()
    test_ambiguous_query_returns_candidates()
    test_resolve_clip_pair_by_adjacent_queries()
    test_resolve_clip_pair_by_explicit_ids()
    test_playhead_single_clip_fallback()
    test_same_file_id_on_two_tracks_is_ambiguous()
    print("test_clip_resolver: ok")
