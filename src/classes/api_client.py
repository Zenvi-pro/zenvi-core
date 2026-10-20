"""
Zenvi Backend API Client.

Drop-in replacement for direct AI class imports. The frontend uses this client
to communicate with the separate zenvi-backend server instead of running LLM /
provider / agent code in-process.

Usage:
    from classes.api_client import ZenviBackendClient
    client = ZenviBackendClient()  # reads ZENVI_BACKEND_URL from settings

    # Chat (WebSocket only)
    response = client.send_message_ws("add a clip to the timeline")

    # Models
    models = client.list_models()

    # Generation
    result = client.generate_video("a cat running on a beach")
"""

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Callable
from classes.logger import log

# Shown when a paid action cannot run because of the login. The wording matters: the plan runner treats
# "login required" / "unauthorized" as an auth failure to report, never as a step to repair with another tool.
LOGIN_REQUIRED_MESSAGE = "Login required: sign in to Zenvi (Zenvi menu > Sign in), then run this again."
SESSION_REJECTED_MESSAGE = "Unauthorized: Zenvi did not accept your login (the session may have expired). Sign in again, then run this again."

from classes.zenvi_env import load_zenvi_dotenv

load_zenvi_dotenv()

_DEFAULT_BACKEND_URL = "https://api.zenvi.pro"
PROVIDER_KEY_HEADER = "X-Zenvi-Provider-Key"
_UNVERIFIED_KEY_TRANSPORT = "Your own provider key is only sent to a verified HTTPS Zenvi backend."


def _refresh_credits_after_backend_billing() -> None:
    """Repaint the credits badge after a request the backend bills itself.

    /generation/video and /generation/morph deduct (or refund) before they
    answer, so the balance is final by the time the request returns.
    """
    try:
        from classes.credits_client import credits
        credits.refresh_balance()
    except Exception as exc:
        log.debug("credits refresh after a backend-billed request failed: %s", exc)


def _v2_refusal(response) -> dict:
    """A 402, 403 or 429 from the media index v2 routes as ``{"error", ...}`` with a flag saying which.

    ``credits``: the account is out of credits; ``rate_limited`` (and ``retry_after`` seconds): too many requests or the
    daily limit; ``forbidden``: the upload or job belongs to another account. The server's own message is kept.
    """
    try:
        detail = (response.json() or {}).get("detail")
    except Exception:  # noqa: BLE001
        detail = None
    detail = detail if isinstance(detail, dict) else {}
    message = str(detail.get("message") or "")
    if response.status_code == 402:
        return {"credits": True, "error": message or "Out of credits for media indexing. Upgrade your plan or add credits to continue."}
    if response.status_code == 429:
        out = {"rate_limited": True, "error": message or "Too many media index requests. Try again shortly."}
        if detail.get("retry_after") is not None:
            out["retry_after"] = detail["retry_after"]
        return out
    return {"forbidden": True, "error": message or "The media index refused this request."}


class ZenviBackendClient:
    """HTTP/WebSocket client for the Zenvi backend API."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or self._get_backend_url()).rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"
        self._session = None
        self._active_wss = set()  # active WebSockets during parallel chat requests
        self._ws_lock = threading.Lock()
        self._shutting_down = False
        # Disable SSL verification for non-production backends (self-signed certs)
        self._ssl_verify = (self.base_url.rstrip("/") == _DEFAULT_BACKEND_URL.rstrip("/"))

    @staticmethod
    def _get_backend_url() -> str:
        """Get backend URL from app settings or environment.

        Supports multi-URL fallback:
          - ZENVI_BACKEND_URLS: comma-separated priority list (first healthy wins)
          - ZENVI_BACKEND_URL: single URL (legacy)
          - settings key zenvi-backend-url (legacy)

        If ZENVI_BACKEND_URLS is set, we probe /health quickly to select a URL.
        """
        urls_raw = os.environ.get("ZENVI_BACKEND_URLS", "").strip()
        if urls_raw:
            candidates = [u.strip().rstrip("/") for u in urls_raw.split(",") if u.strip()]
            if candidates:
                try:
                    import requests
                    for u in candidates:
                        try:
                            r = requests.get(f"{u}/health", timeout=1.5)
                            if r.status_code == 200:
                                return u
                        except Exception:
                            continue
                    # If none respond, fall back to the first candidate to surface real errors upstream.
                    return candidates[0]
                except Exception:
                    return candidates[0]

        url = os.environ.get("ZENVI_BACKEND_URL", "").strip()
        if url:
            return url.rstrip("/")
        try:
            from classes.app import get_app
            app = get_app()
            if app and hasattr(app, "get_settings"):
                s = app.get_settings()
                url = s.get("zenvi-backend-url") if s else ""
                if url:
                    return str(url).rstrip("/")
        except Exception:
            pass
        return _DEFAULT_BACKEND_URL

    @property
    def session(self):
        """Lazy-create a requests.Session and keep its bearer token current.

        Paid backend routes (/search, /generation/*, /research/*) reject any
        request without ``Authorization: Bearer <jwt>``. The token is re-read
        on every access so a refreshed or cleared login is picked up without
        rebuilding the session.
        """
        if self._session is None:
            try:
                import requests
                self._session = requests.Session()
                self._session.headers.update({"Content-Type": "application/json"})
                if not self._ssl_verify:
                    self._session.verify = False
                    import urllib3
                    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except ImportError:
                log.error("requests library is required for ZenviBackendClient")
                raise
        self._apply_bearer(self._session)
        return self._session

    def _apply_bearer(self, session) -> None:
        """Set or clear the Authorization header from the current login."""
        token = self._auth_token()
        if token:
            session.headers["Authorization"] = f"Bearer {token}"
        else:
            session.headers.pop("Authorization", None)

    def auth_token(self) -> Optional[str]:
        """Current user JWT for backend usage/credits tracking."""
        return self._auth_token()

    @staticmethod
    def _auth_token() -> Optional[str]:
        try:
            from classes.auth_manager import AuthManager
            return AuthManager.instance().get_access_token()
        except Exception:
            return None

    def _multipart_headers(self, session) -> Dict[str, Optional[str]]:
        headers = {
            k: v for k, v in session.headers.items()
            if k.lower() != "content-type"
        }
        # Override session default so requests sets multipart boundary.
        headers["Content-Type"] = None
        return headers

    def upload_media_file(
        self,
        local_path: str,
        file_id: str = "",
        filename: Optional[str] = None,
        session=None,
    ) -> Dict[str, Any]:
        """Upload a file to the backend temp store; returns success + file_id."""
        if not local_path or not os.path.isfile(local_path):
            return {"success": False, "error": f"File not found: {local_path}"}
        fid = file_id or uuid.uuid4().hex
        name = filename or os.path.basename(local_path)
        try:
            s = session or self.session
            with open(local_path, "rb") as fh:
                r = s.post(
                    f"{self.api_url}/media/upload",
                    files={"file": (name, fh)},
                    data={"file_id": fid, "filename": name},
                    headers=self._multipart_headers(s),
                    timeout=600,
                )
            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                return {"success": False, "error": data.get("error", "Upload failed")}
            server_path = data.get("server_path", "")
            log.info(
                "Uploaded %s to backend (file_id=%s, server_path=%s)",
                name, fid, server_path or "(unknown)",
            )
            return {"success": True, "file_id": fid, "server_path": server_path}
        except Exception as exc:
            log.error("Media upload failed: %s", exc)
            return {"success": False, "error": str(exc)}

    @staticmethod
    def _download_url_to_temp(
        url: str,
        suffix: str,
        filename_hint: str = "",
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """Download a public CDN URL to a local temp file on the desktop."""
        if not url:
            return {"local_path": "", "error": "No download URL"}
        try:
            import requests
            from classes import info

            safe = re.sub(r"[^\w\-]", "_", filename_hint) if filename_hint else f"zenvi_{uuid.uuid4().hex[:10]}"
            dest_dir = os.path.join(info.USER_PATH, "Downloads")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, f"{safe}{suffix}")
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                return {"local_path": dest}

            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Zenvi/1.0"},
                stream=True,
                timeout=timeout,
            )
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        fh.write(chunk)
            if os.path.getsize(dest) > 0:
                return {"local_path": dest}
            return {"local_path": "", "error": "Downloaded file is empty"}
        except Exception as exc:
            log.error("URL download failed: %s", exc)
            return {"local_path": "", "error": str(exc)}

    @staticmethod
    def pick_pexels_hd_link(video: Dict[str, Any]) -> str:
        files = video.get("video_files") or []
        hd = next((f for f in files if f.get("quality") == "hd"), None)
        pick = hd or (files[0] if files else {})
        return str(pick.get("link") or "")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def health_check(self) -> bool:
        """Check if the backend is running."""
        try:
            r = self.session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------
    def list_models(self) -> List[Dict[str, Any]]:
        """List all known LLM models.

        Rows carry model_id/display_name plus picker metadata (provider,
        featured, rank, tags, available) — hence Dict[str, Any], the values are
        no longer all strings. Unknown keys pass through untouched.

        The timeout allows for the backend's live provider discovery on a cold
        cache; it fetches providers concurrently and degrades to the curated
        catalog, so this should not actually block for long.
        """
        try:
            r = self.session.get(f"{self.api_url}/models", timeout=20)
            r.raise_for_status()
            data = r.json()
            return data.get("models", [])
        except Exception as e:
            log.error("Failed to list models: %s", e)
            return []

    def fetch_model_catalog(self) -> Dict[str, Any]:
        """The whole ``GET /models`` payload: ``models`` plus ``default_model_id``.

        One round trip for callers that want both, instead of ``list_models``
        followed by ``get_default_model_id`` hitting the endpoint twice.
        Returns ``{}`` on any failure.
        """
        try:
            r = self.session.get(f"{self.api_url}/models", timeout=20)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.error("Failed to fetch model catalog: %s", e)
            return {}

    def list_cli_models(self) -> Dict[str, List[Dict[str, Any]]]:
        """Model-picker lineups for the CLI agent backends, keyed by backend id
        (``claude_code``, ``codex``), from ``GET /models/cli``.

        Built by the backend from each provider's live model list, so a new
        release reaches the picker without a desktop update. Entries follow
        the picker contract (id/name/featured/rank/tags/default) with bare
        ids ready for the CLI's ``--model`` flag. ``{}`` on any failure, and
        an older backend without the route answers the same way; callers keep
        their built-in list in both cases.
        """
        try:
            r = self.session.get(f"{self.api_url}/models/cli", timeout=20)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return {}
            return {k: v for k, v in data.items() if isinstance(v, list)}
        except Exception as e:
            log.debug("CLI model lineups unavailable: %s", e)
            return {}

    def get_default_model_id(self) -> str:
        """Get the default model ID."""
        try:
            r = self.session.get(f"{self.api_url}/models", timeout=10)
            r.raise_for_status()
            return r.json().get("default_model_id", "openai/gpt-4o-mini")
        except Exception:
            return "openai/gpt-4o-mini"

    def get_chat_history(self, session_id: str) -> Dict[str, Any]:
        """Get conversation history."""
        try:
            r = self.session.get(f"{self.api_url}/chat/history/{session_id}", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Get history failed: %s", e)
            return {"messages": [], "session_info": {}}

    def get_session_trace(self, session_id: str, limit: int = 200) -> Dict[str, Any]:
        """Fetch agent/tool telemetry events for diagnosing loops and bottlenecks."""
        try:
            r = self.session.get(
                f"{self.api_url}/chat/sessions/{session_id}/trace",
                params={"limit": max(1, min(int(limit or 200), 1000))},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else {"events": []}
        except Exception as e:
            log.error("Get session trace failed: %s", e)
            return {"session_id": session_id, "events": [], "error": str(e)}

    def clear_chat_session(self, session_id: str) -> bool:
        """Clear a chat session."""
        try:
            r = self.session.post(f"{self.api_url}/chat/clear/{session_id}", timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Chat (WebSocket — streaming with tool delegation)
    # ------------------------------------------------------------------
    def send_message_ws(
        self,
        message: str,
        model_id: Optional[str] = None,
        session_id: Optional[str] = None,
        on_tool_call: Optional[Callable] = None,
        on_response: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_token: Optional[Callable] = None,
        on_tool_progress: Optional[Callable] = None,
        auth_token: Optional[str] = None,
        on_thinking: Optional[Callable] = None,
        agent_mode: Optional[str] = None,
        action: Optional[str] = None,
        plan_id: Optional[str] = None,
        on_plan_event: Optional[Callable] = None,
        images: Optional[List[Dict[str, Any]]] = None,
        project_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Send a chat message via WebSocket with tool delegation support.

        Each incoming ``tool_call`` is dispatched to its own worker thread so
        the agent can fan out N concurrent tool calls and we ack them as soon
        as each one finishes.  The recv loop never blocks on tool execution.

        *images* (optional) are vision parts for the current turn only
        (``[{name, mime_type, image_base64, ...}]``).
        """
        try:
            import websocket
        except ImportError:
            log.error("websocket-client library required for WebSocket chat")
            if on_error:
                on_error("websocket-client not installed")
            return None

        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/v1/chat/ws"

        self._shutting_down = False
        ws = None
        try:
            sslopt = {} if self._ssl_verify else {"cert_reqs": 0}  # 0 = ssl.CERT_NONE
            ws = websocket.create_connection(ws_url, timeout=600, sslopt=sslopt)
            with self._ws_lock:
                self._active_wss.add(ws)

            send_lock = threading.Lock()

            def _ws_send(payload: dict) -> bool:
                try:
                    with send_lock:
                        ws.send(json.dumps(payload))
                    return True
                except Exception as exc:  # noqa: BLE001
                    log.debug("WS send failed: %s", exc)
                    return False

            token = auth_token if auth_token is not None else self._auth_token()
            payload_data: Dict[str, Any] = {
                "message": message,
                "model_id": model_id,
                "session_id": session_id,
                "auth_token": token,
            }
            if agent_mode in ("planning", "agent"):
                payload_data["agent_mode"] = agent_mode
            if action in ("chat", "execute_plan", "cancel_execution"):
                payload_data["action"] = action
            if plan_id:
                payload_data["plan_id"] = plan_id
            if images:
                payload_data["images"] = list(images)
            if project_id:
                payload_data["project_id"] = project_id
            _ws_send({"type": "user_message", "data": payload_data})

            # At most four tool calls run at once. Daemon threads, so quitting the
            # editor never waits for a running tool; the drain below waits for
            # them before returning when the server signals 'done'.
            tool_slots = threading.Semaphore(4)
            tool_threads: List[threading.Thread] = []
            last_tool_result_holder: List[Optional[str]] = [None]

            def _spawn_tool_worker(call_data: Dict[str, Any]) -> None:
                if not on_tool_call:
                    # No handler — synthesize an immediate empty result so the
                    # backend doesn't hang.
                    _ws_send({
                        "type": "tool_result",
                        "data": {
                            "call_id": call_data.get("call_id", ""),
                            "result": "Error: no tool handler",
                        },
                    })
                    return

                def _runner():
                    try:
                        result = on_tool_call(
                            call_data.get("tool_name", ""),
                            call_data.get("tool_args", {}),
                            call_data.get("call_id", ""),
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.error("Tool execution error: %s", exc)
                        # Must start with "Error" -- the bridge never populates
                        # a separate `error` field, so the backend classifies a
                        # failed tool call purely by this prefix
                        # (api/routes/chat.py). "Tool execution error: ..."
                        # sailed through as a success, and a crashed undo was
                        # reported to the user as done.
                        result = f"Error: tool execution failed: {exc}"
                    text = str(result) if result is not None else ""
                    from classes.agent_tools.output import get_last_output, ws_images
                    from classes.agent_tools.receipt import is_error_result, parse_receipt
                    if text and not is_error_result(text):
                        last_tool_result_holder[0] = text
                    # Backend classifies failures by an Error: prefix on `result`
                    # (no separate error field). Success stays full receipt JSON.
                    wire = text
                    receipt = parse_receipt(text)
                    if receipt and receipt.get("status") in ("error", "refused"):
                        wire = str(receipt.get("summary") or text)
                    payload = {
                        "call_id": call_data.get("call_id", ""),
                        "result": wire,
                    }
                    # Sidecar for Assistant vision (backend forwards when ready).
                    try:
                        last = get_last_output()
                        if last is not None:
                            images = ws_images(last)
                            if images:
                                payload["images"] = images
                    except Exception:
                        pass
                    _ws_send({
                        "type": "tool_result",
                        "data": payload,
                    })

                def _bounded():
                    with tool_slots:
                        _runner()

                t = threading.Thread(target=_bounded, daemon=True, name="zenvi-tool-worker")
                tool_threads.append(t)
                t.start()

            # Periodic client-side pings so very long tool runs don't get an
            # idle reset from intermediaries.  Sends through the lock so they
            # never interleave with tool_result frames.
            stop_pings = threading.Event()

            def _ping_loop():
                while not stop_pings.wait(30):
                    try:
                        with send_lock:
                            ws.ping()
                    except Exception:
                        return

            ping_thread = threading.Thread(target=_ping_loop, daemon=True, name="zenvi-ws-ping")
            ping_thread.start()

            final_response: Optional[str] = None
            saw_done = False
            try:
                while True:
                    try:
                        raw = ws.recv()
                    except Exception as exc:  # noqa: BLE001
                        log.debug("WS recv failed: %s", exc)
                        break
                    if not raw:
                        # Empty frame == server closed cleanly.
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    msg_type = msg.get("type", "")
                    data = msg.get("data", {}) or {}

                    if msg_type == "tool_call":
                        _spawn_tool_worker(data)
                    elif msg_type == "tool_started":
                        if on_tool_progress:
                            try:
                                on_tool_progress(
                                    "started",
                                    data.get("call_id", ""),
                                    data.get("tool_name", ""),
                                    data.get("tool_args", {}),
                                )
                            except Exception as exc:  # noqa: BLE001
                                log.debug("on_tool_progress(start) error: %s", exc)
                    elif msg_type == "tool_progress":
                        if on_tool_progress:
                            try:
                                on_tool_progress(
                                    "progress",
                                    data.get("call_id", ""),
                                    data.get("tool_name", ""),
                                    {
                                        "line": data.get("line", ""),
                                        "detail": data.get("detail") or {},
                                    },
                                )
                            except Exception as exc:  # noqa: BLE001
                                log.debug("on_tool_progress error: %s", exc)
                    elif msg_type == "tool_completed":
                        if on_tool_progress:
                            try:
                                on_tool_progress(
                                    "completed",
                                    data.get("call_id", ""),
                                    "",
                                    {
                                        "ok": data.get("ok", False),
                                        "result": data.get("result", ""),
                                    },
                                )
                            except Exception as exc:  # noqa: BLE001
                                log.debug("on_tool_completed error: %s", exc)
                    elif msg_type == "token":
                        if on_token:
                            try:
                                on_token(data.get("text", ""))
                            except Exception as exc:  # noqa: BLE001
                                log.debug("on_token handler error: %s", exc)
                    elif msg_type == "thinking":
                        if on_thinking:
                            try:
                                on_thinking(data.get("text", ""))
                            except Exception as exc:  # noqa: BLE001
                                log.debug("on_thinking handler error: %s", exc)
                    elif msg_type == "assistant_response":
                        final_response = data.get("response", "")
                        if on_response:
                            on_response(final_response, data.get("session_id", ""))
                    elif msg_type in ("plan_ready", "plan_updated", "plan_step_status", "plan_execution_done", "plan_questions", "mode_changed"):
                        if on_plan_event:
                            try:
                                on_plan_event(msg_type, data)
                            except Exception as exc:  # noqa: BLE001
                                log.debug("on_plan_event error: %s", exc)
                    elif msg_type == "error":
                        if on_error:
                            on_error(data.get("message", "Unknown error"))
                        break
                    elif msg_type == "done":
                        saw_done = True
                        break
                    elif msg_type in ("keepalive", "pong"):
                        pass
            finally:
                stop_pings.set()
                # Drain in-flight tool workers so any tool_result frames they
                # emit get sent before we close the WS.
                deadline = time.monotonic() + (1.0 if self._shutting_down else 120)
                for t in tool_threads:
                    t.join(max(0.0, deadline - time.monotonic()))

            try:
                ws.close()
            except Exception:
                pass

            if saw_done or final_response is not None:
                return final_response
            # WS dropped without a proper 'done'.  If a tool already produced
            # a usable result, surface that to the caller.
            return final_response or last_tool_result_holder[0]

        except Exception as e:
            log.error("WebSocket chat failed: %s", e)
            if on_error:
                on_error(str(e))
            return None
        finally:
            try:
                with self._ws_lock:
                    if ws is not None:
                        self._active_wss.discard(ws)
            except Exception:
                pass

    def cancel_current_request(self) -> None:
        """Close the active WebSocket connection, unblocking any pending recv() call.

        Safe to call from any thread. Used during app shutdown to allow the
        chat worker thread to exit cleanly instead of blocking QThread::~QThread().
        """
        self._shutting_down = True
        with self._ws_lock:
            websockets = list(self._active_wss)
            self._active_wss.clear()

        for ws in websockets:
            try:
                ws.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------
    def _new_http_session(self):
        """Thread-safe session for parallel upload + indexing requests."""
        import requests
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        if not self._ssl_verify:
            s.verify = False
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._apply_bearer(s)
        return s

    # ------------------------------------------------------------------
    # Video Generation
    # ------------------------------------------------------------------
    def _provider_key_transport_ok(self) -> bool:
        """Verified HTTPS, or a loopback dev backend where the key never leaves the machine."""
        from urllib.parse import urlparse

        if self._ssl_verify and self.base_url.startswith("https://"):
            return True
        return urlparse(self.base_url).hostname in ("localhost", "127.0.0.1", "::1")

    def generate_video(self, prompt: str, duration_seconds: int = 5, **kwargs) -> Dict[str, Any]:
        """Generate a video from a text prompt (the backend picks the managed provider; 2-15 s).

        Supported kwargs: mode, frame_images_paths, seed_video_file_id,
                          keep_original_sound, width, height, input_video_url,
                          provider. ``provider_key`` (BYOK) travels as a header only.
        """
        if not self._auth_token():
            # Nothing to send: say so now instead of waiting for the server's 401 (which a plan would try to "repair").
            return {"error": LOGIN_REQUIRED_MESSAGE, "auth": True}
        try:
            provider_key = kwargs.pop("provider_key", None)
            if provider_key and not self._provider_key_transport_ok():
                return {"error": _UNVERIFIED_KEY_TRANSPORT}
            headers = {PROVIDER_KEY_HEADER: provider_key} if provider_key else None
            payload = {"prompt": prompt, "duration_seconds": duration_seconds}
            payload.update(kwargs)
            r = self.session.post(
                f"{self.api_url}/generation/video", json=payload, headers=headers, timeout=600,
            )
            if r.status_code == 401:
                log.warning("Video generation rejected the login (401)")
                return {"error": SESSION_REJECTED_MESSAGE, "auth": True}
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return {"error": "Unexpected response from the Zenvi backend"}
            if provider_key and data.get("error"):
                from classes.provider_keys import redact
                data["error"] = redact(str(data["error"]), provider_key)
            return data
        except Exception as e:
            from classes.provider_keys import redact
            err = redact(str(e), provider_key or "")
            log.error("Video generation failed: %s", err)
            return {"error": err}
        finally:
            _refresh_credits_after_backend_billing()

    def generate_image(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """Generate a still image from a text prompt (Zenvi-managed; charged by the backend).

        Supported kwargs: width, height (only the aspect ratio is used).
        Returns ``{"image_base64", "mime_type"}`` or ``{"error"}``.
        """
        try:
            r = self.session.post(
                f"{self.api_url}/generation/image", json={"prompt": prompt, **kwargs}, timeout=300,
            )
            if r.status_code in (402, 429):
                # The backend's credit / tier-cap refusal carries the message to show.
                detail = (r.json() or {}).get("detail")
                message = detail.get("message") if isinstance(detail, dict) else detail
                return {"error": str(message or f"Image generation was refused ({r.status_code}).")}
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return {"error": "Unexpected response from the Zenvi backend"}
            return data
        except Exception as e:
            log.error("Image generation failed: %s", e)
            return {"error": str(e)}
        finally:
            _refresh_credits_after_backend_billing()

    def validate_provider_key(self, provider: str, key: str) -> Dict[str, Any]:
        """Test a user's own provider key against the provider (write-only; never echoed).

        ``{"ok", "error"}`` is the provider's verdict. When the request never got
        that far (backend or network failure) the result also has ``unverified``.
        """
        import requests
        from classes.provider_keys import redact

        if not self._provider_key_transport_ok():
            return {"ok": False, "unverified": True, "error": _UNVERIFIED_KEY_TRANSPORT}
        try:
            r = self.session.post(
                f"{self.api_url}/generation/providers/{provider}/validate",
                json={}, headers={PROVIDER_KEY_HEADER: key}, timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return {"ok": False, "unverified": True, "error": "Unexpected response from the Zenvi backend"}
            return data
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", "?")
            if status == 401:
                return {"ok": False, "unverified": True, "error": "sign in to Zenvi first"}
            try:
                body = e.response.json()
                detail = body.get("error") or body.get("detail")
            except Exception:
                detail = None
            msg = f"Zenvi backend returned {status}" + (f": {detail}" if detail else "")
            return {"ok": False, "unverified": True, "error": redact(msg, key)}
        except Exception as e:
            log.error("Provider key validation failed for %s: %s", provider, type(e).__name__)
            return {
                "ok": False, "unverified": True,
                "error": f"Could not reach the Zenvi backend ({type(e).__name__})",
            }

    def generate_tts(
        self,
        text: str,
        voice: str = "alloy",
        model: str = "tts-1",
        speed: float = 1.0,
    ) -> Dict[str, Any]:
        """Generate narration MP3 via backend OpenAI TTS; returns audio_base64 on success."""
        try:
            payload = {
                "text": text,
                "voice": voice,
                "model": model,
                "speed": speed,
            }
            r = self.session.post(
                f"{self.api_url}/generation/tts",
                json=payload,
                timeout=300,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("TTS generation failed: %s", e)
            return {"success": False, "error": str(e)}

    def generate_morph_video(self, first_image_url: str, last_image_url: str, **kwargs) -> Dict[str, Any]:
        """Generate a morph/transition video between two images."""
        try:
            # Backend schema uses start_image_url / end_image_url
            payload = {"start_image_url": first_image_url, "end_image_url": last_image_url}
            payload.update(kwargs)
            r = self.session.post(f"{self.api_url}/generation/morph", json=payload, timeout=600)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Morph video generation failed: %s", e)
            return {"error": str(e)}
        finally:
            _refresh_credits_after_backend_billing()

    # -- media index v2 -----------------------------------------------------------------
    def _v2(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, *,
            timeout: float = 60, session=None) -> Dict[str, Any]:
        """Call /index/v2/<path>. Never raises: errors come back as ``{"error": ...}`` and
        ``unsupported`` marks a backend that has no v2 routes (the caller then fails the file)."""
        try:
            s = session or self.session
            url = f"{self.api_url}/index/v2{path}"
            r = s.get(url, timeout=timeout) if method == "GET" else s.post(url, json=payload or {}, timeout=timeout)
            if r.status_code in (404, 405):
                return {"unsupported": True, "error": "this backend has no media index v2"}
            if r.status_code == 401:
                return {"auth": True, "error": "sign in to use the media index"}
            if r.status_code in (402, 403, 429):
                return _v2_refusal(r)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                return {"error": "unexpected response"}
            billing = data.get("billing") or (data.get("result") or {}).get("billing")
            if isinstance(billing, dict) and billing.get("credits"):
                _refresh_credits_after_backend_billing()          # the backend charged for this: repaint the badge
            return data
        except Exception as exc:
            log.warning("media index v2 %s %s failed: %s", method, path, exc)
            return {"error": str(exc)}

    def v2_upload_session(self, file_id: str, filename: str, total_size: int, mime_type: str = "video/mp4",
                          session=None) -> Dict[str, Any]:
        return self._v2("POST", "/upload-session", {"file_id": file_id, "filename": filename,
                                                    "total_size": int(total_size), "mime_type": mime_type}, session=session)

    def v2_understand(self, file_name: str, file_uri: str, shots: List[Dict[str, Any]],
                      transcript: List[Dict[str, Any]], mime_type: str = "video/mp4",
                      media_type: str = "video", session=None) -> Dict[str, Any]:
        return self._v2("POST", "/understand", {"file_name": file_name, "file_uri": file_uri, "mime_type": mime_type,
                                                "media_type": media_type, "shots": shots, "transcript": transcript},
                        timeout=120, session=session)

    def v2_describe_audio(self, file_name: str, file_uri: str, duration_seconds: float, windows: List[Dict[str, Any]],
                          mime_type: str = "audio/aac", session=None) -> Dict[str, Any]:
        return self._v2("POST", "/describe-audio", {"file_name": file_name, "file_uri": file_uri, "mime_type": mime_type,
                                                    "duration_seconds": float(duration_seconds), "windows": windows},
                        timeout=120, session=session)

    def v2_listen(self, file_name: str, file_uri: str, duration_seconds: float, context: Optional[Dict[str, Any]] = None,
                  mime_type: str = "audio/aac", session=None) -> Dict[str, Any]:
        return self._v2("POST", "/listen", {"file_name": file_name, "file_uri": file_uri, "mime_type": mime_type,
                                            "duration_seconds": float(duration_seconds), "context": context or {}},
                        timeout=120, session=session)

    def v2_job(self, job_id: str, session=None) -> Dict[str, Any]:
        return self._v2("GET", f"/job/{job_id}", timeout=15, session=session)

    def v2_embed(self, items: List[Dict[str, Any]], dims: int = 768, task_type: Optional[str] = None,
                 session=None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"items": items, "dims": int(dims)}
        if task_type:
            payload["task_type"] = task_type
        return self._v2("POST", "/embed", payload, timeout=180, session=session)

    @staticmethod
    def _empty_ai_metadata() -> Dict[str, Any]:
        """Return a default empty ai_metadata dict (Gemini Flash summary shape)."""
        return {
            "analyzed": False,
            "provider": "gemini-flash",
            "short_summary": "",
            "description": "",
            "sounds": "",
            "transcript": "",
            "chapters": [],
            "scene_descriptions": [],
            "tags": {},
            "index": {},
            "twelvelabs": {},
        }

    # ------------------------------------------------------------------
    # Pexels stock video
    # ------------------------------------------------------------------
    def pexels_search(self, query: str, per_page: int = 15, page: int = 1) -> Dict[str, Any]:
        """Search Pexels for stock videos."""
        try:
            r = self.session.get(
                f"{self.api_url}/pexels/search",
                params={"query": query, "per_page": per_page, "page": page},
                timeout=20,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Pexels search failed: %s", e)
            return {"videos": [], "error": str(e)}

    def pexels_download(self, video_id: int, link: str, filename: str = "") -> Dict[str, Any]:
        """Download a Pexels MP4 from the CDN URL to the local machine."""
        hint = filename or f"pexels_{video_id}"
        return self._download_url_to_temp(link, ".mp4", filename_hint=hint, timeout=180)

    # ------------------------------------------------------------------
    # Freesound stock music / SFX
    # ------------------------------------------------------------------
    def freesound_search(self, query: str, page_size: int = 15, page: int = 1) -> Dict[str, Any]:
        """Search Freesound for stock music and sound effects."""
        try:
            r = self.session.get(
                f"{self.api_url}/freesound/search",
                params={"query": query, "page_size": page_size, "page": page},
                timeout=20,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Freesound search failed: %s", e)
            return {"sounds": [], "error": str(e)}

    def freesound_download(self, sound_id: int, preview_url: str, filename: str = "") -> Dict[str, Any]:
        """Download a Freesound preview to the local machine.

        Freesound serves each sound in several preview renditions and any one of
        them can 404 while the others are fine, so fall back through the variants
        instead of failing the whole stock_music run.
        """
        hint = filename or f"freesound_{sound_id}"
        url = str(preview_url or "").strip()
        if not url:
            return {"success": False, "error": "No preview URL"}

        candidates = [url]
        for old_part, new_part in (
            ("-hq.mp3", "-lq.mp3"), ("-lq.mp3", "-hq.mp3"),
            ("-hq.ogg", "-lq.ogg"), ("-lq.ogg", "-hq.ogg"),
        ):
            if old_part in url:
                candidates.append(url.replace(old_part, new_part))
        # Different container as a last resort.
        if "-hq.mp3" in url:
            candidates.append(url.replace("-hq.mp3", "-hq.ogg"))

        last = {"success": False, "error": "No preview URL"}
        for candidate in candidates:
            ext = ".ogg" if candidate.endswith(".ogg") else ".mp3"
            last = self._download_url_to_temp(
                candidate, ext, filename_hint=hint, timeout=180,
            )
            if last.get("success"):
                return last
            log.warning("Freesound preview failed (%s): %s", candidate, last.get("error"))
        return last


# Singleton
_client: Optional[ZenviBackendClient] = None
_client_url: Optional[str] = None


def get_backend_client(reset: bool = False) -> ZenviBackendClient:
    """Get the singleton backend client (refreshed when backend URL changes)."""
    global _client, _client_url
    url = ZenviBackendClient._get_backend_url()
    if reset or _client is None or _client_url != url:
        _client = ZenviBackendClient(base_url=url)
        _client_url = url
    return _client
