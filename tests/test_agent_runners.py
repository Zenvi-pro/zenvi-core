"""Parser tests for the CLI agent runners (windows.agent_runners).

Feed recorded streaming-JSON fixtures through each runner's ``_handle_event``
and assert the emitted signal sequence ΓÇö no subprocess or backend required.
``claude_stream.jsonl`` was captured from a real ``claude`` run against the
in-app MCP server; ``codex_stream.jsonl`` mirrors the Codex thread/turn/item
event schema.
"""

import json
import os
import sys
import types

import pytest

from _qt_support import skip_without_pyqt5  # noqa: E402

skip_without_pyqt5()
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
    monkeypatch.setattr(ar, "_cli_install_dirs", lambda: [])
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
    # Not in the config file ΓåÆ falls back to the CLI check, which we also
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
        stdout = "magic: npx foo - Γ£ö Connected\nzenvi: http://127.0.0.1:7434/mcp (HTTP) - Γ£ö Connected\n"

    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **kw: _FakeResult())
    assert ar._claude_is_registered_via_cli() is True


def test_claude_is_registered_via_cli_false_when_absent(monkeypatch):
    import windows.agent_runners as ar

    class _FakeResult:
        stdout = "magic: npx foo - Γ£ö Connected\n"

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
    monkeypatch.setattr(ar, "_which_cli", lambda name: name)
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
    monkeypatch.setattr(ar, "_which_cli", lambda name: name)
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
    which can resolve to ASCII depending on the *parent* process's locale ΓÇö
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
    """End-to-end sanity check (not a locale-fault repro ΓÇö see above test for
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

    non_ascii = "done ΓÇö Γ£ö all set"  # em dash + checkmark
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
    monkeypatch.setattr(ar, "_which_cli", lambda name: None)

    runner = CodexRunner()
    runner._session_id = "s2"
    events = _collect(runner)
    runner.run_request("hello", "")

    assert any(e[0] == "error" and "Codex CLI not found" in e[1] for e in events)


# ΓöÇΓöÇ Model selection ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def test_claude_argv_carries_model_and_skips_permission_prompts(qapp, monkeypatch):
    """The picked model reaches the CLI, and the agent never waits on a
    permission prompt there is no terminal to answer."""
    import windows.agent_runners as ar
    from windows.agent_runners import ClaudeCodeRunner

    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")
    runner = ClaudeCodeRunner()
    runner._session_id = "s5"
    runner._cli_session_id = "s5"
    runner._model_id = runner._coerce_model("claude-sonnet-5")
    argv = runner._build_argv("hi")

    assert "--dangerously-skip-permissions" in argv
    assert "--permission-mode" not in argv, "would conflict with the skip flag"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"


def test_claude_and_codex_argv_add_typical_media_dirs(qapp, monkeypatch, tmp_path):
    """Claude/Codex cwd stays the project; --add-dir exposes Desktop/Downloads/ΓÇª"""
    import windows.agent_runners as ar
    from windows.agent_runners import ClaudeCodeRunner, CodexRunner

    desktop = tmp_path / "Desktop"
    downloads = tmp_path / "Downloads"
    desktop.mkdir()
    downloads.mkdir()
    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")
    import classes.file_drop as file_drop
    monkeypatch.setattr(file_drop, "media_add_dirs", lambda home=None: [str(desktop), str(downloads)])

    claude = ClaudeCodeRunner()
    claude._session_id = "s5"
    claude._cli_session_id = "s5"
    claude_argv = claude._build_argv("hi")
    assert claude_argv.count("--add-dir") == 2
    assert str(desktop) in claude_argv
    assert str(downloads) in claude_argv

    codex = CodexRunner()
    class _FakeServer:
        token = "tok"
        def url(self):
            return "http://127.0.0.1:7434/mcp"
    codex._server = _FakeServer()
    codex_argv = codex._build_argv("hi")
    assert codex_argv.count("--add-dir") == 2
    assert str(desktop) in codex_argv
    assert str(downloads) in codex_argv


def test_runner_drops_a_model_id_from_another_backend(qapp):
    """A tab switched over from Zenvi still holds a Zenvi model id in the
    shared picker; passing it to the CLI would just make the CLI error out."""
    from windows.agent_runners import ClaudeCodeRunner, CodexRunner

    claude = ClaudeCodeRunner()
    assert claude._coerce_model("claude-opus-5") == "claude-opus-5"
    assert claude._coerce_model("gemini-2.0-flash") == ""
    assert claude._coerce_model("") == ""

    # Codex publishes no lineup, so nothing is ever forced on it.
    assert CodexRunner()._coerce_model("gpt-5") == ""


def test_claude_argv_omits_model_when_none_picked(qapp, monkeypatch):
    import windows.agent_runners as ar
    from windows.agent_runners import ClaudeCodeRunner

    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")
    runner = ClaudeCodeRunner()
    runner._session_id = "s6"
    runner._cli_session_id = "s6"
    assert "--model" not in runner._build_argv("hi")


def test_models_for_backend_matches_the_picker_contract(qapp):
    """chat.js reads id/name off every entry and marks exactly one default."""
    from windows.agent_runners import (
        BACKEND_CLAUDE, BACKEND_CODEX, BACKEND_CURSOR, models_for_backend,
    )

    claude = models_for_backend(BACKEND_CLAUDE)
    assert claude, "Claude Code must offer a model list"
    assert all(m["id"] and m["name"] for m in claude)
    assert len({m["id"] for m in claude}) == len(claude), "duplicate model ids"
    assert sum(1 for m in claude if m.get("default")) == 1

    assert models_for_backend(BACKEND_CODEX) == []
    # Cursor's real list comes from the CLI; built in is only "CLI default".
    assert [m["id"] for m in models_for_backend(BACKEND_CURSOR)] == ["cli-default"]
    assert models_for_backend("zenvi") == []

    # Callers mutate what they get (the JS bridge tags entries), so the
    # catalogue itself must not be handed out by reference.
    claude[0]["name"] = "mutated"
    assert models_for_backend(BACKEND_CLAUDE)[0]["name"] != "mutated"


# ΓöÇΓöÇ Cancel ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ

def test_cancel_does_not_disable_the_tab_for_later_messages(qapp, monkeypatch):
    """Stop must silence the turn in flight and nothing more ΓÇö a cancelled tab
    still has to answer the next message the user sends."""
    import windows.agent_runners as ar
    from windows.agent_runners import ClaudeCodeRunner

    class _FakeServer:
        token = "tok"

        def start(self):
            return self

        def url(self):
            return "http://127.0.0.1:1/mcp"

    monkeypatch.setattr("classes.agent_mcp_server.get_mcp_server", lambda: _FakeServer())
    monkeypatch.setattr(ar.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")

    runner = ClaudeCodeRunner()
    runner._session_id = "s7"
    events = _collect(runner)

    # A turn is cancelled: whatever the dying subprocess still emits is dropped.
    runner._cancelled = True
    runner._emit_response("late output")
    runner._emit_error("late failure")
    assert events == []

    # The next message starts clean again.
    argv = [sys.executable, "-c", "print('{\"type\":\"result\",\"result\":\"ok\"}')"]
    monkeypatch.setattr(ClaudeCodeRunner, "_build_argv", lambda self, text: argv)
    runner.run_request("next message", "")
    assert [e[1] for e in events if e[0] == "response_ready"] == ["ok"]


def test_cancel_signals_the_whole_process_group(qapp, monkeypatch):
    """The CLI spawns children that keep driving the editor through MCP, so
    Stop has to take down the group, not just the CLI process."""
    import os
    import signal
    from windows.agent_runners import ClaudeCodeRunner

    killed = {}

    class _Proc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            killed["terminate"] = True

        def kill(self):
            killed["kill"] = True

    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.setdefault("group", (pgid, sig)))

    runner = ClaudeCodeRunner()
    runner._proc = _Proc()
    runner.cancel()

    assert killed.get("group") == (4242, signal.SIGTERM)
    assert "terminate" not in killed, "group kill succeeded; no need to fall back"
    assert runner._cancelled
    assert not runner._stopping, "cancel is not a shutdown ΓÇö the tab stays usable"


def test_cancel_on_windows_uses_taskkill(qapp, monkeypatch):
    """os.killpg does not exist on Windows; Stop must kill the process tree."""
    import subprocess
    from windows.agent_runners import ClaudeCodeRunner

    called = {}

    class _Proc:
        pid = 4242

        def poll(self):
            return None

        def terminate(self):
            called["terminate"] = True

        def kill(self):
            called["kill"] = True

    monkeypatch.setattr("windows.agent_runners.sys.platform", "win32")

    def fake_call(cmd, **kwargs):
        called["cmd"] = cmd
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)

    runner = ClaudeCodeRunner()
    runner._proc = _Proc()
    runner.cancel()

    assert called.get("cmd")[:4] == ["taskkill", "/PID", "4242", "/T"]
    assert "/F" in called["cmd"]
    assert "terminate" not in called
    assert runner._cancelled


def test_codex_accumulates_several_assistant_messages(qapp):
    """A turn can complete more than one assistant message, and all of them
    stream into the same bubble ΓÇö so the text ``turn.completed`` persists has
    to be all of them, not just the last one."""
    from windows.agent_runners import CodexRunner

    runner = CodexRunner()
    events = _collect(runner)
    for ev in (
        {"type": "item.completed", "item": {"type": "agent_message", "text": "First."}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Second."}},
        {"type": "turn.completed"},
    ):
        runner._handle_event(ev)

    streamed = "".join(t for kind, t in [e for e in events if e[0] == "token"])
    responses = [t for kind, t in [e for e in events if e[0] == "response_ready"]]
    assert streamed == "First.Second."
    assert responses == ["First.\n\nSecond."], "persisted text lost an earlier message"


def test_failed_launch_does_not_leave_a_resume_for_a_session_the_cli_never_made(
        qapp, monkeypatch, tmp_path):
    """A CLI that exits non-zero with no output never created the conversation
    we latched at launch. Keeping that latch makes every later message in the
    tab ``--resume`` an unknown id, which fails until the user clears the
    session ΓÇö so the failure has to reset the continuity."""
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
    monkeypatch.setattr(ar, "_project_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(ar, "_write_claude_mcp_config",
                        lambda server: str(tmp_path / "claude_mcp.json"))
    # A CLI that fails immediately: no stream-json output, non-zero exit.
    real_build_argv = ClaudeCodeRunner._build_argv
    monkeypatch.setattr(ClaudeCodeRunner, "_build_argv",
                        lambda self, text: [sys.executable, "-c", "raise SystemExit(1)"])

    runner = ClaudeCodeRunner()
    runner._session_id = "11111111-1111-1111-1111-111111111111"
    events = _collect(runner)
    runner.run_request("hello", "")

    assert [e[0] for e in events] == ["error"]
    assert not runner._cli_started
    first_id = runner._cli_session_id

    # The next turn starts a conversation instead of resuming a phantom one.
    argv = real_build_argv(runner, "again")
    assert "--resume" not in argv
    assert argv[argv.index("--session-id") + 1] == first_id


def test_no_saved_project_confines_the_agent_outside_home(monkeypatch, tmp_path):
    """These CLIs run with approvals and sandbox bypassed, and an unsaved
    project is the state the app launches in ΓÇö a home-rooted cwd would hand the
    agent unattended write access to everything the user owns."""
    import windows.agent_runners as ar
    from classes import info

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))
    monkeypatch.setattr("classes.app.get_app", lambda: (_ for _ in ()).throw(RuntimeError))

    cwd = ar._project_cwd()
    assert cwd != os.path.expanduser("~")
    assert cwd == str(tmp_path / "agent_workspace")
    assert os.path.isdir(cwd)


def test_claude_mcp_config_is_never_world_readable(monkeypatch, tmp_path):
    """The file carries the MCP bearer token: a plain open() would apply the
    umask first and leave it readable by other local users in between."""
    import stat
    import windows.agent_runners as ar
    from classes import info

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))

    class _FakeServer:
        token = "tok"

        def url(self):
            return "http://127.0.0.1:7434/mcp"

    old_umask = os.umask(0o022)
    try:
        path = ar._write_claude_mcp_config(_FakeServer())
    finally:
        os.umask(old_umask)

    if os.name != "nt":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert os.path.isabs(path)
    assert not path.startswith("~")
    assert os.path.isfile(path)


def test_which_cli_finds_standalone_codex_when_not_on_path(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    bindir = tmp_path / ".codex" / "packages" / "standalone" / "current" / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / ("codex.exe" if os.name == "nt" else "codex")
    exe.write_bytes(b"")
    if os.name != "nt":
        exe.chmod(0o755)

    monkeypatch.setattr(ar.shutil, "which", lambda name: None)
    monkeypatch.setattr(ar, "_resolved_home", lambda: str(tmp_path))
    assert ar._which_cli("codex") == str(exe)


def test_which_cli_finds_local_bin_claude(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    bindir = tmp_path / ".local" / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / ("claude.exe" if os.name == "nt" else "claude")
    exe.write_bytes(b"")
    if os.name != "nt":
        exe.chmod(0o755)

    monkeypatch.setattr(ar.shutil, "which", lambda name: None)
    monkeypatch.setattr(ar, "_resolved_home", lambda: str(tmp_path))
    assert ar._which_cli("claude") == str(exe)


def test_windows_profile_prefers_users_dir_over_msys_home(tmp_path):
    from classes.info import windows_profile_candidates

    users = tmp_path / "Users" / "alice"
    users.mkdir(parents=True)
    env = {
        "USERNAME": "alice",
        "USER": "alice",
        "SYSTEMDRIVE": str(tmp_path),
        "HOME": "/home/alice",
    }
    candidates = windows_profile_candidates(env)
    assert os.path.normpath(str(users)) == os.path.normpath(candidates[0])


def test_agent_bash_prompt_covers_heic_and_zsh():
    from windows.agent_runners import _agent_bash_prompt

    text = _agent_bash_prompt()
    assert "sips" in text
    assert "filter_complex" in text
    assert "${!" in text
    assert "-vf" in text
    assert "HEIC" in text


def test_claude_argv_appends_bash_prompt(qapp, monkeypatch):
    import windows.agent_runners as ar
    from windows.agent_runners import ClaudeCodeRunner, _agent_bash_prompt

    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")
    runner = ClaudeCodeRunner()
    runner._session_id = "s7"
    runner._cli_session_id = "s7"
    argv = runner._build_argv("hi")
    assert "--append-system-prompt" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == _agent_bash_prompt()


def test_cli_child_env_sets_claude_code_shell_for_bash4(monkeypatch):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_resolve_cli_bash", lambda: "/opt/homebrew/bin/bash")
    monkeypatch.delenv("CLAUDE_CODE_SHELL", raising=False)
    env = ar._cli_child_env()
    assert env["CLAUDE_CODE_SHELL"] == "/opt/homebrew/bin/bash"
    assert env["SHELL"] == "/opt/homebrew/bin/bash"


def test_cli_child_env_omits_claude_code_shell_without_bash4(monkeypatch):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_resolve_cli_bash", lambda: None)
    monkeypatch.delenv("CLAUDE_CODE_SHELL", raising=False)
    env = ar._cli_child_env()
    assert "CLAUDE_CODE_SHELL" not in env


def test_cli_child_env_keeps_user_claude_code_shell(monkeypatch):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_resolve_cli_bash", lambda: "/opt/homebrew/bin/bash")
    monkeypatch.setenv("CLAUDE_CODE_SHELL", "/custom/bash")
    env = ar._cli_child_env()
    assert env["CLAUDE_CODE_SHELL"] == "/custom/bash"


def test_resolve_cli_bash_skips_version_below_4(monkeypatch):
    import windows.agent_runners as ar

    ar._resolve_cli_bash.cache_clear()
    monkeypatch.setattr(ar, "_bash_candidates", lambda: ["/bin/bash"])
    monkeypatch.setattr(ar.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(ar.os, "access", lambda p, m: True)
    monkeypatch.setattr(ar, "_bash_major", lambda path: 3)
    try:
        assert ar._resolve_cli_bash() is None
    finally:
        ar._resolve_cli_bash.cache_clear()


def test_resolve_cli_bash_picks_version_4(monkeypatch):
    import windows.agent_runners as ar

    ar._resolve_cli_bash.cache_clear()
    monkeypatch.setattr(ar, "_bash_candidates", lambda: ["/opt/homebrew/bin/bash"])
    monkeypatch.setattr(ar.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(ar.os, "access", lambda p, m: True)
    monkeypatch.setattr(ar, "_bash_major", lambda path: 5)
    try:
        if os.name == "nt":
            assert ar._resolve_cli_bash() is None
        else:
            assert ar._resolve_cli_bash() == "/opt/homebrew/bin/bash"
    finally:
        ar._resolve_cli_bash.cache_clear()


# ── Live model lineups ────────────────────────────────────────────────────

@pytest.fixture
def clear_live_lineups():
    from windows.agent_runners import set_live_lineups
    set_live_lineups({})
    yield
    set_live_lineups({})


def test_live_lineup_replaces_the_built_in_list(qapp, clear_live_lineups):
    """Once the backend has answered, its list is what the picker shows, so a
    release that the backend discovered appears without a desktop update."""
    from windows.agent_runners import (
        BACKEND_CLAUDE, BACKEND_CODEX, ClaudeCodeRunner, models_for_backend,
        set_live_lineups,
    )

    set_live_lineups({
        BACKEND_CLAUDE: [
            {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "featured": True,
             "rank": 9, "tags": ["New"], "provider": "anthropic"},
            {"id": "claude-opus-5", "name": "Claude Opus 5", "featured": True,
             "rank": 10, "default": True},
        ],
        BACKEND_CODEX: [
            {"id": "gpt-5.6-astra", "name": "GPT-5.6 Astra", "featured": True, "rank": 10},
            {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex", "featured": True,
             "rank": 30, "default": True},
        ],
    })
    claude = models_for_backend(BACKEND_CLAUDE)
    assert [m["id"] for m in claude] == ["claude-opus-5-5", "claude-opus-5"]
    assert claude[0]["tags"] == ["New"]
    assert [m["id"] for m in claude if m.get("default")] == ["claude-opus-5"]
    # the built-in catalogue is untouched, ready for the next fallback
    assert ClaudeCodeRunner.MODELS[0]["id"] == "claude-opus-5"

    # Codex, which has no built-in list, now offers one
    codex = models_for_backend(BACKEND_CODEX)
    assert [m["id"] for m in codex] == ["gpt-5.6-astra", "gpt-5.3-codex"]

    # callers mutate what they get; the cache must not leak by reference
    claude[0]["name"] = "mutated"
    assert models_for_backend(BACKEND_CLAUDE)[0]["name"] == "Claude Opus 5.5"


def test_missing_or_empty_live_list_falls_back_to_the_built_in_one(qapp, clear_live_lineups):
    from windows.agent_runners import (
        BACKEND_CLAUDE, BACKEND_CODEX, ClaudeCodeRunner, models_for_backend,
        set_live_lineups,
    )

    set_live_lineups({BACKEND_CLAUDE: [], BACKEND_CODEX: []})
    assert [m["id"] for m in models_for_backend(BACKEND_CLAUDE)] == \
        [m["id"] for m in ClaudeCodeRunner.MODELS]
    assert models_for_backend(BACKEND_CODEX) == []

    set_live_lineups({})
    assert len(models_for_backend(BACKEND_CLAUDE)) == len(ClaudeCodeRunner.MODELS)


def test_live_lineup_ignores_malformed_rows(qapp, clear_live_lineups):
    """The payload comes over the network; junk must not reach chat.js."""
    from windows.agent_runners import BACKEND_CODEX, models_for_backend, set_live_lineups

    set_live_lineups({BACKEND_CODEX: [
        "not-a-dict", {"name": "no id"}, {"id": ""}, {"id": 42},
        {"id": "gpt-5.3-codex"},                      # name defaults to the id
        {"id": "gpt-5.3-codex", "name": "dup"},       # duplicate id dropped
        {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "extra": "ignored"},
    ]})
    rows = models_for_backend(BACKEND_CODEX)
    assert [r["id"] for r in rows] == ["gpt-5.3-codex", "gpt-5.6-sol"]
    assert rows[0]["name"] == "gpt-5.3-codex"
    assert "extra" not in rows[1]


def test_coerce_model_honours_the_live_lineup(qapp, clear_live_lineups):
    """A model the backend surfaced must reach the CLI's --model flag, and a
    Codex tab must accept a Codex model once it has a lineup at all."""
    from windows.agent_runners import (
        BACKEND_CLAUDE, BACKEND_CODEX, ClaudeCodeRunner, CodexRunner, set_live_lineups,
    )

    claude, codex = ClaudeCodeRunner(), CodexRunner()
    # built-in only: a not-yet-known model is refused, Codex takes nothing
    assert claude._coerce_model("claude-opus-5-5") == ""
    assert claude._coerce_model("claude-opus-5") == "claude-opus-5"
    assert codex._coerce_model("gpt-5.3-codex") == ""

    set_live_lineups({
        BACKEND_CLAUDE: [{"id": "claude-opus-5-5", "name": "Claude Opus 5.5"}],
        BACKEND_CODEX: [{"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"}],
    })
    assert claude._coerce_model("claude-opus-5-5") == "claude-opus-5-5"
    assert claude._coerce_model("claude-opus-5") == "", "live list replaces, not extends"
    assert codex._coerce_model("gpt-5.3-codex") == "gpt-5.3-codex"
    # a Zenvi model id left over from a shared picker is still refused
    assert codex._coerce_model("openai/gpt-5.6-sol") == ""


# cursor_stream.jsonl is a trimmed capture of cursor-agent 2026.09.18 running
# `-p --output-format stream-json --stream-partial-output` against an MCP
# server named zenvi-editor: one tool schema lookup, a successful and a failing
# MCP call, and one of Cursor's own shell calls.
_CURSOR_FIRST = ("I'll inspect the zenvi-editor tool schemas, then call "
                 "`list_files_tool` and `add_clip_to_timeline_tool` as requested.")
_CURSOR_LAST = "`city.mp4` could not be added because Track 1 is locked."


def test_cursor_parser_emits_expected_signals(qapp):
    from windows.agent_runners import CursorCliRunner
    runner = CursorCliRunner()
    runner._session_id = "s1"
    events = _collect(runner)
    _feed(runner, "cursor_stream.jsonl")

    assert runner._cli_session_id == "cur-abc-123"
    assert runner._cli_id_from_cli is True
    started = [e[1] for e in events if e[0] == "tool_started" and e[1] != "thinking"]
    assert started == ["look_up_tools", "list_files_tool", "add_clip_to_timeline_tool",
                       "run_shell_command"]
    assert any(e[0] == "tool_started" and e[1] == "thinking" for e in events)
    # Every block that opened is closed, thinking included.
    opened = [e[2] for e in events if e[0] == "tool_started"]
    closed = [e[1] for e in events if e[0] == "tool_completed"]
    assert sorted(opened) == sorted(closed)
    names = {e[2]: e[1] for e in events if e[0] == "tool_started"}
    by_name = {names[e[1]]: (e[2], e[3]) for e in events if e[0] == "tool_completed"}
    assert by_name["list_files_tool"][0] is True
    assert "FIXTURE" in by_name["list_files_tool"][1]
    # An MCP tool that raised still arrives as "success" with isError.
    assert by_name["add_clip_to_timeline_tool"] == (
        False, "Error executing tool add_clip_to_timeline_tool: Track 1 is locked")
    assert by_name["run_shell_command"] == (True, "beach.mp4\ncity.mp4\n")
    assert by_name["look_up_tools"][0] is True

    # Every message streams exactly once: the CLI's whole-message repeat
    # after the deltas is not shown a second time.
    tokens = "".join(e[1] for e in events if e[0] == "token")
    assert tokens == _CURSOR_FIRST + _CURSOR_LAST
    # The reply keeps the messages apart, the way the chat committed them.
    replies = [e[1] for e in events if e[0] == "response_ready"]
    assert replies == [_CURSOR_FIRST + "\n\n" + _CURSOR_LAST]
    assert not any(e[0] == "error" for e in events)


def test_cursor_mcp_tool_args_are_the_tools_own(qapp):
    from windows.agent_runners import CursorCliRunner
    runner = CursorCliRunner()
    seen = []
    runner.tool_started.connect(lambda c, n, a: seen.append((n, json.loads(a))))
    _feed(runner, "cursor_stream.jsonl")

    args = dict(seen)
    assert args["add_clip_to_timeline_tool"] == {"file_name": "city.mp4", "position_seconds": 0}
    assert args["list_files_tool"] == {}
    # Cursor's own tools show what they work on, not the CLI's bookkeeping.
    assert args["run_shell_command"] == {"command": "ls"}
    assert args["look_up_tools"] == {"server": "zenvi-editor"}


def test_cursor_native_tools_are_not_labelled_as_motion_graphics(qapp):
    """Bare read/edit/glob/grep are the Zenvi harness's motion-graphics tools."""
    from classes.tool_handlers import humanize_tool_name
    from windows.agent_runners import _CURSOR_TOOL_NAMES, _cursor_tool_start

    for kind in ("read", "edit", "write", "glob", "grep", "shell", "delete", "ls"):
        name, _ = _cursor_tool_start(kind, {"args": {"path": "a.txt"}})
        assert name == _CURSOR_TOOL_NAMES[kind]
        assert "motion graphic" not in humanize_tool_name(name).lower(), kind
    assert _cursor_tool_start("webSearch", {"args": {"query": "x"}}) == ("web_search", {"query": "x"})


@pytest.mark.parametrize("result,expected", [
    (None, (True, "")),
    ({"success": {"content": [{"text": {"text": "a"}}, {"text": {"text": "b"}}],
                  "isError": False}}, (True, "a\nb")),
    ({"success": {"content": "plain"}}, (True, "plain")),
    ({"success": {"exitCode": 2, "stdout": "", "stderr": "no such file",
                  "interleavedOutput": "no such file"}}, (False, "no such file")),
    ({"spawnError": {"command": "", "error": "no exit status"}}, (False, "no exit status")),
    ({"error": {"error": "Path does not exist: /x"}}, (False, "Path does not exist: /x")),
    ({"rejected": {"reason": "denied"}, "isBackground": False}, (False, "denied")),
    ({"error": "boom"}, (False, "boom")),
    ({"isBackground": False}, (True, "")),
])
def test_cursor_tool_result_reads_success_and_every_failure_shape(result, expected):
    from windows.agent_runners import _cursor_tool_result
    assert _cursor_tool_result(result) == expected


def test_cursor_reply_without_partial_output_is_not_dropped(qapp):
    """A message that never streamed as deltas is shown, not taken for a repeat."""
    from windows.agent_runners import CursorCliRunner
    runner = CursorCliRunner()
    events = _collect(runner)
    for ev in (
        {"type": "system", "subtype": "init", "session_id": "c1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Done."},
    ):
        runner._handle_event(ev)
    assert [e[1] for e in events if e[0] == "token"] == ["Done."]
    assert [e[1] for e in events if e[0] == "response_ready"] == ["Done."]


def test_cursor_new_turn_forgets_the_last_turns_prose(qapp):
    from windows.agent_runners import CursorCliRunner
    runner = CursorCliRunner()
    events = _collect(runner)
    _feed(runner, "cursor_stream.jsonl")
    runner._final_text = ""   # run_request resets this per turn
    for ev in (
        {"type": "system", "subtype": "init", "session_id": "cur-abc-123"},
        {"type": "assistant", "timestamp_ms": 1,
         "message": {"content": [{"type": "text", "text": "Second turn."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Second turn."},
    ):
        runner._handle_event(ev)
    assert [e[1] for e in events if e[0] == "response_ready"][-1] == "Second turn."


def test_cursor_runner_takes_models_from_its_own_lineup(qapp, clear_live_lineups):
    """_coerce_model looks the lineup up by BACKEND_ID; without it no Cursor
    model could ever reach --model."""
    from windows.agent_runners import BACKEND_CURSOR, CursorCliRunner, set_live_lineups

    runner = CursorCliRunner()
    assert runner.BACKEND_ID == BACKEND_CURSOR
    set_live_lineups({BACKEND_CURSOR: [{"id": "composer-2.5", "name": "Composer 2.5"}]})
    assert runner._coerce_model("composer-2.5") == "composer-2.5"
    assert runner._coerce_model("claude-opus-5") == "", "another backend's model is dropped"


def test_cursor_argv_is_headless_and_hides_model(qapp, monkeypatch):
    import windows.agent_runners as ar
    from windows.agent_runners import CursorCliRunner

    monkeypatch.setattr(ar, "_add_dir_args", lambda: ["--add-dir", "C:/footage"])
    runner = CursorCliRunner()
    runner._cli_path = r"C:\cursor-agent\cursor-agent.cmd"
    runner._cli_cwd = r"C:\proj"
    runner._cli_session_id = "cur-abc-123"
    runner._cli_started = True
    runner._cli_id_from_cli = True
    argv = runner._build_argv("make a cut")
    assert argv[0] == runner._cli_path
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--stream-partial-output" in argv
    assert "--force" in argv
    assert "--trust" in argv
    # Not approved yet (no _ensure_ready ran): every server would be, as before.
    assert "--approve-mcps" in argv
    assert argv[argv.index("--workspace") + 1] == r"C:\proj"
    assert argv[argv.index("--resume") + 1] == "cur-abc-123"
    assert argv[argv.index("--add-dir") + 1] == "C:/footage"
    assert argv[-1] == "make a cut"
    assert "--model" not in argv
    assert [m["id"] for m in runner.MODELS] == ["cli-default"]

    runner._mcp_approved = True   # zenvi-editor alone was approved
    assert "--approve-mcps" not in runner._build_argv("make a cut")


def _cursor_home(monkeypatch, tmp_path, which=None):
    import windows.agent_runners as ar
    monkeypatch.setattr(ar.shutil, "which", which or (lambda name: None))
    monkeypatch.setattr(ar, "_resolved_home", lambda: str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    return ar


def _cursor_version(tmp_path, version="2026.09.18-9a7762b"):
    """What the macOS/Linux installer lays down under ~/.local/share."""
    exe = tmp_path / ".local" / "share" / "cursor-agent" / "versions" / version / "cursor-agent"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_which_cursor_cli_prefers_cursor_install_over_other_agent(monkeypatch, tmp_path):
    """Windows: the installer's own folder wins over some other `agent` on PATH."""
    install = tmp_path / "AppData" / "Local" / "cursor-agent"
    install.mkdir(parents=True)
    exe = install / "cursor-agent.cmd"
    exe.write_text("@echo off\n")
    grok = tmp_path / "grok" / "agent.exe"
    grok.parent.mkdir()
    grok.write_bytes(b"")

    ar = _cursor_home(monkeypatch, tmp_path,
                      lambda name: str(grok) if name.startswith("agent") else None)
    assert ar._which_cli("cursor-agent") == str(exe)


def test_which_cursor_cli_ignores_an_unrelated_agent_in_local_bin(monkeypatch, tmp_path):
    other = tmp_path / ".local" / "bin" / "agent"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\necho not cursor\n")
    other.chmod(0o755)

    ar = _cursor_home(monkeypatch, tmp_path)
    assert ar._which_cli("cursor-agent") is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_which_cursor_cli_accepts_an_agent_link_into_a_cursor_install(monkeypatch, tmp_path):
    """The installer's ~/.local/bin/agent is a symlink into versions/; with the
    cursor-agent link removed that alias is still Cursor's."""
    exe = _cursor_version(tmp_path)
    link = tmp_path / ".local" / "bin" / "agent"
    link.parent.mkdir(parents=True)
    link.symlink_to(exe)

    ar = _cursor_home(monkeypatch, tmp_path)
    assert ar._which_cli("cursor-agent") == str(link)
    # ...and the same alias found on PATH is trusted for the same reason.
    ar = _cursor_home(monkeypatch, tmp_path,
                      lambda name: str(link) if name == "agent" else None)
    assert ar._which_cli("cursor-agent") == str(link)


def test_which_cursor_cli_ignores_an_unrelated_agent_on_path(monkeypatch, tmp_path):
    grok = tmp_path / "bin" / "agent"
    grok.parent.mkdir()
    grok.write_text("#!/bin/sh\n")
    ar = _cursor_home(monkeypatch, tmp_path,
                      lambda name: str(grok) if name.startswith("agent") else None)
    assert ar._which_cli("cursor-agent") is None


def test_which_cursor_cli_falls_back_to_the_newest_installed_version(monkeypatch, tmp_path):
    _cursor_version(tmp_path, "2026.09.02-c22c1a3")
    newest = _cursor_version(tmp_path, "2026.09.18-9a7762b")
    ar = _cursor_home(monkeypatch, tmp_path)
    assert ar._which_cli("cursor-agent") == str(newest)


def test_register_cursor_writes_bearer_and_updates_port(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({
        "mcpServers": {
            "other": {"command": "npx", "args": ["something"]},
            "zenvi-editor": {
                "url": "http://127.0.0.1:9999/mcp",
                "headers": {"Authorization": "Bearer old"},
            },
        }
    }))
    monkeypatch.setattr(ar, "_cursor_mcp_path", lambda: str(cfg))

    ok, message = ar.register_cursor(7434, "tok123")
    assert ok is True
    assert "Connected" in message

    data = json.loads(cfg.read_text())
    server = data["mcpServers"]["zenvi-editor"]
    assert server["url"] == "http://127.0.0.1:7434/mcp"
    # The token is named, not stored: cursor-agent expands ${env:...}.
    assert server["headers"]["Authorization"] == "Bearer ${env:ZENVI_MCP_TOKEN}"
    assert "tok123" not in cfg.read_text()
    assert "export ZENVI_MCP_TOKEN=tok123" in message
    assert data["mcpServers"]["other"]["command"] == "npx"
    assert (tmp_path / "mcp.json.zenvi-backup").exists()
    assert ar._cursor_is_registered() is True


def test_register_cursor_refuses_invalid_json(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "mcp.json"
    original = "{not json"
    cfg.write_text(original)
    monkeypatch.setattr(ar, "_cursor_mcp_path", lambda: str(cfg))

    ok, message = ar.register_cursor(7434, "tok123")
    assert ok is False
    assert cfg.read_text() == original


def test_register_cursor_leaves_a_users_own_zenvi_server_alone(monkeypatch, tmp_path):
    """Only zenvi-editor is Zenvi's; a server the user called "zenvi" is theirs."""
    import windows.agent_runners as ar

    cfg = tmp_path / "mcp.json"
    theirs = {"command": "node", "args": ["my-zenvi-scripts.js"]}
    cfg.write_text(json.dumps({"mcpServers": {"zenvi": theirs}}))
    monkeypatch.setattr(ar, "_cursor_mcp_path", lambda: str(cfg))

    assert ar._cursor_is_registered() is False, "their server is not our registration"
    ok, _ = ar.register_cursor(7434, "tok123")
    servers = json.loads(cfg.read_text())["mcpServers"]
    assert ok is True
    assert servers["zenvi"] == theirs
    assert servers["zenvi-editor"]["url"] == "http://127.0.0.1:7434/mcp"


def test_register_cursor_does_not_rewrite_a_current_entry(monkeypatch, tmp_path):
    """The runner re-checks before every turn; that must not churn the file
    the Cursor editor watches, or overwrite the backup of the user's own."""
    import windows.agent_runners as ar

    cfg = tmp_path / "mcp.json"
    users = json.dumps({"mcpServers": {"other": {"url": "https://example.com/mcp"}}})
    cfg.write_text(users)
    monkeypatch.setattr(ar, "_cursor_mcp_path", lambda: str(cfg))

    assert ar.register_cursor(7434, "tok123")[0] is True
    written = cfg.read_text()
    os.utime(cfg, (1, 1))
    assert ar.register_cursor(7434, "tok123")[0] is True
    assert cfg.read_text() == written and cfg.stat().st_mtime == 1
    assert (tmp_path / "mcp.json.zenvi-backup").read_text() == users
    assert not (tmp_path / "mcp.json.zenvi-tmp").exists()
    # A moved port is still picked up, and the backup stays the user's file.
    assert ar.register_cursor(7435, "tok123")[0] is True
    assert json.loads(cfg.read_text())["mcpServers"]["zenvi-editor"]["url"].endswith(":7435/mcp")
    assert (tmp_path / "mcp.json.zenvi-backup").read_text() == users


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_register_cursor_never_leaves_other_servers_secrets_world_readable(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"discord": {"env": {"DISCORD_TOKEN": "s3cret"}}}}))
    cfg.chmod(0o600)
    monkeypatch.setattr(ar, "_cursor_mcp_path", lambda: str(cfg))
    old = os.umask(0o022)
    try:
        assert ar.register_cursor(7434, "tok123")[0] is True
    finally:
        os.umask(old)
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "mcp.json.zenvi-backup").stat().st_mode & 0o777 == 0o600

    fresh = tmp_path / "new" / ".cursor" / "mcp.json"
    monkeypatch.setattr(ar, "_cursor_mcp_path", lambda: str(fresh))
    assert ar.register_cursor(7434, "tok123")[0] is True
    assert fresh.stat().st_mode & 0o777 == 0o600, "the bearer token is in there too"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_register_cursor_updates_a_symlinked_config_through_the_link(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    target = tmp_path / "dotfiles" / "cursor-mcp.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"mcpServers": {}}))
    link = tmp_path / ".cursor" / "mcp.json"
    link.parent.mkdir()
    link.symlink_to(target)
    monkeypatch.setattr(ar, "_cursor_mcp_path", lambda: str(link))

    assert ar.register_cursor(7434, "tok123")[0] is True
    assert link.is_symlink()
    assert "zenvi-editor" in json.loads(target.read_text())["mcpServers"]


def test_register_cursor_refuses_a_non_object_server_table(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "mcp.json"
    original = json.dumps({"mcpServers": ["not", "a", "table"]})
    cfg.write_text(original)
    monkeypatch.setattr(ar, "_cursor_mcp_path", lambda: str(cfg))
    ok, message = ar.register_cursor(7434, "tok123")
    assert ok is False and "not touching" in message
    assert cfg.read_text() == original


# ── Cursor model lineup (asked of the CLI) ────────────────────────────────

@pytest.fixture
def fresh_cursor_lineup(monkeypatch):
    import windows.agent_runners as ar
    monkeypatch.setattr(ar, "_cli_lineups", {})
    monkeypatch.setattr(ar, "_cursor_models_read", {"key": None, "at": 0.0})
    ar.set_live_lineups({})
    yield ar
    ar.set_live_lineups({})


def test_parse_cursor_models_reads_the_real_listing():
    """cursor_models.txt is trimmed `cursor-agent models` output (2026.09.18)."""
    from windows.agent_runners import parse_cursor_models

    with open(os.path.join(_FIX, "cursor_models.txt"), encoding="utf-8") as fh:
        rows = parse_cursor_models(fh.read())

    assert [r["id"] for r in rows] == [
        "cli-default",
        "auto", "gpt-5.3-codex", "composer-2.5", "claude-opus-5-thinking-high",
        "claude-fable-5-thinking-high", "gemini-3.7-flash-high", "grok-4.7-low-fast",
        "claude-opus-5-5-high", "kimi-k2.7-code",
    ]
    # Preselected, and says what the CLI resolves it to.
    assert rows[0] == {"id": "cli-default", "name": "CLI default", "rank": 0,
                       "featured": True, "default": True, "tags": ["Auto"]}
    assert [r["id"] for r in rows if r["featured"]] == ["cli-default"], "search reaches the rest"
    assert not any(r.get("default") for r in rows[1:])
    names = {r["id"]: r["name"] for r in rows}
    assert names["auto"] == "Auto"                                   # flags stripped
    assert names["grok-4.7-low-fast"] == "Grok 4.7 Low Fast"        # zero-width spaces gone
    assert names["claude-fable-5-thinking-high"].endswith("(NO ZDR)")  # not a flag
    assert [r["rank"] for r in rows] == list(range(len(rows)))      # the CLI's order


def test_parse_cursor_models_prefers_the_model_the_cli_is_set_to():
    from windows.agent_runners import parse_cursor_models

    rows = parse_cursor_models(
        "\x1b[1mAvailable models\x1b[0m\n\nauto - Auto (default)\n"
        "composer-2.5 - Composer 2.5 (current)\ncomposer-2.5 - duplicate\n")
    assert [r["id"] for r in rows] == ["cli-default", "auto", "composer-2.5"]
    assert rows[0]["tags"] == ["Composer 2.5"], "what the CLI is set to, not Cursor's pick"
    assert parse_cursor_models("Error: not logged in\n") == []


def test_cursor_lineup_is_read_once_per_cli_version_and_kept_on_failure(
        fresh_cursor_lineup, monkeypatch):
    ar = fresh_cursor_lineup
    calls = []
    answers = [[{"id": "auto", "name": "Auto", "default": True}], []]
    monkeypatch.setattr(ar, "_which_cursor_cli", lambda: "/bin/cursor-agent")
    monkeypatch.setattr(ar, "probe_cursor_models",
                        lambda cli: calls.append(cli) or answers.pop(0))

    assert ar.refresh_cursor_models("2026.09.18") is True
    assert [m["id"] for m in ar.models_for_backend(ar.BACKEND_CURSOR)] == ["auto"]
    # Detection runs every minute; the CLI is not asked again until it is due.
    assert ar.refresh_cursor_models("2026.09.18") is False
    assert calls == ["/bin/cursor-agent"]
    # An update is due at once. This read fails, and the list stays.
    assert ar.refresh_cursor_models("2026.09.28") is False
    assert len(calls) == 2
    assert [m["id"] for m in ar.models_for_backend(ar.BACKEND_CURSOR)] == ["auto"]


def test_cursor_lineup_reaches_the_model_flag(qapp, fresh_cursor_lineup, monkeypatch):
    ar = fresh_cursor_lineup
    ar.set_cli_lineup(ar.BACKEND_CURSOR, [
        {"id": "auto", "name": "Auto", "default": True},
        {"id": "claude-opus-5-5-high", "name": "Claude Opus 5.5 1M High"},
    ])
    monkeypatch.setattr(ar, "_add_dir_args", lambda: [])
    runner = ar.CursorCliRunner()
    runner._cli_cwd = "/proj"
    runner._model_id = runner._coerce_model("claude-opus-5-5-high")
    argv = runner._build_argv("hi")
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5-high"
    assert runner._coerce_model("claude-opus-5") == "", "not one of Cursor's ids"
    # "CLI default" leaves the choice to the CLI's own config.
    runner._model_id = runner._coerce_model("cli-default")
    assert runner._model_id == "" and "--model" not in runner._build_argv("hi")
    # The backend's lineup, when it serves one, still wins (#202).
    ar.set_live_lineups({ar.BACKEND_CURSOR: [{"id": "composer-2.5", "name": "Composer"}]})
    assert [m["id"] for m in ar.models_for_backend(ar.BACKEND_CURSOR)] == ["composer-2.5"]


# ── Cursor: approve zenvi-editor alone, token from the environment ────────

@pytest.fixture
def fresh_approvals(monkeypatch):
    import windows.agent_runners as ar
    monkeypatch.setattr(ar, "_cursor_approved", set())
    return ar


def _ran(calls, stdout, returncode=0):
    def run(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


def test_cursor_approves_only_zenvi_editor_once_per_workspace(fresh_approvals, monkeypatch):
    ar = fresh_approvals
    calls = []
    monkeypatch.setattr(ar.subprocess, "run",
                        _ran(calls, "✓ Enabled and approved MCP server: zenvi-editor\n"))
    env = {"ZENVI_MCP_TOKEN": "tok"}
    assert ar._approve_cursor_mcp("/bin/cursor-agent", "/proj", env, "http://127.0.0.1:7434/mcp")
    argv, kw = calls[0]
    assert argv == ["/bin/cursor-agent", "mcp", "enable", "zenvi-editor"]
    # Approvals are per workspace and hash the expanded header: same cwd, token in env.
    assert kw["cwd"] == "/proj" and kw["env"]["ZENVI_MCP_TOKEN"] == "tok"

    assert ar._approve_cursor_mcp("/bin/cursor-agent", "/proj", env, "http://127.0.0.1:7434/mcp")
    assert len(calls) == 1, "approved once per session"
    ar._approve_cursor_mcp("/bin/cursor-agent", "/proj", env, "http://127.0.0.1:7435/mcp")
    ar._approve_cursor_mcp("/bin/cursor-agent", "/other", env, "http://127.0.0.1:7435/mcp")
    assert len(calls) == 3, "a moved port or another project needs its own approval"


@pytest.mark.parametrize("stdout,returncode", [
    ("MCP server 'zenvi-editor' not found in configuration\n", 0),   # exits 0 anyway
    ("error: unknown command 'enable'\n", 1),
])
def test_cursor_approval_that_did_not_take_reports_false(fresh_approvals, monkeypatch,
                                                         stdout, returncode):
    ar = fresh_approvals
    calls = []
    monkeypatch.setattr(ar.subprocess, "run", _ran(calls, stdout, returncode))
    assert ar._approve_cursor_mcp("cursor-agent", "/proj", {}, "u") is False
    assert ar._approve_cursor_mcp("cursor-agent", "/proj", {}, "u") is False
    assert len(calls) == 2, "a failure is not cached"


def test_cursor_turn_carries_the_token_and_approves_before_launch(qapp, monkeypatch):
    import windows.agent_runners as ar
    seen = {}
    monkeypatch.setattr(ar, "register_cursor", lambda port, token: (True, "ok"))

    def approve(cli, cwd, env, url):
        seen.update(cli=cli, cwd=cwd, token=env.get("ZENVI_MCP_TOKEN"), url=url)
        return True
    monkeypatch.setattr(ar, "_approve_cursor_mcp", approve)
    runner = ar.CursorCliRunner()
    runner._server = types.SimpleNamespace(port=7434, token="tok",
                                           url=lambda: "http://127.0.0.1:7434/mcp")
    runner._cli_path = "/bin/cursor-agent"
    runner._cli_cwd = "/proj"

    assert runner._ensure_ready() is None
    assert seen == {"cli": "/bin/cursor-agent", "cwd": "/proj", "token": "tok",
                    "url": "http://127.0.0.1:7434/mcp"}
    assert runner._build_env()["ZENVI_MCP_TOKEN"] == "tok"
    assert "--approve-mcps" not in runner._build_argv("hi")


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
def test_cursor_turn_leaves_nothing_running_after_the_cli_exits(qapp, monkeypatch, tmp_path):
    """cursor-agent exits without stopping the MCP servers it started; seen in
    the app as one orphaned stdio server per finished turn."""
    import signal as _signal
    import time
    import windows.agent_runners as ar

    pidfile = tmp_path / "child.pid"
    cli = (
        "import json, subprocess, sys\n"
        # Its stdio goes to the CLI, not to us, as an MCP server's does.
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "open(%r, 'w').write(str(child.pid))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
        " 'result': 'done'}), flush=True)\n" % str(pidfile)
    )

    class _FakeServer:
        token, port = "tok", 1

        def start(self):
            return self

        def url(self):
            return "http://127.0.0.1:1/mcp"

    monkeypatch.setattr("classes.agent_mcp_server.get_mcp_server", lambda: _FakeServer())
    monkeypatch.setattr(ar, "_which_cli", lambda name: sys.executable)
    monkeypatch.setattr(ar.CursorCliRunner, "_ensure_ready", lambda self: None)
    monkeypatch.setattr(ar.CursorCliRunner, "_build_argv",
                        lambda self, text: [sys.executable, "-c", cli])
    monkeypatch.setattr(ar, "_project_cwd", lambda: str(tmp_path))

    runner = ar.CursorCliRunner()
    replies = []
    runner.response_ready.connect(replies.append)
    runner.run_request("hi", "")

    assert replies == ["done"]
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        try:
            os.waitpid(child, os.WNOHANG)   # not ours to reap; ignore
        except ChildProcessError:
            pass
        time.sleep(0.1)
    else:
        os.kill(child, _signal.SIGKILL)
        pytest.fail("the CLI's child outlived the turn")


def test_other_clis_keep_their_children(qapp):
    """Only Cursor reaps: Claude Code and Codex behave as before."""
    from windows.agent_runners import ClaudeCodeRunner, CodexRunner, CursorCliRunner
    assert CursorCliRunner.REAP_ON_EXIT is True
    assert ClaudeCodeRunner.REAP_ON_EXIT is False and CodexRunner.REAP_ON_EXIT is False
