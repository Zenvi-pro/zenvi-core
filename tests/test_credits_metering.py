"""Each paid operation is charged exactly once (issue #180).

The backend deducts for AI video and morph generation itself
(zenvi-backend api/routes/generation.py), so the desktop must not charge those
keys a second time. Indexing and Pexels / Freesound downloads never pass
through a billed backend route, so the desktop's charge is the only one: drop
it and the operation is free.
"""

import logging
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

from classes import api_client
from classes import credits_client as cc
from classes import tool_handlers as th


# ── the metering contract ──────────────────────────────────────────────────


@pytest.fixture
def fired(monkeypatch):
    """RPCs the billing client would send, instead of sending them."""
    calls = []
    monkeypatch.setattr(
        cc.credits, "_fire", lambda function_name, payload: calls.append((function_name, dict(payload)))
    )
    return calls


def test_only_generation_is_left_to_the_backend():
    """Changing this set needs a backend route that charges the new key."""
    assert cc.BACKEND_METERED_OPS == {"video_generation", "morph_generation"}


@pytest.mark.parametrize("operation", ["video_generation", "morph_generation"])
def test_generation_is_never_charged_from_the_desktop(fired, operation):
    cc.credits.charge_operation(operation, provider="runware")
    cc.charge_operation_on_success(True, operation, provider="runware")
    assert fired == []


def test_indexing_is_charged_by_the_desktop(fired):
    cc.charge_operation_on_success(
        True, "indexing_per_minute", provider="gemini", note="import f1", duration_seconds=6.0,
    )
    assert fired == [(
        "charge_operation",
        {
            "p_operation": "indexing_per_minute",
            "p_units": 1,
            "p_provider": "gemini",
            "p_session_id": None,
            "p_note": "import f1",
            "p_idempotency_key": None,
            "p_duration_seconds": 6.0,
        },
    )]


def test_a_stock_download_is_charged_by_the_desktop(fired):
    cc.charge_operation_on_success(True, "stock_add", "stock_add", provider="pexels", note="video 7")
    assert [(fn, p["p_operation"], p["p_provider"]) for fn, p in fired] == [
        ("charge_operation", "stock_add", "pexels"),
    ]


def test_a_failed_operation_is_not_charged(fired):
    cc.charge_operation_on_success(False, "indexing_per_minute", duration_seconds=6.0)
    assert fired == []


# ── a refused charge is visible ────────────────────────────────────────────


@pytest.mark.parametrize("status", ["tier_limit", "insufficient", "standard_mode"])
def test_a_charge_the_server_refused_is_logged(caplog, status):
    """The live charge_operation RPC answers with a bare status string."""
    with caplog.at_level(logging.WARNING, logger="classes.credits_client"):
        cc.credits._log_charge_result("indexing_per_minute", status)
    assert status in caplog.text


def test_a_charge_that_went_through_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="classes.credits_client"):
        cc.credits._log_charge_result("indexing_per_minute", "ok")
    assert caplog.text == ""


def test_the_refusal_is_logged_on_the_real_charge_path(monkeypatch, caplog):
    monkeypatch.setattr(cc.credits, "_rpc", lambda function_name, payload, timeout=8: "standard_mode")
    monkeypatch.setattr(cc.credits, "balance", lambda: (True, 0))  # the refetch after the charge
    with caplog.at_level(logging.WARNING, logger="classes.credits_client"):
        cc.credits.charge_operation("indexing_per_minute", duration_seconds=6.0)
        for t in threading.enumerate():
            if t.name == "billing-charge_operation":
                t.join(timeout=5)
    assert "indexing_per_minute returned standard_mode" in caplog.text


# ── the call sites ─────────────────────────────────────────────────────────


@pytest.fixture
def charges(monkeypatch):
    """Preflight always passes; record every desktop charge."""
    calls = []
    monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (True, 1000, None))
    monkeypatch.setattr(cc, "charge_operation_on_success", lambda *a, **k: calls.append((a, k)))
    return calls


class _StockClient:
    def __init__(self, result):
        self.result = result
        self.downloads = 0

    def pexels_download(self, video_id, link, filename=""):
        self.downloads += 1
        return self.result

    def freesound_download(self, sound_id, preview_url, filename=""):
        self.downloads += 1
        return self.result


def _download(source):
    if source == "pexels":
        return th.download_pexels_video_tool(video_id="7", link="https://cdn.example/7.mp4")
    return th.download_freesound_music_tool(sound_id="9", preview_url="https://cdn.example/9-hq.mp3")


@pytest.mark.parametrize("source", ["pexels", "freesound"])
def test_a_stock_download_is_charged_once(monkeypatch, charges, source):
    client = _StockClient({"local_path": "/tmp/stock.mp4"})
    monkeypatch.setattr(api_client, "get_backend_client", lambda: client)

    assert _download(source) == "Downloaded to: /tmp/stock.mp4"
    assert [(a[1], k.get("provider")) for a, k in charges] == [("stock_add", source)]


@pytest.mark.parametrize("source", ["pexels", "freesound"])
def test_a_failed_stock_download_is_not_charged(monkeypatch, charges, source):
    client = _StockClient({"local_path": "", "error": "HTTP 404"})
    monkeypatch.setattr(api_client, "get_backend_client", lambda: client)

    out = _download(source)
    assert out.startswith("Error") and "HTTP 404" in out  # failures read as failures to the agent
    assert charges == []


@pytest.mark.parametrize("source", ["pexels", "freesound"])
def test_a_blocked_stock_download_neither_downloads_nor_charges(monkeypatch, charges, source):
    monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (False, 0, "Insufficient credits"))
    client = _StockClient({"local_path": "/tmp/stock.mp4"})
    monkeypatch.setattr(api_client, "get_backend_client", lambda: client)

    assert _download(source) == "Error: Insufficient credits"
    assert client.downloads == 0
    assert charges == []


def _reindex(monkeypatch, *, result, ai_metadata=None):
    fake_file = types.SimpleNamespace(
        data={"path": "/media/clip.mp4", "duration": 6.0, "ai_metadata": ai_metadata or {}},
        save=lambda: None,
    )
    query = types.ModuleType("classes.query")
    query.File = types.SimpleNamespace(get=lambda **kw: fake_file)
    monkeypatch.setitem(sys.modules, "classes.query", query)
    monkeypatch.setattr(th, "_get_app", lambda: types.SimpleNamespace(project={"id": "p1"}))
    # No Qt event loop runs here, stubbed or real: run the main-thread hops inline.
    monkeypatch.setattr(th, "_run_on_main_thread", lambda fn, *a, timeout=None: fn(*a))
    client = MagicMock()
    client.is_indexing_configured.return_value = True
    client.reindex_video.return_value = result
    monkeypatch.setattr(api_client, "get_backend_client", lambda: client)
    return th.reindex_project_file(file_id="f1"), client


def test_a_reindex_is_charged_once_for_the_clip_duration(monkeypatch, charges):
    out, _ = _reindex(monkeypatch, result={"success": True, "index_id": "zenvi-p1", "video_id": ""})

    assert out.startswith("Re-indexing complete"), out
    assert [(a[1], k.get("duration_seconds")) for a, k in charges] == [("indexing_per_minute", 6.0)]


def test_a_failed_reindex_is_not_charged(monkeypatch, charges):
    out, _ = _reindex(monkeypatch, result={"success": False, "error": "boom"})

    assert out == "Error: Re-indexing failed: boom"
    assert charges == []


def test_an_indexed_clip_is_not_reindexed_or_charged(monkeypatch, charges):
    ready = {"twelvelabs": {"status": "ready", "index_id": "zenvi-p1", "video_id": "v1"}}
    out, client = _reindex(monkeypatch, result={"success": True}, ai_metadata=ready)

    assert "already indexed" in out
    client.reindex_video.assert_not_called()
    assert charges == []


# ── a backend-billed generation repaints the badge ─────────────────────────


def _backend(monkeypatch, response):
    """A ZenviBackendClient whose POST answers *response* (or raises it)."""
    client = api_client.ZenviBackendClient.__new__(api_client.ZenviBackendClient)
    client.api_url = "http://backend.test/api/v1"
    session = MagicMock()
    if isinstance(response, Exception):
        session.post.side_effect = response
    else:
        session.post.return_value.json.return_value = response
    client._session = session
    monkeypatch.setattr(client, "_apply_bearer", lambda s: None)
    return client


def _generate(client, route):
    if route == "video":
        return client.generate_video("a paper plane", duration_seconds=5)
    return client.generate_morph_video("https://x/a.jpg", "https://x/b.jpg")


@pytest.mark.parametrize("route", ["video", "morph"])
@pytest.mark.parametrize(
    "response",
    [
        {"video_url": "https://x/gen.mp4", "local_path": "/tmp/gen.mp4"},  # charged
        {"video_url": "https://x/gen.mp4", "error": "download failed"},  # refunded
        RuntimeError("402 Payment Required"),  # never charged
    ],
    ids=["charged", "refunded", "refused"],
)
def test_a_generation_request_repaints_the_badge_whatever_its_outcome(monkeypatch, route, response):
    """The desktop fires no charge for these, so it must refetch the balance itself."""
    refreshes = []
    monkeypatch.setattr(cc.credits, "refresh_balance", lambda: refreshes.append(route))

    _generate(_backend(monkeypatch, response), route)

    assert refreshes == [route]


def test_a_balance_refresh_fetches_off_the_calling_thread_and_repaints(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CREDITS_FILE", str(tmp_path / "zenvi_credits.json"))
    monkeypatch.setattr(cc, "_current_user_id", lambda: "user-a")
    client = cc.CreditsClient()
    monkeypatch.setattr(client, "_get_auth", lambda: (object(), {}, "u"))
    fetched_on = []

    def rpc(function_name, payload, timeout=8):
        fetched_on.append(threading.current_thread())
        return {"total_points": 4130}

    monkeypatch.setattr(client, "_rpc", rpc)
    heard = threading.Event()
    client.add_listener(lambda balance: balance == 4130 and heard.set())

    client.refresh_balance()

    assert heard.wait(5)
    assert threading.current_thread() not in fetched_on
    # The listener fires before the balance is persisted: let that write land
    # in tmp_path before monkeypatch restores the real CREDITS_FILE.
    for thread in threading.enumerate():
        if thread.name == "billing-refresh":
            thread.join(5)
    assert (tmp_path / "zenvi_credits.json").exists()


# ── the chat turn ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def chat_ui():
    from windows import ai_chat_ui
    return ai_chat_ui


def test_a_payload_that_already_carries_a_snapshot_is_sent_as_is(chat_ui, monkeypatch):
    built = []
    monkeypatch.setattr(
        th, "build_editor_snapshot_for_chat",
        lambda: built.append(1) or "[Editor snapshot]\nnew\n[/Editor snapshot]\n",
    )
    text = "[Editor snapshot]\nold\n[/Editor snapshot]\ntrim the intro"

    assert chat_ui.AIChatWindow._prepend_editor_snapshot(None, text) == text
    assert built == []


def test_a_plain_message_gets_exactly_one_snapshot(chat_ui, monkeypatch):
    monkeypatch.setattr(
        th, "build_editor_snapshot_for_chat", lambda: "[Editor snapshot]\nS\n[/Editor snapshot]\n",
    )
    out = chat_ui.AIChatWindow._prepend_editor_snapshot(None, "trim the intro")

    assert out.count("[Editor snapshot]") == 1
    assert out.endswith("trim the intro")


def test_the_balance_is_fetched_off_the_calling_thread(chat_ui, monkeypatch):
    """The GUI thread calls this after every turn; the RPC must not run on it."""
    seen = {}
    done = threading.Event()

    def fake_balance():
        seen["thread"] = threading.current_thread()
        done.set()
        return True, 4242

    monkeypatch.setattr(cc.credits, "balance", fake_balance)
    chat_ui.AIChatWindow._fetch_credits_balance(MagicMock())

    assert done.wait(5)
    assert seen["thread"] is not threading.current_thread()


def _chat_window(chat_ui, *, active_sid, reply_sid):
    W = chat_ui.AIChatWindow

    class Win:
        _use_web_ui = True
        _user_cancelled = False
        _on_response_ready = W._on_response_ready
        _final_segment_text = W._final_segment_text
        _reset_turn_segments = staticmethod(W._reset_turn_segments)
        _strip_thinking = staticmethod(W._strip_thinking)

        def __init__(self):
            self._active_sid = active_sid
            self._sessions = {"s1": {"messages": []}, "s2": {"messages": []}}
            self._token_buffer = []
            self._token_flush_scheduled = False
            self.refreshes = 0
            self.shown = []

        def sender(self):
            return types.SimpleNamespace(_session_id=reply_sid)

        def _flush_token_buffer(self):
            pass

        def _run_js(self, code):
            pass

        def _add_assistant_msg(self, body):
            self.shown.append(body)

        def _record_message(self, sid, role, text):
            pass

        def _set_processing_ui(self, on):
            pass

        def _push_tabs_to_js(self):
            pass

        def _rebuild_widget_tabs(self):
            pass

        def _fetch_credits_balance(self):
            self.refreshes += 1

    return Win()


def test_the_badge_refreshes_once_after_a_reply(chat_ui):
    win = _chat_window(chat_ui, active_sid="s1", reply_sid="s1")
    win._on_response_ready("Trimmed the intro.")

    assert win.shown == ["Trimmed the intro."]
    assert win.refreshes == 1


def test_the_badge_refreshes_after_a_background_tab_reply(chat_ui):
    win = _chat_window(chat_ui, active_sid="s1", reply_sid="s2")
    win._on_response_ready("Done in the other tab.")

    assert win._sessions["s2"]["messages"]
    assert win.refreshes == 1
