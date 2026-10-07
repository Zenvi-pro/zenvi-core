"""
 @file
 @brief What the Index dock shows: the transcript as readable paragraphs, and which word is being spoken at a frame.

 Pure functions (no Qt), so the rules are tested without a window. The dock renders ``reading_html``, follows playback
 with ``word_at`` (a binary search over the words' start frames, cheap enough to run on every preview frame), and
 turns a click on a word into the frame to seek to.
"""

from __future__ import annotations

import bisect
import html
from typing import Any, Callable, Dict, List, Optional, Sequence

PARAGRAPH_GAP_SECONDS = 2.0       # a pause this long, or a change of speaker, starts a new paragraph
LAST_WORD_HOLD_FRAMES = 45        # how long the final word stays lit when nothing follows it


def reading_model(clips: Sequence[Dict[str, Any]], *, fps: float = 30.0,
                  to_frame: Optional[Callable[[float], int]] = None) -> Dict[str, Any]:
    """Words in timeline order, each with the frame it starts on, grouped into paragraphs.

    *clips* is the ``clips`` list of a get_transcript receipt (each with ``words``). A word with no ``startFrame`` gets one
    from its start second (``to_frame``, else ``fps``); a word with neither is left out of the reading.
    Returns ``{"words": [{"text", "frame", "clip", "speaker"}], "paragraphs": [[word index, ...]]}``.
    """
    convert = to_frame or (lambda sec: int(round(float(sec) * fps)))
    words: List[Dict[str, Any]] = []
    for clip in clips or []:
        for w in clip.get("words") or []:
            text = str(w.get("text") or "").strip()
            frame = w.get("startFrame")
            if frame is None:
                sec = w.get("timelineStartSec", w.get("startSec"))
                frame = convert(sec) if sec is not None else None
            if not text or frame is None:
                continue
            words.append({"text": text, "frame": int(frame), "clip": clip.get("clipId") or "", "speaker": w.get("speakerId") or ""})
    words.sort(key=lambda w: w["frame"])
    gap = max(1, int(round(PARAGRAPH_GAP_SECONDS * fps)))
    paragraphs: List[List[int]] = []
    for i, w in enumerate(words):
        prev = words[i - 1] if i else None
        if prev is None or w["speaker"] != prev["speaker"] or w["frame"] - prev["frame"] > gap:
            paragraphs.append([])
        paragraphs[-1].append(i)
    return {"words": words, "paragraphs": paragraphs}


def reading_html(model: Dict[str, Any]) -> str:
    """The paragraphs as HTML: every word a link (``w:<index>``) so a click can seek and playback can light it."""
    words = model["words"]
    out: List[str] = []
    for para in model["paragraphs"]:
        speaker = words[para[0]]["speaker"]
        label = f"<span class='who'>{html.escape(str(speaker))}</span><br>" if speaker else ""
        body = " ".join(f"<a href='w:{i}'>{html.escape(words[i]['text'])}</a>" for i in para)
        out.append(f"<p>{label}{body}</p>")
    return "".join(out)


def word_at(starts: Sequence[int], frame: int) -> int:
    """Index of the word being spoken at *frame* (the last one that has started), or -1 before the first.

    The final word is not held forever: past ``LAST_WORD_HOLD_FRAMES`` of it, nothing is lit.
    """
    if not starts:
        return -1
    i = bisect.bisect_right(starts, frame) - 1
    if i < 0:
        return -1
    if i == len(starts) - 1 and frame - starts[i] > LAST_WORD_HOLD_FRAMES:
        return -1
    return i


def seek_frame(model: Dict[str, Any], link: str) -> Optional[int]:
    """The frame to move the playhead to for a clicked link (``w:<index>``), or None if it is not one of ours."""
    if not str(link).startswith("w:"):
        return None
    try:
        return int(model["words"][int(str(link)[2:])]["frame"])
    except (ValueError, IndexError, KeyError):
        return None
