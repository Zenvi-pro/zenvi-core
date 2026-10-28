"""End to end through the real zenvi CLI: an engine-only tool on a saved project.

Skipped unless ``ZENVI_ENGINE_CLI`` points at a built CLI bundle
(zenvi-web: ``node packages/cli/scripts/build.mjs --no-plugin``, then
``packages/cli/dist/zenvi.mjs``) and Node is on PATH (or ``ZENVI_NODE``).
"""

import json
import os
import shutil

import pytest

CLI = os.environ.get("ZENVI_ENGINE_CLI", "")
NODE = os.environ.get("ZENVI_NODE") or shutil.which("node")
pytestmark = pytest.mark.skipif(not (CLI and os.path.isfile(CLI) and NODE),
                                reason="needs ZENVI_ENGINE_CLI (a built zenvi.mjs) and Node")


@pytest.fixture
def saved(editor, tmp_path, monkeypatch):
    """The editor on a project saved at tmp/promo.zvn, the real CLI as its bridge."""
    import qt_api
    from classes import engine_bridge, info, tool_handlers
    from classes.editor_tools import engine_bridge as bridged

    # The headless qt_api stub has no file helpers: give the desktop's writer and reader local files.
    def write_file_text(path, content):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def read_file_text(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    monkeypatch.setattr(qt_api, "write_file_text", write_file_text, raising=False)
    monkeypatch.setattr(qt_api, "read_file_text", read_file_text, raising=False)
    monkeypatch.setattr(qt_api, "is_content_uri", lambda path: False, raising=False)

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"))
    monkeypatch.setattr(bridged, "BRIDGED", {})
    client = engine_bridge.EngineBridgeClient(NODE, os.path.abspath(CLI), "server", timeout=60)
    engine_bridge.set_client(client)
    handlers = bridged.register_tools(set(tool_handlers.AGENT_TOOL_HANDLERS), catalog=bridged.load_catalog(client))
    for name, handler in handlers.items():
        monkeypatch.setitem(tool_handlers.TOOL_HANDLERS, name, handler)
    monkeypatch.setattr(tool_handlers, "BACKGROUND_SAFE_TOOLS", tool_handlers.BACKGROUND_SAFE_TOOLS | frozenset(handlers))
    path = str(tmp_path / "promo.zvn")
    editor.store.write_to_file(path, editor.store._data, path_mode="relative")
    editor.store.current_filepath = path
    editor.handlers = handlers
    yield editor
    engine_bridge.set_client(None)


def test_motion_template_lands_in_one_undo_step_and_survives_save_and_reopen(saved, tmp_path):
    assert "add_motion_template_tool" in saved.handlers
    assert "list_files_tool" not in saved.handlers  # the desktop's own tools are never bridged

    receipt = saved.call_receipt("add_motion_template_tool", template="end_card", text="Northwind",
                                 subtitle="Brews in four minutes", position_seconds="1")
    assert receipt["status"] == "applied", receipt
    layers = receipt["data"]["layers"]
    assert len(layers) >= 2
    ids = {layer["timeline_clip_id"] for layer in layers}
    clips = {c["id"]: c for c in saved.clips()}
    assert ids <= set(clips)
    assert saved.undo_steps_since_mark() == 1
    # Each clip reads a drawing the engine wrote into the project's assets folder.
    assets = str(tmp_path / "promo_assets")
    for clip_id in ids:
        path = clips[clip_id]["reader"]["path"]
        assert path.startswith(assets) and os.path.isfile(path), path
        assert saved.file(clips[clip_id]["file_id"]) is not None

    # Save (the desktop's writer, paths relative) and reopen (paths absolute again).
    path = saved.store.current_filepath
    saved.store.write_to_file(path, saved.store._data, path_mode="relative")
    with open(path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert ids <= {c["id"] for c in on_disk["clips"]}
    assert all(not os.path.isabs(c["reader"]["path"]) for c in on_disk["clips"] if c["id"] in ids)
    # read_from_file's path step (the harness store skips __init__, so not its repair scan).
    reopened = json.loads(saved.store.convert_paths_to_absolute(path, json.dumps(on_disk)))
    reopened_clips = {c["id"]: c for c in reopened["clips"]}
    assert ids <= set(reopened_clips)
    assert all(os.path.isfile(reopened_clips[i]["reader"]["path"]) for i in ids)

    saved.undo()
    assert not ids & {c["id"] for c in saved.clips()}
    assert saved.undo_steps_since_mark() == 0


def test_an_engine_refusal_leaves_no_undo_step_and_no_files(saved, tmp_path):
    receipt = saved.call_receipt("add_motion_template_tool", template="word_scroll", words='["Only"]')
    assert receipt["status"] in ("refused", "error"), receipt
    assert saved.undo_steps_since_mark() == 0
    title = tmp_path / "promo_assets" / "title"
    assert not title.exists() or not any(title.iterdir())


def test_the_new_engine_tools_are_listed_and_native_twins_are_not(saved):
    for name in ("add_sfx_tool", "apply_motion_transition_tool", "speed_ramp_tool", "align_match_cut_tool"):
        assert name in saved.handlers, name
    for twin in ("move_clip_tool", "trim_clip_tool", "split_clip_tool", "add_transition_tool"):
        assert twin not in saved.handlers, twin


def test_a_sound_effect_is_one_undo_step_with_its_audio_in_the_assets_folder(saved, tmp_path):
    receipt = saved.call_receipt("add_sfx_tool", recipe="whoosh_short", at_seconds=1)
    assert receipt["status"] == "applied", receipt
    clip = saved.clip(receipt["data"]["clip_id"])
    assert clip is not None and saved.undo_steps_since_mark() == 1
    path = saved.file(clip["file_id"])["path"]
    assert path.startswith(str(tmp_path / "promo_assets")) and os.path.isfile(path), path
    saved.undo()
    assert saved.clip(receipt["data"]["clip_id"]) is None


def test_a_motion_transition_on_a_cut_is_one_undo_step(saved):
    f = saved.add_file("video", duration=4)
    a = saved.add_clip(f, position=0.0, end=2.0)
    b = saved.add_clip(f, position=2.0, start=2.0, end=4.0)
    before = {i: json.dumps(saved.clip(i), sort_keys=True) for i in (a, b)}
    receipt = saved.call_receipt("apply_motion_transition_tool", preset="whip", clip1_id=a)
    assert receipt["status"] == "applied", receipt
    assert saved.undo_steps_since_mark() == 1
    assert {i: json.dumps(saved.clip(i), sort_keys=True) for i in (a, b)} != before
    # Undone exactly (no leftover zenvi_motion_transitions tag), so the cut takes a transition again.
    saved.undo()
    assert {i: json.dumps(saved.clip(i), sort_keys=True) for i in (a, b)} == before
    again = saved.call_receipt("apply_motion_transition_tool", preset="zoom_in", clip1_id=a)
    assert again["status"] == "applied", again


def test_a_ten_call_session_saves_small_and_undoes_and_redoes_exactly(saved, tmp_path):
    """Each undo entry is about the size of what its call changed (E2E round 2, N1: 34 MB after ten calls)."""
    f = saved.add_file("video", duration=120)
    clips = [saved.add_clip(f, position=float(i), start=float(i), end=i + 1.0) for i in range(40)]
    path = saved.store.current_filepath
    saved.store.write_to_file(path, saved.store._data, path_mode="relative")
    saved.manager.actionHistory.clear()  # the session starts on the project as opened

    def state():
        return json.dumps({k: v for k, v in saved.store._data.items() if k != "history"})

    start = state()
    calls = [("add_sfx_tool", {"recipe": "whoosh_short", "at_seconds": 1}),
             ("apply_motion_transition_tool", {"preset": "whip", "clip1_id": clips[3]}),
             ("add_sfx_tool", {"recipe": "impact_punch", "at_seconds": 4}),
             ("apply_motion_transition_tool", {"preset": "zoom_in", "clip1_id": clips[8]}),
             ("add_motion_template_tool", {"template": "end_card", "text": "Northwind", "position_seconds": "30"}),
             ("add_sfx_tool", {"recipe": "riser_noise", "at_seconds": 10}),
             ("apply_motion_transition_tool", {"preset": "glitch", "clip1_id": clips[15]}),
             ("auto_sound_design_tool", {"intensity": "hype", "sources": "procedural"}),
             ("add_sfx_tool", {"recipe": "pop", "at_seconds": 20}),
             ("apply_motion_transition_tool", {"preset": "flash_white", "clip1_id": clips[25]})]
    for name, args in calls:
        saved.mark()
        receipt = saved.call_receipt(name, **args)
        assert receipt["status"] == "applied", (name, receipt)
        assert saved.undo_steps_since_mark() == 1, name
    end = state()

    saved.manager.save_history(saved.store, 50)
    saved.store.write_to_file(path, saved.store._data, path_mode="relative")
    bare = str(tmp_path / "bare.zvn")
    saved.store.write_to_file(bare, dict(saved.store._data, history={"undo": [], "redo": []}), path_mode="relative")
    assert os.path.getsize(path) < 2 * os.path.getsize(bare), (os.path.getsize(path), os.path.getsize(bare))

    for _ in calls:
        saved.undo()
    assert not saved.manager.actionHistory
    assert state() == start
    for _ in calls:
        saved.redo()
    assert state() == end
