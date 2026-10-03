"""Captions as HyperFrames caption components, driven by Zenvi's own word timings.

A caption style is one component of the HyperFrames registry (``caption-*``): a
self-contained HTML composition holding a word list and a duration. Building a
caption track is therefore: take the clip's words from Zenvi's on-device
transcript, swap them into the component, render it transparent on this
computer, and put the video on a track above the clip. No Qt here.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List

# Registry components that share the one shape compile_composition drives: a word
# list ``[{ text, start, end }]`` plus ``data-duration``. Four are left out:
# caption-blend-difference needs the footage inside the composition, and
# caption-camera-follow, -editorial-emphasis and -parallax-layers lay out their
# sample sentence by hand, block by block.
STYLES: Dict[str, Dict[str, str]] = {
    "pill-karaoke": {"label": "Karaoke pill", "description": "Pill-shaped bar; each word lights up as it is spoken."},
    "highlight": {"label": "Highlight sweep", "description": "A colour block sweeps behind the active word, TikTok style."},
    "clip-wipe": {"label": "Wipe reveal", "description": "Each word is revealed left to right as it is spoken."},
    "weight-shift": {"label": "Weight shift", "description": "Elegant lines whose font weight follows the speech."},
    "gradient-fill": {"label": "Gradient bounce", "description": "Gradient-filled words with an elastic entrance."},
    "kinetic-slam": {"label": "Kinetic slam", "description": "One large word at a time, slammed in from alternating sides."},
    "emoji-pop": {"label": "Emoji pop", "description": "Stroked text with emoji accents and a squeeze entrance."},
    "neon-glow": {"label": "Neon glow", "description": "Cyan and magenta glow with accented keywords."},
    "neon-accent": {"label": "Neon accent", "description": "Multi-colour neon accents with a drifting wiggle."},
    "glitch-rgb": {"label": "Glitch", "description": "RGB split and CRT scanlines."},
    "matrix-decode": {"label": "Decode", "description": "Characters scramble before each word resolves."},
    "particle-burst": {"label": "Particle burst", "description": "Keywords set off coloured particle bursts."},
    "texture": {"label": "Textured type", "description": "Large uppercase words filled with a moving texture."},
}
DEFAULT_STYLE = "pill-karaoke"
MAX_GROUP_WORDS = 4
GROUP_PAUSE_SECONDS = 0.6            # a silence this long starts a new group
GROUP_LINGER_SECONDS = 0.5           # a group stays up this long after its last word

_WORD_LIST_RE = re.compile(r"(var\s+(?:TRANSCRIPT|WORDS|W)\s*=\s*)\[\s*\{\s*text\s*:.*?\]\s*;", re.DOTALL)
_RAW_GROUPS_RE = re.compile(r"(var\s+RAW_GROUPS\s*=\s*)\[.*?\]\s*;", re.DOTALL)
_GROUPS_RE = re.compile(r"(var\s+GROUPS\s*=\s*)\[\s*\{.*?\]\s*;", re.DOTALL)
_SAMPLE_END_RE = re.compile(r"(\.start\s*:\s*)8\.0\b")     # "... : 8.0" where the sample has no next group
_DURATION_ATTR_RE = re.compile(r'data-duration="[0-9.]+"')
_DURATION_VAR_RE = re.compile(r"(var\s+DURATION\s*=\s*)[0-9.]+\s*;")


class CaptionError(Exception):
    """A request the caption builder cannot honour."""


def component_id(style: str) -> str:
    if style not in STYLES:
        raise CaptionError("unknown caption style %r; choose one of: %s" % (style, ", ".join(STYLES)))
    return "caption-" + style


def _number(value: float) -> str:
    return ("%.3f" % float(value)).rstrip("0").rstrip(".")


def group_words(words: List[dict], duration: float, max_words: int = MAX_GROUP_WORDS) -> List[dict]:
    """Words split into the short phrases shown together: at most *max_words*, broken
    at a pause or the end of a sentence. Each group ends before the next one starts."""
    spans, first = [], 0
    for i, word in enumerate(words):
        last = i == len(words) - 1
        pause = not last and float(words[i + 1]["start"]) - float(word["end"]) >= GROUP_PAUSE_SECONDS
        if last or pause or i - first + 1 >= max_words or str(word["text"]).rstrip()[-1:] in ".!?":
            spans.append((first, i))
            first = i + 1
    groups = []
    for n, (a, b) in enumerate(spans):
        following = float(words[spans[n + 1][0]]["start"]) if n + 1 < len(spans) else float(duration)
        end = min(float(words[b]["end"]) + GROUP_LINGER_SECONDS, following - 0.05)
        groups.append({"wordStart": a, "wordEnd": b, "start": float(words[a]["start"]),
                       "end": round(max(end, float(words[b]["end"])), 3)})
    return groups


def compile_composition(html: str, words: List[dict], duration: float) -> str:
    """The component's HTML with *words* and *duration* in place of its sample ones.

    Some components also hard-code which sample words form each on-screen group
    (index tables for their 28 sample words); those are rebuilt for the real words.
    """
    if not words:
        raise CaptionError("there are no words to caption")
    if not _WORD_LIST_RE.search(html):
        raise CaptionError("this caption component has no word list Zenvi knows how to fill")
    # Function replacements: the JSON may contain backslashes re.sub would interpret.
    payload = json.dumps(words, ensure_ascii=False).replace("</", "<\\/")      # a word cannot end the <script>
    html = _WORD_LIST_RE.sub(lambda m: m.group(1) + payload + ";", html, count=1)
    groups = group_words(words, duration)
    pairs = json.dumps([[g["wordStart"], g["wordEnd"]] for g in groups])
    html = _RAW_GROUPS_RE.sub(lambda m: m.group(1) + pairs + ";", html, count=1)
    html = _GROUPS_RE.sub(lambda m: m.group(1) + json.dumps(groups) + ";", html, count=1)
    seconds = _number(duration)
    html = _SAMPLE_END_RE.sub(lambda m: m.group(1) + seconds, html)
    html = _DURATION_ATTR_RE.sub('data-duration="%s"' % seconds, html)
    return _DURATION_VAR_RE.sub(lambda m: m.group(1) + seconds + ";", html)


def clip_words(transcript_words: List[dict], clip_start: float, clip_end: float) -> List[dict]:
    """Transcript words (timeline seconds) that fall in a clip, relative to the clip's start."""
    out = []
    for word in transcript_words or []:
        text = str(word.get("text") or word.get("word") or "").strip()
        try:        # a clip's transcript carries timeline times next to the source ones
            start = float(word.get("timelineStartSec", word.get("startSec")))
            end = float(word.get("timelineEndSec", word.get("endSec")))
        except (TypeError, ValueError):
            continue
        if not text or end <= clip_start or start >= clip_end:
            continue
        out.append({"text": text,
                    "start": round(max(start, clip_start) - clip_start, 3),
                    "end": round(min(end, clip_end) - clip_start, 3)})
    return out


def retime_words(words: List[dict], text: str) -> List[dict]:
    """*words* with their text replaced by the user's edited *text*.

    The same number of words keeps every timing. A different number is spread
    evenly over the span the original words covered.
    """
    new = str(text or "").split()
    if not new or not words:
        return []
    if len(new) == len(words):
        return [dict(word, text=t) for word, t in zip(words, new)]
    start, end = float(words[0]["start"]), float(words[-1]["end"])
    step = (end - start) / len(new)
    edges = [round(start + i * step, 3) for i in range(len(new))] + [end]
    return [{"text": t, "start": edges[i], "end": edges[i + 1]} for i, t in enumerate(new)]


def cue_words(cues: List[dict]) -> List[dict]:
    """Phrase cues ``{text, start, end}`` as words, each cue's words spread evenly over it.

    For transcripts that only have phrase timing (the one stored when a file is
    indexed); the highlight then follows the phrase's pace, not each syllable.
    """
    out = []
    for cue in cues or []:
        parts = str(cue.get("text") or "").split()
        try:
            start, end = float(cue["start"]), float(cue["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not parts or end <= start:
            continue
        step = (end - start) / len(parts)
        out.extend({"text": t, "start": round(start + i * step, 3), "end": round(start + (i + 1) * step, 3)}
                   for i, t in enumerate(parts))
    return out
