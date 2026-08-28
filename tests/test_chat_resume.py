"""CLI-agent resume continuity, and the local-vs-backend history merge.

These cover the state that makes a reopened chat tab rejoin its agent
conversation instead of silently starting a new one.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

from _qt_support import skip_without_pyqt5  # noqa: E402

skip_without_pyqt5()
from PyQt5.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def claude(qapp):
    from windows.agent_runners import ClaudeCodeRunner
    runner = ClaudeCodeRunner()
    runner._session_id = "tab-1"
    runner._server = types.SimpleNamespace(url=lambda: "http://127.0.0.1:1/mcp", token="t")
    return runner


@pytest.fixture
def codex(qapp):
    from windows.agent_runners import CodexRunner
    runner = CodexRunner()
    runner._session_id = "tab-1"
    runner._server = types.SimpleNamespace(url=lambda: "http://127.0.0.1:1/mcp", token="t")
    return runner


# ---------------------------------------------------------------------------
# Claude: the id is ours from the start, so only the create/resume flag matters
# ---------------------------------------------------------------------------

def test_claude_creates_then_resumes(claude, monkeypatch):
    monkeypatch.setattr("windows.agent_runners._write_claude_mcp_config", lambda s: "/tmp/cfg")
    claude._cli_session_id = "conv-1"

    argv = claude._build_argv("hello")
    assert "--session-id" in argv and "--resume" not in argv

    claude._cli_started = True
    argv = claude._build_argv("again")
    assert "--resume" in argv and "--session-id" not in argv
    assert argv[argv.index("--resume") + 1] == "conv-1"


def test_a_restored_tab_resumes_rather_than_recreating(claude, monkeypatch):
    """What _make_worker seeds onto a runner must be enough to resume."""
    monkeypatch.setattr("windows.agent_runners._write_claude_mcp_config", lambda s: "/tmp/cfg")
    claude._cli_session_id = "conv-from-disk"
    claude._cli_started = True
    claude._cli_id_from_cli = True

    argv = claude._build_argv("continue where we left off")
    assert argv[argv.index("--resume") + 1] == "conv-from-disk"


# ---------------------------------------------------------------------------
# Codex: the id can only be learned from its output stream
# ---------------------------------------------------------------------------

def test_codex_does_not_resume_a_seeded_placeholder(codex):
    """The uuid we seed is not a Codex thread, so it must not be resumed."""
    codex._cli_session_id = "seeded-uuid"
    codex._cli_started = True
    argv = codex._build_argv("hello")
    assert "resume" not in argv


def test_codex_learns_its_thread_id_and_reports_it(codex):
    seen = []
    codex.cli_session_changed.connect(
        lambda sid, cli, started, cwd: seen.append((sid, cli, started, cwd))
    )
    codex._cli_started = True
    codex._handle_event({"type": "thread.started", "thread_id": "th_123"})

    assert codex._cli_session_id == "th_123"
    assert codex._cli_id_from_cli is True
    # The window needs this to persist the id -- it is knowable nowhere else.
    assert seen and seen[-1][0] == "tab-1" and seen[-1][1] == "th_123"

    argv = codex._build_argv("again")
    assert argv[:3] == ["codex", "exec", "resume"]
    assert argv[3] == "th_123"


def test_codex_ignores_an_empty_thread_id(codex):
    codex._cli_session_id = "seeded"
    codex._handle_event({"type": "thread.started"})
    assert codex._cli_id_from_cli is False
    assert codex._cli_session_id == "seeded"


# ---------------------------------------------------------------------------
# Resume state across Stop and a moved project folder
# ---------------------------------------------------------------------------

def _stub_out_launch(monkeypatch, cwd):
    """Let run_request reach the continuity checks, then bail out."""
    monkeypatch.setattr("windows.agent_runners._project_cwd", lambda: cwd)
    boom = types.ModuleType("classes.agent_mcp_server")
    boom.get_mcp_server = lambda: (_ for _ in ()).throw(RuntimeError("no server"))
    monkeypatch.setitem(sys.modules, "classes.agent_mcp_server", boom)


def test_stop_mid_turn_still_leaves_the_conversation_resumable(claude, monkeypatch):
    """Latching at launch is what keeps a stopped turn recoverable.

    Before, only a turn that ran to completion set the flag, so the next
    message re-issued --session-id with an id the CLI had already taken.
    """
    monkeypatch.setattr("windows.agent_runners._write_claude_mcp_config", lambda s: "/tmp/cfg")
    claude._cli_session_id = "conv-1"
    claude._cli_started = True          # the CLI consumed the id, then Stop hit
    claude._cancelled = True

    argv = claude._build_argv("next message")
    assert "--resume" in argv, "a stopped turn must not try to re-create the session"


def test_moving_the_project_to_another_folder_starts_a_fresh_conversation(claude, monkeypatch):
    """The CLI stores transcripts per folder, so that resume is unreachable."""
    _stub_out_launch(monkeypatch, "/new/folder")
    claude._cli_session_id = "conv-1"
    claude._cli_started = True
    claude._cli_id_from_cli = True
    claude._cli_cwd = "/old/folder"

    claude.run_request("hi", "")

    assert claude._cli_started is False
    assert claude._cli_id_from_cli is False
    assert claude._cli_session_id != "conv-1"
    assert claude._cli_cwd == "/new/folder"


def test_staying_in_the_same_folder_keeps_the_conversation(claude, monkeypatch):
    _stub_out_launch(monkeypatch, "/same/folder")
    claude._cli_session_id = "conv-1"
    claude._cli_started = True
    claude._cli_cwd = "/same/folder"

    claude.run_request("hi", "")

    assert claude._cli_started is True
    assert claude._cli_session_id == "conv-1"


def test_clear_session_forgets_every_piece_of_continuity(claude):
    claude._cli_session_id = "conv-1"
    claude._cli_started = True
    claude._cli_id_from_cli = True
    claude._cli_cwd = "/somewhere"

    claude.clear_session()

    assert (claude._cli_session_id, claude._cli_started) == ("", False)
    assert (claude._cli_id_from_cli, claude._cli_cwd) == (False, "")


# ---------------------------------------------------------------------------
# History merge: local is authoritative, the backend may only extend it
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def window_cls():
    for mod in ("PyQt5.QtWebEngineWidgets", "PyQt5.QtWebKitWidgets", "PyQt5.QtWebKit"):
        sys.modules.setdefault(mod, MagicMock())
    from windows.ai_chat_ui import AIChatWindow
    return AIChatWindow


def _backend(*pairs):
    return [{"role": r, "content": c, "html_body": "", "is_assistant": r == "assistant"}
            for r, c in pairs]


def test_backend_tail_is_taken_when_local_is_a_prefix(window_cls):
    local = [("user", "hi"), ("assistant", "hello")]
    backend = _backend(("user", "hi"), ("assistant", "hello"), ("user", "and then?"))
    tail = window_cls._history_tail_beyond_local(window_cls, local, backend)
    assert [t["content"] for t in tail] == ["and then?"]


def test_nothing_is_taken_when_the_backend_has_no_more(window_cls):
    local = [("user", "hi"), ("assistant", "hello")]
    backend = _backend(("user", "hi"), ("assistant", "hello"))
    assert window_cls._history_tail_beyond_local(window_cls, local, backend) == []


def test_a_diverged_backend_transcript_is_ignored(window_cls):
    """If the two disagree, the local copy is the one we trust."""
    local = [("user", "hi"), ("assistant", "hello")]
    backend = _backend(("user", "something else"), ("assistant", "x"), ("user", "y"))
    assert window_cls._history_tail_beyond_local(window_cls, local, backend) == []


def test_whitespace_and_thinking_headers_do_not_count_as_divergence(window_cls):
    local = [("user", "hi"), ("assistant", "the answer")]
    backend = _backend(
        ("user", "hi\n"),
        ("assistant", "Thinking...\nthe   answer"),
        ("user", "more"),
    )
    tail = window_cls._history_tail_beyond_local(window_cls, local, backend)
    assert [t["content"] for t in tail] == ["more"]


# ---------------------------------------------------------------------------
# Window glue: which bucket a project's chats are filed under
# ---------------------------------------------------------------------------

class _StubWindow:
    """Just enough of AIChatWindow to exercise the real keying method."""

    def __init__(self, window_cls, project_id="", legacy=None):
        self._cls = window_cls
        self._pid = project_id
        self._legacy = legacy or {}
        self._draft_history_key = ""
        self._current_project_path = ""
        # The real method, bound to this stub.
        self._resolve_history_key = types.MethodType(
            window_cls._resolve_history_key, self
        )

    def _project_id(self):
        return self._pid

    def _load_chat_sessions_store(self, project_path=None):
        return self._legacy

    def resolve(self, path):
        return self._cls._resolve_history_key(self, path)


@pytest.fixture
def keyed_store(monkeypatch, tmp_path):
    from classes import chat_history as ch
    ch.close()
    monkeypatch.setattr(ch, "CHAT_DB_PATH", str(tmp_path / "h.db"))
    yield ch
    ch.close()


def test_an_untitled_project_gets_one_stable_draft_bucket(window_cls, keyed_store):
    win = _StubWindow(window_cls)
    first = win.resolve("")
    assert first.startswith("draft:")
    # Stable for the life of the window, so the chat doesn't split mid-session.
    assert win.resolve("") == first


def test_a_saved_project_is_keyed_on_its_own_id(window_cls, keyed_store, tmp_path):
    path = tmp_path / "a.zvn"
    path.write_text("{}")
    win = _StubWindow(window_cls, project_id="ABCDEF1234")
    assert win.resolve(str(path)) == "ABCDEF1234"


def test_a_project_with_no_usable_id_still_gets_a_bucket(window_cls, keyed_store, tmp_path):
    path = tmp_path / "a.zvn"
    path.write_text("{}")
    win = _StubWindow(window_cls, project_id="T0")
    assert win.resolve(str(path)) == keyed_store.path_key(str(path))


def test_the_old_json_store_is_imported_on_first_sight(window_cls, keyed_store, tmp_path):
    """Tab titles and backend choices survive the move to the new store."""
    path = tmp_path / "a.zvn"
    path.write_text("{}")
    legacy = {"sessions": [
        {"session_id": "old-1", "title": "Trim the intro", "backend": "codex"},
    ]}
    win = _StubWindow(window_cls, project_id="ABCDEF1234", legacy=legacy)
    key = win.resolve(str(path))

    rows = keyed_store.load_sessions(key)
    assert [(r["session_id"], r["title"], r["backend"]) for r in rows] == [
        ("old-1", "Trim the intro", "codex"),
    ]


# ---------------------------------------------------------------------------
# The capture hook: one place, every backend
# ---------------------------------------------------------------------------

def _message_window(window_cls, sid="s1"):
    """A stand-in carrying the real message-sink methods."""

    class W:
        _use_web_ui = True
        _active_session = window_cls._active_session
        _record_message = window_cls._record_message
        _add_msg = window_cls._add_msg
        _add_chrome_msg = window_cls._add_chrome_msg
        _add_user_msg = window_cls._add_user_msg
        _add_assistant_msg = window_cls._add_assistant_msg
        _add_system_msg = window_cls._add_system_msg
        _strip_thinking = staticmethod(window_cls._strip_thinking)

        def __init__(self):
            self._active_sid = sid
            self._sessions = {sid: {"messages": []}}

        def _run_js(self, code):
            pass

    return W()


def test_the_message_sink_persists_every_role(window_cls, keyed_store):
    win = _message_window(window_cls)
    win._add_user_msg("trim the first clip")
    win._add_assistant_msg("Thinking...\nDone — trimmed it.")
    win._add_system_msg("Error: backend unreachable")

    stored = [(m["role"], m["content"]) for m in keyed_store.load_messages("s1")]
    assert stored == [
        ("user", "trim the first clip"),
        ("assistant", "Done — trimmed it."),   # thinking header stripped
        ("system", "Error: backend unreachable"),
    ]


def test_ui_banners_are_shown_but_not_persisted(window_cls, keyed_store):
    win = _message_window(window_cls)
    win._add_chrome_msg("New session started. Ask anything about your project.")
    win._add_user_msg("hello")

    assert [m["content"] for m in keyed_store.load_messages("s1")] == ["hello"]
    # Still rendered in the pane, just not part of the transcript.
    assert len(win._sessions["s1"]["messages"]) == 2


# ---------------------------------------------------------------------------
# Project switching: when the open tabs come along, and when they don't
# ---------------------------------------------------------------------------

class _ReloadWindow(_StubWindow):
    """Stub for the continuity path of the real ``reload_for_project``."""

    def __init__(self, window_cls, project_id="", sessions=(), history_key=""):
        super().__init__(window_cls, project_id=project_id)
        self._sessions = {sid: {"messages": []} for sid in sessions}
        self._history_key = history_key
        self.saved = []

    def _save_chat_sessions_store(self, project_path=None):
        self.saved.append(project_path)

    def reload(self, path):
        return self._cls.reload_for_project(self, path)


def test_saving_an_untitled_project_carries_its_chat_over(window_cls, keyed_store, tmp_path):
    draft = keyed_store.new_draft_key()
    keyed_store.upsert_session("s1", draft)
    keyed_store.record_message("s1", "user", "typed before saving")

    path = tmp_path / "saved.zvn"
    path.write_text("{}")
    win = _ReloadWindow(window_cls, project_id="ABCDEF1234",
                        sessions=["s1"], history_key=draft)
    win.reload(str(path))

    # Same live tab, now filed under the real project.
    assert win._history_key == "ABCDEF1234"
    assert win._current_project_path == str(path)
    assert set(win._sessions) == {"s1"}
    assert [m["content"] for m in keyed_store.load_messages("s1")] == ["typed before saving"]
    assert keyed_store.load_sessions(draft) == []


def test_save_as_carries_the_tabs_and_leaves_a_snapshot(window_cls, keyed_store, tmp_path):
    original = tmp_path / "a.zvn"
    original.write_text("{}")
    keyed_store.upsert_session("s1", "ABCDEF1234", project_path=str(original))
    keyed_store.record_message("s1", "user", "shared so far")

    copy = tmp_path / "b.zvn"
    copy.write_text("{}")
    win = _ReloadWindow(window_cls, project_id="ABCDEF1234",
                        sessions=["s1"], history_key="ABCDEF1234")
    win.reload(str(copy))

    # The live tab follows the user into the copy...
    assert win._history_key.startswith("ABCDEF1234:")
    assert set(win._sessions) == {"s1"}
    # ...and the original keeps a snapshot under its own id.
    snapshot = keyed_store.load_sessions("ABCDEF1234")
    assert len(snapshot) == 1 and snapshot[0]["session_id"] != "s1"


def test_a_plain_resave_of_the_same_project_is_a_no_op(window_cls, keyed_store, tmp_path):
    path = tmp_path / "a.zvn"
    path.write_text("{}")
    win = _ReloadWindow(window_cls, project_id="ABCDEF1234",
                        sessions=["s1"], history_key="ABCDEF1234")
    win._current_project_path = str(path)
    win.reload(str(path))
    assert win.saved == []          # returned before doing any work


def test_starting_a_new_project_bins_an_unused_draft(window_cls, keyed_store):
    draft = keyed_store.new_draft_key()
    keyed_store.upsert_session("s1", draft)
    win = _ReloadWindow(window_cls, sessions=["s1"], history_key=draft)

    # An empty path means New Project; the teardown that follows needs the real
    # window, so just assert the draft was cleaned up on the way through.
    try:
        win.reload("")
    except Exception:
        pass
    assert keyed_store.load_sessions(draft) == []


def test_quitting_an_untitled_project_leaves_nothing_behind(window_cls, keyed_store):
    """Otherwise every app start with no project would leave a dead row."""
    draft = keyed_store.new_draft_key()
    keyed_store.upsert_session("s1", draft)

    class W:
        _sessions = {}
        _credits_timer = None
        _history_key = draft
        def _save_chat_sessions_store(self, project_path=None): pass
        def _shutdown_worker(self, *a, **kw): pass

    window_cls._stop_all_threads(W())
    assert keyed_store.load_sessions(draft) == []
