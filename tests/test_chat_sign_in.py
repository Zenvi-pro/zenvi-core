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


class FakeAuth:
    instance_ = None

    def __init__(self, fail_start=False):
        self.fail_start, self.started, self.poll_kwargs = fail_start, 0, None

    @classmethod
    def instance(cls):
        return cls.instance_

    def start_auth_flow(self):
        if self.fail_start:
            raise OSError("no browser")
        self.started += 1
        return "https://zenvi.pro/login?state=abc", "abc"

    def poll_for_session(self, state, on_success=None, on_timeout=None, **kw):
        self.poll_kwargs = {"state": state, "on_success": on_success, "on_timeout": on_timeout, **kw}


class FakeMain:
    def __init__(self):
        self.calls = []

    def raise_(self):
        self.calls.append("raise")

    def activateWindow(self):
        self.calls.append("activate")


def make_window(monkeypatch, auth=None, use_web=True):
    FakeAuth.instance_ = auth or FakeAuth()
    monkeypatch.setattr("classes.auth_manager.AuthManager", FakeAuth)
    js, main = [], FakeMain()
    win = SimpleNamespace(window=lambda: main, _use_web_ui=use_web, _run_js=js.append, refresh_credits_for_account=lambda: main.calls.append("credits"),
                          ZENVI_SIGN_IN_WAIT_SECONDS=ai_chat_ui.AIChatWindow.ZENVI_SIGN_IN_WAIT_SECONDS, _zenvi_sign_in_waiting=False)
    queued = []

    class FakeMeta:
        @staticmethod
        def invokeMethod(obj, name, conn, *args):
            queued.append((name, [a for a in args]))
            getattr(ai_chat_ui.AIChatWindow, name)(win, *[getattr(a, "value", a) for a in args])

    monkeypatch.setattr(ai_chat_ui, "QMetaObject", FakeMeta)
    monkeypatch.setattr(ai_chat_ui, "Q_ARG", lambda t, v: SimpleNamespace(value=v))
    return win, main, js, queued, FakeAuth.instance_


def sign_in(win):
    ai_chat_ui.AIChatWindow._sign_in_zenvi(win)


# ============================ the Python side ============================
def test_the_sign_in_opens_the_website_flow_and_waits_five_minutes_on_a_plain_thread(monkeypatch):
    win, main, js, queued, auth = make_window(monkeypatch)
    sign_in(win)
    assert auth.started == 1 and auth.poll_kwargs["state"] == "abc" and auth.poll_kwargs["timeout"] == 300 and js == [] and win._zenvi_sign_in_waiting is True


def test_on_success_the_app_comes_to_the_front_credits_refresh_and_the_page_is_told(monkeypatch):
    win, main, js, queued, auth = make_window(monkeypatch)
    sign_in(win)
    auth.poll_kwargs["on_success"]({"user_email": "nilay@example.com", "access_token": "x"})
    assert main.calls == ["raise", "activate", "credits"]
    assert js == ['if(window.onZenviSignInResult) onZenviSignInResult(true, "nilay@example.com");']
    assert win._zenvi_sign_in_waiting is False, "ready for another sign-in later"
    assert [q[0] for q in queued] == ["_on_zenvi_sign_in_done"], "the GUI is reached through one queued call, not from the waiting thread"


def test_a_session_without_an_email_still_counts_as_signed_in(monkeypatch):
    win, main, js, queued, auth = make_window(monkeypatch)
    sign_in(win)
    auth.poll_kwargs["on_success"]({"access_token": "x"})
    assert js == ['if(window.onZenviSignInResult) onZenviSignInResult(true, "");']


def test_a_timeout_tells_the_page_it_did_not_finish_and_leaves_the_app_alone(monkeypatch):
    win, main, js, queued, auth = make_window(monkeypatch)
    sign_in(win)
    auth.poll_kwargs["on_timeout"]()
    assert js == ['if(window.onZenviSignInResult) onZenviSignInResult(false, "");'] and main.calls == [] and win._zenvi_sign_in_waiting is False


def test_a_second_click_while_the_browser_is_open_does_not_start_another(monkeypatch):
    win, main, js, queued, auth = make_window(monkeypatch)
    sign_in(win)
    sign_in(win)
    assert auth.started == 1


def test_a_browser_that_cannot_be_opened_is_reported_and_can_be_tried_again(monkeypatch):
    win, main, js, queued, auth = make_window(monkeypatch, auth=FakeAuth(fail_start=True))
    sign_in(win)
    assert js == ['if(window.onZenviSignInResult) onZenviSignInResult(false, "");'] and win._zenvi_sign_in_waiting is False


def test_the_chat_sign_in_uses_no_dialog_and_no_qt_thread():
    import ast
    import inspect
    import textwrap
    code = ""
    for fn in (ai_chat_ui.AIChatWindow._sign_in_zenvi, ai_chat_ui.AIChatWindow._on_zenvi_sign_in_done):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        node = tree.body[0]
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
            node.body = node.body[1:]                       # the docstring explains the crash and so names what is banned
        code += ast.unparse(tree)
    for banned in ("LoginWindow", "QThread", "exec_", ".open("):
        assert banned not in code, banned


def test_the_bridge_exposes_the_sign_in_to_the_page():
    called = []
    bridge = SimpleNamespace(window=SimpleNamespace(_sign_in_zenvi=lambda: called.append(1)))
    fn = getattr(ai_chat_ui.ChatBridge.signInZenvi, "__wrapped__", None) or ai_chat_ui.ChatBridge.signInZenvi
    fn(bridge)
    assert called == [1]


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
