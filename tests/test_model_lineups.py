"""The model pickers follow provider releases without a desktop update.

Covers the desktop half of that: the API client reading the backend's
``/models`` and ``/models/cli`` payloads, and the chat window turning them into
picker entries and installing them, all without a Qt window or a network.
The backend half (discovery, promotion, the routes) is tested in zenvi-backend.
"""

import json
import sys
from unittest.mock import MagicMock

import pytest

from _qt_support import skip_without_pyqt5  # noqa: E402

skip_without_pyqt5()

from classes.api_client import ZenviBackendClient  # noqa: E402


# ── API client ────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)

    def json(self):
        return self._body


def _client(routes):
    """A client whose session answers ``routes`` ({url_suffix: body|status})."""
    c = ZenviBackendClient.__new__(ZenviBackendClient)
    c.api_url = "http://backend/api/v1"
    session = MagicMock()

    def get(url, timeout=0):
        for suffix, answer in routes.items():
            if url.endswith(suffix):
                if isinstance(answer, int):
                    return _Resp({}, answer)
                return _Resp(answer)
        return _Resp({}, 404)

    session.get = get
    c._session = session
    return c


CATALOG = {
    "models": [
        {"model_id": "anthropic/claude-opus-5-5", "display_name": "Claude Opus 5.5",
         "provider": "anthropic", "featured": True, "rank": 9, "tags": ["New"], "available": True},
        {"model_id": "anthropic/claude-opus-5", "display_name": "Claude Opus 5",
         "provider": "anthropic", "featured": True, "rank": 10, "tags": ["High"], "available": True},
    ],
    "default_model_id": "anthropic/claude-opus-5",
}
CLI = {
    "claude_code": [{"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "featured": True,
                     "rank": 9, "tags": ["New"], "provider": "anthropic", "default": False}],
    "codex": [{"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex", "featured": True,
               "rank": 10, "tags": [], "provider": "openai", "default": True}],
}


def test_fetch_model_catalog_returns_models_and_default_in_one_call():
    c = _client({"/models": CATALOG})
    assert c.fetch_model_catalog() == CATALOG


def test_fetch_model_catalog_is_empty_on_failure():
    assert _client({"/models": 503}).fetch_model_catalog() == {}
    assert _client({"/models": ["not", "a", "dict"]}).fetch_model_catalog() == {}


def test_list_cli_models_returns_per_backend_lists():
    c = _client({"/models/cli": CLI})
    assert c.list_cli_models() == CLI


def test_list_cli_models_is_empty_for_an_older_backend_or_bad_payload():
    """A backend without the route (404) must leave the built-in list in charge."""
    assert _client({}).list_cli_models() == {}
    assert _client({"/models/cli": {"claude_code": "nope", "codex": [1]}}).list_cli_models() == {"codex": [1]}


# ── Chat window: catalog -> picker rows, and installing a fetch ───────────

@pytest.fixture(scope="module")
def window_cls():
    for mod in ("PyQt5.QtWebEngineWidgets", "PyQt5.QtWebKitWidgets", "PyQt5.QtWebKit"):
        sys.modules.setdefault(mod, MagicMock())
    from windows.ai_chat_ui import AIChatWindow
    return AIChatWindow


def test_zenvi_rows_carry_the_picker_metadata_and_mark_the_default(window_cls):
    rows = window_cls._zenvi_rows_from_catalog(CATALOG)
    assert [r["id"] for r in rows] == ["anthropic/claude-opus-5-5", "anthropic/claude-opus-5"]
    assert rows[0] == {
        "id": "anthropic/claude-opus-5-5", "name": "Claude Opus 5.5", "default": False,
        "provider": "anthropic", "featured": True, "rank": 9, "tags": ["New"], "available": True,
    }
    assert [r["id"] for r in rows if r["default"]] == ["anthropic/claude-opus-5"]


def test_zenvi_rows_tolerate_an_older_backend_and_junk(window_cls):
    rows = window_cls._zenvi_rows_from_catalog({"models": [
        {"model_id": "openai/gpt-4o", "display_name": "GPT-4o"},   # no picker metadata
        {"display_name": "no id"}, "junk", {"model_id": ""},
    ]})
    assert len(rows) == 1
    assert rows[0]["featured"] is True and rows[0]["rank"] == 500 and rows[0]["tags"] == []
    assert window_cls._zenvi_rows_from_catalog({}) == []
    assert window_cls._zenvi_rows_from_catalog(None) == []


def _fake_window(window_cls):
    """An object with only the lineup methods of AIChatWindow bound to it.

    Building the real dock needs the main window and a web view; the lineup
    code needs neither, so borrow just those methods.
    """
    names = ("_zenvi_rows_from_catalog", "_zenvi_models", "_apply_model_lineups",
             "_on_model_lineups")
    fake_cls = type("FakeChatWindow", (), {n: window_cls.__dict__[n] for n in names})
    return fake_cls()


@pytest.fixture
def clear_live_lineups(window_cls):
    from windows.agent_runners import set_live_lineups
    set_live_lineups({})
    yield
    set_live_lineups({})


def test_apply_installs_both_lineups_and_reports_a_change(window_cls, clear_live_lineups):
    from windows.agent_runners import BACKEND_CLAUDE, BACKEND_CODEX, models_for_backend

    win = _fake_window(window_cls)
    assert window_cls._apply_model_lineups(win, {"zenvi": CATALOG, "cli": CLI}) is True
    assert [r["id"] for r in window_cls._zenvi_models(win)] == \
        ["anthropic/claude-opus-5-5", "anthropic/claude-opus-5"]
    assert models_for_backend(BACKEND_CLAUDE)[0]["id"] == "claude-opus-5-5"
    assert models_for_backend(BACKEND_CODEX)[0]["id"] == "gpt-5.3-codex"


def test_a_failed_fetch_keeps_the_previous_lineups(window_cls, clear_live_lineups):
    """A backend blip must not blank the picker mid-session."""
    from windows.agent_runners import BACKEND_CLAUDE, models_for_backend

    win = _fake_window(window_cls)
    window_cls._apply_model_lineups(win, {"zenvi": CATALOG, "cli": CLI})
    assert window_cls._apply_model_lineups(win, {"zenvi": {}, "cli": {}}) is False
    assert len(window_cls._zenvi_models(win)) == 2
    assert models_for_backend(BACKEND_CLAUDE)[0]["id"] == "claude-opus-5-5"


def test_zenvi_models_never_touches_the_network_and_copies(window_cls):
    """It runs on the GUI thread on every tab switch: cache only, by copy."""
    win = _fake_window(window_cls)
    assert window_cls._zenvi_models(win) == []
    win._zenvi_model_rows = [{"id": "x", "name": "X"}]
    rows = window_cls._zenvi_models(win)
    rows[0]["name"] = "mutated"
    assert win._zenvi_model_rows[0]["name"] == "X"


def test_on_model_lineups_slot_parses_json_and_repushes(window_cls, clear_live_lineups):
    pushed = []
    win = _fake_window(window_cls)
    win._push_models_for_backend = lambda backend=None: pushed.append(backend)
    win._model_lineup_fetching = True
    window_cls._on_model_lineups(win, json.dumps({"zenvi": CATALOG, "cli": CLI}))
    assert win._model_lineup_fetching is False
    assert pushed == [None]
    # unusable payloads neither crash nor repaint
    window_cls._on_model_lineups(win, "not json")
    window_cls._on_model_lineups(win, json.dumps({"zenvi": {}, "cli": {}}))
    assert pushed == [None]
