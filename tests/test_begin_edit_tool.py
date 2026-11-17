"""begin_edit_tool: survey + brief defaults in one call, and the contract it writes. No Qt, no real project."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import edit_contract as EC  # noqa: E402
from classes.editor_tools import REGISTRY, media_index_tools_edit as TE  # noqa: E402

SURVEY = {
    "totals": {"files": 12, "videos": 10, "audio": 2, "images": 0, "video_minutes": 48.0, "usable_minutes": 40.0, "not_indexed": 2},
    "trip": {"days": [{"day": 1, "date": "2026-07-01", "minutes": 20.0, "clips": 5, "places": ["Lisbon"]},
                      {"day": 2, "date": "2026-07-02", "minutes": 28.0, "clips": 5, "places": ["Sintra"]}], "places": []},
    "top_moments": [{"file_id": "f1", "name": "sunset.mp4", "start": 3.0, "end": 9.0, "highlight": 0.93, "why": "golden hour over the river"}],
    "music": [{"file_id": "m1", "name": "beat.mp3", "bpm": 96}],
    "orientation": {"landscape": 10},
    "not_indexed": [],
}


def video(structured=True, searchable=False, rich=False, minutes=5.0):
    return SimpleNamespace(media_type="video", shots=[{"start": 0.0, "end": 4.0}] if structured else [], rows=[{"v": 1}] if searchable else [],
                           vector_model="m" if searchable else "", duration=minutes * 60.0, sentences=[{"t": "hi"}] if rich else [],
                           look_file={"profile": {"present": True}} if rich else None, layers={})


def call(**kw):
    out = REGISTRY["begin_edit_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


@pytest.fixture
def project(monkeypatch):
    store = {TE.BRIEF_KEY: {}, EC.CONTRACT_KEY: {}, "clips": []}
    writes, other = [], []

    class Updates:
        actionHistory: list = []

        def update_untracked(self, key, values):
            writes.append((tuple(key), json.loads(json.dumps(values))))
            store.setdefault(key[0], {}).update(values)

        def update(self, *a, **k):                     # a tracked write would be an undo step: never allowed here
            other.append(("update", a))
            raise AssertionError("begin_edit_tool must only use update_untracked")

    app = SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: store.get(k, d)), updates=Updates())
    monkeypatch.setattr(TE, "get_app", lambda: app)
    monkeypatch.setattr(TE, "on_main", lambda func, *a, **k: func(*a))
    state = SimpleNamespace(store=store, writes=writes, other=other, files=[video() for _ in range(10)], missing=[], survey=dict(SURVEY))
    monkeypatch.setattr(TE, "project_indexes", lambda file_ids=None: (state.files, state.missing))
    monkeypatch.setattr(TE, "survey", lambda files, missing, **kw: state.survey)
    return state


def brief_writes(p):
    return [w for w in p.writes if w[0] == (TE.BRIEF_KEY,)]


def contract_writes(p):
    return [w for w in p.writes if w[0] == (EC.CONTRACT_KEY,)]


# ------------------------------------------------------------------ registration
def test_it_is_registered_core_and_runs_off_the_gui_thread():
    from classes.agent_tools.core_tools import CORE_TOOL_NAMES
    spec = REGISTRY["begin_edit_tool"]
    assert "begin_edit_tool" in CORE_TOOL_NAMES
    assert spec.background_safe and not spec.read_only         # it writes (untracked), and it reads disk: never on the GUI thread
    assert {"begin_edit_tool", "get_edit_brief_tool", "set_edit_brief_tool"} <= set(CORE_TOOL_NAMES)


# ------------------------------------------------------------------ the happy path
def test_a_vague_ask_gets_a_brief_a_contract_and_a_digest(project):
    head, r = call(user_ask="make something cool with my trip")
    assert head.startswith("Finished-piece brief ready")
    assert r["contract"]["mode"] == EC.MODE_FINISHED and r["contract"]["phase"] == "briefed"
    assert r["brief"]["form"] == "vlog" and r["brief"]["target_seconds"] > 0 and r["brief"]["opening"]["file_id"] == "f1"
    assert r["survey_digest"]["top_moments"][0]["file_id"] == "f1"
    assert set(r["next_tools"]) <= {"search_footage_tool", "add_clips_to_timeline_tool", "review_edit_tool", "get_timeline_state_tool"}
    assert project.store[TE.BRIEF_KEY]["form"] == "vlog" and project.store[EC.CONTRACT_KEY]["phase"] == "briefed"
    assert project.other == [], "no tracked write: begin_edit_tool is never an undo step"


def test_the_brief_it_writes_is_enough_to_unlock_placement(project):
    call(user_ask="make a vlog")
    stored_brief, stored_contract = project.store[TE.BRIEF_KEY], project.store[EC.CONTRACT_KEY]
    assert EC.brief_ready(stored_brief)
    assert EC.check_policy(stored_contract, "add_clips_to_timeline_tool", brief=stored_brief, rollout="enforce").action == "allow"


def test_a_stated_length_and_form_are_used(project):
    _h, r = call(user_ask="make a 2 minute reel of my trip")
    assert r["brief"]["form"] == "reel" and r["brief"]["target_seconds"] == 120 and r["brief"]["aspect"] == "9:16"


def test_explicit_arguments_beat_the_ask_and_the_stored_brief(project):
    project.store[TE.BRIEF_KEY].update({"form": "film", "target_seconds": 600})
    _h, r = call(user_ask="make a vlog", form="trailer", target_seconds=45, vibe="fast and loud", bans=["no stock"])
    assert (r["brief"]["form"], r["brief"]["target_seconds"], r["brief"]["vibe"], r["brief"]["bans"]) == ("trailer", 45.0, "fast and loud", ["no stock"])


# ------------------------------------------------------------------ never wipes what the user or agent already decided
def test_it_never_wipes_keys_already_in_the_brief(project):
    project.store[TE.BRIEF_KEY].update({"form": "film", "vibe": "moody", "music": "beat.mp3", "done": ["intro"], "notes": "keep the tram"})
    _h, r = call(user_ask="make a film")
    b = r["brief"]
    assert b["form"] == "film" and b["vibe"] == "moody" and b["music"] == "beat.mp3" and b["done"] == ["intro"] and b["notes"] == "keep the tram"
    assert b["target_seconds"] > 0, "missing keys are filled from the defaults"


def test_calling_it_twice_changes_nothing(project):
    call(user_ask="make a vlog")
    first = json.loads(json.dumps(project.store[TE.BRIEF_KEY]))
    call(user_ask="make a vlog")
    assert project.store[TE.BRIEF_KEY] == first


# ------------------------------------------------------------------ refusals and waiting
def test_an_empty_project_says_to_import_first_and_writes_nothing(project):
    project.files, project.missing = [], []
    out = REGISTRY["begin_edit_tool"].func(user_ask="make a vlog")
    assert out.startswith("Error") and "import" in out.lower() and project.writes == []


def test_nothing_indexed_yet_waits_and_commits_no_brief(project):
    project.files, project.missing = [], ["a.mov", "b.mov", "c.mov"]
    head, r = call(user_ask="make a vlog")
    assert r["wait"] is True and "wait_until_project_indexed_tool" in head
    assert r["contract"]["phase"] == "surveying" and r["contract"]["attempts"] == 1
    assert brief_writes(project) == [] and project.store[TE.BRIEF_KEY] == {}
    assert EC.check_policy(project.store[EC.CONTRACT_KEY], "add_clips_to_timeline_tool", rollout="enforce").action == "refuse"


def test_a_partial_index_waits_then_proceeds_on_the_structured_subset(project):
    project.files = [video(structured=True, minutes=1.0) for _ in range(3)] + [video(structured=False) for _ in range(7)]   # 30%, 3 min
    for attempt in range(1, EC.WAIT_ATTEMPTS_BEFORE_PARTIAL_OK):
        _h, r = call(user_ask="make a vlog")
        assert r["wait"] is True and r["contract"]["attempts"] == attempt
    _h, r = call(user_ask="make a vlog")
    assert r["contract"]["phase"] == "briefed" and r["survey_digest"]["readiness"]["tier"] == "partial" and "wait" not in r


def test_a_few_structured_videos_are_enough_when_they_cover_the_film(project):
    project.files = [video(structured=True, minutes=5.0) for _ in range(3)] + [video(structured=False) for _ in range(7)]   # 30%, but 15 min
    _h, r = call(user_ask="make a vlog")
    assert "wait" not in r and r["survey_digest"]["readiness"]["tier"] == "structure"


def test_enough_structure_does_not_wait(project):
    project.files = [video() for _ in range(7)] + [video(structured=False) for _ in range(3)]                        # 70%
    _h, r = call(user_ask="make a vlog")
    assert r["contract"]["phase"] == "briefed" and r["survey_digest"]["readiness"]["tier"] == "structure"


def test_the_readiness_it_reports_counts_the_layers(project):
    project.files = [video(searchable=True, rich=True) for _ in range(10)]
    _h, r = call(user_ask="make a vlog")
    info = r["survey_digest"]["readiness"]
    assert info["tier"] == "rich" and info["total"] == 10 and info["structured"] == 10 and info["searchable"] == 10


# ------------------------------------------------------------------ modes
def test_a_narrow_ask_is_free_and_writes_nothing(project):
    head, r = call(user_ask="make it warmer")
    assert r["contract"]["mode"] == EC.MODE_FREE and "no gate" in head.lower()
    assert project.writes == []


def test_an_explicit_call_with_no_ask_starts_a_finished_piece(project):
    _h, r = call()
    assert r["contract"]["mode"] == EC.MODE_FINISHED


def test_recreate_is_its_own_mode_and_is_never_gated(project):
    _h, r = call(user_ask="recreate this video from my footage")
    assert r["contract"]["mode"] == EC.MODE_RECREATE
    assert EC.check_policy(project.store[EC.CONTRACT_KEY], "add_clips_to_timeline_tool", rollout="enforce").action == "allow"


def test_asking_for_free_reopens_the_timeline(project):
    call(user_ask="make a vlog")
    _h, r = call(mode="free")
    assert r["contract"]["mode"] == EC.MODE_FREE
    assert EC.check_policy(project.store[EC.CONTRACT_KEY], "add_clips_to_timeline_tool", rollout="enforce").action == "allow"


def test_a_new_ask_on_a_finished_contract_starts_a_fresh_round(project):
    call(user_ask="make a vlog")
    project.store[EC.CONTRACT_KEY] = EC.advance(project.store[EC.CONTRACT_KEY], "review_done", needs=[])
    assert project.store[EC.CONTRACT_KEY]["phase"] == "done" and project.store[EC.CONTRACT_KEY]["audit"]["rounds"] == 1
    _h, r = call(user_ask="make a vlog")
    assert r["contract"]["phase"] == "briefed" and r["contract"]["audit"]["rounds"] == 0


def test_calling_it_again_mid_audit_keeps_the_audit_rounds(project):
    call(user_ask="make a vlog")
    project.store[EC.CONTRACT_KEY] = EC.advance(project.store[EC.CONTRACT_KEY], "review_done", needs=["dead air at 0:41"])
    _h, r = call(user_ask="make a vlog")
    assert r["contract"]["audit"]["rounds"] == 1 and r["contract"]["audit"]["needs"] == ["dead air at 0:41"]


# ------------------------------------------------------------------ commit_brief=False and the digest
def test_commit_brief_false_only_proposes(project):
    _h, r = call(user_ask="make a vlog", commit_brief=False)
    assert r["brief"]["form"] == "vlog" and r["committed"] is False
    assert project.writes == [] and project.store[TE.BRIEF_KEY] == {}


def test_the_digest_obeys_its_cap_and_full_detail_allows_more(project):
    project.survey = dict(SURVEY, top_moments=[{"file_id": f"f{i}", "name": "n" * 70, "start": 1.0, "end": 2.0, "highlight": 0.5, "why": "w" * 200}
                                               for i in range(40)])
    _h, small = call(user_ask="make a vlog")
    _h, big = call(user_ask="make a vlog", detail="full")
    assert len(json.dumps(small["survey_digest"], ensure_ascii=False)) <= EC.DIGEST_CAP_CHARS
    assert EC.DIGEST_CAP_CHARS < len(json.dumps(big["survey_digest"], ensure_ascii=False)) <= EC.DIGEST_FULL_CAP_CHARS


def test_questions_only_when_a_missing_choice_changes_the_film(project):
    project.survey = dict(SURVEY, totals=dict(SURVEY["totals"], usable_minutes=300.0))
    _h, r = call(user_ask="make something cool with my trip")
    assert r["questions"] and r["questions"][0]["id"] == "length"
    _h, r2 = call(user_ask="make a 3 minute vlog")
    assert r2["questions"] == []


def test_a_brief_that_would_blow_the_size_cap_is_refused_and_nothing_is_written(project):
    project.store[TE.BRIEF_KEY]["junk"] = "x" * (TE.BRIEF_MAX_BYTES - 100)
    out = REGISTRY["begin_edit_tool"].func(user_ask="make a vlog")
    assert out.startswith("Error") and "over" in out and project.writes == []
