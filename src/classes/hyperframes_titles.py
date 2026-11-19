"""Titles as HyperFrames compositions: animated, frame-exact HTML rendered on this computer.

A title style is a small HTML page Zenvi writes itself (no registry fetch): the
title's words go in as escaped text, the look is CSS sized from the frame's
short side, and the motion is one paused GSAP timeline the HyperFrames renderer
seeks frame by frame. The render is a transparent video placed above the
footage (an opaque one for a full-frame card). No Qt here.
"""

from __future__ import annotations

import html
import json
import re
from typing import Dict, List, Optional

SOURCE = "hyperframes_title"          # ai_metadata.source of a title file; ai_metadata.title holds its spec
GSAP = "https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"
DEFAULT_FONT = "Inter"
DEFAULT_STYLE = "headline"

# zone: where the style sits by default. full_frame: it paints the whole frame (opaque unless the
# background colour is see-through). fields: which of title / subtitle / lines it draws.
STYLES: Dict[str, Dict] = {
    "headline": {
        "label": "Headline", "zone": "center", "fields": ("title", "subtitle"),
        "description": "Big bold title whose words rise one by one out of a mask, an accent rule drawing "
                       "under it and an optional subtitle fading up. Openers, chapter titles, hooks.",
    },
    "lower_third": {
        "label": "Lower third", "zone": "bottom", "fields": ("title", "subtitle"),
        "description": "Name (title) and role (subtitle) on a dark plate that wipes in behind an accent bar, "
                       "bottom left. Speaker and interview intros, locations.",
    },
    "end_card": {
        "label": "End card", "zone": "center", "fields": ("title", "subtitle"), "full_frame": True,
        "description": "Full-frame card (background_color, default near-black) with a large title easing in "
                       "and a tracked uppercase subtitle. Endings, 'Thanks for watching', section cards.",
    },
    "bold_box": {
        "label": "Bold box", "zone": "center", "fields": ("title", "subtitle"),
        "description": "Social-style text: each line on a solid box (accent_color, default white) that wipes "
                       "in, dark text. Hooks and punchy statements on reels, shorts and TikToks.",
    },
    "kicker": {
        "label": "Kicker", "zone": "top", "fields": ("title", "subtitle"),
        "description": "Small tracked uppercase label between two thin rules, with an optional second line. "
                       "Place and date stamps, chapter labels, small headers.",
    },
    "credits": {
        "label": "Credits", "zone": "center", "fields": ("lines",),
        "description": "One line per entry (lines), fading up in turn; more than six lines roll up the "
                       "screen over the title's duration. Credits, lists, thank-yous.",
    },
}

ZONES = ("top", "center", "bottom")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
_FONT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
ROLL_LINES = 6            # more lines than this roll instead of fading in place


class TitleError(Exception):
    """A request the title builder cannot honour."""


def title_spec(file_data) -> Optional[Dict]:
    """The spec of a HyperFrames title file (its Project Files data), else None."""
    ai = (file_data or {}).get("ai_metadata") if isinstance(file_data, dict) else None
    if isinstance(ai, dict) and ai.get("source") == SOURCE and isinstance(ai.get("title"), dict):
        return ai["title"]
    return None


def style_entry(style: str) -> Dict:
    if style not in STYLES:
        raise TitleError("unknown title style %r; choose one of: %s" % (style, ", ".join(STYLES)))
    return STYLES[style]


def check_font(font: str) -> str:
    font = str(font or "").strip()
    if font and not _FONT_RE.match(font):
        raise TitleError("font must be a family name such as 'Georgia' or 'Playfair Display'")
    return font


def _hex(value: str, what: str) -> str:
    value = str(value or "").strip()
    if value and not _HEX_RE.match(value):
        raise TitleError("%s must be '#RRGGBB' or '#RRGGBBAA'" % what)
    return value


def _css_colour(value: str) -> str:
    if len(value) == 9:
        r, g, b, a = (int(value[i:i + 2], 16) for i in (1, 3, 5, 7))
        return "rgba(%d,%d,%d,%s)" % (r, g, b, _num(a / 255.0))
    return value


def opaque(colour: str) -> bool:
    return bool(colour) and (len(colour) == 7 or colour[7:].lower() == "ff")


def _num(value: float) -> str:
    return ("%.3f" % float(value)).rstrip("0").rstrip(".") or "0"


def _words(text: str) -> str:
    """Each word in a mask span (for the rise), the spaces between them kept as text."""
    return " ".join('<span class="m"><span class="w">%s</span></span>' % html.escape(w)
                    for w in str(text).split())


def _fit(text: str, base: float, chars: int, floor: float = 0.5) -> float:
    """A font size for *text*: *base* while the longest line has at most *chars* characters, smaller after."""
    longest = max((len(line) for line in str(text).split("\n")), default=0)
    return base if longest <= chars else base * max(floor, (chars / float(longest)) ** 0.5)


def spec(style: str, title: str = "", subtitle: str = "", lines: Optional[List[str]] = None,
         text_color: str = "", accent_color: str = "", background_color: str = "", font: str = "",
         font_scale: float = 1.0, zone: str = "") -> Dict:
    """The validated description of a title: what the composition is built from and what an edit changes."""
    entry = style_entry(style)
    lines = [str(v).strip() for v in (lines or []) if str(v).strip()]
    title, subtitle = str(title or "").strip(), str(subtitle or "").strip()
    if "lines" in entry["fields"]:
        lines = lines or [v.strip() for v in (title.split("\n") + [subtitle]) if v.strip()]
        title, subtitle = "", ""
    elif lines:
        title, subtitle = (("\n".join(lines), "") if style == "bold_box" else (lines[0], " ".join(lines[1:])))
    elif "\n" in title and style != "bold_box":
        # 'Jane Doe\nFounder': the second line is the subtitle, as in a lower third's name and role
        first, rest = title.split("\n", 1)
        title, subtitle = first.strip(), " ".join(v.strip() for v in [rest, subtitle] if v.strip())
    zone = zone or entry["zone"]
    if zone not in ZONES:
        raise TitleError("screen_position must be one of: auto, " + ", ".join(ZONES))
    scale = float(font_scale or 1.0)
    if not 0.2 <= scale <= 5.0:
        raise TitleError("font_scale must be between 0.2 and 5")
    background = _hex(background_color, "background_color")
    if not (title or subtitle or lines or background):
        raise TitleError("a title needs text (or subtitle / lines), or a background_color for a blank colour card")
    return {"style": style, "title": title, "subtitle": subtitle, "lines": lines,
            "text_color": _hex(text_color, "text_color"), "accent_color": _hex(accent_color, "accent_color"),
            "background_color": background, "font": check_font(font), "font_scale": scale, "zone": zone}


def is_opaque(s: Dict) -> bool:
    """True when the render paints every pixel (a full-frame card with a solid background)."""
    background = s.get("background_color") or ("#0b0b0f" if STYLES[s["style"]].get("full_frame") else "")
    return opaque(background)


def _safe(width: int, height: int) -> Dict[str, float]:
    """Title-safe margins in px; on 9:16 and taller, clear of the platforms' UI bands."""
    tall = height >= width * 1.6
    return {"x": width * 0.07, "top": height * (0.09 if tall else 0.08), "bottom": height * (0.16 if tall else 0.09)}


def _body(s: Dict, u: float) -> (str, str):
    """(HTML, GSAP script) of the title's own layer; *u* px is 1/1080 of the frame's short side."""
    style, k = s["style"], s["font_scale"]
    title, sub = s["title"], s["subtitle"]
    px = lambda n: _num(n * u * k)                                       # noqa: E731
    if style == "headline":
        size = _fit(title, 124, 16)
        html_ = ('<div class="stack center">%s%s%s</div>' % (
            '<h1 class="t" style="font-size:%spx">%s</h1>' % (px(size), _words(title)) if title else "",
            '<div class="rule" style="height:%spx;width:%spx;margin:%spx auto"></div>' % (px(7), px(150), px(26))
            if title else "",
            '<p class="s fade" style="font-size:%spx">%s</p>' % (px(_fit(sub, 46, 40)), html.escape(sub))
            if sub else ""))
        js = ('tl.fromTo(".w",{yPercent:115},{yPercent:0,duration:0.75,ease:"power4.out",stagger:0.07},0.05);'
              'tl.fromTo(".rule",{scaleX:0},{scaleX:1,duration:0.7,ease:"expo.out"},0.3);'
              'tl.fromTo(".fade",{opacity:0,y:%s},{opacity:1,y:0,duration:0.6,ease:"power3.out"},0.5);' % px(24))
        exit_ = 'tl.to(".stack",{opacity:0,y:%s,duration:OUT,ease:"power2.in"},END-OUT);' % px(-30)
    elif style == "lower_third":
        html_ = ('<div class="lt"><div class="bar" style="width:%spx"></div><div class="plate" '
                 'style="padding:%spx %spx %spx %spx">%s%s</div></div>' % (
                     px(10), px(22), px(44), px(24), px(34),
                     '<div class="m"><div class="n" style="font-size:%spx">%s</div></div>' % (
                         px(_fit(title, 56, 26)), html.escape(title)) if title else "",
                     '<div class="m"><div class="r" style="font-size:%spx;margin-top:%spx">%s</div></div>' % (
                         px(_fit(sub, 32, 44)), px(6), html.escape(sub)) if sub else ""))
        js = ('tl.fromTo(".bar",{scaleY:0},{scaleY:1,duration:0.45,ease:"expo.out"},0);'
              'tl.fromTo(".plate",{clipPath:"inset(0 100% 0 0)"},{clipPath:"inset(0 0% 0 0)",duration:0.7,'
              'ease:"expo.inOut"},0.15);'
              'tl.fromTo(".n",{yPercent:110},{yPercent:0,duration:0.6,ease:"power4.out"},0.45);'
              'tl.fromTo(".r",{yPercent:110},{yPercent:0,duration:0.6,ease:"power4.out"},0.58);')
        exit_ = ('tl.to(".plate",{clipPath:"inset(0 100% 0 0)",duration:OUT,ease:"expo.in"},END-OUT);'
                 'tl.to(".bar",{scaleY:0,duration:OUT*0.6,ease:"power2.in"},END-OUT*0.6);')
    elif style == "end_card":
        html_ = '<div class="stack center">%s%s</div>' % (
            '<h1 class="t big" style="font-size:%spx">%s</h1>' % (px(_fit(title, 112, 18)), html.escape(title))
            if title else "",
            '<p class="s caps fade" style="font-size:%spx;margin-top:%spx">%s</p>' % (
                px(_fit(sub, 32, 42)), px(30), html.escape(sub)) if sub else "")
        js = ('tl.fromTo(".big",{opacity:0,scale:0.92,filter:"blur(%spx)"},{opacity:1,scale:1,filter:"blur(0px)",'
              'duration:1.1,ease:"expo.out"},0.1);'
              'tl.fromTo(".fade",{opacity:0,letterSpacing:"0.5em"},{opacity:0.8,letterSpacing:"0.24em",'
              'duration:1.2,ease:"expo.out"},0.45);' % px(18))
        exit_ = 'tl.to(".stack",{opacity:0,duration:OUT,ease:"power2.in"},END-OUT);'
    elif style == "bold_box":
        rows = [r for r in title.split("\n") if r.strip()] + ([sub] if sub else [])
        size = _fit("\n".join(rows), 76, 20)
        html_ = '<div class="stack center">%s</div>' % "".join(
            '<div class="boxrow"><span class="box" style="font-size:%spx;padding:%spx %spx">%s</span></div>' % (
                px(size * (0.72 if sub and i == len(rows) - 1 else 1)), px(10), px(26), html.escape(r))
            for i, r in enumerate(rows))
        js = ('tl.fromTo(".box",{clipPath:"inset(0 100% 0 0)"},{clipPath:"inset(0 0% 0 0)",duration:0.5,'
              'ease:"expo.out",stagger:0.12},0.05);'
              'tl.fromTo(".box",{scale:0.96},{scale:1,duration:0.6,ease:"back.out(1.6)",stagger:0.12},0.05);')
        exit_ = 'tl.to(".box",{clipPath:"inset(0 0 0 100%)",duration:OUT,ease:"expo.in",stagger:0.05},END-OUT);'
    elif style == "kicker":
        html_ = '<div class="stack center"><div class="kick"><span class="line"></span>%s<span class="line">' \
                '</span></div>%s</div>' % (
                    '<span class="k" style="font-size:%spx;margin:0 %spx">%s</span>' % (
                        px(_fit(title, 30, 34)), px(22), html.escape(title)) if title else "",
                    '<p class="s fade" style="font-size:%spx;margin-top:%spx">%s</p>' % (
                        px(_fit(sub, 26, 50)), px(14), html.escape(sub)) if sub else "")
        js = ('tl.fromTo(".k",{opacity:0,letterSpacing:"0.7em"},{opacity:1,letterSpacing:"0.32em",duration:1.0,'
              'ease:"expo.out"},0.05);'
              'tl.fromTo(".line",{scaleX:0},{scaleX:1,duration:0.8,ease:"expo.out"},0.2);'
              'tl.fromTo(".fade",{opacity:0},{opacity:0.75,duration:0.6},0.5);')
        exit_ = 'tl.to(".stack",{opacity:0,duration:OUT,ease:"power2.in"},END-OUT);'
    else:                                                           # credits
        rows = s["lines"]
        size = _fit("\n".join(rows), 46, 34)
        html_ = '<div class="stack center roll">%s</div>' % "".join(
            '<p class="c" style="font-size:%spx;margin:%spx 0">%s</p>' % (px(size), px(size * 0.32), html.escape(r))
            for r in rows)
        if len(rows) > ROLL_LINES:
            js = 'tl.fromTo(".roll",{yPercent:0,y:H*0.55},{yPercent:-100,y:-H*0.55,duration:END,ease:"none"},0);'
            exit_ = ""
        else:
            js = 'tl.fromTo(".c",{opacity:0,y:%s},{opacity:1,y:0,duration:0.6,ease:"power3.out",stagger:0.18},0.1);' \
                 % px(20)
            exit_ = 'tl.to(".roll",{opacity:0,duration:OUT,ease:"power2.in"},END-OUT);'
    return html_, js + exit_


def compile_composition(s: Dict, width: int, height: int, duration: float, fps: float = 30.0) -> str:
    """The full HyperFrames composition (index.html) for title spec *s* at *width* x *height*."""
    width, height, duration = int(width), int(height), float(duration)
    entry = STYLES[s["style"]]
    u = min(width, height) / 1080.0
    safe = _safe(width, height)
    font = s["font"] or DEFAULT_FONT
    ink = _css_colour(s["text_color"] or ("#111111" if s["style"] == "bold_box" else "#ffffff"))
    accent = _css_colour(s["accent_color"] or ("#ffffff" if s["style"] == "bold_box" else "#ffd166"))
    background = s["background_color"] or ("#0b0b0f" if entry.get("full_frame") else "")
    justify = {"top": "flex-start", "center": "center", "bottom": "flex-end"}[s["zone"]]
    body, js = _body(s, u) if (s["title"] or s["subtitle"] or s["lines"]) else ("", "")
    outro = _num(min(0.5, max(duration * 0.2, 0.0)))
    css = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:%(w)dpx;height:%(h)dpx;overflow:hidden;background:transparent}
#title{position:relative;width:%(w)dpx;height:%(h)dpx;overflow:hidden;background:%(bg)s;display:flex;
  flex-direction:column;justify-content:%(justify)s;padding:%(top)spx %(x)spx %(bottom)spx;
  font-family:"%(font)s",Inter,"Segoe UI",Arial,sans-serif;color:%(ink)s;-webkit-font-smoothing:antialiased}
.stack{width:100%%;display:flex;flex-direction:column}
.center{align-items:center;text-align:center}
.t{font-weight:800;line-height:1.02;letter-spacing:-0.03em;text-wrap:balance;max-width:100%%;
  text-shadow:0 %(sh)spx %(shb)spx rgba(0,0,0,0.35)}
.m{display:inline-block;overflow:hidden;vertical-align:bottom;padding-bottom:0.06em}
.w{display:inline-block}
.rule{background:%(accent)s;border-radius:999px;transform-origin:50%% 50%%}
.s{font-weight:500;line-height:1.3;opacity:0.9;text-wrap:balance;max-width:90%%;
  text-shadow:0 %(sh)spx %(shb)spx rgba(0,0,0,0.35)}
.caps{text-transform:uppercase;letter-spacing:0.24em;font-weight:600}
.lt{display:flex;align-items:stretch;align-self:flex-start;max-width:78%%}
.bar{background:%(accent)s;transform-origin:50%% 100%%}
.plate{background:rgba(12,12,16,0.82);display:flex;flex-direction:column;justify-content:center}
.n{font-weight:700;line-height:1.1;letter-spacing:-0.01em;white-space:nowrap}
.r{font-weight:500;line-height:1.2;opacity:0.78;white-space:nowrap}
.boxrow{line-height:1;margin:%(gap)spx 0}
.box{display:inline-block;background:%(accent)s;color:%(ink)s;font-weight:800;line-height:1.15;
  letter-spacing:-0.01em;border-radius:%(rad)spx}
.kick{display:flex;align-items:center;justify-content:center;width:100%%}
.line{display:block;width:%(lw)spx;height:%(lh)spx;background:%(accent)s}
.k{font-weight:700;text-transform:uppercase;letter-spacing:0.32em;white-space:nowrap}
.c{font-weight:600;line-height:1.2}
""" % {"w": width, "h": height, "bg": _css_colour(background) if background else "transparent",
       "justify": justify, "top": _num(safe["top"]), "x": _num(safe["x"]), "bottom": _num(safe["bottom"]),
       "font": font, "ink": ink, "accent": accent, "sh": _num(4 * u), "shb": _num(24 * u), "gap": _num(6 * u),
       "rad": _num(8 * u), "lw": _num(90 * u), "lh": _num(max(2 * u, 1))}
    fonts = "https://fonts.googleapis.com/css2?family=%s:wght@400;500;600;700;800&display=block" % (
        font.replace(" ", "+"))
    script = ("var END=%s,OUT=%s,H=%d;window.__timelines=window.__timelines||{};"
              "var tl=gsap.timeline({paused:true});%s"
              "tl.set({},{},END);window.__timelines[\"zenvi-title\"]=tl;tl.seek(0);") % (
        _num(duration), outro, height, js)
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=%(w)d, height=%(h)d" />
<link href="%(fonts)s" rel="stylesheet" />
<script src="%(gsap)s"></script>
<style>%(css)s</style>
</head>
<body>
<div id="title" data-composition-id="zenvi-title" data-timeline-locked data-start="0" data-duration="%(d)s"
  data-fps="%(fps)s" data-width="%(w)d" data-height="%(h)d" data-zenvi-title="%(spec)s">%(body)s</div>
<script>%(script)s</script>
</body>
</html>
""" % {"w": width, "h": height, "fonts": html.escape(fonts), "gsap": GSAP, "css": css, "d": _num(duration),
       "fps": _num(fps), "spec": html.escape(json.dumps(s, sort_keys=True)), "body": body, "script": script}
