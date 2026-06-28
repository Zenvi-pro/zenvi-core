"""Parser tests for the CLI agent runners (windows.agent_runners).

Feed recorded streaming-JSON fixtures through each runner's ``_handle_event``
and assert the emitted signal sequence — no subprocess or backend required.
``claude_stream.jsonl`` was captured from a real ``claude`` run against the
in-app MCP server; ``codex_stream.jsonl`` mirrors the Codex thread/turn/item
event schema.
"""

import json
import os

import pytest

pytest.importorskip("PyQt5.QtCore")
from PyQt5.QtWidgets import QApplication  # noqa: E402

_FIX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _collect(runner):
    events = []
    runner.token_received.connect(lambda t: events.append(("token", t)))
    runner.tool_started.connect(lambda c, n, a: events.append(("tool_started", n, c)))
    runner.tool_log.connect(lambda c, l: events.append(("tool_log", c)))
    runner.tool_completed.connect(lambda c, ok, r: events.append(("tool_completed", c, ok, r)))
    runner.response_ready.connect(lambda t: events.append(("response_ready", t)))
    runner.error_occurred.connect(lambda t: events.append(("error", t)))
    return events


def _feed(runner, fixture_name):
    with open(os.path.join(_FIX, fixture_name)) as fh:
        for line in fh:
            line = line.strip()
            if line:
                runner._handle_event(json.loads(line))


def test_claude_parser_emits_expected_signals(qapp):
    from windows.agent_runners import ClaudeCodeRunner
    runner = ClaudeCodeRunner()
    runner._session_id = "11111111-1111-1111-1111-111111111111"
    events = _collect(runner)
    _feed(runner, "claude_stream.jsonl")

    assert any(e[0] == "tool_started" and "list_files" in e[1] for e in events)
    assert any(e[0] == "tool_completed" and "FIXTURE" in e[3] for e in events)
    assert any(e[0] == "response_ready" and e[1] for e in events)
    # Extended thinking is surfaced as a collapsible "thinking" tool block.
    assert any(e[0] == "tool_started" and e[1] == "thinking" for e in events)


def test_codex_parser_emits_expected_signals(qapp):
    from windows.agent_runners import CodexRunner
    runner = CodexRunner()
    runner._session_id = "s1"
    events = _collect(runner)
    _feed(runner, "codex_stream.jsonl")

    assert runner._cli_session_id == "cdx-abc-123"  # captured for resume
    assert any(e[0] == "tool_started" and "list_files" in e[1] for e in events)
    assert any(e[0] == "tool_completed" and "FIXTURE" in e[3] for e in events)
    assert any(e[0] == "response_ready" and "3 files" in e[1] for e in events)


def test_strip_mcp_prefix():
    from windows.agent_runners import _strip_mcp_prefix
    assert _strip_mcp_prefix("mcp__zenvi-editor__add_track_tool") == "add_track_tool"
    assert _strip_mcp_prefix("Bash") == "Bash"
    assert _strip_mcp_prefix("") == ""


def test_codex_missing_cli_reports_friendly_error(qapp, monkeypatch):
    import windows.agent_runners as ar
    from windows.agent_runners import CodexRunner

    class _FakeServer:
        token = "tok"

        def start(self):
            return self

        def url(self):
            return "http://127.0.0.1:1/mcp"

    monkeypatch.setattr("classes.agent_mcp_server.get_mcp_server", lambda: _FakeServer())
    monkeypatch.setattr(ar.shutil, "which", lambda name: None)

    runner = CodexRunner()
    runner._session_id = "s2"
    events = _collect(runner)
    runner.run_request("hello", "")

    assert any(e[0] == "error" and "Codex CLI not found" in e[1] for e in events)
