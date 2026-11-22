"""Acceptance rules for the offline edit-contract eval (tests/eval/contract_eval.py).

These pin the MECHANISM, not a real model's behaviour: with the gate on, a scripted agent that follows a refusal ends up
survey-first, briefed and opening on a strong moment; with it off nothing changes; narrow asks are never touched; nothing is
refused outside `enforce`. A live comparison on real models is opt-in (tests/eval/README_contract.md).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eval import contract_eval as C  # noqa: E402


@pytest.fixture(scope="module")
def result():
    t0 = time.time()
    table, narrow = C.evaluate(seeds=range(2))
    return SimpleResult(table, narrow, time.time() - t0)


class SimpleResult:
    def __init__(self, table, narrow, seconds):
        self.t, self.narrow, self.seconds = table, narrow, seconds


def test_it_is_fast_and_deterministic(result):
    assert result.seconds < 60
    again, narrow = C.evaluate(seeds=range(2))
    assert again == result.t and narrow == result.narrow


def test_narrow_asks_are_never_refused_or_warned_in_any_mode(result):
    for rollout, m in result.narrow.items():
        assert m["refused_total"] == 0 and m["warned_total"] == 0, rollout
        assert m["n"] > 0


def test_nothing_is_ever_refused_outside_enforce(result):
    for (agent, rollout), m in result.t.items():
        if rollout in ("off", "warn"):
            assert m["refused_per_run"] == 0, (agent, rollout)


def test_with_the_gate_off_behaviour_is_todays(result):
    """An eager agent never surveys and never has a brief; the contract changes nothing for it."""
    for agent in ("eager", "recovering"):
        m = result.t[(agent, "off")]
        assert m["survey_first_rate"] == 0 and m["brief_at_first_placement_rate"] == 0 and m["placed_rate"] == 1 and m["warned_per_run"] == 0


def test_warn_lets_everything_run_and_says_what_enforce_would_do(result):
    m = result.t[("eager", "warn")]
    assert m["placed_rate"] == 1 and m["warned_per_run"] >= 1 and m["refused_per_run"] == 0


def test_enforce_turns_an_eager_trajectory_into_a_survey_first_briefed_one(result):
    m = result.t[("recovering", "enforce")]
    assert m["survey_first_rate"] == 1.0 and m["brief_at_first_placement_rate"] == 1.0 and m["placed_rate"] == 1.0
    assert m["refused_per_run"] >= 1, "it only got there because the first placement was held back"


def test_an_agent_that_ignores_the_refusal_places_nothing_rather_than_cutting_blind(result):
    m = result.t[("eager", "enforce")]
    assert m["placed_rate"] == 0 and m["refused_per_run"] >= 1 and m["survey_first_rate"] == 0


def test_the_survey_first_edit_opens_stronger_and_uses_more_of_the_best_moments(result):
    off, on = result.t[("recovering", "off")], result.t[("recovering", "enforce")]
    assert on["mean_opening_rank"] is not None and off["mean_opening_rank"] is not None
    assert on["mean_opening_rank"] < off["mean_opening_rank"]
    assert on["top5_used_mean"] > off["top5_used_mean"]


def test_enforce_is_never_worse_than_off_on_any_process_metric(result):
    for agent in ("recovering", "careful"):
        off, on = result.t[(agent, "off")], result.t[(agent, "enforce")]
        assert on["survey_first_rate"] >= off["survey_first_rate"]
        assert on["brief_at_first_placement_rate"] >= off["brief_at_first_placement_rate"]
        assert on["top5_used_mean"] >= off["top5_used_mean"]
        assert on["mean_opening_rank"] <= off["mean_opening_rank"]


def test_the_survey_digest_stays_inside_its_cap(result):
    assert max(m["digest_chars_max"] for m in result.t.values()) <= C.EC.DIGEST_CAP_CHARS


def test_every_whole_piece_prompt_is_really_classified_as_a_whole_piece_and_every_narrow_one_is_not():
    for p in C.WHOLE_PROMPTS:
        assert C.EC.classify_intent(p, timeline_empty=True, media_minutes=40) == C.EC.MODE_FINISHED, p
    for p in C.NARROW_PROMPTS:
        assert C.EC.classify_intent(p, timeline_empty=True, media_minutes=40) == C.EC.MODE_FREE, p


def test_the_synthetic_footage_hides_its_best_moments_from_a_bin_order_agent():
    project = C.make_project(1)
    best = C.moments_of(project)
    first_two = {f.file_id for f in project[:2]}
    assert all(m["file_id"] not in first_two for m in best[:4]), "the strong moments are scattered, as in real footage"
    assert best[0]["highlight"] >= 0.82
