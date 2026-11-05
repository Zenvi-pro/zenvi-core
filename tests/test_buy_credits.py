"""Buying credits from the Zenvi Assistant (#272).

The tool only quotes and shows a card; the charge happens when the user presses
Pay on that card (a page click, never a model call). MCP callers get the quote
and a zenvi.pro/topup link, where they confirm in the browser.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes import credits_client as cc  # noqa: E402
from classes.agent_tools.core_tools import CORE_TOOL_NAMES  # noqa: E402
from classes.editor_tools import REGISTRY  # noqa: E402
from classes.editor_tools import ai_generation_credits as tool_mod  # noqa: E402
from windows import ai_chat_ui  # noqa: E402

CARD = {"pack": 10, "credits": 1000, "amount": 10, "currency": "usd", "mode": "card", "card": "Visa •4242"}


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def test_the_client_posts_to_the_buy_credits_function_with_the_users_session(monkeypatch):
    sent = []
    client = cc.CreditsClient()
    monkeypatch.setattr(client, "_get_auth", lambda: (object(), {"Authorization": "Bearer jwt", "apikey": "anon"}, "https://sb.example"))
    monkeypatch.setattr("requests.post", lambda url, **kw: sent.append((url, kw)) or FakeResp(200, CARD))

    assert client.buy(10) == CARD
    client.buy(15, confirm=True)

    assert sent[0][0] == "https://sb.example/functions/v1/buy-credits"
    assert sent[0][1]["headers"]["Authorization"] == "Bearer jwt"
    assert sent[0][1]["json"] == {"pack": 10}
    assert sent[1][1]["json"] == {"pack": 15, "confirm": True}


def test_the_client_reports_errors_and_a_missing_sign_in(monkeypatch):
    client = cc.CreditsClient()
    monkeypatch.setattr(client, "_get_auth", lambda: (None, None, None))
    assert "sign in" in client.buy(10)["error"].lower()

    monkeypatch.setattr(client, "_get_auth", lambda: (object(), {}, "https://sb.example"))
    monkeypatch.setattr("requests.post", lambda url, **kw: FakeResp(400, {"error": "pack must be 5, 10 or 15"}))
    assert client.buy(10)["error"] == "pack must be 5, 10 or 15"


def test_the_tool_is_found_with_find_tools_not_bound_on_every_turn():
    spec = REGISTRY["buy_credits_tool"]
    assert "buy_credits_tool" not in CORE_TOOL_NAMES
    assert spec.schema["properties"]["pack"]["enum"] == [5, 10, 15]
    assert "ai.credits" in spec.covers


def test_the_tool_quotes_and_shows_the_card_but_never_charges(monkeypatch):
    calls, shown = [], []
    monkeypatch.setattr(cc.credits, "buy", lambda pack, confirm=False: calls.append((pack, confirm)) or dict(CARD, pack=pack))
    monkeypatch.setattr(tool_mod, "_show_card", shown.append)

    out = tool_mod.buy_credits_tool(pack=10)

    assert calls == [(10, False)]
    assert shown and shown[0]["pack"] == 10
    assert not out.startswith("Error")
    for words in ("$10", "1,000 credits", "Visa •4242", "https://zenvi.pro/topup?pack=10"):
        assert words in out, words


def test_the_tool_says_why_it_could_not_quote(monkeypatch):
    monkeypatch.setattr(cc.credits, "buy", lambda pack, confirm=False: {"error": "Sign in to Zenvi first."})
    monkeypatch.setattr(tool_mod, "_show_card", lambda q: None)
    assert tool_mod.buy_credits_tool(pack=5).startswith("Error: Sign in")


def test_the_block_message_points_at_the_top_up():
    text = cc.credit_block_message(300, "video", 20)
    assert "https://zenvi.pro/topup" in text
    assert 'run_tool(name="buy_credits_tool"' in text


# ============================ the chat dock ============================
def _window(monkeypatch):
    js, opened, refreshed = [], [], []
    win = SimpleNamespace(_use_web_ui=True, _run_js=js.append, refresh_credits_for_account=lambda: refreshed.append(1))
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **k: opened.append(url))
    return win, js, opened, refreshed


def test_paying_from_the_card_refreshes_the_badge(monkeypatch):
    win, js, opened, refreshed = _window(monkeypatch)
    ai_chat_ui.AIChatWindow._on_topup_result(win, json.dumps({"status": "paid", "credits": 1000, "balance": 1200}))
    assert refreshed == [1] and not opened
    assert any("onTopUpResult" in c and '"paid"' in c for c in js)


def test_a_user_without_a_saved_card_finishes_on_stripe(monkeypatch):
    win, js, opened, _ = _window(monkeypatch)
    ai_chat_ui.AIChatWindow._on_topup_result(win, json.dumps({"status": "checkout", "url": "https://checkout.stripe.com/c/pay/x"}))
    assert opened == ["https://checkout.stripe.com/c/pay/x"]


def test_only_a_stripe_checkout_url_is_opened(monkeypatch):
    win, js, opened, _ = _window(monkeypatch)
    ai_chat_ui.AIChatWindow._on_topup_result(win, json.dumps({"status": "checkout", "url": "https://evil.example/pay"}))
    assert opened == []


def test_the_bridge_charges_only_through_the_page(monkeypatch):
    called = []
    bridge = SimpleNamespace(window=SimpleNamespace(_buy_credits=called.append))
    fn = getattr(ai_chat_ui.ChatBridge.buyCredits, "__wrapped__", None) or ai_chat_ui.ChatBridge.buyCredits
    fn(bridge, 15)
    assert called == [15]


def test_an_out_of_credits_error_offers_the_card():
    pattern = ai_chat_ui.OUT_OF_CREDITS_RE
    for message in ("Out of credits for image generation. Need 3, have 0.", "Insufficient credits (20 remaining, need 300 for video)."):
        assert pattern.search(message), message
    assert not pattern.search("Error: clip not found")


def test_links_in_the_chat_open_in_the_browser_not_in_the_dock(monkeypatch):
    from windows import chat_web_view
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    url = lambda u: SimpleNamespace(scheme=lambda: u.split(":")[0], toString=lambda: u)
    assert chat_web_view.open_web_link(url("https://zenvi.pro/topup?pack=10"), True)
    assert not chat_web_view.open_web_link(url("https://zenvi.pro/topup"), False)
    assert not chat_web_view.open_web_link(url("file:///c/x.mp4"), True)
    assert opened == ["https://zenvi.pro/topup?pack=10"]
    code = (SRC / "windows" / "chat_web_view.py").read_text(encoding="utf-8")
    assert code.count("open_web_link(url, nav_type ==") == 2


def test_the_page_has_the_top_up_card():
    text = (SRC / "chat_ui" / "chat.js").read_text(encoding="utf-8")
    assert "window.showTopUpCard" in text and "window.onTopUpResult" in text
    card = text[text.index("window.showTopUpCard"):]
    assert re.search(r"bridge\.buyCredits\(", card)
    assert "=>" not in card[: card.index("window.onTopUpResult")]  # ES5 for WebKit 602


def test_replies_cannot_inject_script_into_the_chat_that_holds_the_pay_bridge():
    render = ai_chat_ui._markdown_to_html
    for evil in ('<img src=x onerror="zenviChatBridge.buyCredits(15)">',
                 '<script>zenviChatBridge.buyCredits(15)</script>',
                 '<div onclick="x()">hi</div>',
                 '[pay](javascript:zenviChatBridge.buyCredits(15))',
                 '![x](https://invalid.invalid/x){: onerror="zenviChatBridge.buyCredits(15)"}',
                 '[x](https://zenvi.pro){: onmouseover="zenviChatBridge.buyCredits(15)"}'):
        out = render(evil)
        assert "<script" not in out, out
        assert 'href="javascript:' not in out.lower(), out
        assert not re.search(r"<[a-z][^>]*\son\w+=", out), out
    good = render("**bold** and `code <b>` and [top up](https://zenvi.pro/topup)")
    assert "<strong>bold</strong>" in good and "&lt;b&gt;" in good and 'href="https://zenvi.pro/topup"' in good


def test_the_bridge_charges_once_per_card_and_retries_reuse_the_same_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(cc.credits, "buy", lambda pack, confirm=False, attempt=None: calls.append((pack, confirm, attempt)) or {"error": "timeout"})
    monkeypatch.setattr(ai_chat_ui.threading, "Thread", lambda target, **kw: SimpleNamespace(start=target))
    monkeypatch.setattr(ai_chat_ui, "QMetaObject", SimpleNamespace(invokeMethod=lambda *a: None))
    monkeypatch.setattr(ai_chat_ui, "Q_ARG", lambda t, v: v)
    win = SimpleNamespace(_use_web_ui=True, _run_js=lambda c: None)
    for name in ("show_topup_card", "_buy_credits", "_on_topup_result"):
        setattr(win, name, getattr(ai_chat_ui.AIChatWindow, name).__get__(win))
    win.refresh_credits_for_account = lambda: None

    win._buy_credits(10)  # no card shown: a script cannot charge
    assert calls == []

    win.show_topup_card(dict(CARD))
    win._buy_credits(10)
    win._buy_credits(10)  # in flight: ignored
    assert len(calls) == 1 and calls[0][:2] == (10, True) and calls[0][2]
    win._on_topup_result(json.dumps({"error": "timeout"}))
    win._buy_credits(10)  # a retry after an unknown outcome replays the same attempt
    assert calls[1][2] == calls[0][2]
    win._on_topup_result(json.dumps({"status": "paid", "credits": 1000}))
    win._buy_credits(10)  # paid: the card is spent
    assert len(calls) == 2


def test_the_client_sends_the_attempt_id(monkeypatch):
    sent = []
    client = cc.CreditsClient()
    monkeypatch.setattr(client, "_get_auth", lambda: (object(), {}, "https://sb.example"))
    monkeypatch.setattr("requests.post", lambda url, **kw: sent.append(kw["json"]) or FakeResp(200, {"status": "paid"}))
    client.buy(10, confirm=True, attempt="a1b2c3d4e5")
    assert sent == [{"pack": 10, "confirm": True, "attempt": "a1b2c3d4e5"}]
