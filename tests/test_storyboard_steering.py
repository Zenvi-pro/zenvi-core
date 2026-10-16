"""An agent that skips every other hint must still be pointed at the shot list, not at fixed-interval sampling."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def test_the_server_instructions_and_playbook_start_from_the_shot_list():
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS
    from classes.edit_playbook import PLAYBOOK
    assert "storyboard_tool first" in SERVER_INSTRUCTIONS and "prefer overview" not in SERVER_INSTRUCTIONS
    assert "storyboard_tool first" in PLAYBOOK and "fixed interval" in PLAYBOOK


def test_inspect_media_points_at_storyboard_when_the_file_has_a_shot_list(tmp_path, monkeypatch):
    from classes.agent_tools import inspect as I
    from classes.media_index.store import Shelf
    shelf = Shelf(str(tmp_path))
    key = "ab" * 32
    shelf.write_json(key, "structure.json", {"shots": [{"id": 0, "start": 0, "end": 2}, {"id": 1, "start": 2, "end": 4}]})
    shelf.set_layer(key, "structure", version=1, status="ready")
    monkeypatch.setattr("classes.media_index.default_shelf", lambda: shelf)
    note = I._shot_list_hint({"fingerprint": {"sha256": key}})
    assert note and "storyboard_tool" in note[0] and "2 shots" in note[0]
    assert I._shot_list_hint({"fingerprint": {"sha256": "cd" * 32}}) == []
    assert I._shot_list_hint({}) == []
