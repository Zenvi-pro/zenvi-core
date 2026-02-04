"""
Utility to list available Gemini models with simple in-process caching.
Caching prevents hammering the Gemini Models API (429 Too Many Requests) when
the AI dock is opened repeatedly.
"""
import os
import time
from typing import List, Tuple

from classes.logger import log

# Cache (models, timestamp). We keep this in memory only for the current process
# to avoid repeated models.list calls on every dock open.
_CACHE: Tuple[List[str], float] = ([], 0.0)
_CACHE_TTL_SECONDS = 600  # 10 minutes


def _load_env_key() -> str:
    """Load the GEMINI_API_KEY from .env/environment."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY") or ""


def _cached_models_valid(now: float) -> bool:
    _, cached_at = _CACHE
    return cached_at > 0 and (now - cached_at) < _CACHE_TTL_SECONDS


def list_available_models() -> List[str]:
    """Return available Gemini models with caching and graceful fallbacks."""
    now = time.monotonic()
    if _cached_models_valid(now):
        return _CACHE[0]

    api_key = _load_env_key()
    if not api_key:
        log.warning("GEMINI_API_KEY not set")
        return []

    try:
        from google import genai
        from google.api_core import exceptions as gexc

        client = genai.Client(api_key=api_key)
        models = client.models.list()
        available = [
            m.name.replace("models/", "")
            for m in models
            if m.name is not None and m.supported_actions and "generateContent" in m.supported_actions
        ]

        if available:
            log.info(f"Found {len(available)} models: {available}")
            _update_cache(available, now)
            return available

        log.warning("No generateContent models found")
        _update_cache([], now)
        return []

    except gexc.ResourceExhausted as exc:
        # 429 Too Many Requests from Google API
        log.warning(f"Model list throttled (ResourceExhausted): {exc}")
        _update_cache(_CACHE[0], now)
        return _CACHE[0]
    except Exception as exc:
        log.error(f"Failed to list models: {exc}")
        _update_cache(_CACHE[0], now)
        return _CACHE[0]


def _update_cache(models: List[str], now: float) -> None:
    global _CACHE
    # Always store sorted list for consistent UI ordering
    _CACHE = (sorted(models), now)
