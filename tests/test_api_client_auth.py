"""ZenviBackendClient must send the signed-in user's JWT as a bearer header.

Paid backend routes (/search, /generation/*, /research/*) return 401 without
``Authorization: Bearer <jwt>``. The header has to track the live login:
present after sign-in, refreshed when the token rotates, gone after sign-out.
"""

import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from classes import api_client  # noqa: E402


def _client():
    return api_client.ZenviBackendClient(base_url="https://backend.test")


def test_session_carries_bearer_token_when_signed_in():
    c = _client()
    with patch.object(api_client.ZenviBackendClient, "_auth_token", staticmethod(lambda: "jwt-1")):
        assert c.session.headers["Authorization"] == "Bearer jwt-1"
        assert c.session.headers["Content-Type"] == "application/json"


def test_session_has_no_bearer_header_when_signed_out():
    c = _client()
    with patch.object(api_client.ZenviBackendClient, "_auth_token", staticmethod(lambda: None)):
        assert "Authorization" not in c.session.headers


def test_session_bearer_follows_token_rotation_and_sign_out():
    c = _client()
    with patch.object(api_client.ZenviBackendClient, "_auth_token", staticmethod(lambda: "jwt-1")):
        assert c.session.headers["Authorization"] == "Bearer jwt-1"
    with patch.object(api_client.ZenviBackendClient, "_auth_token", staticmethod(lambda: "jwt-2")):
        assert c.session.headers["Authorization"] == "Bearer jwt-2"
    with patch.object(api_client.ZenviBackendClient, "_auth_token", staticmethod(lambda: None)):
        assert "Authorization" not in c.session.headers


def test_search_posts_with_bearer_header():
    c = _client()
    with patch.object(api_client.ZenviBackendClient, "_auth_token", staticmethod(lambda: "jwt-1")):
        sess = c.session
        seen = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": []}

        def fake_post(url, **kw):
            seen["url"] = url
            seen["auth"] = sess.headers.get("Authorization")
            return _Resp()

        with patch.object(sess, "post", fake_post):
            out = c.search("a dog", top_k=3)
    assert out == {"results": []}
    assert seen["url"].endswith("/search")
    assert seen["auth"] == "Bearer jwt-1"


def test_parallel_indexing_session_also_carries_bearer():
    c = _client()
    with patch.object(api_client.ZenviBackendClient, "_auth_token", staticmethod(lambda: "jwt-1")):
        s = c._new_http_session()
    assert s.headers["Authorization"] == "Bearer jwt-1"
