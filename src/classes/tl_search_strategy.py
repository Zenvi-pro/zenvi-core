"""Heuristics for when TwelveLabs audio+visual search is preferred."""

from __future__ import annotations

import re
from typing import Any, Dict

_AUDIO_MARKERS = (
    "podcast",
    "interview",
    "dialogue",
    "voiceover",
    "voice over",
    "talking",
    "speech",
    "monologue",
    "narrat",
    "lecture",
    "conversation",
    "music",
    "lyrics",
    "subtitle",
)
_NON_LATIN = re.compile(r"[^\x00-\x7F]")


def _tag_corpus_sparse(ai: Dict[str, Any]) -> bool:
    """True when clip summary text is too thin for confident clip targeting."""
    short = (ai.get("short_summary") or "").strip()
    desc = (ai.get("description") or "").strip()
    transcript = (ai.get("transcript") or "").strip()
    sounds = (ai.get("sounds") or "").strip()
    chapters = ai.get("chapters") or []
    combined = f"{short} {desc} {transcript} {sounds}".strip()
    if len(combined) < 40 and len(chapters) <= 1:
        return True
    # Legacy Gemini tags
    tags = ai.get("tags") if isinstance(ai.get("tags"), dict) else {}
    objs = tags.get("objects") or []
    scenes = tags.get("scenes") or []
    activities = tags.get("activities") or []
    unique = {str(x).lower() for x in (objs + scenes + activities) if x}
    scene_count = len(ai.get("scene_descriptions") or [])
    if unique and len(unique) <= 2 and scene_count <= 2 and len(combined) < 40:
        return True
    if not unique and not combined and scene_count <= 2:
        return True
    return False


def infer_tl_search_hint(ai: Dict[str, Any], filename: str = "") -> str:
    """Return 'prefer_audio_and_visual' or 'visual_ok'."""
    if not isinstance(ai, dict):
        return "visual_ok"
    tl = ai.get("twelvelabs") if isinstance(ai.get("twelvelabs"), dict) else {}
    if str(tl.get("status") or "").lower() != "ready":
        return "visual_ok"
    name = (filename or "").lower()
    desc = ((ai.get("description") or "") + " " + name).lower()
    if any(m in desc for m in _AUDIO_MARKERS):
        return "prefer_audio_and_visual"
    lang = str(ai.get("language") or tl.get("language") or "").lower()
    if lang and lang not in ("en", "english"):
        return "prefer_audio_and_visual"
    transcript = ai.get("transcription") or tl.get("transcription") or ""
    if isinstance(transcript, str) and _NON_LATIN.search(transcript):
        return "prefer_audio_and_visual"
    if _tag_corpus_sparse(ai):
        return "prefer_audio_and_visual"
    return "visual_ok"
