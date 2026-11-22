"""The edit contract in the one executor every agent shares: what it holds back, warns about, observes and never breaks.

The real ``execute_tool_rich`` runs with fake tool handlers and a fake project, so every claim here is about the dispatcher
itself (gate before any undo group, observation after the call), not about one editor tool.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import edit_contract as EC  # noqa: E402
from classes.agent_tools import execute as X  # noqa: E402
from classes.editor_tools import media_index_tools_edit as TE  # noqa: E402

PLACE = "add_clips_to_timeline_tool"
BRIEF_OK = {"form": "vlog", "target_seconds": 120}


def review_text(needs, fix=("balance_mix_tool",)):
    layers = {"sound": [{"id": n, "layer": "sound", "status": "needs", "fix": list(fix)} for n in needs]} if needs else {}
    return "Review.\n" + json.dumps({"needs": list(needs), "unknown": [], "layers": layers, "fix_with": "Tools named in fix run as run_tool(...)"})


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv("ZENVI_EDIT_CONTRACT", raising=False)
    store = {"clips": [], "layers": [{"number": 1000000, "id": "t1"}], "markers": [], TE.BRIEF_KEY: {}, EC.CONTRACT_KEY: {}}
    reads = []
    writes = []

    class Project:
        def get(self, key, default=None):
            reads.append(key)
            return store.get(key, default)

    class Updates:
        actionHistory: list = []

        def update_untracked(self, key, values):
            writes.append((tuple(key), json.loads(json.dumps(values))))
            store.setdefault(key[0], {}).update(values)

    app = SimpleNamespace(project=Project(), updates=Updates(), thread=lambda: None)
    calls = []
    state = SimpleNamespace(store=store, reads=reads, writes=writes, calls=calls, app=app, review="Nothing needs work.\n{\"needs\":[],\"unknown\":[]}",
                            result={})

    def make(name):
        def handler(**kw):
            calls.append((name, kw))
            if name in state.result:
                return state.result[name]
            if name == "review_edit_tool":
                return state.review
            return f"{name} done."
        return handler

    names = [PLACE, "add_clip_to_timeline_tool", "trim_clips_tool", "color_grade_clip_tool", "review_edit_tool", "export_video_tool",
             "get_project_overview_tool", "undo_tool", "add_captions_tool"]
    monkeypatch.setattr(X, "_HANDLERS", {n: make(n) for n in names})
    monkeypatch.setattr(X, "_READ_ONLY", frozenset({"get_project_overview_tool", "review_edit_tool"}))
    monkeypatch.setattr(X, "_BACKGROUND_SAFE", frozenset())
    monkeypatch.setattr(X, "_UNGROUPED", frozenset({"get_project_overview_tool", "review_edit_tool", "undo_tool"}))
    monkeypatch.setattr(X, "_GET_APP", lambda: app)
    monkeypatch.setattr(X, "_RUN_ON_MAIN", lambda fn, timeout=None: fn())
    monkeypatch.setattr(X, "_ATOMIC", lambda a, h: h)
    monkeypatch.setattr(X, "_QTHREAD", None)
    monkeypatch.setattr("classes.agent_tools.schema.validate_args", lambda tool, args: None)
    monkeypatch.setattr(TE, "get_app", lambda: app)
    monkeypatch.setattr(TE, "on_main", lambda func, *a, **k: func(*a))
    return state


def run(name, **args):
    return X.execute_tool_rich(name, args).receipt


def set_contract(world, mode, phase, **extra):
    c = EC.new_contract(mode)
    c["phase"] = phase
    c.update(extra)
    world.store[EC.CONTRACT_KEY] = c


def history(world):
    return len(world.app.updates.actionHistory)


# ------------------------------------------------------------------ the gate
def test_enforce_holds_back_placement_before_a_brief_and_leaves_history_untouched(world, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, "idle")
    r = run(PLACE, items=[])
    assert r.status == "refused" and "begin_edit_tool" in r.summary and r.undoSteps == 0
    assert world.calls == [], "the handler never ran"
    assert history(world) == 0 and world.writes == []


def test_the_default_is_warn_the_call_runs_and_the_receipt_says_what_enforce_would_do(world):
    set_contract(world, EC.MODE_FINISHED, "idle")
    r = run(PLACE, items=[])
    assert r.status != "refused" and [c[0] for c in world.calls] == [PLACE]
    assert any("begin_edit_tool" in w for w in r.warnings)


def test_off_changes_nothing_and_never_reads_the_project(world, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "off")
    set_contract(world, EC.MODE_FINISHED, "idle")
    world.reads.clear()
    r = run(PLACE, items=[])
    assert r.warnings == [] and [c[0] for c in world.calls] == [PLACE]
    assert EC.CONTRACT_KEY not in world.reads and TE.BRIEF_KEY not in world.reads


@pytest.mark.parametrize("flag", ["off", "warn", "enforce"])
def test_a_free_edit_gets_byte_identical_receipts_in_every_mode(world, monkeypatch, flag):
    baseline = {}
    for name in (PLACE, "trim_clips_tool", "export_video_tool"):
        monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "off")
        baseline[name] = X.execute_tool_rich(name, {}).receipt.to_json()
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", flag)
    for name, expected in baseline.items():
        assert X.execute_tool_rich(name, {}).receipt.to_json() == expected


@pytest.mark.parametrize("phase", [p for p in EC.PHASES if p not in EC.PRE_BRIEF_PHASES])
def test_placement_after_the_brief_is_never_held_back(world, monkeypatch, phase):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, phase)
    r = run(PLACE, items=[])
    assert r.status != "refused" and not any("edit contract" in w for w in r.warnings)


def test_a_brief_saved_with_set_edit_brief_tool_unlocks_it(world, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, "idle")
    assert run(PLACE, items=[]).status == "refused"
    world.store[TE.BRIEF_KEY] = dict(BRIEF_OK)
    assert run(PLACE, items=[]).status != "refused"


@pytest.mark.parametrize("mode", [EC.MODE_FREE, EC.MODE_RECREATE])
@pytest.mark.parametrize("name", [PLACE, "add_clip_to_timeline_tool", "trim_clips_tool", "export_video_tool", "undo_tool"])
def test_only_a_finished_piece_is_ever_held_back(world, monkeypatch, mode, name):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, mode, "idle")
    assert run(name).status != "refused"


@pytest.mark.parametrize("name", ["trim_clips_tool", "color_grade_clip_tool", "export_video_tool", "get_project_overview_tool", "undo_tool"])
def test_every_other_tool_is_never_held_back_even_in_an_unbriefed_finished_piece(world, monkeypatch, name):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, "idle")
    assert run(name).status != "refused"


def test_tools_the_contract_does_not_watch_never_read_the_project_contract(world, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, "idle")
    world.reads.clear()
    for name in ("trim_clips_tool", "get_project_overview_tool", "undo_tool"):
        run(name)
    assert EC.CONTRACT_KEY not in world.reads


def test_a_bug_in_the_gate_never_stops_an_edit(world, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, "idle")
    monkeypatch.setattr(TE, "read_contract", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    r = run(PLACE, items=[])
    assert r.status != "refused" and [c[0] for c in world.calls] == [PLACE]


def test_a_garbage_stored_contract_is_a_free_edit(world, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    for junk in ("x", 5, {"version": 99, "mode": "finished_piece"}, [1, 2]):
        world.store[EC.CONTRACT_KEY] = junk
        assert run(PLACE, items=[]).status != "refused"


# ------------------------------------------------------------------ observation
def test_a_placement_that_worked_moves_the_piece_to_assembling(world):
    set_contract(world, EC.MODE_FINISHED, "briefed")
    run(PLACE, items=[])
    assert world.store[EC.CONTRACT_KEY]["phase"] == "assembling"
    assert all(w[0] == (EC.CONTRACT_KEY,) for w in world.writes), "only untracked contract writes"
    assert history(world) == 0


def test_a_placement_that_failed_or_was_refused_does_not_advance(world):
    set_contract(world, EC.MODE_FINISHED, "briefed")
    world.result[PLACE] = "Error: the track is locked."
    run(PLACE, items=[])
    assert world.store[EC.CONTRACT_KEY]["phase"] == "briefed" and world.writes == []


def test_a_finishing_tool_moves_an_assembled_piece_to_finishing(world):
    set_contract(world, EC.MODE_FINISHED, "assembling")
    run("color_grade_clip_tool")
    assert world.store[EC.CONTRACT_KEY]["phase"] == "finishing"


def test_the_audit_is_recorded_with_the_tools_the_findings_name(world):
    set_contract(world, EC.MODE_FINISHED, "finishing")
    world.review = review_text(["music_too_loud"])
    run("review_edit_tool")
    c = world.store[EC.CONTRACT_KEY]
    assert c["phase"] == "audited" and c["audit"]["needs"] == ["music_too_loud"] and c["audit"]["rounds"] == 1
    assert c["audit"]["fix_with"] == ["balance_mix_tool"], "the findings' tools, not the generic how-to-run sentence"


def test_a_clean_audit_finishes_the_piece_and_the_cap_ends_the_loop(world):
    set_contract(world, EC.MODE_FINISHED, "finishing")
    world.review = review_text([])
    run("review_edit_tool")
    assert world.store[EC.CONTRACT_KEY]["phase"] == "done"
    set_contract(world, EC.MODE_FINISHED, "finishing")
    world.review = review_text(["still_dark"])
    for _ in range(EC.MAX_AUDIT_ROUNDS):
        run("review_edit_tool")
    c = world.store[EC.CONTRACT_KEY]
    assert c["phase"] == "done" and c["audit"]["open_needs"] == ["still_dark"]


def test_a_free_edit_is_never_observed(world):
    run(PLACE, items=[])
    run("review_edit_tool")
    assert world.writes == [] and world.store[EC.CONTRACT_KEY] == {}


def test_off_does_not_observe_either(world, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "off")
    set_contract(world, EC.MODE_FINISHED, "briefed")
    run(PLACE, items=[])
    assert world.store[EC.CONTRACT_KEY]["phase"] == "briefed"


def test_an_error_while_observing_never_changes_the_tools_own_result(world, monkeypatch):
    set_contract(world, EC.MODE_FINISHED, "briefed")
    before = run(PLACE, items=[]).to_json()
    set_contract(world, EC.MODE_FINISHED, "briefed")
    monkeypatch.setattr(TE, "write_contract", lambda c: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert run(PLACE, items=[]).to_json() == before


# ------------------------------------------------------------------ export is never refused
@pytest.mark.parametrize("flag", ["warn", "enforce"])
def test_export_is_never_refused_but_an_unaudited_piece_is_warned(world, monkeypatch, flag):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", flag)
    set_contract(world, EC.MODE_FINISHED, "assembling")
    r = run("export_video_tool")
    assert r.status == "applied" and any("not audited" in w for w in r.warnings)


def test_export_after_a_clean_audit_has_no_warning_and_open_needs_are_named(world):
    set_contract(world, EC.MODE_FINISHED, "finishing")
    world.review = review_text([])
    run("review_edit_tool")
    assert run("export_video_tool").warnings == []
    set_contract(world, EC.MODE_FINISHED, "finishing")
    world.review = review_text(["dead_air"])
    run("review_edit_tool")
    assert any("dead_air" in w for w in run("export_video_tool").warnings)


def test_export_of_a_free_edit_has_no_contract_warning(world):
    assert run("export_video_tool").warnings == []


# ------------------------------------------------------------------ parallel calls (the Assistant fans out up to 4 tool calls)
def test_four_concurrent_placements_leave_a_consistent_contract(world):
    set_contract(world, EC.MODE_FINISHED, "briefed")
    errors = []

    def go():
        try:
            for _ in range(20):
                run(PLACE, items=[])
        except Exception as e:                       # pragma: no cover - the assertion below reports it
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and world.store[EC.CONTRACT_KEY]["phase"] == "assembling"
    assert len([c for c in world.calls if c[0] == PLACE]) == 80
