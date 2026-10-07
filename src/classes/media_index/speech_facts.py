"""
 @file
 @brief What is said, from the app's own local transcription, kept on the shelf by content.

 The editor already transcribes locally (Apple SpeechAnalyzer or whisper.cpp) and caches by
 file *path*. This layer adds what the index needs without changing that: the transcript is
 stored on the shelf by fingerprint, so a moved, renamed or re-imported file reuses it instead
 of transcribing again, and a transcript found on the shelf is handed back to the path cache so
 the agent's ``get_transcript`` tool hits it too.

 A transcript is a model's reading of the audio, so it is ``inferred`` and carries each word's
 confidence when the engine gives one.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from classes.logger import log
from classes.media_index import schema as S
from classes.media_index.store import Shelf

SENTENCE_GAP_SECONDS = 0.8
SENTENCE_MAX_WORDS = 30
SENTENCE_MAX_SECONDS = 15.0
SPEECH_JOIN_GAP = 0.4          # pauses shorter than this are still one stretch of speech
MAX_TRANSCRIBE_SECONDS = 60 * 60


def _text_of(words: List[Dict[str, Any]]) -> str:
    out = ""
    for w in words:
        piece = str(w.get("text") or "").strip()
        if not piece:
            continue
        # engines differ: some words carry their leading space, some punctuation stands alone
        if out and piece[:1] not in ".,!?;:%)" and not out.endswith(("(", "$")):
            out += " "
        out += piece
    return out


def sentences_from_words(words: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group words into sentences: at end punctuation, a long pause, a speaker change, or a size cap."""
    out: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        speakers = {w.get("speakerId") for w in current if w.get("speakerId")}
        out.append({
            "start": round(float(current[0]["startSec"]), 3),
            "end": round(float(current[-1]["endSec"]), 3),
            "text": _text_of(current),
            "speaker": next(iter(speakers)) if len(speakers) == 1 else None,
        })
        current.clear()

    for w in words:
        if current:
            gap = float(w["startSec"]) - float(current[-1]["endSec"])
            span = float(w["endSec"]) - float(current[0]["startSec"])
            changed = bool(w.get("speakerId") and current[-1].get("speakerId") and w["speakerId"] != current[-1]["speakerId"])
            if gap >= SENTENCE_GAP_SECONDS or changed or len(current) >= SENTENCE_MAX_WORDS or span > SENTENCE_MAX_SECONDS:
                flush()
        current.append(w)
        if str(w.get("text") or "").rstrip().endswith((".", "?", "!")):
            flush()
    flush()
    return out


def speech_ranges(words: List[Dict[str, Any]]) -> List[List[float]]:
    """Stretches of speech: word spans joined across pauses shorter than SPEECH_JOIN_GAP."""
    ranges: List[List[float]] = []
    for w in words:
        start, end = float(w["startSec"]), float(w["endSec"])
        if ranges and start - ranges[-1][1] < SPEECH_JOIN_GAP:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    return [[round(a, 3), round(b, 3)] for a, b in ranges]


def speech_stats(words: List[Dict[str, Any]], duration: float) -> Dict[str, Any]:
    ranges = speech_ranges(words)
    seconds = sum(b - a for a, b in ranges)
    return {
        "word_count": len(words),
        "speech_seconds": round(seconds, 2),
        "speech_ratio": round(seconds / duration, 4) if duration > 0 else 0.0,
        "words_per_minute": round(len(words) / (seconds / 60.0), 1) if seconds > 1.0 else 0.0,
    }


def per_shot_speech(words: List[Dict[str, Any]], shots: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """How much of each shot is speech: word count, seconds spoken, and who speaks."""
    ranges = speech_ranges(words)
    out: Dict[int, Dict[str, Any]] = {}
    for shot in shots:
        a, b = float(shot["start"]), float(shot["end"])
        inside = [w for w in words if a <= (float(w["startSec"]) + float(w["endSec"])) / 2.0 < b]
        spoken = sum(max(0.0, min(b, r[1]) - max(a, r[0])) for r in ranges)
        speakers = sorted({str(w["speakerId"]) for w in inside if w.get("speakerId")})
        out[int(shot["id"])] = {
            "words": len(inside),
            "speech_seconds": round(spoken, 2),
            "speech_ratio": round(spoken / (b - a), 3) if b > a else 0.0,
            "speakers": speakers,
        }
    return out


# ============================ the transcript itself ============================
def _record_to_layer(record: Any, duration: float) -> Dict[str, Any]:
    words = [w.to_dict() if hasattr(w, "to_dict") else dict(w) for w in record.words]
    return {
        "version": S.LAYER_VERSIONS[S.LAYER_SPEECH],
        "kind": S.INFERRED,
        "engine": getattr(record, "transcriptionSource", "local"),
        "model": getattr(record, "modelId", ""),
        "language": getattr(record, "language", ""),
        "generation": int(getattr(record, "generation", 1) or 1),
        "words": words,
        "sentences": sentences_from_words(words),
        "speech_ranges": speech_ranges(words),
        "stats": speech_stats(words, duration),
    }


def _prime_path_cache(path: str, layer: Dict[str, Any], cache: Any) -> None:
    """Give the path-keyed transcript cache this transcript, so tools that ask for it by path hit."""
    try:
        from classes.speech import asr
        from classes.speech.cache import DEFAULT_MODEL_ID, TranscriptRecord, Word, file_identity

        abspath, size, mtime_ns = file_identity(path)
        current_model = asr._cache_model_id(asr.resolve_engine("auto"), DEFAULT_MODEL_ID)
        if layer.get("model") != current_model:
            return  # a transcript from another engine is not what this machine's tools would ask for
        record = TranscriptRecord(
            path=abspath, size=size, mtimeNs=mtime_ns, modelId=layer["model"],
            language=layer.get("language") or "", words=[Word.from_dict(w) for w in layer["words"]],
            generation=int(layer.get("generation") or 1), transcriptionSource=layer.get("engine") or "local",
            requestLanguage="auto")
        if cache is None:
            from classes.speech.cache import get_default_cache
            cache = get_default_cache()
        if cache.get(path, model_id=layer["model"], language="auto") is None:
            cache.put(record)
    except Exception:
        log.debug("Could not prime the transcript cache", exc_info=True)


def load_or_transcribe(
    path: str,
    probe: Dict[str, Any],
    sha: str,
    shelf: Shelf,
    *,
    transcribe: Optional[Callable[..., Any]] = None,
    cache: Any = None,
    token: Any = None,
) -> Optional[Dict[str, Any]]:
    """The speech layer for *path*, or None when there is no audio or no engine.

    Order: the shelf (by content), else the app's transcriber (which has its own path cache),
    then the result is saved on the shelf. Raises only for a cancelled run.
    """
    if not (probe or {}).get("has_audio"):
        return None
    duration = float((probe or {}).get("duration") or 0.0)
    if duration > MAX_TRANSCRIBE_SECONDS:
        return {"version": S.LAYER_VERSIONS[S.LAYER_SPEECH], "skipped": "longer than %d minutes" % (MAX_TRANSCRIBE_SECONDS // 60)}

    if sha and shelf.layer_ready(sha, S.LAYER_SPEECH, version=S.LAYER_VERSIONS[S.LAYER_SPEECH]):
        saved = shelf.read_json(sha, "speech.json")
        if isinstance(saved, dict) and isinstance(saved.get("words"), list):
            _prime_path_cache(path, saved, cache)
            return saved

    if transcribe is None:
        from classes.speech.asr import transcribe_file
        transcribe = transcribe_file
    try:
        kwargs: Dict[str, Any] = {"token": token}
        if cache is not None:
            kwargs["cache"] = cache
        record = transcribe(path, **kwargs)
    except InterruptedError:
        raise                       # the speech runtime's cancellation: let the caller stop
    except Exception as exc:
        log.info("Transcription unavailable for %s: %s", path, exc)
        return {"version": S.LAYER_VERSIONS[S.LAYER_SPEECH], "unavailable": str(exc)[:300]}
    layer = _record_to_layer(record, duration)
    if sha:
        shelf.write_json(sha, "speech.json", layer)
        shelf.set_layer(sha, S.LAYER_SPEECH, version=S.LAYER_VERSIONS[S.LAYER_SPEECH], status="ready",
                        model=layer["model"], engine=layer["engine"], words=len(layer["words"]))
    return layer
