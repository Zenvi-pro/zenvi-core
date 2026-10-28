"""Reel typography as SVG title images: the hook, a shot label, the end card and the white flash.

Ported from zenvi-web ``packages/engine/src/reels/typography.ts``, so a reel's
titles look the same on the desktop and on the web. Generated rather than
templated so the text fits the frame and follows the style. Only what Qt's SVG
renderer (the desktop), resvg (Zenvi Cloud) and browsers all draw the same:
full-frame ``<rect>``s and centred ``<text>`` with a font stack that ends in
sans-serif; the shadow is a second, offset copy of the text. (Qt's renderer
reads the whole stack as one family name and draws its default sans instead;
the type still fits, its size comes from average glyph widths.)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import List

from classes.editor_tools.timeline_edit_reels_plan import ReelTypography, js_round

FONT = "Inter, 'Helvetica Neue', Helvetica, Arial, 'DejaVu Sans', sans-serif"
# Average advance of a bold sans glyph, in em (capitals run wider).
GLYPH_UPPER = 0.68
GLYPH_MIXED = 0.58


def js_number(value: float) -> str:
    """A number as JavaScript prints it in a template string (76, 72.5)."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def _fixed1(value: float) -> str:
    """JavaScript's ``value.toFixed(1)``: ties round away from zero (11.25 -> 11.3; Python's format gives 11.2)."""
    return str(Decimal(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def cased(text: str, typography: ReelTypography) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean.upper() if typography.case == "upper" else clean


def _balance(words: List[str], lines: int) -> List[str]:
    """Break *words* into *lines* lines of balanced length."""
    if lines <= 1 or len(words) <= 1:
        return [" ".join(words)]
    target = len(" ".join(words)) / lines
    out: List[str] = []
    line: List[str] = []
    for word in words:
        nxt = " ".join([*line, word])
        if line and len(nxt) > target and len(out) < lines - 1:
            out.append(" ".join(line))
            line = [word]
        else:
            line.append(word)
    if line:
        out.append(" ".join(line))
    return out


@dataclass
class Fitted:
    lines: List[str]
    size: int


def fit_text(text: str, typography: ReelTypography, max_width: float, max_height: float,
             max_size: float) -> Fitted:
    """The largest font size (and line breaks) that keeps the text inside max_width x max_height."""
    words = [w for w in text.split(" ") if w]
    glyph = (GLYPH_UPPER if typography.case == "upper" else GLYPH_MIXED) + max(0.0, typography.tracking)
    best = Fitted([text], 1)
    for n in range(1, min(3, max(1, len(words))) + 1):
        lines = _balance(words, n)
        longest = max([len(line) for line in lines] + [1])
        by_width = max_width / (longest * glyph)
        by_height = max_height / (len(lines) * 1.08)
        size = math.floor(min(by_width, by_height, max_size))
        # A line more is only worth it for clearly bigger type.
        if size > best.size * 1.12:
            best = Fitted(lines, size)
    return best


def _text_block(lines: List[str], x: float, first_baseline: float, size: float, line_height: float,
                typography: ReelTypography, fill: str, anchor: str) -> str:
    shadow = max(2, js_round(size * 0.045))
    attrs = (f'font-family="{FONT}" font-size="{js_number(size)}" font-weight="{typography.weight}" '
             f'letter-spacing="{_fixed1(typography.tracking * size)}" text-anchor="{anchor}"')

    def spans(dx: float, dy: float, color: str, opacity: float) -> str:
        return "".join(
            f'<text x="{js_number(x + dx)}" y="{js_round(first_baseline + i * line_height + dy)}" {attrs} '
            f'fill="{color}" fill-opacity="{js_number(opacity)}">{_escape(line)}</text>'
            for i, line in enumerate(lines))

    return spans(shadow, shadow, "#000000", 0.42) + spans(0, 0, fill, 1)


def _svg(width: int, height: int, body: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">{body}</svg>\n')


def card_svg(width: int, height: int, text: str, role: str, typography: ReelTypography,
             subtitle: str = "") -> str:
    """A title card the size of the project frame, transparent around the type.

    role: "hook" (over the opening), "label" (small, top left, on a shot) or
    "end" (the end card, with an optional smaller *subtitle* line).
    """
    w, h = int(width), int(height)
    portrait = h > w
    short = min(w, h)
    body = cased(text, typography)
    parts: List[str] = []
    if role == "label":
        size = js_round(short * 0.034)
        label = replace(typography, tracking=max(typography.tracking, 0.22), weight=600)
        x = js_round(w * 0.07)
        y = js_round(h * (0.085 if portrait else 0.11))
        parts.append(f'<rect x="{x}" y="{y - js_round(size * 1.45)}" width="{js_round(size * 1.6)}" '
                     f'height="{max(2, js_round(size * 0.14))}" fill="#ffffff"/>')
        parts.append(_text_block([cased(text, replace(label, case="upper"))], x, y, size, size, label, "#ffffff",
                                 "start"))
    else:
        end = role == "end"
        if end and typography.end_plate:
            parts.append(f'<rect x="0" y="0" width="{w}" height="{h}" fill="#000000" fill-opacity="0.5"/>')
        max_size = js_round(short * (0.12 if end else 0.135))
        fitted = fit_text(body, typography, w * 0.84, h * (0.26 if portrait else 0.36), max_size)
        line_height = js_round(fitted.size * 1.04)
        centre = h * (0.5 if end else 0.4 if portrait else 0.5)
        sub = cased(subtitle, replace(typography, case="as_is")) if subtitle else ""
        # Readable on a phone: never below 3% of the frame's short side.
        sub_size = max(js_round(fitted.size * 0.42), js_round(short * 0.03))
        block_height = len(fitted.lines) * line_height + (sub_size * 1.8 if sub else 0)
        first_baseline = centre - block_height / 2 + fitted.size * 0.82
        parts.append(_text_block(fitted.lines, js_round(w / 2), first_baseline, fitted.size, line_height, typography,
                                 "#ffffff", "middle"))
        if sub:
            sub_type = replace(typography, weight=400, tracking=max(0.04, typography.tracking / 2))
            y = first_baseline + (len(fitted.lines) - 1) * line_height + sub_size * 2.2
            parts.append(_text_block([sub], js_round(w / 2), y, sub_size, sub_size, sub_type, "#ffffff", "middle"))
    return _svg(w, h, "".join(parts))


def flash_svg(width: int, height: int) -> str:
    """A full-frame white image: the flash on a cut (its alpha is keyframed down over a few frames)."""
    w, h = int(width), int(height)
    return _svg(w, h, f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff"/>')
