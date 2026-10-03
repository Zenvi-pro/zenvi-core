"""Local ASR with platform routing.

* **macOS:** prefer Apple SpeechAnalyzer (native helper); fall back to faster-whisper.
* **Windows / Linux:** faster-whisper only.

``engine`` arg on tools: ``auto`` | ``apple`` | ``whisper``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Optional, Protocol

from classes.speech.audio_extract import extract_mono_16k_wav
from classes.speech.cache import (
    DEFAULT_MODEL_ID,
    TranscriptRecord,
    Word,
    file_identity,
    get_default_cache,
)
from classes.speech.runtime import CancelToken, inference_slot

log = logging.getLogger("speech.asr")

# Map our modelId → faster-whisper size name.
_MODEL_SIZES = {
    "faster-whisper-tiny": "tiny",
    "faster-whisper-base": "base",
    "faster-whisper-small": "small",
    "faster-whisper-medium": "medium",
}

APPLE_MODEL_ID = "apple-speech-analyzer"


class Transcriber(Protocol):
    def transcribe(
        self,
        wav_path: str,
        *,
        language: Optional[str],
        token: CancelToken,
    ) -> tuple[list[Word], str]:
        """Return (words, detected_language)."""
        ...


class FasterWhisperTranscriber:
    model_id = DEFAULT_MODEL_ID

    def __init__(self, model_id: str = DEFAULT_MODEL_ID) -> None:
        self.model_id = model_id or DEFAULT_MODEL_ID
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "No local speech engine: releases bundle whisper.cpp "
                "(installer/build-whisper-cli.sh puts it in src/whisper); "
                "or pip install -r requirements-speech.txt for faster-whisper"
            ) from exc
        size = _MODEL_SIZES.get(self.model_id, "base")
        self._model = WhisperModel(size, device="cpu", compute_type="int8")
        return self._model

    def transcribe(
        self,
        wav_path: str,
        *,
        language: Optional[str],
        token: CancelToken,
    ) -> tuple[list[Word], str]:
        model = self._load()
        token.raise_if_cancelled()
        lang = None if not language or language == "auto" else language
        segments, info = model.transcribe(
            wav_path,
            language=lang,
            word_timestamps=True,
            vad_filter=True,
        )
        words: list[Word] = []
        for seg in segments:
            token.raise_if_cancelled()
            for w in getattr(seg, "words", None) or []:
                text = (getattr(w, "word", None) or "").strip()
                if not text:
                    continue
                words.append(
                    Word(
                        text=text,
                        startSec=float(getattr(w, "start", 0) or 0),
                        endSec=float(getattr(w, "end", 0) or 0),
                        confidence=(
                            float(w.probability)
                            if getattr(w, "probability", None) is not None
                            else None
                        ),
                    )
                )
        detected = str(getattr(info, "language", None) or lang or "unknown")
        return words, detected


def resolve_engine(engine: str = "auto") -> str:
    """Return ``apple`` or ``whisper`` after applying platform rules."""
    choice = (engine or "auto").strip().lower() or "auto"
    if choice == "whisper":
        return "whisper"
    if choice == "apple":
        if sys.platform != "darwin":
            raise RuntimeError("Apple SpeechAnalyzer is only available on macOS")
        return "apple"
    # auto
    if sys.platform == "darwin":
        try:
            from classes.speech.apple_asr import apple_asr_available
            if apple_asr_available():
                return "apple"
        except Exception as exc:
            log.info("Apple ASR probe failed: %s", exc)
    return "whisper"


def make_transcriber(engine: str = "auto", model_id: str = DEFAULT_MODEL_ID) -> Transcriber:
    resolved = resolve_engine(engine)
    if resolved == "apple":
        from classes.speech.apple_asr import AppleSpeechTranscriber
        return AppleSpeechTranscriber()
    return _whisper_transcriber(model_id)


def _whisper_transcriber(model_id: str = DEFAULT_MODEL_ID) -> Transcriber:
    """The bundled whisper.cpp when present (every release), else faster-whisper."""
    from classes.speech import whisper_cpp
    if whisper_cpp.available():
        return whisper_cpp.WhisperCppTranscriber()
    return FasterWhisperTranscriber(model_id=model_id or DEFAULT_MODEL_ID)


_transcriber_factory: Optional[Callable[[str, str], Transcriber]] = None


def set_transcriber_factory(factory: Callable[[str], Transcriber]) -> None:
    """Tests: factory(model_id) → Transcriber (Whisper-shaped). Forces whisper path."""
    global _transcriber_factory

    def _wrap(engine: str, model_id: str) -> Transcriber:
        return factory(model_id)

    _transcriber_factory = _wrap


def set_engine_transcriber_factory(
    factory: Callable[[str, str], Transcriber],
) -> None:
    """Tests: factory(engine, model_id) → Transcriber."""
    global _transcriber_factory
    _transcriber_factory = factory


def reset_transcriber_factory() -> None:
    global _transcriber_factory
    _transcriber_factory = None


def _cache_model_id(engine: str, model_id: str) -> str:
    if engine == "apple":
        return APPLE_MODEL_ID
    from classes.speech import whisper_cpp
    if whisper_cpp.available():
        return whisper_cpp.MODEL_ID  # one bundled model, whatever was asked for
    return (model_id or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID


def transcribe_file(
    media_path: str,
    *,
    language: str = "auto",
    model_id: str = DEFAULT_MODEL_ID,
    force: bool = False,
    engine: str = "auto",
    cache=None,
    token: Optional[CancelToken] = None,
) -> TranscriptRecord:
    """Return a word-level transcript for *media_path*, using cache unless forced."""
    store = cache if cache is not None else get_default_cache()
    lang_key = (language or "auto").strip().lower() or "auto"

    # Resolve engine up front so cache keys match the provider we will use.
    try:
        resolved = resolve_engine(engine)
    except RuntimeError:
        # e.g. engine=apple on Windows — fall back for auto only; re-raise for explicit
        if (engine or "auto").strip().lower() == "apple":
            raise
        resolved = "whisper"

    cache_model = _cache_model_id(resolved, model_id)

    if not force:
        hit = store.get(media_path, model_id=cache_model, language=lang_key)
        if hit is not None:
            return hit

    abspath, size, mtime_ns = file_identity(media_path)
    with inference_slot(token) as tok:
        wav_path, err = extract_mono_16k_wav(media_path)
        if err:
            raise RuntimeError(err)
        words: list[Word] = []
        detected = lang_key
        used_engine = resolved
        used_model = cache_model
        try:
            tok.raise_if_cancelled()
            if _transcriber_factory is not None:
                engine_obj = _transcriber_factory(resolved, cache_model)
            else:
                engine_obj = make_transcriber(resolved, model_id=model_id)

            try:
                words, detected = engine_obj.transcribe(
                    wav_path,
                    language=None if lang_key == "auto" else lang_key,
                    token=tok,
                )
            except Exception as exc:
                # Mac auto: Apple failed → Whisper once.
                if (
                    resolved == "apple"
                    and (engine or "auto").strip().lower() in ("", "auto")
                ):
                    log.warning("Apple ASR failed (%s); falling back to whisper", exc)
                    used_engine = "whisper"
                    used_model = _cache_model_id("whisper", model_id)
                    if _transcriber_factory is not None:
                        engine_obj = _transcriber_factory("whisper", used_model)
                    else:
                        engine_obj = _whisper_transcriber(model_id)
                    words, detected = engine_obj.transcribe(
                        wav_path,
                        language=None if lang_key == "auto" else lang_key,
                        token=tok,
                    )
                else:
                    raise
        finally:
            try:
                if os.path.isfile(wav_path):
                    os.remove(wav_path)
            except OSError:
                pass

    prev = store.get(media_path, model_id=used_model, language=lang_key)
    generation = (prev.generation + 1) if prev is not None else 1
    source = "local-apple" if used_engine == "apple" else "local"
    record = TranscriptRecord(
        path=abspath,
        size=size,
        mtimeNs=mtime_ns,
        modelId=used_model,
        language=detected if lang_key == "auto" else lang_key,
        words=words,
        generation=generation,
        transcriptionSource=source,
        requestLanguage=lang_key,
    )
    store.put(record)
    try:
        from classes.speech.audit import audit, enabled
        if enabled():
            audit(
                "asr_complete",
                engine=used_engine,
                modelId=used_model,
                language=record.language,
                wordCount=len(words),
                transcriptionSource=source,
                path=abspath,
                sample=[w.text for w in words[:20]],
            )
    except Exception:
        pass
    return record
