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


@pytest.fixture(autouse=True)
def _isolated_from_this_machine(monkeypatch):
    """No test may see the CLIs really installed here or another test's lineup."""
    import windows.agent_runners as ar
    monkeypatch.setattr(ar, "_windows_path_dirs", lambda: ())
    monkeypatch.setattr(ar, "_cli_lineups", {})
    monkeypatch.setattr(ar, "_cli_models_read", {})


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
    assert ar.detect_cli("claude") == {
        "installed": False, "version": None, "registered": False, "logged_in": None,
    }


def test_detect_cli_installed_with_version(monkeypatch):
    import windows.agent_runners as ar

    class _FakeResult:
        stdout = "1.2.3\n"
        stderr = ""

    monkeypatch.setattr(ar.shutil, "which", lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **kw: _FakeResult())
    monkeypatch.setattr(ar, "_is_registered", lambda name: True)
    monkeypatch.setattr(ar, "claude_is_logged_in", lambda: True)
    assert ar.detect_cli("claude") == {
        "installed": True, "version": "1.2.3", "registered": True, "logged_in": True,
    }


def test_detect_cli_installed_version_check_fails(monkeypatch):
    import windows.agent_runners as ar

    def _raise(*a, **kw):
        raise OSError("timed out")

    monkeypatch.setattr(ar.shutil, "which", lambda name: "/usr/local/bin/" + name)
    monkeypatch.setattr(ar.subprocess, "run", _raise)
    monkeypatch.setattr(ar, "_is_registered", lambda name: False)
    assert ar.detect_cli("codex") == {"installed": True, "version": None, "registered": False,
                                      "logged_in": None}
    # A CLI with no browser sign-in reports no login state at all.
    assert "logged_in" not in ar.detect_cli("opencode")


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

    # Codex and Cursor list their own models; built in is only "CLI default".
    assert [m["id"] for m in models_for_backend(BACKEND_CODEX)] == ["cli-default"]
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
    assert "dry_run=true" in text
    assert "import_files_tool" in text
    assert "list_files_tool only lists" in text
    assert "C:/Users/" in text or "/c/Users/" in text
    assert "IMMEDIATELY" in text
    assert "/mnt/c" in text
    assert "Do NOT use Glob" in text or "do NOT use Glob" in text
    assert "sips" in text
    assert "filter_complex" in text
    assert "${!" in text
    assert "-vf" in text
    assert "HEIC" in text


def test_claude_argv_appends_system_prompt(qapp, monkeypatch, tmp_path):
    import windows.agent_runners as ar
    from windows.agent_runners import ClaudeCodeRunner, _claude_code_system_prompt

    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")
    runner = ClaudeCodeRunner()
    runner._session_id = "s7"
    runner._cli_session_id = "s7"
    monkeypatch.setattr(ar, "_agent_mcp_dir", lambda: str(tmp_path))
    argv = runner._build_argv("hi")
    # From a file: the prompt is many lines, and an npm install's claude.cmd
    # would cut the whole command line at the first of them.
    with open(argv[argv.index("--append-system-prompt-file") + 1], encoding="utf-8") as fh:
        prompt = fh.read()
    assert prompt == _claude_code_system_prompt()
    assert "ingest_web_video_tool" in prompt
    assert "ToolSearch" in prompt
    assert "watch_clip_window_tool" in prompt
    assert "HEIC" in prompt


def test_is_cli_auth_error_matches_oauth_strings():
    from windows.agent_runners import CLI_AUTH_REQUIRED, is_cli_auth_error

    assert is_cli_auth_error(
        "Failed to authenticate: OAuth session expired and could not be refreshed"
    )
    assert is_cli_auth_error(CLI_AUTH_REQUIRED)
    assert not is_cli_auth_error("MCP server not connected")
    assert not is_cli_auth_error("")


def test_claude_ensure_ready_blocks_when_signed_out(qapp, monkeypatch):
    import windows.agent_runners as ar
    from windows.agent_runners import CLI_AUTH_REQUIRED, ClaudeCodeRunner

    monkeypatch.setattr(ar, "claude_is_logged_in", lambda: False)
    runner = ClaudeCodeRunner()
    assert runner._ensure_ready() == CLI_AUTH_REQUIRED
    monkeypatch.setattr(ar, "claude_is_logged_in", lambda: True)
    assert runner._ensure_ready() is None
    monkeypatch.setattr(ar, "claude_is_logged_in", lambda: None)
    assert runner._ensure_ready() is None


def test_emit_error_routes_auth_to_auth_required(qapp):
    from windows.agent_runners import ClaudeCodeRunner

    runner = ClaudeCodeRunner()
    events = []
    runner.auth_required.connect(lambda t: events.append(("auth", t)))
    runner.error_occurred.connect(lambda t: events.append(("error", t)))
    runner._emit_error(
        "Failed to authenticate: OAuth session expired and could not be refreshed"
    )
    assert events and events[0][0] == "auth"
    assert not any(e[0] == "error" for e in events)


def test_an_expired_login_asks_for_sign_in_on_every_cli_that_has_one(qapp, monkeypatch):
    """Codex gets the same Sign-in card as Claude Code; a CLI that signs in
    through terminal prompts (OpenCode) keeps the plain error."""
    import windows.agent_runners as ar

    def events_of(runner, text):
        events = []
        runner.auth_required.connect(lambda t: events.append(("auth", t)))
        runner.error_occurred.connect(lambda t: events.append(("error", t)))
        runner._emit_error(text)
        return events

    expired = ("Your access token could not be refreshed because your refresh token "
               "was already used. Please log out and sign in again.")
    assert events_of(ar.CodexRunner(), expired) == [("auth", expired)]
    assert events_of(ar.OpenCodeRunner(), expired) == [("error", expired)]

    monkeypatch.setattr(ar, "codex_is_logged_in", lambda: False)
    assert ar.CodexRunner()._ensure_ready() == ar.CLI_AUTH_REQUIRED
    monkeypatch.setattr(ar, "codex_is_logged_in", lambda: None)
    assert ar.CodexRunner()._ensure_ready() is None
    assert ar.OpenCodeRunner()._ensure_ready() is None
    # The card names the CLI that needs it.
    runner, said = ar.CodexRunner(), []
    runner.auth_required.connect(said.append)
    runner._emit_auth_required(ar.CLI_AUTH_REQUIRED)
    assert said == ["Codex needs you to sign in again."]


def test_codex_login_state_is_its_status_exit_code(monkeypatch):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_which_cli", lambda name: "/bin/" + name)
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return types.SimpleNamespace(returncode=run.code, stdout="", stderr=run.err)

    monkeypatch.setattr(ar.subprocess, "run", run)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    run.code, run.err = 0, "Logged in using ChatGPT"
    assert ar.codex_is_logged_in() is True and seen[-1] == ["/bin/codex", "login", "status"]
    run.code, run.err = 1, "Not logged in"
    assert ar.codex_is_logged_in() is False
    # Anything else is not an answer: an old CLI without `login status` must
    # not lock its user out of every turn.
    run.code, run.err = 2, "error: unrecognized subcommand 'status'"
    assert ar.codex_is_logged_in() is None
    # Nor is "not logged in" when an API key does the signing in.
    run.code, run.err = 1, "Not logged in"
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert ar.codex_is_logged_in() is None
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(ar, "_which_cli", lambda name: None)
    assert ar.codex_is_logged_in() is None


def test_sign_in_runs_the_clis_own_login_and_waits_for_it(monkeypatch):
    import windows.agent_runners as ar

    procs = []

    class FakeProc:
        def __init__(self, argv, **kw):
            self.argv, self.kw, self.killed = argv, kw, False
            self.stderr = None
            procs.append(self)

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    monkeypatch.setattr(ar, "_which_cli", lambda name: "/bin/" + name)
    monkeypatch.setattr(ar.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(ar.time, "sleep", lambda s: None)
    monkeypatch.setattr(ar, "_login_procs", {})
    answers = iter([False, False, True])
    monkeypatch.setattr(ar, "codex_is_logged_in", lambda: next(answers))
    assert ar.start_cli_login(ar.BACKEND_CODEX) == (True, "Signed in to Codex.")
    assert procs[0].argv == ["/bin/codex", "login"]
    assert procs[0].killed, "the login process does not outlive the sign-in"

    # A second click while the first sign-in is still waiting replaces it.
    monkeypatch.setattr(ar, "claude_is_logged_in", lambda: True)
    stale = FakeProc(["stale"])
    ar._login_procs[ar.BACKEND_CLAUDE] = stale
    assert ar.start_cli_login(ar.BACKEND_CLAUDE)[0] is True
    assert stale.killed and procs[-1].argv == ["/bin/claude", "auth", "login"]

    ok, message = ar.start_cli_login(ar.BACKEND_OPENCODE)
    assert ok is False and "OpenCode" in message


def test_windows_children_can_find_programs_by_name(monkeypatch):
    """An MSYS2 login shell drops PATHEXT and COMSPEC. Without PATHEXT Claude
    Code cannot start the browser for its sign-in, and just waits."""
    import windows.agent_runners as ar

    if os.name != "nt":
        pytest.skip("Windows environment variables")
    monkeypatch.delenv("PATHEXT", raising=False)
    monkeypatch.delenv("COMSPEC", raising=False)
    env = {k.upper(): v for k, v in ar._cli_child_env().items()}
    assert ".EXE" in env["PATHEXT"].upper().split(";")
    assert env["COMSPEC"].lower().endswith("cmd.exe")
    monkeypatch.setenv("PATHEXT", ".EXE;.FOO")
    assert ar._cli_child_env()["PATHEXT"] == ".EXE;.FOO", "the user's own is kept"


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
    assert [m["id"] for m in models_for_backend(BACKEND_CODEX)] == ["cli-default"]

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
    assert "make a cut" not in argv, "the prompt goes in on stdin"
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
    monkeypatch.setattr(ar, "_cli_models_read", {})
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
    # The menu opens on a short list, not on "CLI default" alone; search
    # reaches the rest.
    assert [r["id"] for r in rows if r["featured"]] == [r["id"] for r in rows[:9]]
    assert rows[9]["featured"] is False
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
    monkeypatch.setattr(ar.CursorCliRunner, "list_models",
                        staticmethod(lambda cli: calls.append(cli) or answers.pop(0)))

    assert ar.refresh_cli_models(ar.BACKEND_CURSOR, "2026.09.18") is True
    assert [m["id"] for m in ar.models_for_backend(ar.BACKEND_CURSOR)] == ["auto"]
    # Detection runs every minute; the CLI is not asked again until it is due.
    assert ar.refresh_cli_models(ar.BACKEND_CURSOR, "2026.09.18") is False
    assert calls == ["/bin/cursor-agent"]
    # An update is due at once. This read fails, and the list stays.
    assert ar.refresh_cli_models(ar.BACKEND_CURSOR, "2026.09.28") is False
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
    # What the CLI listed beats the backend's lineup (#136).
    ar.set_live_lineups({ar.BACKEND_CURSOR: [{"id": "composer-2.5", "name": "Composer"}]})
    assert [m["id"] for m in ar.models_for_backend(ar.BACKEND_CURSOR)] == [
        "auto", "claude-opus-5-5-high"]


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


def test_a_failed_cursor_model_read_is_retried_soon(fresh_cursor_lineup, monkeypatch):
    """Seen in the app: logged out at startup, the list stayed empty for the
    full 15 minutes after the user signed in."""
    ar = fresh_cursor_lineup
    clock = [1000.0]
    answers = [[], [{"id": "auto", "name": "Auto"}]]
    calls = []
    monkeypatch.setattr(ar.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ar, "_which_cursor_cli", lambda: "/bin/cursor-agent")
    monkeypatch.setattr(ar.CursorCliRunner, "list_models",
                        staticmethod(lambda cli: calls.append(cli) or answers.pop(0)))

    assert ar.refresh_cli_models(ar.BACKEND_CURSOR, "v1") is False          # logged out
    clock[0] += 30
    assert ar.refresh_cli_models(ar.BACKEND_CURSOR, "v1") is False and len(calls) == 1
    clock[0] += 30                                           # next detection
    assert ar.refresh_cli_models(ar.BACKEND_CURSOR, "v1") is True and len(calls) == 2
    clock[0] += 120                                          # a success holds
    assert ar.refresh_cli_models(ar.BACKEND_CURSOR, "v1") is False and len(calls) == 2


# ── OpenCode ────────────────────────────────────────────────────────────────

class _OpenCodeServer:
    token = "tok"

    def start(self):
        return self

    def url(self):
        return "http://127.0.0.1:7434/mcp"


def test_opencode_parser_emits_expected_signals(qapp):
    """opencode_stream.jsonl is a real ``opencode run --format json`` capture
    (a resumed two-turn session with a failed read), with the first tool call
    renamed to an editor MCP tool."""
    from windows.agent_runners import OpenCodeRunner
    runner = OpenCodeRunner()
    runner._session_id = "s1"
    sessions = []
    runner.cli_session_changed.connect(lambda ui, cli, started, cwd: sessions.append(cli))
    events = _collect(runner)
    _feed(runner, "opencode_stream.jsonl")

    # OpenCode mints its own id; it is captured once for --session resume.
    assert runner._cli_session_id == "ses_f2b3f9489ffelMwif35FMOUI5P"
    assert runner._cli_id_from_cli
    assert sessions == ["ses_f2b3f9489ffelMwif35FMOUI5P"]

    started = [e for e in events if e[0] == "tool_started"]
    assert started[0][1] == "list_files_tool", "server prefix stripped"
    done = {e[1]: e for e in events if e[0] == "tool_completed"}
    assert done[started[0][2]][2:] == (True, "FIXTURE: 3 files")
    # OpenCode's own "read" is renamed so it is not labelled a motion-graphics step.
    read_call = next(e for e in started if e[1] == "read_file")
    assert done[read_call[2]][2] is False
    assert "File not found" in done[read_call[2]][3]
    # Reasoning is surfaced as a collapsible "thinking" block.
    assert any(e[1] == "thinking" for e in started)
    # Every text part streams, and all of them make up the final answer.
    assert [e[1] for e in events if e[0] == "token"][0] == "There are 3 files."
    assert runner._final_text.startswith("There are 3 files.\n\n")
    assert "zenvi-ok" in runner._final_text


def test_opencode_error_event_becomes_the_reported_error(qapp):
    from windows.agent_runners import OpenCodeRunner
    runner = OpenCodeRunner()
    runner._handle_event({"type": "error", "sessionID": "ses_x", "error": {
        "name": "UnknownError", "data": {"message": "Unexpected server error."}}})
    assert runner._last_error == "Unexpected server error."


def test_opencode_argv_runs_headless_and_resumes_only_a_real_session(qapp, monkeypatch):
    from windows.agent_runners import OpenCodeRunner

    import classes.file_drop as file_drop
    monkeypatch.setattr(file_drop, "media_add_dirs", lambda home=None: [])
    runner = OpenCodeRunner()
    runner._server = _OpenCodeServer()
    runner._cli_session_id = "placeholder-from-the-ui"
    argv = runner._build_argv("hi")
    assert argv[1] == "run" and argv[-1] == "hi"
    assert argv[argv.index("--format") + 1] == "json"
    assert "--auto" in argv, "no terminal to answer a permission prompt"
    assert "--session" not in argv, "OpenCode rejects ids it did not mint"

    runner._cli_started = True
    runner._cli_session_id = "ses_real"
    runner._cli_id_from_cli = True
    argv = runner._build_argv("again")
    assert argv[argv.index("--session") + 1] == "ses_real"


def test_opencode_env_points_at_a_scoped_config_with_the_editor_server(
        qapp, monkeypatch, tmp_path):
    """A scoped OPENCODE_CONFIG is merged over the user's own config, so their
    other MCP servers keep working; the token never lands in the file."""
    from classes import info
    from windows.agent_runners import OpenCodeRunner

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path))
    runner = OpenCodeRunner()
    runner._server = _OpenCodeServer()
    env = runner._build_env()

    assert env["ZENVI_MCP_TOKEN"] == "tok"
    with open(env["OPENCODE_CONFIG"], encoding="utf-8") as fh:
        text = fh.read()
    entry = json.loads(text)["mcp"]["zenvi_editor"]
    assert entry["type"] == "remote"
    assert entry["url"] == "http://127.0.0.1:7434/mcp"
    assert entry["headers"]["Authorization"] == "Bearer {env:ZENVI_MCP_TOKEN}"
    assert entry["enabled"] is True
    assert "tok" not in text.replace("{env:ZENVI_MCP_TOKEN}", "")


def test_opencode_run_request_closes_stdin(qapp, monkeypatch, tmp_path):
    """``opencode run`` waits on an inherited stdin forever; it must get EOF."""
    import windows.agent_runners as ar
    from windows.agent_runners import OpenCodeRunner

    monkeypatch.setattr("classes.agent_mcp_server.get_mcp_server", lambda: _OpenCodeServer())
    monkeypatch.setattr(ar, "_which_cli", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ar, "_project_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(OpenCodeRunner, "_build_env", lambda self: dict(os.environ))
    monkeypatch.setattr(OpenCodeRunner, "_build_argv", lambda self, text: [
        sys.executable, "-c",
        "import sys, json; sys.stdin.read(); print(json.dumps({'type': 'text', "
        "'sessionID': 'ses_1', 'part': {'type': 'text', 'text': 'done'}}))"])

    runner = OpenCodeRunner()
    runner._session_id = "s7"
    events = _collect(runner)
    runner.run_request("hello", "")
    assert ("response_ready", "done") in events


def test_opencode_is_registered_reads_json_and_jsonc(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_opencode_config_dir", lambda: str(tmp_path))
    assert ar._is_registered("opencode") is False
    (tmp_path / "opencode.jsonc").write_text(
        '{\n  // mine\n  "mcp": {"zenvi_editor": {"type": "remote"}}\n}\n')
    assert ar._is_registered("opencode") is True


def test_register_opencode_creates_new_file(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_opencode_config_dir", lambda: str(tmp_path / "opencode"))
    ok, message = ar.register_opencode(7434, "tok123")
    assert ok is True
    assert "ZENVI_MCP_TOKEN=tok123" in message

    data = json.loads((tmp_path / "opencode" / "opencode.json").read_text())
    entry = data["mcp"]["zenvi_editor"]
    assert entry["url"] == "http://127.0.0.1:7434/mcp"
    assert entry["headers"]["Authorization"] == "Bearer {env:ZENVI_MCP_TOKEN}"
    assert ar._is_registered("opencode") is True


def test_register_opencode_keeps_other_servers_and_updates_stale_port(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "model": "anthropic/claude-sonnet-5",
        "mcp": {
            "playwright": {"type": "local", "command": ["npx", "@playwright/mcp"]},
            "zenvi_editor": {"type": "remote", "url": "http://127.0.0.1:9999/mcp"},
        },
    }))
    monkeypatch.setattr(ar, "_opencode_config_dir", lambda: str(tmp_path))

    ok, _ = ar.register_opencode(7434, "tok123")
    assert ok is True
    data = json.loads(cfg.read_text())
    assert data["mcp"]["zenvi_editor"]["url"] == "http://127.0.0.1:7434/mcp"
    assert data["mcp"]["playwright"]["command"] == ["npx", "@playwright/mcp"]
    assert data["model"] == "anthropic/claude-sonnet-5"
    assert (tmp_path / "opencode.json.zenvi-backup").exists()


def test_register_opencode_refuses_to_touch_invalid_json(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "opencode.json"
    original = '{"mcp": {  // a comment is not JSON'
    cfg.write_text(original)
    monkeypatch.setattr(ar, "_opencode_config_dir", lambda: str(tmp_path))

    ok, _ = ar.register_opencode(7434, "tok123")
    assert ok is False
    assert cfg.read_text() == original


def test_which_cli_finds_opencode_outside_path(monkeypatch, tmp_path):
    """A GUI-launched editor often has a trimmed PATH: find OpenCode in its
    official installer dir and in an nvm-windows node folder."""
    import windows.agent_runners as ar

    name = "opencode.exe" if os.name == "nt" else "opencode"
    monkeypatch.setattr(ar.shutil, "which", lambda n: None)
    monkeypatch.setattr(ar, "_resolved_home", lambda: str(tmp_path / "home"))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    nvm = tmp_path / "nvm4w" / "nodejs"
    nvm.mkdir(parents=True)
    (nvm / name).write_bytes(b"")
    monkeypatch.setenv("NVM_SYMLINK", str(nvm))
    assert ar._which_cli("opencode") == str(nvm / name)

    official = tmp_path / "home" / ".opencode" / "bin"
    official.mkdir(parents=True)
    (official / name).write_bytes(b"")
    assert ar._which_cli("opencode") == str(official / name)


def test_opencode_argv_skips_the_npm_cmd_shim(qapp, monkeypatch, tmp_path):
    """npm's ``opencode.cmd`` would pass the prompt through cmd.exe, which
    mangles quotes, ``&`` and ``%``; run the native binary it wraps instead."""
    from windows.agent_runners import OpenCodeRunner

    import classes.file_drop as file_drop
    monkeypatch.setattr(file_drop, "media_add_dirs", lambda home=None: [])
    shim = tmp_path / "opencode.cmd"
    shim.write_text("@echo off\n")
    native = tmp_path / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"")

    runner = OpenCodeRunner()
    runner._cli_path = str(shim)
    assert runner._build_argv('say "a & b" 100%')[0] == str(native)

    native.unlink()
    assert runner._build_argv("hi")[0] == str(shim)


@pytest.mark.skipif(os.name != "nt", reason="npm's sh shim only shadows on Windows")
def test_which_cli_prefers_a_windows_launcher_over_npms_sh_shim(monkeypatch, tmp_path):
    """npm drops an extension-less sh script next to ``opencode.cmd``; Windows
    cannot launch it (WinError 193)."""
    import windows.agent_runners as ar

    (tmp_path / "opencode").write_text("#!/bin/sh\n")
    (tmp_path / "opencode.cmd").write_text("@echo off\n")
    monkeypatch.setattr(ar.shutil, "which", lambda n: None)
    monkeypatch.setattr(ar, "_cli_install_dirs", lambda: [str(tmp_path)])
    assert ar._which_cli("opencode") == str(tmp_path / "opencode.cmd")


# opencode_mcp_turn.jsonl: opencode 1.18.32 `run --format json` against an
# MCP server named zenvi_editor, one call that worked and one that raised.
def test_opencode_mcp_turn_maps_tools_and_failures(qapp):
    from windows.agent_runners import OpenCodeRunner
    runner = OpenCodeRunner()
    events = _collect(runner)
    _feed(runner, "opencode_mcp_turn.jsonl")

    names = {e[2]: e[1] for e in events if e[0] == "tool_started"}
    done = {names[e[1]]: (e[2], e[3]) for e in events if e[0] == "tool_completed"}
    assert done["list_files_tool"][0] is True and "city.mp4" in done["list_files_tool"][1]
    assert done["add_clip_to_timeline_tool"] == (
        False, "Error executing tool add_clip_to_timeline_tool: Track 1 is locked")
    # Two text parts with a tool between them: the separator streams too.
    tokens = [e[1] for e in events if e[0] == "token"]
    assert tokens[0] == "I'll list the files first." and tokens[1] == "\n\n"
    assert runner._final_text == "".join(tokens)


def test_opencode_native_tools_are_not_labelled_as_motion_graphics(qapp):
    """The Zenvi Assistant harness is OpenCode, so its bare tool names carry
    motion-graphics labels in humanize_tool_name."""
    from classes.tool_handlers import humanize_tool_name
    from windows.agent_runners import OpenCodeRunner

    runner = OpenCodeRunner()
    seen = []
    runner.tool_started.connect(lambda c, n, a: seen.append(n))
    for i, tool in enumerate(("bash", "read", "edit", "write", "glob", "grep", "list", "webfetch")):
        runner._handle_event({"type": "tool_use", "sessionID": "ses_1", "part": {
            "tool": tool, "callID": "c%d" % i, "state": {"status": "completed", "input": {}}}})
    assert seen[:7] == ["run_shell_command", "read_file", "edit_file", "write_file",
                        "find_files", "search_files", "list_directory"]
    for name in seen:
        assert "motion graphic" not in humanize_tool_name(name).lower(), name


def test_parse_opencode_models_reads_the_real_listing():
    """opencode_models.txt is `opencode models` with no provider signed in."""
    from windows.agent_runners import parse_opencode_models

    with open(os.path.join(_FIX, "opencode_models.txt"),
              encoding="utf-8") as fh:
        rows = parse_opencode_models(fh.read())
    assert rows[0]["id"] == "cli-default" and rows[0]["default"] is True
    assert [r["id"] for r in rows[1:3]] == ["opencode/big-pickle",
                                           "opencode/ling-3.0-flash-fin-free"]
    assert rows[1]["name"] == "big-pickle" and rows[1]["provider"] == "opencode"
    assert [r["id"] for r in rows if r.get("featured")] == [r["id"] for r in rows[:9]]
    assert parse_opencode_models("Error: something\n") == []


def test_opencode_model_pill_lists_what_the_cli_reports(qapp, fresh_cursor_lineup, monkeypatch):
    ar = fresh_cursor_lineup
    monkeypatch.setattr(ar, "_which_cli", lambda name: "/bin/" + name)
    monkeypatch.setattr(ar.OpenCodeRunner, "list_models", staticmethod(
        lambda cli: ar.parse_opencode_models("anthropic/claude-sonnet-5\nopencode/big-pickle\n")))
    assert ar.refresh_cli_models(ar.BACKEND_OPENCODE, "1.18.32") is True
    ids = [m["id"] for m in ar.models_for_backend(ar.BACKEND_OPENCODE)]
    assert ids == ["cli-default", "anthropic/claude-sonnet-5", "opencode/big-pickle"]

    runner = ar.OpenCodeRunner()
    assert runner._coerce_model("opencode/big-pickle") == "opencode/big-pickle"
    runner._model_id = runner._coerce_model("cli-default")
    assert "--model" not in runner._build_argv("hi")
    runner._model_id = "anthropic/claude-sonnet-5"
    argv = runner._build_argv("hi")
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-5"


def test_register_opencode_does_not_rewrite_a_current_entry(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "opencode.json"
    users = json.dumps({"provider": {"x": {"options": {"apiKey": "sk-secret"}}}})
    cfg.write_text(users)
    monkeypatch.setattr(ar, "_opencode_config_dir", lambda: str(tmp_path))

    assert ar.register_opencode(7434, "tok")[0] is True
    written = cfg.read_text()
    os.utime(cfg, (1, 1))
    assert ar.register_opencode(7434, "tok")[0] is True
    assert cfg.read_text() == written and cfg.stat().st_mtime == 1
    assert ar.register_opencode(7435, "tok")[0] is True     # moved port
    assert (tmp_path / "opencode.json.zenvi-backup").read_text() == users, "first backup kept"
    assert not (tmp_path / "opencode.json.zenvi-tmp").exists()
    assert "tok" not in cfg.read_text().replace("{env:ZENVI_MCP_TOKEN}", "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_register_opencode_never_leaves_provider_keys_world_readable(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"provider": {"x": {"options": {"apiKey": "sk-secret"}}}}))
    cfg.chmod(0o600)
    monkeypatch.setattr(ar, "_opencode_config_dir", lambda: str(tmp_path))
    old = os.umask(0o022)
    try:
        assert ar.register_opencode(7434, "tok")[0] is True
    finally:
        os.umask(old)
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "opencode.json.zenvi-backup").stat().st_mode & 0o777 == 0o600


# ── Hermes (ACP over stdio) ─────────────────────────────────────────────────

class _HermesServer:
    token = "tok"
    port = 7434

    def start(self):
        return self

    def url(self):
        return "http://127.0.0.1:7434/mcp"


class _Pipe:
    def __init__(self):
        self.lines = []
        self.closed = False

    def write(self, data):
        self.lines.extend(l for l in data.splitlines() if l.strip())

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _HermesProc:
    def __init__(self):
        self.stdin = _Pipe()

    def poll(self):
        return None


def _hermes(text="hi", resume_id=""):
    from windows.agent_runners import HermesRunner
    runner = HermesRunner()
    runner._session_id = "ui-1"
    runner._server = _HermesServer()
    runner._cli_cwd = "/proj"
    if resume_id:
        runner._cli_started = True
        runner._cli_id_from_cli = True
        runner._cli_session_id = resume_id
    runner._proc = _HermesProc()
    runner._after_launch(text)
    return runner


def _sent(runner):
    return [json.loads(l) for l in runner._proc.stdin.lines]


def test_hermes_parser_drives_a_new_acp_session(qapp):
    """hermes_acp_stream.jsonl mirrors a real ``hermes acp`` (v0.15.2) turn
    against the in-app MCP server: initialize -> session/new -> prompt."""
    runner = _hermes("count the files")
    sessions = []
    runner.cli_session_changed.connect(lambda ui, cli, started, cwd: sessions.append(cli))
    events = _collect(runner)
    _feed(runner, "hermes_acp_stream.jsonl")

    sent = _sent(runner)
    assert [m["method"] for m in sent] == ["initialize", "session/new", "session/prompt"]
    new = sent[1]["params"]
    assert new["cwd"] == "/proj"
    assert new["mcpServers"] == [{
        "type": "http", "name": "zenvi_editor", "url": "http://127.0.0.1:7434/mcp",
        "headers": [{"name": "Authorization", "value": "Bearer tok"}],
    }]
    prompt = sent[2]["params"]
    assert prompt["sessionId"] == "fixture-hermes-session-1"
    assert prompt["prompt"] == [{"type": "text", "text": "count the files"}]

    # Hermes mints the id; it is kept for the next turn's session/load.
    assert runner._cli_session_id == "fixture-hermes-session-1"
    assert runner._cli_id_from_cli
    assert sessions == ["fixture-hermes-session-1"]

    started = [e for e in events if e[0] == "tool_started"]
    # Hermes' own "read" is renamed so it is not labelled a motion-graphics step.
    assert [e[1] for e in started] == ["list_files_tool", "terminal", "read_file"]
    done = {e[1]: e for e in events if e[0] == "tool_completed"}
    assert done["tc-list"][2] is True and "FIXTURE: 3 files" in done["tc-list"][3]
    assert done["tc-read"][2] is False and "File not found" in done["tc-read"][3]

    text = ("I'll list the files and run the command."
            "There are **3 files** and the shell printed `zenvi-ok`.")
    assert "".join(e[1] for e in events if e[0] == "token") == text
    # The reply keeps the prose before and after the tools apart, as the chat
    # committed it (#200).
    assert events[-1] == ("response_ready", text.replace("command.There", "command.\n\nThere"))
    # End of turn: stdin closes so ``hermes acp`` exits and the read loop ends.
    assert runner._proc.stdin.closed


def test_hermes_resumes_with_session_load_and_hides_the_replay(qapp):
    runner = _hermes("again", resume_id="ses-old")
    events = _collect(runner)

    runner._handle_event({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}})
    load = _sent(runner)[-1]
    assert load["method"] == "session/load"
    assert load["params"]["sessionId"] == "ses-old"
    assert load["params"]["mcpServers"][0]["name"] == "zenvi_editor"

    # session/load replays the old transcript before it answers.
    runner._handle_event({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "ses-old", "update": {"sessionUpdate": "agent_message_chunk",
                                           "content": {"type": "text", "text": "OLD"}}}})
    runner._handle_event({"jsonrpc": "2.0", "id": load["id"], "result": {"models": {}}})
    prompt = _sent(runner)[-1]
    assert prompt["method"] == "session/prompt"
    assert prompt["params"]["sessionId"] == "ses-old"
    assert not [e for e in events if e[0] == "token"], "replayed history must not re-render"


def test_hermes_starts_fresh_when_load_does_not_know_the_session(qapp):
    """Hermes answers session/load for an unknown id with an empty result."""
    runner = _hermes("hello", resume_id="gone")

    runner._handle_event({"jsonrpc": "2.0", "id": 1, "result": {}})
    load_id = _sent(runner)[-1]["id"]
    runner._handle_event({"jsonrpc": "2.0", "id": load_id, "result": {}})
    new = _sent(runner)[-1]
    assert new["method"] == "session/new"
    runner._handle_event({"jsonrpc": "2.0", "id": new["id"], "result": {"sessionId": "ses-new"}})
    prompt = _sent(runner)[-1]
    assert prompt["method"] == "session/prompt"
    assert prompt["params"]["sessionId"] == "ses-new"
    assert runner._cli_session_id == "ses-new"


def test_hermes_starts_fresh_when_load_fails_outright(qapp):
    runner = _hermes("hello", resume_id="gone")
    runner._handle_event({"jsonrpc": "2.0", "id": 1, "result": {}})
    load_id = _sent(runner)[-1]["id"]
    runner._handle_event({"jsonrpc": "2.0", "id": load_id, "error": {
        "code": -32002, "message": "Resource not found"}})
    assert _sent(runner)[-1]["method"] == "session/new"
    assert not runner._last_error
    assert not runner._proc.stdin.closed


def test_hermes_env_carries_the_token_its_config_entry_reads(qapp):
    """Connect writes ``Bearer ${ZENVI_MCP_TOKEN}`` into config.yaml, which
    ``hermes acp`` also loads; without the variable that entry gets a 401."""
    from windows.agent_runners import HermesRunner
    runner = HermesRunner()
    runner._server = _HermesServer()
    assert runner._build_env()["ZENVI_MCP_TOKEN"] == "tok"


def test_hermes_answers_agent_requests_so_a_turn_never_hangs(qapp):
    """No one can click a permission dialog; other client calls get an error."""
    runner = _hermes()
    runner._handle_event({"jsonrpc": "2.0", "id": 7, "method": "session/request_permission",
                          "params": {"sessionId": "s", "options": [
                              {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
                              {"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"}]}})
    reply = _sent(runner)[-1]
    assert reply["id"] == 7
    assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": "allow_once"}}

    runner._handle_event({"jsonrpc": "2.0", "id": 8, "method": "fs/read_text_file", "params": {}})
    reply = _sent(runner)[-1]
    assert reply["id"] == 8 and reply["error"]["code"] == -32601


def test_hermes_setup_error_is_reported_and_ends_the_run(qapp):
    runner = _hermes()
    runner._handle_event({"jsonrpc": "2.0", "id": 1, "result": {}})
    runner._handle_event({"jsonrpc": "2.0", "id": 2, "error": {
        "code": -32603, "message": "Internal error", "data": {
            "details": "No LLM provider configured. Run `hermes model` to select a provider."}}})
    assert "No LLM provider configured" in runner._last_error
    assert runner._proc.stdin.closed


def test_hermes_thoughts_become_one_thinking_block(qapp):
    runner = _hermes()
    events = _collect(runner)
    runner._handle_event({"jsonrpc": "2.0", "id": 1, "result": {}})
    runner._handle_event({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s"}})

    def update(kind, text):
        runner._handle_event({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s", "update": {"sessionUpdate": kind,
                                         "content": {"type": "text", "text": text}}}})

    update("agent_thought_chunk", "let me ")
    update("agent_thought_chunk", "think")
    update("agent_message_chunk", "Done.")
    assert [e[0] for e in events] == [
        "tool_started", "tool_log", "tool_log", "tool_completed", "token"]
    assert events[0][1] == "thinking"


def test_hermes_run_request_talks_acp_over_stdin_and_exits(qapp, monkeypatch, tmp_path):
    """End to end against a stand-in ``hermes acp`` that only exits once its
    stdin closes, like the real one."""
    import windows.agent_runners as ar
    from windows.agent_runners import HermesRunner

    fake = tmp_path / "fake_hermes.py"
    fake.write_text(
        "import json, sys\n"
        "def out(m):\n"
        "    sys.stdout.write(json.dumps(m) + '\\n'); sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') == 'initialize':\n"
        "        out({'jsonrpc': '2.0', 'id': m['id'], 'result': {'protocolVersion': 1}})\n"
        "    elif m.get('method') == 'session/new':\n"
        "        out({'jsonrpc': '2.0', 'id': m['id'], 'result': {'sessionId': 'ses-1'}})\n"
        "    elif m.get('method') == 'session/prompt':\n"
        "        out({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 'ses-1',\n"
        "             'update': {'sessionUpdate': 'agent_message_chunk',\n"
        "                        'content': {'type': 'text', 'text': 'done'}}}})\n"
        "        out({'jsonrpc': '2.0', 'id': m['id'], 'result': {'stopReason': 'end_turn'}})\n"
    )
    monkeypatch.setattr("classes.agent_mcp_server.get_mcp_server", lambda: _HermesServer())
    monkeypatch.setattr(ar, "_which_cli", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ar, "_project_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(HermesRunner, "_build_env", lambda self: dict(os.environ))
    monkeypatch.setattr(HermesRunner, "_build_argv",
                        lambda self, text: [sys.executable, str(fake)])

    runner = HermesRunner()
    runner._session_id = "s9"
    events = _collect(runner)
    runner.run_request("hello", "")
    assert ("response_ready", "done") in events
    assert runner._proc.returncode == 0
    assert runner._cli_session_id == "ses-1"


def test_hermes_argv_runs_the_acp_adapter(qapp):
    from windows.agent_runners import HermesRunner
    runner = HermesRunner()
    runner._cli_path = "/usr/bin/hermes"
    argv = runner._build_argv("hi")
    assert argv[:2] == ["/usr/bin/hermes", "acp"]
    assert "hi" not in argv, "the prompt travels over ACP, not argv"


def test_hermes_is_registered_reads_its_config_yaml(monkeypatch, tmp_path):
    import windows.agent_runners as ar

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert ar._is_registered("hermes") is False
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: anthropic\n  zenvi_editor: nope\n")
    assert ar._is_registered("hermes") is False, "only an mcp_servers entry counts"
    (tmp_path / "config.yaml").write_text(
        "model:\n  provider: anthropic\nmcp_servers:\n  other:\n    command: npx\n"
        "  zenvi_editor:\n    url: http://127.0.0.1:7434/mcp\n")
    assert ar._is_registered("hermes") is True


def test_register_hermes_writes_url_and_token_header_via_the_cli(monkeypatch):
    import windows.agent_runners as ar

    calls = []

    class _Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    envs = []
    monkeypatch.setattr(ar, "_which_cli", lambda name: "/usr/bin/hermes")
    monkeypatch.setattr(ar, "_cli_child_env", lambda extra=None: {"USERPROFILE": "/resolved"})
    monkeypatch.setattr(ar.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or envs.append(kw.get("env")) or _Done())
    ok, message = ar.register_hermes(7434, "tok123")
    assert ok is True
    # Same home as the runner and the "connected" check, not the GUI's.
    assert envs == [{"USERPROFILE": "/resolved"}] * 2
    assert ["/usr/bin/hermes", "config", "set", "mcp_servers.zenvi_editor.url",
            "http://127.0.0.1:7434/mcp"] in calls
    assert ["/usr/bin/hermes", "config", "set",
            "mcp_servers.zenvi_editor.headers.Authorization",
            "Bearer ${ZENVI_MCP_TOKEN}"] in calls
    assert "ZENVI_MCP_TOKEN=tok123" in message
    assert not any("tok123" in " ".join(c) for c in calls), "token stays out of the file"

    _Done.returncode = 1
    _Done.stderr = "config.yaml is not valid YAML"
    ok, message = ar.register_hermes(7434, "tok123")
    assert ok is False and "not valid YAML" in message


# hermes_acp_v019_turn.jsonl: hermes-agent 0.19.0 `acp` against an MCP server
# named zenvi_editor (the answers to initialize, session/new, session/prompt).
def test_hermes_019_names_editor_tools_and_sees_a_failure_inside_completed(qapp):
    runner = _hermes("list, then add the shorter clip")
    events = _collect(runner)
    _feed(runner, "hermes_acp_v019_turn.jsonl")

    names = {e[2]: e[1] for e in events if e[0] == "tool_started"}
    assert sorted(names.values()) == ["add_clip_to_timeline_tool", "list_files_tool"]
    done = {names[e[1]]: (e[2], e[3]) for e in events if e[0] == "tool_completed"}
    # The untrusted-content wrapper and its warning are gone; the payload stays.
    assert done["list_files_tool"][0] is True
    assert done["list_files_tool"][1].startswith('[{"name": "beach.mp4"')
    # Hermes reports a tool that raised as "completed"; the error is in the body.
    assert done["add_clip_to_timeline_tool"] == (
        False, "Error executing tool add_clip_to_timeline_tool: Track 1 is locked")
    assert events[-1][0] == "response_ready" and "Track 1 is locked" in events[-1][1]
    assert runner._proc.stdin.closed


@pytest.mark.parametrize("title,name", [
    ("mcp__zenvi_editor__list_files_tool", "list_files_tool"),
    ("mcp_zenvi_editor_list_files_tool", "list_files_tool"),       # Hermes 0.15
    ("python: import json", "python"),
    ("read: notes.txt", "read_file"),
    ("terminal: ls -la", "terminal"),
    (None, "tool"),
])
def test_hermes_tool_names(title, name):
    from classes.tool_handlers import humanize_tool_name
    from windows.agent_runners import _hermes_tool_name
    assert _hermes_tool_name(title) == name
    assert "motion graphic" not in humanize_tool_name(name).lower()


def test_only_clis_that_are_written_to_keep_stdin_open(qapp):
    """A CLI nobody writes to gets no stdin (``opencode run`` blocks on one).
    Hermes speaks ACP on it; Claude Code, Codex and Cursor take their prompt
    from it."""
    import subprocess
    from windows.agent_runners import CLI_RUNNERS, OpenCodeRunner
    for backend, runner in CLI_RUNNERS.items():
        expected = subprocess.DEVNULL if runner is OpenCodeRunner else subprocess.PIPE
        assert runner.STDIN == expected, backend


def test_hermes_offers_cli_default_and_drops_other_models(qapp):
    from windows.agent_runners import BACKEND_HERMES, HermesRunner, models_for_backend
    assert [m["id"] for m in models_for_backend(BACKEND_HERMES)] == ["cli-default"]
    runner = HermesRunner()
    assert runner.BACKEND_ID == BACKEND_HERMES
    assert runner._coerce_model("cli-default") == ""
    assert runner._coerce_model("claude-opus-5") == ""


def test_register_hermes_decodes_its_output_as_utf8(monkeypatch):
    """Seen in the app: Connect failed with "'ascii' codec can't decode byte
    0xe2". Hermes prints "✓ Set ...", and text=True follows a GUI app's empty
    locale."""
    import windows.agent_runners as ar

    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=0, stdout="✓ Set", stderr="")

    monkeypatch.setattr(ar.subprocess, "run", run)
    monkeypatch.setattr(ar, "_which_cli", lambda name: "/bin/hermes")
    ok, message = ar.register_hermes(7434, "tok")
    assert ok is True and "ZENVI_MCP_TOKEN=tok" in message
    assert [c[0][2:4] for c in calls] == [
        ["set", "mcp_servers.zenvi_editor.url"],
        ["set", "mcp_servers.zenvi_editor.headers.Authorization"]]
    assert calls[1][0][4] == "Bearer ${ZENVI_MCP_TOKEN}", "the token is not written"
    for _, kw in calls:
        assert kw.get("encoding") == "utf-8" and kw.get("errors") == "replace"
        assert "text" not in kw


# ── Model lineups come from the installed harness (#136) ──────────────────

CODEX_CATALOG = json.dumps({"models": [
    {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list", "priority": 13},
    {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide", "priority": 4},
    {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "visibility": "list", "priority": 8},
    {"slug": "gpt-5.6-terra", "display_name": "dup", "visibility": "list", "priority": 9},
    {"display_name": "no slug", "visibility": "list", "priority": 1},
]})


def test_parse_codex_models_lists_visible_models_by_priority():
    from windows.agent_runners import parse_codex_models

    rows = parse_codex_models(CODEX_CATALOG)
    assert [r["id"] for r in rows] == ["cli-default", "gpt-5.6-terra", "gpt-5.5"]
    assert rows[1]["name"] == "GPT-5.6-Terra"
    assert rows[0].get("default") is True and sum(1 for r in rows if r.get("default")) == 1


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"models": []}',
                                  '{"models": [{"slug": "x", "visibility": "hide"}]}'])
def test_parse_codex_models_gives_nothing_for_junk_or_an_empty_catalog(text):
    from windows.agent_runners import parse_codex_models
    assert parse_codex_models(text) == []


def test_every_harness_with_a_model_command_can_list_its_own_models():
    import windows.agent_runners as ar
    for backend in (ar.BACKEND_CODEX, ar.BACKEND_CURSOR, ar.BACKEND_OPENCODE):
        assert ar.CLI_RUNNERS[backend].list_models is not None, backend


def test_probe_codex_models_runs_codex_debug_models(monkeypatch):
    import windows.agent_runners as ar
    seen = []
    monkeypatch.setattr(ar, "_models_command_output",
                        lambda argv: seen.append(argv) or CODEX_CATALOG)
    assert [r["id"] for r in ar.probe_codex_models("/bin/codex")][1:] == ["gpt-5.6-terra", "gpt-5.5"]
    assert seen == [["/bin/codex", "debug", "models"]]


def test_the_installed_cli_lineup_beats_the_zenvi_backends(fresh_cursor_lineup):
    """The user's own CLI knows which ids it accepts; the API only fills gaps."""
    ar = fresh_cursor_lineup
    ar.set_live_lineups({ar.BACKEND_CODEX: [{"id": "from-api", "name": "API"}]})
    assert [m["id"] for m in ar.models_for_backend(ar.BACKEND_CODEX)] == ["from-api"]
    ar.set_cli_lineup(ar.BACKEND_CODEX, [{"id": "gpt-5.5", "name": "GPT-5.5"}])
    assert [m["id"] for m in ar.models_for_backend(ar.BACKEND_CODEX)] == ["gpt-5.5"]


def test_codex_lineup_is_asked_of_the_cli_and_reaches_the_model_flag(
        qapp, fresh_cursor_lineup, monkeypatch):
    ar = fresh_cursor_lineup
    monkeypatch.setattr(ar, "_which_cli", lambda name: "/bin/codex")
    monkeypatch.setattr(ar.CodexRunner, "list_models",
                        staticmethod(lambda cli: ar.parse_codex_models(CODEX_CATALOG)))
    assert ar.refresh_cli_models(ar.BACKEND_CODEX, "0.151.0") is True
    monkeypatch.setattr(ar, "_add_dir_args", lambda: [])
    runner = ar.CodexRunner()
    runner._server = None
    runner._model_id = runner._coerce_model("gpt-5.6-terra")
    argv = runner._build_argv("hi")
    assert argv[argv.index("--model") + 1] == "gpt-5.6-terra"


def test_a_models_command_that_fails_once_is_retried(monkeypatch):
    import subprocess
    import windows.agent_runners as ar
    runs = []

    def fake_run(argv, **kw):
        runs.append(argv)
        code = 1 if len(runs) == 1 else 0
        return subprocess.CompletedProcess(argv, code, stdout="" if code else "ok\n", stderr="boom")

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    monkeypatch.setattr(ar.time, "sleep", lambda s: None)
    assert ar._models_command_output(["x", "models"]) == "ok\n"
    assert len(runs) == 2


def test_a_models_command_that_keeps_failing_gives_up_with_nothing(monkeypatch):
    import subprocess
    import windows.agent_runners as ar
    monkeypatch.setattr(ar.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "e"))
    monkeypatch.setattr(ar.time, "sleep", lambda s: None)
    assert ar._models_command_output(["x", "models"]) == ""


@pytest.mark.parametrize("backend", ["codex", "cursor_cli", "opencode", "hermes", "claude_code"])
def test_a_model_id_from_another_harness_is_never_passed_on(qapp, fresh_cursor_lineup, backend):
    ar = fresh_cursor_lineup
    ar.set_cli_lineup("codex", [{"id": "gpt-5.5", "name": "GPT-5.5"}])
    ar.set_cli_lineup("opencode", [{"id": "openai/gpt-5.5", "name": "gpt-5.5"}])
    runner = ar.CLI_RUNNERS[backend]()
    assert runner._coerce_model("gpt-5.5") == ("gpt-5.5" if backend == "codex" else "")
    assert runner._coerce_model("openai/gpt-5.5") == ("openai/gpt-5.5" if backend == "opencode" else "")
    assert runner._coerce_model("anthropic/claude-opus-5-5") == ""


# ── Harness fixes found while verifying #136 (#281, #265) ─────────────────

def test_codex_resume_never_passes_add_dir(qapp, monkeypatch):
    """`codex exec resume` has no --add-dir: every follow-up turn exited 2 (#281)."""
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_add_dir_args", lambda: ["--add-dir", "C:/footage"])
    runner = ar.CodexRunner()
    runner._server = None
    assert "--add-dir" in runner._build_argv("first")
    runner._cli_started = runner._cli_id_from_cli = True
    runner._cli_session_id = "thread-1"
    argv = runner._build_argv("again")
    assert argv[1:4] == ["exec", "resume", "thread-1"]
    assert "--add-dir" not in argv
    # The prompt is read from stdin ("-"), so a .cmd launcher cannot cut it.
    assert argv[-1] == "-" and runner._stdin_prompt.endswith("again")


def test_which_cli_reads_the_windows_path_a_stripped_launch_lost(monkeypatch, tmp_path):
    """run-zenvi-core.sh starts the editor with `env -i`, so PATH and
    NVM_SYMLINK are gone; the user's real PATH is still in the registry (#265)."""
    import windows.agent_runners as ar

    name = "opencode.cmd" if os.name == "nt" else "opencode"
    node = tmp_path / "nodejs"
    node.mkdir()
    (node / name).write_bytes(b"")
    monkeypatch.setattr(ar.shutil, "which", lambda n, **kw: None)
    monkeypatch.setattr(ar, "_cli_install_dirs", lambda: [])
    monkeypatch.setattr(ar, "_windows_path_dirs", lambda: [str(node)])
    assert ar._which_cli("opencode") == os.path.join(str(node), name)
    # ...and the CLI's own children (node, git) resolve from it too.
    assert str(node) in ar._cli_child_env()["PATH"].split(os.pathsep)


def test_windows_path_dirs_expand_registry_variables():
    import windows.agent_runners as ar

    dirs = ar._expand_path_value(
        r"%NVM_SYMLINK%;C:\Tools;;%NOPE%\bin", {"NVM_SYMLINK": r"C:\nvm4w\nodejs"})
    assert dirs[:2] == [r"C:\nvm4w\nodejs", r"C:\Tools"]
    assert not [d for d in dirs if d.startswith(r"C:\nvm4w") is False and "%" in d and "NVM" in d]


def test_cursor_prompt_goes_through_stdin_not_the_cmd_launcher(qapp, monkeypatch):
    """cursor-agent.cmd runs through cmd.exe, which cuts an argument at its
    first newline: Cursor only ever saw "[Editor snapshot]" (#265)."""
    import subprocess
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_add_dir_args", lambda: [])
    runner = ar.CursorCliRunner()
    runner._cli_cwd = "/proj"
    prompt = "[Editor snapshot]\nclips: 0\n\nmake a cut"
    argv = runner._build_argv(prompt)
    assert prompt not in argv and not [a for a in argv if "\n" in a]
    assert runner.STDIN == subprocess.PIPE

    import time

    class _Raw:
        data, closed = "", False

        def write(self, text):
            self.data += text

        def close(self):
            self.closed = True

    runner._proc = types.SimpleNamespace(stdin=_Raw())
    runner._after_launch(prompt)
    for _ in range(200):
        if runner._proc.stdin.closed:
            break
        time.sleep(0.01)
    assert runner._proc.stdin.data == prompt
    assert runner._proc.stdin.closed, "EOF tells the CLI the prompt is complete"


def test_opencode_native_is_the_binary_the_shim_really_runs(tmp_path):
    """npm kept an old `opencode-ai` exe beside the new `@opencode/cli` one;
    running the stale one failed with "Token refresh failed: 401"."""
    import windows.agent_runners as ar

    old = tmp_path / "node_modules" / "opencode-ai" / "bin"
    new = tmp_path / "node_modules" / "@opencode" / "cli" / "bin"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "opencode.exe").write_bytes(b"")
    (new / "opencode.exe").write_bytes(b"")
    shim = tmp_path / "opencode.cmd"
    shim.write_text('@ECHO off\r\nCALL :find_dp0\r\n'
                    '"%dp0%\\node_modules\\@opencode\\cli\\bin\\opencode.exe"   %*\r\n')
    assert os.path.normpath(ar._opencode_native(str(shim))) == str(new / "opencode.exe")

    # A shim that names no exe: the legacy location still works.
    shim.write_text('@ECHO off\r\nnode "%dp0%\\x.js" %*\r\n')
    assert os.path.normpath(ar._opencode_native(str(shim))) == str(old / "opencode.exe")
    assert ar._opencode_native("/usr/bin/opencode") == "/usr/bin/opencode"


CLAUDE_INIT = {"type": "control_response", "response": {"subtype": "success", "response": {"models": [
    {"value": "default", "displayName": "Default (recommended)", "resolvedModel": "claude-sonnet-5",
     "description": "Sonnet 5 \u00b7 Efficient for routine tasks"},
    {"value": "sonnet", "displayName": "Sonnet", "resolvedModel": "claude-sonnet-5",
     "description": "Sonnet 5 \u00b7 Efficient"},
    {"value": "claude-fable-5-1[1m]", "displayName": "Fable",
     "description": "Fable 5.1 \u00b7 Most capable \u00b7 Requires usage credits"},
    {"value": "haiku", "displayName": "Haiku", "description": "Haiku 4.5 \u00b7 Fastest"},
    {"displayName": "no value"}, "junk",
]}}}


def test_parse_claude_models_reads_what_the_installed_cli_offers():
    """Claude Code lists its models in the reply to a stream-json `initialize`."""
    from windows.agent_runners import parse_claude_models

    lines = '{"type":"system"}\nnot json\n' + json.dumps(CLAUDE_INIT) + "\n"
    rows = parse_claude_models(lines)
    assert [r["id"] for r in rows] == ["cli-default", "sonnet", "claude-fable-5-1[1m]", "haiku"]
    assert rows[0]["tags"] == ["Sonnet 5"], "what the CLI's own default resolves to"
    assert [r["name"] for r in rows[1:]] == ["Sonnet 5", "Fable 5.1", "Haiku 4.5"]
    assert parse_claude_models('{"type":"system"}\n') == []
    assert parse_claude_models("") == []


def test_claude_models_are_named_by_the_cli_not_by_its_blurbs():
    """Some Claude Code builds describe a model without naming it ("For complex
    tasks"); the picker then listed the blurbs instead of the models."""
    from windows.agent_runners import parse_claude_models

    init = {"type": "control_response", "response": {"subtype": "success", "response": {"models": [
        {"value": "default", "displayName": "Default (recommended)", "resolvedModel": "claude-opus-5-5",
         "description": "Use the default model (currently Opus 5.5) · $4/$20 per Mtok"},
        {"value": "opus", "displayName": "Opus", "resolvedModel": "claude-opus-5-5",
         "description": "For complex work and everyday tasks"},
        {"value": "sonnet", "displayName": "Sonnet", "resolvedModel": "claude-sonnet-5-5",
         "description": "Sonnet 5.5 · Efficient for routine tasks"},
        {"value": "haiku", "description": "Fastest for quick answers"},
    ]}}}
    rows = parse_claude_models(json.dumps(init))
    assert [r["name"] for r in rows] == ["CLI default", "Opus", "Sonnet 5.5", "haiku"]
    assert rows[0]["tags"] == ["Opus"], "the model the default resolves to, not its blurb"


def test_claude_code_lists_its_own_models_and_passes_them_on(qapp, fresh_cursor_lineup, monkeypatch):
    ar = fresh_cursor_lineup
    assert ar.ClaudeCodeRunner.list_models is not None
    ar.set_cli_lineup(ar.BACKEND_CLAUDE, ar.parse_claude_models(json.dumps(CLAUDE_INIT)))
    runner = ar.ClaudeCodeRunner()
    assert runner._coerce_model("claude-fable-5-1[1m]") == "claude-fable-5-1[1m]"
    assert runner._coerce_model("cli-default") == ""


def test_hermes_home_follows_the_platform(monkeypatch, tmp_path):
    """Hermes keeps config.yaml under %LOCALAPPDATA%\\hermes on Windows, so
    Connect wrote a file Hermes never read."""
    import windows.agent_runners as ar

    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(ar, "_resolved_home", lambda: str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(ar.sys, "platform", "win32")
    assert ar._hermes_config_path() == os.path.join(str(tmp_path / "Local"), "hermes", "config.yaml")
    monkeypatch.setattr(ar.sys, "platform", "linux")
    assert ar._hermes_config_path() == os.path.join(str(tmp_path), ".hermes", "config.yaml")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert ar._hermes_config_path() == os.path.join(str(tmp_path / "custom"), "config.yaml")


HERMES_MODELS = {"availableModels": [
    {"modelId": "opencode-go:kimi-k2.6", "name": "kimi-k2.6",
     "description": "Provider: OpenCode Go \u2022 current"},
    {"modelId": "opencode-go:glm-5.2", "name": "glm-5.2", "description": "Provider: OpenCode Go"},
    {"name": "no id"},
], "currentModelId": "opencode-go:kimi-k2.6"}


def test_hermes_lineup_comes_from_its_acp_session(qapp, fresh_cursor_lineup):
    ar = fresh_cursor_lineup
    rows = ar.parse_hermes_models(HERMES_MODELS)
    assert [r["id"] for r in rows] == ["cli-default", "opencode-go:kimi-k2.6", "opencode-go:glm-5.2"]
    assert rows[0]["tags"] == ["kimi-k2.6"] and rows[1]["provider"] == "OpenCode Go"
    assert ar.parse_hermes_models({}) == [] and ar.parse_hermes_models(None) == []

    # A turn's session/new answer refreshes the picker for free.
    runner = _hermes("hi")
    runner._handle_event({"jsonrpc": "2.0", "id": 1, "result": {}})
    runner._handle_event({"jsonrpc": "2.0", "id": 2,
                          "result": {"sessionId": "s1", "models": HERMES_MODELS}})
    assert [m["id"] for m in ar.models_for_backend(ar.BACKEND_HERMES)][1:] == [
        "opencode-go:kimi-k2.6", "opencode-go:glm-5.2"]
    assert _sent(runner)[-1]["method"] == "session/prompt", "no model picked: straight to the prompt"


def test_hermes_sets_the_picked_model_before_prompting(qapp, fresh_cursor_lineup):
    ar = fresh_cursor_lineup
    ar.set_cli_lineup(ar.BACKEND_HERMES, ar.parse_hermes_models(HERMES_MODELS))
    runner = _hermes("hi")
    runner._model_id = runner._coerce_model("opencode-go:glm-5.2")
    runner._handle_event({"jsonrpc": "2.0", "id": 1, "result": {}})
    runner._handle_event({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "s1"}})
    set_model = _sent(runner)[-1]
    assert set_model["method"] == "session/set_model"
    assert set_model["params"] == {"sessionId": "s1", "modelId": "opencode-go:glm-5.2"}
    runner._handle_event({"jsonrpc": "2.0", "id": set_model["id"], "result": {}})
    prompt = _sent(runner)[-1]
    assert prompt["method"] == "session/prompt" and prompt["params"]["sessionId"] == "s1"


def test_claude_command_line_survives_a_cmd_launcher(qapp, monkeypatch, tmp_path):
    """npm installs Claude Code as claude.cmd, and cmd.exe cuts a command line
    at its first newline: nothing multi-line may travel in argv."""
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")
    monkeypatch.setattr(ar, "_agent_mcp_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ar, "_add_dir_args", lambda: [])
    runner = ar.ClaudeCodeRunner()
    runner._cli_session_id = "s1"
    prompt = "[Editor snapshot]\nclips: 0\n\nmake a cut"
    argv = runner._build_argv(prompt)
    assert "-p" in argv and prompt not in argv
    assert not [a for a in argv if "\n" in a]

    class _Raw:
        data, closed = "", False

        def write(self, text):
            self.data += text

        def close(self):
            self.closed = True

    import time
    runner._proc = types.SimpleNamespace(stdin=_Raw())
    runner._after_launch(prompt)
    for _ in range(200):
        if runner._proc.stdin.closed:
            break
        time.sleep(0.01)
    assert runner._proc.stdin.data == prompt and runner._proc.stdin.closed


# ── Effort picker for the CLI harnesses (#147) ────────────────────────────

CLAUDE_EFFORT_INIT = json.dumps({"type": "control_response", "response": {"response": {"models": [
    {"value": "default", "description": "Sonnet 5 \u00b7 Efficient",
     "supportsEffort": True, "supportedEffortLevels": ["low", "medium", "high"]},
    {"value": "opus", "description": "Opus 5 \u00b7 Best", "supportsEffort": True,
     "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    {"value": "haiku", "description": "Haiku 4.5 \u00b7 Fastest"},
    {"value": "odd", "description": "Odd", "supportsEffort": False,
     "supportedEffortLevels": ["low"]},
]}}})

CODEX_EFFORT_CATALOG = json.dumps({"models": [
    {"slug": "a", "display_name": "A", "visibility": "list", "priority": 1,
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"}]},
    {"slug": "b", "display_name": "B", "visibility": "list", "priority": 2,
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, "junk", {"x": 1}]},
]})


def test_each_cli_reports_the_effort_levels_of_its_models(tmp_path):
    import windows.agent_runners as ar

    claude = {r["id"]: r.get("efforts") for r in ar.parse_claude_models(CLAUDE_EFFORT_INIT)}
    assert claude == {"cli-default": ["low", "medium", "high"],   # what the default resolves to
                      "opus": ["low", "medium", "high", "xhigh", "max"],
                      "haiku": None, "odd": None}

    codex = {r["id"]: r.get("efforts") for r in ar.parse_codex_models(CODEX_EFFORT_CATALOG)}
    assert codex["a"] == ["low", "high", "ultra"] and codex["b"] == ["low", "high"]
    # "CLI default" could be either model: only what every one of them takes.
    assert codex["cli-default"] == ["low", "high"]

    cache = tmp_path / "models.json"
    cache.write_text(json.dumps({
        "openai": {"models": {"gpt-5.5": {"reasoning_options": [
            {"type": "toggle"}, {"type": "effort", "values": ["none", "low", "high"]}]}}},
        "opencode": {"models": {"big-pickle": {"reasoning_options": []}, "old": {}}},
    }))
    efforts = ar._opencode_efforts(str(cache))
    assert efforts == {"openai/gpt-5.5": ["none", "low", "high"]}
    rows = {r["id"]: r.get("efforts") for r in ar.parse_opencode_models(
        "openai/gpt-5.5\nopencode/big-pickle\n", efforts)}
    assert rows == {"cli-default": None, "openai/gpt-5.5": ["none", "low", "high"],
                    "opencode/big-pickle": None}
    assert ar._opencode_efforts(str(tmp_path / "missing.json")) == {}
    cache.write_text("not json")
    assert ar._opencode_efforts(str(cache)) == {}


def test_efforts_reach_the_picker(fresh_cursor_lineup):
    ar = fresh_cursor_lineup
    ar.set_cli_lineup(ar.BACKEND_CODEX, ar.parse_codex_models(CODEX_EFFORT_CATALOG))
    rows = {m["id"]: m for m in ar.models_for_backend(ar.BACKEND_CODEX)}
    assert rows["a"]["efforts"] == ["low", "high", "ultra"]


def test_only_an_effort_the_chosen_model_offers_is_passed_on(qapp, fresh_cursor_lineup):
    ar = fresh_cursor_lineup
    ar.set_cli_lineup(ar.BACKEND_CODEX, ar.parse_codex_models(CODEX_EFFORT_CATALOG))
    runner = ar.CodexRunner()
    runner._model_id = "b"
    assert runner._coerce_effort("high") == "high"
    assert runner._coerce_effort("ultra") == "", "model a's level, not b's"
    assert runner._coerce_effort("") == "" and runner._coerce_effort(None) == ""
    runner._model_id = ""                      # "CLI default"
    assert runner._coerce_effort("low") == "low" and runner._coerce_effort("ultra") == ""
    # A harness whose models list no levels never passes one.
    assert ar.HermesRunner()._coerce_effort("high") == ""


def test_each_cli_gets_the_effort_in_its_own_dialect(qapp, monkeypatch, tmp_path):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_add_dir_args", lambda: [])
    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")
    monkeypatch.setattr(ar, "_agent_mcp_dir", lambda: str(tmp_path))

    claude = ar.ClaudeCodeRunner()
    claude._cli_session_id = "s1"
    assert "--effort" not in claude._build_argv("hi")
    claude._effort = "xhigh"
    argv = claude._build_argv("hi")
    assert argv[argv.index("--effort") + 1] == "xhigh"

    codex = ar.CodexRunner()
    codex._server = None
    assert not [a for a in codex._build_argv("hi") if "reasoning_effort" in a]
    codex._effort = "high"
    argv = codex._build_argv("hi")
    assert argv[argv.index('model_reasoning_effort="high"') - 1] == "-c"

    opencode = ar.OpenCodeRunner()
    opencode._model_id, opencode._effort = "openai/gpt-5.5", "high"
    argv = opencode._build_argv("hi")
    assert argv[argv.index("--model") + 1] == "openai/gpt-5.5#high"
    opencode._model_id = ""                    # no model picked: nothing to hang it on
    assert "--model" not in opencode._build_argv("hi")


def test_a_turn_uses_the_effort_the_chat_left_for_it_once(qapp, fresh_cursor_lineup, monkeypatch):
    ar = fresh_cursor_lineup
    ar.set_cli_lineup(ar.BACKEND_CODEX, ar.parse_codex_models(CODEX_EFFORT_CATALOG))
    runner = ar.CodexRunner()
    runner._pending_effort = "high"
    monkeypatch.setattr(ar, "_which_cli", lambda name: None)   # stop before launching
    import classes.agent_mcp_server as mcp
    monkeypatch.setattr(mcp, "get_mcp_server", lambda: types.SimpleNamespace(
        start=lambda: types.SimpleNamespace(token="t", port=1, url=lambda: "u")))
    runner.run_request("hi", "b")
    assert runner._effort == "high" and runner._pending_effort == ""
    runner.run_request("hi", "b")
    assert runner._effort == "", "the next turn does not inherit it"


# ── Ultracode and permission modes, as each CLI offers them (#147) ─────────

def _claude_probe_text(applied):
    init = {"type": "control_response", "response": {"request_id": "zenvi-models", "response": {"models": [
        {"value": "default", "displayName": "Default", "resolvedModel": "o", "supportsEffort": True,
         "supportedEffortLevels": ["low", "high"], "supportsAutoMode": True},
        {"value": "opus", "displayName": "Opus", "resolvedModel": "o", "supportsEffort": True,
         "supportedEffortLevels": ["low", "high"], "supportsAutoMode": True},
        {"value": "haiku", "displayName": "Haiku"},
    ]}}}
    settings = {"type": "control_response", "response": {"request_id": "zenvi-settings",
                                                         "response": {"applied": applied}}}
    return json.dumps(init) + "\n" + json.dumps(settings) + "\n"


def test_claude_offers_ultracode_and_modes_only_as_the_cli_reports_them():
    import windows.agent_runners as ar

    rows = {r["id"]: r for r in ar.parse_claude_models(
        _claude_probe_text({"ultracode": False, "ultracodeAvailable": True}))}
    assert rows["opus"]["efforts"] == ["low", "high", "ultracode"]
    assert rows["cli-default"]["efforts"] == ["low", "high", "ultracode"]
    assert rows["opus"]["modes"] == ["bypass", "auto", "plan"]
    assert rows["cli-default"]["modes"] == ["bypass", "auto", "plan"]
    # No effort levels: no ultracode. No auto mode: it is not offered.
    assert "efforts" not in rows["haiku"] and rows["haiku"]["modes"] == ["bypass", "plan"]

    # An older CLI answers get_settings without ultracodeAvailable (or not at all).
    for text in (_claude_probe_text({"ultracode": False}), _claude_probe_text(None),
                 _claude_probe_text({}).splitlines()[0]):
        assert ar.parse_claude_models(text)[1]["efforts"] == ["low", "high"]


def test_modes_reach_the_picker_and_only_an_offered_one_is_passed_on(qapp, fresh_cursor_lineup):
    ar = fresh_cursor_lineup
    ar.set_cli_lineup(ar.BACKEND_CLAUDE, ar.parse_claude_models(_claude_probe_text({})))
    rows = {m["id"]: m for m in ar.models_for_backend(ar.BACKEND_CLAUDE)}
    assert rows["haiku"]["modes"] == ["bypass", "plan"]
    runner = ar.ClaudeCodeRunner()
    runner._model_id = "haiku"
    assert runner._coerce_mode("plan") == "plan"
    assert runner._coerce_mode("auto") == "", "opus takes auto, haiku does not"
    assert runner._coerce_mode("nonsense") == "" and runner._coerce_mode(None) == ""
    # The other harnesses list theirs too; Hermes and Cursor have none.
    assert ar.parse_opencode_models("a/b\n")[1]["modes"] == ["bypass", "plan"]
    assert ar.parse_codex_models(CODEX_EFFORT_CATALOG)[1]["modes"] == ["bypass", "workspace", "readonly"]
    assert ar.HermesRunner()._coerce_mode("plan") == ""


def test_each_cli_gets_ultracode_and_the_mode_in_its_own_dialect(qapp, monkeypatch, tmp_path):
    import windows.agent_runners as ar

    monkeypatch.setattr(ar, "_add_dir_args", lambda: [])
    monkeypatch.setattr(ar, "_write_claude_mcp_config", lambda server: "/tmp/cfg.json")
    monkeypatch.setattr(ar, "_agent_mcp_dir", lambda: str(tmp_path))

    claude = ar.ClaudeCodeRunner()
    claude._cli_session_id = "s1"
    argv = claude._build_argv("hi")
    assert "--dangerously-skip-permissions" in argv and "--permission-mode" not in argv
    assert "--settings" not in argv
    claude._mode, claude._effort = "plan", "ultracode"
    argv = claude._build_argv("hi")
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in argv
    # Ultracode is a setting, not an --effort level.
    assert "--effort" not in argv
    assert json.load(open(argv[argv.index("--settings") + 1])) == {"ultracode": True}

    codex = ar.CodexRunner()
    codex._server = None
    assert "--dangerously-bypass-approvals-and-sandbox" in codex._build_argv("hi")
    # As -c settings: `codex exec resume` takes no --sandbox.
    codex._mode = "readonly"
    argv = codex._build_argv("hi")
    assert argv[argv.index('sandbox_mode="read-only"') - 1] == "-c"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    codex._mode = "workspace"
    assert 'sandbox_mode="workspace-write"' in codex._build_argv("hi")

    opencode = ar.OpenCodeRunner()
    assert "--agent" not in opencode._build_argv("hi")
    opencode._mode = "plan"
    argv = opencode._build_argv("hi")
    assert argv[argv.index("--agent") + 1] == "plan" and argv[-1] == "hi"


def test_a_turn_uses_the_mode_the_chat_left_for_it_once(qapp, fresh_cursor_lineup, monkeypatch):
    ar = fresh_cursor_lineup
    ar.set_cli_lineup(ar.BACKEND_CODEX, ar.parse_codex_models(CODEX_EFFORT_CATALOG))
    runner = ar.CodexRunner()
    runner._pending_mode = "readonly"
    monkeypatch.setattr(ar, "_which_cli", lambda name: None)   # stop before launching
    import classes.agent_mcp_server as mcp
    monkeypatch.setattr(mcp, "get_mcp_server", lambda: types.SimpleNamespace(
        start=lambda: types.SimpleNamespace(token="t", port=1, url=lambda: "u")))
    runner.run_request("hi", "b")
    assert runner._mode == "readonly" and runner._pending_mode == ""
    runner.run_request("hi", "b")
    assert runner._mode == "", "the next turn does not inherit it"


def test_opencode_v2_turns_run_on_a_private_server(qapp, monkeypatch):
    """OpenCode 2 sends `run` to a background service that keeps the config
    and environment it started with, so the turn's own OPENCODE_CONFIG (the
    editor's MCP server and token) never reached it: the agent had no editor
    tools. --standalone gives the turn its own server; OpenCode 1 has no such
    flag and no such service."""
    import windows.agent_runners as ar

    asked = []

    def run(argv, **kw):
        asked.append(argv)
        return types.SimpleNamespace(returncode=0, stderr="", stdout=run.help)

    monkeypatch.setattr(ar.subprocess, "run", run)
    monkeypatch.setattr(ar, "_opencode_standalone", {})
    runner = ar.OpenCodeRunner()
    runner._cli_path = "/bin/opencode"

    run.help = "  --standalone   Run with a private server instead of the background service\n"
    argv = runner._build_argv("hi")
    assert argv[1:3] == ["run", "--standalone"] and argv[-1] == "hi"
    runner._build_argv("again")
    assert len(asked) == 1 and asked[0][1:] == ["run", "--help"], "asked once per CLI"

    monkeypatch.setattr(ar, "_opencode_standalone", {})
    run.help = "  --format   json\n"
    assert "--standalone" not in runner._build_argv("hi")


def test_claude_code_is_started_with_workflows_on_so_ultracode_is_offered(monkeypatch):
    """Claude Code reports Ultracode as unavailable while its dynamic workflows
    are off for the account; this is its own switch for them."""
    import windows.agent_runners as ar

    monkeypatch.delenv("CLAUDE_CODE_WORKFLOWS", raising=False)
    assert ar._cli_child_env()["CLAUDE_CODE_WORKFLOWS"] == "1"
    monkeypatch.setenv("CLAUDE_CODE_WORKFLOWS", "0")
    assert ar._cli_child_env()["CLAUDE_CODE_WORKFLOWS"] == "0", "the user's own choice is kept"


def test_opencode_effort_levels_are_the_variants_opencode_itself_lists(monkeypatch):
    """OpenCode 2 builds each model's variants itself (`opencode api
    model.list`); the models.dev catalogue it caches lists fewer, so models
    with an effort choice in OpenCode showed none here."""
    import windows.agent_runners as ar

    listing = json.dumps({"data": [
        {"providerID": "opencode-go", "modelID": "deepseek-v4.1-flash",
         "variants": [{"id": "low"}, {"id": "high"}, {"id": "max"}, {"id": "low"}, "junk", {}]},
        {"providerID": "openrouter", "modelID": "xiaomi/mimo-v2.6-pro",
         "variants": [{"id": "none"}, {"id": "thinking"}]},
        {"providerID": "opencode", "modelID": "big-pickle", "variants": []},
        {"modelID": "no-provider"}, "junk",
    ]})
    assert ar.parse_opencode_variants(listing) == {
        "opencode-go/deepseek-v4.1-flash": ["low", "high", "max"],
        "openrouter/xiaomi/mimo-v2.6-pro": ["none", "thinking"],
        "opencode/big-pickle": [],
    }
    # Not an answer (OpenCode 1 has no `api` command): the catalogue is used.
    for text in ("", "<!doctype html>", json.dumps({"data": "x"}), json.dumps([1]),
                 json.dumps({"data": []}), json.dumps({"data": ["junk"]})):
        assert ar.parse_opencode_variants(text) is None

    outputs = {"models": "opencode-go/deepseek-v4.1-flash\nopencode/big-pickle\n", "api": listing}
    monkeypatch.setattr(ar, "_models_command_output", lambda argv, attempts=2: outputs[argv[1]])
    monkeypatch.setattr(ar, "_opencode_efforts", lambda path: {"opencode/big-pickle": ["high"]})
    rows = {r["id"]: r.get("efforts") for r in ar.probe_opencode_models("opencode")}
    assert rows["opencode-go/deepseek-v4.1-flash"] == ["low", "high", "max"]
    assert rows["opencode/big-pickle"] is None, "OpenCode's own answer wins over the catalogue"
    outputs["api"] = ""
    rows = {r["id"]: r.get("efforts") for r in ar.probe_opencode_models("opencode")}
    assert rows["opencode/big-pickle"] == ["high"] and rows["opencode-go/deepseek-v4.1-flash"] is None
