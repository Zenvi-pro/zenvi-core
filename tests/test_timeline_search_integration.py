"""Integration tests for deep timeline placement search (mocked Clip/File, no Qt)."""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes.clip_resolver import (  # noqa: E402
    resolve_timeline_clip,
    search_timeline_placements,
)


def _file_data(*, scenes, file_id="file-main"):
    return {
        "id": file_id,
        "name": "long_video.mp4",
        "path": "/tmp/long_video.mp4",
        "duration": 120.0,
        "ai_metadata": {
            "analyzed": True,
            "tags": {"objects": ["tennis"], "scenes": ["court"], "activities": []},
            "scene_descriptions": scenes,
            "twelvelabs": {"status": "ready", "index_id": "i", "video_id": "v"},
        },
    }


def _clip(clip_id, *, position, start, end, layer=1000000, file_id="file-main"):
    obj = MagicMock()
    obj.id = clip_id
    obj.data = {
        "id": clip_id,
        "file_id": file_id,
        "position": position,
        "start": start,
        "end": end,
        "layer": layer,
        "title": f"Clip {clip_id}",
    }
    return obj


def _mock_env(clips, files):
    mock_clip = MagicMock()
    mock_clip.filter.return_value = clips
    mock_clip.get.side_effect = lambda id=None, **kw: next(
        (c for c in clips if str(c.id) == str(id or kw.get("id", ""))), None
    )
    mock_file = MagicMock()
    mock_file.get.side_effect = lambda id=None, path=None, **kw: MagicMock(
        id=id,
        data=files.get(str(id or ""), {}),
    ) if id and str(id) in files else None
    mock_file.filter.return_value = [
        MagicMock(id=fid, data=fd) for fid, fd in files.items()
    ]
    mock_app = MagicMock()
    mock_app.get_app.return_value.project.get.return_value = [
        {"number": 1000000, "id": "t1"},
        {"number": 2000000, "id": "t2"},
    ]
    return patch.dict(
        sys.modules,
        {"classes.query": MagicMock(Clip=mock_clip, File=mock_file), "classes.app": mock_app},
    )


def test_search_timeline_placements_two_trims_same_file():
    scenes = [
        {"time": 5.0, "description": "tennis serve on court"},
        {"time": 65.0, "description": "tennis rally on court"},
    ]
    files = {"file-main": _file_data(scenes=scenes)}
    clips = [
        _clip("c1", position=0.0, start=0.0, end=30.0),
        _clip("c2", position=30.0, start=60.0, end=90.0),
    ]
    with _mock_env(clips, files):
        hits = search_timeline_placements("tennis")
    assert len(hits) == 2
    assert hits[0].timeline_clip_id in ("c1", "c2")


def test_resolve_occurrence_second_placement():
    scenes = [
        {"time": 5.0, "description": "tennis serve"},
        {"time": 65.0, "description": "tennis rally"},
    ]
    files = {"file-main": _file_data(scenes=scenes)}
    clips = [
        _clip("c1", position=0.0, start=0.0, end=30.0),
        _clip("c2", position=30.0, start=60.0, end=90.0),
    ]
    with _mock_env(clips, files):
        result = resolve_timeline_clip(clip_query="tennis", occurrence=2)
    assert result.ok
    assert result.clip.id == "c2"


def test_same_file_different_tracks_requires_track():
    scenes = [{"time": 5.0, "description": "tennis match"}]
    fd = _file_data(scenes=scenes)
    files = {"file-main": fd}
    clips = [
        _clip("a", position=0.0, start=0.0, end=30.0, layer=1000000),
        _clip("b", position=0.0, start=0.0, end=30.0, layer=2000000),
    ]
    with _mock_env(clips, files):
        ambiguous = resolve_timeline_clip(clip_query="tennis")
        with_track = resolve_timeline_clip(clip_query="tennis", track="2")
    assert not ambiguous.ok
    assert with_track.ok
    assert with_track.clip.id == "b"


if __name__ == "__main__":
    test_search_timeline_placements_two_trims_same_file()
    test_resolve_occurrence_second_placement()
    test_same_file_different_tracks_requires_track()
    print("test_timeline_search_integration: ok")
