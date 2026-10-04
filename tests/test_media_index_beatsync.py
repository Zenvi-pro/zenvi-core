"""Moving cuts onto the beat: what moves, by how much, and why some cuts stay."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes.media_index.beatsync import CutClip, plan_beat_sync  # noqa: E402

BEATS = [round(0.5 * i, 3) for i in range(0, 40)]
FRAME = 1.0 / 30.0


def c(cid, start, end, *, src_in=10.0, layer=1, max_src=100.0, speed=1.0, speech=False):
    return CutClip(cid, layer, start, end, src_in, src_in + (end - start) * speed, speed, max_src, speech)


def plan(clips, beats=BEATS, **kw):
    return plan_beat_sync(clips, beats, frame=FRAME, **kw)


def test_a_cut_already_on_a_beat_is_left_alone():
    out = plan([c("a", 0, 2.0), c("b", 2.0, 5.0)])
    assert out["moves"] == [] and out["already_on_beat"] == 1 and out["cuts"] == 1


def test_a_cut_just_before_a_beat_moves_later_the_earlier_clip_runs_longer_and_the_later_one_loses_its_head():
    out = plan([c("a", 0, 1.9, src_in=10.0), c("b", 1.9, 5.0, src_in=30.0)])
    m = out["moves"][0]
    assert m["from"] == pytest.approx(1.9, abs=0.01) and m["to"] == pytest.approx(2.0, abs=0.02)
    delta = m["delta"]
    assert 0.09 < delta < 0.12
    assert m["earlier_source_out"] == pytest.approx(10.0 + 1.9 + delta, abs=1e-3), "A's out point moves with the cut"
    assert m["later_source_in"] == pytest.approx(30.0 + delta, abs=1e-3), "B starts later in its source by the same amount"
    assert m["later_position"] == pytest.approx(1.9 + delta, abs=1e-3)


def test_a_cut_just_after_a_beat_moves_earlier_the_earlier_clip_shortens_and_the_later_one_starts_sooner_in_its_source():
    out = plan([c("a", 0, 2.1, src_in=10.0), c("b", 2.1, 5.0, src_in=30.0)])
    m = out["moves"][0]
    assert m["delta"] == pytest.approx(-0.1, abs=0.02) and m["to"] == pytest.approx(2.0, abs=0.02)
    assert m["earlier_source_out"] == pytest.approx(10.0 + 2.1 + m["delta"], abs=1e-3)
    assert m["later_source_in"] == pytest.approx(30.0 + m["delta"], abs=1e-3)


def test_the_total_length_and_every_other_edge_stay_put():
    clips = [c("a", 0, 1.9), c("b", 1.9, 4.1, src_in=30.0), c("c", 4.1, 7.0, src_in=50.0)]
    out = plan(clips)
    assert len(out["moves"]) == 2
    ends = {m["later"]: m["later_position"] for m in out["moves"]}
    assert ends["b"] == pytest.approx(2.0, abs=0.02) and ends["c"] == pytest.approx(4.0, abs=0.02)
    # the outer edges (start of the first, end of the last) are never touched: nothing in the plan refers to them
    assert all(m["earlier"] != "c" for m in out["moves"]) and all(m["later"] != "a" for m in out["moves"])


def test_both_cuts_around_one_clip_can_move_and_the_plan_stays_consistent():
    out = plan([c("a", 0, 1.9), c("b", 1.9, 2.7, src_in=30.0), c("c", 2.7, 6.0, src_in=50.0)])
    first, second = out["moves"]
    assert first["to"] == pytest.approx(2.0, abs=0.02) and second["to"] == pytest.approx(2.5, abs=0.02)
    assert first["later"] == second["earlier"] == "b"
    b_start, b_end = first["later_position"], second["to"]
    assert b_end - b_start == pytest.approx(0.5, abs=0.04), "the clip between them is the distance between the two beats"
    assert first["later_source_in"] == pytest.approx(30.0 + first["delta"], abs=1e-3)
    assert second["earlier_source_out"] == pytest.approx(30.8 + second["delta"], abs=1e-3), "b's tail is only moved by the second cut"


def test_a_second_cut_that_would_crush_the_clip_between_stays_and_says_why():
    out = plan([c("a", 0, 1.9), c("b", 1.9, 2.2, src_in=30.0), c("c", 2.2, 6.0, src_in=50.0)], min_length=0.15)
    assert len(out["moves"]) == 1 and out["moves"][0]["earlier"] == "a"
    assert any("earlier clip would become too short" in s["why"] for s in out["skipped"])
    none = plan([c("a", 0, 1.9), c("b", 1.9, 2.2, src_in=30.0), c("c", 2.2, 6.0, src_in=50.0)], min_length=0.3)
    assert none["moves"] == [] and sum("too short" in s["why"] for s in none["skipped"]) == 2


def test_no_beat_close_enough_means_no_move():
    out = plan([c("a", 0, 1.78), c("b", 1.78, 5.0)], max_shift=0.2)
    assert out["moves"] == [] and "no beat within 0.20 s" in out["skipped"][0]["why"]
    assert len(plan([c("a", 0, 1.78), c("b", 1.78, 5.0)], max_shift=0.3)["moves"]) == 1


def test_an_empty_beat_list_moves_nothing():
    out = plan([c("a", 0, 1.9), c("b", 1.9, 5.0)], beats=[])
    assert out["moves"] == [] and out["skipped"][0]["why"].startswith("no beat within")


@pytest.mark.parametrize("a_max,expect_move", [(100.0, True), (11.95, False)])
def test_extending_the_earlier_clip_needs_spare_source_after_it(a_max, expect_move):
    out = plan([c("a", 0, 1.9, src_in=10.0, max_src=a_max), c("b", 1.9, 5.0, src_in=30.0)])
    assert bool(out["moves"]) is expect_move
    if not expect_move:
        assert "no spare source after" in out["skipped"][0]["why"]


def test_a_still_image_can_always_be_extended():
    out = plan([CutClip("img", 1, 0, 1.9, 0.0, 1.9, 1.0, None), c("b", 1.9, 5.0)])
    assert len(out["moves"]) == 1


@pytest.mark.parametrize("b_in,expect_move", [(30.0, True), (0.05, False)])
def test_moving_the_cut_earlier_needs_spare_source_before_the_later_clip(b_in, expect_move):
    out = plan([c("a", 0, 2.1), c("b", 2.1, 5.0, src_in=b_in)])
    assert bool(out["moves"]) is expect_move
    if not expect_move:
        assert "no spare source before" in out["skipped"][0]["why"]


def test_speaking_clips_are_left_alone_unless_asked():
    clips = [c("a", 0, 1.9), c("b", 1.9, 5.0, speech=True)]
    out = plan(clips)
    assert out["moves"] == [] and "mid-word" in out["skipped"][0]["why"]
    assert len(plan(clips, include_speech=True)["moves"]) == 1


def test_clips_whose_speed_is_changed_are_left_alone():
    out = plan([c("a", 0, 1.9, speed=2.0), c("b", 1.9, 5.0)])
    assert out["moves"] == [] and "speed" in out["skipped"][0]["why"]


def test_overlapping_clips_are_a_transition_and_are_left_alone_and_gaps_are_not_cuts():
    out = plan([c("a", 0, 2.3), c("b", 2.0, 5.0)])
    assert out["moves"] == [] and out["cuts"] == 0 and "transition" in out["skipped"][0]["why"]
    gap = plan([c("a", 0, 1.9), c("b", 2.4, 5.0)])
    assert gap["moves"] == [] and gap["cuts"] == 0 and gap["skipped"] == []


def test_each_track_is_planned_on_its_own():
    clips = [c("a1", 0, 1.9, layer=1), c("a2", 1.9, 4.0, layer=1, src_in=30.0), c("b1", 0, 2.6, layer=2), c("b2", 2.6, 5.0, layer=2, src_in=60.0)]
    out = plan(clips)
    assert sorted((m["layer"], m["to"]) for m in out["moves"]) == [(1, pytest.approx(2.0, abs=0.02)), (2, pytest.approx(2.5, abs=0.02))]


def test_targets_snap_to_whole_frames():
    out = plan([c("a", 0, 1.9), c("b", 1.9, 5.0)], beats=[2.013])
    assert out["moves"][0]["to"] * 30 == pytest.approx(round(out["moves"][0]["to"] * 30), abs=1e-6)


def test_the_input_is_not_modified_and_a_single_clip_has_no_cuts():
    clips = [c("a", 0, 1.9), c("b", 1.9, 5.0)]
    before = [(x.start, x.end, x.src_in, x.src_out) for x in clips]
    plan(clips)
    assert [(x.start, x.end, x.src_in, x.src_out) for x in clips] == before
    assert plan([c("solo", 0, 5.0)]) == {"moves": [], "skipped": [], "already_on_beat": 0, "cuts": 0}
