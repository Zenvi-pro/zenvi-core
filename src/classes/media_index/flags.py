"""
 @file
 @brief Media index v2 is how indexing works, not a preference; only people recognition is opt-in.
"""

from __future__ import annotations


PEOPLE_SETTING = "media-index-people"


def people_enabled() -> bool:
    """True when the user turned people identity on (default: off). Headless reads as off."""
    try:
        from classes.app import get_settings

        return bool(get_settings().get(PEOPLE_SETTING))
    except Exception:
        return False
