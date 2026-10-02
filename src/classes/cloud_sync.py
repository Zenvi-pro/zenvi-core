"""
 @file
 @brief Zenvi Cloud sync: push a desktop project and its media, pull one back (no Qt).

 Everything here runs on a worker thread (see windows/cloud_sync_ui.py): the REST
 client for Zenvi Cloud, full-file SHA-256 hashing with a cache, uploads to Azure
 SAS URLs, and the push / pull flows. Nothing in this module touches Qt or the
 project store; results come back as plain data for the GUI thread to apply.

 Contract (zenvi-web, read as the source of truth):
   - REST /v1: packages/cloud/src/routes/v1.ts (projects with If-Match
     revisions, /media/check, /media declare -> SAS upload -> /complete).
   - Media refs on project files: packages/engine/src/model/types.ts
     ``CloudMediaRef`` = ``file["cloud"] = {sha256, size, content_type, name}``.
     The desktop adds ``mtime`` so a later push can reuse the hash.

 Media is content-addressed by the FULL-file SHA-256. ``file["fingerprint"]``
 (classes/media_fingerprint.py) hashes only the size plus the first and last
 1 MB, so it is never used as a cloud key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence
from urllib.parse import quote, urlsplit

import requests
from requests.auth import AuthBase

log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_CLOUD_URL = "https://zenvi-cloud.whitesky-da3c8402.eastus.azurecontainerapps.io"
DEFAULT_WEBSITE = "https://zenvi.pro"
PROJECT_EXT = ".zvn"

#: One Put Blob up to this size; Put Block + Put Block List above it.
SINGLE_PUT_MAX = 64 * 1024 * 1024
BLOCK_SIZE = 8 * 1024 * 1024
#: Azure allows at most 50,000 committed blocks per blob.
MAX_BLOCKS = 50_000
HASH_CHUNK = 8 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024
#: POST /v1/media/check takes at most 1000 hashes per call.
CHECK_BATCH = 1000
#: The cloud refuses project JSON larger than this (packages/cloud/src/config.ts).
MAX_PROJECT_BYTES = 20 * 1024 * 1024
UPLOAD_ATTEMPTS = 4
API_RETRIES = 2
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
#: Seconds of slack when comparing a cached mtime with the file's current one.
MTIME_TOLERANCE = 1e-3
API_TIMEOUT = (10.0, 60.0)
TRANSFER_TIMEOUT = (15.0, 120.0)
#: Sent when the local link has no revision: it never matches, so the cloud
#: answers 409 and the person decides instead of a silent overwrite.
UNKNOWN_REVISION = "zenvi-desktop-unknown-revision"

_PROJECT_ID = re.compile(r"^c_[a-z0-9]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_base_url(url: str) -> str:
    """``https://host`` without a trailing slash (adds https:// when no scheme is given)."""
    value = (url or "").strip().rstrip("/")
    if value and not re.match(r"^https?://", value, re.IGNORECASE):
        value = "https://" + value
    return value


def resolve_cloud_url(setting: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """Cloud base URL: env ``ZENVI_CLOUD_URL``, then the ``zenvi-cloud-url`` setting, then the default."""
    env = os.environ if environ is None else environ
    for candidate in (env.get("ZENVI_CLOUD_URL", ""), setting or ""):
        value = normalize_base_url(str(candidate))
        if value:
            return value
    return DEFAULT_CLOUD_URL


def resolve_website(environ: Optional[Mapping[str, str]] = None) -> str:
    """Website base (``ZENVI_WEBSITE``, as classes/auth_manager.py uses for sign-in)."""
    env = os.environ if environ is None else environ
    return normalize_base_url(env.get("ZENVI_WEBSITE", "")) or DEFAULT_WEBSITE


def editor_url(website: str, project_id: str) -> str:
    """Web editor link for a cloud project: ``<website>/editor/p/<project_id>``."""
    base = normalize_base_url(website) or DEFAULT_WEBSITE
    return f"{base}/editor/p/{quote(project_id, safe='')}"


def host_of(url: str) -> str:
    return urlsplit(url).netloc or url


def is_project_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_PROJECT_ID.match(value))


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.match(value.lower()))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_bytes(count: Optional[int]) -> str:
    value = float(count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ── Errors ────────────────────────────────────────────────────────────────────

class CloudError(Exception):
    """A Zenvi Cloud operation failed. ``message`` is written for the person using Zenvi."""

    def __init__(self, message: str, *, status: int = 0, code: str = "", body: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.body = body


class CloudAuthError(CloudError):
    """Not signed in, or Zenvi Cloud rejected the session."""


class CloudOfflineError(CloudError):
    """Zenvi Cloud (or its storage) could not be reached."""


class CloudNotFoundError(CloudError):
    """The project or media does not exist in Zenvi Cloud."""


class CloudCancelled(CloudError):
    """The person cancelled; nothing to report."""


class CloudConflictError(CloudError):
    """The cloud copy changed since the revision we sent (HTTP 409)."""

    def __init__(self, message: str, *, revision: Optional[str] = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.revision = revision


class CloudQuotaError(CloudError):
    """The upload would exceed the account's Zenvi Cloud storage."""

    def __init__(self, message: str, *, used_bytes: Optional[int] = None,
                 quota_bytes: Optional[int] = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.used_bytes = used_bytes
        self.quota_bytes = quota_bytes


class _LocalFileChanged(CloudError):
    """A media file changed size while it was being uploaded."""


# ── Cancellation and progress ─────────────────────────────────────────────────

class CancelToken:
    """Set from the GUI thread, checked by the worker between chunks and requests."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise CloudCancelled("Cancelled.")

    def sleep(self, seconds: float) -> None:
        """Wait, waking (and raising) as soon as the operation is cancelled."""
        if self._event.wait(max(0.0, seconds)):
            raise CloudCancelled("Cancelled.")


@dataclass(frozen=True)
class Progress:
    """One progress report. ``phase`` is one of: prepare, hash, check, upload,
    project, list, fetch, verify, download, write. ``done``/``total`` are bytes
    (0/0 when the phase has no measurable size)."""

    phase: str
    done: int = 0
    total: int = 0
    detail: str = ""


ProgressFn = Callable[[Progress], None]


def _no_progress(_progress: Progress) -> None:
    return None


class ThrottledProgress:
    """Forward at most one report per ``interval`` seconds, but always pass
    phase or file changes and completions (so the GUI event queue never floods)."""

    def __init__(self, sink: ProgressFn, interval: float = 0.1,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sink = sink
        self._interval = interval
        self._clock = clock
        self._last_key: Optional[tuple[str, str]] = None
        self._last_time = 0.0

    def __call__(self, progress: Progress) -> None:
        now = self._clock()
        key = (progress.phase, progress.detail)
        finished = progress.total > 0 and progress.done >= progress.total
        if key != self._last_key or finished or now - self._last_time >= self._interval:
            self._last_key = key
            self._last_time = now
            self._sink(progress)


# ── Hashing ───────────────────────────────────────────────────────────────────

def sha256_file(path: str, *, cancel: Optional[CancelToken] = None,
                on_bytes: Optional[Callable[[int], None]] = None,
                chunk_size: int = HASH_CHUNK) -> str:
    """Full-file SHA-256 (lowercase hex), streamed in ``chunk_size`` reads."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            if cancel is not None:
                cancel.check()
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return digest.hexdigest()


def cached_sha256(ref: Any, size: int, mtime: float) -> Optional[str]:
    """The hash in a ``file["cloud"]`` ref, when its size AND mtime still match the file."""
    if not isinstance(ref, dict):
        return None
    sha = ref.get("sha256")
    if not is_sha256(sha):
        return None
    ref_size = ref.get("size")
    if isinstance(ref_size, bool) or not isinstance(ref_size, int) or ref_size != size:
        return None
    ref_mtime = ref.get("mtime")
    if isinstance(ref_mtime, bool) or not isinstance(ref_mtime, (int, float)):
        return None
    if abs(float(ref_mtime) - float(mtime)) > MTIME_TOLERANCE:
        return None
    return str(sha).lower()


# ── Content types ─────────────────────────────────────────────────────────────

_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
    ".qt": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".avi": "video/x-msvideo", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".ts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv", ".3gp": "video/3gpp",
    ".ogv": "video/ogg", ".mxf": "application/mxf", ".dv": "video/dv",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/opus", ".aif": "audio/aiff",
    ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif",
    ".tga": "image/x-tga", ".exr": "image/x-exr", ".psd": "image/vnd.adobe.photoshop",
    ".srt": "application/x-subrip", ".vtt": "text/vtt",
}

#: Non-media types the cloud also stores (services/media.ts EXTRA_TYPES).
_CLOUD_EXTRA_TYPES = frozenset({
    "application/ogg", "application/mp4", "application/mxf",
    "application/x-subrip", "text/vtt", "application/octet-stream",
})
_CLOUD_MEDIA_TYPE = re.compile(r"^(video|audio|image)/[a-z0-9.+-]{1,100}$")


def cloud_accepts(content_type: str) -> bool:
    return bool(_CLOUD_MEDIA_TYPE.match(content_type)) or content_type in _CLOUD_EXTRA_TYPES


def content_type_for(path: str) -> str:
    """A content type Zenvi Cloud accepts for this file (octet-stream when unknown)."""
    ext = os.path.splitext(path)[1].lower()
    guess = _CONTENT_TYPES.get(ext) or mimetypes.guess_type(path)[0] or ""
    guess = guess.split(";")[0].strip().lower()
    return guess if guess and cloud_accepts(guess) else "application/octet-stream"


# ── REST client ───────────────────────────────────────────────────────────────

class TokenSource(Protocol):
    """Where the Supabase access token comes from (the desktop sign-in)."""

    def access_token(self) -> Optional[str]:
        """A valid token (refreshing an expired one), or None when signed out."""
        ...

    def refresh(self) -> Optional[str]:
        """Force a refresh after the cloud said 401; None when that is impossible."""
        ...


class StaticTokenSource:
    """A fixed token (tests, scripts)."""

    def __init__(self, token: Optional[str], refreshed: Optional[str] = None) -> None:
        self._token = token
        self._refreshed = refreshed

    def access_token(self) -> Optional[str]:
        return self._token

    def refresh(self) -> Optional[str]:
        if self._refreshed is not None:
            self._token = self._refreshed
        return self._refreshed


class _HeadersOnlyAuth(AuthBase):
    """Keep ``requests`` from reading ~/.netrc: a matching entry (or a ``default``
    one) would replace our Bearer header on API calls and add a password to the
    Azure storage calls. Proxies from the environment still apply."""

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        return request


def new_session() -> requests.Session:
    session = requests.Session()
    session.auth = _HeadersOnlyAuth()
    return session


def normalize_revision(value: Any) -> Optional[str]:
    """Bare revision from an ETag / If-Match style value (drops ``W/`` and quotes)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.startswith("W/"):
        text = text[2:]
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    return text or None


def _retry_after(response: requests.Response, fallback: float) -> float:
    """Seconds to wait before retrying: the server's Retry-After (capped), else ``fallback``."""
    value = response.headers.get("Retry-After", "")
    try:
        return min(max(float(value), 0.0), 30.0) if value else fallback
    except ValueError:
        return fallback


def _json_or_none(response: requests.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def error_from_response(status: int, body: Any, base_url: str) -> CloudError:
    """Turn a failed /v1 response into the CloudError the UI knows how to explain."""
    api_error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(api_error, dict):
        if status == 401:
            return CloudAuthError("Your Zenvi session has expired. Sign in again.", status=status, code="unauthorized")
        return CloudError(
            f"Zenvi Cloud at {host_of(base_url)} answered HTTP {status} instead of an API response. "
            "Check the Zenvi Cloud URL in Preferences, or try again later.",
            status=status, code="bad_response", body=body)
    code = str(api_error.get("code") or "")
    message = str(api_error.get("message") or "").strip()
    assert isinstance(body, dict)
    if status == 401:
        return CloudAuthError(message or "Your Zenvi session has expired. Sign in again.",
                              status=status, code=code or "unauthorized", body=body)
    if status == 409:
        return CloudConflictError(message or "The project changed in Zenvi Cloud.",
                                  revision=normalize_revision(body.get("revision")),
                                  status=status, code=code or "conflict", body=body)
    if status == 413 and code == "quota_exceeded":
        used = body.get("usedBytes")
        quota = body.get("quotaBytes")
        return CloudQuotaError(message or "Your Zenvi Cloud storage is full.",
                               used_bytes=used if isinstance(used, int) else None,
                               quota_bytes=quota if isinstance(quota, int) else None,
                               status=status, code=code, body=body)
    if status == 404:
        return CloudNotFoundError(message or "Not found in Zenvi Cloud.", status=status, code=code, body=body)
    if status >= 500:
        return CloudError(message or f"Zenvi Cloud had a problem (HTTP {status}). Try again in a moment.",
                          status=status, code=code, body=body)
    return CloudError(message or f"Zenvi Cloud refused the request (HTTP {status}).",
                      status=status, code=code, body=body)


@dataclass
class ApiResponse:
    status: int
    body: Any
    headers: Mapping[str, str]

    def revision(self) -> Optional[str]:
        """``revision`` from the body, else the ETag header."""
        if isinstance(self.body, dict):
            found = normalize_revision(self.body.get("revision"))
            if found:
                return found
        return normalize_revision(self.headers.get("ETag"))


class CloudClient:
    """Zenvi Cloud REST /v1 over one ``requests.Session``.

    Every call sends the Supabase access token. A 401 triggers one forced
    refresh and a retry. Connection failures and 502/503/504 are retried a
    couple of times for idempotent calls. SAS uploads and downloads use
    :attr:`session` directly, never with the Authorization header.
    """

    def __init__(self, base_url: str, tokens: TokenSource, *,
                 session: Optional[requests.Session] = None,
                 cancel: Optional[CancelToken] = None,
                 retry_delay: float = 1.0,
                 user_agent: str = "Zenvi-Desktop") -> None:
        self.base_url = normalize_base_url(base_url)
        self._tokens = tokens
        self.session = session or new_session()
        self.cancel = cancel or CancelToken()
        self.retry_delay = retry_delay
        self.user_agent = user_agent

    # -- plumbing

    def ensure_signed_in(self) -> None:
        """Fail fast (before hashing gigabytes) when there is no session at all."""
        self._token()

    def _token(self, *, refresh: bool = False) -> str:
        token = self._tokens.refresh() if refresh else self._tokens.access_token()
        if not token:
            raise CloudAuthError("Sign in to your Zenvi account to use Zenvi Cloud.",
                                 status=401, code="not_signed_in")
        return token

    def request(self, method: str, path: str, *, json_body: Any = None,
                data: Optional[bytes] = None, headers: Optional[Mapping[str, str]] = None,
                expect: Sequence[int] = (200,), retry: bool = True) -> ApiResponse:
        url = f"{self.base_url}{path}"
        token = self._token()
        refreshed = False
        attempt = 0
        while True:
            self.cancel.check()
            send_headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": self.user_agent,
            }
            if data is not None:
                send_headers["Content-Type"] = "application/json"
            if headers:
                send_headers.update(headers)
            try:
                response = self.session.request(
                    method, url, json=json_body, data=data, headers=send_headers, timeout=API_TIMEOUT)
            except requests.RequestException as exc:
                if retry and attempt < API_RETRIES:
                    attempt += 1
                    self.cancel.sleep(self.retry_delay * attempt)
                    continue
                raise CloudOfflineError(
                    f"Couldn't reach Zenvi Cloud ({host_of(self.base_url)}). "
                    "Check your internet connection and try again.", code="offline") from exc
            if response.status_code == 401 and not refreshed:
                refreshed = True
                token = self._token(refresh=True)
                continue
            if retry and response.status_code in (429, 502, 503, 504) and attempt < API_RETRIES:
                attempt += 1
                self.cancel.sleep(_retry_after(response, self.retry_delay * attempt))
                continue
            break
        body = _json_or_none(response)
        if response.status_code in expect:
            return ApiResponse(response.status_code, body, response.headers)
        raise error_from_response(response.status_code, body, self.base_url)

    # -- account and projects

    def me(self) -> dict[str, Any]:
        body = self.request("GET", "/v1/me").body
        return body if isinstance(body, dict) else {}

    def list_projects(self) -> list[dict[str, Any]]:
        body = self.request("GET", "/v1/projects").body
        items = body.get("projects") if isinstance(body, dict) else body
        if not isinstance(items, list):
            return []
        return [p for p in items if isinstance(p, dict) and isinstance(p.get("id"), str)]

    def get_project(self, project_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/v1/projects/{quote(project_id, safe='')}")
        body = response.body if isinstance(response.body, dict) else {}
        if not isinstance(body.get("project"), dict):
            raise CloudError("Zenvi Cloud returned a project without its data.", code="bad_response")
        body = dict(body)
        body["revision"] = response.revision()
        return body

    def create_project(self, name: str, project_json: bytes) -> ApiResponse:
        # Not retried: a lost response could otherwise create the project twice.
        data = b'{"name":' + json.dumps(name).encode("utf-8") + b',"project":' + project_json + b"}"
        return self.request("POST", "/v1/projects", data=data, expect=(200, 201), retry=False)

    def save_project(self, project_id: str, project_json: bytes, if_match: str) -> ApiResponse:
        match = if_match if if_match == "*" else f'"{if_match}"'
        return self.request("PUT", f"/v1/projects/{quote(project_id, safe='')}",
                            data=project_json, headers={"If-Match": match})

    # -- media

    def check_media(self, hashes: Iterable[str]) -> set[str]:
        """The subset of ``hashes`` Zenvi Cloud already stores."""
        unique = sorted({h.lower() for h in hashes})
        existing: set[str] = set()
        for start in range(0, len(unique), CHECK_BATCH):
            batch = unique[start:start + CHECK_BATCH]
            body = self.request("POST", "/v1/media/check", json_body={"sha256": batch}).body
            found = body.get("existing") if isinstance(body, dict) else None
            if isinstance(found, list):
                existing.update(h.lower() for h in found if isinstance(h, str))
        return existing

    def declare_media(self, sha256: str, size: int, name: str, content_type: str) -> dict[str, Any]:
        body = self.request("POST", "/v1/media", json_body={
            "sha256": sha256, "size": size, "name": name, "contentType": content_type,
        }, expect=(200, 201)).body
        return body if isinstance(body, dict) else {}

    def complete_media(self, sha256: str) -> dict[str, Any]:
        body = self.request("POST", f"/v1/media/{sha256}/complete").body
        return body if isinstance(body, dict) else {}

    def media_url(self, sha256: str) -> str:
        body = self.request("GET", f"/v1/media/{sha256}/url").body
        url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(url, str) or not url:
            raise CloudError("Zenvi Cloud returned no download link.", code="bad_response")
        return url


# ── Uploads to Azure SAS URLs ─────────────────────────────────────────────────

class _FileWindow:
    """Bytes ``[offset, offset + length)`` of a file as a streaming request body.

    ``requests`` sets Content-Length from ``__len__`` and urllib3 calls
    :meth:`read` in small chunks, so progress and cancellation are checked
    while the bytes go out (Azure needs Content-Length; no chunked encoding).
    """

    def __init__(self, path: str, offset: int, length: int, cancel: CancelToken,
                 on_read: Callable[[int], None]) -> None:
        self._fh = open(path, "rb")
        self._fh.seek(offset)
        self._length = length
        self._left = length
        self._cancel = cancel
        self._on_read = on_read

    def __len__(self) -> int:
        return self._length

    def read(self, size: int = -1) -> bytes:
        self._cancel.check()
        if self._left <= 0:
            return b""
        if size is None or size < 0 or size > self._left:
            size = self._left
        chunk = self._fh.read(min(size, 1024 * 1024))
        if not chunk:
            raise _LocalFileChanged("A media file got shorter while it was being uploaded.",
                                    code="file_changed")
        self._left -= len(chunk)
        self._on_read(len(chunk))
        return chunk

    def close(self) -> None:
        self._fh.close()


class SasTarget:
    """Where one media blob goes: a SAS URL plus the headers the PUT must carry.

    ``renew`` asks the cloud for a fresh URL (a SAS lives one hour); it returns
    None when the blob already exists by then (someone else finished it).
    """

    def __init__(self, url: str, headers: Optional[Mapping[str, str]] = None,
                 renew: Optional[Callable[[], Optional[tuple[str, dict[str, str]]]]] = None) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self._renew = renew

    def renew(self) -> bool:
        if self._renew is None:
            return False
        fresh = self._renew()
        if fresh is None:
            return False
        self.url, self.headers = fresh[0], dict(fresh[1])
        return True


class _AlreadyStored(Exception):
    """The blob appeared in the cloud while we were uploading it."""


def _azure_error(response: requests.Response) -> str:
    code = response.headers.get("x-ms-error-code")
    return f"HTTP {response.status_code}" + (f" ({code})" if code else "")


def block_id(index: int) -> str:
    """Azure block ids must all have the same length within a blob."""
    return base64.b64encode(f"{index:06d}".encode("ascii")).decode("ascii")


def block_list_xml(ids: Sequence[str]) -> bytes:
    latest = "".join(f"<Latest>{value}</Latest>" for value in ids)
    return f'<?xml version="1.0" encoding="utf-8"?><BlockList>{latest}</BlockList>'.encode("utf-8")


def _block_size_for(size: int, block_size: int) -> int:
    needed = math.ceil(size / MAX_BLOCKS)
    if needed <= block_size:
        return block_size
    mib = 1024 * 1024
    return int(math.ceil(needed / mib) * mib)


class _Uploader:
    """Put Blob / Put Block / Put Block List against one SAS target, with retries."""

    def __init__(self, session: requests.Session, target: SasTarget, cancel: CancelToken, *,
                 attempts: int = UPLOAD_ATTEMPTS, retry_delay: float = 1.0) -> None:
        self.session = session
        self.target = target
        self.cancel = cancel
        self.attempts = max(1, attempts)
        self.retry_delay = retry_delay
        self._renewed = False

    def send(self, what: str, make_request: Callable[[], requests.Response]) -> requests.Response:
        delay = self.retry_delay
        problem = ""
        for attempt in range(1, self.attempts + 1):
            self.cancel.check()
            try:
                response = make_request()
            except requests.RequestException as exc:
                problem = f"network error ({exc.__class__.__name__})"
            else:
                if 200 <= response.status_code < 300:
                    self._renewed = False   # a later expiry (multi-hour uploads) may renew again
                    return response
                problem = _azure_error(response)
                if response.status_code == 403 and not self._renewed:
                    # Most likely the one-hour SAS expired: get a fresh one and go again.
                    self._renewed = True
                    if not self.target.renew():
                        raise _AlreadyStored()
                    continue
                if response.status_code not in RETRYABLE_STATUS:
                    break
            if attempt < self.attempts:
                self.cancel.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise CloudError(f"Uploading {what} to Zenvi Cloud failed: {problem}.", code="upload_failed")


def upload_to_sas(session: requests.Session, target: SasTarget, path: str, size: int,
                  content_type: str, *, cancel: CancelToken,
                  on_progress: Callable[[int], None],
                  single_put_max: int = SINGLE_PUT_MAX, block_size: int = BLOCK_SIZE,
                  attempts: int = UPLOAD_ATTEMPTS, retry_delay: float = 1.0,
                  name: str = "") -> bool:
    """Upload ``path`` to an Azure SAS URL. Returns False when the blob turned
    out to exist already (nothing more to do), True when we uploaded it.

    ``on_progress`` receives the bytes of this file sent so far (it restarts
    from the last committed block when a retry resends data).
    """
    uploader = _Uploader(session, target, cancel, attempts=attempts, retry_delay=retry_delay)
    what = name or os.path.basename(path)
    try:
        if size <= single_put_max:
            _put_single(uploader, path, size, content_type, on_progress, what)
        else:
            _put_blocks(uploader, path, size, content_type, on_progress, what,
                        _block_size_for(size, block_size))
    except _AlreadyStored:
        return False
    on_progress(size)
    return True


def _put_single(uploader: _Uploader, path: str, size: int, content_type: str,
                on_progress: Callable[[int], None], what: str) -> None:
    def make_request() -> requests.Response:
        sent = [0]
        on_progress(0)

        def counted(count: int) -> None:
            sent[0] += count
            on_progress(sent[0])

        headers = {"x-ms-blob-type": "BlockBlob", "Content-Type": content_type}
        headers.update(uploader.target.headers)
        if size == 0:
            return uploader.session.put(uploader.target.url, data=b"", headers=headers, timeout=TRANSFER_TIMEOUT)
        body = _FileWindow(path, 0, size, uploader.cancel, counted)
        try:
            return uploader.session.put(uploader.target.url, data=body, headers=headers, timeout=TRANSFER_TIMEOUT)
        finally:
            body.close()

    uploader.send(what, make_request)


def _put_blocks(uploader: _Uploader, path: str, size: int, content_type: str,
                on_progress: Callable[[int], None], what: str, block_size: int) -> None:
    ids: list[str] = []
    committed = 0
    count = math.ceil(size / block_size)
    for index in range(count):
        offset = index * block_size
        length = min(block_size, size - offset)
        bid = block_id(index)

        def make_request(offset: int = offset, length: int = length, bid: str = bid,
                         base: int = committed) -> requests.Response:
            sent = [0]
            on_progress(base)

            def counted(n: int) -> None:
                sent[0] += n
                on_progress(base + sent[0])

            body = _FileWindow(path, offset, length, uploader.cancel, counted)
            try:
                return uploader.session.put(uploader.target.url, params={"comp": "block", "blockid": bid},
                                            data=body, timeout=TRANSFER_TIMEOUT)
            finally:
                body.close()

        uploader.send(f"{what} (part {index + 1} of {count})", make_request)
        ids.append(bid)
        committed += length

    xml = block_list_xml(ids)

    def commit() -> requests.Response:
        return uploader.session.put(uploader.target.url, params={"comp": "blocklist"}, data=xml,
                                    headers={"Content-Type": "application/xml",
                                             "x-ms-blob-content-type": content_type},
                                    timeout=TRANSFER_TIMEOUT)

    uploader.send(what, commit)


# ── Downloads from signed URLs ────────────────────────────────────────────────

def download_to(session: requests.Session, url: str, dest: str, *, expected_sha256: str,
                expected_size: Optional[int], cancel: CancelToken,
                on_progress: Callable[[int], None],
                renew_url: Optional[Callable[[], str]] = None,
                attempts: int = 3, retry_delay: float = 1.0) -> None:
    """Stream ``url`` into ``dest`` through a temporary file, verify the SHA-256,
    then move it into place (the destination never holds a partial file)."""
    folder = os.path.dirname(dest) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f".{os.path.basename(dest)}.{uuid.uuid4().hex[:8]}.part")
    problem = ""
    renewed = False
    delay = retry_delay
    try:
        for attempt in range(1, attempts + 1):
            cancel.check()
            digest = hashlib.sha256()
            written = 0
            on_progress(0)
            try:
                with session.get(url, stream=True, timeout=TRANSFER_TIMEOUT) as response:
                    if response.status_code == 403 and renew_url is not None and not renewed:
                        renewed = True
                        url = renew_url()
                        continue
                    if response.status_code != 200:
                        problem = f"HTTP {response.status_code}"
                        if response.status_code not in RETRYABLE_STATUS:
                            break
                    else:
                        with open(tmp, "wb") as out:
                            for chunk in response.iter_content(DOWNLOAD_CHUNK):
                                cancel.check()
                                if not chunk:
                                    continue
                                out.write(chunk)
                                digest.update(chunk)
                                written += len(chunk)
                                on_progress(written)
                        if expected_size is not None and written != expected_size:
                            problem = f"got {written} of {expected_size} bytes"
                        elif digest.hexdigest() != expected_sha256:
                            problem = "the downloaded bytes don't match their checksum"
                        else:
                            os.replace(tmp, dest)
                            return
            except requests.RequestException as exc:
                problem = f"network error ({exc.__class__.__name__})"
            if attempt < attempts:
                cancel.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise CloudError(f"Downloading {os.path.basename(dest)} failed: {problem}.", code="download_failed")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                log.debug("Could not remove %s", tmp, exc_info=True)


# ── Project JSON helpers ──────────────────────────────────────────────────────

_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def is_local_media_path(path: Any) -> bool:
    """A path we can hash and upload: local, and not an image-sequence pattern."""
    if not isinstance(path, str) or not path.strip():
        return False
    return "%" not in path and not _URL_SCHEME.match(path) and not path.startswith("content:")


def display_name(entry: Mapping[str, Any], path: Optional[str] = None) -> str:
    name = entry.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    for candidate in (path, entry.get("path")):
        if isinstance(candidate, str) and candidate:
            base = os.path.basename(candidate.replace("\\", "/").rstrip("/"))
            if base:
                return base
    return str(entry.get("id") or "media")


def read_project_file(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise CloudError(f"The project file {path} is missing. Save the project, then try again.",
                         code="project_missing") from None
    except (OSError, ValueError) as exc:
        raise CloudError(f"Couldn't read the project file {path}: {exc}", code="project_unreadable") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise CloudError(f"{path} isn't a Zenvi project file.", code="project_unreadable")
    return data


def serialize_project(project: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(project, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except ValueError as exc:
        raise CloudError("This project contains numbers Zenvi Cloud can't store (NaN or infinity). "
                         "Check keyframe values, then push again.", code="invalid_project") from exc


def build_cloud_copy(project: dict[str, Any], refs: Mapping[str, dict[str, Any]],
                     drop_refs: Iterable[str] = ()) -> dict[str, Any]:
    """Prepare the saved project for upload (mutates and returns it).

    Sets ``file["cloud"]`` from ``refs``, removes stale refs listed in
    ``drop_refs``, and strips what only makes sense on this computer: the
    ``zenvi_cloud`` link and optimized-preview (proxy) readers. Paths stay in
    the saved, portable form (``@transitions/…``, relative paths), which is
    what the web editor and the cloud renderer resolve.
    """
    dropped = set(drop_refs)
    for entry in project.get("files") or []:
        if not isinstance(entry, dict):
            continue
        file_id = str(entry.get("id") or "")
        entry.pop("proxy_reader", None)
        if file_id in refs:
            entry["cloud"] = dict(refs[file_id])
        elif file_id in dropped:
            entry.pop("cloud", None)
    project.pop("zenvi_cloud", None)
    return project


def rewrite_media_paths(project: dict[str, Any], local_paths: Mapping[str, str]) -> None:
    """Point ``files[].path`` and each clip's ``reader.path`` at the local copies."""
    for entry in project.get("files") or []:
        if isinstance(entry, dict) and entry.get("id") in local_paths:
            entry["path"] = local_paths[entry["id"]]
            entry.pop("proxy_reader", None)
    for clip in project.get("clips") or []:
        if not isinstance(clip, dict):
            continue
        local = local_paths.get(str(clip.get("file_id") or ""))
        reader = clip.get("reader")
        if local and isinstance(reader, dict):
            reader["path"] = local


_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_name(name: Any, fallback: str = "Untitled project", limit: int = 80) -> str:
    """A folder/file stem that is valid on macOS, Windows and Linux."""
    text = _ILLEGAL_NAME_CHARS.sub(" ", name if isinstance(name, str) else "")
    text = " ".join(text.split()).strip(" .")
    text = text[:limit].rstrip(" .")
    if not text:
        text = fallback
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = f"{text}_"
    return text


def safe_file_name(name: Any, fallback: str = "media") -> str:
    """Like :func:`safe_name` but keeps the extension."""
    raw = os.path.basename(name.replace("\\", "/")) if isinstance(name, str) else ""
    stem, ext = os.path.splitext(raw)
    ext = _ILLEGAL_NAME_CHARS.sub("", ext)[:16]
    return safe_name(stem, fallback=fallback, limit=120) + ext


def _write_json_atomic(path: str, data: Mapping[str, Any]) -> None:
    folder = os.path.dirname(path) or "."
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ── Push ──────────────────────────────────────────────────────────────────────

@dataclass
class PushRequest:
    """What the GUI thread hands the worker: the saved project file, where each
    file's bytes are on disk (by file id), and the name for a new cloud project."""

    project_file: str
    local_paths: dict[str, str]
    name: str = ""


@dataclass
class PushResult:
    project_id: str
    revision: str
    editor_url: str
    name: str
    created: bool
    overwritten: bool
    #: New ``project["zenvi_cloud"]`` to store locally (untracked, not an undo step).
    link: dict[str, Any]
    #: ``file["cloud"]`` per file id, only where it changed.
    cloud_refs: dict[str, dict[str, Any]]
    uploaded_files: int = 0
    uploaded_bytes: int = 0
    already_in_cloud: int = 0
    hashed_files: int = 0
    history_dropped: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class _LocalMedia:
    file_id: str
    path: str
    name: str
    size: int
    mtime: float
    content_type: str
    old_ref: Any
    sha256: str = ""
    skipped: bool = False

    def ref(self) -> dict[str, Any]:
        return {"sha256": self.sha256, "size": self.size, "content_type": self.content_type,
                "name": self.name, "mtime": self.mtime}


@dataclass
class _Unavailable:
    """A project file whose bytes are not on this computer right now."""

    file_id: str
    name: str
    problem: str
    #: The hash from an earlier push, if any: the cloud may still hold those bytes.
    sha256: str = ""


def _collect_media(project: Mapping[str, Any], local_paths: Mapping[str, str]
                   ) -> tuple[list[_LocalMedia], list[_Unavailable]]:
    items: list[_LocalMedia] = []
    unavailable: list[_Unavailable] = []
    for entry in project.get("files") or []:
        if not isinstance(entry, dict):
            continue
        file_id = str(entry.get("id") or "")
        path = local_paths.get(file_id) or ""
        name = display_name(entry, path)
        old_ref = entry.get("cloud")
        old_sha = str(old_ref.get("sha256")).lower() if isinstance(old_ref, dict) and is_sha256(old_ref.get("sha256")) else ""
        problem = ""
        info: Optional[os.stat_result] = None
        if "%" in path:
            problem = "image sequences aren't supported in Zenvi Cloud yet"
        elif not is_local_media_path(path):
            problem = "not a file on this computer"
        else:
            try:
                info = os.stat(path)
            except OSError:
                problem = f"not found at {path}"
            else:
                if not stat.S_ISREG(info.st_mode):
                    problem = "not a regular file"
        if problem or info is None:
            unavailable.append(_Unavailable(file_id, name, problem or "not readable", old_sha))
            continue
        items.append(_LocalMedia(file_id, path, name, int(info.st_size), float(info.st_mtime),
                                 content_type_for(path), old_ref))
    return items, unavailable


def _hash_media(items: list[_LocalMedia], cancel: CancelToken, progress: ProgressFn,
                warnings: list[str]) -> int:
    """Fill in ``sha256`` (cache first). Returns how many files were read."""
    todo = []
    for item in items:
        cached = cached_sha256(item.old_ref, item.size, item.mtime)
        if cached:
            item.sha256 = cached
        else:
            todo.append(item)
    total = sum(item.size for item in todo)
    done = 0
    for item in todo:
        cancel.check()
        for _attempt in range(2):
            base = done
            seen = [0]

            def on_bytes(count: int, base: int = base, name: str = item.name) -> None:
                seen[0] += count
                progress(Progress("hash", base + seen[0], total, name))

            progress(Progress("hash", base, total, item.name))
            try:
                digest = sha256_file(item.path, cancel=cancel, on_bytes=on_bytes)
                after = os.stat(item.path)
            except OSError as exc:
                warnings.append(f"{item.name}: couldn't be read ({exc.strerror or exc}); left out.")
                item.skipped = True
                break
            if int(after.st_size) == item.size and abs(float(after.st_mtime) - item.mtime) <= MTIME_TOLERANCE:
                item.sha256 = digest
                break
            # Changed while we read it: take the new size/mtime and read it once more.
            total += int(after.st_size) - item.size
            item.size, item.mtime = int(after.st_size), float(after.st_mtime)
        else:
            warnings.append(f"{item.name}: kept changing while it was being read; left out.")
            item.skipped = True
        done += item.size
    return len(todo)


def _upload_one(client: CloudClient, item: _LocalMedia, cancel: CancelToken,
                on_progress: Callable[[int], None], *, single_put_max: int, block_size: int,
                retry_delay: float) -> bool:
    """Declare, upload and complete one media blob. False when it already existed."""
    ticket = client.declare_media(item.sha256, item.size, item.name, item.content_type)
    if ticket.get("exists"):
        return False
    url = ticket.get("uploadUrl")
    if not isinstance(url, str) or not url:
        raise CloudError("Zenvi Cloud didn't return an upload link.", code="bad_response")
    headers = ticket.get("uploadHeaders")

    def renew() -> Optional[tuple[str, dict[str, str]]]:
        fresh = client.declare_media(item.sha256, item.size, item.name, item.content_type)
        fresh_url = fresh.get("uploadUrl")
        if fresh.get("exists") or not isinstance(fresh_url, str):
            return None
        fresh_headers = fresh.get("uploadHeaders")
        return fresh_url, dict(fresh_headers) if isinstance(fresh_headers, dict) else {}

    target = SasTarget(url, headers if isinstance(headers, dict) else None, renew)
    for attempt in range(2):
        uploaded = upload_to_sas(client.session, target, item.path, item.size, item.content_type,
                                 cancel=cancel, on_progress=on_progress,
                                 single_put_max=single_put_max, block_size=block_size,
                                 retry_delay=retry_delay, name=item.name)
        if not uploaded:
            return False
        try:
            client.complete_media(item.sha256)
            return True
        except CloudError as exc:
            # 422: the cloud re-checked size/hash and threw the blob away.
            if exc.status != 422 or attempt == 1:
                raise
            if not target.renew():
                return False
    return True


def _save_to_cloud(client: CloudClient, body: bytes, link: Mapping[str, Any], name: str,
                   confirm_overwrite: Optional[Callable[[CloudConflictError], bool]],
                   warnings: list[str]) -> tuple[str, str, bool, bool]:
    """PUT (If-Match) or POST the project. Returns (id, revision, created, overwritten)."""
    project_id = link.get("project_id")
    if is_project_id(project_id):
        assert isinstance(project_id, str)
        revision = normalize_revision(link.get("revision")) or UNKNOWN_REVISION
        try:
            response = client.save_project(project_id, body, revision)
            return project_id, response.revision() or "", False, False
        except CloudConflictError as conflict:
            if confirm_overwrite is None or not confirm_overwrite(conflict):
                raise CloudCancelled("The cloud copy was left as it is.", code="conflict_cancelled") from None
            response = client.save_project(project_id, body, "*")
            return project_id, response.revision() or "", False, True
        except CloudNotFoundError:
            warnings.append("The earlier cloud copy of this project was deleted, so a new one was created.")
    response = client.create_project(name, body)
    new_id = response.body.get("id") if isinstance(response.body, dict) else None
    if not is_project_id(new_id):
        raise CloudError("Zenvi Cloud didn't return a project id.", code="bad_response")
    assert isinstance(new_id, str)
    return new_id, response.revision() or "", True, False


def push_project(client: CloudClient, request: PushRequest, *, website: str,
                 progress: Optional[ProgressFn] = None,
                 confirm_overwrite: Optional[Callable[[CloudConflictError], bool]] = None,
                 now: Callable[[], str] = utc_now_iso,
                 single_put_max: int = SINGLE_PUT_MAX, block_size: int = BLOCK_SIZE,
                 max_project_bytes: int = MAX_PROJECT_BYTES) -> PushResult:
    """Push the saved project file and whatever media the cloud lacks.

    Hashes every referenced media file (reusing ``file["cloud"]`` when size and
    mtime still match), asks the cloud which hashes it lacks, uploads only those,
    then creates or updates the cloud project with ``If-Match``. On a 409 it
    calls ``confirm_overwrite``; False (or no callback) cancels the push.
    """
    report = progress or _no_progress
    cancel = client.cancel
    warnings: list[str] = []
    name = request.name or os.path.splitext(os.path.basename(request.project_file))[0] or "Untitled project"

    report(Progress("prepare", detail=name))
    client.ensure_signed_in()
    project = read_project_file(request.project_file)
    raw_link = project.get("zenvi_cloud")
    link: Mapping[str, Any] = raw_link if isinstance(raw_link, dict) else {}
    items, unavailable = _collect_media(project, request.local_paths)
    hashed = _hash_media(items, cancel, report, warnings)
    present = [item for item in items if not item.skipped and item.sha256]

    cancel.check()
    report(Progress("check"))
    unique: dict[str, _LocalMedia] = {}
    for item in present:
        unique.setdefault(item.sha256, item)
    wanted = set(unique) | {u.sha256 for u in unavailable if u.sha256}
    existing = client.check_media(wanted) if wanted else set()
    missing = [item for sha, item in unique.items() if sha not in existing]

    drop: list[str] = []
    for gone in unavailable:
        if gone.sha256 and gone.sha256 in existing:
            warnings.append(f"{gone.name}: {gone.problem}; the copy already in Zenvi Cloud is used.")
        else:
            warnings.append(f"{gone.name}: {gone.problem}; left out.")
            drop.append(gone.file_id)

    total = sum(item.size for item in missing)
    sent_before = 0
    uploaded_files = 0
    uploaded_bytes = 0
    for item in missing:
        cancel.check()

        def on_file(sent: int, base: int = sent_before, file_name: str = item.name) -> None:
            report(Progress("upload", base + sent, total, file_name))

        try:
            if _upload_one(client, item, cancel, on_file, single_put_max=single_put_max,
                           block_size=block_size, retry_delay=client.retry_delay):
                uploaded_files += 1
                uploaded_bytes += item.size
        except (CloudCancelled, CloudQuotaError, CloudAuthError, CloudOfflineError):
            raise
        except (CloudError, OSError) as exc:
            if isinstance(exc, OSError):
                note = f"couldn't be read ({exc.strerror or exc}); left out."
            elif exc.status in (413, 415) or isinstance(exc, _LocalFileChanged):
                note = f"{exc.message} Left out."
            else:
                raise
            warnings.append(f"{item.name}: {note}")
            for other in present:
                if other.sha256 == item.sha256:
                    other.skipped = True
        sent_before += item.size

    refs: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.skipped or not item.sha256:
            drop.append(item.file_id)
        else:
            refs[item.file_id] = item.ref()

    cloud_project = build_cloud_copy(project, refs, drop)
    body = serialize_project(cloud_project)
    history_dropped = False
    if len(body) > max_project_bytes and cloud_project.get("history"):
        cloud_project["history"] = {"undo": [], "redo": []}
        body = serialize_project(cloud_project)
        history_dropped = True
        warnings.append("The undo history was left out of the cloud copy to keep it under the size limit.")
    if len(body) > max_project_bytes:
        raise CloudError(f"This project's data is {format_bytes(len(body))}; Zenvi Cloud stores projects "
                         f"up to {format_bytes(max_project_bytes)}.", code="too_large")

    cancel.check()
    report(Progress("project", detail=name))
    project_id, revision, created, overwritten = _save_to_cloud(
        client, body, link, name, confirm_overwrite, warnings)

    old_refs = {item.file_id: item.old_ref for item in items}
    changed_refs = {file_id: ref for file_id, ref in refs.items() if _ref_changed(old_refs.get(file_id), ref)}
    return PushResult(
        project_id=project_id,
        revision=revision,
        editor_url=editor_url(website, project_id),
        name=name,
        created=created,
        overwritten=overwritten,
        link={"project_id": project_id, "revision": revision, "pushed_at": now()},
        cloud_refs=changed_refs,
        uploaded_files=uploaded_files,
        uploaded_bytes=uploaded_bytes,
        already_in_cloud=len(unique) - len(missing),
        hashed_files=hashed,
        history_dropped=history_dropped,
        warnings=warnings,
    )


def _ref_changed(old: Any, new: Mapping[str, Any]) -> bool:
    if not isinstance(old, dict):
        return True
    return any(old.get(key) != value for key, value in new.items())


# ── Pull ──────────────────────────────────────────────────────────────────────

@dataclass
class PullResult:
    project_file: str
    project_id: str
    revision: str
    name: str
    downloaded_files: int = 0
    downloaded_bytes: int = 0
    reused_files: int = 0
    #: True when an older local copy of the project file was replaced (it was backed up first).
    replaced_existing: bool = False
    warnings: list[str] = field(default_factory=list)


def default_pull_root(home: Optional[str] = None) -> str:
    """``~/Zenvi/Cloud``: one folder per pulled project."""
    return os.path.join(home or os.path.expanduser("~"), "Zenvi", "Cloud")


def _linked_project(project_file: str) -> Optional[dict[str, Any]]:
    try:
        with open(project_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def choose_pull_folder(root: str, stem: str, project_id: str) -> tuple[str, Optional[dict[str, Any]]]:
    """``<root>/<stem>``, or ``<stem> (2)``… when that folder holds another project.

    Returns the folder and, when it already holds this cloud project, the
    local project data found there (for reusing its media hashes).
    """
    for n in range(1, 1000):
        folder = os.path.join(root, stem if n == 1 else f"{stem} ({n})")
        project_file = os.path.join(folder, stem + PROJECT_EXT)
        if not os.path.exists(folder):
            return folder, None
        if not os.path.isdir(folder):
            continue
        if not os.path.exists(project_file):
            # A cancelled pull leaves media but no project file: reuse it, unless
            # the folder belongs to some other project.
            if not any(name.lower().endswith(PROJECT_EXT) for name in os.listdir(folder)):
                return folder, None
            continue
        existing = _linked_project(project_file)
        link = existing.get("zenvi_cloud") if existing else None
        if isinstance(link, dict) and link.get("project_id") == project_id:
            return folder, existing
    raise CloudError(f"Couldn't find a free folder for {stem} in {root}.", code="no_folder")


def _known_hashes(existing: Optional[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Local path -> ``cloud`` ref, from a previously pulled copy of this project."""
    known: dict[str, dict[str, Any]] = {}
    for entry in (existing or {}).get("files") or []:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("cloud"), dict):
            known[os.path.normcase(os.path.abspath(entry["path"]))] = entry["cloud"]
    return known


def _matches(path: str, sha256: str, size: Optional[int], known: Mapping[str, Mapping[str, Any]],
             cancel: CancelToken, progress: ProgressFn, label: str) -> bool:
    try:
        info = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or (size is not None and info.st_size != size):
        return False
    cached = cached_sha256(known.get(os.path.normcase(os.path.abspath(path))),
                           int(info.st_size), float(info.st_mtime))
    if cached is not None:
        return cached == sha256
    progress(Progress("verify", 0, int(info.st_size), label))
    seen = [0]

    def on_bytes(count: int) -> None:
        seen[0] += count
        progress(Progress("verify", seen[0], int(info.st_size), label))

    try:
        return sha256_file(path, cancel=cancel, on_bytes=on_bytes) == sha256
    except OSError:
        return False


def pull_project(client: CloudClient, project_id: str, *, dest_root: str,
                 progress: Optional[ProgressFn] = None,
                 backup: Optional[Callable[[str], None]] = None,
                 now: Callable[[], str] = utc_now_iso) -> PullResult:
    """Download a cloud project into ``<dest_root>/<name>/`` and return the
    local project file to open.

    Media goes to ``<name>_assets/media/``; files already there with the same
    SHA-256 are kept. ``files[].path`` and ``clips[].reader.path`` are rewritten
    to the local copies. An existing project file is passed to ``backup``
    before it is replaced; the new one is written atomically.
    """
    try:
        return _pull_into(client, project_id, dest_root=dest_root, progress=progress, backup=backup, now=now)
    except OSError as exc:
        # Disk full, no permission, ...: say where, instead of a bare errno.
        where = exc.filename or dest_root
        raise CloudError(f"Couldn't save the project on this computer ({where}): {exc.strerror or exc}",
                         code="local_io") from exc


def _pull_into(client: CloudClient, project_id: str, *, dest_root: str,
               progress: Optional[ProgressFn], backup: Optional[Callable[[str], None]],
               now: Callable[[], str]) -> PullResult:
    report = progress or _no_progress
    cancel = client.cancel
    report(Progress("fetch"))
    doc = client.get_project(project_id)
    project: dict[str, Any] = doc["project"]
    name = doc.get("name") if isinstance(doc.get("name"), str) and doc.get("name") else "Untitled project"
    assert isinstance(name, str)
    revision = str(doc.get("revision") or "")
    stem = safe_name(name)
    folder, existing = choose_pull_folder(dest_root, stem, project_id)
    project_file = os.path.join(folder, stem + PROJECT_EXT)
    media_dir = os.path.join(folder, f"{stem}_assets", "media")
    os.makedirs(media_dir, exist_ok=True)
    known = _known_hashes(existing)
    warnings: list[str] = []

    files_by_id = {str(f.get("id")): f for f in project.get("files") or [] if isinstance(f, dict)}
    by_sha: dict[str, dict[str, Any]] = {}
    file_ids_by_sha: dict[str, list[str]] = {}
    for entry in doc.get("media") or []:
        if not isinstance(entry, dict) or not is_sha256(entry.get("sha256")) or not isinstance(entry.get("url"), str):
            continue
        sha = str(entry["sha256"]).lower()
        by_sha.setdefault(sha, entry)
        file_ids_by_sha.setdefault(sha, []).append(str(entry.get("fileId") or ""))

    # Decide where each blob lives locally; keep the ones already on disk.
    taken: set[str] = set()
    targets: dict[str, str] = {}
    downloads: list[tuple[str, str, dict[str, Any]]] = []
    reused = 0
    for sha, entry in by_sha.items():
        cancel.check()
        first = files_by_id.get(file_ids_by_sha[sha][0], {})
        preferred = safe_file_name(entry.get("name") or display_name(first), fallback=sha[:12])
        size = entry.get("size") if isinstance(entry.get("size"), int) else None
        stem_part, ext = os.path.splitext(preferred)
        for n in range(1, 10_000):
            candidate = preferred if n == 1 else f"{stem_part} ({n}){ext}"
            if candidate.casefold() in taken:
                continue
            path = os.path.join(media_dir, candidate)
            if not os.path.exists(path):
                taken.add(candidate.casefold())
                targets[sha] = path
                downloads.append((sha, path, entry))
                break
            if _matches(path, sha, size, known, cancel, report, candidate):
                taken.add(candidate.casefold())
                targets[sha] = path
                reused += 1
                break
        else:
            raise CloudError(f"Too many files named {preferred} in {media_dir}.", code="no_name")

    total = sum(int(entry.get("size") or 0) for _sha, _path, entry in downloads)
    done = 0
    downloaded_bytes = 0
    for sha, path, entry in downloads:
        cancel.check()
        label = os.path.basename(path)

        def on_bytes(count: int, base: int = done, label: str = label) -> None:
            report(Progress("download", base + count, total, label))

        def renew(sha: str = sha) -> str:
            return client.media_url(sha)

        size = entry.get("size") if isinstance(entry.get("size"), int) else None
        download_to(client.session, str(entry["url"]), path, expected_sha256=sha, expected_size=size,
                    cancel=cancel, on_progress=on_bytes, renew_url=renew, retry_delay=client.retry_delay)
        grown = int(entry.get("size") or os.path.getsize(path))
        done += grown
        downloaded_bytes += grown

    # Rewrite paths and refresh the hash cache with the local copies' size/mtime.
    report(Progress("write", detail=stem))
    local_paths: dict[str, str] = {}
    for sha, path in targets.items():
        info = os.stat(path)
        for file_id in file_ids_by_sha.get(sha, []):
            local_paths[file_id] = path
            entry = files_by_id.get(file_id)
            if entry is None:
                continue
            ref = dict(entry.get("cloud") or {}) if isinstance(entry.get("cloud"), dict) else {}
            ref.update({"sha256": sha, "size": int(info.st_size), "mtime": float(info.st_mtime)})
            ref.setdefault("name", by_sha[sha].get("name") or os.path.basename(path))
            content_type = by_sha[sha].get("contentType")
            if isinstance(content_type, str) and content_type:
                ref.setdefault("content_type", content_type)
            entry["cloud"] = ref
    rewrite_media_paths(project, local_paths)
    for missing in doc.get("missingMedia") or []:
        if isinstance(missing, dict):
            label = str(missing.get("name") or missing.get("fileId") or "A file")
            warnings.append(f"{label}: not in Zenvi Cloud (it was missing when the project was pushed).")
    project["zenvi_cloud"] = {"project_id": project_id, "revision": revision, "pulled_at": now()}

    cancel.check()
    replaced = os.path.exists(project_file)
    if replaced and backup is not None:
        backup(project_file)
    _write_json_atomic(project_file, project)
    return PullResult(
        project_file=project_file,
        project_id=project_id,
        revision=revision,
        name=name,
        downloaded_files=len(downloads),
        downloaded_bytes=downloaded_bytes,
        reused_files=reused,
        replaced_existing=replaced,
        warnings=warnings,
    )


def fetch_thumbnails(session: requests.Session, projects: Sequence[Mapping[str, Any]], *,
                     cancel: CancelToken, limit: int = 40,
                     max_bytes: int = 2 * 1024 * 1024) -> dict[str, bytes]:
    """Thumbnail bytes by project id, for projects whose summary carries a ``thumbnailUrl``."""
    out: dict[str, bytes] = {}
    for project in list(projects)[:limit]:
        url = project.get("thumbnailUrl")
        project_id = project.get("id")
        if not isinstance(url, str) or not url.startswith("https://") or not isinstance(project_id, str):
            continue
        cancel.check()
        try:
            response = session.get(url, timeout=(5.0, 10.0))
        except requests.RequestException:
            continue
        if response.status_code == 200 and 0 < len(response.content) <= max_bytes:
            out[project_id] = response.content
    return out
