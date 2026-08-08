"""Parser tests for the CLI agent runners (windows.agent_runners).

Feed recorded streaming-JSON fixtures through each runner's ``_handle_event``
and assert the emitted signal sequence — no subprocess or backend required.
``claude_stream.jsonl`` was captured from a real ``claude`` run against the
in-app MCP server; ``codex_stream.jsonl`` mirrors the Codex thread/turn/item
event schema.
"""

import json
import os
import sys

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


def test_detect_cli_not_installed(monkeypatch):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar.shutil, "which", lambda name: None)
    assert ar.detect_cli("claude") == {"installed": False, "version": None, "registered": False}


def test_detect_cli_installed_with_version(monkeypatch):
    import windows.agent_runners as ar

    class _FakeResult:
        stdout = "1.2.3\n"
        stderr = ""

    monkeypatch.setattr(ar.shutil, "which", lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **kw: _FakeResult())
    monkeypatch.setattr(ar, "_is_registered", lambda name: True)
    assert ar.detect_cli("claude") == {"installed": True, "version": "1.2.3", "registered": True}


def test_detect_cli_installed_version_check_fails(monkeypatch):
    import windows.agent_runners as ar

    def _raise(*a, **kw):
        raise OSError("timed out")

    monkeypatch.setattr(ar.shutil, "which", lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(ar.subprocess, "run", _raise)
    monkeypatch.setattr(ar, "_is_registered", lambda name: False)
    assert ar.detect_cli("codex") == {"installed": True, "version": None, "registered": False}


# --- registration detection + connect flow (Phase 8) -----------------------

def test_claude_is_registered_true_from_config_file(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "claude.json"
    cfg.write_text('{"mcpServers": {"zenvi": {"type": "http", "url": "http://127.0.0.1:7434/mcp"}}}')
    monkeypatch.setattr(ar, "_claude_config_path", lambda: str(cfg))

    def _fail_if_called(*a, **kw):
        raise AssertionError("should not shell out when the config file has the answer")

    monkeypatch.setattr(ar.subprocess, "run", _fail_if_called)
    assert ar._claude_is_registered() is True


def test_claude_is_registered_false_from_config_file(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "claude.json"
    cfg.write_text('{"mcpServers": {"magic": {"type": "stdio"}}}')
    monkeypatch.setattr(ar, "_claude_config_path", lambda: str(cfg))
    # Not in the config file → falls back to the CLI check, which we also
    # make say "absent" here so the overall result is a clean False.
    monkeypatch.setattr(ar, "_claude_is_registered_via_cli", lambda: False)
    assert ar._claude_is_registered() is False


def test_claude_is_registered_falls_back_to_cli_when_config_unreadable(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_claude_config_path", lambda: str(tmp_path / "missing.json"))
    monkeypatch.setattr(ar, "_claude_is_registered_via_cli", lambda: True)
    assert ar._claude_is_registered() is True


def test_claude_is_registered_via_cli_true(monkeypatch):
    import windows.agent_runners as ar

    class _FakeResult:
        stdout = "magic: npx foo - ✔ Connected\nzenvi: http://127.0.0.1:7434/mcp (HTTP) - ✔ Connected\n"

    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **kw: _FakeResult())
    assert ar._claude_is_registered_via_cli() is True


def test_claude_is_registered_via_cli_false_when_absent(monkeypatch):
    import windows.agent_runners as ar

    class _FakeResult:
        stdout = "magic: npx foo - ✔ Connected\n"

    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **kw: _FakeResult())
    assert ar._claude_is_registered_via_cli() is False


def test_claude_is_registered_via_cli_false_on_error(monkeypatch):
    import windows.agent_runners as ar

    def _raise(*a, **kw):
        raise OSError("not found")

    monkeypatch.setattr(ar.subprocess, "run", _raise)
    assert ar._claude_is_registered_via_cli() is False


def test_codex_is_registered_true(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "config.toml"
    cfg.write_text('[mcp_servers.zenvi_editor]\nurl = "http://127.0.0.1:7434/mcp"\n')
    monkeypatch.setattr(ar, "_codex_config_path", lambda: str(cfg))
    assert ar._codex_is_registered() is True


def test_codex_is_registered_false_when_missing(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_codex_config_path", lambda: str(tmp_path / "config.toml"))
    assert ar._codex_is_registered() is False


def test_register_claude_success(monkeypatch):
    import windows.agent_runners as ar

    calls = []

    def _fake_run(argv, **kw):
        calls.append(argv)
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(ar.subprocess, "run", _fake_run)
    ok, message = ar.register_claude(7434, "tok123")
    assert ok is True
    assert "claude" in message.lower() or "terminal" in message.lower()
    # remove ran before add, and add carries the transport/url/header/scope.
    assert calls[0][:3] == ["claude", "mcp", "remove"]
    add_argv = calls[1]
    assert add_argv[:3] == ["claude", "mcp", "add"]
    assert "http://127.0.0.1:7434/mcp" in add_argv
    assert "Authorization: Bearer tok123" in add_argv


def test_register_claude_add_failure_surfaces_stderr(monkeypatch):
    import windows.agent_runners as ar

    def _fake_run(argv, **kw):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "boom"
        return _R()

    monkeypatch.setattr(ar.subprocess, "run", _fake_run)
    ok, message = ar.register_claude(7434, "tok123")
    assert ok is False
    assert "boom" in message


def test_register_codex_creates_new_file(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(ar, "_codex_config_path", lambda: str(cfg))

    ok, message = ar.register_codex(7434, "tok123")
    assert ok is True
    assert "ZENVI_MCP_TOKEN=tok123" in message

    import tomllib
    data = tomllib.loads(cfg.read_text())
    assert data["mcp_servers"]["zenvi_editor"]["url"] == "http://127.0.0.1:7434/mcp"
    assert data["mcp_servers"]["zenvi_editor"]["bearer_token_env_var"] == "ZENVI_MCP_TOKEN"


def test_register_codex_preserves_unrelated_content_and_updates_stale_port(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[mcp_servers.context7]\n'
        'command = "npx"\n'
        'args = ["-y", "@upstash/context7-mcp"]\n'
        '\n'
        '[mcp_servers.zenvi_editor]\n'
        'url = "http://127.0.0.1:9999/mcp"\n'
        'bearer_token_env_var = "ZENVI_MCP_TOKEN"\n'
        '\n'
        '[mcp_servers.figma]\n'
        'url = "https://mcp.figma.com/mcp"\n'
        'bearer_token_env_var = "FIGMA_OAUTH_TOKEN"\n'
    )
    monkeypatch.setattr(ar, "_codex_config_path", lambda: str(cfg))

    ok, message = ar.register_codex(7434, "tok123")
    assert ok is True

    import tomllib
    data = tomllib.loads(cfg.read_text())
    # Stale port replaced.
    assert data["mcp_servers"]["zenvi_editor"]["url"] == "http://127.0.0.1:7434/mcp"
    # Unrelated sections untouched.
    assert data["mcp_servers"]["context7"]["command"] == "npx"
    assert data["mcp_servers"]["figma"]["bearer_token_env_var"] == "FIGMA_OAUTH_TOKEN"
    # Backup written.
    assert (tmp_path / "config.toml.zenvi-backup").exists()


def test_register_codex_refuses_to_touch_invalid_toml(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "config.toml"
    original = "this is not [valid toml"
    cfg.write_text(original)
    monkeypatch.setattr(ar, "_codex_config_path", lambda: str(cfg))

    ok, message = ar.register_codex(7434, "tok123")
    assert ok is False
    assert cfg.read_text() == original  # untouched


def test_run_request_popen_uses_explicit_utf8_encoding(qapp, monkeypatch):
    """Regression guard for the actual fix: subprocess.Popen(..., text=True)
    with no explicit encoding falls back to locale.getpreferredencoding(),
    which can resolve to ASCII depending on the *parent* process's locale —
    decoding happens on this side of the pipe, so nothing the child's env
    declares can influence it. That crashed the whole read loop with
    UnicodeDecodeError the moment real claude/codex output contained an em
    dash, arrow, or checkmark (all routine). Asserts Popen is called with an
    explicit encoding regardless of ambient locale, deterministically.
    """
    import windows.agent_runners as ar
    from windows.agent_runners import ClaudeCodeRunner

    class _FakeServer:
        token = "tok"

        def start(self):
            return self

        def url(self):
            return "http://127.0.0.1:1/mcp"

    monkeypatch.setattr("classes.agent_mcp_server.get_mcp_server", lambda: _FakeServer())
    monkeypatch.setattr(ar.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ClaudeCodeRunner, "_build_argv", lambda self, text: [sys.executable, "-c", ""])

    captured = {}
    real_popen = ar.subprocess.Popen

    def _spy_popen(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(ar.subprocess, "Popen", _spy_popen)

    runner = ClaudeCodeRunner()
    runner._session_id = "s3"
    runner.run_request("hello", "")

    assert captured.get("encoding") == "utf-8"
    assert captured.get("errors") == "replace"


def test_run_request_decodes_real_non_ascii_subprocess_output(qapp, monkeypatch):
    """End-to-end sanity check (not a locale-fault repro — see above test for
    that): a real subprocess emitting UTF-8 non-ASCII bytes decodes cleanly
    through the actual run_request/Popen/read-loop path, with no error."""
    import windows.agent_runners as ar
    from windows.agent_runners import ClaudeCodeRunner

    class _FakeServer:
        token = "tok"

        def start(self):
            return self

        def url(self):
            return "http://127.0.0.1:1/mcp"

    monkeypatch.setattr("classes.agent_mcp_server.get_mcp_server", lambda: _FakeServer())
    monkeypatch.setattr(ar.shutil, "which", lambda name: "/usr/bin/" + name)

    non_ascii = "done — ✔ all set"  # em dash + checkmark
    payload = json.dumps({"type": "result", "is_error": False, "result": non_ascii})
    argv = [sys.executable, "-c", "import sys; print(sys.argv[1])", payload]
    monkeypatch.setattr(ClaudeCodeRunner, "_build_argv", lambda self, text: argv)

    runner = ClaudeCodeRunner()
    runner._session_id = "s4"
    events = _collect(runner)
    runner.run_request("hello", "")

    responses = [e[1] for e in events if e[0] == "response_ready"]
    errors = [e[1] for e in events if e[0] == "error"]
    assert not errors, "run_request errored instead of decoding non-ASCII output: %r" % errors
    assert responses == [non_ascii]


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
