"""The editing playbook: every tool it names exists, it stays short, and both agent surfaces carry the identical text."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from classes import edit_playbook as P  # noqa: E402


def test_the_hash_matches_the_text_so_a_stale_copy_is_detectable():
    assert P.PLAYBOOK_SHA256 == hashlib.sha256(P.PLAYBOOK.encode("utf-8")).hexdigest()
    m = P.playbook_manifest()
    assert m == {"version": P.PLAYBOOK_VERSION, "sha256": P.PLAYBOOK_SHA256, "text": P.PLAYBOOK}


def test_every_tool_the_playbook_names_is_a_real_registered_tool():
    from classes import tool_handlers
    from classes.editor_tools import REGISTRY

    named = set(re.findall(r"\b[a-z][a-z_]*_tool\b", P.PLAYBOOK))
    core = {"review_edit_tool", "get_project_overview_tool", "search_footage_tool", "get_segment_dossier_tool", "balance_mix_tool", "harmonize_look_tool",
            "sync_cuts_to_beats_tool", "audition_music_tool", "analyze_music_tool", "set_edit_brief_tool", "get_edit_brief_tool", "listen_tool"}
    assert core <= named, f"the playbook must name the tools it relies on: {sorted(core - named)}"
    real = set(REGISTRY) | set(tool_handlers.AGENT_TOOL_HANDLERS)
    assert not named - real, f"the playbook names tools that do not exist: {sorted(named - real)}"


def test_the_playbook_covers_the_scope_rule_the_six_layers_the_loop_and_the_trust_rule():
    for needle in ("narrow request", "STORY", "PICTURE", "SOUND", "TEXT", "GRAPHICS/EFFECTS", "DELIVERY", "THE LOOP", "review_edit_tool",
                   "WHEN EACH LAYER IS NEEDED", "TRUST", "LONG-FORM", "Measured facts"):
        assert needle in P.PLAYBOOK, needle


def test_it_stays_short_enough_for_every_agent_to_carry():
    assert len(P.PLAYBOOK) < 7000 and len(P.PLAYBOOK.split()) < 1100


def test_it_never_orders_a_step_without_leaving_room_for_judgement():
    assert "guidance, not a script" in P.PLAYBOOK and "only for a reason you can state" in P.PLAYBOOK and "reorder" in P.PLAYBOOK


def test_claude_code_gets_the_playbook_in_the_server_instructions():
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS
    assert P.PLAYBOOK in SERVER_INSTRUCTIONS and SERVER_INSTRUCTIONS.startswith("These tools drive a live video editor")
    assert SERVER_INSTRUCTIONS.count("EDITING PLAYBOOK") == 1


def test_the_tool_manifest_carries_the_same_playbook_for_the_zenvi_assistant():
    import export_editor_tool_manifest as ex
    manifest = ex.build_manifest()
    assert manifest["playbook"]["text"] == P.PLAYBOOK and manifest["playbook"]["sha256"] == P.PLAYBOOK_SHA256 and manifest["manifest_version"] == 1
