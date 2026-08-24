"""The zenvi_api_request MCP tool — the agent's authenticated door to the backend.

This call carries the signed-in user's credentials, so most of what matters here
is what it REFUSES to do.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from classes import agent_api_proxy as proxy  # noqa: E402


BASE = "https://api.zenvi.pro"


class FakeResponse:
    def __init__(self, status=200, text='{"ok": true}'):
        self.status_code = status
        self.text = text


@pytest.fixture
def sent(monkeypatch):
    """Capture the outgoing request instead of making one."""
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(proxy, "_backend_url", lambda: BASE)
    monkeypatch.setattr(proxy, "_access_token", lambda: "jwt-token")
    fake_requests = type("requests", (), {"request": staticmethod(fake_request)})
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    return calls


# ── Refusals ───────────────────────────────────────────────────────────────

def test_refuses_a_url_pointing_at_another_host(sent):
    """The whole point of the guard: this request carries the user's token, so
    the agent must not be able to aim it somewhere else."""
    for hostile in ("https://evil.example.com/steal",
                    "http://169.254.169.254/latest/meta-data/",
                    "//evil.example.com/steal"):
        result = proxy.zenvi_api_request("GET", hostile)
        assert result.startswith("Error:"), hostile
        assert "api.zenvi.pro" in result
    assert sent == [], "a refused request must never be sent"


def test_allows_an_absolute_url_on_the_backend_itself(sent):
    proxy.zenvi_api_request("GET", BASE + "/api/v1/models?limit=2")
    assert sent[0]["url"] == BASE + "/api/v1/models?limit=2"


def test_refuses_an_unsupported_method(sent):
    for verb in ("TRACE", "CONNECT", "nonsense"):
        assert proxy.zenvi_api_request(verb, "/api/v1/models").startswith("Error:")
    assert sent == []


def test_requires_a_path(sent):
    assert proxy.zenvi_api_request("GET", "").startswith("Error:")
    assert proxy.zenvi_api_request("GET", "   ").startswith("Error:")
    assert sent == []


def test_reports_a_signed_out_user_instead_of_calling_anonymously(monkeypatch):
    monkeypatch.setattr(proxy, "_backend_url", lambda: BASE)
    monkeypatch.setattr(proxy, "_access_token", lambda: None)
    result = proxy.zenvi_api_request("GET", "/api/v1/models")
    assert "not signed in" in result


def test_rejects_malformed_body_and_query(sent):
    assert proxy.zenvi_api_request("POST", "/x", body="not json").startswith("Error:")
    assert proxy.zenvi_api_request("GET", "/x", query="not json").startswith("Error:")
    assert proxy.zenvi_api_request("GET", "/x", query='["a"]').startswith("Error:")
    assert sent == []


# ── Successful calls ───────────────────────────────────────────────────────

def test_sends_the_users_token_and_normalises_the_path(sent):
    proxy.zenvi_api_request("get", "api/v1/models")
    call = sent[0]
    assert call["method"] == "GET"
    assert call["url"] == BASE + "/api/v1/models"       # leading slash added
    assert call["headers"]["Authorization"] == "Bearer jwt-token"
    assert call["timeout"] == proxy.REQUEST_TIMEOUT


def test_passes_body_and_query_through_as_json(sent):
    proxy.zenvi_api_request("POST", "/api/v1/thing",
                            body='{"name": "demo"}', query='{"limit": 10}')
    call = sent[0]
    assert call["json"] == {"name": "demo"}
    assert call["params"] == {"limit": 10}
    assert call["headers"]["Content-Type"] == "application/json"


def test_a_get_sends_no_body_or_content_type(sent):
    proxy.zenvi_api_request("GET", "/api/v1/models")
    assert sent[0]["json"] is None
    assert "Content-Type" not in sent[0]["headers"]


def test_result_leads_with_the_status(sent):
    result = proxy.zenvi_api_request("GET", "/api/v1/models")
    assert result.startswith("HTTP 200 GET /api/v1/models")
    assert '{"ok": true}' in result


def test_a_huge_response_is_truncated(monkeypatch):
    monkeypatch.setattr(proxy, "_backend_url", lambda: BASE)
    monkeypatch.setattr(proxy, "_access_token", lambda: "jwt-token")
    big = "x" * (proxy.MAX_RESPONSE_CHARS * 3)
    fake = type("requests", (), {
        "request": staticmethod(lambda *a, **k: FakeResponse(200, big))})
    monkeypatch.setitem(sys.modules, "requests", fake)

    result = proxy.zenvi_api_request("GET", "/api/v1/big")
    assert "truncated" in result
    assert len(result) < proxy.MAX_RESPONSE_CHARS * 2


def test_a_network_failure_does_not_leak_the_token(monkeypatch, caplog):
    monkeypatch.setattr(proxy, "_backend_url", lambda: BASE)
    monkeypatch.setattr(proxy, "_access_token", lambda: "super-secret-jwt")

    def boom(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setitem(sys.modules, "requests",
                        type("requests", (), {"request": staticmethod(boom)}))
    with caplog.at_level("DEBUG"):
        result = proxy.zenvi_api_request("GET", "/api/v1/models")

    assert result.startswith("Error:")
    assert "super-secret-jwt" not in result
    assert "super-secret-jwt" not in caplog.text


# ── MCP wiring ─────────────────────────────────────────────────────────────

def test_the_tool_is_advertised_over_mcp():
    from classes.agent_mcp_server import iter_tool_defs

    defs = {d["name"]: d for d in iter_tool_defs()}
    assert "zenvi_api_request" in defs, "the agent cannot use a tool it never sees"

    schema = defs["zenvi_api_request"]["inputSchema"]
    assert set(schema["properties"]) == {"method", "path", "body", "query"}
    assert "Zenvi backend API" in defs["zenvi_api_request"]["description"]


def test_extra_tools_do_not_leak_into_the_built_in_assistant():
    """The Zenvi Assistant runs inside the backend this proxies to — handing it
    a tool to call itself would be circular."""
    from classes.tool_handlers import AGENT_TOOL_HANDLERS
    from classes.agent_api_proxy import MCP_EXTRA_TOOLS

    assert not (set(MCP_EXTRA_TOOLS) & set(AGENT_TOOL_HANDLERS))
