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
    assert rows["F1"]["stale"] == ["structure"], "a free local layer saved by an older version: refreshed on the next index run"
    assert rows["F1"]["older_cloud"] == ["watch"], "a paid cloud layer from an older version is kept as is (newer fields read as unknown)"
    assert rows["F2"]["ready"] == ["structure"] and "watch" in rows["F2"]["missing"]
    assert rows["F3"]["missing"] == ["structure", "look", "audio", "speech", "watch", "vectors"] and rows["F3"]["searchable"] is False
    _, incomplete = call("index_status_tool", only_incomplete=True)
    assert len(incomplete["files"]) == 3
    _, one = call("index_status_tool", file_ids=["F1"])
    assert [x["file_id"] for x in one["files"]] == ["F1"]


@pytest.mark.parametrize("out,fragment", [({"auth": True}, "sign in"), ({"unsupported": True}, "search_clips_tool"),
                                          ({"credits": True, "error": "Out of credits"}, "out of credits"),
                                          ({"rate_limited": True, "retry_after": 600, "error": "slow down"}, "about 11 min"),
                                          ({"rate_limited": True, "error": "slow down"}, "too many searches"),
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


# -- match / locate / view audio ---------------------------------------------------------
@pytest.fixture
def recreate(env, monkeypatch):
    """A reference (R) of two shots plus the two project files from `env`."""
    from test_media_index_reference import REF
    build(env.shelf, REF, shots=[shot(0, 0, 4), shot(1, 4, 7)], duration=7.0,
          watch=[w(0, 0, 4, "A calm lake"), w(1, 4, 7, "A spaceship lands")],
          text=[], image=[({"shot": 0, "t": 1.0}, unit(0)), ({"shot": 1, "t": 5.0}, unit(12))])
    env.files.append(file_obj("R", "ref.mp4", REF))
    return env


def test_match_reference_plans_each_shot_and_reports_the_gaps(recreate):
    head, r = call("match_reference_tool", reference_file_id="R")
    assert r["total"] == 2 and r["matched"] == 1 and "gaps: shots [1]" in head
    first, second = r["shots"]
    assert first["status"] == "matched" and first["candidates"][0]["file_id"] == "F1" and first["candidates"][0]["fits"] is True
    assert second["status"] == "no_match" and second["stock_query"] == "A spaceship lands"


def test_match_reference_can_be_limited_to_a_range_and_to_source_files(recreate):
    _, r = call("match_reference_tool", reference_file_id="R", reference_start=0.0, reference_end=4.0, candidate_file_ids=["F1"])
    assert [s["reference_shot"] for s in r["shots"]] == [0]
    out = REGISTRY["match_reference_tool"].func(reference_file_id="R", candidate_file_ids=["R"])
    assert out.startswith("Error") and "no other indexed footage" in out


def test_match_reference_needs_an_indexed_reference(recreate):
    assert REGISTRY["match_reference_tool"].func(reference_file_id="F3").startswith("Error")
    out = REGISTRY["match_reference_tool"].func(reference_file_id="nope")
    assert out.startswith("Error") and "no saved index" in out


def test_locate_finds_an_object_with_time_and_box_and_says_the_box_is_rough(env):
    build(env.shelf, "e" * 64, shots=[shot(0, 0, 8)], duration=8.0,
          watch=[w(0, 0, 8, "A kitchen", objects=[{"label": "kettle", "box": [0.5, 0.4, 0.1, 0.2], "t": 2.0}],
                   on_screen_text=[{"text": "SALE", "box": [0.1, 0.1, 0.2, 0.1], "t": 5.0}])])
    env.files.append(file_obj("F9", "kitchen.mp4", "e" * 64))
    head, r = call("locate_in_footage_tool", what="kettle")
    assert r["hits"][0]["file_id"] == "F9" and r["hits"][0]["t"] == 2.0 and r["hits"][0]["box"] == [0.5, 0.4, 0.1, 0.2]
    assert r["hits"][0]["box_precision"] == "rough" and "rough" in r["note"]
    _, t = call("locate_in_footage_tool", what="sale", kind="text")
    assert [h["label"] for h in t["hits"]] == ["SALE"]
    head, none = call("locate_in_footage_tool", what="zebra")
    assert none["hits"] == [] and "not found" in head and "not proof" in none["note"]


def test_locate_with_nothing_indexed_is_an_error(env, monkeypatch):
    monkeypatch.setattr(T, "_all_files", lambda: [env.files[2]])
    assert REGISTRY["locate_in_footage_tool"].func(what="x").startswith("Error")


def test_view_audio_draws_a_spectrogram_and_remembers_it(env, tmp_path, monkeypatch):
    import subprocess
    sys.path.insert(0, str(Path(__file__).parent))
    import media_fixtures as mf
    wav = tmp_path / "t.wav"
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=700:duration=4", str(wav)], check=True)
    sha = "f" * 64
    env.shelf.set_source(sha, duration=4.0, has_audio=True, media_type="audio")
    f = file_obj("A1", "t.wav", sha, "audio")
    f.data["path"] = str(wav)
    env.files.append(f)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    head, r = call("view_audio_tool", file_id="A1", start=0.0, end=4.0)
    assert Path(r["image_path"]).is_file() and r["cached"] is False and r["start"] == 0.0 and r["end"] == 4.0 and "log" in r["axes"]
    _, again = call("view_audio_tool", file_id="A1", start=0.0, end=4.0)
    assert again["cached"] is True and again["image_path"] == r["image_path"]


def test_view_audio_over_a_minute_comes_back_as_several_strips(env, tmp_path, monkeypatch):
    import subprocess
    sys.path.insert(0, str(Path(__file__).parent))
    import media_fixtures as mf
    wav = tmp_path / "long.wav"
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=400:duration=100", str(wav)], check=True)
    sha = "e" * 64
    env.shelf.set_source(sha, duration=100.0, has_audio=True, media_type="audio")
    f = file_obj("A2", "long.wav", sha, "audio")
    f.data["path"] = str(wav)
    env.files.append(f)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    head, r = call("view_audio_tool", file_id="A2", start=0.0, end=100.0)
    assert len(r["strips"]) == 2 and [(s["start"], s["end"]) for s in r["strips"]] == [(0.0, 50.0), (50.0, 100.0)] and "2 strips" in head
    assert all(Path(s["image_path"]).is_file() for s in r["strips"]) and r["image_path"] == r["strips"][0]["image_path"]


def test_view_audio_refuses_silent_files_and_unfingerprinted_ones(env):
    env.shelf.set_source(SHA2, duration=5.0, has_audio=False, media_type="video")
    out = REGISTRY["view_audio_tool"].func(file_id="F2")
    assert out.startswith("Error") and "no audio track" in out
    out = REGISTRY["view_audio_tool"].func(file_id="F3")
    assert out.startswith("Error") and "fingerprinted" in out
    out = REGISTRY["view_audio_tool"].func(file_id="F1", start=0.0, end=500.0)
    assert out.startswith("Error")


# -- dates, places and the new search arguments ------------------------------------------
@pytest.fixture
def trip_env(env):
    """F1 shot in San Francisco on 1 May, F2 in Tokyo on 3 May, F3 has no tags."""
    env.shelf.set_source(SHA1, captured_at="2024-05-01T09:00:00+00:00", gps={"lat": 37.7749, "lon": -122.4194})
    env.shelf.set_source(SHA2, captured_at="2024-05-03T10:00:00+00:00", gps={"lat": 35.6762, "lon": 139.6503})
    sha3 = "3" * 64                                                    # indexed, but its camera wrote no date or position
    build(env.shelf, sha3, shots=[shot(0, 0, 6)], duration=6.0, watch=[w(0, 0, 6, "A calm lake again")],
          text=[({"kind": "shot", "shot": 0, "start": 0, "end": 6, "text": "A calm lake again"}, unit(0))], image=[({"shot": 0, "t": 2.0}, unit(0))])
    env.files.append(file_obj("F5", "undated.mp4", sha3))
    library.clear_cache()
    return env


def run_search(**kw):
    _, r = call("search_footage_tool", limit=50, **kw)
    return sorted({h["file_id"] for h in r["hits"]})


def test_footage_can_be_limited_to_a_date_window(trip_env):
    assert run_search(captured_after="2024-05-02") == ["F2"]
    assert run_search(captured_before="2024-05-01") == ["F1"], "a date as the upper bound means the end of that day"
    assert run_search(captured_after="2024-05-01", captured_before="2024-05-03") == ["F1", "F2"]


def test_a_window_with_nothing_in_it_is_an_empty_search_not_an_error(trip_env):
    out = REGISTRY["search_footage_tool"].func(captured_after="2030-01-01")
    assert out.startswith("Error") and "no indexed footage" in out


def test_a_bad_date_is_a_clear_error(trip_env):
    out = REGISTRY["search_footage_tool"].func(captured_after="last tuesday")
    assert out.startswith("Error") and "is not a date" in out


def test_clips_without_a_capture_time_are_left_out_of_a_date_window_but_found_otherwise(trip_env):
    assert "F5" in run_search()
    assert "F5" not in run_search(captured_after="2000-01-01") and "F5" not in run_search(captured_before="2100-01-01")


def test_footage_can_be_limited_to_one_place_by_the_ids_the_overview_gives(trip_env):
    assert run_search(place_id=0) == ["F1"] and run_search(place_id=1) == ["F2"]
    assert run_search(place_id=-1) == ["F1", "F2", "F5"]
    assert run_search(place_id=1, captured_after="2024-05-02") == ["F2"]


def test_chronological_search_follows_when_things_were_shot(trip_env):
    _, r = call("search_footage_tool", sort="chronological", limit=50)
    assert list(dict.fromkeys(h["file_id"] for h in r["hits"])) == ["F1", "F2", "F5"], "by capture time, with undated footage last"
    _, back = call("search_footage_tool", sort="chronological", captured_after="2024-05-03", limit=50)
    assert {h["file_id"] for h in back["hits"]} == {"F2"}


def test_an_audio_only_file_with_just_an_audio_analysis_counts_as_indexed(env):
    sha = "7" * 64
    env.shelf.set_source(sha, duration=40.0, media_type="audio")
    env.shelf.write_json(sha, "audio.json", {"tempo": {"bpm": 100.0, "beats": [1.0, 1.6]}})
    env.shelf.set_layer(sha, "audio", version=2, status="ready")
    env.files.append(file_obj("M9", "song.mp3", sha, "audio"))
    found, missing = T.project_indexes()
    assert "M9" in [f.file_id for f in found] and "song.mp3" not in missing
    assert "new.mp4" in missing, "a file with no index at all is still reported as not indexed"


# -- which files only the original index can find ---------------------------------------
def test_v1_only_files_are_the_ones_with_an_original_index_and_no_local_vectors(monkeypatch):
    from classes.media_index import flags
    from classes.editor_tools import media_index_tools as MT
    from classes import twelvelabs_match as tm
    ready = {"index": {"status": "ready", "index_id": "i", "video_id": "v"}}

    def f(fid, ai, mt="video"):
        return SimpleNamespace(id=fid, data={"media_type": mt, "ai_metadata": ai})

    files = [f("old", ready), f("covered", ready), f("none", {}), f("pic", ready, "image"), f("song", ready, "audio")]
    monkeypatch.setattr(MT, "_all_files", lambda: files)
    monkeypatch.setattr(MT, "_index_for", lambda fo, shelf=None: SimpleNamespace(layers={"vectors": fo.id == "covered"}))
    monkeypatch.setattr(flags, "v2_enabled", lambda: True)
    assert MT.v1_only_file_ids() == {"old", "song"}, "images are not searched by the original index; a file with no index at all is not offered"
    monkeypatch.setattr(flags, "v2_enabled", lambda: False)
    assert MT.v1_only_file_ids() == set()
    assert tm.twelvelabs_is_indexed(tm.get_index_block(ready)) is True


def test_locate_with_an_action_gives_each_hit_the_masking_tools_arguments(env):
    build(env.shelf, "e" * 64, shots=[shot(0, 0, 8)], duration=8.0,
          watch=[w(0, 0, 8, "A kitchen", objects=[{"label": "kettle", "box": [0.5, 0.4, 0.1, 0.2], "t": 2.0}, {"label": "kettle", "t": 4.0}])])
    f = file_obj("F9", "kitchen.mp4", "e" * 64)
    f.data.update(width=1280, height=720, video_length=200)
    env.files.append(f)
    for fi_id in ("e" * 64,):
        env.shelf.set_source(fi_id, duration=8.0, has_audio=True, media_type="video", technical={"video": {"width": 640, "height": 360, "fps": 25.0}})
    library.clear_cache()
    _, plain = call("locate_in_footage_tool", what="kettle")
    assert all("handoff" not in h for h in plain["hits"])
    _, r = call("locate_in_footage_tool", what="kettle", for_action="blur_object")
    with_box, no_box = r["hits"]
    args = with_box["handoff"]["args"]
    assert args["action"] == "blur_object" and args["file_id"] == "F9" and args["seed_frame"] == 51 and args["boxes"][0]["x2"] <= 1280 and with_box["handoff"]["frame_size"] == [1280, 720], "the project file's size wins: it is what the masking tool checks against"
    assert "unavailable" in no_box["handoff"]


def test_the_handoff_is_given_only_for_the_first_few_hits(env):
    objects = [{"label": "kettle", "box": [0.1, 0.1, 0.1, 0.1], "t": float(i)} for i in range(8)]
    build(env.shelf, "e" * 64, shots=[shot(0, 0, 9)], duration=9.0, watch=[w(0, 0, 9, "A kitchen", objects=objects)])
    f = file_obj("F9", "kitchen.mp4", "e" * 64)
    f.data.update(width=640, height=360)
    env.files.append(f)
    env.shelf.set_source("e" * 64, duration=9.0, has_audio=True, media_type="video", technical={"video": {"width": 640, "height": 360, "fps": 25.0}})
    library.clear_cache()
    _, r = call("locate_in_footage_tool", what="kettle", for_action="mask_object")
    assert [("handoff" in h) for h in r["hits"]] == [True] * 5 + [False] * 3


def test_footage_can_be_limited_to_a_place_by_name(trip_env):
    assert run_search(place="san francisco") == ["F1"] and run_search(place="Tokyo") == ["F2"] and run_search(place="japan") == ["F2"]
    assert run_search(place="tokyo", captured_after="2024-05-02") == ["F2"]
    out = REGISTRY["search_footage_tool"].func(place="Lisbon")
    assert out.startswith("Error") and "no footage shot in 'Lisbon'" in out and "Tokyo, Japan" in out and "San Francisco" in out


def test_a_place_name_with_no_positions_says_so(env):
    out = REGISTRY["search_footage_tool"].func(place="Paris")
    assert out.startswith("Error") and "none of the clips has a position" in out


def test_the_overview_gives_place_names_not_coordinates(trip_env):
    _, r = call("get_project_overview_tool")
    places = r["trip"]["places"]
    assert [p["name"] for p in places] == ["San Francisco, CA, United States", "Tokyo, Japan"] and all("lat" not in p and "lon" not in p for p in places)
    assert "GeoNames" in r["trip"]["place_names"]
    _, precise = call("get_project_overview_tool", precise_places=True)
    assert all("lat" in p and "name" in p for p in precise["trip"]["places"])


def test_a_place_that_cannot_be_named_keeps_its_rounded_coordinates(env):
    env.shelf.set_source(SHA1, captured_at="2024-05-01T09:00:00+00:00", gps={"lat": 0.05, "lon": 0.05})
    library.clear_cache()
    _, r = call("get_project_overview_tool")
    p = r["trip"]["places"][0]
    assert "name" not in p and p["lat"] == 0.1 and p["lon"] == 0.1
