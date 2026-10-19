"""
 @file
 @brief The model that turns pictures, speech, sound and words into search vectors.

 One interface, two models, chosen in Preferences ("Footage search model"):

 * ``LocalEmbedder``: EmbeddingGemma 2 on this computer, in a Node worker (``worker/embed_worker.mjs``) running
   Transformers.js on WebGPU, or on the CPU when the GPU fails its self-test. Free and offline once the model is
   downloaded (on first use, into ``~/.openshot_qt/models``; it is never shipped with the app).
 * ``CloudEmbedder``: Gemini Embedding 2 through the backend's ``/index/v2/embed`` route, billed in credits.

 Both return one 768-number unit vector per input. Vectors of two models are never compared: every file's
 layer records which model made it, and a query is embedded with that file's model.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import wave
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from classes.logger import log
from classes.media_index import schema as S

MODEL_LOCAL = "embeddinggemma-2"
MODEL_CLOUD = "gemini-embedding-2"
SETTING = "media-index-embedding-model"          # "local" (default) or "cloud"
DESCRIBE_SETTING = "media-index-describe-on-import"
QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: none | text: "
SOFT_TOKENS = 70                                  # the image budget the worker sets; keyframes are cut to fit it exactly
PATCH = 48                                        # 16-pixel patches pooled 3 by 3
IMAGE_BATCH, TEXT_BATCH, AUDIO_BATCH, CLOUD_BATCH = 6, 16, 4, 48
IDLE_SECONDS = 120.0
READY_TIMEOUT = 45 * 60                           # the first start downloads the model
REQUEST_TIMEOUT = 15 * 60
AUDIO_RATE = 16000

Window = Tuple[float, float, np.ndarray]          # (start, end, 16 kHz mono float32 samples)


class EmbedError(Exception):
    """The model could not embed something. ``flags`` carries the backend's auth/credits/rate_limited markers."""

    def __init__(self, message: str, **flags: Any) -> None:
        super().__init__(message)
        self.flags = flags


def keyframe_size(width: float, height: float, budget: int = SOFT_TOKENS) -> Tuple[int, int]:
    """The keyframe size the processor reads as is: sides in 48 px steps, close to the frame's shape, using most of the budget."""
    aspect = float(width) / float(height) if width and height else 16 / 9
    best = None
    for a in range(1, budget + 1):
        for b in range(1, budget // a + 1):
            if a * b < 0.85 * budget:
                continue
            err = abs(np.log((a / b) / aspect))
            if best is None or (err, -a * b) < best[0]:
                best = ((err, -a * b), a, b)
    return best[1] * PATCH, best[2] * PATCH


# ============================ which model ============================
def _setting() -> str:
    try:
        from classes.app import get_settings
        return str(get_settings().get(SETTING) or "")
    except Exception:
        return ""


def describe_on_import() -> bool:
    """True when the user asked for Gemini descriptions of all footage on import (default: off)."""
    try:
        from classes.app import get_settings
        return bool(get_settings().get(DESCRIBE_SETTING))
    except Exception:
        return False


def chosen_model(signed_in: bool) -> str:
    """The model to index with: the cloud one only when the user picked it and can use it."""
    return MODEL_CLOUD if _setting() == "cloud" and signed_in else MODEL_LOCAL


_shared: Dict[str, Any] = {}
_shared_lock = threading.Lock()


def get_embedder(model: str) -> Any:
    """One embedder per model for the whole app (the local one holds a worker process)."""
    with _shared_lock:
        if model not in _shared:
            _shared[model] = CloudEmbedder() if model == MODEL_CLOUD else LocalEmbedder()
        return _shared[model]


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return (v / n).astype(np.float32) if n > 0 else v.astype(np.float32)


# ============================ on this computer ============================
class LocalEmbedder:
    """EmbeddingGemma 2 in a long-lived Node worker that stops after two idle minutes and starts again on demand."""

    model = MODEL_LOCAL

    def __init__(self, command: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None) -> None:
        self._command = command
        self._env = env
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._next_id = 0
        self.device = ""
        self.dtype = ""
        self.sent: List[Dict[str, Any]] = []      # the last requests, for diagnosis

    def _start(self) -> None:
        command, env = self._command, self._env
        if command is None:
            from classes.media_index import embed_runtime
            command, env = embed_runtime.worker_command()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      env={**os.environ, **(env or {})}, text=True, encoding="utf-8", bufsize=1,
                                      creationflags=flags)
        hello = self._read(READY_TIMEOUT)
        if not hello.get("ready"):
            self._stop()
            raise EmbedError("the search model did not start: " + str(hello.get("error") or "no reply"))
        self.device, self.dtype = str(hello.get("device") or ""), str(hello.get("dtype") or "")
        log.info("Search model ready on %s (%s)", self.device, self.dtype)

    def _read(self, timeout: float) -> Dict[str, Any]:
        proc = self._proc
        box: List[str] = []
        reader = threading.Thread(target=lambda: box.append(proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout)
        if not box or not box[0]:
            self._stop()
            raise EmbedError("the search model stopped answering")
        return json.loads(box[0])

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                proc.stdin.flush()
                proc.wait(5)
            except Exception:
                proc.kill()

    def start(self) -> None:
        """Start the worker now (its device is then known), unless it is already running."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()

    def close(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._stop()

    def _ask(self, op: str, **payload: Any) -> List[np.ndarray]:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            self._next_id += 1
            req = {"id": self._next_id, "op": op, **payload}
            self.sent = (self.sent + [req])[-8:]
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
            except OSError as exc:
                self._stop()
                raise EmbedError(f"the search model stopped: {exc}")
            reply = self._read(REQUEST_TIMEOUT)
            self._timer = threading.Timer(IDLE_SECONDS, self.close)
            self._timer.daemon = True
            self._timer.start()
        if reply.get("error") or reply.get("id") != req["id"]:
            raise EmbedError("the search model failed: " + str(reply.get("error") or "out-of-order reply"))
        return [_unit(np.frombuffer(base64.b64decode(v), dtype="<f4")) for v in reply["vectors"]]

    def texts(self, texts: Sequence[str], query: bool = False) -> List[np.ndarray]:
        prefix = QUERY_PREFIX if query else DOC_PREFIX
        out: List[np.ndarray] = []
        for lo in range(0, len(texts), TEXT_BATCH):
            out += self._ask("embed_texts", texts=[prefix + t for t in texts[lo:lo + TEXT_BATCH]])
        return out

    def images(self, paths: Sequence[str]) -> List[np.ndarray]:
        out: List[np.ndarray] = []
        for lo in range(0, len(paths), IMAGE_BATCH):
            out += self._ask("embed_images", paths=list(paths[lo:lo + IMAGE_BATCH]))
        return out

    def audio(self, windows: Sequence[Window]) -> List[np.ndarray]:
        out: List[np.ndarray] = []
        with tempfile.TemporaryDirectory(prefix="zenvi_aw_") as tmp:
            for lo in range(0, len(windows), AUDIO_BATCH):
                paths = []
                for i, (_, _, samples) in enumerate(windows[lo:lo + AUDIO_BATCH]):
                    paths.append(os.path.join(tmp, f"{lo + i}.f32"))
                    np.asarray(samples, dtype="<f4").tofile(paths[-1])
                out += self._ask("embed_audio", paths=paths)
        return out


# ============================ in the cloud ============================
def _wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_RATE)
        w.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class CloudEmbedder:
    """Gemini Embedding 2 through the backend: the same inputs, billed per token by the backend."""

    model = MODEL_CLOUD
    device = "cloud"

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _embed(self, items: List[Dict[str, Any]], task_type: Optional[str]) -> List[np.ndarray]:
        from classes.api_client import get_backend_client
        client = self._client or get_backend_client()
        out: List[np.ndarray] = []
        for lo in range(0, len(items), CLOUD_BATCH):
            reply = client.v2_embed(items[lo:lo + CLOUD_BATCH], dims=S.EMBED_DIMS, task_type=task_type)
            vectors = reply.get("vectors") or []
            if reply.get("error") or len(vectors) != len(items[lo:lo + CLOUD_BATCH]) or not all(vectors):
                flags = {k: reply[k] for k in ("auth", "credits", "rate_limited", "retry_after", "unsupported") if reply.get(k)}
                raise EmbedError(str(reply.get("error") or "the cloud returned no vector"), **flags)
            out += [_unit(np.frombuffer(base64.b64decode(v), dtype="<f2").astype(np.float32)) for v in vectors]
        return out

    def texts(self, texts: Sequence[str], query: bool = False) -> List[np.ndarray]:
        return self._embed([{"kind": "text", "text": t} for t in texts], "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT")

    def images(self, paths: Sequence[str]) -> List[np.ndarray]:
        items = []
        for p in paths:
            with open(p, "rb") as fh:
                items.append({"kind": "image", "mime": "image/jpeg", "data": base64.b64encode(fh.read()).decode("ascii")})
        return self._embed(items, None)

    def audio(self, windows: Sequence[Window]) -> List[np.ndarray]:
        return self._embed([{"kind": "audio", "mime": "audio/wav", "data": base64.b64encode(_wav(samples)).decode("ascii")}
                            for _, _, samples in windows], None)
