"""The relative taste check: a much stronger moment exists in the footage and the edit does not use it.

The absolute hook check (is the opening weak?) already lives in review.story_findings. These two findings compare the edit
with what the project *has*, so they need the project's best moments; with none given they say nothing (no false alarms).
They are notes (status 'info'), never 'needs': taste hints must not start credit-burning audit loops.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from test_media_index_review import FakeIndex, by_id, clip, provider, run, shot  # noqa: E402

BEST = {"file_id": "BEST", "name": "sunset.mp4", "start": 10.0, "end": 16.0, "highlight": 0.95}
OK2 = {"file_id": "F2", "name": "tram.mp4", "start": 0.0, "end": 5.0, "highlight": 0.85}


def opening_index(highlight):
    return provider(F1=FakeIndex([shot(0, 20, highlight=highlight, hook=0.9)]))


def test_a_much_stronger_unused_moment_is_noted_against_a_weak_opening():
    f = by_id(run([clip("a", 0, 5)], index=opening_index(0.40), best_moments=[BEST]))["opening_best"]
    assert f["status"] == "info" and f["layer"] == "story"
    assert f["evidence"]["opening_highlight"] == 0.4 and f["evidence"]["best"]["file_id"] == "BEST"
    assert "sunset.mp4" in f["summary"] and f["fix"] == ["search_footage_tool", "add_clips_to_timeline_tool"]


def test_it_is_never_a_need_so_it_cannot_start_an_audit_loop():
    result = run([clip("a", 0, 5)], index=opening_index(0.05), best_moments=[BEST])
    assert by_id(result)["opening_best"]["status"] == "info"
    assert "opening_best" not in result["needs"]


def test_a_strong_opening_is_ok():
    assert by_id(run([clip("a", 0, 5)], index=opening_index(0.92), best_moments=[BEST]))["opening_best"]["status"] == "ok"


def test_an_opening_close_to_the_best_is_ok():
    assert by_id(run([clip("a", 0, 5)], index=opening_index(0.80), best_moments=[BEST]))["opening_best"]["status"] == "ok"


def test_the_best_moment_already_on_the_timeline_is_not_unused():
    used = [clip("a", 0, 5), clip("b", 5, 11, file="BEST", src_in=10.0)]
    assert by_id(run(used, index=opening_index(0.40), best_moments=[BEST]))["opening_best"]["status"] == "ok"


def test_a_same_file_clip_from_another_part_does_not_count_as_using_the_moment():
    other_part = [clip("a", 0, 5), clip("b", 5, 8, file="BEST", src_in=40.0)]
    assert by_id(run(other_part, index=opening_index(0.40), best_moments=[BEST]))["opening_best"]["status"] == "info"


def test_only_half_overlap_counts_as_used():
    half = [clip("a", 0, 5), clip("b", 5, 8, file="BEST", src_in=10.0)]                 # uses 3 of the moment's 6 seconds
    assert by_id(run(half, index=opening_index(0.40), best_moments=[BEST]))["opening_best"]["status"] == "ok"
    little = [clip("a", 0, 5), clip("b", 5, 7, file="BEST", src_in=10.0)]               # 2 of 6
    assert by_id(run(little, index=opening_index(0.40), best_moments=[BEST]))["opening_best"]["status"] == "info"


@pytest.mark.parametrize("best", [None, [], ()])
def test_no_best_moments_means_no_finding_and_the_audit_is_unchanged(best):
    kw = {} if best is None else {"best_moments": best}
    result = run([clip("a", 0, 5)], index=opening_index(0.40), **kw)
    assert "opening_best" not in by_id(result) and "best_unused" not in by_id(result)


def test_an_unindexed_opening_says_nothing_extra():
    assert "opening_best" not in by_id(run([clip("a", 0, 5)], best_moments=[BEST]))               # the hook finding already says 'unknown'


def test_weak_best_moments_are_not_worth_noting():
    weak = dict(BEST, highlight=0.55)
    assert "opening_best" not in by_id(run([clip("a", 0, 5)], index=opening_index(0.10), best_moments=[weak]))


def test_most_of_the_best_moments_missing_from_the_edit_is_noted():
    moments = [dict(BEST, file_id=f"B{i}", start=0.0, end=4.0, highlight=0.9 - i * 0.01) for i in range(5)]
    clips = [clip("a", 0, 40)]
    f = by_id(run(clips, index=opening_index(0.9), best_moments=moments))["best_unused"]
    assert f["status"] == "info" and f["evidence"]["unused"] == 5 and f["evidence"]["of"] == 5


def test_using_most_of_the_best_moments_is_ok():
    moments = [dict(BEST, file_id=f"B{i}", start=0.0, end=4.0, highlight=0.9) for i in range(5)]
    clips = [clip("a", 0, 40)] + [clip(f"u{i}", 40 + 4 * i, 44 + 4 * i, file=f"B{i}", src_in=0.0) for i in range(4)]
    assert by_id(run(clips, index=opening_index(0.9), best_moments=moments))["best_unused"]["status"] == "ok"


def test_a_short_edit_is_not_judged_on_unused_moments():
    moments = [dict(BEST, file_id=f"B{i}", start=0.0, end=4.0, highlight=0.9) for i in range(5)]
    assert "best_unused" not in by_id(run([clip("a", 0, 12)], index=opening_index(0.9), best_moments=moments))


def test_the_existing_craft_findings_are_untouched_by_the_new_argument():
    clips = [clip("a", 0, 5), clip("b", 5, 12)]
    before = run(clips, index=opening_index(0.4))
    after = run(clips, index=opening_index(0.4), best_moments=[BEST, OK2])
    for key in before["layers"]:
        base = {f["id"]: f for f in before["layers"][key]}
        for f in after["layers"][key]:
            if f["id"] in base:
                assert f == base[f["id"]]
    assert before["needs"] == after["needs"] and before["unknown"] == after["unknown"]
    assert {f["id"] for layer in after["layers"].values() for f in layer} - {f["id"] for layer in before["layers"].values() for f in layer} \
        == {"opening_best"}, "a 12 s edit gets the opening note only; unused-moment counts need a longer edit"


# ------------------------------------------------------------------ the tool wiring
def fake_index(sha, file_id, name, shots):
    from types import SimpleNamespace
    return SimpleNamespace(sha=sha, file_id=file_id, name=name, media_type="video", shots=shots)


def test_best_moments_for_ranks_the_whole_project_and_counts_duplicate_imports_once():
    from classes.editor_tools.media_index_tools_review import best_moments_for
    a = fake_index("S1", "f1", "a.mp4", [shot(0, 5, highlight=0.9), shot(5, 9, highlight=0.3)])
    dup = fake_index("S1", "f1b", "a-again.mp4", [shot(0, 5, highlight=0.9)])
    b = fake_index("S2", "f2", "b.mp4", [shot(0, 4, highlight=0.95), shot(4, 8, highlight=0.2, flags=["blurry"])])
    best = best_moments_for([a, dup, b], n=3)
    assert [(m["file_id"], m["highlight"]) for m in best] == [("f2", 0.95), ("f1", 0.9), ("f1", 0.3)]


def test_black_and_unusable_shots_are_never_best_moments():
    from classes.editor_tools.media_index_tools_review import best_moments_for
    idx = fake_index("S1", "f1", "a.mp4", [dict(shot(0, 5, highlight=0.99), black=True),
                                           {"start": 5, "end": 9, "quality": {"highlight": 0.98, "inferred": {"usable": False}}},
                                           shot(9, 12, highlight=0.6)])
    assert [m["highlight"] for m in best_moments_for([idx])] == [0.6]


def test_a_moment_carries_what_the_survey_has_always_shown():
    """The survey's top_moments come from the same function now (its own tests in test_media_index_tools_review guard the survey)."""
    from classes.editor_tools.media_index_tools_review import _moments
    idx = fake_index("S1", "f1", "a.mp4", [dict(shot(0, 5, highlight=0.4), watch={"description": "a tram"}),
                                           dict(shot(5, 9, highlight=0.9, flags=["shaky"]), quality={"highlight": 0.9, "flags": ["shaky"],
                                                                                                   "inferred": {"highlight_reason": "golden hour"}})])
    got = _moments([idx])
    assert [m["highlight"] for m in got] == [0.9, 0.4]
    assert got[0] == {"file_id": "f1", "name": "a.mp4", "start": 5, "end": 9, "highlight": 0.9, "why": "golden hour", "flags": ["shaky"]}
    assert got[1]["why"] == "a tram" and got[1]["flags"] == []


def test_an_ordinary_audit_pays_nothing_and_a_finished_piece_gets_the_check(monkeypatch):
    from classes import edit_contract as EC
    from classes.editor_tools import media_index_tools_edit as TE, media_index_tools_review as RV
    calls = []
    idx = fake_index("S1", "f1", "a.mp4", [shot(0, 5, highlight=0.9)])
    monkeypatch.setattr(RV, "project_indexes", lambda *a, **k: calls.append(1) or ([idx], []))
    monkeypatch.setattr(TE, "read_contract", lambda: EC.new_contract(EC.MODE_FREE))
    assert RV._best_moments_if_a_finished_piece() == [] and calls == [], "a narrow edit never loads every file's index"
    monkeypatch.setattr(TE, "read_contract", lambda: EC.new_contract(EC.MODE_RECREATE))
    assert RV._best_moments_if_a_finished_piece() == [] and calls == []
    monkeypatch.setattr(TE, "read_contract", lambda: EC.new_contract(EC.MODE_FINISHED))
    assert [m["file_id"] for m in RV._best_moments_if_a_finished_piece()] == ["f1"] and calls == [1]


def test_a_failure_while_gathering_moments_never_fails_the_audit(monkeypatch):
    from classes import edit_contract as EC
    from classes.editor_tools import media_index_tools_edit as TE, media_index_tools_review as RV
    monkeypatch.setattr(TE, "read_contract", lambda: EC.new_contract(EC.MODE_FINISHED))
    monkeypatch.setattr(RV, "project_indexes", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert RV._best_moments_if_a_finished_piece() == []
    monkeypatch.setattr(TE, "read_contract", lambda: (_ for _ in ()).throw(RuntimeError("no project")))
    assert RV._best_moments_if_a_finished_piece() == []
