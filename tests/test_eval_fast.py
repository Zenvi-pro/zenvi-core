"""The fast eval subset: the index must keep measuring what the known-answer corpus says it should.

Builds a small synthetic corpus with ffmpeg (cached) and checks every metric in ``tests/eval/thresholds.json``.
A failure here means a real accuracy regression, not a flaky test: look at the numbers it prints.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eval import corpus  # noqa: E402


@pytest.fixture(scope="module")
def report():
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg for the eval corpus")
    from eval import suite
    return suite.run_fast()


def test_every_metric_is_within_its_limit(report):
    from eval.run_eval import load_limits, violations
    bad = violations(report, load_limits())
    assert not bad, "accuracy regression:\n  " + "\n  ".join(bad)


def test_a_limit_that_names_a_metric_that_does_not_exist_is_reported(report):
    from eval.run_eval import violations
    assert violations(report, {"shots.nonsense": {"min": 0.5}}) == ["shots.nonsense: not measured"]


def test_the_checker_flags_a_floor_and_a_ceiling():
    from eval.run_eval import violations
    rep = {"a": {"f1": 0.4, "err": 9.0}}
    out = violations(rep, {"a.f1": {"min": 0.9}, "a.err": {"max": 1.0}, "a.ok": {"min": 0.0}})
    assert out == ["a.f1: 0.4 is below the floor 0.9", "a.err: 9.0 is above the ceiling 1.0", "a.ok: not measured"]
