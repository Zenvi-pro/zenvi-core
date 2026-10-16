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


# ============================ media index v2: what a refused request looks like to the editor ============================
class _Reply:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _v2_reply(reply):
    c = _client()

    class Session:
        def post(self, *a, **k):
            return reply

        get = post

    return c._v2("POST", "/understand", {}, session=Session())


def test_a_402_is_out_of_credits_with_the_servers_message():
    out = _v2_reply(_Reply(402, {"detail": {"code": "insufficient_credits", "message": "Out of credits for video indexing. Need 18, have 3."}}))
    assert out == {"credits": True, "error": "Out of credits for video indexing. Need 18, have 3."}


def test_a_402_without_a_message_still_says_what_happened():
    out = _v2_reply(_Reply(402, None))
    assert out["credits"] is True and "credits" in out["error"].lower()


def test_a_429_says_when_to_come_back():
    out = _v2_reply(_Reply(429, {"detail": {"code": "rate_limited", "message": "Too many embed requests this hour.", "retry_after": 120}}))
    assert out == {"rate_limited": True, "error": "Too many embed requests this hour.", "retry_after": 120}


def test_a_403_is_a_refusal_not_a_request_to_sign_in():
    out = _v2_reply(_Reply(403, {"detail": {"code": "not_owner", "message": "That upload belongs to another account."}}))
    assert out == {"forbidden": True, "error": "That upload belongs to another account."} and "auth" not in out


def test_a_401_still_asks_the_user_to_sign_in():
    assert _v2_reply(_Reply(401, {"detail": "no"}))["auth"] is True


def test_the_credits_badge_is_refreshed_only_when_the_backend_really_charged():
    for body, expected in (({"job_id": "j", "billing": {"credits": 5, "basis": "tokens"}}, 1), ({"status": "done", "result": {"billing": {"credits": 3}}}, 1),
                           ({"status": "done", "result": {"billing": {"credits": 0}}}, 0), ({"vectors": []}, 0)):
        with patch.object(api_client, "_refresh_credits_after_backend_billing") as refresh:
            assert _v2_reply(_Reply(200, body)) == body
        assert refresh.call_count == expected, body
