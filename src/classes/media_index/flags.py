"""
 @file
 @brief The ``media-index-v2`` preference: off means the app behaves exactly as before.
"""

from __future__ import annotations

SETTING = "media-index-v2"


def v2_enabled() -> bool:
    """True when the user turned the media index v2 analysis on (default: off).

    Safe to call without an application (headless tests, scripts): that reads as off.
    """
    try:
        from classes.app import get_settings

        return bool(get_settings().get(SETTING))
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
