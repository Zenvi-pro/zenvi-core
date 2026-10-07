"""
 @file
 @brief Media index v2 is how indexing works, not a preference; only people recognition is opt-in.
"""

from __future__ import annotations


def v2_enabled() -> bool:
    """True whenever the application is running: the index v2 analysis is always on.

    Without an application (headless tests, scripts) this reads as off, and tests switch it with ``monkeypatch``.
    """
    try:
        from classes.app import get_settings

        return get_settings() is not None
    except Exception:
        return False


PEOPLE_SETTING = "media-index-people"


def people_enabled() -> bool:
    """True when the user turned people identity on (default: off) and media index v2 is on too. Headless reads as off."""
    try:
        from classes.app import get_settings

        return bool(get_settings().get(PEOPLE_SETTING)) and v2_enabled()
    except Exception:
        return False
