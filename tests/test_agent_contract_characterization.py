"""Characterization of Phase 3 agent-contract defects.

Tests that Phase 3 has fixed assert the desired behaviour directly.
Remaining xfails (if any) document unfinished work.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest

from classes.agent_mcp_server import iter_tool_defs
from classes import agent_gap_log
from classes import tool_handlers
from classes.agent_tools.schema import TOOL_SCHEMAS, get_schema
from classes.agent_tools.receipt import is_error_result, parse_receipt


def test_kwargs_only_tools_have_explicit_schemas():
    """Defect A fixed: every tool has an explicit schema with properties or {}."""
    for name in tool_handlers.AGENT_TOOL_HANDLERS:
        schema = get_schema(name)
        assert schema is not None, name
        assert "properties" in schema, name


def test_every_advertised_schema_rejects_unknown_keys():
    """Defect B fixed."""
    for name in tool_handlers.AGENT_TOOL_HANDLERS:
        schema = get_schema(name)
        assert schema.get("additionalProperties") is False, name


def test_execute_tool_returns_contract_3_receipt_json():
    """Defect C fixed: execute_tool always returns receipt JSON."""
    with patch.object(tool_handlers, "TOOL_HANDLERS", {
        "list_files_tool": lambda **_kw: "FIXTURE_FILES",
    }), patch.object(tool_handlers, "READ_ONLY_TOOLS", frozenset({"list_files_tool"})), \
         patch.object(tool_handlers, "_UNGROUPED_TOOLS", frozenset({"list_files_tool"})), \
         patch.object(tool_handlers, "BACKGROUND_SAFE_TOOLS", frozenset()), \
         patch("classes.tool_handlers.QThread", None), \
         patch.object(tool_handlers, "_get_app", return_value=MagicMock(
             updates=MagicMock(actionHistory=[], transaction_id=None),
             project=MagicMock(**{"get.return_value": []}),
             thread=MagicMock(return_value=object()),
         )):
        # Re-bind through real execute_tool path
        raw = tool_handlers.execute_tool("list_files_tool", {})
    payload = parse_receipt(raw)
    assert payload is not None
    assert payload["contract"] == 3
    assert payload["status"] in ("applied", "unchanged", "refused", "error")
    assert "summary" in payload


def test_schema_failure_never_opens_transaction():
    """Defect D (schema path): invalid args refuse before _atomic."""
    app = MagicMock()
    app.updates.transaction_id = None
    app.updates.actionHistory = []
    app.thread.return_value = object()
    called = {"handler": False}

    def boom(**_kw):
        called["handler"] = True
        return "should not run"

    with patch.object(tool_handlers, "TOOL_HANDLERS", {
        "add_clip_to_timeline_tool": boom,
    }), patch.object(tool_handlers, "READ_ONLY_TOOLS", frozenset()), \
         patch.object(tool_handlers, "_UNGROUPED_TOOLS", frozenset()), \
         patch.object(tool_handlers, "BACKGROUND_SAFE_TOOLS", frozenset({
             "add_clip_to_timeline_tool",
         })), \
         patch("classes.tool_handlers.QThread", None), \
         patch.object(tool_handlers, "_get_app", return_value=app):
        raw = tool_handlers.execute_tool(
            "add_clip_to_timeline_tool",
            {"file_id": "x", "__bogus__": 1},
        )
    assert called["handler"] is False
    assert is_error_result(raw)
    assert app.updates.transaction_id is None
    receipt = parse_receipt(raw)
    assert receipt["status"] == "refused"
    assert receipt["undoSteps"] == 0


def test_set_clip_volume_does_not_mint_its_own_transaction_id():
    """Defect E fixed."""
    src = inspect.getsource(tool_handlers.set_clip_volume)
    assert "transaction_id = str(uuid_module.uuid4())" not in src
    duck_src = inspect.getsource(tool_handlers.duck_under_speech)
    assert "transaction_id = str(uuid_module.uuid4())" not in duck_src


def test_gap_log_tools_are_registered():
    """Defect F fixed."""
    names = set(tool_handlers.AGENT_TOOL_HANDLERS)
    for required in (
        "add_effect_tool",
        "add_title_tool",
        "set_project_setting_tool",
        "set_keyframes_tool",
    ):
        assert required in names
    assert agent_gap_log.classify_gap("add a title card please") is None
    assert agent_gap_log.classify_gap("please change the project fps to 24") is None
    assert agent_gap_log.classify_gap("duck the audio under speech") is None


def test_iter_tool_defs_count_matches_registry():
    defs = iter_tool_defs()
    editor = {d["name"] for d in defs if d["name"] in tool_handlers.AGENT_TOOL_HANDLERS}
    assert editor == set(tool_handlers.AGENT_TOOL_HANDLERS)


def test_priority_tools_in_schema_registry():
    for name in (
        "add_clip_to_timeline_tool",
        "delete_from_timeline_tool",
        "export_video_tool",
    ):
        assert name in TOOL_SCHEMAS
