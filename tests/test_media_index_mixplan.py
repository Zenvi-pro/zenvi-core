"""The arithmetic of balancing a mix."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes.media_index import mixplan as M  # noqa: E402


def item(cid, level, gain=0.0):
    return {"id": cid, "level_db": level, "gain_db": gain}


def adj(out):
    return {a["id"]: a["delta_db"] for a in out["adjust"]}


def test_every_voice_is_brought_to_the_median_level():
    out = M.speech_adjustments([item("a", -18.0), item("b", -24.0), item("c", -20.0)])
    assert out["target_db"] == -20.0 and adj(out) == {"a": -2.0, "b": 4.0}
    assert [a["to_db"] for a in out["adjust"]] == [-20.0, -20.0] and out["left_alone"] == []


def test_the_gain_already_set_counts_toward_where_a_clip_sits_now():
    out = M.speech_adjustments([item("a", -30.0, gain=10.0), item("b", -20.0, gain=0.0), item("c", -20.0, gain=0.0)])
    assert out["adjust"] == [] and out["target_db"] == -20.0, "a is boosted to the same level already"


def test_voices_within_tolerance_are_left_alone_and_the_target_can_be_chosen():
    assert M.speech_adjustments([item("a", -20.0), item("b", -21.0), item("c", -19.5)])["adjust"] == []
    out = M.speech_adjustments([item("a", -20.0), item("b", -21.0)], target_db=-16.0)
    assert out["target_db"] == -16.0 and adj(out) == {"a": 4.0, "b": 5.0}


def test_one_pass_never_cuts_or_boosts_beyond_the_limits_and_says_so():
    out = M.speech_adjustments([item("quiet", -50.0), item("normal", -20.0), item("normal2", -20.0)])
    a = out["adjust"][0]
    assert a["id"] == "quiet" and a["delta_db"] == M.MAX_GAIN_DB and a["limited"] is True
    loud = M.speech_adjustments([item("loud", 0.0), item("a", -30.0), item("b", -30.0)])
    assert adj(loud)["loud"] == M.MIN_GAIN_DB


def test_a_clip_already_at_its_limit_is_reported_not_adjusted():
    out = M.speech_adjustments([item("quiet", -50.0, gain=9.0), item("a", -20.0), item("b", -20.0)])
    assert out["adjust"] == [] and out["left_alone"] == [{"id": "quiet", "why": "already at the boost limit"}]


def test_clips_without_a_level_or_with_automated_volume_are_left_alone():
    out = M.speech_adjustments([item("a", -18.0), {"id": "b", "level_db": None, "gain_db": 0.0}, {"id": "c", "level_db": -25.0, "gain_db": None}, item("d", -22.0)])
    assert {x["id"]: x["why"] for x in out["left_alone"]} == {"b": "its level is not indexed", "c": "its volume is automated"}
    assert set(adj(out)) <= {"a", "d"}


def test_nothing_to_balance_is_an_empty_plan():
    assert M.speech_adjustments([]) == {"target_db": None, "adjust": [], "left_alone": []}
    assert M.speech_adjustments([item("only", -20.0)])["adjust"] == []


@pytest.mark.parametrize("measured,target,peak,delta", [(-20.0, -14.0, -10.0, 6.0), (-8.0, -14.0, -1.0, -6.0), (-14.5, -14.0, -3.0, 0.0),
                                                        (-30.0, -14.0, -20.0, 9.0), (0.0, -14.0, 0.0, -14.0), (5.0, -14.0, 0.0, -15.0)])
def test_the_master_gain_moves_loudness_toward_the_target_within_limits(measured, target, peak, delta):
    assert M.master_correction(measured, target, peak)["delta_db"] == pytest.approx(delta, abs=0.01)


def test_raising_the_level_never_pushes_peaks_over_the_limit():
    out = M.master_correction(-20.0, -14.0, -4.0)            # wants +6 dB, only 3 dB of headroom to -1 dB
    assert out["delta_db"] == pytest.approx(3.0, abs=0.01) and "stay under" in out["peak_warning"]
    stuck = M.master_correction(-20.0, -14.0, -1.2)
    assert stuck["delta_db"] == 0.0 and "cannot get louder" in stuck["peak_warning"] and "compressor" in stuck["peak_warning"]


def test_lowering_the_level_ignores_the_peak_limit_and_unknown_peaks_are_not_a_blocker():
    assert M.master_correction(-8.0, -14.0, 0.0)["delta_db"] == -6.0
    assert M.master_correction(-20.0, -14.0, None)["delta_db"] == 6.0


def test_a_silent_mix_has_nothing_to_correct():
    out = M.master_correction(None, -14.0)
    assert out["delta_db"] == 0.0 and "silent" in out["reason"]
