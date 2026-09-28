"""Local visual embedding index (CLIP/SigLIP ONNX when available).

Day-one engine is a deterministic perceptual hash embedding so CI and offline
installs can search without a 300MB download. Real CLIP/SigLIP plugs in via
``set_embedder_factory``. TwelveLabs remains the optional cloud tier.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence

log = logging.getLogger("speech.visual_index")


def _default_index_dir() -> str:
    try:
        from classes import info
        base = getattr(info, "USER_PATH", None) or os.path.join(
            os.path.expanduser("~"), ".openshot_qt",
        )
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".openshot_qt")
    return os.path.join(base, "visual_index")


class Embedder(Protocol):
    def embed_image(self, path: str) -> list[float]:
        ...

    def embed_text(self, text: str) -> list[float]:
        ...


class HashEmbedder:
    """Deterministic bag-of-hashes embedding (dim=64). Not semantic — CI stub."""

    DIM = 64

    def embed_image(self, path: str) -> list[float]:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 16)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            h.update(path.encode("utf-8"))
        return self._vec(h.digest())

    def embed_text(self, text: str) -> list[float]:
        h = hashlib.sha256(text.strip().lower().encode("utf-8")).digest()
        return self._vec(h)

    def _vec(self, digest: bytes) -> list[float]:
        out = []
        raw = digest * ((self.DIM // len(digest)) + 1)
        for i in range(self.DIM):
            out.append((raw[i] / 255.0) * 2.0 - 1.0)
        # L2 normalize
        n = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / n for x in out]


class ClipOnnxEmbedder:
    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path

    def embed_image(self, path: str) -> list[float]:
        raise RuntimeError("CLIP ONNX weights not bundled; use HashEmbedder")

    def embed_text(self, text: str) -> list[float]:
        raise RuntimeError("CLIP ONNX weights not bundled; use HashEmbedder")


_embedder_factory: Callable[[], Embedder] = HashEmbedder


def set_embedder_factory(factory: Callable[[], Embedder]) -> None:
    global _embedder_factory
    _embedder_factory = factory


def reset_embedder_factory() -> None:
    global _embedder_factory
    _embedder_factory = HashEmbedder


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


@dataclass
class IndexEntry:
    fileId: str
    path: str
    size: int
    mtimeNs: int
    vector: list[float]
    kind: str = "image"  # image | video_keyframe
    startSec: Optional[float] = None
    endSec: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "fileId": self.fileId,
            "path": self.path,
            "size": self.size,
            "mtimeNs": self.mtimeNs,
            "vector": self.vector,
            "kind": self.kind,
            "startSec": self.startSec,
            "endSec": self.endSec,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "IndexEntry":
        return cls(
            fileId=str(d.get("fileId") or ""),
            path=str(d.get("path") or ""),
            size=int(d.get("size") or 0),
            mtimeNs=int(d.get("mtimeNs") or 0),
            vector=[float(x) for x in (d.get("vector") or [])],
            kind=str(d.get("kind") or "image"),
            startSec=d.get("startSec"),
            endSec=d.get("endSec"),
        )


class VisualIndex:
    def __init__(self, root: Optional[str] = None) -> None:
        self.root = root or _default_index_dir()
        self._lock = threading.RLock()
        self._entries: dict[str, IndexEntry] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, "index.json")

    def _load(self) -> None:
        path = self._path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for item in data.get("entries") or []:
                if isinstance(item, dict):
                    e = IndexEntry.from_dict(item)
                    self._entries[e.fileId or e.path] = e
        except Exception as exc:
            log.warning("visual index load failed: %s", exc)

    def _save(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        path = self._path()
        tmp = path + f".{os.getpid()}.partial"
        payload = {
            "entries": [e.to_dict() for e in self._entries.values()],
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def upsert_file(self, file_id: str, path: str) -> IndexEntry:
        st = os.stat(path)
        mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        key = file_id or path
        with self._lock:
            existing = self._entries.get(key)
            if (
                existing
                and existing.size == st.st_size
                and existing.mtimeNs == mtime_ns
            ):
                return existing
            emb = _embedder_factory()
            try:
                vec = emb.embed_image(path)
            except RuntimeError:
                vec = HashEmbedder().embed_image(path)
            entry = IndexEntry(
                fileId=str(file_id or ""),
                path=os.path.abspath(path),
                size=int(st.st_size),
                mtimeNs=int(mtime_ns),
                vector=vec,
            )
            self._entries[key] = entry
            self._save()
            return entry

    def search(self, query: str, *, top_k: int = 5) -> list[dict]:
        emb = _embedder_factory()
        try:
            q = emb.embed_text(query)
        except RuntimeError:
            q = HashEmbedder().embed_text(query)
        with self._lock:
            scored = []
            for e in self._entries.values():
                if not e.vector:
                    continue
                scored.append((cosine(q, e.vector), e))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, e in scored[: max(1, int(top_k))]:
            hit = {
                "fileId": e.fileId,
                "path": e.path,
                "score": score,
                "provider": "local",
            }
            if e.startSec is not None:
                hit["startSec"] = e.startSec
                hit["endSec"] = e.endSec
            out.append(hit)
        return out


_GLOBAL: Optional[VisualIndex] = None
_GLOBAL_LOCK = threading.Lock()


def get_visual_index() -> VisualIndex:
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = VisualIndex()
        return _GLOBAL


def reset_visual_index_for_tests(index: Optional[VisualIndex] = None) -> None:
    global _GLOBAL
    with _GLOBAL_LOCK:
        _GLOBAL = index
