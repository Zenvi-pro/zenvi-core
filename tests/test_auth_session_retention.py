"""A sign-in survives updates, network blips and other Zenvi processes.

is_authenticated() used to delete zenvi_auth.json whenever a refresh failed --
offline, a timeout, a 5xx -- so the next launch (typically the one right after
an auto-update) opened the website's login page again. And every process
refreshing on its own spent the same rotating refresh token twice, which the
auth server answers by revoking the whole sign-in.
"""

import base64
import json
import os
import sys
import threading
import time

import pytest
import requests

from classes import auth_manager as am


def _jwt(claims: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'HS256'})}.{seg(claims)}.sig"


def _session(exp_in: float, refresh: str = "r1", email: str = "a@example.com") -> dict:
    claims = {"sub": "user-123", "email": email, "exp": int(time.time() + exp_in)}
    return {"access_token": _jwt(claims), "refresh_token": refresh,
            "user_id": "user-123", "user_email": email}


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeAuthServer:
    """Stands in for POST /auth/v1/token?grant_type=refresh_token."""

    def __init__(self, answer=None, delay=0.0):
        self.answer = answer
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, url, headers=None, json=None, timeout=None):
        with self._lock:
            self.calls.append(json["refresh_token"])
        if self.delay:
            time.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer is not None:
            return self.answer
        n = len(self.calls)
        fresh = _session(3600, refresh=f"r{n + 1}")
        return FakeResponse(200, {
            "access_token": fresh["access_token"],
            "refresh_token": fresh["refresh_token"],
            "expires_at": int(time.time()) + 3600,
            "user": {"id": "user-123", "email": "a@example.com"},
        })


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "AUTH_FILE", str(tmp_path / "zenvi_auth.json"))
    monkeypatch.setattr(am, "AUTH_LOCK_FILE", str(tmp_path / "zenvi_auth.lock"))
    monkeypatch.setattr(am.info, "USER_PATH", str(tmp_path))
    monkeypatch.setenv("SUPABASE_URL", "https://auth.invalid")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setattr(am, "load_zenvi_dotenv", lambda *a, **k: None)
    manager = am.AuthManager()
    monkeypatch.setattr(am.AuthManager, "_instance", manager)
    return manager


def _server(monkeypatch, **kwargs) -> FakeAuthServer:
    server = FakeAuthServer(**kwargs)
    monkeypatch.setattr(am.requests, "post", server)
    return server


def _on_disk() -> dict | None:
    try:
        with open(am.AUTH_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


# ── Transient failures keep the sign-in ──────────────────────────────────────

@pytest.mark.parametrize("answer", [
    requests.ConnectionError("offline"),
    requests.Timeout("slow"),
    FakeResponse(500, {"message": "upstream"}),
    FakeResponse(503),
    FakeResponse(429, {"error_code": "over_request_rate_limit"}),
    FakeResponse(401, {"message": "Invalid API key"}),
    FakeResponse(409, {"code": 409, "error_code": "conflict",
                       "msg": "Too many concurrent token refresh requests on the same "
                              "session or refresh token"}),
    FakeResponse(200, {"unexpected": True}),
])
def test_a_refresh_that_fails_for_a_transient_reason_keeps_the_session(auth, monkeypatch, answer):
    auth.save_session(_session(-60))
    _server(monkeypatch, answer=answer)

    assert auth.is_authenticated()
    assert auth.has_session()
    assert _on_disk()["refresh_token"] == "r1"


def test_a_failed_refresh_is_not_retried_on_every_call(auth, monkeypatch):
    auth.save_session(_session(-60))
    server = _server(monkeypatch, answer=requests.ConnectionError("offline"))

    for _ in range(5):
        assert auth.is_authenticated()
    assert len(server.calls) == 1

    # After the back-off a new attempt goes out, and its success sticks.
    auth._retry_refresh_at = 0.0
    server.answer = None
    assert auth.get_access_token() == _on_disk()["access_token"]
    assert _on_disk()["refresh_token"] == "r3"


# ── Only the server rejecting the token ends it ─────────────────────────────

@pytest.mark.parametrize("answer", [
    FakeResponse(400, {"code": 400, "error_code": "refresh_token_already_used",
                       "msg": "Invalid Refresh Token: Already Used"}),
    FakeResponse(400, {"error_code": "refresh_token_not_found"}),
    FakeResponse(400, {"error": "invalid_grant",
                       "error_description": "Invalid Refresh Token: Refresh Token Not Found"}),
    FakeResponse(403, {"error_code": "session_not_found"}),
    FakeResponse(404, {"error_code": "user_not_found"}),
])
def test_a_rejected_refresh_token_ends_the_sign_in(auth, monkeypatch, answer):
    auth.save_session(_session(-60))
    _server(monkeypatch, answer=answer)

    assert not auth.is_authenticated()
    assert _on_disk() is None
    assert not auth.has_session()


def test_an_explicit_refresh_after_a_401_uses_the_server(auth, monkeypatch):
    auth.save_session(_session(3600))
    server = _server(monkeypatch)

    token = auth.refresh_access_token()
    assert token and token == _on_disk()["access_token"]
    assert server.calls == ["r1"]


# ── One refresh token, many threads and processes ───────────────────────────

def test_concurrent_callers_spend_the_refresh_token_once(auth, monkeypatch):
    auth.save_session(_session(-60))
    server = _server(monkeypatch, delay=0.2)

    results = []
    threads = [threading.Thread(target=lambda: results.append(auth.get_access_token()))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert server.calls == ["r1"]
    assert len(set(results)) == 1 and results[0] == _on_disk()["access_token"]


def test_a_session_another_process_refreshed_is_adopted_without_the_network(auth, monkeypatch):
    auth.save_session(_session(-60, refresh="r1"))
    # Another Zenvi process refreshes and writes the file.
    am.AuthManager._write_session_file(_session(3600, refresh="r2"))
    server = _server(monkeypatch)

    assert auth.get_access_token() == _on_disk()["access_token"]
    assert server.calls == []
    assert auth._session["refresh_token"] == "r2"


def test_a_stale_file_does_not_replace_a_newer_session_in_memory(auth, monkeypatch):
    auth.save_session(_session(-60, refresh="r1"))
    # Our newer tokens never reached the disk (a failed write).
    auth._session = _session(-30, refresh="r9")
    server = _server(monkeypatch)

    assert auth.is_authenticated()
    assert server.calls == ["r9"]


def test_a_profile_that_cannot_lock_still_refreshes(auth, monkeypatch):
    auth.save_session(_session(-60))
    server = _server(monkeypatch)

    @am.contextlib.contextmanager
    def no_locking(path, timeout):
        yield am.LOCK_UNAVAILABLE

    monkeypatch.setattr(am, "_interprocess_lock", no_locking)
    assert auth.is_authenticated()
    assert server.calls == ["r1"]


def test_logging_out_does_not_wait_for_a_refresh_in_flight(auth, monkeypatch):
    auth.save_session(_session(-60))
    server = _server(monkeypatch, delay=1.5)
    worker = threading.Thread(target=auth.is_authenticated)
    worker.start()
    while not server.calls:
        time.sleep(0.01)

    started = time.monotonic()
    auth.clear_session()
    assert time.monotonic() - started < 0.5
    worker.join(5)
    # The refresh that was in flight must not bring the sign-in back.
    assert _on_disk() is None
    assert not auth.has_session()


def test_a_sign_in_ended_in_another_process_is_not_brought_back(auth, monkeypatch):
    auth.save_session(_session(-60))
    os.remove(am.AUTH_FILE)
    server = _server(monkeypatch)

    assert not auth.is_authenticated()
    assert server.calls == []
    assert _on_disk() is None


def test_listeners_hear_when_the_server_ends_the_sign_in(auth, monkeypatch):
    auth.save_session(_session(-60))
    _server(monkeypatch, answer=FakeResponse(400, {"error_code": "refresh_token_already_used"}))
    heard = []
    auth.add_revoked_listener(lambda: heard.append(1))

    assert auth.get_access_token() is None
    assert auth.get_access_token() is None
    assert heard == [1]


def test_a_refresh_waits_for_another_process_holding_the_lock(auth, monkeypatch):
    auth.save_session(_session(-60))
    server = _server(monkeypatch)
    monkeypatch.setattr(am, "REFRESH_LOCK_TIMEOUT", 0.3)

    with am._interprocess_lock(am.AUTH_LOCK_FILE, 1) as held:
        assert held == am.LOCK_HELD
        # Still signed in, just not refreshed yet.
        assert auth.is_authenticated()
    assert server.calls == []

    auth._retry_refresh_at = 0.0
    assert auth.is_authenticated()
    assert server.calls == ["r1"]


# ── The file itself ──────────────────────────────────────────────────────────

def test_an_unreadable_session_file_is_kept_for_the_next_sign_in_to_replace(auth):
    with open(am.AUTH_FILE, "w", encoding="utf-8") as fh:
        fh.write('{"access_token": ')
    assert not auth.has_session()
    assert os.path.exists(am.AUTH_FILE)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_the_session_file_is_private_and_written_whole(auth, tmp_path):
    auth.save_session(_session(3600))
    assert (os.stat(am.AUTH_FILE).st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_the_startup_gate_needs_no_network_for_an_expired_token(auth, monkeypatch):
    am.AuthManager._write_session_file(_session(-3600))
    server = _server(monkeypatch)
    assert auth.has_session()
    assert server.calls == []


def test_a_background_check_reports_only_a_revoked_sign_in(auth, monkeypatch):
    auth.save_session(_session(-60))
    server = _server(monkeypatch, answer=requests.ConnectionError("offline"))
    told = threading.Event()

    auth.check_session_async(on_revoked=told.set)
    time.sleep(0.3)
    assert not told.is_set()

    auth._retry_refresh_at = 0.0
    server.answer = FakeResponse(400, {"error_code": "refresh_token_already_used"})
    auth.check_session_async(on_revoked=told.set)
    assert told.wait(5)


# ── The browser hand-off ─────────────────────────────────────────────────────

def test_the_browser_hand_off_waits_for_the_rows_whole_lifetime():
    # desktop_auth_sessions rows expire after 10 minutes; the old 45 s cut the
    # user off mid sign-in and showed the password form.
    assert am.POLL_TIMEOUT >= 600


def test_a_cancelled_hand_off_does_not_replace_the_sign_in_made_meanwhile(auth, monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        handle.set()  # the user switched to the password form meanwhile
        return FakeResponse(200, [{"authenticated": True, **_session(3600, refresh="browser")}])

    auth.save_session(_session(3600, refresh="password"))
    monkeypatch.setattr(am.requests, "post", fake_post)
    done = threading.Event()
    handle = threading.Event()
    real_event = threading.Event
    monkeypatch.setattr(am.threading, "Event", lambda: handle)
    auth.poll_for_session("s", on_success=lambda s: done.set(), on_timeout=done.set,
                          interval=0.05, timeout=1)
    monkeypatch.setattr(am.threading, "Event", real_event)
    time.sleep(0.5)
    assert not done.is_set()
    assert _on_disk()["refresh_token"] == "password"


def test_cancelling_one_hand_off_leaves_another_running(auth, monkeypatch):
    answers = {"a": [], "b": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        state = json["session_state"]
        answers[state].append(1)
        if state == "b" and len(answers["b"]) >= 2:
            return FakeResponse(200, [{"authenticated": True, **_session(3600, refresh="rb")}])
        return FakeResponse(200, [{"authenticated": False}])

    monkeypatch.setattr(am.requests, "post", fake_post)
    a_done, b_done = threading.Event(), threading.Event()
    handle_a = auth.poll_for_session("a", on_success=lambda s: a_done.set(),
                                     on_timeout=a_done.set, interval=0.05, timeout=5)
    auth.poll_for_session("b", on_success=lambda s: b_done.set(),
                          on_timeout=lambda: None, interval=0.05, timeout=5)
    auth.cancel_poll(handle_a)

    assert b_done.wait(5)
    assert not a_done.is_set()
    assert _on_disk()["refresh_token"] == "rb"


# ── Races found in review ────────────────────────────────────────────────────

def test_a_failing_refresh_does_not_wipe_a_sign_in_made_meanwhile(auth, monkeypatch):
    """Refresh in flight, Log Out, sign in again: the old refresh's failure
    must not clear the new sign-in."""
    auth.save_session(_session(-60, refresh="old"))
    server = _server(monkeypatch, answer=FakeResponse(400, {"error_code": "refresh_token_already_used"}),
                     delay=0.5)
    heard = []
    auth.add_revoked_listener(lambda: heard.append(1))
    worker = threading.Thread(target=auth.is_authenticated)
    worker.start()
    while not server.calls:
        time.sleep(0.01)
    auth.clear_session()
    signer = threading.Thread(target=lambda: auth.save_session(_session(3600, refresh="new")))
    signer.start()
    worker.join(5)
    signer.join(10)

    assert _on_disk()["refresh_token"] == "new"
    assert auth.has_session()
    assert heard == []


def test_a_sign_out_in_another_process_during_a_refresh_stays_signed_out(auth, monkeypatch):
    auth.save_session(_session(-60))

    def server(url, headers=None, json=None, timeout=None):
        os.remove(am.AUTH_FILE)          # another process logs out mid-request
        return FakeAuthServer()(url, headers=headers, json=json, timeout=timeout)

    monkeypatch.setattr(am.requests, "post", server)
    assert not auth.is_authenticated()
    assert _on_disk() is None


def test_the_gui_thread_never_queues_behind_another_threads_refresh(auth, monkeypatch):
    auth.save_session(_session(-60))
    server = _server(monkeypatch, delay=1.5)
    worker = threading.Thread(target=auth.is_authenticated)
    worker.start()
    while not server.calls:
        time.sleep(0.01)

    # pytest runs this test on the main thread, which is Zenvi's GUI thread.
    started = time.monotonic()
    assert auth.get_access_token() is not None
    assert time.monotonic() - started < 0.5
    worker.join(5)
    assert server.calls == ["r1"]


# ── The confirmation code ────────────────────────────────────────────────────

def test_the_confirmation_code_matches_the_websites():
    # src/lib/desktop-handoff.test.ts in zenvi-pro asserts the same values.
    assert am.confirmation_code("00000000-0000-4000-8000-000000000000") == "5QA-FM2"
    assert am.confirmation_code("3f2b8c1e-9d4a-4e7b-a1c2-5d6e7f8a9b0c") == "UPJ-EH3"


def test_the_sign_in_link_says_this_app_shows_the_code(auth, monkeypatch):
    opened = []
    monkeypatch.setattr(am.webbrowser, "open", opened.append)
    monkeypatch.setattr(am.sys, "platform", "darwin")
    url, state = auth.start_auth_flow()
    assert url.endswith(f"/login?state={state}&v=2") and opened == [url]


def test_a_saved_sign_in_tells_its_listeners_once_and_a_failing_listener_breaks_nothing(auth):
    heard = []
    auth.add_session_listener(lambda: heard.append("saved"))
    auth.add_session_listener(lambda: 1 / 0)
    auth.save_session(_session(3600))
    assert heard == ["saved"] and auth.load_session() is not None
