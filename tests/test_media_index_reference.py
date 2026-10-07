"""Recreate-from-reference matching and locating things in footage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index import library, reference  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from test_media_index_search import blend, build, shot, unit, w  # noqa: E402

REF, A, B = "a" * 64, "b" * 64, "c" * 64


@pytest.fixture
def lib(tmp_path):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "s"))
    # reference: 3 shots of 4 s, 2 s, 6 s with pictures 0, 1, 2 (plus a black one at the end)
    build(shelf, REF, shots=[shot(0, 0, 4), shot(1, 4, 6), shot(2, 6, 12), shot(3, 12, 14, black=True)], duration=14.0,
          watch=[w(0, 0, 4, "A runner on a beach"), w(1, 4, 6, "A close up of a clock"), w(2, 6, 12, "A spaceship lands"), w(3, 12, 14, "")],
          text=[], image=[({"shot": 0, "t": 1.0}, unit(0)), ({"shot": 1, "t": 5.0}, unit(1)), ({"shot": 2, "t": 8.0}, unit(2))])
    # A: a long beach clip (10 s) and a short clock clip (1 s)
    build(shelf, A, shots=[shot(0, 0, 10), shot(1, 10, 11)], duration=11.0,
          watch=[w(0, 0, 10, "Someone jogging on the sand"), w(1, 10, 11, "A wall clock")],
          text=[], image=[({"shot": 0, "t": 3.0}, unit(0)), ({"shot": 0, "t": 8.0}, blend(0, 5, 0.3)), ({"shot": 1, "t": 10.5}, unit(1))])
    # B: a second clock clip that is long enough
    build(shelf, B, shots=[shot(0, 0, 5)], duration=5.0, watch=[w(0, 0, 5, "A clock ticking", objects=[{"label": "clock", "box": [0.4, 0.3, 0.2, 0.3], "t": 1.5}],
                                                                  on_screen_text=[{"text": "OPEN", "box": [0.1, 0.1, 0.3, 0.1], "t": 2.0}])],
          text=[], image=[({"shot": 0, "t": 2.0}, blend(1, 9, 0.2))])
    f = lambda sha, fid, name: library.load_file_index(shelf, sha, file_id=fid, name=name)  # noqa: E731
    return f(REF, "R", "ref.mp4"), [f(REF, "R", "ref.mp4"), f(A, "A", "a.mp4"), f(B, "B", "b.mp4")]


def test_each_reference_shot_gets_candidates_that_fit_its_duration(lib):
    ref, files = lib
    out = reference.match_reference(ref, files)
    assert out["total"] == 3, "the black shot is not matched"
    s0, s1, s2 = out["shots"]
    assert (s0["reference_shot"], s0["duration"], s0["status"]) == (0, 4.0, "matched")
    top = s0["candidates"][0]
    assert (top["file_id"], top["fits"]) == ("A", True) and top["out"] - top["in"] == pytest.approx(4.0, abs=1e-3)
    assert 0.0 <= top["in"] and top["out"] <= 10.0, "the window stays inside the source shot"
    assert s1["status"] == "matched" and s1["candidates"][0]["file_id"] == "B", "a clip that is too short is ranked below one that fits"
    short = [c for c in s1["candidates"] if c["file_id"] == "A"]
    assert short and short[0]["fits"] is False and short[0]["short_by"] == pytest.approx(1.0, abs=1e-3)


def test_the_reference_is_never_its_own_candidate(lib):
    ref, files = lib
    for row in reference.match_reference(ref, files)["shots"]:
        assert all(c["file_id"] != "R" for c in row["candidates"])
    assert any(c["file_id"] == "R" for row in reference.match_reference(ref, files, include_reference=True)["shots"] for c in row["candidates"])


def test_a_shot_nothing_matches_says_so_and_gives_a_stock_query(lib):
    ref, files = lib
    s2 = reference.match_reference(ref, files)["shots"][2]
    assert s2["status"] == "no_match" and s2["candidates"] == [] and s2["stock_query"] == "A spaceship lands"


def test_a_shot_only_matched_by_too_short_footage_is_flagged_and_gets_a_stock_query(lib):
    ref, files = lib
    row = reference.match_reference(ref, [files[1]], start=4.0, end=6.0)["shots"][0]      # needs 2 s; A's clock is 1 s
    assert row["status"] == "short_only" and row["candidates"][0]["fits"] is False
    assert row["candidates"][0]["short_by"] == pytest.approx(1.0, abs=1e-3) and row["stock_query"] == "A close up of a clock"
    assert reference.match_reference(ref, [files[1]])["matched"] == 1, "only the long beach shot is filled"


def test_a_range_limits_the_reference_shots(lib):
    ref, files = lib
    out = reference.match_reference(ref, files, start=6.0, end=12.0)
    assert [r["reference_shot"] for r in out["shots"]] == [2]
    out = reference.match_reference(ref, files, start=1.0, end=3.0)
    assert out["shots"][0]["duration"] == pytest.approx(2.0)


def test_filters_narrow_the_candidates(lib):
    ref, files = lib
    out = reference.match_reference(ref, files, filters={"min_duration": 4.0})
    s0 = out["shots"][0]
    assert all(c["file_id"] == "A" and c["shot_id"] == 0 for c in s0["candidates"])


def test_a_reference_without_pictures_falls_back_to_its_descriptions(tmp_path):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "s2"))
    build(shelf, REF, shots=[shot(0, 0, 3)], duration=3.0, watch=[w(0, 0, 3, "A cat")],
          text=[({"kind": "shot", "shot": 0, "start": 0, "end": 3, "text": "A cat"}, unit(4))])
    build(shelf, A, shots=[shot(0, 0, 8)], duration=8.0, watch=[w(0, 0, 8, "A cat sleeping")],
          text=[({"kind": "shot", "shot": 0, "start": 0, "end": 8, "text": "A cat sleeping"}, unit(4))])
    ref = library.load_file_index(shelf, REF, file_id="R", name="r")
    other = library.load_file_index(shelf, A, file_id="A", name="a")
    out = reference.match_reference(ref, [other])
    assert out["shots"][0]["status"] == "matched" and out["shots"][0]["candidates"][0]["file_id"] == "A"


def test_a_file_with_no_analysed_shots_gives_an_empty_result(tmp_path):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "s3"))
    build(shelf, REF, shots=[], duration=3.0, watch=None, layers=("structure",))
    ref = library.load_file_index(shelf, REF, file_id="R")
    assert reference.match_reference(ref, [ref]) == {"shots": [], "matched": 0, "total": 0}


# ============================ locate ============================
def test_locate_finds_objects_and_text_with_absolute_time_and_box(lib):
    _, files = lib
    got = reference.locate(files, "clock")
    obj = next(r for r in got if r["kind"] == "object" and r["file_id"] == "B")
    assert obj["t"] == 1.5 and obj["box"] == [0.4, 0.3, 0.2, 0.3] and obj["box_precision"] == "rough"
    txt = reference.locate(files, "open", kind="text")
    assert len(txt) == 1 and txt[0]["label"] == "OPEN" and txt[0]["t"] == 2.0


def test_locate_time_is_relative_to_the_shot_start_in_the_file(tmp_path):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "s4"))
    build(shelf, A, shots=[shot(0, 0, 10), shot(1, 10, 20)], duration=20.0,
          watch=[w(0, 0, 10, "x"), w(1, 10, 20, "y", objects=[{"label": "red car", "box": None, "t": 3.0}])])
    fi = library.load_file_index(shelf, A, file_id="A", name="a")
    got = reference.locate([fi], "car")
    assert got[0]["t"] == 13.0 and got[0]["box"] is None and got[0]["shot_start"] == 10.0


def test_locate_respects_kind_range_and_limit(lib):
    _, files = lib
    assert reference.locate(files, "clock", kind="text") == []
    assert reference.locate(files, "clock", start=3.0, end=4.0) == []
    assert reference.locate(files, "") == [] and reference.locate(files, "zebra") == []
    assert len(reference.locate(files, "clock", limit=0)) == 0


@pytest.mark.parametrize("peak,expected", [(5.0, (3.0, 7.0)), (9.5, (6.0, 10.0)), (0.5, (0.0, 4.0)), (10.0, (6.0, 10.0))])
def test_the_cut_window_is_centred_on_the_peak_and_never_leaves_the_source_shot(peak, expected):
    got = reference._window({"peak": peak}, {"start": 0.0, "end": 10.0}, 4.0)
    assert (got["in"], got["out"], got["fits"]) == (expected[0], expected[1], True)


def test_a_source_shot_shorter_than_the_need_returns_the_whole_shot_flagged():
    got = reference._window({"peak": 3.0}, {"start": 2.0, "end": 4.5}, 4.0)
    assert got == {"in": 2.0, "out": 4.5, "fits": False, "short_by": 1.5}
