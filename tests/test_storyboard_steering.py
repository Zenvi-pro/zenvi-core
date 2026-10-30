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
