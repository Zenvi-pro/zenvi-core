"""Local ASR with platform routing.

* **macOS 26+:** Apple SpeechAnalyzer (native helper) when present.
* **Everywhere else, and when Apple fails:** the recogniser the Captions dock uses:
  Whistle (18 MB, downloaded on first use) or Parakeet when the user picked it and it is installed.

``engine`` arg on tools: ``auto`` | ``apple`` | ``whistle`` | ``parakeet``.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable, Optional, Protocol

from classes.speech.audio_extract import extract_mono_16k_wav
from classes.speech.cache import (
    TranscriptRecord,
    Word,
    file_identity,
    get_default_cache,
)
from classes.speech.runtime import CancelToken, inference_slot

log = logging.getLogger("speech.asr")

APPLE_MODEL_ID = "apple-speech-analyzer"
LOCAL_ENGINES = ("whistle", "parakeet")


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


def _words(entries) -> list[Word]:
    return [Word(text=w["text"], startSec=float(w["start"]), endSec=float(w["end"])) for w in entries]


class WhistleTranscriber:
    """The small captions recogniser; downloaded (pinned, checksummed) the first time it is needed."""
    model_id = "whistle"

    def transcribe(self, wav_path, *, language, token):
        from classes import hyperframes_local as hf
        if not hf.speech_ready(fresh=True):
            hf.install_speech()
        token.raise_if_cancelled()
        out = hf.recognise(wav_path, language=language)
        return _words(out["words"]), out.get("language") or language or "unknown"


class ParakeetTranscriber:
    """The larger captions recogniser, run by the HyperFrames CLI once the user installed it."""
    model_id = "parakeet"

    def transcribe(self, wav_path, *, language, token):
        from classes import hyperframes_local as hf
        token.raise_if_cancelled()
        return _words(hf.transcribe_parakeet(wav_path, language=language)), language or "unknown"


def _local_engine() -> str:
    """The captions recogniser: Parakeet when picked and installed, else Whistle where it has an engine."""
    from classes import hyperframes_local as hf
    if hf.recogniser() == "parakeet" and hf.parakeet_ready():
        return "parakeet"
    if hf.speech_supported():
        return "whistle"
    raise RuntimeError(
        "No on-device speech recogniser for this computer yet: install Parakeet (about 900 MB) "
        "from HyperFrames setup with accurate speech, then try again")


def resolve_engine(engine: str = "auto") -> str:
    """Return ``apple``, ``whistle`` or ``parakeet`` after applying platform rules."""
    choice = (engine or "auto").strip().lower() or "auto"
    if choice == "apple":
        if sys.platform != "darwin":
            raise RuntimeError("Apple SpeechAnalyzer is only available on macOS")
        return "apple"
    if choice == "parakeet":
        from classes import hyperframes_local as hf
        if not hf.parakeet_ready():
            raise RuntimeError("Parakeet is not installed; install it from HyperFrames setup with accurate speech")
        return "parakeet"
    if choice == "whistle":
        return "whistle"
    if choice != "auto":
        raise RuntimeError(f"unknown speech engine {engine!r}; use auto, apple, whistle or parakeet")
    if sys.platform == "darwin":
        try:
            from classes.speech.apple_asr import apple_asr_available
            if apple_asr_available():
                return "apple"
        except Exception as exc:
            log.info("Apple ASR probe failed: %s", exc)
    return _local_engine()


def make_transcriber(engine: str = "auto") -> Transcriber:
    resolved = engine if engine in ("apple", *LOCAL_ENGINES) else resolve_engine(engine)
    if resolved == "apple":
        from classes.speech.apple_asr import AppleSpeechTranscriber
        return AppleSpeechTranscriber()
    return ParakeetTranscriber() if resolved == "parakeet" else WhistleTranscriber()


_transcriber_factory: Optional[Callable[[str, str], Transcriber]] = None


def set_transcriber_factory(factory: Callable[[str], Transcriber]) -> None:
    """Tests: factory(model_id) → Transcriber, whichever engine resolves."""
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


def _cache_model_id(engine: str) -> str:
    """Transcripts are cached per engine: the recogniser that heard them."""
    return APPLE_MODEL_ID if engine == "apple" else engine


def _engine_language(engine: str, lang_key: str):
    """What each engine takes for a language: Apple a locale (ja-JP), the others the two-letter code (ja); None means detect."""
    if lang_key == "auto":
        return None
    return lang_key if engine == "apple" else lang_key.split("-")[0]


def _engine(engine: str) -> Transcriber:
    if _transcriber_factory is not None:
        return _transcriber_factory(engine, _cache_model_id(engine))
    return make_transcriber(engine)


def transcribe_file(
    media_path: str,
    *,
    language: str = "auto",
    force: bool = False,
    engine: str = "auto",
    cache=None,
    token: Optional[CancelToken] = None,
) -> TranscriptRecord:
    """Return a word-level transcript for *media_path*, using cache unless forced."""
    store = cache if cache is not None else get_default_cache()
    lang_key = (language or "auto").strip().lower() or "auto"
    auto = (engine or "auto").strip().lower() in ("", "auto")

    # Resolve engine up front so cache keys match the provider we will use.
    resolved = resolve_engine(engine)
    cache_model = _cache_model_id(resolved)

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
            try:
                words, detected = _engine(resolved).transcribe(
                    wav_path,
                    language=_engine_language(resolved, lang_key),
                    token=tok,
                )
            except Exception as exc:
                if resolved != "apple" or not auto:
                    raise
                # Mac auto: Apple failed → the captions recogniser once.
                used_engine = _local_engine()
                used_model = _cache_model_id(used_engine)
                log.warning("Apple ASR failed (%s); falling back to %s", exc, used_engine)
                words, detected = _engine(used_engine).transcribe(
                    wav_path,
                    language=_engine_language(used_engine, lang_key),
                    token=tok,
                )
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
