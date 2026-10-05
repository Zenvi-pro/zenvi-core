"""Switching agent harness mid-chat keeps the conversation (#136).

Zenvi Assistant, Claude Code, Codex... each keep their own memory, so a turn
one of them answered is unknown to the others. The chat hands the next agent
a bounded recap of the turns it has not seen.
"""

import sys
from unittest.mock import MagicMock

import pytest

from _qt_support import skip_without_pyqt5  # noqa: E402

skip_without_pyqt5()


def _msgs(*pairs):
    return [{"seq": i + 1, "role": r, "content": c} for i, (r, c) in enumerate(pairs)]


# ── the recap ─────────────────────────────────────────────────────────────

def test_recap_covers_only_turns_after_the_last_one_seen():
    from classes.chat_history import handoff_recap

    m = _msgs(("user", "cut the intro"), ("assistant", "Cut it."), ("user", "add music"))
    text = handoff_recap(m, after_seq=1)
    assert "Cut it." in text and "add music" in text
    assert "cut the intro" not in text
    assert text.index("Cut it.") < text.index("add music")


def test_recap_is_empty_when_nothing_is_new():
    from classes.chat_history import handoff_recap

    m = _msgs(("user", "hi"), ("assistant", "hello"))
    assert handoff_recap(m, after_seq=2) == ""
    assert handoff_recap([], after_seq=0) == ""


def test_recap_skips_system_banners_and_is_bounded():
    from classes.chat_history import handoff_recap

    m = _msgs(("system", "welcome"), *[("user" if i % 2 == 0 else "assistant", "x" * 5000)
                                      for i in range(30)])
    text = handoff_recap(m, after_seq=0)
    assert "welcome" not in text
    assert len(text) < 9000
    # the newest turns survive the cut, the oldest are dropped
    assert text.count("x") > 1000


# ── wiring in the chat window ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def window_cls():
    for mod in ("PyQt5.QtWebEngineWidgets", "PyQt5.QtWebKitWidgets", "PyQt5.QtWebKit"):
        sys.modules.setdefault(mod, MagicMock())
    from windows.ai_chat_ui import AIChatWindow
    return AIChatWindow


def _window(window_cls, sess, history):
    win = MagicMock()
    win._sessions = {"s1": sess}
    win._active_sid = "s1"
    win._active_session.return_value = sess
    win._use_web_ui = True
    win._make_worker.return_value = (MagicMock(), MagicMock())
    win._handoff_prefix = lambda s: window_cls._handoff_prefix(win, s)
    return win


@pytest.fixture
def history(monkeypatch):
    from classes import chat_history
    rows = _msgs(("user", "cut the intro"), ("assistant", "Cut it."))
    monkeypatch.setattr(chat_history, "load_messages", lambda sid: list(rows))
    return rows


def test_leaving_a_harness_remembers_how_much_it_saw_and_mutes_its_worker(window_cls, history):
    old = MagicMock()
    sess = {"backend": "zenvi", "worker": old, "thread": MagicMock(), "messages": []}
    win = _window(window_cls, sess, history)

    window_cls._set_session_backend(win, "s1", "codex")

    assert sess["seen_seq"] == {"zenvi": 2}
    old.blockSignals.assert_called_with(True)   # a late reply must not hit the new tab


def test_the_new_harness_gets_a_recap_the_first_time_and_only_what_it_missed_later(
        window_cls, history):
    sess = {"backend": "codex", "seen_seq": {"zenvi": 2}}
    win = _window(window_cls, sess, history)
    first = window_cls._handoff_prefix(win, sess)
    assert "cut the intro" in first and "Cut it." in first

    # back on Codex after Zenvi answered a third message: only that one
    history.append({"seq": 3, "role": "user", "content": "add music"})
    sess["seen_seq"] = {"zenvi": 3, "codex": 2}
    again = window_cls._handoff_prefix(win, sess)
    assert "add music" in again and "cut the intro" not in again


def test_a_tab_that_never_switched_gets_no_recap(window_cls, history):
    sess = {"backend": "zenvi"}
    assert window_cls._handoff_prefix(_window(window_cls, sess, history), sess) == ""


def test_the_recap_is_resent_until_the_harness_answers(window_cls, history):
    """A first turn that fails (not logged in, a bad flag) never reached the
    agent, so the recap must still be there on the retry."""
    sess = {"backend": "codex", "seen_seq": {"zenvi": 2}}
    win = _window(window_cls, sess, history)
    assert "Cut it." in window_cls._handoff_prefix(win, sess)
    assert "Cut it." in window_cls._handoff_prefix(win, sess)
    win.sender.return_value._session_id = "s1"
    win._user_cancelled = True     # shortest path through the slot
    window_cls._on_response_ready(win, "done")    # its reply arrived
    assert window_cls._handoff_prefix(win, sess) == ""
    # leaving and coming back resumes counting from where it left
    history.append({"seq": 3, "role": "user", "content": "add music"})
    window_cls._set_session_backend(win, "s1", "zenvi")
    assert sess["seen_seq"]["codex"] == 3
