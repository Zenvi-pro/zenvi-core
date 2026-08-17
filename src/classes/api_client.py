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

    # Search
    results = client.search("sunset")

    # Generation
    result = client.generate_video("a cat running on a beach")
"""

import json
import os
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Callable, Tuple
from classes.logger import log

from classes.zenvi_env import load_zenvi_dotenv

load_zenvi_dotenv()

_DEFAULT_BACKEND_URL = "https://api.zenvi.pro"


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
        """Lazy-create a requests.Session."""
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
        return self._session

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

    def cleanup_backend_upload(self, file_id: str, session=None) -> None:
        """Remove a temp upload on the backend after tag/index complete."""
        if not file_id:
            return
        try:
            s = session or self.session
            r = s.delete(f"{self.api_url}/media/upload/{file_id}", timeout=30)
            r.raise_for_status()
        except Exception as exc:
            log.debug("Backend upload cleanup failed for %s: %s", file_id, exc)

    def _post_media_multipart(
        self,
        endpoint: str,
        local_path: str,
        file_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
        session=None,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """POST multipart with file to a media endpoint; return parsed JSON."""
        if not os.path.isfile(local_path):
            return {"error": f"File not found: {local_path}"}
        extra = dict(extra or {})
        fid = file_id or extra.pop("file_id", None) or uuid.uuid4().hex
        name = extra.pop("filename", None) or os.path.basename(local_path)
        data = {"file_id": fid, "filename": name, **{k: v for k, v in extra.items() if v is not None}}
        try:
            s = session or self.session
            with open(local_path, "rb") as fh:
                r = s.post(
                    f"{self.api_url}{endpoint}",
                    files={"file": (name, fh)},
                    data=data,
                    headers=self._multipart_headers(s),
                    timeout=timeout,
                )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.error("Multipart POST %s failed: %s", endpoint, exc)
            return {"error": str(exc)}

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
        agent_mode: Optional[str] = None,
        action: Optional[str] = None,
        plan_id: Optional[str] = None,
        on_plan_event: Optional[Callable] = None,
    ) -> Optional[str]:
        """
        Send a chat message via WebSocket with tool delegation support.

        Each incoming ``tool_call`` is dispatched to its own worker thread so
        the agent can fan out N concurrent tool calls and we ack them as soon
        as each one finishes.  The recv loop never blocks on tool execution.
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
            _ws_send({"type": "user_message", "data": payload_data})

            # Track outstanding tool worker threads so we can drain them
            # before returning when the server signals 'done'.
            tool_workers: List[threading.Thread] = []
            tool_workers_lock = threading.Lock()
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
                        result = f"Tool execution error: {exc}"
                    text = str(result) if result is not None else ""
                    if text and not text.startswith("Error"):
                        last_tool_result_holder[0] = text
                    _ws_send({
                        "type": "tool_result",
                        "data": {
                            "call_id": call_data.get("call_id", ""),
                            "result": text,
                        },
                    })

                t = threading.Thread(target=_runner, daemon=True, name="zenvi-tool-worker")
                with tool_workers_lock:
                    tool_workers.append(t)
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
                with tool_workers_lock:
                    pending = list(tool_workers)
                if self._shutting_down:
                    import time as _time
                    deadline = _time.time() + 1.0
                    for t in pending:
                        remaining = deadline - _time.time()
                        if remaining <= 0:
                            break
                        t.join(timeout=remaining)
                else:
                    for t in pending:
                        t.join(timeout=120)

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
    # Search
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        top_k: int = 5,
        index_id: Optional[str] = None,
        video_id: Optional[str] = None,
        page_limit: Optional[int] = None,
        media_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search for clips matching a query."""
        try:
            effective_top_k = top_k
            if page_limit and page_limit > effective_top_k:
                effective_top_k = min(int(page_limit), 50)
            effective_top_k = min(effective_top_k, 50)
            payload: Dict[str, Any] = {"query": query, "top_k": effective_top_k}
            if index_id:
                payload["index_id"] = index_id
            if video_id:
                payload["video_id"] = video_id
            if page_limit:
                payload["page_limit"] = page_limit
            if media_type:
                payload["media_type"] = media_type
            r = self.session.post(f"{self.api_url}/search", json=payload, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Search failed: %s", e)
            return {"results": [], "error": str(e)}

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
        return s

    def start_direct_indexing_job(
        self,
        file_path: str,
        index_name: str,
        file_id: str = "",
        filename: str = "",
        existing_index_id: Optional[str] = None,
        session=None,
        progress_callback: Optional[Callable[[str, int], None]] = None,
        project_id: str = "",
        duration_sec: float = 0.0,
        force: bool = False,
        media_type: str = "video",
    ) -> Dict[str, Any]:
        """Index via editor-side chunks + Gemini Files direct upload (no backend media store)."""
        from classes.index_chunker import extract_chunks, cleanup_chunk_dir, guess_mime
        from classes.gemini_direct_upload import upload_file_to_gemini_resumable

        if not file_path or not os.path.isfile(file_path):
            return {"success": False, "error": f"File not found: {file_path}"}

        mt = (media_type or "video").strip().lower() or "video"
        if mt not in ("video", "image", "audio"):
            mt = "video"
        # Guard mislabeled imports (libopenshot often sets has_video on MP3).
        _audio_exts = (
            ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma",
            ".opus", ".aiff", ".aif", ".oga",
        )
        if mt != "audio" and os.path.splitext(file_path or "")[1].lower() in _audio_exts:
            mt = "audio"
        name = filename or os.path.basename(file_path) or file_id or "media"
        fid = file_id or uuid.uuid4().hex
        s = session or self._new_http_session()
        work_dir = ""

        pid = (project_id or "").strip()
        if not pid and str(index_name or "").startswith("zenvi-"):
            pid = str(index_name)[len("zenvi-"):]

        try:
            if progress_callback:
                progress_callback("planning", 0)

            duration = float(duration_sec or 0)
            if mt == "image":
                duration = 0.0
            elif duration <= 0:
                try:
                    import subprocess
                    from classes.ffmpeg_cli import run_ffmpeg
                    proc = run_ffmpeg(
                        [
                            "ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", file_path,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    duration = float((proc.stdout or "0").strip() or 0)
                except Exception:
                    duration = 0.0
            if mt != "image" and duration <= 0:
                return {"success": False, "error": f"Could not determine {mt} duration"}

            pr = s.post(
                f"{self.api_url}/indexing/plan-chunks",
                json={"duration_sec": duration, "media_type": mt},
                timeout=30,
            )
            pr.raise_for_status()
            plan_data = pr.json()
            if plan_data.get("error"):
                return {"success": False, "error": plan_data["error"]}
            plan = plan_data.get("chunks") or []
            if not plan:
                return {"success": False, "error": "Empty chunk plan from backend"}

            if progress_callback:
                progress_callback("chunking", 5)
            chunk_infos, work_dir, chunk_err = extract_chunks(
                file_path, plan, media_type=mt,
            )
            if chunk_err:
                return {"success": False, "error": chunk_err}

            job_id = ""
            uploaded_chunks = []
            total = len(chunk_infos)
            for i, info in enumerate(chunk_infos):
                mime = str(info.get("mime_type") or guess_mime(info["path"], mt))
                payload: Dict[str, Any] = {
                    "file_id": fid,
                    "project_id": pid,
                    "index_name": index_name,
                    "filename": name,
                    "total_size": int(info["size"]),
                    "mime_type": mime,
                    "media_type": mt,
                    "chunk_index": int(info["chunk_index"]),
                    "start_ts": float(info["start"]),
                    "end_ts": float(info["end"]),
                }
                if job_id:
                    payload["job_id"] = job_id
                if existing_index_id:
                    payload["existing_index_id"] = existing_index_id

                r = s.post(f"{self.api_url}/indexing/upload-session", json=payload, timeout=60)
                r.raise_for_status()
                session_data = r.json()
                if session_data.get("error"):
                    return {"success": False, "error": session_data["error"]}
                job_id = str(session_data.get("job_id") or job_id)
                upload_url = str(session_data.get("upload_url") or "")
                if not upload_url:
                    urls = session_data.get("presigned_urls") or []
                    if urls:
                        upload_url = str(urls[0].get("url") or "")
                if not job_id or not upload_url:
                    return {"success": False, "error": "Invalid upload-session response"}

                file_info, up_err = upload_file_to_gemini_resumable(
                    info["path"],
                    upload_url,
                    mime_type=mime,
                )
                if up_err:
                    return {"success": False, "error": up_err}

                uploaded_chunks.append({
                    "chunk_index": int(info["chunk_index"]),
                    "gemini_file_name": str(file_info.get("name") or ""),
                    "gemini_file_uri": str(file_info.get("uri") or ""),
                    "start_ts": float(info["start"]),
                    "end_ts": float(info["end"]),
                    "size": int(info["size"]),
                    "mime_type": mime,
                    "media_type": mt,
                })
                if progress_callback and total > 0:
                    progress_callback("uploading", int((i + 1) * 80 / total))

            cr = s.post(
                f"{self.api_url}/indexing/upload-complete",
                json={
                    "job_id": job_id,
                    "chunks": uploaded_chunks,
                    "force": bool(force),
                    "media_type": mt,
                },
                timeout=60,
            )
            cr.raise_for_status()
            complete = cr.json()
            if not complete.get("success"):
                return {"success": False, "error": complete.get("error", "upload-complete failed")}

            if progress_callback:
                progress_callback("indexing", -1)
            result = self._poll_indexing_job(job_id, progress_callback=progress_callback)
            if isinstance(result, dict) and result.get("video_id") and not result.get("error"):
                result.setdefault("status", "ready")
                result["success"] = True
            return result
        except Exception as exc:
            log.error("Gemini indexing failed: %s", exc)
            return {"success": False, "error": str(exc)}
        finally:
            if work_dir:
                cleanup_chunk_dir(work_dir)

    def _poll_indexing_job(
        self,
        job_id: str,
        max_wait: int = 1800,
        poll_interval: int = 10,
        progress_callback: Optional[Callable[[str, int], None]] = None,
    ) -> Dict[str, Any]:
        """Poll /indexing/job/{job_id} until the job finishes or max_wait seconds pass."""
        import time
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if progress_callback:
                progress_callback("indexing", -1)
            try:
                r = self.session.get(f"{self.api_url}/indexing/job/{job_id}", timeout=15)
                r.raise_for_status()
                data = r.json()
                status = data.get("status", "running")
                if status == "done":
                    return data.get("result") or {"success": True}
                if status == "failed":
                    result = data.get("result") or {}
                    err = result.get("error", "Indexing failed") if isinstance(result, dict) else "Indexing failed"
                    return {"success": False, "error": err, "message": err}
                if status == "not_found":
                    return {"success": False, "message": f"Job {job_id} not found on backend"}
            except Exception as e:
                log.warning("Indexing poll error (will retry): %s", e)
            time.sleep(poll_interval)
        return {"success": False, "message": f"Indexing job {job_id} timed out after {max_wait}s"}

    # ------------------------------------------------------------------
    # Video Generation
    # ------------------------------------------------------------------
    def generate_video(self, prompt: str, duration_seconds: int = 5, **kwargs) -> Dict[str, Any]:
        """Generate a video from a text prompt (Kling O1 Pro via Runware).

        Supported kwargs: mode, frame_images_paths, seed_video_file_id,
                          keep_original_sound, width, height, input_video_url.
        """
        try:
            payload = {"prompt": prompt, "duration_seconds": duration_seconds}
            payload.update(kwargs)
            r = self.session.post(f"{self.api_url}/generation/video", json=payload, timeout=600)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Video generation failed: %s", e)
            return {"error": str(e)}

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

    # ------------------------------------------------------------------
    # Indexing & Pegasus summarize (for files_model)
    # ------------------------------------------------------------------
    def summarize_indexed_video(
        self,
        video_id: str,
        *,
        file_id: str = "",
        index_id: str = "",
        index_name: str = "",
        session=None,
    ) -> Dict[str, Any]:
        """Generate Pegasus audiovisual summary for an indexed TwelveLabs video_id."""
        payload = {
            "video_id": str(video_id or "").strip(),
            "file_id": str(file_id or ""),
            "index_id": str(index_id or "") or None,
            "index_name": str(index_name or ""),
        }
        try:
            s = session or self.session
            r = s.post(
                f"{self.api_url}/indexing/summarize",
                json=payload,
                timeout=300,
            )
            r.raise_for_status()
            data = r.json() if isinstance(r.json(), dict) else {}
            meta = data.get("ai_metadata") if isinstance(data.get("ai_metadata"), dict) else None
            if meta and (meta.get("analyzed") or data.get("success")):
                return meta
            out = self._empty_ai_metadata()
            out["error"] = data.get("error") or (meta or {}).get("error") or "Summarize did not complete"
            if meta and isinstance(meta.get("twelvelabs"), dict):
                out["twelvelabs"] = meta["twelvelabs"]
            return out
        except Exception as exc:
            log.error("Pegasus summarize failed: %s", exc)
            meta = self._empty_ai_metadata()
            meta["error"] = str(exc)
            return meta

    def is_indexing_configured(self) -> bool:
        """Check whether the backend has video indexing configured."""
        try:
            r = self.session.get(f"{self.api_url}/indexing/status", timeout=5)
            return r.status_code == 200 and r.json().get("configured", False)
        except Exception:
            return False

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

    def get_project_catalog(self, project_id: str) -> Dict[str, Any]:
        """Orientation catalog for a project (no vector search)."""
        try:
            r = self.session.get(
                f"{self.api_url}/indexing/catalog",
                params={"project_id": project_id},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Catalog fetch failed: %s", e)
            return {"items": [], "error": str(e)}

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

    # ------------------------------------------------------------------
    # Re-indexing (manual trigger via agent tools)
    # ------------------------------------------------------------------
    def reindex_video(
        self,
        file_id: str,
        file_path: str,
        index_name: str = "zenvi-videos",
        existing_index_id: str = "",
        force: bool = False,
        session=None,
    ) -> Dict[str, Any]:
        """Re-index via direct TwelveLabs presigned upload."""
        if isinstance(force, str):
            force = force.strip().lower() in ("true", "1", "yes", "force")
        if not force and not file_path:
            return {"success": False, "error": "file_path is required"}

        result = self.start_direct_indexing_job(
            file_path,
            index_name,
            file_id=file_id,
            filename=os.path.basename(file_path) if file_path else "",
            existing_index_id=existing_index_id or None,
            session=session,
        )
        if result.get("index_id"):
            return {
                "success": True,
                "file_id": file_id,
                "index_id": result.get("index_id"),
                "video_id": result.get("video_id"),
            }
        return {
            "success": False,
            "file_id": file_id,
            "error": result.get("error") or result.get("message") or "Re-index failed",
        }

    def freesound_download(self, sound_id: int, preview_url: str, filename: str = "") -> Dict[str, Any]:
        """Download a Freesound preview MP3 from the CDN URL to the local machine."""
        hint = filename or f"freesound_{sound_id}"
        return self._download_url_to_temp(preview_url, ".mp3", filename_hint=hint, timeout=180)


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
