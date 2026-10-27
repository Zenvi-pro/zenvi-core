"""Which engine tools the desktop lists through the engine bridge, and how they run.

Against the fake zenvi CLI (tests/fixtures/engine_bridge/fake_zenvi.py): native
tools and their engine twins are never bridged, the catalog is read off the GUI
thread, engine frames come back as images, linked clips the engine rendered
(Zenvi Motion, Zenvi 3D, Zenvi FX) re-render through their engine tool in one
undo step, and the CLI / Node discovery refuses with what to set.
"""

import json
import os
import stat
import subprocess
import sys
import threading

import pytest

from test_engine_bridge import FAKE, _calls, bridge  # noqa: F401  (the fixture)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _link(kind, **extra):
    return {"kind": kind, "version": 1, "source": {"composition": "counter"}, "props": {"to": 100},
            "render": {"codec": "h264", "fingerprint": "sha256:old"}, "state": "fresh", "error": None, **extra}


# -- what is listed --------------------------------------------------------------

def test_native_tools_and_their_engine_twins_are_never_bridged(bridge):  # noqa: F811
    from classes import tool_handlers
    from classes.editor_tools import engine_bridge as bridged

    assert "list_files_tool" not in bridge.handlers  # the desktop's own, by name
    assert "move_clip_tool" not in bridge.handlers  # the engine's twin of move_clips_tool
    assert bridged.NATIVE_TWINS["move_clip_tool"] in tool_handlers.AGENT_TOOL_HANDLERS
    assert "export_video_tool" not in bridge.handlers  # needs a host capability the bridge lacks
    assert {"add_motion_template_tool", "update_motion_block_tool", "update_fx_tool"} <= set(bridge.handlers)


def test_linked_clip_tools_are_added_only_when_the_desktop_lacks_them(bridge):  # noqa: F811
    from classes.editor_tools import engine_bridge as bridged

    assert {"get_linked_clip_tool", "rerender_linked_clip_tool"} <= set(bridge.handlers)
    listed = bridged.tool_def("rerender_linked_clip_tool")
    assert bridged.SURFACE_NOTE not in listed["description"]  # it runs on the desktop
    assert set(listed["inputSchema"]["properties"]) == {"clip_id", "file_id"}
    # With native linked-clip tools (handoff-core), native wins.
    catalog = [t for t in bridged.BRIDGED.values() if not t.get("desktop")]
    again = bridged.register_tools({"get_linked_clip_tool", "rerender_linked_clip_tool"}, catalog=catalog)
    assert "rerender_linked_clip_tool" not in again and "update_motion_block_tool" in again
    # Without an engine tool that re-renders a kind, there is nothing for them to do.
    plain = [t for t in catalog if t["name"] not in ("update_motion_block_tool", "update_fx_tool")]
    bridged.BRIDGED.clear()
    assert "get_linked_clip_tool" not in bridged.register_tools(set(), catalog=plain)


def test_the_catalog_is_read_off_the_gui_thread_and_listed_when_it_arrives(editor, tmp_path, monkeypatch):
    from classes import agent_mcp_server, engine_bridge, info, tool_handlers
    from classes.editor_tools import engine_bridge as bridged

    monkeypatch.setenv("ZENVI_ENGINE_BRIDGE", "1")
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.setattr(bridged, "BRIDGED", {})
    engine_bridge.set_client(engine_bridge.EngineBridgeClient(sys.executable, FAKE, "server", timeout=30))
    seen = {}

    def install(handlers):
        seen["thread"] = threading.current_thread()
        for name, handler in handlers.items():
            monkeypatch.setitem(tool_handlers.AGENT_TOOL_HANDLERS, name, handler)

    revision = bridged.REVISION
    try:
        worker = bridged.register_in_background(lambda: set(tool_handlers.AGENT_TOOL_HANDLERS), install)
        worker.join(30)
    finally:
        engine_bridge.set_client(None)
    assert seen["thread"] is worker and worker is not threading.main_thread()
    assert bridged.REVISION == revision + 1
    names = {d["name"] for d in agent_mcp_server.iter_tool_defs()}
    assert {"add_motion_template_tool", "rerender_linked_clip_tool"} <= names


def test_nothing_is_registered_or_started_while_the_bridge_is_off(monkeypatch):
    from classes.editor_tools import engine_bridge as bridged

    monkeypatch.delenv("ZENVI_ENGINE_BRIDGE", raising=False)
    assert bridged.register_in_background(set, lambda handlers: pytest.fail("installed")) is None


# -- running ---------------------------------------------------------------------

def test_the_project_is_serialized_without_its_history_and_retried_when_torn():
    from classes.editor_tools import engine_bridge as bridged

    class Torn(dict):
        tries = 0

        def items(self):
            Torn.tries += 1
            if Torn.tries == 1:
                raise RuntimeError("dictionary changed size during iteration")
            return super().items()

    text = bridged._serialize(Torn({"clips": [1], "history": {"undo": []}}))
    assert json.loads(text) == {"clips": [1]} and Torn.tries == 2


def test_engine_frames_come_back_as_images_with_the_receipt(bridge):  # noqa: F811
    from classes import tool_handlers

    out = tool_handlers.execute_tool_rich("review_frames_tool", {})
    assert out.receipt.status == "applied" and out.receipt.data == {"frames": 2}
    assert len(out.images) == 2 and out.images[0].data.startswith(b"\x89PNG")
    assert bridge.undo_steps_since_mark() == 0


def test_bin_files_indexed_while_the_engine_ran_do_not_refuse_the_answer(bridge, monkeypatch):  # noqa: F811
    from classes.query import File
    from test_engine_bridge import _during_the_run

    f = bridge.add_file("video")

    def index():
        record = File.get(id=f)
        record.data["summary"] = "a test card"
        record.save()

    _during_the_run(monkeypatch, index)
    receipt = bridge.call_receipt("add_motion_template_tool", template="end_card")
    assert receipt["status"] == "applied", receipt
    assert bridge.clip("FAKECLIP01") is not None and bridge.file(f)["summary"] == "a test card"


def test_a_file_the_answer_updates_that_changed_meanwhile_refuses_it(bridge, monkeypatch, tmp_path):  # noqa: F811
    from classes.query import File
    from test_engine_bridge import _during_the_run

    f = bridge.add_file("video", path=str(tmp_path / "counter.mov"), zenvi_link=_link("zenvi-motion"))

    def rename():
        record = File.get(id=f)
        record.data["name"] = "renamed meanwhile"
        record.save()

    _during_the_run(monkeypatch, rename)
    receipt = bridge.call_receipt("rerender_linked_clip_tool", file_id=f)
    assert "changed while" in receipt["summary"] and f"files/{f}" in receipt["summary"]
    assert bridge.file(f)["path"].endswith("counter.mov") and bridge.file(f)["name"] == "renamed meanwhile"


def test_an_answer_never_lands_in_a_project_opened_while_the_engine_ran(bridge, monkeypatch):  # noqa: F811
    import copy

    from test_engine_bridge import _during_the_run

    def open_another():
        bridge.store._data = copy.deepcopy(bridge.store._data)
        bridge.manager.commit_data_version()

    _during_the_run(monkeypatch, open_another)
    receipt = bridge.call_receipt("add_motion_template_tool", template="end_card")
    assert "another project was opened" in receipt["summary"]
    assert bridge.clip("FAKECLIP01") is None and bridge.file("FAKEFILE01") is None
    bridge.store.current_filepath = None  # nor one saved under another name
    assert bridge.call_receipt("add_motion_template_tool", template="end_card")["status"] in ("refused", "error")


def test_a_file_the_answers_clips_play_that_changed_meanwhile_refuses_it():
    from classes.editor_tools import engine_bridge as bridged

    actions = [{"type": "insert", "key": ["clips"], "value": {"id": "C", "file_id": "F", "reader": {"path": "/a"}}}]
    assert bridged._conflicts(["files/F"], actions) == ["files/F"]
    assert bridged._conflicts(["files/G"], actions) == []


def test_an_answer_replacing_the_whole_files_list_conflicts_with_any_file_changed_meanwhile():
    from classes.editor_tools import engine_bridge as bridged

    # The engine sends a top-level list update as is: applying it would revert the desktop's indexing.
    actions = [{"type": "update", "key": ["files"], "value": [{"id": "F"}]}]
    assert bridged._conflicts(["files/G"], actions) == ["files/G"]
    # Inserting a record leaves the others alone.
    assert bridged._conflicts(["files/G"], [{"type": "insert", "key": ["files"], "value": {"id": "N"}}]) == []


def test_only_kinds_the_engine_re_renders_are_engine_kinds():
    from classes.editor_tools import engine_links

    assert engine_links.is_engine_kind("zenvi-motion") and engine_links.is_engine_kind("blender")
    for kind in ("Blender", " zenvi-fx", "remotion", None, {"kind": "blender"}):
        assert not engine_links.is_engine_kind(kind), kind


def test_installing_bridged_tools_lists_them_background_safe_and_labelled(monkeypatch):
    from classes import tool_handlers

    for name in ("AGENT_TOOL_HANDLERS", "TOOL_HANDLERS", "TOOL_DISPLAY_LABELS"):
        monkeypatch.setattr(tool_handlers, name, dict(getattr(tool_handlers, name)))
    monkeypatch.setattr(tool_handlers, "BACKGROUND_SAFE_TOOLS", tool_handlers.BACKGROUND_SAFE_TOOLS)
    handler = object()
    tool_handlers.install_bridged_tools({"add_sfx_tool": handler})
    assert tool_handlers.AGENT_TOOL_HANDLERS["add_sfx_tool"] is handler
    assert tool_handlers.TOOL_HANDLERS["add_sfx_tool"] is handler
    assert "add_sfx_tool" in tool_handlers.BACKGROUND_SAFE_TOOLS and tool_handlers.TOOL_DISPLAY_LABELS["add_sfx_tool"]


def test_what_changed_names_list_items_by_id_and_other_keys_by_name():
    from classes.editor_tools import engine_bridge as bridged

    before = json.dumps({"clips": [{"id": "A", "x": 1}, {"id": "B"}], "files": [], "width": 1})
    after = json.dumps({"clips": [{"id": "A", "x": 2}], "files": [{"id": "F"}], "width": 2})
    assert bridged.what_changed(before, after) == ["clips/A", "clips/B removed", "files/F", "width"]
    # Bin files imported or updated meanwhile leave an answer valid; a removed one does not.
    actions = [{"type": "update", "key": ["files", {"id": "G"}], "value": {}}]
    assert bridged._conflicts(["files/F", "files/H removed", "files/G"], actions) == ["files/H removed", "files/G"]
    assert bridged.what_changed(before, None) is None


# -- linked clips the engine rendered -------------------------------------------

def test_get_linked_clip_reads_a_motion_block_and_notices_a_missing_render(bridge, tmp_path):  # noqa: F811
    render = tmp_path / "counter.mov"
    render.write_bytes(b"mov")
    props = {"$zenvi_json": json.dumps({"image": "F1", "to": 100})}
    f = bridge.add_file("video", path=str(render), zenvi_link=_link("zenvi-motion", props=props,
                                                                    motion={"block": "counter", "seconds": 3}))
    c = bridge.add_clip(f)
    receipt = bridge.call_receipt("get_linked_clip_tool", clip_id=c)
    data = receipt["data"]
    assert data["kind"] == "zenvi-motion" and data["kind_label"] == "Zenvi Motion" and data["state"] == "fresh"
    assert data["props"] == {"image": "F1", "to": 100}  # path-like keys decoded
    assert data["spec"] == {"motion": {"block": "counter", "seconds": 3}}
    assert data["rerender_tool"] == "update_motion_block_tool" and data["rerender_available"]
    assert data["clips"] == [c]
    render.unlink()
    data = bridge.call_receipt("get_linked_clip_tool", file_id=f)["data"]
    assert data["state"] == "stale" and data["render_present"] is False
    assert bridge.undo_steps_since_mark() == 0


def test_rerender_runs_the_kinds_engine_tool_as_one_undo_step(bridge, tmp_path):  # noqa: F811
    f = bridge.add_file("video", path=str(tmp_path / "counter.mov"), zenvi_link=_link("zenvi-motion"))
    bridge.add_clip(f)
    receipt = bridge.call_receipt("rerender_linked_clip_tool", file_id=f)
    assert receipt["status"] == "applied", receipt
    assert bridge.file(f)["path"].endswith("counter.mov.v2.mov")
    assert _calls(bridge.log)[-1]["tool"] == "update_motion_block_tool"
    assert _calls(bridge.log)[-1]["args"] == {"file_id": f}
    assert bridge.undo_steps_since_mark() == 1
    bridge.undo()
    assert bridge.file(f)["path"].endswith("counter.mov")


def test_an_identical_render_is_unchanged_and_leaves_no_undo_step(bridge, tmp_path):  # noqa: F811
    link = _link("zenvi-motion", render={"fingerprint": "sha256:same"})
    f = bridge.add_file("video", path=str(tmp_path / "counter.mov"), zenvi_link=link)
    receipt = bridge.call_receipt("rerender_linked_clip_tool", file_id=f)
    assert receipt["status"] == "unchanged"
    assert bridge.undo_steps_since_mark() == 0


def test_an_fx_render_re_renders_on_its_clip_with_the_whole_stack(bridge, tmp_path):  # noqa: F811
    effects = [{"effect": "radial_blur", "start": 0, "end": 0.3}, {"effect": "bloom"}]
    f = bridge.add_file("video", path=str(tmp_path / "fx.mov"), zenvi_link=_link("zenvi-fx", props={"effects": effects}))
    c = bridge.add_clip(f)
    assert bridge.call_receipt("rerender_linked_clip_tool", file_id=f)["status"] == "applied"
    assert _calls(bridge.log)[-1]["args"] == {"clip_id": c, "effects": effects}
    assert bridge.undo_steps_since_mark() == 1


def test_links_the_engine_does_not_render_are_refused_without_history(bridge, tmp_path):  # noqa: F811
    remotion = bridge.add_file("video", path=str(tmp_path / "intro.mov"), zenvi_link=_link("remotion"))
    receipt = bridge.call_receipt("rerender_linked_clip_tool", file_id=remotion)
    assert receipt["status"] in ("error", "refused") and "linked-clip support" in receipt["summary"]
    scene = bridge.add_file("video", path=str(tmp_path / "cube.mov"), zenvi_link=_link("blender"))
    receipt = bridge.call_receipt("rerender_linked_clip_tool", file_id=scene)
    # This CLI build lists no rerender_3d_scene_tool: say so instead of guessing.
    assert "no rerender_3d_scene_tool" in receipt["summary"]
    plain = bridge.add_file("video")
    assert "not a linked clip" in bridge.call_receipt("rerender_linked_clip_tool", file_id=plain)["summary"]
    assert "say which" in bridge.call_receipt("rerender_linked_clip_tool")["summary"]
    assert bridge.undo_steps_since_mark() == 0
    assert not [c for c in _calls(bridge.log) if c.get("tool") == "rerender_3d_scene_tool"]


# -- finding the CLI and Node ----------------------------------------------------

def _fake_node(folder, version):
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        pytest.skip("a shell-script node stand-in")
    node = folder / "node"
    node.write_text(f"#!/bin/sh\necho {version}\n")
    node.chmod(node.stat().st_mode | stat.S_IEXEC)
    return str(node)


def test_the_cli_is_the_named_one_else_the_bundled_one(tmp_path, monkeypatch):
    from classes import engine_bridge, info

    monkeypatch.setattr(info, "PATH", str(tmp_path / "app"))
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    with pytest.raises(engine_bridge.BridgeUnavailable, match="ZENVI_ENGINE_CLI") as missing:
        engine_bridge.find_cli({})
    assert os.path.join(str(tmp_path / "app"), "engine", "zenvi.mjs") in str(missing.value)
    user_copy = tmp_path / "user" / "engine" / "zenvi.mjs"
    user_copy.parent.mkdir(parents=True)
    user_copy.write_text("//")
    assert engine_bridge.find_cli({}) == str(user_copy)
    bundled = tmp_path / "app" / "engine" / "zenvi.mjs"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("//")
    assert engine_bridge.find_cli({}) == str(bundled)
    # A path the user names is the one they mean: no quiet fallback when it is wrong.
    with pytest.raises(engine_bridge.BridgeUnavailable, match="not at"):
        engine_bridge.find_cli({"ZENVI_ENGINE_CLI": str(tmp_path / "nope.mjs")})


def test_node_prefers_the_named_one_then_hyperframes_private_copy_and_needs_22(tmp_path, monkeypatch):
    from classes import engine_bridge, info

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.setattr(engine_bridge, "_node_majors", {})
    empty = {"PATH": str(tmp_path / "empty")}
    monkeypatch.setattr(engine_bridge, "node_candidates", _only_under(tmp_path, engine_bridge.node_candidates))
    with pytest.raises(engine_bridge.BridgeUnavailable, match="Node.js 22 or newer"):
        engine_bridge.find_node(empty)
    old = _fake_node(tmp_path / "bin", "v20.11.0")
    with pytest.raises(engine_bridge.BridgeUnavailable, match=r"v20"):
        engine_bridge.find_node({"PATH": str(tmp_path / "bin")})
    private = _fake_node(tmp_path / "user" / "hyperframes" / "node" / "bin", "v22.12.0")
    assert engine_bridge.find_node({"PATH": str(tmp_path / "bin")}) == private
    named = _fake_node(tmp_path / "named", "v24.1.0")
    assert engine_bridge.find_node({"ZENVI_NODE": str(tmp_path / "named"), "PATH": ""}) == named
    assert old != named
    # A bare name is looked up on PATH; a named Node that is not there is refused, not swapped for another.
    assert engine_bridge.find_node({"ZENVI_NODE": "node", "PATH": str(tmp_path / "named")}) == named
    with pytest.raises(engine_bridge.BridgeUnavailable, match="not at"):
        engine_bridge.find_node({"ZENVI_NODE": str(tmp_path / "gone" / "node"), "PATH": str(tmp_path / "bin")})


def _only_under(root, candidates):
    """node_candidates without this machine's own Node (Homebrew, Volta)."""
    def wrapped(env=None):
        return [p for p in candidates(env) if p.startswith(str(root))]
    return wrapped


def test_the_bridge_turns_on_from_the_setting_too(editor, monkeypatch):
    from classes import engine_bridge

    monkeypatch.delenv("ZENVI_ENGINE_BRIDGE", raising=False)
    assert engine_bridge.bridge_mode() == ""
    editor.settings["engine-bridge"] = "server"
    assert engine_bridge.bridge_mode() == "server"
    editor.settings["engine-bridge"] = "off"
    assert engine_bridge.bridge_mode() == ""


def test_bridged_handlers_drop_the_dispatchers_own_arguments(monkeypatch):
    from classes.editor_tools import engine_bridge as bridged

    linked = bridged._handler("get_linked_clip_tool", "", run=lambda clip_id="", file_id="": clip_id)
    assert linked(clip_id="C1", transaction_id="t-1", chat_session_id="s-1") == "C1"
    monkeypatch.setattr(bridged, "run_tool", lambda name, args: args)
    assert bridged._handler("add_sfx_tool", "")(kind="whoosh", transaction_id="t-1") == {"kind": "whoosh"}


# -- the editor-tool manifest ----------------------------------------------------

def test_the_manifest_lists_bridged_tools_only_when_asked(tmp_path):
    catalog = tmp_path / "catalog.json"
    out = subprocess.run([sys.executable, FAKE, "bridge", "--tools"], capture_output=True, text=True, check=True)
    catalog.write_text(out.stdout)
    script = os.path.join(ROOT, "scripts", "export_editor_tool_manifest.py")
    env = {k: v for k, v in os.environ.items() if k != "ZENVI_ENGINE_BRIDGE"}
    plain = json.loads(subprocess.run([sys.executable, script], capture_output=True, text=True, check=True,
                                      env=env).stdout)
    assert not [t for t in plain["tools"] if t.get("surface")]
    full = json.loads(subprocess.run([sys.executable, script, "--engine-catalog", str(catalog)], capture_output=True,
                                     text=True, check=True, env=env).stdout)
    bridged = {t["name"]: t for t in full["tools"] if t.get("surface")}
    assert set(bridged) == {"add_motion_template_tool", "update_motion_block_tool", "update_fx_tool",
                            "review_frames_tool", "get_linked_clip_tool", "rerender_linked_clip_tool"}
    assert bridged["review_frames_tool"]["read_only"] is True and bridged["review_frames_tool"]["workstream"] == "engine"
    assert bridged["get_linked_clip_tool"]["surface"] == "desktop"
    assert len(full["tools"]) == len(plain["tools"]) + len(bridged)


def test_the_manifest_refuses_a_catalog_without_tools(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text('{"protocol": 2}')
    script = os.path.join(ROOT, "scripts", "export_editor_tool_manifest.py")
    env = {k: v for k, v in os.environ.items() if k != "ZENVI_ENGINE_BRIDGE"}
    out = subprocess.run([sys.executable, script, "--engine-catalog", str(catalog)], capture_output=True, text=True,
                         env=env)
    assert out.returncode != 0 and "no tools list" in out.stderr and not out.stdout
