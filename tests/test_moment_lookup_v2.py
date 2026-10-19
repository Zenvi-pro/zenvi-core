"""Moment lookup on the local index: add_clip_to_timeline, split_file_add_clip and the overlay boosts take the
best search hit for a described moment (its start, end and peak), keep the window asked for when there is none,
and never call the cloud search or the cloud watch."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

from classes import tool_handlers as th  # noqa: E402
from classes.editor_tools import media_index_tools as T  # noqa: E402
from classes.editor_tools._base import ToolError  # noqa: E402
from classes.media_index import library  # noqa: E402
from classes.media_index import schema as S  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from test_media_index_search import DIMS, SHA1, build, shot, unit, w  # noqa: E402

LAKE = {"path": "/media/lake.mp4", "name": "lake.mp4", "media_type": "video", "duration": 20.0,
        "start": 0.0, "end": 20.0, "has_video": True, "fingerprint": {"sha256": SHA1}}


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    """F1 is indexed: a calm lake in 0-10 s, a dog in 10-20 s. F2 is in the project but not indexed."""
    monkeypatch.setattr(S, "EMBED_DIMS", DIMS)
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    build(shelf, SHA1, shots=[shot(0, 0, 10), shot(1, 10, 20)],
          watch=[w(0, 0, 10, "A calm lake"), w(1, 10, 20, "A dog runs")],
          speech={"sentences": [], "per_shot": {}}, duration=20.0,
          text=[({"kind": "shot", "shot": 0, "start": 0, "end": 10, "text": "A calm lake"}, unit(0)),
                ({"kind": "shot", "shot": 1, "start": 10, "end": 20, "text": "A dog runs"}, unit(1))],
          image=[({"shot": 0, "t": 2.0}, unit(0)), ({"shot": 1, "t": 12.0}, unit(1))])
    files = [SimpleNamespace(id="F1", data=dict(LAKE)),
             SimpleNamespace(id="F2", data={"path": "/media/new.mp4", "name": "new.mp4", "media_type": "video",
                                            "duration": 20.0, "fingerprint": None})]
    monkeypatch.setattr(T, "default_shelf", lambda: shelf)
    monkeypatch.setattr(T, "_all_files", lambda: files)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    embedded = []

    def fake_embed(text):
        embedded.append(text)
        return {"a dog runs": unit(1), "a calm lake": unit(0)}.get(text, unit(14))

    monkeypatch.setattr(T, "embed_query", fake_embed)
    return SimpleNamespace(embedded=embedded)


def test_the_best_hit_is_clipped_to_the_window(indexed):
    hit = th._v2_moment("a dog runs", file_id="F1", window=(5.0, 15.0))
    assert (hit["file_id"], hit["start"], hit["end"]) == ("F1", 10.0, 15.0)
    assert 10.0 <= hit["peak"] <= 15.0
    assert indexed.embedded == ["a dog runs"]


def test_no_hit_in_the_window_and_no_index_are_both_none_and_the_second_costs_nothing(indexed):
    assert th._v2_moment("a calm lake", file_id="F1", window=(10.0, 20.0)) is None
    assert th._v2_moment("a dog runs", file_id="F2") is None
    assert indexed.embedded == ["a calm lake"], "a file with no index is not sent to be embedded"


def test_a_refused_search_is_no_moment_not_an_error(indexed, monkeypatch):
    def refused(text):
        raise ToolError("out of credits for searching by description")

    monkeypatch.setattr(T, "embed_query", refused)
    assert th._v2_moment("a dog runs", file_id="F1") is None


def _place(monkeypatch, file_data, **kwargs):
    """add_clip_to_timeline on F1 with the app stubbed; returns (out, placed clip, the cloud client)."""
    file_obj = MagicMock()
    file_obj.id = "F1"
    file_obj.data = file_data
    query_mod = MagicMock()
    query_mod.File.get.return_value = file_obj
    query_mod.Track.get.return_value = MagicMock(data={"number": 1000000})
    query_mod.Clip.filter.return_value = []
    app = MagicMock()

    def project_get(key, default=None):
        if key == "layers":
            return [{"number": 1000000, "label": "1"}]
        if key == "fps":
            return {"num": 30, "den": 1}
        return default

    app.project.get.side_effect = project_get
    app.window.selected_tracks = []
    placed = {"id": "C1", "start": 0.0, "end": 20.0}
    app.window.timeline.addClip.return_value = placed
    cloud = MagicMock()
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: cloud)
    monkeypatch.setattr(th, "_get_app", lambda: app)
    kwargs.setdefault("position_seconds", "0")
    with patch.dict(sys.modules, {"classes.query": query_mod}):
        out = th.add_clip_to_timeline(file_id="F1", **kwargs)
    return out, placed, cloud


def test_add_clip_places_the_hit_for_the_described_moment(indexed, monkeypatch):
    out, placed, cloud = _place(monkeypatch, dict(LAKE), query="a dog runs", start_seconds="0", duration_seconds="20")
    assert not out.startswith("Error:"), out
    assert (placed["start"], placed["end"]) == (10.0, 20.0)
    assert "matched" in out
    assert indexed.embedded == ["a dog runs"]
    cloud.search.assert_not_called()
    cloud.watch_window.assert_not_called()


def test_add_clip_keeps_the_window_when_the_moment_is_not_in_it(indexed, monkeypatch):
    out, placed, cloud = _place(monkeypatch, dict(LAKE), query="a calm lake", start_seconds="10", duration_seconds="10")
    assert not out.startswith("Error:"), out
    assert (placed["start"], placed["end"]) == (10.0, 20.0)
    assert "matched" not in out
    cloud.search.assert_not_called()
    cloud.watch_window.assert_not_called()


def test_add_clip_refuses_a_blind_first_n_seconds_trim_when_the_search_is_refused(indexed, monkeypatch):
    def refused(text):
        raise ToolError("sign in to Zenvi to search by description")

    monkeypatch.setattr(T, "embed_query", refused)
    out, _placed, cloud = _place(monkeypatch, dict(LAKE), query="a dog runs", start_seconds="0", duration_seconds="20")
    assert out.startswith("Error:") and "start_seconds and end_seconds" in out, out
    cloud.watch_window.assert_not_called()


def _split(monkeypatch, *, start, end, query, duration=60.0):
    """split_file_add_clip on F1 with a fake File; returns (out, the subclips saved)."""
    saved = []
    parent = MagicMock()
    parent.data = {"fps": {"num": 30, "den": 1}, "start": 0.0, "end": duration, "duration": duration,
                   "path": "/media/lake.mp4", "name": "lake.mp4", "media_type": "video",
                   "fingerprint": {"sha256": SHA1}}

    class FakeFile:
        def __init__(self):
            self.data = {}
            self.id = None
            self.key = None
            self.type = None

        def save(self):
            self.id = "new-sub"
            saved.append(dict(self.data))

        @staticmethod
        def get(id=""):
            return parent

    query_mod = MagicMock()
    query_mod.File = FakeFile
    with patch.dict(sys.modules, {"classes.query": query_mod}):
        out = th.split_file_add_clip(file_id="F1", start_seconds=start, end_seconds=end, query=query, name="clip")
    return out, saved


def test_split_cuts_the_subclip_at_the_hit(indexed, monkeypatch):
    out, saved = _split(monkeypatch, start="0", end="20", duration=20.0, query="a dog runs")
    assert saved, out
    assert (saved[0]["start"], saved[0]["end"]) == (10.0, 20.0)
    assert indexed.embedded == ["a dog runs"]


def test_split_refuses_a_long_window_the_index_cannot_place(indexed, monkeypatch):
    out, saved = _split(monkeypatch, start="10", end="40", query="a calm lake")
    assert out.startswith("Error:") and "visual match" in out.lower(), out
    assert saved == []


def test_the_overlay_boosts_are_the_index_peaks_for_the_avoid_and_prefer_queries(indexed, monkeypatch):
    from classes.mg_placement import AVOID_QUERY, PREFER_QUERY

    monkeypatch.setattr(T, "embed_query", lambda text: {AVOID_QUERY: unit(0), PREFER_QUERY: unit(1)}[text])
    ctx = SimpleNamespace(file_id="F1", parent_file_id="F1", timeline_position=0.0, timeline_end=20.0, source_start=0.0)
    boosts = sorted(th._overlay_index_boosts([ctx]), key=lambda b: b["t"])
    assert boosts == [
        {"t": 5.0, "score_delta": -3, "avoid": True, "prefer": False},
        {"t": 15.0, "score_delta": 3, "avoid": False, "prefer": True},
    ]
