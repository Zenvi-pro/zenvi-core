"""The eval's scoring functions."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from eval import metrics as M  # noqa: E402


def test_each_true_time_takes_the_nearest_unused_found_time_inside_the_tolerance():
    pairs, extra, missed = M.match_times([1.02, 1.05, 4.0], [1.0, 2.5], 0.1)
    assert pairs == [(1.02, 1.0)] and extra == [1.05, 4.0] and missed == [2.5]


def test_one_found_time_cannot_serve_two_true_times():
    pairs, extra, missed = M.match_times([1.0], [0.95, 1.05], 0.1)
    assert len(pairs) == 1 and len(missed) == 1 and extra == []


def test_prf_is_perfect_when_everything_matches_and_counts_both_kinds_of_error():
    perfect = M.prf([1.0, 2.0], [1.01, 2.02], 0.1)
    assert perfect["f1"] == 1.0 and perfect["mean_error"] == pytest.approx(0.015, abs=1e-6) and perfect["extra"] == perfect["missed"] == 0
    r = M.prf([1.0, 9.0], [1.0, 5.0], 0.1)
    assert (r["precision"], r["recall"], r["extra"], r["missed"]) == (0.5, 0.5, 1, 1) and r["f1"] == 0.5


def test_nothing_to_find_and_nothing_found_is_a_perfect_score_but_a_false_alarm_is_not():
    assert M.prf([], [], 0.1)["f1"] == 0.0 or M.prf([], [], 0.1)["precision"] == 1.0
    assert M.prf([], [], 0.1)["precision"] == 1.0 and M.prf([], [], 0.1)["recall"] == 1.0
    assert M.prf([3.0], [], 0.1)["precision"] == 0.0 and M.prf([3.0], [], 0.1)["extra"] == 1
    assert M.prf([], [3.0], 0.1)["recall"] == 0.0


def test_pooling_counts_instead_of_averaging_percentages():
    a = M.prf([1.0, 2.0], [1.0, 2.0], 0.1)          # 2 of 2
    b = M.prf([5.0], [5.0, 6.0, 7.0], 0.1)          # 1 of 3
    pooled = M.combine(a, b)
    assert pooled["recall"] == pytest.approx(3 / 5) and pooled["precision"] == 1.0 and pooled["missed"] == 2 and pooled["truth"] == 5


def test_span_edge_errors_are_measured_against_the_overlapping_found_span():
    out = M.edge_errors([[1.1, 3.0], [4.6, 6.2]], [[1.0, 3.0], [4.5, 6.0], [9.0, 10.0]])
    assert out["start_mae"] == pytest.approx(0.1, abs=1e-6) and out["end_mae"] == pytest.approx(0.1, abs=1e-6)
    assert out["missed"] == 1 and out["truth"] == 3 and out["start_max"] == pytest.approx(0.1, abs=1e-6)


def test_precision_and_recall_at_k():
    ranked = ["a", "b", "c", "d"]
    assert M.precision_at_k(ranked, ["a", "c"], 2) == 0.5 and M.recall_at_k(ranked, ["a", "c"], 2) == 0.5
    assert M.recall_at_k(ranked, ["a", "c"], 4) == 1.0 and M.recall_at_k(ranked, [], 3) == 1.0


def test_a_flag_is_scored_by_precision_and_recall():
    out = M.confusion([True, True, False, False], [True, False, True, False])
    assert (out["tp"], out["fp"], out["fn"]) == (1, 1, 1) and out["precision"] == 0.5 and out["recall"] == 0.5
    assert M.confusion([False], [False]) == {"precision": 1.0, "recall": 1.0, "tp": 0, "fp": 0, "fn": 0}


def test_rank_agreement_is_the_share_of_correctly_ordered_pairs():
    assert M.rank_agreement([1, 2, 3], [0, 1, 2]) == 1.0
    assert M.rank_agreement([3, 2, 1], [0, 1, 2]) == 0.0
    assert M.rank_agreement([1, 3, 2], [0, 1, 2]) == pytest.approx(2 / 3, abs=1e-3)
    assert M.rank_agreement([1, 1], [0, 0]) == 1.0, "tied expectations are not counted"
