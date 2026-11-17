"""Every way an agent learns the contract: the Claude append prompt, the Codex/ChatGPT first-turn prefix, and the playbook that
Cursor CLI, OpenCode, Hermes and the Zenvi Assistant read. Short, one doctrine, and naming only core tools (AGENTS.md)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import edit_playbook as P  # noqa: E402
from classes.agent_tools.core_tools import CORE_TOOL_NAMES  # noqa: E402


def tool_names(text):
    return set(re.findall(r"\b[a-z][a-z_]*_tool\b", text))


def test_the_one_sentence_is_short_and_names_only_core_tools():
    from windows.agent_runners import _edit_contract_prompt
    text = _edit_contract_prompt()
    assert len(text) <= 350 and "begin_edit_tool" in text and tool_names(text) <= set(CORE_TOOL_NAMES)
    assert "Narrow asks" in text, "a narrow ask must not be told to run begin_edit_tool"


def test_claude_gets_it_once_in_the_editor_block_before_the_bash_recipes():
    from windows.agent_runners import _claude_code_system_prompt, _edit_contract_prompt
    prompt = _claude_code_system_prompt()
    assert prompt.count(_edit_contract_prompt()) == 1
    assert prompt.index(_edit_contract_prompt()) < prompt.index("HEIC"), "inside the Zenvi editor block, ahead of the shell recipes"
    assert "ingest_web_video_tool" in prompt and "ToolSearch" in prompt, "the existing steering is intact"


def test_codex_and_chatgpt_get_it_on_the_first_turn_only():
    from windows.agent_runners import ChatGPTPlanRunner, CodexRunner, _agent_import_prompt, _edit_contract_prompt
    for cls in (CodexRunner, ChatGPTPlanRunner):
        runner = object.__new__(cls)         # the suite stubs Qt, so the QObject __init__ cannot run: set what _build_argv reads
        runner.__dict__.update(_server=None, _mode="", _model_id="", _effort="", _cli_path="codex", _cli_started=False,
                               _cli_id_from_cli=False, _cli_session_id="")
        runner._build_argv("cut the intro")
        first = runner._stdin_prompt
        assert first.startswith(_agent_import_prompt()) and first.endswith("cut the intro") and _edit_contract_prompt() in first
        runner._cli_started = True
        runner._cli_id_from_cli = True
        runner._cli_session_id = "thread-1"
        runner._build_argv("now add music")
        assert runner._stdin_prompt == "now add music", "resumed turns do not repeat the guidance"


# ------------------------------------------------------------------ the playbook (MCP instructions and the Assistant manifest)
def test_the_playbook_points_at_begin_edit_tool_once_in_the_brief_step():
    assert P.PLAYBOOK.count("begin_edit_tool") == 1
    step = [ln for ln in P.PLAYBOOK.splitlines() if ln.startswith("1. Brief:")][0]
    assert "begin_edit_tool" in step and "set_edit_brief_tool" in step and "placing clips waits" in step


def test_the_playbook_stays_inside_its_caps_with_the_pointer():
    assert len(P.PLAYBOOK) < 7000 and len(P.PLAYBOOK.split()) < 1200


def test_the_playbook_names_only_tools_that_exist_and_the_brief_tools_are_core():
    from classes import tool_handlers
    from classes.editor_tools import REGISTRY
    named = tool_names(P.PLAYBOOK)
    assert {"begin_edit_tool", "set_edit_brief_tool"} <= named
    assert not named - (set(REGISTRY) | set(tool_handlers.AGENT_TOOL_HANDLERS) | {"run_tool"})
    assert {"begin_edit_tool", "set_edit_brief_tool"} <= set(CORE_TOOL_NAMES)


def test_the_mcp_instructions_carry_the_pointer_exactly_once():
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS
    assert SERVER_INSTRUCTIONS.count("begin_edit_tool surveys and saves it") == 1


def test_the_assistant_manifest_carries_the_same_playbook():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import export_editor_tool_manifest as ex
    m = ex.build_manifest()
    assert "begin_edit_tool surveys and saves it" in m["playbook"]["text"] and "begin_edit_tool" in m["core_tools"]


def test_the_playbook_still_leaves_room_for_judgement_and_the_narrow_scope_rule():
    assert "guidance, not a script" in P.PLAYBOOK and "A narrow request" in P.PLAYBOOK and "Do not overshoot" in P.PLAYBOOK


# ------------------------------------------------------------------ the tool's own words
def test_begin_edit_tools_text_names_only_core_tools():
    from classes.editor_tools import REGISTRY
    spec = REGISTRY["begin_edit_tool"]
    texts = [spec.func.__doc__ or ""]
    texts += [p.get("description", "") for p in spec.schema["properties"].values()]
    for t in texts:
        assert tool_names(t) <= set(CORE_TOOL_NAMES), tool_names(t) - set(CORE_TOOL_NAMES)


@pytest.mark.parametrize("name", ["begin_edit_tool", "get_edit_brief_tool", "set_edit_brief_tool"])
def test_every_property_of_the_brief_tools_is_described(name):
    from classes.editor_tools import REGISTRY
    props = REGISTRY[name].schema["properties"]
    assert all(p.get("description") for p in props.values())
