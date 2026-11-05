"""
Zenvi billing client — thin Supabase RPC wrapper.

Pricing and point amounts live in Supabase (operation_pricing, llm_model_tiers).
Clients pass operation keys only, never raw point values.

The backend bills AI video and morph generation itself (/generation/video and
/generation/morph deduct after success), so the desktop must not charge those
keys a second time; check_operation remains for preflight UX.

Media index v2 is metered by the backend (/index/v2/* checks credits first and
charges from Gemini's real token usage after), so the desktop fires no charge
for it. The original indexing routes and stock downloads (Pexels / Freesound
files come straight from the CDN) are not billed by a backend route; the
desktop's charge is the only one for those, so they are not in
BACKEND_METERED_OPS.
"""

import json
import logging
import os
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from classes import info

log = logging.getLogger(__name__)

# Ops the backend already bills. Desktop charge_operation is a no-op for these.
# Add a key here only in the same release that makes a backend route charge it.
BACKEND_METERED_OPS = frozenset({
    "video_generation",
    "morph_generation",
})


# Last known balance, so the badge can paint instantly on the next launch.
CREDITS_FILE = os.path.join(info.USER_PATH, "zenvi_credits.json")


def _current_user_id() -> Optional[str]:
    """Signed-in user id from the stored session (no network)."""
    try:
        from classes.auth_manager import AuthManager
        auth = AuthManager.instance()
        session = auth._session or auth.load_session() or {}
        return session.get("user_id")
    except Exception:
        return None


def _write_stored(user_id: str, balance: int) -> None:
    """Replace CREDITS_FILE in one step with a file only this user can read.

    The new content is staged in a temp file beside it, so a crash mid-write
    leaves the previous balance rather than a truncated file.
    """
    folder = os.path.dirname(CREDITS_FILE)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".zenvi_credits.", suffix=".tmp", dir=folder)  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"user_id": user_id, "balance": balance}, fh)
        os.replace(tmp, CREDITS_FILE)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class CreditsClient:
    """Singleton billing client using AuthManager JWT."""

    def __init__(self) -> None:
        self._lock = threading.RLock()  # listeners run under it and may read back
        # Orders the file writes. Disk I/O happens under this lock, never under
        # _lock, so the GUI thread reading the cache cannot wait on a slow disk.
        self._persist_lock = threading.Lock()
        self._cached_balance: Optional[int] = None
        self._cached_user: Optional[str] = None
        self._cache_loaded = False
        self._listeners: List[Callable[[int], None]] = []

    def add_listener(self, callback: Callable[[int], None]) -> None:
        """Call *callback(balance)* (on any thread) whenever the balance changes."""
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[int], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    @staticmethod
    def _read_stored(user_id: Optional[str]) -> Optional[int]:
        """The last launch's balance for *user_id* (None for another account)."""
        if not user_id:
            return None
        try:
            with open(CREDITS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("user_id") == user_id:
                return int(data["balance"])
        except Exception:
            pass
        return None

    def _notify(self, total: int) -> None:
        """Tell the listeners; call with _lock held so they hear changes in order."""
        for callback in list(self._listeners):
            try:
                callback(total)
            except Exception as exc:
                log.debug("credits_client: listener failed: %s", exc)

    def _store_balance(self, total: int, user_id: Optional[str]) -> None:
        """Record a fresh balance for *user_id*: cache, notify on change, persist.

        Dropped when that account is no longer the signed-in one, so a fetch
        that outlives a sign-out never paints or saves the wrong account.
        """
        if not user_id or user_id != _current_user_id():
            return
        self.load_stored_balance()  # seed from disk so an unchanged value is not "new"
        # Check, cache and notify as one step so a sign-in landing mid-commit
        # can never interleave with the old account's result.
        with self._lock:
            if user_id != _current_user_id():
                return
            if self._cached_user == user_id and self._cached_balance == total:
                return
            self._cached_user, self._cached_balance = user_id, total
            self._notify(total)
        self._persist()

    def _persist(self) -> None:
        """Save the cached balance for the next launch (outside _lock).

        Writes what is cached when it runs, not what the caller saw, so when
        two commits race the file still ends on the newer one.
        """
        with self._persist_lock:
            with self._lock:
                user_id, total = self._cached_user, self._cached_balance
            if not user_id or total is None:
                return
            try:
                _write_stored(user_id, total)
            except Exception as exc:
                log.debug("credits_client: could not persist balance: %s", exc)

    def _get_auth(self):
        try:
            from classes.auth_manager import AuthManager, SUPABASE_URL
            auth = AuthManager.instance()
            if not auth.is_authenticated():
                return None, None, None
            return auth, auth._authed_headers(), SUPABASE_URL.rstrip("/")
        except Exception as exc:
            log.debug("credits_client: could not get auth: %s", exc)
            return None, None, None

    def _rpc(self, function_name: str, payload: dict, timeout: int = 8) -> Optional[Any]:
        auth, headers, url = self._get_auth()
        if auth is None:
            return None
        try:
            import requests
            resp = requests.post(
                f"{url}/rest/v1/rpc/{function_name}",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            log.warning(
                "credits_client: RPC %s returned %s: %s",
                function_name,
                resp.status_code,
                resp.text[:200],
            )
            return None
        except Exception as exc:
            log.warning("credits_client: RPC %s failed: %s", function_name, exc)
            return None

    def _rpc_then_refresh(self, function_name: str, payload: dict) -> None:
        """Run a spend/refund RPC, then refetch so the badge repaints right away."""
        result = self._rpc(function_name, payload)
        if function_name == "charge_operation":
            self._log_charge_result(str(payload.get("p_operation") or ""), result)
        self.balance()

    def _fire(self, function_name: str, payload: dict) -> None:
        threading.Thread(
            target=self._rpc_then_refresh,
            args=(function_name, payload),
            daemon=True,
            name=f"billing-{function_name}",
        ).start()

    def _row(self, result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, list) and result:
            return result[0] if isinstance(result[0], dict) else {}
        return {}

    def _log_charge_result(self, operation: str, result: Optional[Any]) -> None:
        """Surface charge failures (tier_limit / insufficient) instead of discarding them."""
        if result is None:
            log.warning(
                "credits_client: charge_operation failed for %s (no RPC result)",
                operation,
            )
            return
        if isinstance(result, str):
            # The live RPC returns a bare status: 'ok', or 'tier_limit' /
            # 'insufficient' / 'standard_mode' when nothing was deducted.
            status = result.strip().lower()
            if status != "ok":
                log.warning(
                    "credits_client: charge_operation %s returned %s",
                    operation,
                    status or "an empty status",
                )
            return
        row = self._row(result)
        status = str(
            row.get("status")
            or row.get("reason")
            or row.get("block_reason")
            or row.get("error")
            or ""
        ).lower()
        denied = (
            row.get("success") is False
            or row.get("ok") is False
            or row.get("charged") is False
            or row.get("allowed") is False
        )
        if any(
            token in status
            for token in ("tier_limit", "insufficient", "standard_mode", "denied", "failed")
        ):
            log.warning(
                "credits_client: charge_operation %s returned %s: %s",
                operation,
                status or "denied",
                row,
            )
        elif denied:
            log.warning(
                "credits_client: charge_operation %s denied: %s",
                operation,
                row,
            )

    def cached_balance(self) -> Optional[int]:
        """Signed-in account's last known balance, this launch or the previous one.

        Memory only, so the GUI thread can paint from it: None until
        load_stored_balance() or a fetch has filled it for this account.
        """
        user_id = _current_user_id()
        with self._lock:
            if self._cache_loaded and self._cached_user == user_id:
                return self._cached_balance
        return None

    def load_stored_balance(self) -> Optional[int]:
        """Fill the cache from the previous launch (reads disk: not on the GUI thread).

        Reads CREDITS_FILE once per account and tells the listeners what it
        found, so a load that lands after the chat page is ready still paints.
        """
        user_id = _current_user_id()
        with self._lock:
            if self._cache_loaded and self._cached_user == user_id:
                return self._cached_balance
        stored = self._read_stored(user_id)
        with self._lock:
            if user_id != _current_user_id():
                return None  # signed in as someone else while reading
            if not self._cache_loaded or self._cached_user != user_id:
                self._cache_loaded = True
                self._cached_user, self._cached_balance = user_id, stored
                if stored is not None:
                    self._notify(stored)
            return self._cached_balance

    def balance(self) -> Tuple[bool, int]:
        """Return (authenticated, total_points). Fail closed balance 0 when authed but RPC fails.

        Listeners hear the stored balance first, so the badge keeps the last
        known number while a token refresh and the RPC are in flight.
        """
        self.load_stored_balance()
        auth, _, _ = self._get_auth()
        if auth is None:
            return False, 0
        user_id = _current_user_id()
        result = self._rpc("get_credits_balance", {}, timeout=5)
        if result is None:
            cached = self.cached_balance()
            return True, cached if cached is not None else 0
        row = self._row(result)
        total = int(row.get("total_points", 0))
        self._store_balance(total, user_id)
        return True, total

    def refresh_balance(self) -> None:
        """Refetch the balance in the background; the listeners repaint the badge.

        For requests the backend bills itself (BACKEND_METERED_OPS): the
        desktop fires no charge for those, so nothing else would refetch
        before the 60 s refresh.
        """
        threading.Thread(target=self.balance, daemon=True, name="billing-refresh").start()

    def check(self, points_needed: int = 0) -> Tuple[bool, int]:
        """Legacy balance check by raw points (prefer check_operation)."""
        authed, total = self.balance()
        if not authed:
            return True, 0
        if points_needed <= 0:
            return True, total
        result = self._rpc(
            "check_credits_allowed",
            {"p_estimated_credits": points_needed},
            timeout=5,
        )
        if result is None:
            return False, total
        row = self._row(result)
        allowed = bool(row.get("allowed", False))
        return allowed, int(row.get("balance", total))

    def charge_operation(
        self,
        operation: str,
        units: int = 1,
        duration_seconds: Optional[float] = None,
        provider: Optional[str] = None,
        session_id: Optional[str] = None,
        note: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        if operation in BACKEND_METERED_OPS:
            log.debug("skipped desktop charge — backend meters %s", operation)
            return
        payload: Dict[str, Any] = {
            "p_operation": operation,
            "p_units": units,
            "p_provider": provider,
            "p_session_id": session_id,
            "p_note": note,
            "p_idempotency_key": idempotency_key,
        }
        if duration_seconds is not None:
            payload["p_duration_seconds"] = float(duration_seconds)
        self._fire("charge_operation", payload)

    def refund(
        self,
        points: int,
        operation: str,
        note: Optional[str] = None,
    ) -> None:
        if points <= 0:
            return
        self._fire(
            "refund_points",
            {
                "p_points": points,
                "p_operation": operation,
                "p_original_txn": None,
                "p_note": note or "Operation failed — refund",
            },
        )

    def award_bonus(self, event_type: str, event_key: Optional[str] = None) -> None:
        self._fire(
            "award_bonus",
            {"p_event_type": event_type, "p_event_key": event_key},
        )

    def get_mode(self) -> str:
        auth, _, _ = self._get_auth()
        if auth is None:
            return "premium"
        result = self._rpc("get_credits_balance", {}, timeout=5)
        if result is None:
            return "premium"
        row = self._row(result)
        return "standard" if bool(row.get("in_standard_mode", False)) else "premium"


credits = CreditsClient()


def credit_block_message(points_needed: int, label: str, balance: int) -> str:
    return (
        f"Insufficient credits ({balance} remaining, need {points_needed} for {label}). "
        "Enable pay-as-you-go in Account → Credits, or wait for your next billing cycle."
    )


def _check_operation_rpc(
    operation: str,
    units: int = 1,
    duration_seconds: Optional[float] = None,
) -> Tuple[bool, int, int, Optional[str]]:
    payload: Dict[str, Any] = {
        "p_operation": operation,
        "p_units": units,
    }
    if duration_seconds is not None:
        payload["p_duration_seconds"] = float(duration_seconds)
    user_id = _current_user_id()
    result = credits._rpc("check_operation_allowed", payload, timeout=5)
    auth, _, _ = credits._get_auth()
    if auth is None:
        return True, 0, 0, None
    if result is None:
        return False, 0, 0, (
            f"Could not verify credits for {operation}. Check your connection and try again."
        )
    row = credits._row(result)
    allowed = bool(row.get("allowed", False))
    balance = int(row.get("balance", 0))
    if "balance" in row:
        credits._store_balance(balance, user_id)
    required = int(row.get("required", 0))
    block_reason = row.get("block_reason")
    if block_reason:
        return False, balance, required, str(block_reason)
    if not allowed:
        return False, balance, required, credit_block_message(required, operation, balance)
    return True, balance, required, None


def check_operation(
    operation: str,
    label: str,
    units: int = 1,
    duration_seconds: Optional[float] = None,
) -> Tuple[bool, int, Optional[str]]:
    allowed, balance, _, err = _check_operation_rpc(operation, units, duration_seconds)
    return allowed, balance, err


def charge_operation_on_success(
    success: bool,
    operation: str,
    operation_name: Optional[str] = None,
    provider: Optional[str] = None,
    note: Optional[str] = None,
    units: int = 1,
    duration_seconds: Optional[float] = None,
    idempotency_key: Optional[str] = None,
) -> None:
    if not success:
        return
    if operation in BACKEND_METERED_OPS:
        log.debug("skipped desktop charge — backend meters %s", operation)
        return
    credits.charge_operation(
        operation,
        units=units,
        duration_seconds=duration_seconds,
        provider=provider,
        note=note,
        idempotency_key=idempotency_key,
    )
