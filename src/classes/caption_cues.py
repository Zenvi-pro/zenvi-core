"""Caption cues and the text format of libopenshot's Caption effect (no Qt).

A Caption effect keeps all of its cues in one ``caption_text`` string, the
format the Captions dock edits and ``actionInsertTimestamp`` writes::

    00:00:00:500 --> 00:00:03:000
    Welcome back to my channel.

    00:00:03:200 --> 00:00:07:500
    Today we are exploring the city.

Times are the clip's own (source) seconds: libopenshot compares them with the
clip frame number, so a clip trimmed to start at 12 s shows the cue written as
``00:00:12:000`` at its first visible frame.

``parse_caption_text`` reads it with the same rules as ``Caption::process_regex``
/ ``GetFrame`` (Caption.cpp): cue text runs until the next ``dd:dd`` (so a
``10:30`` inside text would cut the cue short), lines starting with ``NOTE`` or
one character long are not drawn. ``safe_line`` rewrites text so every line is
drawn as written.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Iterable, List, Tuple

ZERO_WIDTH = "​"
RATIO_COLON = "∶"   # looks like ':' but does not end a cue

# libopenshot Caption.cpp, process_regex()
_CAPTION_RE = re.compile(
    r"(\d{2})?:*(\d{2}):(\d{2}).(\d{2,3})\s*-->\s*(\d{2})?:*(\d{2}):(\d{2}).(\d{2,3})([\s\S]*?)(.*?)(?=\d{2}:\d{2,3}|\Z)")
_TIME_IN_TEXT = re.compile(r"(?<=\d\d):(?=\d\d)")


@dataclass
class Cue:
    start: float     # seconds, in the time base of whoever holds it (clip source time in effects)
    end: float
    text: str

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


# ---------------------------------------------------------------------------
# Caption effect text
# ---------------------------------------------------------------------------

def format_time(seconds: float) -> str:
    """Seconds -> 'HH:MM:SS:mmm' (the Captions dock's timestamps), rounded to the millisecond."""
    ms = int(round(max(0.0, float(seconds)) * 1000.0))
    hours, rem = divmod(ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, milli = divmod(rem, 1000)
    return "%02d:%02d:%02d:%03d" % (hours, minutes, secs, milli)


def _frac_seconds(frac: str) -> float:
    return int((frac or "").ljust(3, "0")[:3]) / 1000.0


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def drawn_lines(text: str) -> List[str]:
    """The lines of a cue libopenshot actually draws."""
    out = []
    for line in str(text or "").split("\n"):
        if not line or line.startswith("NOTE") or _utf16_len(line) <= 1:
            continue
        out.append(line)
    return out


def parse_caption_text(caption_text: str) -> List[Cue]:
    """Cues of a Caption effect, read the way libopenshot reads them."""
    prepared = str(caption_text or "")
    if not prepared.endswith("\n\n"):
        prepared += "\n\n"
    cues = []
    for m in _CAPTION_RE.finditer(prepared):
        start = (float(m.group(1) or 0) * 3600 + float(m.group(2)) * 60 + float(m.group(3))
                 + _frac_seconds(m.group(4)))
        end = (float(m.group(5) or 0) * 3600 + float(m.group(6)) * 60 + float(m.group(7))
               + _frac_seconds(m.group(8)))
        body = (m.group(9) or "") + (m.group(10) or "")
        lines = [ln.strip() for ln in body.split("\n")]
        text = "\n".join(ln for ln in lines if ln)
        cues.append(Cue(start, end, text))
    return cues


def safe_line(line: str) -> str:
    """Rewrite one line so libopenshot draws all of it (see module docstring)."""
    line = _TIME_IN_TEXT.sub(RATIO_COLON, line)
    if line.startswith("NOTE"):
        line = ZERO_WIDTH + line
    if _utf16_len(line) == 1:
        line += ZERO_WIDTH
    return line


def safe_text(text: str) -> Tuple[str, bool]:
    """(drawable text, changed?) -- blank lines dropped, each line made safe."""
    lines = [ln.strip() for ln in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out = [safe_line(ln) for ln in lines if ln]
    result = "\n".join(out)
    return result, result != "\n".join(ln for ln in lines if ln)


def display_text(text: str) -> str:
    """Undo safe_line for showing cue text to people (and to the model)."""
    return str(text or "").replace(ZERO_WIDTH, "").replace(RATIO_COLON, ":")


def build_caption_text(cues: Iterable[Cue]) -> str:
    """Caption effect text for *cues* (sorted by start; text must already be safe)."""
    blocks = []
    for cue in sorted(cues, key=lambda c: (c.start, c.end)):
        blocks.append("%s --> %s\n%s" % (format_time(cue.start), format_time(cue.end), cue.text))
    return "\n\n".join(blocks) + ("\n\n" if blocks else "")


# ---------------------------------------------------------------------------
# Subtitle files (.srt / .vtt)
# ---------------------------------------------------------------------------

_FILE_TIME = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")
_TAGS = re.compile(r"<[^>]{1,80}>|\{\\[^}]{0,80}\}")


def _file_seconds(h, m, s, frac) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + _frac_seconds(frac)


def parse_subtitles(text: str) -> List[Cue]:
    """Cues from SRT or WebVTT text (index lines, cue settings, NOTE/STYLE blocks and tags dropped)."""
    body = str(text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n[ \t]*\n", body.strip()):
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        head = lines[0].strip()
        if head.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")) and "-->" not in head:
            continue
        timing_at = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_at is None:
            continue
        m = _FILE_TIME.search(lines[timing_at])
        if not m:
            continue
        start = _file_seconds(*m.groups()[0:4])
        end = _file_seconds(*m.groups()[4:8])
        text_lines = [html.unescape(_TAGS.sub("", ln)).strip() for ln in lines[timing_at + 1:]]
        caption = "\n".join(ln for ln in text_lines if ln)
        if caption and end > start:
            cues.append(Cue(start, end, caption))
    return cues


def cues_to_srt(cues: Iterable[Cue]) -> str:
    out = []
    for i, cue in enumerate(sorted(cues, key=lambda c: c.start), start=1):
        def ts(sec):
            return format_time(sec)[:8] + "," + format_time(sec)[9:]
        out += [str(i), "%s --> %s" % (ts(cue.start), ts(cue.end)), display_text(cue.text), ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Phrasing long transcript cues into readable captions
# ---------------------------------------------------------------------------

def split_cue(cue: Cue, max_words: int = 8, max_chars: int = 42) -> List[Cue]:
    """Split a long cue into chunks of <= max_words words / <= max_chars characters.

    Time is shared out by character count (transcript cues carry no word
    timings). A one-word tail too short to read on its own joins the chunk before.
    """
    words = str(cue.text or "").split()
    max_words = max(1, int(max_words))
    max_chars = max(8, int(max_chars))
    if not words:
        return []
    if len(words) <= max_words and len(" ".join(words)) <= max_chars:
        return [Cue(cue.start, cue.end, " ".join(words))]
    chunks: List[List[str]] = []
    current: List[str] = []
    for word in words:
        if current and (len(current) >= max_words or len(" ".join(current + [word])) > max_chars):
            chunks.append(current)
            current = []
        current.append(word)
    if current:
        chunks.append(current)
    if len(chunks) > 1 and len(chunks[-1]) == 1 and len(chunks[-1][0]) <= 3:
        chunks[-2].extend(chunks.pop())
    total = sum(len(" ".join(c)) + 1 for c in chunks)
    span = max(0.0, cue.end - cue.start)
    out = []
    t = cue.start
    for i, chunk in enumerate(chunks):
        share = span * (len(" ".join(chunk)) + 1) / float(total)
        end = cue.end if i == len(chunks) - 1 else t + share
        out.append(Cue(t, end, " ".join(chunk)))
        t = end
    return out
