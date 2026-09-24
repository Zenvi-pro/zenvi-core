"""Credits badge paint path (#50).

The badge must show the last known balance the moment the chat page is ready,
converge to the live value in the background, and repaint right after a
paid operation instead of waiting for the 60s refresh.
"""

import importlib.util
import json
import sys
import types

import pytest

from classes import credits_client as cc


_USER = {"id": "user-a"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "zenvi_credits.json"
    monkeypatch.setattr(cc, "CREDITS_FILE", str(path))
    monkeypatch.setitem(_USER, "id", "user-a")
    monkeypatch.setattr(cc, "_current_user_id", lambda: _USER["id"])
    return path


def _client(monkeypatch, rpc_result, authed=True):
    client = cc.CreditsClient()
    monkeypatch.setattr(
        client, "_get_auth", lambda: (object(), {}, "u") if authed else (None, None, None)
    )
    monkeypatch.setattr(client, "_rpc", lambda *a, **k: rpc_result)
    return client


def test_a_stored_balance_is_served_before_any_fetch(store, monkeypatch):
    store.write_text(json.dumps({"user_id": "user-a", "balance": 420}))
    client = _client(monkeypatch, None)
    assert client.cached_balance() == 420


def test_a_stored_balance_from_another_account_is_ignored(store, monkeypatch):
    store.write_text(json.dumps({"user_id": "someone-else", "balance": 420}))
    client = _client(monkeypatch, None)
    assert client.cached_balance() is None


def test_a_successful_fetch_is_persisted_for_the_next_launch(store, monkeypatch):
    _client(monkeypatch, {"total_points": 77}).balance()
    fresh = _client(monkeypatch, None)
    assert fresh.cached_balance() == 77


def test_a_failed_fetch_keeps_serving_the_stored_balance(store, monkeypatch):
    store.write_text(json.dumps({"user_id": "user-a", "balance": 420}))
    assert _client(monkeypatch, None).balance() == (True, 420)


def test_switching_account_drops_the_previous_accounts_balance(store, monkeypatch):
    client = _client(monkeypatch, {"total_points": 500})
    client.balance()
    _USER["id"] = "user-b"
    assert client.cached_balance() is None


def test_a_fetch_that_finishes_after_an_account_switch_is_discarded(store, monkeypatch):
    client = cc.CreditsClient()
    monkeypatch.setattr(client, "_get_auth", lambda: (object(), {}, "u"))

    def rpc(*a, **k):
        _USER["id"] = "user-b"          # user B signs in while A's fetch is in flight
        return {"total_points": 500}

    monkeypatch.setattr(client, "_rpc", rpc)
    heard = []
    client.add_listener(heard.append)
    client.balance()
    assert heard == []
    assert client.cached_balance() is None
    assert not store.exists()


def test_an_account_switch_after_the_rpc_returns_is_still_caught(store, monkeypatch):
    """The switch can land while the result is being committed, not only mid-RPC."""
    client = _client(monkeypatch, {"total_points": 500})

    def switch_during_seed(user_id):
        _USER["id"] = "user-b"
        return None

    monkeypatch.setattr(client, "_read_stored", switch_during_seed)
    heard = []
    client.add_listener(heard.append)
    client.balance()
    assert heard == []
    assert not store.exists()


def test_a_balance_without_a_signed_in_user_is_not_persisted(store, monkeypatch):
    store.write_text(json.dumps({"user_id": "user-a", "balance": 420}))
    _USER["id"] = None
    _client(monkeypatch, {"total_points": 1}).balance()
    assert json.loads(store.read_text())["balance"] == 420


def test_listeners_hear_a_changed_balance(store, monkeypatch):
    client = _client(monkeypatch, {"total_points": 12})
    heard = []
    client.add_listener(heard.append)
    client.balance()
    client.balance()  # unchanged -> no second repaint
    assert heard == [12]


def test_a_check_operation_response_repaints_the_badge(store, monkeypatch):
    client = _client(monkeypatch, {"allowed": True, "balance": 55, "required": 5})
    monkeypatch.setattr(cc, "credits", client)
    heard = []
    client.add_listener(heard.append)
    allowed, balance, err = cc.check_operation("video_gen", "Video")
    assert (allowed, balance, err) == (True, 55, None)
    assert heard == [55]
    assert client.cached_balance() == 55


def test_a_charge_refetches_the_balance_right_after_the_rpc(store, monkeypatch):
    client = cc.CreditsClient()
    monkeypatch.setattr(client, "_get_auth", lambda: (object(), {}, "u"))
    calls = []

    def rpc(name, payload, timeout=8):
        calls.append(name)
        return {"total_points": 90} if name == "get_credits_balance" else {"ok": True}

    monkeypatch.setattr(client, "_rpc", rpc)

    class _SyncThread:
        def __init__(self, target, args=(), **kw):
            self._run = lambda: target(*args)

        def start(self):
            self._run()

    monkeypatch.setattr(cc.threading, "Thread", _SyncThread)
    heard = []
    client.add_listener(heard.append)
    client.charge_operation("video_gen")
    assert calls == ["charge_operation", "get_credits_balance"]
    assert heard == [90]


# ── Chat UI side ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def chat_ui():
    """ai_chat_ui loaded with real (empty) Qt widget base classes.

    The shared stub makes ``PyQt5.QtWidgets`` a MagicMock, which turns
    ``class AIChatWindow(QDockWidget)`` into a mock; load a private copy of
    the module against a widgets stub whose ``Q*`` names are real classes.
    """
    widgets = sys.modules["conftest"]._StubQtModule("PyQt5.QtWidgets")
    real = sys.modules["PyQt5.QtWidgets"]
    sys.modules["PyQt5.QtWidgets"] = widgets
    try:
        import windows
        path = windows.__path__[0] + "/ai_chat_ui.py"
        spec = importlib.util.spec_from_file_location("_ai_chat_ui_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.modules["PyQt5.QtWidgets"] = real
    return mod


def _fake_window(chat_ui, order):
    """Just enough of AIChatWindow for _inject_web_ready to run headless."""
    w = types.SimpleNamespace(_use_web_ui=True, _chat_web_initial_sync_done=False)
    w._run_js = lambda code, callback=None: order.append(code)
    w._push_models_for_backend = lambda: order.append("MODELS")
    w._get_preamble_html = lambda: ""
    w._push_tabs_to_js = lambda: None
    w._start_restore_chat_histories_async = lambda: None
    w._push_attachments_to_js = lambda: None
    w._on_credits_balance = lambda b: chat_ui.AIChatWindow._on_credits_balance(w, b)
    w._paint_cached_credits = lambda: chat_ui.AIChatWindow._paint_cached_credits(w)
    return w


def _credit_calls(order):
    return [c for c in order if "updateCreditsBalance" in c]


def test_ready_paints_the_known_balance_before_models_load(chat_ui, store, monkeypatch):
    """A slow /models must not hold the badge back."""
    client = _client(monkeypatch, None)
    store.write_text(json.dumps({"user_id": "user-a", "balance": 420}))
    monkeypatch.setattr(cc, "credits", client)
    order = []
    chat_ui.AIChatWindow._inject_web_ready(_fake_window(chat_ui, order))
    first_credit = next(i for i, c in enumerate(order) if "updateCreditsBalance" in c)
    assert first_credit < order.index("MODELS")
    assert "updateCreditsBalance(420)" in order[first_credit]
    assert all("(-1)" not in c for c in _credit_calls(order))


def test_a_fetch_that_beats_page_ready_is_painted_on_ready(chat_ui, store, monkeypatch):
    """The early push is a no-op in JS; ready must repaint the fetched value."""
    client = _client(monkeypatch, {"total_points": 31})
    monkeypatch.setattr(cc, "credits", client)
    order = []
    w = _fake_window(chat_ui, order)
    client.balance()                              # fetch lands before loadFinished
    chat_ui.AIChatWindow._inject_web_ready(w)
    assert "updateCreditsBalance(31)" in _credit_calls(order)[0]


def test_signing_in_again_repaints_for_the_new_account_and_refetches(chat_ui, store, monkeypatch):
    """Logout keeps the dock alive, so re-login must not keep the old number."""
    client = _client(monkeypatch, {"total_points": 500})
    monkeypatch.setattr(cc, "credits", client)
    client.balance()                              # user A's balance is showing
    _USER["id"] = "user-b"
    order, fetched = [], []
    w = _fake_window(chat_ui, order)
    w._fetch_credits_balance = lambda: fetched.append(True)
    chat_ui.AIChatWindow.refresh_credits_for_account(w)
    assert _credit_calls(order) == [
        "if(window.updateCreditsBalance) updateCreditsBalance(-1);"
    ]
    assert fetched == [True]


def test_first_ever_run_shows_the_loading_placeholder(chat_ui, store, monkeypatch):
    monkeypatch.setattr(cc, "credits", _client(monkeypatch, None))
    order = []
    chat_ui.AIChatWindow._inject_web_ready(_fake_window(chat_ui, order))
    assert "updateCreditsBalance(-1)" in _credit_calls(order)[0]
