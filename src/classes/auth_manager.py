"""
Zenvi authentication manager.

Primary flow (browser OAuth):
  1. start_auth_flow() → opens browser at ZENVI_WEBSITE/login?state=<uuid>
  2. poll_for_session() → polls Supabase RPC every 2s in a background thread
  3. On success, saves session to disk and calls on_success(session_dict)
  4. On timeout, calls on_timeout() so the UI can offer to try again

Fallback flow (email + password):
  sign_in_with_password(email, password) / sign_up_with_password(email, password)
  — call Supabase Auth REST API directly; no browser required.

Keeping the session:
  A sign-in lasts until the server says the refresh token is dead. A refresh
  that fails for any other reason (offline, a timeout, a 5xx, a rate limit)
  keeps the session and is retried later; deleting it there is what used to
  send people back to the browser after an update or a network blip.
  Every process that runs Zenvi (the window, a headless session, the old and
  new process around an update) shares ~/.openshot_qt/zenvi_auth.json, and
  Supabase rotates the refresh token on every use. So a refresh holds a lock
  across threads and processes and first re-reads the file: if another
  process already refreshed, its token is adopted instead of reusing ours,
  which the server would treat as a stolen token.
"""

import base64
import contextlib
import errno
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser

import requests

from classes import info
from classes.zenvi_env import load_zenvi_dotenv

# A child of the app's "OpenShot" logger, so sign-in events reach
# openshot-qt.log (a module logger only forwards ERRORs there).
log = logging.getLogger("OpenShot.auth")

load_zenvi_dotenv()


def _supabase_url() -> str:
    """Read at call time so values from ``.env`` are visible after load (frozen layouts)."""
    load_zenvi_dotenv()
    return os.environ.get("SUPABASE_URL", "").strip()


def _supabase_anon_key() -> str:
    load_zenvi_dotenv()
    return os.environ.get("SUPABASE_ANON_KEY", "").strip()


def _zenvi_website() -> str:
    load_zenvi_dotenv()
    return os.environ.get("ZENVI_WEBSITE", "https://zenvi.pro").strip()


# ── Constants ──────────────────────────────────────────────────────────────────
# Module-level snapshots evaluated after load_zenvi_dotenv() above. Used by
# external callers (credits_client, query_tests); the underscore helpers above
# are the authoritative read path for auth_manager's own code, since they
# re-call load_zenvi_dotenv() in case .env arrives after this module imports.
SUPABASE_URL = _supabase_url()
SUPABASE_ANON_KEY = _supabase_anon_key()
ZENVI_WEBSITE = _zenvi_website()

AUTH_FILE = os.path.join(info.USER_PATH, "zenvi_auth.json")
# Held while refreshing, so two processes never spend the same refresh token.
AUTH_LOCK_FILE = os.path.join(info.USER_PATH, "zenvi_auth.lock")

POLL_INTERVAL = 2
# The website's desktop_auth_sessions row lives 10 minutes: wait for all of it,
# since the browser may need a password, a code or a new account first.
POLL_TIMEOUT = 600

# Refresh this long before the access token's exp, so no request leaves with a
# token that expires on the way.
EXPIRY_MARGIN = 60
# After a refresh failed for a transient reason, don't ask again for this long.
REFRESH_RETRY_AFTER = 30
# How long a refresh waits for another process's refresh to finish.
REFRESH_LOCK_TIMEOUT = 20

# _refresh_session() outcomes
REFRESHED = "refreshed"
TRANSIENT = "transient"   # offline, timeout, 5xx, 429...: keep the session, retry later
REVOKED = "revoked"       # the server will never accept this refresh token again
BUSY = "busy"             # internal: another thread is refreshing right now

# Auth error codes that mean the refresh token is dead for good.
_REVOKED_ERROR_CODES = frozenset({
    "refresh_token_not_found",
    "refresh_token_already_used",
    "session_not_found",
    "session_expired",
    "user_not_found",
    "user_banned",
    "invalid_grant",
})


class AuthError(Exception):
    """Raised when sign-in fails."""


# Letters and digits that can't be misread for each other (no I/1, O/0).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def confirmation_code(state: str) -> str:
    """The code both the app and the website show for a browser sign-in.

    Derived from the state in the URL (the website computes the same, in
    src/lib/desktop-handoff.ts), so a sign-in link someone else made shows a
    code that doesn't match the one in the user's Zenvi window.
    """
    digest = hashlib.sha256(state.encode("utf-8")).digest()
    bits = int.from_bytes(digest[:4], "big") >> 2   # 30 bits = 6 characters
    chars = [_CODE_ALPHABET[(bits >> (5 * (5 - i))) & 31] for i in range(6)]
    return "".join(chars[:3]) + "-" + "".join(chars[3:])


def _refresh_failure_kind(status_code: int, body: dict) -> str:
    """REVOKED when the auth server rejected the refresh token itself, else TRANSIENT.

    Only a 400/401/403/404 that names a dead token or session counts: a rate
    limit, a 409 for concurrent refreshes, a gateway error or an unexpected
    answer must not cost the user their sign-in.
    """
    if status_code not in (400, 401, 403, 404):
        return TRANSIENT
    code = str(body.get("error_code") or "").lower()
    error = str(body.get("error") or "").lower()
    message = " ".join(
        str(body.get(key) or "") for key in ("msg", "message", "error_description")
    ).strip().lower()
    if code in _REVOKED_ERROR_CODES or error in _REVOKED_ERROR_CODES:
        return REVOKED
    if message.startswith("invalid refresh token"):
        return REVOKED
    return TRANSIENT


# errno values meaning "someone else holds the lock" (anything else: no locking here)
_LOCK_BUSY_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK})

# Lock outcomes yielded by _interprocess_lock
LOCK_HELD = "held"
LOCK_BUSY = "busy"            # another process kept it past the timeout
LOCK_UNAVAILABLE = "none"     # the file system can't lock; carry on without


@contextlib.contextmanager
def _interprocess_lock(path: str, timeout: float):
    """Hold an OS file lock on *path*; yields LOCK_HELD, LOCK_BUSY or LOCK_UNAVAILABLE.

    The OS drops the lock when its holder exits, so a crashed process never
    leaves Zenvi locked out of its own session. A profile on a file system
    without locking (some network homes) yields LOCK_UNAVAILABLE at once.
    """
    handle = None
    state = LOCK_UNAVAILABLE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+b")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = LOCK_HELD
                break
            except OSError as exc:
                if exc.errno not in _LOCK_BUSY_ERRNOS:
                    log.warning("Could not lock %s (%s); refreshing without it", path, exc)
                    break
                if time.monotonic() >= deadline:
                    state = LOCK_BUSY
                    break
                time.sleep(0.1)
    except OSError as exc:
        log.warning("Could not open the Zenvi auth lock %s (%s); refreshing without it", path, exc)
    try:
        yield state
    finally:
        if handle is not None:
            if state == LOCK_HELD:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


class AuthManager:
    """Singleton that manages the Zenvi account session."""

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "AuthManager":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._session: dict | None = None
        # One refresh at a time in this process (held across the network call);
        # the file lock covers other processes.
        self._refresh_lock = threading.RLock()
        # Short critical sections: swapping the in-memory session, writing the file.
        self._state_lock = threading.RLock()
        # Bumped by clear_session(), so a refresh in flight can't bring back a
        # sign-in the user just ended.
        self._sign_out_count = 0
        # Monotonic time before which a transiently failed refresh isn't retried.
        self._retry_refresh_at = 0.0
        # Called (from whichever thread noticed) when the server ends the sign-in.
        self._revoked_listeners: list = []
        # Cancel handles of the browser hand-offs still being polled.
        self._polls: set[threading.Event] = set()
        self._poll_thread: threading.Thread | None = None
        # True while this thread holds the auth file lock (refresh in progress).
        self._file_lock_owner = threading.local()
        # Whether the in-memory session is the one in the file (read or written).
        self._session_on_disk = False
        self._session_listeners: list = []

    def add_session_listener(self, callback) -> None:
        """Call *callback()* (on whichever thread saved it) after a session is saved.

        Used to resume work that waited for a signed-in user. Keep it fast and thread-safe,
        for example emitting a Qt signal.
        """
        if callback not in self._session_listeners:
            self._session_listeners.append(callback)

    def remove_session_listener(self, callback) -> None:
        try:
            self._session_listeners.remove(callback)
        except ValueError:
            pass

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    def _anon_headers(self) -> dict:
        return {
            "apikey": _supabase_anon_key(),
            "Content-Type": "application/json",
        }

    def _authed_headers(self) -> dict:
        token = self.get_access_token()
        h = self._anon_headers()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    # ── Session persistence ────────────────────────────────────────────────────

    @classmethod
    def _read_session_file(cls) -> dict | None:
        """The session on disk, or None (missing or unreadable -- never deleted here)."""
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as fh:
                session = json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning("Could not load Zenvi auth session: %s", exc)
            return None
        if not isinstance(session, dict):
            return None
        return cls._with_identity(session)

    @staticmethod
    def _write_session_file(session: dict) -> None:
        """Write *session* atomically and readable by this user only.

        Another process may read the file at any moment, and a crash mid-write
        must not leave half a file: write a private temp file, then rename it
        over the old one.
        """
        os.makedirs(info.USER_PATH, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".zenvi_auth.", suffix=".tmp", dir=info.USER_PATH)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(session, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            for attempt in range(10):
                try:
                    os.replace(tmp_path, AUTH_FILE)
                    break
                except PermissionError:
                    # Windows refuses while another process has the file open.
                    if attempt == 9:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load_session(self) -> dict | None:
        with self._state_lock:
            session = self._read_session_file()
            if session is not None:
                self._session = session
                self._session_on_disk = True
            return session

    def save_session(self, session: dict) -> None:
        """Store a new sign-in (or refreshed tokens) for every Zenvi process."""
        session = self._with_identity(session)
        if getattr(self._file_lock_owner, "held", False):
            # Inside this thread's own refresh, which holds both locks.
            stored = self._store(session)
        else:
            # A sign-in must not interleave with a refresh -- this process's, or
            # another's -- which would then write the old sign-in over this one.
            with self._refresh_lock:
                with _interprocess_lock(AUTH_LOCK_FILE, 5):
                    stored = self._store(session)
        if stored:          # outside the locks: a listener may hop threads
            for callback in list(self._session_listeners):
                try:
                    callback()
                except Exception as exc:  # a dead listener must never break sign-in
                    log.debug("Session listener failed: %s", exc)

    def _store(self, session: dict, sign_outs: int | None = None) -> bool:
        """Make *session* current, on disk and in memory.

        With *sign_outs*, only if nobody signed out since that count was read
        (checked under the same lock clear_session() takes). Returns whether
        it was stored.
        """
        with self._state_lock:
            if sign_outs is not None and sign_outs != self._sign_out_count:
                return False
            try:
                self._write_session_file(session)
                self._session_on_disk = True
                log.info("Zenvi session saved for user: %s", session.get("user_email", "?"))
            except Exception as exc:
                self._session_on_disk = False
                log.error("Could not save Zenvi auth session: %s", exc)
            # Keep the new tokens in memory even if the disk write failed:
            # the old refresh token is spent either way.
            self._session = session
            self._retry_refresh_at = 0.0
            return True

    def clear_session(self) -> None:
        """Forget the sign-in here and on disk. Never waits for a refresh."""
        with self._state_lock:
            self._sign_out_count += 1
            try:
                if os.path.exists(AUTH_FILE):
                    os.remove(AUTH_FILE)
            except Exception as exc:
                log.warning("Could not clear Zenvi auth file: %s", exc)
            self._session = None
            self._session_on_disk = False
            self._retry_refresh_at = 0.0

    def add_revoked_listener(self, callback) -> None:
        """Call *callback()* whenever the server ends the sign-in.

        Runs on the thread that noticed (often a worker); marshal to the GUI
        thread yourself.
        """
        if callback not in self._revoked_listeners:
            self._revoked_listeners.append(callback)

    def remove_revoked_listener(self, callback) -> None:
        if callback in self._revoked_listeners:
            self._revoked_listeners.remove(callback)

    def _notify_revoked(self) -> None:
        for callback in list(self._revoked_listeners):
            try:
                callback()
            except Exception:
                log.warning("A sign-in-ended listener failed", exc_info=True)

    # ── Token helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _jwt_claims(token: str) -> dict:
        """Decode a JWT's payload using stdlib only (no signature check)."""
        try:
            payload_b64 = token.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @classmethod
    def _decode_jwt_exp(cls, token: str) -> int | None:
        """Extract the ``exp`` claim from a JWT using stdlib only."""
        return cls._jwt_claims(token).get("exp")

    @classmethod
    def _with_identity(cls, session: dict) -> dict:
        """Fill a missing user_id / user_email from the access token's claims.

        The browser flow's poll_desktop_auth_session row carries only the
        tokens, and the credits badge keys its balance on user_id.
        """
        if not isinstance(session, dict):
            return session
        if session.get("user_id") and session.get("user_email"):
            return session
        claims = cls._jwt_claims(session.get("access_token") or "")
        filled = dict(session)
        if not filled.get("user_id") and claims.get("sub"):
            filled["user_id"] = claims["sub"]
        if not filled.get("user_email") and claims.get("email"):
            filled["user_email"] = claims["email"]
        return filled

    @classmethod
    def _token_is_fresh(cls, session: dict | None) -> bool:
        """True while *session*'s access token has more than EXPIRY_MARGIN left.

        A token without a readable exp is taken at face value; the server
        decides, and a 401 triggers refresh_access_token().
        """
        token = (session or {}).get("access_token")
        if not token:
            return False
        exp = cls._decode_jwt_exp(token)
        return exp is None or exp - EXPIRY_MARGIN > time.time()

    @classmethod
    def _is_newer_session(cls, candidate: dict | None, current: dict | None) -> bool:
        """True when *candidate* (read from disk) replaces *current* (in memory).

        A different refresh token means someone refreshed or signed in again;
        it is adopted unless its access token is older than ours (a write of
        ours that never reached the disk).
        """
        if not candidate or not candidate.get("refresh_token"):
            return False
        if not current or candidate.get("refresh_token") == current.get("refresh_token"):
            return not current
        theirs = cls._decode_jwt_exp(candidate.get("access_token") or "")
        ours = cls._decode_jwt_exp(current.get("access_token") or "")
        return theirs is None or ours is None or theirs >= ours

    def _refresh_session(self, seen_refresh_token: str | None = None) -> str:
        """Trade the refresh token for a new session: REFRESHED, TRANSIENT,
        REVOKED, or BUSY (another thread is refreshing; only on the GUI thread).

        *seen_refresh_token* is the token the caller found stale; if another
        thread replaced it while this one waited, that refresh is reused.
        Only REVOKED means the sign-in is over; the caller decides what to do
        with it. Nothing here deletes the session file.
        """
        # The GUI (main) thread never queues behind another thread's refresh:
        # it goes on with the token it has and leaves the refresh to finish.
        on_main_thread = threading.current_thread() is threading.main_thread()
        if not self._refresh_lock.acquire(blocking=not on_main_thread):
            return BUSY
        try:
            return self._refresh_locked(seen_refresh_token, 1 if on_main_thread else REFRESH_LOCK_TIMEOUT)
        finally:
            self._refresh_lock.release()

    def _refresh_locked(self, seen_refresh_token, lock_timeout) -> str:
        sign_outs = self._sign_out_count
        current = self._session
        if current and seen_refresh_token and current.get("refresh_token") != seen_refresh_token \
                and self._token_is_fresh(current):
            return REFRESHED
        with _interprocess_lock(AUTH_LOCK_FILE, lock_timeout) as lock:
            self._file_lock_owner.held = lock == LOCK_HELD
            try:
                # Another process (or this one, before a restart) may have
                # refreshed already: its token is the live one, ours is spent.
                on_disk = self._read_session_file()
                with self._state_lock:
                    current = self._session
                    if current is not None and self._session_on_disk and not os.path.exists(AUTH_FILE):
                        # The file this session came from is gone: signed
                        # out in another Zenvi process. Don't bring it back.
                        log.info("The Zenvi sign-in was ended by another Zenvi process")
                        return REVOKED
                    if self._is_newer_session(on_disk, current):
                        log.info("Adopting the Zenvi session another process refreshed")
                        self._session = current = on_disk
                        self._session_on_disk = True
                        if self._token_is_fresh(on_disk):
                            self._retry_refresh_at = 0.0
                            return REFRESHED
                if lock == LOCK_BUSY:
                    log.warning("Another Zenvi process is still refreshing the session; trying again later")
                    return TRANSIENT
                return self._refresh_with_server(current or {}, sign_outs)
            finally:
                self._file_lock_owner.held = False

    def _refresh_with_server(self, session: dict, sign_outs: int) -> str:
        refresh_token = session.get("refresh_token")
        if not refresh_token:
            # Nothing to trade in: an access token alone can't be renewed.
            return REVOKED
        if not _supabase_url() or not _supabase_anon_key():
            return TRANSIENT
        try:
            resp = requests.post(
                f"{_supabase_url()}/auth/v1/token?grant_type=refresh_token",
                headers=self._anon_headers(),
                json={"refresh_token": refresh_token},
                timeout=10,
            )
        except requests.RequestException as exc:
            log.warning("Token refresh network error (keeping the session): %s", exc)
            return TRANSIENT

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            access_token = data.get("access_token") if isinstance(data, dict) else None
            if not access_token:
                log.warning("Token refresh answered 200 without an access token (keeping the session)")
                return TRANSIENT
            if self._session_on_disk and not os.path.exists(AUTH_FILE):
                # Signed out in another Zenvi process while the request was
                # in flight: don't write the sign-in back.
                return REVOKED
            user = data.get("user") or {}
            stored = self._store(self._with_identity({
                "access_token": access_token,
                "refresh_token": data.get("refresh_token") or refresh_token,
                "expires_at": data.get("expires_at"),
                "user_id": user.get("id") or session.get("user_id"),
                "user_email": user.get("email") or session.get("user_email"),
            }), sign_outs=sign_outs)
            if not stored:
                # Signed out here while the request was in flight: drop the new tokens.
                return REVOKED
            log.info("Zenvi access token refreshed")
            return REFRESHED

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        kind = _refresh_failure_kind(resp.status_code, body)
        log.warning(
            "Token refresh failed (status=%s, error=%s): %s",
            resp.status_code,
            body.get("error_code") or body.get("error") or "",
            "the sign-in has ended" if kind == REVOKED else "keeping the session",
        )
        return kind

    def _ensure_fresh_token(self, force: bool = False) -> str:
        """Refresh the access token when it is (nearly) expired.

        Returns REFRESHED when the session holds a usable token, TRANSIENT
        when it couldn't be renewed right now, REVOKED when the sign-in ended
        (the session is then cleared and the listeners told).
        """
        with self._state_lock:
            if self._session is None:
                self.load_session()
            session = self._session
            if not session:
                return REVOKED
            if not force and self._token_is_fresh(session):
                return REFRESHED
            if not force and time.monotonic() < self._retry_refresh_at:
                return TRANSIENT
            seen = session.get("refresh_token")
        outcome = self._refresh_session(seen)
        if outcome == BUSY:
            return TRANSIENT
        if outcome == TRANSIENT:
            self._retry_refresh_at = time.monotonic() + REFRESH_RETRY_AFTER
        elif outcome == REVOKED:
            with self._state_lock:
                current = self._session
                if current is not None and current.get("refresh_token") != seen:
                    # Signed in again (or refreshed) while this refresh was
                    # failing: that newer sign-in stands.
                    return REFRESHED if self._token_is_fresh(current) else TRANSIENT
                ended = current is not None
                if ended:
                    log.info("The Zenvi sign-in has ended; clearing the stored session")
                    self.clear_session()
            if ended:
                self._notify_revoked()
        return outcome

    # ── Session queries ────────────────────────────────────────────────────────

    def has_session(self) -> bool:
        """True when a sign-in is stored, without touching the network.

        For the startup gate on the GUI thread: an expired access token with a
        refresh token is still a sign-in, refreshed in the background
        (check_session_async). Doesn't wait for a refresh in progress.
        """
        session = self._session
        if session is None:
            session = self.load_session()
        session = session or {}
        return bool(session.get("refresh_token")) or self._token_is_fresh(session)

    def is_authenticated(self) -> bool:
        """True while the user is signed in, refreshing the token if needed.

        Being offline (or the auth server failing) does not sign anyone out:
        the session is kept and the refresh retried later. Only the server
        rejecting the refresh token ends the sign-in.
        """
        return self._ensure_fresh_token() != REVOKED

    def get_access_token(self) -> str | None:
        if self._ensure_fresh_token() == REVOKED:
            return None
        return (self._session or {}).get("access_token")

    def refresh_access_token(self) -> str | None:
        """Refresh now, even if the token looks valid locally (a server answered 401,
        e.g. because of clock skew). Returns the new access token, or None."""
        if self._ensure_fresh_token(force=True) == REFRESHED:
            return (self._session or {}).get("access_token")
        return None

    def check_session_async(self, on_revoked=None) -> None:
        """Refresh an expired session off the calling thread.

        If the server says the sign-in has ended, the revoked listeners and
        *on_revoked* are called from that background thread; offline or
        failing servers keep the session.
        """
        def _worker():
            try:
                if self._ensure_fresh_token() == REVOKED and on_revoked is not None:
                    on_revoked()
            except Exception:
                log.warning("Checking the Zenvi session failed", exc_info=True)

        threading.Thread(target=_worker, daemon=True, name="zenvi-auth-check").start()

    def get_user_email(self) -> str | None:
        # Lock-free like has_session(): menus ask for this on the GUI thread.
        session = self._session
        if session is None:
            session = self._read_session_file()
        return (session or {}).get("user_email")

    # ── Browser OAuth flow ─────────────────────────────────────────────────────

    def start_auth_flow(self) -> tuple[str, str]:
        """
        Open ZENVI_WEBSITE/login?state=<uuid> in the system browser.
        Returns (url, state) so the caller can display/copy the URL.
        Tries xdg-open first (Linux), then webbrowser as fallback.
        """
        state = str(uuid.uuid4())
        # v=2: this app shows confirmation_code(state), so the website may
        # offer a signed-in browser one-click approval next to the same code.
        url = f"{_zenvi_website()}/login?state={state}&v=2"
        log.info("Opening auth URL: %s", url)
        opened = False
        if sys.platform.startswith("linux"):
            try:
                subprocess.Popen(["xdg-open", url],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                opened = True
            except (FileNotFoundError, OSError):
                pass
        if not opened:
            webbrowser.open(url)
        return url, state

    def poll_for_session(
        self,
        state: str,
        on_success,
        on_timeout,
        interval: int = POLL_INTERVAL,
        timeout: int = POLL_TIMEOUT,
    ) -> threading.Event:
        """
        Poll the Supabase RPC in a daemon thread.
        Calls on_success(session_dict) or on_timeout() from the background thread.
        Aborts immediately on fatal RPC errors (404 / 405 = RPC not found).
        Returns a handle for cancel_poll(); a cancelled poll calls neither callback.
        """
        cancel = threading.Event()
        self._polls.add(cancel)

        def _cancelled():
            return cancel.is_set()

        def _worker():
            try:
                _poll()
            finally:
                self._polls.discard(cancel)

        def _poll():
            deadline = time.monotonic() + timeout
            while not _cancelled() and time.monotonic() < deadline:
                try:
                    resp = requests.post(
                        f"{_supabase_url()}/rest/v1/rpc/poll_desktop_auth_session",
                        headers=self._anon_headers(),
                        json={"session_state": state},
                        timeout=8,
                    )
                    # Never log the body: once signed in it holds the tokens.
                    log.debug("Poll status %s", resp.status_code)
                    if resp.status_code == 200:
                        rows = resp.json()
                        if rows and isinstance(rows, list) and rows[0].get("authenticated"):
                            if _cancelled():
                                # Whoever asked has moved on (e.g. signed in with
                                # a password): don't overwrite that sign-in.
                                return
                            session = self._with_identity(
                                {k: v for k, v in rows[0].items() if k != "authenticated"})
                            self.save_session(session)
                            on_success(session)
                            return
                    elif resp.status_code in (404, 405):
                        # RPC function does not exist — no point polling further
                        log.warning("poll_desktop_auth_session RPC not found (%s). Falling back.", resp.status_code)
                        if not _cancelled():
                            on_timeout()
                        return
                except requests.RequestException as exc:
                    log.debug("Poll request failed (will retry): %s", exc)

                cancel.wait(interval)

            if not _cancelled():
                on_timeout()

        self._poll_thread = threading.Thread(target=_worker, daemon=True, name="zenvi-auth-poll")
        self._poll_thread.start()
        return cancel

    def cancel_poll(self, handle: threading.Event | None = None) -> None:
        """Stop polling for *handle*'s hand-off, or for every one in flight."""
        for poll in ([handle] if handle is not None else list(self._polls)):
            poll.set()

    # ── Direct email/password fallback ────────────────────────────────────────

    def sign_in_with_password(self, email: str, password: str) -> dict:
        """
        Sign in via Supabase email+password REST API.
        Returns session dict on success; raises AuthError on failure.
        """
        if not _supabase_url() or not _supabase_anon_key():
            raise AuthError("Supabase is not configured (check SUPABASE_URL / SUPABASE_ANON_KEY in .env).")

        try:
            resp = requests.post(
                f"{_supabase_url()}/auth/v1/token?grant_type=password",
                headers=self._anon_headers(),
                json={"email": email, "password": password},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise AuthError(f"Network error: {exc}") from exc

        log.debug("sign_in status=%s body=%s", resp.status_code, resp.text[:500])
        if resp.status_code == 200:
            data = resp.json()
            access_token = data.get("access_token")
            if not access_token:
                raise AuthError(f"No access_token in response: {resp.text[:300]}")
            user = data.get("user") or {}
            session = {
                "access_token": access_token,
                "refresh_token": data.get("refresh_token"),
                "expires_at": data.get("expires_at"),
                "user_id": user.get("id"),
                "user_email": user.get("email") or email,
            }
            self.save_session(session)
            return session

        try:
            err = resp.json()
            msg = (err.get("error_description")
                   or err.get("msg")
                   or err.get("message")
                   or err.get("error")
                   or resp.text)
        except Exception:
            msg = resp.text or f"HTTP {resp.status_code}"
        log.warning("sign_in failed: %s", msg)
        raise AuthError(msg)

    def sign_up_with_password(self, email: str, password: str) -> dict:
        """
        Register a new account via Supabase.
        Raises AuthError on failure (including when email confirmation is required).
        """
        if not _supabase_url() or not _supabase_anon_key():
            raise AuthError("Supabase is not configured (check SUPABASE_URL / SUPABASE_ANON_KEY in .env).")

        try:
            resp = requests.post(
                f"{_supabase_url()}/auth/v1/signup",
                headers=self._anon_headers(),
                json={"email": email, "password": password},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise AuthError(f"Network error: {exc}") from exc

        log.debug("sign_up status=%s body=%s", resp.status_code, resp.text[:500])
        if resp.status_code in (200, 201):
            data = resp.json()
            access_token = data.get("access_token")
            if not access_token:
                raise AuthError("Account created — check your email to confirm it, then sign in.")
            user = data.get("user") or {}
            session = {
                "access_token": access_token,
                "refresh_token": data.get("refresh_token"),
                "expires_at": data.get("expires_at"),
                "user_id": user.get("id"),
                "user_email": user.get("email") or email,
            }
            self.save_session(session)
            return session

        try:
            err = resp.json()
            msg = (err.get("error_description")
                   or err.get("msg")
                   or err.get("message")
                   or err.get("error")
                   or resp.text)
        except Exception:
            msg = resp.text or f"HTTP {resp.status_code}"
        log.warning("sign_up failed: %s", msg)
        raise AuthError(msg)

    # ── Authenticated API calls ────────────────────────────────────────────────

    def get_subscription_tier(self) -> str | None:
        if not self.is_authenticated():
            return None
        try:
            resp = requests.post(
                f"{_supabase_url()}/rest/v1/rpc/get_user_subscription",
                headers=self._authed_headers(),
                json={},
                timeout=8,
            )
            if resp.status_code == 200:
                rows = resp.json()
                if rows and isinstance(rows, list) and rows[0].get("tier"):
                    if rows[0].get("status") in ("active", "trialing"):
                        return rows[0]["tier"]
        except Exception as exc:
            log.warning("Could not fetch subscription tier: %s", exc)
        return None

    def get_subscription_info(self) -> dict | None:
        if not self.is_authenticated():
            return None
        try:
            resp = requests.post(
                f"{_supabase_url()}/rest/v1/rpc/get_user_subscription",
                headers=self._authed_headers(),
                json={},
                timeout=8,
            )
            if resp.status_code == 200:
                rows = resp.json()
                if rows and isinstance(rows, list):
                    return rows[0]
        except Exception as exc:
            log.warning("Could not fetch subscription info: %s", exc)
        return None
