"""Prose streamed before a tool call stays on screen, in order.

A turn is text -> tools -> text -> ... Each stretch of prose is frozen into
its own bubble when the next tool starts, and the end-of-turn text is only
rendered for the part not already shown.
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


@pytest.fixture(scope="module")
def window_cls(qapp):
    for mod in ("PyQt5.QtWebEngineWidgets", "PyQt5.QtWebKitWidgets", "PyQt5.QtWebKit"):
        sys.modules.setdefault(mod, MagicMock())
    from windows.ai_chat_ui import AIChatWindow
    return AIChatWindow


@pytest.fixture
def keyed_store(monkeypatch, tmp_path):
    from classes import chat_history as ch
    ch.close()
    monkeypatch.setattr(ch, "CHAT_DB_PATH", str(tmp_path / "h.db"))
    yield ch
    ch.close()


def _window(window_cls, sid="s1"):
    """A stand-in carrying the real streaming / tool-start methods."""

    class W:
        _use_web_ui = True
        _active_session = window_cls._active_session
        _record_message = window_cls._record_message
        _record_tool_started = window_cls._record_tool_started
        _add_msg = window_cls._add_msg
        _add_assistant_msg = window_cls._add_assistant_msg
        _strip_thinking = staticmethod(window_cls._strip_thinking)
        _reset_turn_segments = staticmethod(window_cls._reset_turn_segments)
        _commit_streaming_segment = window_cls._commit_streaming_segment
        _final_segment_text = window_cls._final_segment_text
        _flush_token_buffer = window_cls._flush_token_buffer
        _on_token = window_cls._on_token
        _on_tool_started = window_cls._on_tool_started
        _user_cancelled = False

        def __init__(self):
            self._active_sid = sid
            self._sessions = {sid: {"messages": []}}
            self._token_buffer = []
            self._token_flush_scheduled = False
            self.js = []

        def sender(self):
            return types.SimpleNamespace(_session_id=sid)

        def _schedule_token_flush(self):
            self._token_flush_scheduled = True

        def _run_js(self, code):
            self.js.append(code)

    return W()


def _stream(win, *chunks):
    for c in chunks:
        win._on_token(c)


def test_pre_tool_prose_is_kept_as_its_own_bubble(window_cls, keyed_store):
    win = _window(window_cls)
    _stream(win, "Let me look at ", "the timeline first.")

    win._on_tool_started("call-1", "get_timeline_state_tool", "{}")

    # The bubble is frozen with proper markdown, not thrown away.
    committed = [c for c in win.js if "commitStreamingSegment" in c]
    assert len(committed) == 1 and "Let me look at the timeline first." in committed[0]
    assert not any("resetStreamingMessage" in c for c in win.js)
    # A fresh Thinking block opens below the prose, after the commit.
    assert any("reopenThinkingForTools" in c for c in win.js)
    assert win.js.index(committed[0]) < win.js.index(
        next(c for c in win.js if "reopenThinkingForTools" in c))
    # Kept for tab switching and for the restored transcript.
    msgs = win._sessions["s1"]["messages"]
    assert len(msgs) == 1 and msgs[0][0] == "assistant" and msgs[0][2] is True
    assert "Let me look at the timeline first." in msgs[0][1]
    stored = [(m["role"], m["content"]) for m in keyed_store.load_messages("s1")]
    assert stored == [("assistant", "Let me look at the timeline first.")]
    assert win._sessions["s1"]["turn_segments"] == ["Let me look at the timeline first."]
    assert win._sessions["s1"]["turn_tail"] == ""


def test_tool_blocks_restore_after_the_prose_that_preceded_them(window_cls, keyed_store):
    win = _window(window_cls)
    _stream(win, "Checking the clips.")
    win._on_tool_started("call-1", "list_clips_tool", "{}")

    msgs = keyed_store.load_messages("s1")
    events = keyed_store.load_tool_events("s1")
    assert len(msgs) == 1 and len(events) == 1
    assert events[0]["after_seq"] == msgs[0]["seq"]


def test_whitespace_only_prose_is_dropped_not_committed(window_cls, keyed_store):
    win = _window(window_cls)
    _stream(win, "  \n", "Thinking...\n")
    win._on_tool_started("call-1", "list_clips_tool", "{}")

    assert not any("commitStreamingSegment" in c for c in win.js)
    assert any("resetStreamingMessage" in c for c in win.js)
    assert keyed_store.load_messages("s1") == []
    assert win._sessions["s1"].get("turn_segments", []) == []


def test_final_text_that_is_only_the_last_message_renders_whole(window_cls):
    """Claude reports just the final assistant message: show it as-is."""
    win = _window(window_cls)
    sess = win._sessions["s1"]
    sess["turn_segments"] = ["First I looked."]
    sess["turn_tail"] = "All done, the clip is trimmed."

    assert win._final_segment_text(sess, "All done, the clip is trimmed.") == \
        "All done, the clip is trimmed."


def test_final_text_that_repeats_committed_prose_is_reduced_to_the_tail(window_cls):
    """Codex joins every message of the turn: only the unseen tail remains."""
    win = _window(window_cls)
    sess = win._sessions["s1"]
    sess["turn_segments"] = ["First I looked.", "Then I trimmed."]
    sess["turn_tail"] = "Done."

    joined = "First I looked.\n\nThen I trimmed.\n\nDone."
    assert win._final_segment_text(sess, joined) == "Done."
    # Nothing after the last tool: nothing left to render.
    sess["turn_tail"] = ""
    assert win._final_segment_text(sess, "First I looked.\n\nThen I trimmed.") == ""


def test_unrelated_final_text_and_no_segments_pass_through(window_cls):
    win = _window(window_cls)
    sess = win._sessions["s1"]
    assert win._final_segment_text(sess, "Hello") == "Hello"
    sess["turn_segments"] = ["First I looked."]
    sess["turn_tail"] = "partial"
    assert win._final_segment_text(sess, "Something else entirely") == \
        "Something else entirely"
