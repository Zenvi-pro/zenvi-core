"""A browser sign-in must leave a session the credits badge can key on.

poll_desktop_auth_session returns only the tokens (no user_id / user_email),
and CreditsClient drops any balance it cannot tie to the signed-in user_id,
so the badge sat on its placeholder until the first token refresh.
"""

import base64
import json
import time

import pytest

from classes import auth_manager as am
from classes import credits_client as cc


def _jwt(claims: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'HS256'})}.{seg(claims)}.sig"


_CLAIMS = {"sub": "user-123", "email": "a@example.com", "exp": int(time.time()) + 3600}

# The row poll_desktop_auth_session returns.
_BROWSER_ROW = {"authenticated": True, "access_token": _jwt(_CLAIMS), "refresh_token": "r"}


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "AUTH_FILE", str(tmp_path / "zenvi_auth.json"))
    monkeypatch.setattr(am.info, "USER_PATH", str(tmp_path))
    monkeypatch.setattr(cc, "CREDITS_FILE", str(tmp_path / "zenvi_credits.json"))
    manager = am.AuthManager()
    monkeypatch.setattr(am.AuthManager, "_instance", manager)
    return manager


def test_saving_a_token_only_session_fills_the_identity_from_the_token(auth):
    auth.save_session(dict(_BROWSER_ROW))
    with open(am.AUTH_FILE, encoding="utf-8") as fh:
        saved = json.load(fh)
    assert saved["user_id"] == "user-123"
    assert saved["user_email"] == "a@example.com"
    assert auth.get_user_email() == "a@example.com"


def test_a_token_only_session_already_on_disk_is_filled_on_load(auth):
    with open(am.AUTH_FILE, "w", encoding="utf-8") as fh:
        json.dump(_BROWSER_ROW, fh)
    assert auth.load_session()["user_id"] == "user-123"


def test_an_identity_already_present_is_kept(auth):
    auth.save_session(dict(_BROWSER_ROW, user_id="kept", user_email="kept@example.com"))
    assert auth.load_session()["user_id"] == "kept"
    assert auth.get_user_email() == "kept@example.com"


def test_a_session_without_a_readable_token_is_saved_unchanged(auth):
    auth.save_session({"authenticated": True, "access_token": "not-a-jwt"})
    assert "user_id" not in auth.load_session()


def test_the_badge_paints_after_a_browser_sign_in(auth, monkeypatch):
    auth.save_session(dict(_BROWSER_ROW))
    client = cc.CreditsClient()
    monkeypatch.setattr(client, "_rpc", lambda *a, **k: [{"total_points": 24692}])
    heard = []
    client.add_listener(heard.append)
    assert client.balance() == (True, 24692)
    assert heard == [24692]
    assert client.cached_balance() == 24692
