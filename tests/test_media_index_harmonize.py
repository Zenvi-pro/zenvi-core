"""Choosing the reference look and the clips that stray from it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import color_agent as ca  # noqa: E402
from classes.media_index import harmonize as H  # noqa: E402


def look(luma=0.5, warm=0.0, sat=0.3, contrast=0.5):
    return {"present": True, "avg_luma": luma, "warm_cool": warm, "green_magenta": 0.0, "sat_proxy": sat, "contrast_span": contrast,
            "clipped_highlights": 0.0, "clipped_shadows": 0.0, "channel_means": {"red": luma, "green": luma, "blue": luma}}


def d(a, b):
    return ca.look_profile_distance(a, b)


def test_the_real_distance_separates_the_same_look_from_a_different_one():
    assert d(look(), look()) == pytest.approx(0.0, abs=1e-6)
    assert d(look(0.5, 0.0), look(0.52, 0.01)) < H.DEFAULT_TOLERANCE, "a slightly different take is within tolerance"
    assert d(look(0.5, 0.0), look(0.25, -0.2)) > H.DEFAULT_TOLERANCE, "a dark cool clip is clearly a different look"


def test_the_reference_is_the_clip_nearest_to_all_the_others_not_an_outlier():
    profiles = {"a": look(0.50), "b": look(0.52), "c": look(0.48), "odd": look(0.10, -0.3)}
    assert H.pick_reference(profiles, list(profiles), d) in ("a", "b", "c")
    assert H.pick_reference(profiles, list(profiles), d) == "a", "ties go to the earliest in the timeline"
    assert H.pick_reference({"x": look(0.1), "y": look(0.5), "z": look(0.9)}, ["x", "y", "z"], d) == "y"


def test_a_named_clip_can_be_the_reference_but_must_have_a_look():
    profiles = {"a": look(), "b": look(0.8)}
    assert H.pick_reference(profiles, ["a", "b"], d, "b") == "b"
    with pytest.raises(ValueError, match="no measured look"):
        H.pick_reference(profiles, ["a", "b"], d, "ghost")
    with pytest.raises(ValueError, match="no clip has"):
        H.pick_reference({}, [], d)
    assert H.pick_reference({"only": look()}, ["only"], d) == "only"


def test_only_the_clips_beyond_tolerance_are_matched_worst_first():
    profiles = {"a": look(0.50), "b": look(0.51), "c": look(0.50), "dark": look(0.20), "bright": look(0.85)}
    plan = H.plan_harmonize(profiles, list(profiles), d)
    assert plan["reference"] in ("a", "b", "c")
    assert plan["to_match"] == ["bright", "dark"], "the clip furthest from the reference (0.35 away, against 0.30) comes first"
    assert plan["distances"]["bright"] > plan["distances"]["dark"] > H.DEFAULT_TOLERANCE
    assert set(plan["within_tolerance"]) == {"a", "b", "c"} - {plan["reference"]}
    assert plan["reference"] not in plan["distances"] and plan["tolerance"] == H.DEFAULT_TOLERANCE


def test_a_consistent_group_needs_no_matching():
    profiles = {f"c{i}": look(0.50 + i * 0.005, 0.01) for i in range(5)}
    plan = H.plan_harmonize(profiles, list(profiles), d)
    assert plan["to_match"] == [] and plan["left_out"] == []


def test_the_tolerance_decides_who_strays():
    profiles = {"a": look(0.50), "b": look(0.58), "c": look(0.50)}
    assert H.plan_harmonize(profiles, list(profiles), d, tolerance=0.30)["to_match"] == []
    assert H.plan_harmonize(profiles, list(profiles), d, tolerance=0.02)["to_match"] == ["b"]


def test_more_strays_than_the_limit_are_split_between_now_and_later():
    profiles = {"ref1": look(0.5), "ref2": look(0.5), "ref3": look(0.5), **{f"s{i}": look(0.5 + 0.06 * i, 0.2) for i in range(1, 6)}}
    plan = H.plan_harmonize(profiles, list(profiles), d, reference="ref1", max_clips=2, tolerance=0.10)
    assert len(plan["to_match"]) == 2 and len(plan["left_out"]) == 3
    assert plan["distances"][plan["to_match"][0]] >= plan["distances"][plan["left_out"][0]], "the worst are matched first"


def test_clips_with_no_measured_look_are_listed_not_guessed():
    profiles = {"a": look(), "b": look(0.9)}
    plan = H.plan_harmonize(profiles, ["a", "b", "unmeasured1", "unmeasured2"], d, reference="a")
    assert plan["unmeasured"] == ["unmeasured1", "unmeasured2"] and plan["to_match"] == ["b"]
    assert H.plan_harmonize(profiles, ["a", "b"], lambda x, y: None, reference="a")["unmeasured"] == ["b"]
