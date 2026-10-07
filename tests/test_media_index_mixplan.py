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


def test_a_loud_outlier_is_cut_to_the_median_voice():
    out = M.speech_adjustments([item("loud", -10.0), item("b", -21.0), item("c", -22.0)])
    assert out["target_db"] == -21.0 and adj(out) == {"loud": -11.0} and out["adjust"][0]["to_db"] == -21.0 and out["left_alone"] == []


def test_voices_are_levelled_to_what_the_quietest_can_reach_because_volume_stops_at_130_percent():
    # a clip can be set to at most +2.28 dB, so the quiet voice is lifted as far as it can go and the others come down to it
    out = M.speech_adjustments([item("a", -18.0), item("b", -24.0), item("c", -20.0)])
    assert M.MAX_GAIN_DB == pytest.approx(2.28, abs=0.005)
    assert out["target_db"] == pytest.approx(-21.72, abs=0.01)
    assert adj(out) == {"a": pytest.approx(-3.72, abs=0.01), "b": M.MAX_GAIN_DB, "c": pytest.approx(-1.72, abs=0.01)}
    assert all(a["to_db"] == pytest.approx(-21.72, abs=0.06) for a in out["adjust"])
    assert all(a["limited"] is False for a in out["adjust"])


def test_no_voice_is_ever_asked_for_more_than_the_editor_can_set():
    out = M.speech_adjustments([item("a", -20.0), item("b", -20.0), item("quiet", -30.0)])
    assert out["target_db"] == pytest.approx(-27.72, abs=0.01)
    assert adj(out)["quiet"] == M.MAX_GAIN_DB and adj(out)["a"] == pytest.approx(-7.72, abs=0.01)
    for u in out["adjust"]:
        assert u["delta_db"] <= M.MAX_GAIN_DB + 1e-9, "a clip that has no gain yet cannot gain more than the ceiling"


def test_the_gain_already_set_counts_toward_where_a_clip_sits_now():
    out = M.speech_adjustments([item("a", -22.0, gain=2.0), item("b", -20.0, gain=0.0), item("c", -20.0, gain=0.0)])
    assert out["adjust"] == [] and out["target_db"] == -20.0, "a is boosted to the same level already"


def test_voices_within_tolerance_are_left_alone_and_the_target_can_be_chosen():
    assert M.speech_adjustments([item("a", -20.0), item("b", -21.0), item("c", -19.5)])["adjust"] == []
    out = M.speech_adjustments([item("a", -20.0), item("b", -21.0)], target_db=-23.0)
    assert out["target_db"] == -23.0 and adj(out) == {"a": -3.0, "b": -2.0}


def test_a_target_above_what_a_voice_can_reach_is_limited_and_says_so():
    out = M.speech_adjustments([item("a", -20.0), item("b", -21.0)], target_db=-16.0)
    assert adj(out) == {"a": M.MAX_GAIN_DB, "b": M.MAX_GAIN_DB} and all(a["limited"] for a in out["adjust"])


def test_one_pass_never_cuts_or_boosts_beyond_the_limits_and_says_so():
    out = M.speech_adjustments([item("quiet", -50.0), item("normal", -20.0), item("normal2", -20.0)], target_db=-20.0)
    a = out["adjust"][0]
    assert a["id"] == "quiet" and a["delta_db"] == M.MAX_GAIN_DB and a["limited"] is True
    loud = M.speech_adjustments([item("loud", 0.0), item("a", -30.0), item("b", -30.0)])
    assert adj(loud)["loud"] == M.MIN_GAIN_DB


def test_a_clip_already_at_its_limit_is_reported_not_adjusted():
    out = M.speech_adjustments([item("quiet", -50.0, gain=M.MAX_GAIN_DB), item("a", -20.0), item("b", -20.0)], target_db=-20.0)
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


def test_the_master_gain_stops_at_the_volume_ceiling_and_says_what_to_do():
    out = M.master_correction(-22.4, -14.0, -6.4, headroom_db=2.28)
    assert out["delta_db"] == pytest.approx(2.28, abs=0.01) and out["volume_ceiling"] is True
    assert "130%" in out["peak_warning"] and "louder source" in out["peak_warning"]
    assert "voice_compressor" in out["peak_warning"] and "render_mix" in out["peak_warning"] and "not been measured" in out["peak_warning"]
    maxed = M.master_correction(-22.4, -14.0, -6.4, headroom_db=0.0)
    assert maxed["delta_db"] == 0.0 and maxed["volume_ceiling"] is True and "maximum volume" in maxed["reason"]
    roomy = M.master_correction(-20.0, -14.0, -10.0, headroom_db=6.0)
    assert roomy["delta_db"] == pytest.approx(6.0, abs=0.01) and roomy["volume_ceiling"] is False and roomy["peak_warning"] is None
    lowering = M.master_correction(-8.0, -14.0, 0.0, headroom_db=0.0)
    assert lowering["delta_db"] == -6.0, "the ceiling never stops a cut"


def test_a_silent_mix_has_nothing_to_correct():
    out = M.master_correction(None, -14.0)
    assert out["delta_db"] == 0.0 and "silent" in out["reason"]


# ============================ how deep to duck ============================
@pytest.mark.parametrize("speech,bed,expected", [([-20.0], -22.0, -8.0), ([-26.0], -20.0, -16.0), ([-20.0, -26.0], -20.0, -16.0), ([-20.0], -40.0, None)])
def test_the_duck_puts_the_bed_a_margin_under_the_quietest_voice(speech, bed, expected):
    out = M.duck_depth(speech, bed)
    assert out["duck_db"] == expected
    if expected is not None:
        assert (min(speech) - (bed + expected)) == pytest.approx(M.DUCK_MARGIN_DB, abs=0.11), "voice minus bed is exactly the margin afterwards"


def test_a_bed_that_is_already_far_enough_under_is_not_ducked():
    out = M.duck_depth([-20.0], -33.0)
    assert out["duck_db"] is None and out["why"] == "already far enough under the voice" and out["needed"] == 3.0


def test_the_duck_is_limited_by_the_floor_and_says_so():
    out = M.duck_depth([-40.0], -10.0)             # would need -40 dB
    assert out["duck_db"] == M.DUCK_FLOOR_DB and out["limited"] is True and out["needed"] == -40.0


def test_a_tiny_duck_is_not_worth_keyframing_but_a_small_one_is_raised_to_the_ceiling():
    assert M.duck_depth([-20.0], -29.7)["duck_db"] is None, "needs only 0.3 dB"
    assert M.duck_depth([-20.0], -28.5)["duck_db"] == M.DUCK_CEILING_DB, "needs 1.5 dB: a duck is either worth keyframing (3 dB) or not at all"
    assert M.duck_depth([-20.0], -26.0)["duck_db"] == -4.0, "deeper needs are used as they are"


def test_unknown_levels_leave_the_decision_to_the_caller():
    assert M.duck_depth([], -20.0)["duck_db"] is None and M.duck_depth([-20.0], None)["why"] == "levels unknown"
    assert M.duck_depth([None, -20.0], -22.0)["duck_db"] == -8.0, "voices without a level are ignored"


def test_the_margin_can_be_chosen():
    assert M.duck_depth([-20.0], -22.0, margin_db=6.0)["duck_db"] == -4.0
