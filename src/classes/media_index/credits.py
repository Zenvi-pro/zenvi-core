"""
 @file
 @brief The attributions the media index owes its third-party data and models (licences that require a credit).

 One list, read by the About dialog and by the tools that already return the same texts, so they cannot drift.
"""

from __future__ import annotations

import html
from typing import List


def third_party_credits() -> List[str]:
    """Credit lines for the place-name data (GeoNames, CC BY 4.0) and the voice model (WeSpeaker, CC BY 4.0)."""
    from classes.media_index import gazetteer, people_models

    return [gazetteer.ATTRIBUTION] + [spec["attribution"] for spec in people_models.MODELS.values() if spec.get("attribution")]


def credits_html() -> str:
    """The credit lines as one small HTML paragraph for the About dialog."""
    lines = "<br>".join(html.escape(line) for line in third_party_credits())
    return f"<p style=\"font-size:8pt; font-weight:300;\">{lines}</p>"
