"""Upload a local chunk file to a Gemini Files resumable upload URL."""

from __future__ import annotations

import email.utils
import json
import os
import random
import time
from typing import Any, Dict, Optional, Tuple

import requests

from classes.logger import log

# Same policy as the backend's Gemini calls (zenvi-backend gemini_retry):
# overload, outage and quota are retried; any other 4xx fails the same way
# every time. Bounded by attempts and a total time budget.
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_ATTEMPTS = 5
_BUDGET_SEC = 300.0
_BASE_DELAY = 2.0
_MAX_DELAY = 60.0
_QUERY_TIMEOUT = 30

# Module-level so tests can swap in a fake clock.
_clock = time.monotonic
_sleep = time.sleep
_rng = random.Random()


def _retry_after(resp: Any) -> Optional[float]:
    value = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _file_info(resp: Any, mime_type: str) -> Tuple[Dict[str, Any], str]:
    payload: Dict[str, Any] = {}
    try:
        payload = resp.json() if resp.content else {}
    except Exception:
        try:
            payload = json.loads(resp.text or "{}")
        except Exception:
            payload = {}

    # Response shapes: {"file": {...}} or flat file object
    file_obj = payload.get("file") if isinstance(payload.get("file"), dict) else payload
    if not isinstance(file_obj, dict):
        file_obj = {}
    name = str(file_obj.get("name") or "").strip()
    uri = str(file_obj.get("uri") or "").strip()
    if not name and not uri:
        return {}, f"Gemini upload finalize missing file name/uri: {resp.text[:300]}"
    return {
        "name": name,
        "uri": uri,
        "mime_type": str(file_obj.get("mimeType") or file_obj.get("mime_type") or mime_type),
        "raw": file_obj,
    }, ""


def _query(url: str) -> Tuple[Optional[str], int, Any]:
    """Ask Gemini how far a resumable upload got: (status, bytes received, response).

    status is "active", "final" (the finalize landed; its file is in the body)
    or another terminal state; None when the question itself failed.
    """
    try:
        resp = requests.post(
            url,
            headers={"X-Goog-Upload-Command": "query", "Content-Length": "0"},
            timeout=_QUERY_TIMEOUT,
        )
    except requests.RequestException:
        return None, 0, None
    status = str(resp.headers.get("X-Goog-Upload-Status") or "").strip().lower()
    if resp.status_code >= 400 or not status:
        return (None if resp.status_code in _RETRY_STATUS else "gone"), 0, resp
    try:
        received = int(resp.headers.get("X-Goog-Upload-Size-Received") or 0)
    except ValueError:
        received = 0
    return status, received, resp


def upload_file_to_gemini_resumable(
    file_path: str,
    upload_url: str,
    *,
    mime_type: str = "video/mp4",
    timeout: int = 600,
) -> Tuple[Dict[str, Any], str]:
    """PUT/finalize bytes to a Gemini resumable upload URL.

    A 429 / 5xx / timeout / dropped connection is retried with backoff (at
    least as long as Retry-After). Before each retry Gemini is asked how far
    the upload got, because sending a finished upload again is a 400
    ("Upload has already been terminated"): a finalize whose answer was lost
    is picked up from the query, and a partial one resumes at its offset.

    Returns ({name, uri, mime_type, ...}, error).
    """
    if not file_path or not os.path.isfile(file_path):
        return {}, f"File not found: {file_path}"
    url = (upload_url or "").strip()
    if not url:
        return {}, "upload_url is required"

    size = os.path.getsize(file_path)
    mime = mime_type or "video/mp4"
    started = _clock()
    offset = 0
    error = ""
    attempt = 0
    try:
        while True:
            attempt += 1
            wait_hint: Optional[float] = None
            send = True
            if attempt > 1:
                state, received, qresp = _query(url)
                if state == "final":
                    return _file_info(qresp, mime)
                if state == "active":
                    offset = min(max(0, received), size)
                elif state is None:
                    send = False  # ask again after the next wait
                    error = error or "Gemini upload status unavailable"
                else:
                    return {}, f"{error} (the upload session is no longer usable)".strip()
            if send:
                try:
                    with open(file_path, "rb") as fh:
                        fh.seek(offset)
                        resp = requests.post(
                            url,
                            data=fh,
                            headers={
                                "Content-Length": str(size - offset),
                                "X-Goog-Upload-Offset": str(offset),
                                "X-Goog-Upload-Command": "upload, finalize",
                                "Content-Type": mime,
                            },
                            timeout=timeout,
                        )
                except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
                    error = f"Gemini upload error: {exc}"
                else:
                    if resp.status_code < 400:
                        return _file_info(resp, mime)
                    error = f"Gemini upload failed ({resp.status_code}): {resp.text[:300]}"
                    if resp.status_code not in _RETRY_STATUS:
                        return {}, error
                    wait_hint = _retry_after(resp)

            cap = min(_MAX_DELAY, _BASE_DELAY * (2 ** (attempt - 1)))
            delay = cap / 2 + _rng.uniform(0, cap / 2)
            if wait_hint is not None:
                delay = max(delay, wait_hint + _rng.uniform(0, 1.0))
            elapsed = _clock() - started
            if attempt >= _ATTEMPTS or elapsed + delay > _BUDGET_SEC:
                return {}, f"{error} (gave up after {attempt} attempts in {elapsed:.0f}s)"
            log.warning(
                "Gemini chunk upload: %s; retry %d/%d in %.1fs",
                error, attempt + 1, _ATTEMPTS, delay,
            )
            _sleep(delay)
    except Exception as exc:
        return {}, f"Gemini upload error: {exc}"
