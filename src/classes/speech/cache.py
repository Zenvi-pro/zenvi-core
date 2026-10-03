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
from dataclasses import dataclass, field
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


def normalize_request_language(language: str) -> str:
    """Canonical cache language tag (casefold; empty → auto)."""
    return (language or "auto").strip().lower() or "auto"


def cache_key(
    path: str,
    size: int,
    mtime_ns: int,
    model_id: str,
    language: str,
) -> str:
    lang = normalize_request_language(language)
    model = (model_id or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    return f"{os.path.abspath(path)}|{size}|{mtime_ns}|{model}|{lang}"


def _identity_prefix(path: str, size: int, mtime_ns: int, model_id: str) -> str:
    model = (model_id or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    return f"{os.path.abspath(path)}|{int(size)}|{int(mtime_ns)}|{model}|"


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

    def _load_disk(self, key: str) -> Optional[TranscriptRecord]:
        disk = self._disk_path(key)
        if not os.path.isfile(disk):
            return None
        try:
            with open(disk, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return TranscriptRecord.from_dict(data)
        except Exception as exc:
            log.warning("transcript cache read failed: %s", exc)
            return None

    def _record_matches_identity(
        self,
        rec: TranscriptRecord,
        *,
        abspath: str,
        size: int,
        mtime_ns: int,
        model_id: str,
    ) -> bool:
        model = (model_id or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
        return (
            rec.size == size
            and rec.mtimeNs == mtime_ns
            and os.path.abspath(rec.path) == abspath
            and (rec.modelId or DEFAULT_MODEL_ID).strip() == model
        )

    def _find_sibling(
        self,
        *,
        abspath: str,
        size: int,
        mtime_ns: int,
        model_id: str,
        prefer_lang: str,
    ) -> Optional[TranscriptRecord]:
        """Any cache row for this file+model, preferring *prefer_lang* then newest gen.

        Agents often re-call with detected ``en-CA`` after an ``auto`` transcript
        (or the reverse). Those must resolve to one record or remove_words sees
        generation 1 while get_transcript returns generation N.
        """
        prefer = normalize_request_language(prefer_lang)
        prefix = _identity_prefix(abspath, size, mtime_ns, model_id)
        candidates: list[TranscriptRecord] = []

        def _base(lang) -> str:
            return str(lang or "").split("-")[0].split("_")[0].lower()

        def _consider(rec: Optional[TranscriptRecord]) -> None:
            if rec is None:
                return
            if not self._record_matches_identity(
                rec, abspath=abspath, size=size, mtime_ns=mtime_ns, model_id=model_id,
            ):
                return
            # An explicit language only shares a record in that language
            # (en-CA ~ en): another language's words would skip ASR wrongly.
            if prefer != "auto" and _base(prefer) not in (
                _base(rec.language), _base(rec.requestLanguage),
            ):
                return
            candidates.append(rec)

        with self._lock:
            for key, rec in list(self._mem.items()):
                if key.startswith(prefix):
                    _consider(rec)

        try:
            names = os.listdir(self.root)
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".json") or ".partial" in name:
                continue
            disk_path = os.path.join(self.root, name)
            try:
                with open(disk_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                rec = TranscriptRecord.from_dict(data)
            except Exception:
                continue
            _consider(rec)

        if not candidates:
            return None

        def _rank(rec: TranscriptRecord) -> tuple:
            req = normalize_request_language(rec.requestLanguage or rec.language)
            lang_rank = 2 if req == prefer else (1 if req == "auto" else 0)
            # Generation first: a fresh auto ASR must beat a stale en-ca twin.
            return (int(rec.generation or 0), lang_rank, float(rec.createdAt or 0))

        return max(candidates, key=_rank)

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
        lang = normalize_request_language(language)
        key = cache_key(abspath, size, mtime_ns, model_id, lang)
        exact: Optional[TranscriptRecord] = None
        with self._lock:
            hit = self._mem.get(key)
            if hit is not None:
                self._mem.move_to_end(key)
                exact = hit
            else:
                rec = self._load_disk(key)
                if rec is not None and self._record_matches_identity(
                    rec, abspath=abspath, size=size, mtime_ns=mtime_ns, model_id=model_id,
                ):
                    self._mem[key] = rec
                    self._mem.move_to_end(key)
                    self._trim_locked()
                    exact = rec

        sibling = self._find_sibling(
            abspath=abspath,
            size=size,
            mtime_ns=mtime_ns,
            model_id=model_id,
            prefer_lang=lang,
        )
        chosen = sibling
        if exact is not None and (
            sibling is None or int(exact.generation or 0) > int(sibling.generation or 0)
        ):
            chosen = exact
        if chosen is None:
            return None
        # Alias into the requested key so the next lookup is O(1).
        with self._lock:
            self._mem[key] = chosen
            self._mem.move_to_end(key)
            self._trim_locked()
        return chosen

    def put(self, record: TranscriptRecord) -> None:
        req_lang = normalize_request_language(record.requestLanguage)
        record.requestLanguage = req_lang
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
            # Keep auto ↔ explicit language aliases pointing at the same record
            # so remove_words(language=en-CA) and get_transcript(auto) agree.
            abspath = os.path.abspath(record.path)
            for alias_lang in ("auto", req_lang, normalize_request_language(record.language)):
                if not alias_lang:
                    continue
                alias_key = cache_key(
                    abspath, record.size, record.mtimeNs, record.modelId, alias_lang,
                )
                self._mem[alias_key] = record
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
