"""The agent-facing media index tools, over a real shelf with the cloud and project stubbed."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import REGISTRY, media_index_tools as T  # noqa: E402
from classes.editor_tools._base import ToolError  # noqa: E402
from classes.media_index import library  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from test_media_index_search import DIMS, SHA1, SHA2, blend, build, profile, shot, unit, w  # noqa: E402
import classes.media_index.schema as S  # noqa: E402


def call(name, **kw):
    out = REGISTRY[name].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


def file_obj(fid, name, sha, media_type="video"):
    return SimpleNamespace(id=fid, data={"path": f"/media/{name}", "media_type": media_type, "name": name,
                                         "fingerprint": {"sha256": sha} if sha else None})


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "EMBED_DIMS", DIMS)
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    build(shelf, SHA1, shots=[shot(0, 0, 10, "pan", _look=profile(0.2, -0.1)), shot(1, 10, 20, "static", _look=profile(0.7, 0.2))],
          watch=[w(0, 0, 10, "A calm lake", shot_type="wide"), w(1, 10, 20, "A dog runs", shot_type="medium")],
          speech={"sentences": [], "per_shot": {}}, duration=20.0,
          text=[({"kind": "shot", "shot": 0, "start": 0, "end": 10, "text": "A calm lake"}, unit(0)),
                ({"kind": "shot", "shot": 1, "start": 10, "end": 20, "text": "A dog runs"}, unit(1))],
          image=[({"shot": 0, "t": 2.0}, unit(0)), ({"shot": 1, "t": 12.0}, unit(1))])
    build(shelf, SHA2, shots=[shot(0, 0, 5)], layers=("structure",), duration=5.0)
    files = [file_obj("F1", "lake.mp4", SHA1), file_obj("F2", "partial.mp4", SHA2), file_obj("F3", "new.mp4", None),
             file_obj("F4", "title.svg", None, "title")]
    monkeypatch.setattr(T, "default_shelf", lambda: shelf)
    monkeypatch.setattr(T, "_all_files", lambda: files)
    monkeypatch.setattr("classes.editor_tools.media_index_tools.resolve_files",
                        lambda ids, query="": [f for f in files if (f.id in (ids or []) or (query and query in f.data["name"]))] or
                        (_ for _ in ()).throw(ToolError("no project file matches")))
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    queries = []

    def fake_embed(text):
        queries.append(text)
        return {"lake": unit(0), "dog": unit(1)}.get(text.split()[0], unit(14))

    monkeypatch.setattr(T, "embed_query", fake_embed)
    return SimpleNamespace(shelf=shelf, queries=queries, files=files)


def test_search_returns_moments_with_file_and_times(env):
    _, r = call("search_footage_tool", query="lake view")
    top = r["hits"][0]
    assert (top["file_id"], top["start"], top["end"]) == ("F1", 0, 10) and top["name"] == "lake.mp4" and "sha" not in top
    assert env.queries[-1] == "lake view" and r["total"] >= 1 and r["next"] is None


def test_files_without_an_index_are_reported_not_hidden(env):
    _, r = call("search_footage_tool", query="lake view")
    assert r["not_indexed"] == ["new.mp4"] and all(h["file_id"] == "F1" for h in r["hits"])


def test_a_query_that_matches_nothing_says_so(env):
    head, r = call("search_footage_tool", query="spaceship launch")
    assert r["hits"] == [] and head.startswith("no matches")


def test_filters_work_without_a_query_and_without_signing_in(env, monkeypatch):
    monkeypatch.setattr(T, "embed_query", lambda t: (_ for _ in ()).throw(AssertionError("no embedding needed")))
    _, r = call("search_footage_tool", filters={"camera": "pan"})
    assert [(h["file_id"], h["shot_id"]) for h in r["hits"]] == [("F1", 0)]
    _, r = call("search_footage_tool", filters={"look": "warm"})
    assert [(h["file_id"], h["shot_id"]) for h in r["hits"]] == [("F1", 1)]


def test_a_reference_range_finds_similar_footage_without_any_cloud_call(env, monkeypatch):
    monkeypatch.setattr(T, "embed_query", lambda t: (_ for _ in ()).throw(AssertionError("no embedding needed")))
    _, r = call("search_footage_tool", reference_file_id="F1", reference_start=10.0, reference_end=20.0)
    assert (r["hits"][0]["file_id"], r["hits"][0]["shot_id"]) == ("F1", 1)


def test_a_look_reference_ranks_by_colour(env):
    _, r = call("search_footage_tool", reference_file_id="F1", reference_start=10.0, reference_end=20.0, match="look")
    assert r["hits"][0]["shot_id"] == 1 and r["hits"][0]["scores"]["look_distance"] == pytest.approx(0, abs=1e-3)


def test_a_reference_that_is_not_fully_indexed_is_a_clear_error(env):
    out = REGISTRY["search_footage_tool"].func(reference_file_id="F2")
    assert out.startswith("Error") and "no picture vectors" in out
    out = REGISTRY["search_footage_tool"].func(reference_file_id="F3")
    assert out.startswith("Error") and "no saved index" in out


def test_a_project_with_nothing_indexed_says_what_to_do(env, monkeypatch):
    monkeypatch.setattr(T, "_all_files", lambda: [env.files[2]])
    out = REGISTRY["search_footage_tool"].func(query="lake")
    assert out.startswith("Error") and "index_status_tool" in out


def test_results_page(env):
    _, first = call("search_footage_tool", limit=1)
    assert len(first["hits"]) == 1 and first["next"] == 1
    _, second = call("search_footage_tool", limit=1, cursor=first["next"])
    assert second["hits"][0]["shot_id"] != first["hits"][0]["shot_id"] or second["hits"][0]["file_id"] != first["hits"][0]["file_id"]


def test_dossier_reads_the_notes_of_a_file(env):
    head, r = call("get_segment_dossier_tool", file_id="F1")
    assert "A dog runs" in r["notes"] and "FILE lake.mp4" in r["notes"] and r["file_id"] == "F1"
    _, part = call("get_segment_dossier_tool", file_id="F1", start=11.0, end=15.0)
    assert "A dog runs" in part["notes"] and "A calm lake" not in part["notes"]


def test_dossier_of_an_unindexed_file_is_an_error(env):
    out = REGISTRY["get_segment_dossier_tool"].func(file_query="new.mp4")
    assert out.startswith("Error") and "no saved index" in out


def test_look_profile_reads_stored_numbers_and_compares(env):
    _, r = call("get_look_profile_tool", file_id="F1")
    assert r["profile"]["present"] and r["across_shots"]["luma_min"] == 0.2 and r["across_shots"]["luma_max"] == 0.7
    _, part = call("get_look_profile_tool", file_id="F1", start=10.0, end=20.0)
    assert part["profile"]["avg_luma"] == 0.7 and "across_shots" not in part
    _, cmp = call("get_look_profile_tool", file_id="F1", start=10.0, end=20.0, compare_to_file_id="F1")
    assert cmp["distance_to_compare"] is not None and cmp["distance_to_compare"] >= 0


def test_look_profile_needs_a_measured_look(env):
    out = REGISTRY["get_look_profile_tool"].func(file_id="F2")
    assert out.startswith("Error") and "no measured look" in out


def test_a_layer_a_file_cannot_have_is_not_reported_as_missing(env):
    env.shelf.set_layer(SHA2, "audio", version=1, status="not_applicable", note="no audio track")
    env.shelf.set_layer(SHA2, "speech", version=1, status="not_applicable", note="no audio track")
    _, r = call("index_status_tool", file_ids=["F2"])
    row = r["files"][0]
    assert row["not_applicable"] == ["audio", "speech"] and "audio" not in row["missing"] and "speech" not in row["missing"]
    assert "watch" in row["missing"]
    fi = library.load_file_index(env.shelf, SHA2)
    assert fi.not_applicable == ["audio", "speech"]
    from classes.media_index.dossier import build_dossier
    notes = build_dossier(fi)["text"]
    assert "NOT INDEXED YET" in notes and "audio" not in notes.split("NOT INDEXED YET")[1] and "watch" in notes


def test_index_status_lists_each_layer_and_skips_non_media(env):
    head, r = call("index_status_tool")
    rows = {x["file_id"]: x for x in r["files"]}
    assert set(rows) == {"F1", "F2", "F3"}
    assert rows["F1"]["missing"] == ["audio"] and rows["F1"]["searchable"] is True
    assert rows["F2"]["ready"] == ["structure"] and "watch" in rows["F2"]["missing"]
    assert rows["F3"]["missing"] == ["structure", "look", "audio", "speech", "watch", "vectors"] and rows["F3"]["searchable"] is False
    _, incomplete = call("index_status_tool", only_incomplete=True)
    assert len(incomplete["files"]) == 3
    _, one = call("index_status_tool", file_ids=["F1"])
    assert [x["file_id"] for x in one["files"]] == ["F1"]


@pytest.mark.parametrize("out,fragment", [({"auth": True}, "sign in"), ({"unsupported": True}, "search_clips_tool"),
                                          ({"error": "boom"}, "boom"), ({"vectors": [None]}, "no vector")])
def test_embedding_failures_become_actionable_errors(monkeypatch, out, fragment):
    monkeypatch.undo()
    import classes.api_client as api
    monkeypatch.setattr(api, "get_backend_client", lambda: SimpleNamespace(v2_embed=lambda *a, **k: out))
    with pytest.raises(ToolError) as e:
        T.embed_query("x")
    assert fragment in str(e.value)


def test_a_good_embedding_is_a_float32_vector(monkeypatch):
    import classes.api_client as api
    vec = unit(3).astype("<f2").tobytes()
    monkeypatch.setattr(api, "get_backend_client",
                        lambda: SimpleNamespace(v2_embed=lambda *a, **k: {"vectors": [base64.b64encode(vec).decode()]}))
    v = T.embed_query("x")
    assert v.dtype == np.float32 and v.shape == (DIMS,) and v[3] == 1.0 and blend(0, 1).shape == (DIMS,)


# -- the older search tools -------------------------------------------------------------
@pytest.fixture
def v2_on(env, monkeypatch):
    from classes.media_index import flags
    monkeypatch.setattr(flags, "v2_enabled", lambda: True)
    return env


def test_with_the_preference_off_the_older_search_is_untouched(env, monkeypatch):
    from classes.media_index import flags
    monkeypatch.setattr(flags, "v2_enabled", lambda: False)
    assert T.legacy_search_clips("lake", 5) is None
    assert T.legacy_search_in_clip("lake", 5, "F1", 0, 20, "lake") is None


def test_with_the_preference_on_search_clips_answers_in_its_own_format(v2_on):
    out = T.legacy_search_clips("lake view", 5)
    assert out.startswith("Found 1 match(es) across 1 project media item(s)")
    assert "lake.mp4 media_bin_file_id=F1" in out and "start_seconds=0.000 end_seconds=10.000 peak_seconds=" in out
    assert "To PLACE a moment: place_moment(file_id=" in out and "1 file(s) are not in the local index yet" in out


def test_no_match_says_so_in_the_original_words(v2_on):
    assert T.legacy_search_clips("spaceship launch", 5).startswith("No index matches for 'spaceship launch'")


def test_several_occurrences_are_numbered_in_time_order_with_the_best_marked(v2_on, monkeypatch):
    monkeypatch.setattr(T, "embed_query", lambda t: blend(0, 1, 0.7))     # the later shot ranks first
    out = T.legacy_search_clips("anything", 5)
    assert "2 occurrences:" in out and out.index("1. start_seconds=0.000") < out.index("2. start_seconds=10.000")
    best_line = next(x for x in out.splitlines() if "<-- best match" in x)
    assert "2. start_seconds=10.000" in best_line
    second = T.legacy_search_clips("anything", 5, nth=2)
    assert "occurrence #2 start_seconds=10.000" in second


def test_with_nothing_indexed_yet_the_original_search_runs(v2_on, monkeypatch):
    monkeypatch.setattr(T, "_all_files", lambda: [v2_on.files[2]])
    assert T.legacy_search_clips("lake", 5) is None


def test_a_signed_out_search_is_an_error_not_a_silent_fallback(v2_on, monkeypatch):
    monkeypatch.setattr(T, "embed_query", lambda t: (_ for _ in ()).throw(ToolError("sign in to Zenvi")))
    assert T.legacy_search_clips("lake", 5).startswith("Error: sign in")


def test_search_in_one_clip_is_limited_to_its_trimmed_range(v2_on, monkeypatch):
    monkeypatch.setattr(T, "embed_query", lambda t: blend(0, 1, 0.5))
    out = T.legacy_search_in_clip("anything", 5, "F1", 8.0, 14.0, "lake clip")
    assert out.startswith("Index matches in 'lake clip' (0:08 - 0:14):")
    assert "keep 0:00-0:02 peak" in out and "keep 0:02-0:06 peak" in out
    assert T.legacy_search_in_clip("anything", 5, "F1", 100.0, 110.0, "x") is None
    assert T.legacy_search_in_clip("anything", 5, "F2", 0.0, 5.0, "x") is None
