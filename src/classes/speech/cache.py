"""Disk + memory LRU transcript cache.

Key identity: path + size + mtime_ns + modelId + language.
Atomic writes (temp + replace). Memory is a true LRU, not wipe-all-at-4.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

log = logging.getLogger("speech.cache")

DEFAULT_MODEL_ID = "faster-whisper-base"
_MEMORY_CAP = 32


@dataclass
class Word:
    text: str
    startSec: float
    endSec: float
    confidence: Optional[float] = None
    speakerId: Optional[str] = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "text": self.text,
            "startSec": float(self.startSec),
            "endSec": float(self.endSec),
        }
        if self.confidence is not None:
            out["confidence"] = float(self.confidence)
        if self.speakerId is not None:
            out["speakerId"] = self.speakerId
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Word":
        return cls(
            text=str(d.get("text") or ""),
            startSec=float(d.get("startSec") or d.get("start") or 0),
            endSec=float(d.get("endSec") or d.get("end") or 0),
            confidence=(
                float(d["confidence"])
                if d.get("confidence") is not None
                else None
            ),
            speakerId=str(d["speakerId"]) if d.get("speakerId") else None,
        )


@dataclass
class TranscriptRecord:
    path: str
    size: int
    mtimeNs: int
    modelId: str
    language: str
    words: list[Word] = field(default_factory=list)
    generation: int = 1
    transcriptionSource: str = "local"
    createdAt: float = field(default_factory=time.time)
    # Cache identity uses the *request* language ("auto" vs "en"), not only
    # the detected language written into ``language``.
    requestLanguage: str = "auto"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "mtimeNs": self.mtimeNs,
            "modelId": self.modelId,
            "language": self.language,
            "requestLanguage": self.requestLanguage,
            "words": [w.to_dict() for w in self.words],
            "generation": int(self.generation),
            "transcriptionSource": self.transcriptionSource,
            "createdAt": self.createdAt,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TranscriptRecord":
        words = [Word.from_dict(w) for w in (d.get("words") or []) if isinstance(w, dict)]
        return cls(
            path=str(d.get("path") or ""),
            size=int(d.get("size") or 0),
            mtimeNs=int(d.get("mtimeNs") or 0),
            modelId=str(d.get("modelId") or DEFAULT_MODEL_ID),
            language=str(d.get("language") or ""),
            words=words,
            generation=int(d.get("generation") or 1),
            transcriptionSource=str(d.get("transcriptionSource") or "local"),
            createdAt=float(d.get("createdAt") or time.time()),
            requestLanguage=str(d.get("requestLanguage") or d.get("language") or "auto"),
        )


def file_identity(path: str) -> tuple[str, int, int]:
    st = os.stat(path)
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
    return os.path.abspath(path), int(st.st_size), int(mtime_ns)


def cache_key(
    path: str,
    size: int,
    mtime_ns: int,
    model_id: str,
    language: str,
) -> str:
    lang = (language or "auto").strip().lower() or "auto"
    model = (model_id or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    return f"{os.path.abspath(path)}|{size}|{mtime_ns}|{model}|{lang}"


def default_cache_dir() -> str:
    try:
        from classes import info
        base = getattr(info, "USER_PATH", None) or os.path.join(
            os.path.expanduser("~"), ".openshot_qt",
        )
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".openshot_qt")
    return os.path.join(base, "transcripts")


class TranscriptCache:
    """Process-wide cache. Safe for BACKGROUND_SAFE callers (internal lock)."""

    def __init__(
        self,
        root: Optional[str] = None,
        *,
        memory_cap: int = _MEMORY_CAP,
    ) -> None:
        self.root = root or default_cache_dir()
        self.memory_cap = max(1, int(memory_cap))
        self._mem: OrderedDict[str, TranscriptRecord] = OrderedDict()
        self._lock = threading.RLock()

    def _disk_path(self, key: str) -> str:
        # Stable filename from key hash — avoid huge path-in-name.
        import hashlib
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(self.root, f"{digest}.json")

    def get(
        self,
        path: str,
        *,
        model_id: str = DEFAULT_MODEL_ID,
        language: str = "auto",
    ) -> Optional[TranscriptRecord]:
        try:
            abspath, size, mtime_ns = file_identity(path)
        except OSError:
            return None
        key = cache_key(abspath, size, mtime_ns, model_id, language)
        with self._lock:
            hit = self._mem.get(key)
            if hit is not None:
                self._mem.move_to_end(key)
                return hit
            disk = self._disk_path(key)
            if not os.path.isfile(disk):
                return None
            try:
                with open(disk, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                rec = TranscriptRecord.from_dict(data)
            except Exception as exc:
                log.warning("transcript cache read failed: %s", exc)
                return None
            # Identity must still match (stale rename / clock skew).
            if (
                rec.size != size
                or rec.mtimeNs != mtime_ns
                or os.path.abspath(rec.path) != abspath
            ):
                return None
            self._mem[key] = rec
            self._mem.move_to_end(key)
            self._trim_locked()
            return rec

    def put(self, record: TranscriptRecord) -> None:
        req_lang = (record.requestLanguage or "auto").strip().lower() or "auto"
        key = cache_key(
            record.path, record.size, record.mtimeNs, record.modelId, req_lang,
        )
        os.makedirs(self.root, exist_ok=True)
        disk = self._disk_path(key)
        tmp = disk + f".{os.getpid()}.partial"
        payload = json.dumps(record.to_dict(), ensure_ascii=False, indent=0)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, disk)
        except Exception as exc:
            log.warning("transcript cache write failed: %s", exc)
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            # Still keep memory hit so the session works.
        with self._lock:
            self._mem[key] = record
            self._mem.move_to_end(key)
            self._trim_locked()

    def _trim_locked(self) -> None:
        while len(self._mem) > self.memory_cap:
            self._mem.popitem(last=False)


_GLOBAL: Optional[TranscriptCache] = None
_GLOBAL_LOCK = threading.Lock()


def get_default_cache() -> TranscriptCache:
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = TranscriptCache()
        return _GLOBAL


def reset_default_cache_for_tests(cache: Optional[TranscriptCache] = None) -> None:
    global _GLOBAL
    with _GLOBAL_LOCK:
        _GLOBAL = cache
