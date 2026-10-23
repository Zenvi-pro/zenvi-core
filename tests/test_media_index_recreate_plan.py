"""plan_recreation: one source window for every reference shot, chosen together.

Found on a real project: asked shot by shot, one 3 s clip was the best match for 41 of 85 reference shots (picture similarity only
spread from 0.54 to 0.65), so a recreation reused a handful of clips dozens of times. The plan is chosen for all shots at once, under
limits on reusing the same moment or the same file, and never plays one file twice in a row.
"""

import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes.media_index import reference as mref  # noqa: E402
from classes.media_index.library import FileIndex  # noqa: E402

DIMS = 8


def unit(i, noise=0.0, seed=0):
    v = np.zeros(DIMS, dtype=np.float32)
    v[i % DIMS] = 1.0
    if noise:
        v += np.random.default_rng(seed).normal(0, noise, DIMS).astype(np.float32)
    return v / np.linalg.norm(v)


def look(luma=0.5, warm=0.0, sat=0.3):
    return {"present": True, "avg_luma": luma, "warm_cool": warm, "sat_proxy": sat, "contrast_span": 0.4}


def make_file(sha, shots, kind_of=lambda i: i, luma_of=lambda i: 0.5, motion="static", per_shot_rows=3):
    """shots = [(start, end)]; each shot's pictures are one direction so a query finds it."""
    fi = FileIndex(sha=sha, file_id=sha, name=sha, duration=shots[-1][1])
    rows, vecs = [], []
    for i, (a, b) in enumerate(shots):
        fi.shots.append({"id": i, "start": a, "end": b, "motion": {"class": motion}, "look": look(luma_of(i)), "watch": {"description": f"{sha} shot {i}"}})
        for k in range(per_shot_rows):
            rows.append({"shot": i, "t": a + (b - a) * (k + 0.5) / per_shot_rows})
            vecs.append(unit(kind_of(i), noise=0.02, seed=zlib.crc32(f"{sha}/{i}/{k}".encode()) % 1000))   # stable across runs, unlike hash()
    fi.image_rows, fi.image_matrix = rows, np.stack(vecs)
    return fi


def reference(n, kind_of=lambda i: i, luma_of=lambda i: 0.5, length=1.0):
    shots = [(i * length, (i + 1) * length) for i in range(n)]
    return make_file("ref", shots, kind_of=kind_of, luma_of=luma_of)


def test_every_shot_gets_a_window_the_right_length_inside_one_source_shot():
    ref = reference(6)
    pool = [make_file("a", [(0, 8), (8, 16)]), make_file("b", [(0, 8), (8, 16)])]
    plan = mref.plan_recreation(ref, pool)
    assert plan["total"] == 6 and plan["unfilled"] == 0
    for row in plan["shots"]:
        c = row["choice"]
        assert abs((c["out"] - c["in"]) - row["duration"]) < 1e-6
        src = next(f for f in pool if f.file_id == c["file_id"]).shots[c["source_shot"]]
        assert src["start"] - 1e-6 <= c["in"] and c["out"] <= src["end"] + 1e-6


def test_a_moment_of_footage_is_used_once_and_no_file_plays_twice_in_a_row():
    ref = reference(12, kind_of=lambda i: i)                        # twelve different images; every source looks the same: one source is the "best" for all
    pool = [make_file("a", [(0, 12)], kind_of=lambda i: 0), make_file("b", [(0, 12)], kind_of=lambda i: 0), make_file("c", [(0, 12)], kind_of=lambda i: 0)]
    plan = mref.plan_recreation(ref, pool)
    assert plan["reuse"]["most_uses_of_one_moment"] <= 1
    assert plan["reuse"]["same_file_back_to_back"] == 0
    assert plan["reuse"]["files_used"] == 3 and max(plan["reuse"]["uses_per_file"].values()) <= 6


def test_footage_is_never_repeated_to_fill_a_gap_it_is_left_unfilled():
    ref = reference(10, kind_of=lambda i: i)
    pool = [make_file("a", [(0, 4)], kind_of=lambda i: 0)]          # four seconds of footage for ten one-second shots (plus a lone window for each)
    plan = mref.plan_recreation(ref, pool)
    assert plan["reuse"]["most_uses_of_one_moment"] <= 1
    assert plan["unfilled"] >= 5 and plan["matched"] + plan["weak"] + plan["unfilled"] == 10


def test_only_a_reference_image_that_comes_back_reuses_the_same_footage():
    ref = reference(9, kind_of=lambda i: i % 4 if i < 8 else 0)     # shots 0..3, then the same four again, then shot 0's image a third time
    pool = [make_file(f"f{k}", [(0, 6)], kind_of=lambda i, k=k: k) for k in range(4)]            # one file per image, so a repeat is not also a back-to-back file
    plan = mref.plan_recreation(ref, pool)
    repeats = {r["reference_shot"]: r["repeat_of"] for r in plan["shots"] if r.get("repeat_of") is not None}
    assert repeats.get(4) == 0 and repeats.get(5) == 1
    assert all(plan["shots"][i]["choice"]["source_shot"] == plan["shots"][i - 4]["choice"]["source_shot"] for i in (4, 5, 6, 7))


def test_a_clearly_better_picture_wins_when_nothing_else_differs():
    ref = reference(3, kind_of=lambda i: i)
    pool = [make_file("a", [(0, 6), (6, 12), (12, 18)], kind_of=lambda i: i)]
    plan = mref.plan_recreation(ref, pool)
    assert [r["choice"]["source_shot"] for r in plan["shots"]] == [0, 1, 2]


def test_the_measured_look_decides_when_the_pictures_are_alike():
    ref = reference(2, kind_of=lambda i: 0, luma_of=lambda i: 0.1)           # dark shots
    pool = [make_file("bright", [(0, 8)], kind_of=lambda i: 0, luma_of=lambda i: 0.9),
            make_file("dark1", [(0, 8)], kind_of=lambda i: 0, luma_of=lambda i: 0.1),
            make_file("dark2", [(0, 8)], kind_of=lambda i: 0, luma_of=lambda i: 0.1)]       # two dark ones, so no clip has to play twice in a row
    plan = mref.plan_recreation(ref, pool, max_reuse=1)
    assert {r["choice"]["file_id"] for r in plan["shots"]} == {"dark1", "dark2"}


def test_with_one_dark_and_one_bright_source_no_clip_plays_twice_in_a_row_even_if_that_costs_the_look():
    ref = reference(2, kind_of=lambda i: 0, luma_of=lambda i: 0.1)
    pool = [make_file("bright", [(0, 8)], kind_of=lambda i: 0, luma_of=lambda i: 0.9), make_file("dark", [(0, 8)], kind_of=lambda i: 0, luma_of=lambda i: 0.1)]
    plan = mref.plan_recreation(ref, pool, max_reuse=1)
    assert plan["reuse"]["same_file_back_to_back"] == 0 and {r["choice"]["file_id"] for r in plan["shots"]} == {"dark", "bright"}


def test_footage_too_short_for_a_slot_is_not_used_and_the_slot_is_reported():
    ref = reference(2, length=4.0)
    plan = mref.plan_recreation(ref, [make_file("tiny", [(0, 1.5)])])
    assert plan["unfilled"] == 2 and all(r["choice"] is None and r["status"] in ("no_match", "short_only") for r in plan["shots"])
    assert all(r["stock_query"] for r in plan["shots"])


def test_a_weak_choice_is_marked_weak_and_given_a_stock_query():
    ref = reference(2, kind_of=lambda i: 0, luma_of=lambda i: 0.05)
    pool = [make_file("a", [(0, 8)], kind_of=lambda i: 5, luma_of=lambda i: 0.95, motion="pan")]
    plan = mref.plan_recreation(ref, pool, min_score=0.9)
    assert all(r["status"] == "weak" and r["stock_query"] for r in plan["shots"])


def test_black_frames_and_tiny_flashes_are_left_out_and_alternatives_come_from_other_files():
    ref = reference(3)
    ref.shots[1]["black"] = True
    ref.shots[2]["end"] = ref.shots[2]["start"] + 0.1
    pool = [make_file("a", [(0, 8)]), make_file("b", [(0, 8)]), make_file("c", [(0, 8)])]
    plan = mref.plan_recreation(ref, pool)
    assert plan["total"] == 1
    first = plan["shots"][0]
    assert len({first["choice"]["file_id"], *[a["file_id"] for a in first["alternatives"]]}) == 3


def test_rank_of_window_says_how_good_a_placed_window_was_among_the_candidates():
    ref = reference(1, kind_of=lambda i: 0)
    pool = [make_file("same", [(0, 6)], kind_of=lambda i: 0), make_file("other", [(0, 6)], kind_of=lambda i: 4)]
    scored = mref.scored_windows(ref, ref.shots[0], 1.0, pool)
    good = mref.rank_of_window(scored, 0, 0.0, 1.0)
    bad = mref.rank_of_window(scored, 1, 0.0, 1.0)
    assert good > bad and good > 0.9


def test_a_shot_nothing_in_the_footage_looks_like_is_weak_even_if_it_is_the_best_of_what_there_is():
    ref = reference(8, kind_of=lambda i: i % 4)                     # four different images, each twice (the repeats are 4 apart)
    pool = [make_file(f"f{k}", [(0, 10)], kind_of=lambda i, k=k: k) for k in range(3)]       # footage of the first three images only
    plan = mref.plan_recreation(ref, pool)
    status = {r["reference_shot"]: r["status"] for r in plan["shots"]}
    assert status[0] == status[1] == status[2] == "matched"
    assert status[3] == "weak" and plan["shots"][3]["choice"]["picture_fit"] < mref.FIT_MIN


def test_a_stock_query_keeps_the_description_as_written_and_adds_the_lighting():
    q = mref.stock_query_for("Close-up of a female anime character with pink hair, looking down", {"avg_luma": 0.12})
    assert q.startswith("dark moody") and "anime character" in q and "person" not in q
    assert mref.stock_query_for("A wide shot of a street", {"avg_luma": 0.8}).startswith("bright")
    assert mref.stock_query_for("", None) == "footage"


def test_a_shot_that_nothing_in_the_footage_resembles_stands_out_from_shots_that_all_have_a_close_match():
    good = {i: 0.95 for i in range(10)}
    assert mref.unmatchable_shots({**good, 5: 0.75}) == {5: 0.2}
    assert mref.unmatchable_shots({i: 0.62 + 0.01 * (i % 3) for i in range(10)}) == {}          # everything matches equally badly: nothing stands out
    assert mref.unmatchable_shots({0: 0.9, 1: 0.5}) == {}                                          # too few shots to say


def test_a_returning_image_may_use_the_same_footage_even_when_that_file_plays_next_to_it():
    ref = reference(4, kind_of=lambda i: [0, 1, 2, 0][i])
    pool = [make_file("a", [(0, 4), (4, 8)], kind_of=lambda i: [0, 2][i]), make_file("b", [(0, 4)], kind_of=lambda i: 1)]
    plan = mref.plan_recreation(ref, pool)
    last = plan["shots"][3]
    assert last["repeat_of"] == 0 and last["choice"]["file_id"] == "a" and last["choice"]["source_shot"] == plan["shots"][0]["choice"]["source_shot"]
