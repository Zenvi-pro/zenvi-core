"""The engine bridge (classes/engine_bridge.py, classes/editor_tools/engine_bridge.py)
against a fake zenvi CLI (tests/fixtures/engine_bridge/fake_zenvi.py): engine tools
are listed next to the desktop's, a call applies the engine's actions as one undo
step, and refusals, stale snapshots and broken CLIs change nothing."""

import json
import os
import sys

import pytest

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "engine_bridge", "fake_zenvi.py")


def _calls(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture
def bridge(editor, tmp_path, monkeypatch):
    """The fake CLI as the session's bridge, its tools registered like tool_handlers does."""
    from classes import engine_bridge, info, tool_handlers
    from classes.editor_tools import engine_bridge as bridged

    log = str(tmp_path / "calls.jsonl")
    monkeypatch.setenv("FAKE_ZENVI_LOG", log)
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.setattr(bridged, "BRIDGED", {})
    client = engine_bridge.EngineBridgeClient(sys.executable, FAKE, "server", timeout=30)
    engine_bridge.set_client(client)
    handlers = bridged.register_tools(set(tool_handlers.AGENT_TOOL_HANDLERS), catalog=bridged.load_catalog(client))
    for name, handler in handlers.items():
        monkeypatch.setitem(tool_handlers.TOOL_HANDLERS, name, handler)
    monkeypatch.setattr(tool_handlers, "BACKGROUND_SAFE_TOOLS", tool_handlers.BACKGROUND_SAFE_TOOLS | frozenset(handlers))
    editor.store.current_filepath = str(tmp_path / "promo.zvn")
    editor.handlers = handlers
    editor.log = log
    yield editor
    engine_bridge.set_client(None)


def test_lists_engine_tools_the_desktop_lacks_and_caches_the_catalog(bridge, tmp_path):
    from classes.editor_tools import engine_bridge as bridged

    # list_files_tool is the desktop's own; export needs a capability the bridge host lacks.
    assert "add_motion_template_tool" in bridge.handlers
    assert "list_files_tool" not in bridge.handlers and "export_video_tool" not in bridge.handlers
    definition = bridged.tool_def("add_motion_template_tool")
    assert definition["inputSchema"]["properties"] == {"template": {"type": "string"}}
    assert bridged.SURFACE_NOTE in definition["description"]
    assert len([c for c in _calls(bridge.log) if c.get("catalog")]) == 1
    # A second launch reads the cache instead of starting the CLI.
    from classes import engine_bridge
    bridged.load_catalog(engine_bridge.get_client())
    assert len([c for c in _calls(bridge.log) if c.get("catalog")]) == 1


def test_a_call_applies_the_engines_change_as_one_undo_step(bridge):
    before = len(bridge.clips())
    receipt = bridge.call_receipt("add_motion_template_tool", template="end_card")
    assert receipt["status"] == "applied", receipt
    clip = bridge.clip("FAKECLIP01")
    assert clip is not None and clip["location_x"]["Points"][0]["co"]["X"] == 1
    assert bridge.file("FAKEFILE01") is not None
    assert any(layer["id"] == "FAKELAYR01" for layer in bridge.get("layers"))
    # The engine's bundled-asset alias resolves to the desktop's own transitions.
    wipe = next(t for t in bridge.get("effects") if t["id"] == "FAKETRAN01")
    assert os.path.isabs(wipe["reader"]["path"]) and wipe["reader"]["path"].endswith(os.path.join("transitions", "common", "fade.svg"))
    assert bridge.undo_steps_since_mark() == 1
    # What the CLI got: the in-memory project without its history, the absolute project file.
    (call,) = _calls(bridge.log)[-1:]
    assert call["project_file"] == bridge.store.current_filepath
    assert "history" not in call["project_keys"] and "clips" in call["project_keys"]
    assert call["clips"] == before

    bridge.undo()
    assert bridge.clip("FAKECLIP01") is None and bridge.file("FAKEFILE01") is None
    assert not any(layer["id"] == "FAKELAYR01" for layer in bridge.get("layers"))
    bridge.redo()
    assert bridge.clip("FAKECLIP01") is not None


def test_a_refusal_changes_nothing(bridge):
    receipt = bridge.call_receipt("add_motion_template_tool", template="nope")
    assert receipt["status"] in ("refused", "error")
    assert bridge.clip("FAKECLIP01") is None
    assert bridge.undo_steps_since_mark() == 0


def test_an_unsaved_project_is_refused_before_the_cli_runs(bridge):
    bridge.store.current_filepath = None
    receipt = bridge.call_receipt("add_motion_template_tool", template="end_card")
    assert receipt["summary"].startswith("Error") and "saved" in receipt["summary"]
    assert _calls(bridge.log)[-1].get("catalog")
    assert bridge.undo_steps_since_mark() == 0


def test_a_project_changed_while_the_engine_ran_is_not_overwritten(bridge, monkeypatch):
    from classes.editor_tools import engine_bridge as bridged

    real = bridged._snapshot
    monkeypatch.setattr(bridged, "_snapshot", lambda: (real()[0], "an older version", real()[2]))
    receipt = bridge.call_receipt("add_motion_template_tool", template="end_card")
    assert "changed while" in receipt["summary"]
    assert bridge.clip("FAKECLIP01") is None
    assert bridge.undo_steps_since_mark() == 0


def test_a_crashed_cli_fails_the_call_and_the_next_call_restarts_it(bridge):
    receipt = bridge.call_receipt("add_motion_template_tool", template="crash")
    assert receipt["summary"].startswith("Error") and "engine bridge" in receipt["summary"]
    assert bridge.undo_steps_since_mark() == 0
    assert bridge.call_receipt("add_motion_template_tool", template="end_card")["status"] == "applied"


def test_oneshot_mode_runs_tool_run_per_call(bridge):
    from classes import engine_bridge

    engine_bridge.set_client(engine_bridge.EngineBridgeClient(sys.executable, FAKE, "oneshot", timeout=30))
    receipt = bridge.call_receipt("add_motion_template_tool", template="end_card")
    assert receipt["status"] == "applied"
    assert bridge.clip("FAKECLIP01") is not None
    assert bridge.undo_steps_since_mark() == 1


def test_a_cli_speaking_another_protocol_is_refused(monkeypatch):
    from classes import engine_bridge

    monkeypatch.setenv("FAKE_ZENVI_PROTOCOL", "99")
    client = engine_bridge.EngineBridgeClient(sys.executable, FAKE, "server", timeout=30)
    with pytest.raises(engine_bridge.BridgeUnavailable, match="protocol 99"):
        client.run("add_motion_template_tool", {}, "{}", "/tmp/x.zvn")
    with pytest.raises(engine_bridge.BridgeUnavailable, match="protocol 99"):
        client.tools()
    client.close()


def test_the_bridge_is_off_by_default(monkeypatch):
    from classes import engine_bridge
    from classes.editor_tools import engine_bridge as bridged

    monkeypatch.delenv("ZENVI_ENGINE_BRIDGE", raising=False)
    assert engine_bridge.bridge_mode() == ""
    assert bridged.register_tools(set()) == {}
    assert engine_bridge.bridge_mode({"ZENVI_ENGINE_BRIDGE": "1"}) == "server"
    assert engine_bridge.bridge_mode({"ZENVI_ENGINE_BRIDGE": "oneshot"}) == "oneshot"


def test_the_mcp_server_lists_bridged_tools_with_the_engines_schema(bridge, monkeypatch):
    from classes import agent_mcp_server, tool_handlers

    for name, handler in bridge.handlers.items():
        monkeypatch.setitem(tool_handlers.AGENT_TOOL_HANDLERS, name, handler)
    (listed,) = [d for d in agent_mcp_server.iter_tool_defs() if d["name"] == "add_motion_template_tool"]
    assert listed["inputSchema"] == {"type": "object", "properties": {"template": {"type": "string"}}}
    assert listed["description"].startswith("Place a motion template.")


def test_an_untracked_change_is_applied_without_an_undo_step(bridge):
    assert bridge.call_receipt("add_motion_template_tool", template="seek")["status"] == "applied"
    assert bridge.get("playhead_position") == 2.5
    assert bridge.undo_steps_since_mark() == 0


def test_an_action_kind_the_desktop_does_not_know_applies_nothing(bridge):
    receipt = bridge.call_receipt("add_motion_template_tool", template="future")
    assert receipt["summary"].startswith("Error") and "update_sideways" in receipt["summary"]
    assert not any(layer["id"] == "FAKELAYR02" for layer in bridge.get("layers"))
    assert bridge.get("playhead_position") != 2.5
    assert bridge.undo_steps_since_mark() == 0
