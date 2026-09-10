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
    monkeypatch.setattr(ar, "_which_cli", lambda name: None)

    runner = CodexRunner()
    runner._session_id = "s2"
    events = _collect(runner)
    runner.run_request("hello", "")

    assert any(e[0] == "error" and "Codex CLI not found" in e[1] for e in events)


# ── Model selection ────────────────────────────────────────────────────────

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
    """Claude/Codex cwd stays the project; --add-dir exposes Desktop/Downloads/…"""
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
        BACKEND_CLAUDE, BACKEND_CODEX, models_for_backend,
    )

    claude = models_for_backend(BACKEND_CLAUDE)
    assert claude, "Claude Code must offer a model list"
    assert all(m["id"] and m["name"] for m in claude)
    assert len({m["id"] for m in claude}) == len(claude), "duplicate model ids"
    assert sum(1 for m in claude if m.get("default")) == 1

    assert models_for_backend(BACKEND_CODEX) == []
    assert models_for_backend("zenvi") == []

    # Callers mutate what they get (the JS bridge tags entries), so the
    # catalogue itself must not be handed out by reference.
    claude[0]["name"] = "mutated"
    assert models_for_backend(BACKEND_CLAUDE)[0]["name"] != "mutated"


# ── Cancel ─────────────────────────────────────────────────────────────────

def test_cancel_does_not_disable_the_tab_for_later_messages(qapp, monkeypatch):
    """Stop must silence the turn in flight and nothing more — a cancelled tab
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
    assert not runner._stopping, "cancel is not a shutdown — the tab stays usable"


def test_codex_accumulates_several_assistant_messages(qapp):
    """A turn can complete more than one assistant message, and all of them
    stream into the same bubble — so the text ``turn.completed`` persists has
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
    session — so the failure has to reset the continuity."""
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
    project is the state the app launches in — a home-rooted cwd would hand the
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
