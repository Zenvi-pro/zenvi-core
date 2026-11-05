"""Lyrics for a local song.

Typed lyrics are used as written. Otherwise the indexing analyze model writes
them from the description, and that call spends indexing credits.
"""

from classes.api_client import get_backend_client
from classes.credits_client import charge_operation_on_success, check_operation

_OPERATION = "indexing_per_minute"
_CREDIT_SECONDS = 60.0


class LyricsError(Exception):
    """The lyrics could not be written. The message is for the user."""


def explain_lyrics_error(error: str) -> str:
    """Turn a transport or provider failure into a message the dock can show."""
    text = (error or "").strip()
    low = text.lower()
    if "prepayment credits are depleted" in low or "resource_exhausted" in low:
        return ("The indexing model is out of Google prepay credits, so the lyrics were not written. "
                "Add credits in Google AI Studio and try again.")
    if text == "Not Found" or low == "not found":
        return ("The backend has no lyrics route (Not Found). "
                "Point this editor at the local backend that provides it.")
    return text


def prepare_lyrics(description: str, typed: str) -> str:
    """Return the lyrics to sing. A non-empty *typed* value skips the model."""
    typed = (typed or "").strip()
    if typed:
        return typed
    return write_lyrics(description)


def write_lyrics(description: str) -> str:
    """Ask the indexing model for lyrics and charge indexing credits on success."""
    description = (description or "").strip()
    if not description:
        raise LyricsError("Describe the music first.")
    _allowed, _balance, blocked = check_operation(_OPERATION, "song lyrics", duration_seconds=_CREDIT_SECONDS)
    if blocked:
        raise LyricsError(blocked)
    try:
        result = get_backend_client().write_song_lyrics(description)
    except Exception as exc:
        raise LyricsError("Could not write lyrics: %s" % exc) from exc
    if not isinstance(result, dict):
        raise LyricsError("The indexing model returned nothing.")
    if result.get("error"):
        raise LyricsError(explain_lyrics_error(str(result["error"])))
    lyrics = str(result.get("lyrics") or "").strip()
    if not lyrics:
        raise LyricsError("The indexing model returned no lyrics.")
    model = str(result.get("model") or "indexing model")
    provider = str(result.get("provider") or ("gemini" if "gemini" in model else "openai"))
    charge_operation_on_success(
        True,
        _OPERATION,
        provider=provider,
        note="song lyrics (%s)" % model,
        duration_seconds=_CREDIT_SECONDS,
    )
    return lyrics
