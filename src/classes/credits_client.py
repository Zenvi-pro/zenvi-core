"""
Zenvi billing client — thin Supabase RPC wrapper.

Pricing and point amounts live in Supabase (operation_pricing, llm_model_tiers).
Clients pass operation keys only, never raw point values.

Backend already meters video/morph/indexing/stock (and related AI routes).
Desktop must not double-charge those keys; check_operation remains for preflight UX.
"""

import logging
import threading
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Ops the backend already bills. Desktop charge_operation is a no-op for these.
BACKEND_METERED_OPS = frozenset({
    "video_generation",
    "morph_generation",
    "indexing_per_minute",
    "stock_add",
})


class CreditsClient:
    """Singleton billing client using AuthManager JWT."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached_balance: Optional[int] = None

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
        if any(token in status for token in ("tier_limit", "insufficient", "denied", "failed")):
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

    def _fire(self, function_name: str, payload: dict) -> None:
        def _run() -> None:
            result = self._rpc(function_name, payload)
            if function_name == "charge_operation":
                self._log_charge_result(str(payload.get("p_operation") or ""), result)

        threading.Thread(
            target=_run,
            daemon=True,
            name=f"billing-{function_name}",
        ).start()

    def cached_balance(self) -> Optional[int]:
        """Last known balance from a successful fetch (None if never loaded)."""
        with self._lock:
            return self._cached_balance

    def balance(self) -> Tuple[bool, int]:
        """Return (authenticated, total_points). Fail closed balance 0 when authed but RPC fails."""
        auth, _, _ = self._get_auth()
        if auth is None:
            return False, 0
        result = self._rpc("get_credits_balance", {}, timeout=5)
        if result is None:
            with self._lock:
                if self._cached_balance is not None:
                    return True, self._cached_balance
            return True, 0
        row = self._row(result)
        total = int(row.get("total_points", 0))
        with self._lock:
            self._cached_balance = total
        return True, total

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

    def charge_operation_sync(
        self,
        operation: str,
        units: int = 1,
        duration_seconds: Optional[float] = None,
        provider: Optional[str] = None,
        session_id: Optional[str] = None,
        note: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Synchronous charge; returns RPC row (or None). Skips backend-metered ops."""
        if operation in BACKEND_METERED_OPS:
            log.debug("skipped desktop charge — backend meters %s", operation)
            return None
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
        result = self._rpc("charge_operation", payload)
        self._log_charge_result(operation, result)
        if result is None:
            return None
        return self._row(result)

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
