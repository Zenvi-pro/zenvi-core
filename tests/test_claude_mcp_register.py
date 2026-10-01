"""Headless tests for Claude Code MCP auto-registration (no real Qt)."""

from __future__ import annotations

import json


def test_ensure_claude_registered_skips_when_matching(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "zenvi": {
                "type": "http",
                "url": "http://127.0.0.1:7434/mcp",
                "headers": {"Authorization": "Bearer tok123"},
            }
        }
    }))
    monkeypatch.setattr(ar, "_claude_config_path", lambda: str(cfg))
    monkeypatch.setattr(ar, "_which_cli", lambda name: name)
    calls = []
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: calls.append(a) or type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )
    ok, message, changed = ar.ensure_claude_registered(7434, "tok123")
    assert ok is True
    assert changed is False
    assert calls == []
    assert "already" in message.lower()


def test_ensure_claude_registered_rewires_stale_port(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "zenvi": {
                "type": "http",
                "url": "http://127.0.0.1:9999/mcp",
                "headers": {"Authorization": "Bearer old"},
            }
        }
    }))
    monkeypatch.setattr(ar, "_claude_config_path", lambda: str(cfg))
    monkeypatch.setattr(ar, "_which_cli", lambda name: name)

    def _fake_run(argv, **kw):
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(ar.subprocess, "run", _fake_run)
    ok, _message, changed = ar.ensure_claude_registered(7434, "tok123")
    assert ok is True
    assert changed is True


def test_ensure_claude_registered_without_cli(monkeypatch):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_which_cli", lambda name: None)
    ok, message, changed = ar.ensure_claude_registered(7434, "tok")
    assert ok is False
    assert changed is False
    assert "not found" in message.lower()
