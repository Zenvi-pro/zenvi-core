"""The desktop side of the edit contract: Plan-mode allowlist, handoff and CLI "carry out the plan" carry the brief,
and the optional WS hint. The chat-window methods run unbound on a fake ``self`` (the suite stubs Qt), so these checks
do not need a real PyQt5."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import edit_contract as EC  # noqa: E402
from classes.editor_tools import REGISTRY, media_index_tools_edit as TE  # noqa: E402

BLOCK = "[Edit brief] contract=finished_piece/briefed\nform: \"vlog\"\ntarget_seconds: 210\n[/Edit brief]"


@pytest.fixture
def project(monkeypatch):
    store = {TE.BRIEF_KEY: {}, EC.CONTRACT_KEY: {}, "clips": []}
    app = SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: store.get(k, d)))
    monkeypatch.setattr(TE, "get_app", lambda: app)
    return store


# ------------------------------------------------------------------ Plan mode keeps its promise
def test_planning_may_survey_and_write_the_brief_but_never_place_footage():
    from windows.ai_chat_ui import _is_planning_tool_allowed as allowed

    for name in ("begin_edit_tool", "get_edit_brief_tool", "set_edit_brief_tool"):
        assert allowed(name), name
    for name in sorted(EC.PLACEMENT_TOOLS):
        assert not allowed(name), f"{name} would change the timeline in Plan mode"


def test_every_core_read_only_tool_is_allowed_in_plan_mode():
    """Drift guard: a new read-only core tool must not silently become unusable while planning."""
    from classes.agent_tools.core_tools import CORE_TOOL_NAMES
    from windows.ai_chat_ui import _is_planning_tool_allowed as allowed

    read_only = [n for n in CORE_TOOL_NAMES if n in REGISTRY and REGISTRY[n].read_only]
    assert "get_edit_brief_tool" in read_only
    assert [n for n in read_only if not allowed(n)] == []


def test_no_tool_the_contract_watches_for_placement_is_allowed_in_plan_mode():
    from windows.ai_chat_ui import _is_planning_tool_allowed as allowed
    assert not any(allowed(n) for n in EC.PLACEMENT_TOOLS | {"export_video_tool", "trim_clips_tool", "slice_clips_tool"})


# ------------------------------------------------------------------ the brief block a window hands over
def test_the_window_builds_its_brief_block_from_the_project(project):
    from windows.ai_chat_ui import AIChatWindow

    assert AIChatWindow._edit_brief_context(None) == ""
    project[TE.BRIEF_KEY] = {"form": "vlog", "target_seconds": 210}
    project[EC.CONTRACT_KEY] = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    text = AIChatWindow._edit_brief_context(None)
    assert text.startswith("[Edit brief] contract=finished_piece/briefed") and "target_seconds: 210" in text and len(text) <= 1500


def test_a_broken_project_never_breaks_the_chat(monkeypatch):
    from windows.ai_chat_ui import AIChatWindow
    monkeypatch.setattr(TE, "get_app", lambda: (_ for _ in ()).throw(RuntimeError("no app")))
    assert AIChatWindow._edit_brief_context(None) == ""


# ------------------------------------------------------------------ handoff: a new agent gets the taste decisions too
@pytest.fixture
def handoff(monkeypatch):
    from classes import chat_history
    state = SimpleNamespace(recap="RECAP of missed turns")
    monkeypatch.setattr(chat_history, "load_messages", lambda sid: [])
    monkeypatch.setattr(chat_history, "handoff_recap", lambda messages, after: state.recap)
    return state


def fake_window(block):
    return SimpleNamespace(_active_sid="s1", _edit_brief_context=lambda: block)


def test_the_handoff_adds_the_brief_after_the_recap(handoff):
    from windows.ai_chat_ui import AIChatWindow
    sess = {"backend": "claude_code", "seen_seq": {"claude_code": 3}}
    out = AIChatWindow._handoff_prefix(fake_window(BLOCK), sess)
    assert out == f"RECAP of missed turns\n{BLOCK}\n"


def test_without_a_brief_the_handoff_is_exactly_the_recap(handoff):
    from windows.ai_chat_ui import AIChatWindow
    sess = {"backend": "claude_code", "seen_seq": {"claude_code": 3}}
    assert AIChatWindow._handoff_prefix(fake_window(""), sess) == "RECAP of missed turns"


def test_a_brief_alone_is_handed_over_when_there_is_no_recap(handoff):
    from windows.ai_chat_ui import AIChatWindow
    handoff.recap = ""
    sess = {"backend": "claude_code", "seen_seq": {"claude_code": 3}}
    assert AIChatWindow._handoff_prefix(fake_window(BLOCK), sess) == BLOCK + "\n"
    assert AIChatWindow._handoff_prefix(fake_window(""), sess) == ""


def test_a_tab_that_never_switched_agent_gets_nothing(handoff):
    from windows.ai_chat_ui import AIChatWindow
    assert AIChatWindow._handoff_prefix(fake_window(BLOCK), {"backend": "claude_code"}) == ""
    assert AIChatWindow._handoff_prefix(fake_window(BLOCK), {"backend": "claude_code", "seen_seq": {"claude_code": None}}) == ""


# ------------------------------------------------------------------ CLI "carry out the plan" keeps the brief
def run_execute(block):
    from windows import ai_chat_ui as UI

    sent = []
    sess = {"backend": "claude_code", "current_plan": {"cli": True}, "last_user_model_id": "m1", "last_user_effort": "high"}
    win = SimpleNamespace(is_processing=False, _use_web_ui=False, _edit_brief_context=lambda: block,
                          _active_session=lambda: sess, _run_js=lambda js: None, _set_agent_mode=lambda m: None,
                          _dispatch_user_message=lambda text, model, **kw: sent.append((text, model, kw)))
    UI.AIChatWindow._execute_plan(win, "cli", "")
    return sent


def test_carrying_out_a_cli_plan_repeats_the_brief():
    ((text, model, kw),) = run_execute(BLOCK)
    assert text == f"Carry out the plan.\n\n{BLOCK}" and model == "m1" and kw["agent_mode"] == "agent" and kw["effort"] == "high"


def test_without_a_brief_carrying_out_the_plan_is_exactly_what_it_was():
    ((text, _model, _kw),) = run_execute("")
    assert text == "Carry out the plan."


# ------------------------------------------------------------------ the optional WS hint
def test_a_stored_finished_piece_is_the_hint(project):
    from classes.api_client import edit_contract_hint
    project[EC.CONTRACT_KEY] = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    assert edit_contract_hint("make it warmer") == "finished_piece"
    project[EC.CONTRACT_KEY] = EC.new_contract(EC.MODE_RECREATE)
    assert edit_contract_hint("hi") == "recreate"


@pytest.fixture
def free_project(project, monkeypatch):
    """A free edit with media in the bin and an empty timeline."""
    import classes.app as app_mod
    import classes.query as Q
    monkeypatch.setattr(app_mod, "get_app", lambda: SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: project.get(k, d))), raising=False)
    monkeypatch.setattr(Q.File, "filter", staticmethod(lambda *a, **k: [object()]), raising=False)
    return project


def test_a_whole_piece_ask_on_an_empty_timeline_is_hinted(free_project):
    from classes.api_client import edit_contract_hint
    assert edit_contract_hint("make something cool with my trip") == "finished_piece"
    assert edit_contract_hint("recreate this video") == "recreate"


@pytest.mark.parametrize("text", ["make it warmer", "add captions", "", "what is in my footage?"])
def test_a_narrow_ask_gets_no_hint(free_project, text):
    from classes.api_client import edit_contract_hint
    assert edit_contract_hint(text) == ""


def test_file_names_in_the_snapshot_are_not_read_as_the_users_ask(free_project):
    from classes.api_client import edit_contract_hint
    snapshot = "[Editor snapshot]\nMEDIA_BIN:\n  name='my vlog trailer reel montage.mp4'\n[/Editor snapshot]\n"
    assert edit_contract_hint(snapshot + "\nmake it warmer") == ""
    assert edit_contract_hint(snapshot + "\n[Edit brief] contract=free\nform: \"vlog\"\n[/Edit brief]\nmake it warmer") == ""
    assert edit_contract_hint(snapshot + "\nmake a vlog of my trip") == "finished_piece"


def test_no_media_or_a_busy_timeline_means_no_hint(project, monkeypatch):
    import classes.app as app_mod
    import classes.query as Q
    from classes.api_client import edit_contract_hint
    monkeypatch.setattr(Q.File, "filter", staticmethod(lambda *a, **k: []), raising=False)
    monkeypatch.setattr(app_mod, "get_app", lambda: SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: project.get(k, d))), raising=False)
    assert edit_contract_hint("make a vlog") == ""                      # nothing to survey yet
    monkeypatch.setattr(Q.File, "filter", staticmethod(lambda *a, **k: [object()]), raising=False)
    project["clips"] = [{"id": "c1"}]
    assert edit_contract_hint("make a vlog") == ""                      # an edit is under way


def test_a_failure_to_read_the_project_means_no_hint(monkeypatch):
    from classes.api_client import edit_contract_hint
    monkeypatch.setattr(TE, "get_app", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert edit_contract_hint("make a vlog") == ""


def sent_payloads(monkeypatch, message):
    from classes import api_client
    sent = []

    class _FakeWs:
        def send(self, raw):
            sent.append(json.loads(raw))

        def recv(self):
            return json.dumps({"type": "done", "data": {}})

        def ping(self):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "websocket", types.SimpleNamespace(create_connection=lambda *a, **k: _FakeWs()))
    api_client.ZenviBackendClient(base_url="https://backend.test").send_message_ws(message, auth_token="tok")
    return [f["data"] for f in sent if f["type"] == "user_message"][0]


def test_the_hint_rides_the_ws_payload_only_when_there_is_one(project, monkeypatch):
    project[EC.CONTRACT_KEY] = EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True)
    assert sent_payloads(monkeypatch, "hi")["edit_contract_mode"] == "finished_piece"
    project[EC.CONTRACT_KEY] = {}
    assert "edit_contract_mode" not in sent_payloads(monkeypatch, "hello")


# ------------------------------------------------------------------ the desktop starts the contract from what the user typed
@pytest.fixture
def desk(project, monkeypatch):
    """A free project with ten minutes of video in the bin, an empty timeline, and a way to see what was written."""
    import classes.app as app_mod
    import classes.query as Q
    writes = []
    files = [SimpleNamespace(data={"duration": 600.0, "media_type": "video"}), SimpleNamespace(data={"duration": 30.0, "media_type": "audio"})]
    monkeypatch.setattr(app_mod, "get_app", lambda: SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: project.get(k, d))), raising=False)
    monkeypatch.setattr(Q.File, "filter", staticmethod(lambda *a, **k: files), raising=False)
    monkeypatch.setattr(TE, "write_contract", lambda c: (writes.append(c), project.__setitem__(EC.CONTRACT_KEY, c)))
    monkeypatch.delenv("ZENVI_EDIT_CONTRACT", raising=False)
    return SimpleNamespace(project=project, writes=writes, files=files)


def note(text):
    from windows.ai_chat_ui import AIChatWindow
    AIChatWindow._note_edit_intent(None, text)


def test_a_whole_piece_message_starts_the_contract(desk):
    note("make something cool with my trip")
    (c,) = desk.writes
    assert c["mode"] == EC.MODE_FINISHED and c["phase"] == "idle" and c["source"] == "classifier"


@pytest.mark.parametrize("text", ["make it warmer", "add captions", "45 seconds", "", "what is in my footage?"])
def test_a_narrow_message_writes_nothing(desk, text):
    note(text)
    assert desk.writes == []


def test_the_flag_off_writes_nothing(desk, monkeypatch):
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "off")
    note("make a vlog")
    assert desk.writes == []


def test_a_piece_under_way_is_neither_downgraded_nor_rewritten(desk):
    desk.project[EC.CONTRACT_KEY] = EC.advance(EC.advance(EC.new_contract(EC.MODE_FINISHED), "begin_ok", ready=True), "placed")
    note("make a vlog")
    note("make it warmer")
    assert desk.writes == []


def test_a_busy_timeline_starts_nothing(desk):
    desk.project["clips"] = [{"id": "c1"}]
    note("make a vlog")
    assert desk.writes == []


def test_no_video_in_the_bin_starts_nothing(desk):
    desk.files[:] = [SimpleNamespace(data={"duration": 30.0, "media_type": "audio"}),
                     SimpleNamespace(data={"zenvi_subclip": True, "duration": 99, "media_type": "video"})]
    note("make a vlog")
    assert desk.writes == []


def test_any_error_leaves_the_contract_alone_and_never_raises(desk, monkeypatch):
    monkeypatch.setattr(TE, "read_contract", lambda: (_ for _ in ()).throw(RuntimeError("no project")))
    note("make a vlog")
    assert desk.writes == []


def test_the_message_pipeline_notes_the_intent_before_it_builds_the_snapshot():
    import inspect
    from windows.ai_chat_ui import AIChatWindow
    src = inspect.getsource(AIChatWindow._dispatch_user_message)
    assert "self._note_edit_intent(text)" in src and src.index("_note_edit_intent(") < src.index("_prepend_editor_snapshot(")
    import re
    guard = r'if action == "chat" and text:' + r"\s+" + r"self\._note_edit_intent\(text\)"
    assert re.search(guard, src), "only a chat message counts, never a plan action"


def test_end_to_end_a_vague_message_then_an_eager_placement_is_held_back_in_enforce(desk, monkeypatch):
    """The gap the eval first hid: nothing but the user's message sets the contract, and the executor then holds the placement."""
    from classes.agent_tools import execute as X
    monkeypatch.setenv("ZENVI_EDIT_CONTRACT", "enforce")
    note("make a vlog of my trip")
    app = SimpleNamespace(project=SimpleNamespace(get=lambda k, d=None: desk.project.get(k, d)), updates=SimpleNamespace(actionHistory=[]), thread=lambda: None)
    calls = []
    monkeypatch.setattr(X, "_HANDLERS", {"add_clips_to_timeline_tool": lambda **k: calls.append(k) or "Placed."})
    monkeypatch.setattr(X, "_READ_ONLY", frozenset())
    monkeypatch.setattr(X, "_BACKGROUND_SAFE", frozenset())
    monkeypatch.setattr(X, "_UNGROUPED", frozenset())
    monkeypatch.setattr(X, "_GET_APP", lambda: app)
    monkeypatch.setattr(X, "_RUN_ON_MAIN", lambda fn, timeout=None: fn())
    monkeypatch.setattr(X, "_ATOMIC", lambda a, h: h)
    monkeypatch.setattr(X, "_QTHREAD", None)
    monkeypatch.setattr("classes.agent_tools.schema.validate_args", lambda t, a: None)
    receipt = X.execute_tool_rich("add_clips_to_timeline_tool", {"items": []}).receipt
    assert receipt.status == "refused" and "begin_edit_tool" in receipt.summary and calls == []
