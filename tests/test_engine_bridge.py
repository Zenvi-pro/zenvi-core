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
    assert sorted(bridge.handlers) == ["add_motion_template_tool"]
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
    monkeypatch.setattr(bridged, "_snapshot", lambda: (*real()[:2], "an older version"))
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


class _Answering:
    """A bridge client that answers every call with *answer*, after running *meanwhile*."""

    def __init__(self, answer, meanwhile=lambda: None):
        self.answer, self.meanwhile = answer, meanwhile

    def run(self, name, args, project_json, project_file, token):
        self.meanwhile()
        return self.answer


def _drawing(project_file):
    from classes.assets import get_assets_path
    folder = os.path.join(get_assets_path(project_file, create_paths=False), "title")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "generated.svg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("<svg/>")
    return path


def test_a_project_closed_while_the_engine_ran_gets_nothing(bridge, monkeypatch):
    # A failed open of another project leaves a blank default project with the same data version.
    from classes import engine_bridge

    drawing = _drawing(bridge.store.current_filepath)
    layer = {"type": "insert", "key": ["layers"], "value": {"id": "FAKELAYR02", "number": 9000000}}
    answer = {"ok": True, "text": "{}", "actions": [layer], "files": [drawing]}
    monkeypatch.setattr(engine_bridge, "get_client",
                        lambda: _Answering(answer, lambda: setattr(bridge.store, "current_filepath", None)))
    receipt = bridge.call_receipt("add_motion_template_tool", template="end_card")
    assert "another project" in receipt["summary"]
    assert not any(x["id"] == "FAKELAYR02" for x in bridge.get("layers"))
    assert not os.path.exists(drawing)
    assert bridge.undo_steps_since_mark() == 0


@pytest.mark.parametrize("bad", [
    "insert",                                                        # not an action object
    {"type": "update", "key": "layers", "value": {}},                # key is not a list
    {"type": "update", "key": [], "value": {}},                      # empty key
    {"type": "update", "key": ["layers", 3], "value": {}},           # a key part that is neither name nor selector
    {"type": "insert", "key": ["layers"]},                           # an insert without a value
])
def test_a_malformed_action_applies_nothing_and_removes_the_files(bridge, monkeypatch, bad):
    from classes import engine_bridge

    drawing = _drawing(bridge.store.current_filepath)
    layer = {"type": "insert", "key": ["layers"], "value": {"id": "FAKELAYR02", "number": 9000000}}
    answer = {"ok": True, "text": "{}", "actions": [layer, bad], "files": [drawing]}
    monkeypatch.setattr(engine_bridge, "get_client", lambda: _Answering(answer))
    receipt = bridge.call_receipt("add_motion_template_tool", template="end_card")
    assert receipt["summary"].startswith("Error") and "malformed" in receipt["summary"]
    assert not any(x["id"] == "FAKELAYR02" for x in bridge.get("layers"))
    assert not os.path.exists(drawing)
    assert bridge.undo_steps_since_mark() == 0


def test_discard_does_not_follow_a_symlink_out_of_the_assets_folder(tmp_path):
    from classes.assets import get_assets_path
    from classes.editor_tools import engine_bridge as bridged

    project_file = str(tmp_path / "promo.zvn")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "generated.svg").write_text("keep me")
    assets = get_assets_path(project_file, create_paths=False)
    os.makedirs(assets, exist_ok=True)
    os.symlink(str(outside), os.path.join(assets, "title"))
    inside = os.path.join(assets, "drawn.svg")
    with open(inside, "w", encoding="utf-8") as fh:
        fh.write("<svg/>")
    bridged._discard([os.path.join(assets, "title", "generated.svg"), inside], project_file)
    assert (outside / "generated.svg").exists()
    assert not os.path.exists(inside)
