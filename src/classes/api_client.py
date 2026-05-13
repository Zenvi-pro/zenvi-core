"""
Zenvi Backend API Client.

Drop-in replacement for direct AI class imports. The frontend uses this client
to communicate with the separate zenvi-backend server instead of running LLM /
provider / agent code in-process.

Usage:
    from classes.api_client import ZenviBackendClient
    client = ZenviBackendClient()  # reads ZENVI_BACKEND_URL from settings

    # Chat
    response = client.send_message("add a clip to the timeline")

    # Models
    models = client.list_models()

    # Search
    results = client.search("sunset")

    # Tags
    tags = client.get_tags(file_id)

    # Generation
    result = client.generate_video("a cat running on a beach")
"""

import json
import os
import threading
from typing import Any, Dict, List, Optional, Callable
from classes.logger import log

# Load .env so ZENVI_BACKEND_URL is available early
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_DEFAULT_BACKEND_URL = "https://api.zenvi.pro"


class ZenviBackendClient:
    """HTTP/WebSocket client for the Zenvi backend API."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or self._get_backend_url()).rstrip("/")
        self.api_url = f"{self.base_url}/api/v1"
        self._session = None
        self._active_wss = set()  # active WebSockets during parallel chat requests
        self._ws_lock = threading.Lock()
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
    def list_models(self) -> List[Dict[str, str]]:
        """List all available LLM models."""
        try:
            r = self.session.get(f"{self.api_url}/models", timeout=10)
            r.raise_for_status()
            data = r.json()
            return data.get("models", [])
        except Exception as e:
            log.error("Failed to list models: %s", e)
            return []

    def list_available_models(self) -> List[Dict[str, str]]:
        """List models with valid API keys."""
        try:
            r = self.session.get(f"{self.api_url}/models/available", timeout=10)
            r.raise_for_status()
            data = r.json()
            return data.get("models", [])
        except Exception as e:
            log.error("Failed to list available models: %s", e)
            return []

    def get_default_model_id(self) -> str:
        """Get the default model ID."""
        try:
            r = self.session.get(f"{self.api_url}/models", timeout=10)
            r.raise_for_status()
            return r.json().get("default_model_id", "openai/gpt-4o-mini")
        except Exception:
            return "openai/gpt-4o-mini"

    # ------------------------------------------------------------------
    # Chat (synchronous REST)
    # ------------------------------------------------------------------
    def send_message(
        self,
        message: str,
        model_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a chat message and get a response."""
        try:
            payload = {"message": message}
            if model_id:
                payload["model_id"] = model_id
            if context:
                payload["context"] = context
            if session_id:
                payload["session_id"] = session_id

            r = self.session.post(f"{self.api_url}/chat", json=payload, timeout=600)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Chat request failed: %s", e)
            return {"response": f"Error communicating with backend: {e}", "session_id": session_id or ""}

    def get_chat_history(self, session_id: str) -> Dict[str, Any]:
        """Get conversation history."""
        try:
            r = self.session.get(f"{self.api_url}/chat/history/{session_id}", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Get history failed: %s", e)
            return {"messages": [], "session_info": {}}

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

            _ws_send({
                "type": "user_message",
                "data": {
                    "message": message,
                    "model_id": model_id,
                    "session_id": session_id,
                },
            })

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
    ) -> Dict[str, Any]:
        """Search for clips matching a query."""
        try:
            payload: Dict[str, Any] = {"query": query, "top_k": top_k}
            if index_id:
                payload["index_id"] = index_id
            if video_id:
                payload["video_id"] = video_id
            if page_limit:
                payload["page_limit"] = page_limit
            r = self.session.post(f"{self.api_url}/search", json=payload, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Search failed: %s", e)
            return {"results": [], "error": str(e)}

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------
    def index_video(self, file_path: str, index_name: str, filename: Optional[str] = None,
                    async_mode: bool = True) -> Dict[str, Any]:
        """Index a video file.

        Always uses the background-job pattern: POST returns a job_id immediately,
        then we poll GET /indexing/job/{job_id} until complete. This avoids the
        read-timeout that occurred when TwelveLabs took longer than the HTTP timeout.
        The async_mode parameter is kept for backwards compatibility but ignored.
        """
        try:
            payload: Dict[str, Any] = {"file_path": file_path, "index_name": index_name}
            if filename:
                payload["filename"] = filename
            r = self.session.post(f"{self.api_url}/indexing", json=payload, timeout=30)
            r.raise_for_status()
            job_id = r.json().get("job_id")
            if not job_id:
                return {"success": False, "message": "Backend returned no job_id"}
            return self._poll_indexing_job(job_id)
        except Exception as e:
            log.error("Indexing failed: %s", e)
            return {"success": False, "message": str(e)}

    def _poll_indexing_job(self, job_id: str, max_wait: int = 1800, poll_interval: int = 10) -> Dict[str, Any]:
        """Poll /indexing/job/{job_id} until the job finishes or max_wait seconds pass."""
        import time
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                r = self.session.get(f"{self.api_url}/indexing/job/{job_id}", timeout=15)
                r.raise_for_status()
                data = r.json()
                status = data.get("status", "running")
                if status == "done":
                    return data.get("result") or {"success": True}
                if status == "failed":
                    result = data.get("result") or {}
                    return {"success": False, "message": result.get("error", "Indexing failed")}
                if status == "not_found":
                    return {"success": False, "message": f"Job {job_id} not found on backend"}
            except Exception as e:
                log.warning("Indexing poll error (will retry): %s", e)
            time.sleep(poll_interval)
        return {"success": False, "message": f"Indexing job {job_id} timed out after {max_wait}s"}

    # ------------------------------------------------------------------
    # Video Generation
    # ------------------------------------------------------------------
    def generate_video(self, prompt: str, duration_seconds: int = 4, **kwargs) -> Dict[str, Any]:
        """Generate a video from a text prompt.

        Supported kwargs: input_image_path, seed_video, strength, frame_images,
                          model, width, height, input_video_url.
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

    def research_web(self, query: str, max_images: int = 3, **kwargs) -> Dict[str, Any]:
        """Search the web via Perplexity through the backend."""
        try:
            payload = {"query": query, "max_images": max_images}
            payload.update(kwargs)
            r = self.session.post(f"{self.api_url}/research/search", json=payload, timeout=180)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Research failed: %s", e)
            return {"error": str(e)}

    def research_plan(self, topic: str, content_type: str = "video", aspects: str = "", **kwargs) -> Dict[str, Any]:
        """Research a topic for content planning."""
        try:
            payload = {"query": topic, "content_type": content_type, "aspects": aspects}
            payload.update(kwargs)
            r = self.session.post(f"{self.api_url}/research/plan", json=payload, timeout=180)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Research plan failed: %s", e)
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------
    def get_tags(self, file_id: str) -> Dict[str, Any]:
        try:
            r = self.session.get(f"{self.api_url}/tags/{file_id}", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Get tags failed: %s", e)
            return {}

    def update_tags(self, file_id: str, tags: Dict[str, Any]) -> bool:
        try:
            r = self.session.post(f"{self.api_url}/tags", json={"file_id": file_id, "tags": tags}, timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def search_by_tag(self, tag_value: str, tag_type: Optional[str] = None) -> List[str]:
        try:
            payload = {"tag_value": tag_value}
            if tag_type:
                payload["tag_type"] = tag_type
            r = self.session.post(f"{self.api_url}/tags/search", json=payload, timeout=10)
            r.raise_for_status()
            return r.json().get("file_ids", [])
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Faces
    # ------------------------------------------------------------------
    def list_people(self) -> List[Dict[str, Any]]:
        try:
            r = self.session.get(f"{self.api_url}/faces/people", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return []

    def create_person(self, person_id: str, name: str = "") -> Dict[str, Any]:
        try:
            r = self.session.post(f"{self.api_url}/faces/people", json={"person_id": person_id, "name": name}, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Collections
    # ------------------------------------------------------------------
    def list_collections(self) -> List[Dict[str, Any]]:
        try:
            r = self.session.get(f"{self.api_url}/collections", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return []

    def create_collection(self, collection_id: str, name: str, collection_type: str = "manual") -> Dict[str, Any]:
        try:
            r = self.session.post(f"{self.api_url}/collections", json={
                "collection_id": collection_id, "name": name, "collection_type": collection_type,
            }, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------
    def media_command(self, command: str) -> Dict[str, Any]:
        """Process a natural-language media management command."""
        try:
            r = self.session.post(f"{self.api_url}/media/command", params={"command": command}, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Media command failed: %s", e)
            return {"success": False, "message": str(e)}

    def get_media_statistics(self) -> Dict[str, Any]:
        try:
            r = self.session.get(f"{self.api_url}/media/statistics", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Analysis queue
    # ------------------------------------------------------------------
    def get_analysis_status(self) -> Dict[str, Any]:
        """Get the analysis queue status from the backend."""
        try:
            r = self.session.get(f"{self.api_url}/media/analysis/status", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Get analysis status failed: %s", e)
            return {"pending": 0, "processing": 0, "total": 0, "current_file": "", "queue": []}

    def start_analysis(self) -> Dict[str, Any]:
        """Start processing the analysis queue."""
        try:
            r = self.session.post(f"{self.api_url}/media/analysis/start", timeout=120)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Start analysis failed: %s", e)
            return {"success": False, "message": str(e)}

    def clear_analysis_queue(self) -> Dict[str, Any]:
        """Clear the analysis queue."""
        try:
            r = self.session.post(f"{self.api_url}/media/analysis/clear", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Clear analysis queue failed: %s", e)
            return {"success": False, "message": str(e)}

    # ------------------------------------------------------------------
    # Tagging & Indexing (for files_model)
    # ------------------------------------------------------------------
    def tag_video(self, video_path: str, file_id: str = "", session=None) -> Dict[str, Any]:
        """Send a video to the backend for AI tagging/analysis (replaces GeminiVideoTagger)."""
        try:
            s = session or self.session
            payload: Dict[str, Any] = {"video_path": video_path}
            if file_id:
                payload["file_id"] = file_id
            r = s.post(
                f"{self.api_url}/tags/analyze",
                json=payload,
                timeout=180,  # frame extraction + Gemini upload + inference can take > 30s
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Video tagging failed: %s", e)
            return self._empty_ai_metadata()

    def index_video_for_search(
        self,
        file_path: str,
        index_name: str,
        filename: str = "",
        existing_index_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Index a video via the backend. Uses the job-based async pattern to avoid read timeouts."""
        try:
            r = self.session.post(
                f"{self.api_url}/indexing/index",
                json={
                    "file_path": file_path,
                    "index_name": index_name,
                    "filename": filename,
                    "existing_index_id": existing_index_id,
                },
                timeout=30,
            )
            r.raise_for_status()
            job_id = r.json().get("job_id")
            if not job_id:
                return {"error": "Backend returned no job_id"}
            return self._poll_indexing_job(job_id)
        except Exception as e:
            log.error("Video indexing failed: %s", e)
            return {"error": str(e)}

    def delete_indexed_video(self, index_id: str, video_id: str) -> bool:
        """Delete a video from the search index (replaces twelvelabs delete_video_from_index)."""
        try:
            r = self.session.delete(
                f"{self.api_url}/indexing/video",
                params={"index_id": index_id, "video_id": video_id},
                timeout=30,
            )
            return r.status_code == 200
        except Exception:
            return False

    def is_indexing_configured(self) -> bool:
        """Check whether the backend has video indexing configured."""
        try:
            r = self.session.get(f"{self.api_url}/indexing/status", timeout=5)
            return r.status_code == 200 and r.json().get("configured", False)
        except Exception:
            return False

    @staticmethod
    def _empty_ai_metadata() -> Dict[str, Any]:
        """Return a default empty ai_metadata dict (mirrors old GeminiVideoTagger.empty_metadata)."""
        from datetime import datetime
        return {
            "analyzed": False,
            "analysis_version": "2.0",
            "analysis_date": datetime.now().isoformat(),
            "provider": "backend",
            "scene_descriptions": [],
            "tags": {"objects": [], "scenes": [], "activities": [], "mood": [], "quality": {}},
            "faces": [],
            "colors": {},
            "audio_analysis": {},
            "description": "",
            "confidence": 0.0,
        }

    def queue_file_for_analysis(self, file_id: str, file_path: str, media_type: str = "video") -> Dict[str, Any]:
        """Add a file to the backend analysis queue."""
        try:
            r = self.session.post(
                f"{self.api_url}/media/analysis/queue",
                json={"file_id": file_id, "file_path": file_path, "media_type": media_type},
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Queue file for analysis failed: %s", e)
            return {"success": False, "message": str(e)}

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
        """Download a Pexels video via the backend and return its local path."""
        try:
            r = self.session.post(
                f"{self.api_url}/pexels/download",
                json={"video_id": video_id, "link": link, "filename": filename},
                timeout=300,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Pexels download failed: %s", e)
            return {"local_path": "", "error": str(e)}

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
    # Re-tagging and re-indexing (manual triggers)
    # ------------------------------------------------------------------
    def retag_video(self, file_id: str, file_path: str, force: bool = True) -> Dict[str, Any]:
        """Re-run Gemini tagging for a file. Clips > 30 min are rejected by the backend."""
        try:
            r = self.session.post(
                f"{self.api_url}/tags/retag",
                json={"file_id": file_id, "file_path": file_path, "force": force},
                timeout=300,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("retag_video failed: %s", e)
            return {"success": False, "error": str(e)}

    def reindex_video(self, file_id: str, file_path: str, index_name: str = "zenvi-videos",
                      existing_index_id: str = "") -> Dict[str, Any]:
        """Re-index a video in TwelveLabs. Clips > 30 min are rejected by the backend."""
        try:
            payload: Dict[str, Any] = {
                "file_id": file_id,
                "file_path": file_path,
                "index_name": index_name,
                "force": True,
            }
            if existing_index_id:
                payload["existing_index_id"] = existing_index_id
            r = self.session.post(
                f"{self.api_url}/indexing/reindex",
                json=payload,
                timeout=600,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("reindex_video failed: %s", e)
            return {"success": False, "error": str(e)}

    def freesound_download(self, sound_id: int, preview_url: str, filename: str = "") -> Dict[str, Any]:
        """Download a Freesound HQ MP3 preview via the backend and return its local path."""
        try:
            r = self.session.post(
                f"{self.api_url}/freesound/download",
                json={"sound_id": sound_id, "preview_url": preview_url, "filename": filename},
                timeout=120,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("Freesound download failed: %s", e)
            return {"local_path": "", "error": str(e)}


# Singleton
_client: Optional[ZenviBackendClient] = None


def get_backend_client() -> ZenviBackendClient:
    """Get the singleton backend client."""
    global _client
    if _client is None:
        _client = ZenviBackendClient()
    return _client
