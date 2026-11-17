"""The local edit-contract log: counts and event names only, written off the calling thread, never raising, capped in size,
and summarized into the one number that matters (does an agent recover after a refusal?)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes import edit_contract as EC  # noqa: E402
from classes import edit_contract_log as L  # noqa: E402
from test_edit_contract_executor import PLACE, review_text, run, set_contract, world  # noqa: E402,F401  (the real-executor fixture)


@pytest.fixture
def logfile(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG", "1")
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG_PATH", str(path))
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG_SYNC", "1")
    return path


def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ------------------------------------------------------------------ what is written
def test_only_whitelisted_plain_values_are_ever_written(logfile):
    L.record("gate", action="refuse", tool=PLACE, user_ask="make a vlog of my trip to Lisbon", text="secret", file_name="/home/me/a.mp4",
             path="C:\\Users\\me", rounds=2, needs=1.0, ok=True, flag=None, mode="finished_piece" * 10)
    (e,) = lines(logfile)
    assert set(e) <= {"ts", "event", "action", "tool", "rounds", "needs", "mode"}
    assert e["event"] == "gate" and e["action"] == "refuse" and e["rounds"] == 2 and e["needs"] == 1
    assert len(e["mode"]) <= 40 and "Lisbon" not in json.dumps(e) and "secret" not in json.dumps(e) and "mp4" not in json.dumps(e)


def test_nothing_is_written_when_it_is_switched_off(logfile, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG", "0")
    L.record("gate", action="refuse")
    assert not logfile.exists()
    for off in ("off", "false", "no", "OFF"):
        monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG", off)
        assert L.enabled() is False


def test_recording_never_raises_even_when_the_file_cannot_be_written(tmp_path, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG", "1")
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG_SYNC", "1")
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG_PATH", str(tmp_path))            # a directory: opening it for append fails
    L.record("gate", action="refuse")
    (tmp_path / "blocker").write_text("a file, so nothing can be created under it", encoding="utf-8")
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG_PATH", str(tmp_path / "blocker" / "sub" / "log.jsonl"))
    L.record("gate", action="refuse")


def test_the_background_writer_writes_off_the_calling_thread(tmp_path, monkeypatch):
    import threading
    path = tmp_path / "async.jsonl"
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG", "1")
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT_LOG_PATH", str(path))
    monkeypatch.delenv("ZENVI_EDIT_CONTRACT_LOG_SYNC", raising=False)
    writers = []
    real = L._write
    monkeypatch.setattr(L, "_write", lambda item: (writers.append(threading.current_thread().name), real(item)))
    for i in range(5):
        L.record("gate", action="warn", rounds=i)
    assert L.flush(5.0)
    assert [e["rounds"] for e in lines(path)] == [0, 1, 2, 3, 4]
    assert writers and all(w == "edit-contract-log" for w in writers), "never written on the calling (GUI) thread"


def test_the_file_is_capped_and_the_older_half_is_kept(logfile, monkeypatch):
    monkeypatch.setattr(L, "MAX_BYTES", 400)
    for i in range(30):
        L.record("gate", action="warn", rounds=i)
    assert logfile.stat().st_size <= 400 + 120 and Path(str(logfile) + ".1").exists()
    events = L.read(str(logfile))
    assert events and events[-1]["rounds"] == 29 and len(events) < 30, "the newest is kept, the oldest are dropped"


def test_reading_skips_damaged_lines_and_missing_files(logfile, tmp_path):
    logfile.write_text('{"event":"gate","action":"warn"}\nnot json\n\n{"event":"placed"}\n', encoding="utf-8")
    assert [e["event"] for e in L.read(str(logfile))] == ["gate", "placed"]
    assert L.read(str(tmp_path / "missing.jsonl")) == []


# ------------------------------------------------------------------ the summary
def ev(event, **kw):
    return {"event": event, **kw}


def test_recovery_after_a_refusal_is_counted():
    s = L.summarize([ev("gate", action="refuse"), ev("begin_edit", status="ok"), ev("placed"), ev("audit", needs=2), ev("audit", needs=0)])
    assert (s["refused"], s["recovered_after_refusal"], s["refusals_without_recovery"], s["mean_needs_at_audit"]) == (1, 1, 0, 1.0)


def test_a_refusal_the_agent_ignored_is_not_a_recovery():
    s = L.summarize([ev("gate", action="refuse"), ev("gate", action="refuse"), ev("gate", action="refuse")])
    assert s["refused"] == 3 and s["recovered_after_refusal"] == 0 and s["refusals_without_recovery"] == 3


def test_a_brief_saved_another_way_clears_the_refusal_without_counting_a_recovery():
    s = L.summarize([ev("gate", action="refuse"), ev("placed")])
    assert s["recovered_after_refusal"] == 0 and s["refusals_without_recovery"] == 0


def test_waiting_is_not_recovery_and_warnings_and_pieces_are_counted():
    s = L.summarize([ev("piece_started"), ev("gate", action="refuse"), ev("begin_edit", status="wait"), ev("begin_edit", status="ok"),
                     ev("gate", action="warn"), ev("export_warned")])
    assert s["begin_edit_wait"] == 1 and s["recovered_after_refusal"] == 1 and s["warned"] == 1 and s["pieces_started"] == 1 and s["exports_warned"] == 1


def test_an_empty_log_summarizes_to_zeros():
    s = L.summarize([])
    assert s["events"] == 0 and s["mean_needs_at_audit"] is None


# ------------------------------------------------------------------ the hooks, through the real executor and tool
def test_a_refusal_a_begin_edit_and_an_audit_leave_exactly_these_events(world, logfile, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, "idle")
    run(PLACE, items=[])                                                                 # refused
    set_contract(world, EC.MODE_FINISHED, "briefed")
    run(PLACE, items=[])                                                                 # placed
    world.review = review_text(["dead_air"])
    run("review_edit_tool")
    run("export_video_tool")
    got = [(e["event"], e.get("action"), e.get("phase"), e.get("needs")) for e in lines(logfile)]
    assert got == [("gate", "refuse", "idle", None), ("placed", None, "assembling", None), ("audit", None, "audited", 1), ("export_warned", None, "audited", None)]


def test_the_hooks_never_record_what_was_typed_or_which_files(world, logfile, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, "idle")
    run(PLACE, items=[{"file_id": "SECRET_FILE_ID", "source_start": 1, "source_end": 2}], note="my private trip to Lisbon")
    text = logfile.read_text(encoding="utf-8")
    assert "SECRET_FILE_ID" not in text and "Lisbon" not in text and "private" not in text
    for e in lines(logfile):
        assert set(e) <= {"ts", "event", *L._ALLOWED}


def test_off_logs_nothing_and_a_free_edit_logs_nothing(world, logfile, monkeypatch):
    run(PLACE, items=[])                                                                 # free edit, default warn
    assert lines(logfile) == []
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "off")
    set_contract(world, EC.MODE_FINISHED, "idle")
    run(PLACE, items=[])
    assert lines(logfile) == []


def test_a_broken_logger_never_changes_a_tools_result(world, logfile, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    set_contract(world, EC.MODE_FINISHED, "idle")
    before = run(PLACE, items=[]).to_json()
    monkeypatch.setattr(L, "_write", lambda item: (_ for _ in ()).throw(OSError("disk full")))
    assert run(PLACE, items=[]).to_json() == before


def test_begin_edit_logs_ok_and_wait_with_counts_only(logfile, monkeypatch):
    import json as _json
    from types import SimpleNamespace
    from classes.editor_tools import REGISTRY, media_index_tools_edit as TE
    store = {TE.BRIEF_KEY: {}, EC.CONTRACT_KEY: {}, "clips": []}
    app = SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: store.get(k, d)), updates=SimpleNamespace(update_untracked=lambda k, v: store.setdefault(k[0], {}).update(v)))
    monkeypatch.setattr(TE, "get_app", lambda: app)
    monkeypatch.setattr(TE, "on_main", lambda f, *a, **k: f(*a))
    from types import SimpleNamespace as NS
    vid = lambda s: NS(media_type="video", shots=[{"start": 0, "end": 4}] if s else [], rows=[], vector_model="", duration=300.0, sentences=[], look_file=None, layers={})
    survey = {"totals": {"usable_minutes": 40.0}, "trip": {"days": []}, "top_moments": [], "music": [], "orientation": {"landscape": 5}, "not_indexed": []}
    monkeypatch.setattr(TE, "survey", lambda files, missing, **kw: survey)
    monkeypatch.setattr(TE, "project_indexes", lambda file_ids=None: ([], ["a.mov", "b.mov"]))
    REGISTRY["begin_edit_tool"].func(user_ask="make a vlog")
    monkeypatch.setattr(TE, "project_indexes", lambda file_ids=None: ([vid(True) for _ in range(5)], []))
    REGISTRY["begin_edit_tool"].func(user_ask="make a vlog")
    waited, ok_ = lines(logfile)
    assert (waited["event"], waited["status"], waited["structured"], waited["total"], waited["attempts"]) == ("begin_edit", "wait", 0, 2, 1)
    assert (ok_["event"], ok_["status"], ok_["tier"], ok_["structured"], ok_["total"]) == ("begin_edit", "ok", "structure", 5, 5)
    assert "a.mov" not in _json.dumps([waited, ok_])


def test_the_desktop_notes_a_started_piece(logfile, monkeypatch):
    from types import SimpleNamespace
    import classes.app as app_mod
    import classes.query as Q
    from classes.editor_tools import media_index_tools_edit as TE
    store = {TE.BRIEF_KEY: {}, EC.CONTRACT_KEY: {}, "clips": []}
    monkeypatch.setattr(app_mod, "get_app", lambda: SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: store.get(k, d))), raising=False)
    monkeypatch.setattr(Q.File, "filter", staticmethod(lambda *a, **k: [SimpleNamespace(data={"duration": 600.0, "media_type": "video"})]), raising=False)
    monkeypatch.setattr(TE, "write_contract", lambda c: store.__setitem__(EC.CONTRACT_KEY, c))
    monkeypatch.setattr(TE, "get_app", lambda: SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: store.get(k, d))))
    monkeypatch.delenv("ZENVI_EDIT_CONTRACT", raising=False)
    from windows.ai_chat_ui import AIChatWindow
    AIChatWindow._note_edit_intent(None, "make a vlog of my trip")
    AIChatWindow._note_edit_intent(None, "make it warmer")
    (e,) = lines(logfile)
    assert (e["event"], e["mode"], e["source"], e["rollout"]) == ("piece_started", "finished_piece", "classifier", "warn")
