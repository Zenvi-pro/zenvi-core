"""A tool that fails because nobody is signed in gets a Sign in button; the browser sign-in returns to the app by itself."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from windows import ai_chat_ui  # noqa: E402

CHAT_UI = SRC / "chat_ui"


class FakeSignal:
    def __init__(self):
        self.handlers = []

    def connect(self, fn):
        self.handlers.append(fn)

    def emit(self, *args):
        for h in list(self.handlers):
            h(*args)


class FakeLogin:
    instances = []

    def __init__(self, parent=None, browser_timeout=None):
        self.parent, self.browser_timeout, self.opened = parent, browser_timeout, False
        self.auth_completed, self.auth_cancelled = FakeSignal(), FakeSignal()
        FakeLogin.instances.append(self)

    def open(self):
        self.opened = True


class FakeMain:
    def __init__(self):
        self.calls = []

    def raise_(self):
        self.calls.append("raise")

    def activateWindow(self):
        self.calls.append("activate")


def make_window(monkeypatch, use_web=True):
    FakeLogin.instances = []
    monkeypatch.setattr("windows.login_window.LoginWindow", FakeLogin)
    js, main = [], FakeMain()
    win = SimpleNamespace(window=lambda: main, _use_web_ui=use_web, _run_js=js.append, refresh_credits_for_account=lambda: main.calls.append("credits"),
                          ZENVI_SIGN_IN_WAIT_SECONDS=ai_chat_ui.AIChatWindow.ZENVI_SIGN_IN_WAIT_SECONDS)
    return win, main, js


def sign_in(win):
    ai_chat_ui.AIChatWindow._sign_in_zenvi(win)


# ============================ the Python side ============================
def test_the_sign_in_opens_the_website_flow_and_waits_five_minutes(monkeypatch):
    win, main, js = make_window(monkeypatch)
    sign_in(win)
    dlg = FakeLogin.instances[0]
    assert dlg.opened and dlg.parent is main and dlg.browser_timeout == 300 and js == []


def test_on_success_the_app_comes_to_the_front_credits_refresh_and_the_page_is_told(monkeypatch):
    win, main, js = make_window(monkeypatch)
    sign_in(win)
    FakeLogin.instances[0].auth_completed.emit({"user_email": "nilay@example.com"})
    assert main.calls == ["raise", "activate", "credits"]
    assert js == ["if(window.onZenviSignInResult) onZenviSignInResult(true, \"nilay@example.com\");"]
    assert win._zenvi_sign_in_dialog is None, "ready for another sign-in later"


def test_closing_the_sign_in_without_finishing_tells_the_page_it_did_not_finish(monkeypatch):
    win, main, js = make_window(monkeypatch)
    sign_in(win)
    FakeLogin.instances[0].auth_cancelled.emit()
    assert js == ["if(window.onZenviSignInResult) onZenviSignInResult(false, \"\");"] and main.calls == []


def test_the_page_is_told_once_even_if_the_dialog_reports_twice(monkeypatch):
    win, main, js = make_window(monkeypatch)
    sign_in(win)
    dlg = FakeLogin.instances[0]
    dlg.auth_completed.emit({"user_email": "a@b.c"})
    dlg.auth_cancelled.emit()          # a dialog that closes after accepting also reports a cancel
    assert len(js) == 1 and js[0].endswith('onZenviSignInResult(true, "a@b.c");')


def test_a_second_click_while_the_browser_is_open_does_not_open_another(monkeypatch):
    win, main, js = make_window(monkeypatch)
    sign_in(win)
    sign_in(win)
    assert len(FakeLogin.instances) == 1


def test_the_bridge_exposes_the_sign_in_to_the_page():
    called = []
    bridge = SimpleNamespace(window=SimpleNamespace(_sign_in_zenvi=lambda: called.append(1)))
    fn = getattr(ai_chat_ui.ChatBridge.signInZenvi, "__wrapped__", None) or ai_chat_ui.ChatBridge.signInZenvi
    fn(bridge)
    assert called == [1]


def test_the_login_dialog_can_wait_longer_than_the_first_launch_default():
    from windows import login_window
    seen = {}
    auth = SimpleNamespace(poll_for_session=lambda **kw: seen.update(kw))
    worker = login_window._PollWorker.__new__(login_window._PollWorker)
    worker._auth, worker._state, worker._timeout = auth, "st", 300
    worker.succeeded, worker.timed_out = FakeSignal(), FakeSignal()
    login_window._PollWorker.start(worker)
    assert seen["timeout"] == 300 and seen["state"] == "st"
    seen.clear()
    worker._timeout = None
    login_window._PollWorker.start(worker)
    assert "timeout" not in seen, "the default wait is left to AuthManager"


# ============================ the page ============================
def js():
    return (CHAT_UI / "chat.js").read_text(encoding="utf-8")


def test_a_failed_tool_with_a_login_error_gets_a_sign_in_button():
    text = js()
    regex = re.search(r"var AUTH_FAILURE_RE = /(.+?)/i;", text).group(1)
    pattern = re.compile(regex, re.I)
    for message in ("Error: Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again.",
                    "Error: Unauthorized: Zenvi did not accept your login", "Not authenticated", "Sign in to Zenvi to describe music"):
        assert pattern.search(message), message
    for message in ("Error: clip not found", "Error: render took 4012 ms", "Error: file not found"):
        assert not pattern.search(message), message
    body = text[text.index("window.completeToolBlock = function"):]
    body = body[:body.index("window.replayToolBlock")]
    assert "!ok && AUTH_FAILURE_RE.test(" in body and "addSignInPrompt(block)" in body


def test_the_button_opens_the_sign_in_and_after_it_offers_continue():
    text = js()
    assert "bridge.signInZenvi()" in text and "window.onZenviSignInResult = function (ok, email)" in text
    result = text[text.index("window.onZenviSignInResult"):]
    result = result[:result.index("window.completeToolBlock")]
    assert "'Continue'" in result and "'Signed in'" in result and "Sign-in was not finished." in result
    assert "I have signed in. Please continue where you left off." in text


def test_the_sign_in_row_is_styled_and_a_finished_chat_replay_does_not_grow_buttons():
    css = (CHAT_UI / "chat.css").read_text(encoding="utf-8")
    for selector in (".chat-signin-row", ".chat-signin-btn", ".chat-signin-status.ok", ".chat-signin-status.error"):
        assert selector in css
    replay = js()[js().index("window.replayToolBlock"):]
    replay = replay[:replay.index("/* ── Processing state")]
    assert "completeToolBlock(data.call_id, !!ok, '')" in replay, "history replays pass no result text, so no button appears"
