"""An assistant turn cannot reach the tools that drive the assistant or the backend.

send_assistant_prompt_tool and zenvi_api_request are on the editor MCP for
external harnesses (Claude Code, Codex) testing Zenvi. The CLI agents Zenvi runs
in its own chat dock connect to that same MCP, so while any assistant turn is
running both tools refuse: the assistant would otherwise prompt itself or spend
the user's credits through the backend API.
"""

import ast
import pathlib

from classes import assistant_turns
from classes.agent_api_proxy import send_assistant_prompt_tool, zenvi_api_request

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def test_the_harness_tools_refuse_during_an_assistant_turn():
    with assistant_turns.turn():
        assert assistant_turns.running()
        for out in (send_assistant_prompt_tool("hi"), zenvi_api_request("GET", "/api/v1/models")):
            assert out.startswith("Error") and "assistant turn" in out, out
    assert not assistant_turns.running()


def test_outside_a_turn_the_prompt_tool_is_not_refused_for_that_reason():
    out = send_assistant_prompt_tool("")
    assert "assistant turn" not in out


def _decorators(path, cls, method):
    tree = ast.parse((SRC / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method:
                    return [ast.unparse(d) for d in item.decorator_list]
    raise AssertionError(f"{cls}.{method} not found")


def test_every_assistant_turn_entry_point_counts_as_a_turn():
    for path, cls in (("windows/ai_chat_ui.py", "AIChatWorker"),
                      ("windows/agent_runners.py", "BaseAgentRunner")):
        assert "assistant_turns.counts_as_turn" in _decorators(path, cls, "run_request"), cls
