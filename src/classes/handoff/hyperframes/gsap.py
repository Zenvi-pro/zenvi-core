"""Best-effort reader for the GSAP timelines in HyperFrames compositions (pure Python).

A HyperFrames composition animates with a paused GSAP timeline registered as
``window.__timelines["<composition id>"]``. This module reads the simple,
declarative part of such scripts -- ``tl.to / from / fromTo / set`` calls
with a string selector, a literal vars object and a literal position --
into :class:`Tween` records with absolute start times, placed the way GSAP
places them: a number is absolute seconds, ``"+=n"`` / ``"-=n"`` are
relative to the timeline's end so far, ``"<"`` / ``">"`` (``"<0.2"``,
``">-=0.1"``) to the previous tween's start / end, label names to
``addLabel`` positions, and no position appends at the end.

Anything it does not understand -- computed targets, function or variable
values, ``stagger``, ``repeat``, ``keyframes``, nested timelines, callbacks,
global ``gsap.to`` tweens, ``timeScale`` -- is reported as *unparsed* with a
reason. Nothing is guessed: an importer turns parsed tweens into keyframes
and treats the rest as scripted animation.

Also here: GSAP's standard eases as Python functions (:func:`ease_spec`),
with their exact cubic-bezier pieces where one exists (``none``, ``power1``
and ``power2`` in / out / inOut), so a parsed tween becomes one Zenvi bezier
segment instead of sampled keyframes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_DURATION = 0.5          # GSAP's default tween duration
DEFAULT_EASE = "power1.out"     # GSAP's default ease

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

_PUNCT = sorted(["===", "!==", "...", "**=", "=>", "==", "!=", "<=", ">=", "&&", "||", "??", "?.", "++", "--",
                 "+=", "-=", "*=", "/=", "%=", "**", "{", "}", "(", ")", "[", "]", ";", ",", ".", ":", "?",
                 "=", "+", "-", "*", "/", "%", "<", ">", "!", "&", "|", "^", "~", "@", "#"],
                key=len, reverse=True)
_IDENT_START = re.compile(r"[A-Za-z_$]")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_NUMBER = re.compile(r"0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_REGEX_PREFIX = {"(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "&&", "||", "??", "=>", "return",
                 "typeof", "case", "+", "-", "*", "%", "<", ">", "==", "===", "!=", "!=="}
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


@dataclass(frozen=True)
class Token:
    kind: str       # ident | num | str | template | regex | punct
    value: Any
    line: int
    pos: int


def _read_string(src: str, i: int, quote: str) -> Tuple[str, int, bool]:
    """(decoded text, index after the closing quote, has ${...}) for a string starting at src[i] == quote."""
    out: List[str] = []
    j = i + 1
    has_expr = False
    while j < len(src):
        ch = src[j]
        if ch == "\\" and j + 1 < len(src):
            nxt = src[j + 1]
            if nxt == "u" and src[j + 2:j + 3] == "{":
                end = src.find("}", j + 3)
                code = src[j + 3:end] if end > 0 else ""
                try:
                    out.append(chr(int(code, 16)))
                except ValueError:
                    pass
                j = (end + 1) if end > 0 else j + 2
                continue
            if nxt == "u" and re.match(r"[0-9a-fA-F]{4}", src[j + 2:j + 6] or ""):
                out.append(chr(int(src[j + 2:j + 6], 16)))
                j += 6
                continue
            if nxt == "x" and re.match(r"[0-9a-fA-F]{2}", src[j + 2:j + 4] or ""):
                out.append(chr(int(src[j + 2:j + 4], 16)))
                j += 4
                continue
            if nxt == "\n":
                j += 2
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            j += 2
            continue
        if quote == "`" and ch == "$" and src[j + 1:j + 2] == "{":
            has_expr = True
        if ch == quote:
            return "".join(out), j + 1, has_expr
        if ch == "\n" and quote != "`":
            break  # unterminated string: stop at the line end
        out.append(ch)
        j += 1
    return "".join(out), j, has_expr


def _read_regex(src: str, i: int) -> int:
    """Index after a regex literal starting at src[i] == '/' (best effort)."""
    j = i + 1
    in_class = False
    while j < len(src):
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "\n":
            return j
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
        elif ch == "/":
            j += 1
            while j < len(src) and (src[j].isalnum() or src[j] == "_"):
                j += 1
            return j
        j += 1
    return j


def tokenize(src: str, first_line: int = 1) -> List[Token]:
    """JavaScript tokens of *src* (comments dropped); good enough for timeline-building code."""
    tokens: List[Token] = []
    i, line, n = 0, first_line, len(src)
    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end < 0 else end
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += src.count("\n", i, end)
            i = end
            continue
        if src.startswith("<!--", i) or src.startswith("-->", i):  # legacy HTML comment markers in scripts
            end = src.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch in "\"'`":
            text, j, has_expr = _read_string(src, i, ch)
            kind = "template" if ch == "`" else "str"
            tokens.append(Token(kind, (text, has_expr) if kind == "template" else text, line, i))
            line += src.count("\n", i, j)
            i = j
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUMBER.match(src, i)
            if m:
                text = m.group(0)
                value = float(int(text, 16)) if text[:2].lower() == "0x" else float(text)
                tokens.append(Token("num", value, line, i))
                i = m.end()
                continue
        if _IDENT_START.match(ch):
            m = _IDENT.match(src, i)
            assert m is not None
            tokens.append(Token("ident", m.group(0), line, i))
            i = m.end()
            continue
        if ch == "/":
            prev = tokens[-1] if tokens else None
            if prev is None or (prev.kind == "punct" and prev.value in _REGEX_PREFIX) or (
                    prev.kind == "ident" and prev.value in _REGEX_PREFIX):
                j = _read_regex(src, i)
                tokens.append(Token("regex", src[i:j], line, i))
                i = j
                continue
        for p in _PUNCT:
            if src.startswith(p, i):
                tokens.append(Token("punct", p, line, i))
                i += len(p)
                break
        else:
            i += 1  # a character JavaScript would reject; skip it
    return tokens


# ---------------------------------------------------------------------------
# Literal values
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    """A value that is not a literal (a variable, a call, a function); ``text`` is its source."""

    text: str


_CLOSERS = {"(": ")", "[": "]", "{": "}"}


def _skip_expression(toks: Sequence[Token], i: int, stops: Tuple[str, ...] = (",", ")", "]", "}", ";")) -> int:
    """Index of the token that ends the expression starting at *i* (a stop at depth 0)."""
    depth: List[str] = []
    while i < len(toks):
        t = toks[i]
        if t.kind == "punct":
            if t.value in _CLOSERS:
                depth.append(_CLOSERS[t.value])
            elif depth and t.value == depth[-1]:
                depth.pop()
            elif not depth and t.value in stops:
                return i
            elif t.value in (")", "]", "}"):
                return i  # unbalanced closer: the enclosing construct ends here
        i += 1
    return i


def _source_text(toks: Sequence[Token], a: int, b: int) -> str:
    parts = []
    for t in toks[a:b]:
        if t.kind == "str":
            parts.append(repr(t.value))
        elif t.kind == "template":
            parts.append("`" + t.value[0] + "`")
        elif t.kind == "num":
            parts.append("%g" % t.value)
        else:
            parts.append(str(t.value))
    return " ".join(parts)


def _arith(toks: Sequence[Token], i: int, end: int) -> Optional[float]:
    """The value of a pure-number arithmetic expression toks[i:end] (+ - * / and parentheses), else None."""
    pos = [i]

    def peek():
        return toks[pos[0]] if pos[0] < end else None

    def primary():
        t = peek()
        if t is None:
            raise ValueError
        if t.kind == "num":
            pos[0] += 1
            return float(t.value)
        if t.kind == "punct" and t.value in ("-", "+"):
            pos[0] += 1
            v = primary()
            return -v if t.value == "-" else v
        if t.kind == "punct" and t.value == "(":
            pos[0] += 1
            v = additive()
            t2 = peek()
            if t2 is None or t2.value != ")":
                raise ValueError
            pos[0] += 1
            return v
        raise ValueError

    def term():
        v = primary()
        while True:
            t = peek()
            if t is not None and t.kind == "punct" and t.value in ("*", "/"):
                pos[0] += 1
                r = primary()
                v = v * r if t.value == "*" else (v / r if r else math.inf)
            else:
                return v

    def additive():
        v = term()
        while True:
            t = peek()
            if t is not None and t.kind == "punct" and t.value in ("+", "-"):
                pos[0] += 1
                r = term()
                v = v + r if t.value == "+" else v - r
            else:
                return v

    try:
        value = additive()
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return value if pos[0] == end else None


def parse_value(toks: Sequence[Token], i: int) -> Tuple[Any, int]:
    """(value, next index) of the expression at toks[i]: literals as Python values, anything else a Ref."""
    if i >= len(toks):
        return Ref(""), i
    end = _skip_expression(toks, i)
    t = toks[i]
    if t.kind == "punct" and t.value == "{":
        value, j = _parse_object(toks, i)
        if j == end:
            return value, j
        return Ref(_source_text(toks, i, end)), end
    if t.kind == "punct" and t.value == "[":
        value, j = _parse_array(toks, i)
        if j == end:
            return value, j
        return Ref(_source_text(toks, i, end)), end
    if end == i + 1:
        if t.kind == "num":
            return float(t.value), end
        if t.kind == "str":
            return t.value, end
        if t.kind == "template":
            text, has_expr = t.value
            return (Ref("`" + text + "`") if has_expr else text), end
        if t.kind == "ident":
            if t.value in ("true", "false"):
                return t.value == "true", end
            if t.value in ("null", "undefined"):
                return None, end
            if t.value == "Infinity":
                return math.inf, end
    number = _arith(toks, i, end)
    if number is not None:
        return number, end
    return Ref(_source_text(toks, i, end)), end


def _parse_object(toks: Sequence[Token], i: int) -> Tuple[Any, int]:
    """An object literal at toks[i] == '{'; returns (dict or Ref, index after '}')."""
    out: Dict[str, Any] = {}
    j = i + 1
    while j < len(toks):
        t = toks[j]
        if t.kind == "punct" and t.value == "}":
            return out, j + 1
        if t.kind == "punct" and t.value == ",":
            j += 1
            continue
        if t.kind in ("ident", "str", "num") and j + 1 < len(toks) and toks[j + 1].value == ":":
            key = t.value if t.kind != "num" else ("%g" % t.value)
            value, j = parse_value(toks, j + 2)
            out[str(key)] = value
            continue
        if t.kind == "ident" and j + 1 < len(toks) and toks[j + 1].value in (",", "}"):
            out[t.value] = Ref(t.value)  # shorthand {x}
            j += 1
            continue
        # spread, computed key, method: not a plain literal
        close = _skip_expression(toks, j, stops=("}",))
        return Ref(_source_text(toks, i, close + 1)), close + 1
    return Ref(_source_text(toks, i, j)), j


def _parse_array(toks: Sequence[Token], i: int) -> Tuple[Any, int]:
    out: List[Any] = []
    j = i + 1
    while j < len(toks):
        t = toks[j]
        if t.kind == "punct" and t.value == "]":
            return out, j + 1
        if t.kind == "punct" and t.value == ",":
            j += 1
            continue
        value, j = parse_value(toks, j)
        out.append(value)
    return Ref(_source_text(toks, i, j)), j


def _parse_args(toks: Sequence[Token], i: int) -> Tuple[List[Any], int]:
    """Arguments of a call whose '(' is at toks[i]; returns (values, index after ')')."""
    args: List[Any] = []
    j = i + 1
    while j < len(toks):
        t = toks[j]
        if t.kind == "punct" and t.value == ")":
            return args, j + 1
        if t.kind == "punct" and t.value == ",":
            j += 1
            continue
        value, nj = parse_value(toks, j)
        if nj == j:  # a stray closer: give up on this call
            return args, j + 1
        args.append(value)
        j = nj
    return args, j


def has_ref(value: Any) -> bool:
    """True when *value* (or anything inside it) is not a literal."""
    if isinstance(value, Ref):
        return True
    if isinstance(value, dict):
        return any(has_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(has_ref(v) for v in value)
    return False


# ---------------------------------------------------------------------------
# Scripts: timelines, registrations, calls
# ---------------------------------------------------------------------------

TWEEN_METHODS = ("to", "from", "fromTo", "set")
# Names that mean a script builds or draws content itself (not just animating what the HTML has).
DRAWING_NAMES = frozenset({
    "createElement", "createElementNS", "appendChild", "append", "prepend", "insertBefore", "replaceChildren",
    "insertAdjacentHTML", "innerHTML", "outerHTML", "textContent", "innerText", "cloneNode", "getContext",
    "requestAnimationFrame", "THREE", "PIXI", "lottie", "bodymovin", "d3", "Konva", "fabric", "p5",
})
QUIET_METHODS = ("seek", "pause", "play", "paused", "progress", "totalProgress", "time", "totalTime",
                 "invalidate", "kill", "revert", "restart", "resume", "reversed")


@dataclass
class Call:
    """One method call on a timeline variable (or a global ``gsap.*`` tween)."""

    owner: str          # the timeline variable, or "gsap"
    method: str
    args: List[Any]
    line: int
    text: str


@dataclass
class ScriptInfo:
    """What a composition's scripts build: timelines, their registration and the calls on them."""

    timelines: Dict[str, dict] = field(default_factory=dict)       # variable -> timeline vars ({defaults...})
    registrations: Dict[str, str] = field(default_factory=dict)    # composition id -> variable
    calls: List[Call] = field(default_factory=list)
    global_tweens: List[Call] = field(default_factory=list)        # gsap.to/from/... (not seek-synced)
    other_code: bool = False                                       # the script builds or draws content


def _member_name(toks: Sequence[Token], i: int) -> Tuple[Optional[str], int]:
    """A dotted name ``a.b.c`` starting at toks[i]; (name, index after it)."""
    if i >= len(toks) or toks[i].kind != "ident":
        return None, i
    parts = [toks[i].value]
    j = i + 1
    while j + 1 < len(toks) and toks[j].value == "." and toks[j + 1].kind == "ident":
        if j + 2 < len(toks) and toks[j + 2].value == "(":
            break  # a method call, not part of the name
        parts.append(toks[j + 1].value)
        j += 2
    return ".".join(parts), j


def _is_gsap_timeline(toks: Sequence[Token], i: int) -> bool:
    return (i + 3 < len(toks) and toks[i].value == "gsap" and toks[i + 1].value == "."
            and toks[i + 2].value == "timeline" and toks[i + 3].value == "(")


def _registration(toks: Sequence[Token], i: int) -> Optional[Tuple[str, int]]:
    """``window.__timelines["id"] =`` / ``window.__timelines.id =`` / ``__timelines["id"] =`` at *i*:
    (composition id, index of the value)."""
    j = i
    if toks[j].kind == "ident" and toks[j].value == "window":
        if j + 1 < len(toks) and toks[j + 1].value == ".":
            j += 2
        elif j + 1 < len(toks) and toks[j + 1].value == "[" and j + 2 < len(toks) and \
                toks[j + 2].kind == "str" and toks[j + 2].value == "__timelines":
            j += 3
            if j < len(toks) and toks[j].value == "]":
                j += 1
            else:
                return None
        else:
            return None
        if toks[j - 1].value != "]":
            if j >= len(toks) or toks[j].value != "__timelines":
                return None
            j += 1
    elif toks[j].kind == "ident" and toks[j].value == "__timelines":
        j += 1
    else:
        return None
    if j < len(toks) and toks[j].value == "[" and j + 2 < len(toks) and toks[j + 1].kind in ("str", "template") \
            and toks[j + 2].value == "]":
        key = toks[j + 1].value if toks[j + 1].kind == "str" else toks[j + 1].value[0]
        j += 3
    elif j + 1 < len(toks) and toks[j].value == "." and toks[j + 1].kind == "ident":
        key = toks[j + 1].value
        j += 2
    else:
        return None
    if j < len(toks) and toks[j].value == "=":
        return str(key), j + 1
    return None


def scan_script(src: str, first_line: int = 1) -> ScriptInfo:
    """Timelines, registrations and timeline calls in one script (or several joined)."""
    toks = tokenize(src, first_line)
    info = ScriptInfo()
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        # NAME = gsap.timeline({...})
        if t.kind == "ident" and t.value not in ("const", "let", "var"):
            reg = _registration(toks, i)
            if reg is not None:
                comp_id, vi = reg
                name, after = _member_name(toks, vi)
                if name and (after >= n or toks[after].value in (";", ",", ")", "}")):
                    info.registrations[comp_id] = name
                    i = after
                    continue
                if _is_gsap_timeline(toks, vi):  # window.__timelines["x"] = gsap.timeline(...)
                    args, after = _parse_args(toks, vi + 3)
                    var = "__timelines[%s]" % comp_id
                    info.timelines[var] = args[0] if args and isinstance(args[0], dict) else {}
                    info.registrations[comp_id] = var
                    i = _chain(toks, after, var, info)
                    continue
                i = vi
                continue
            name, after = _member_name(toks, i)
            if name and after + 1 < n and toks[after].value == "=" and _is_gsap_timeline(toks, after + 1):
                args, j = _parse_args(toks, after + 4)
                info.timelines[name] = args[0] if args and isinstance(args[0], dict) else {}
                i = _chain(toks, j, name, info)
                continue
            if name and name in info.timelines and after < n and toks[after].value == ".":
                i = _chain(toks, after, name, info)
                continue
            if name == "gsap" and after + 2 < n and toks[after].value == "." and toks[after + 1].kind == "ident" \
                    and toks[after + 2].value == "(":
                method = toks[after + 1].value
                if method in TWEEN_METHODS:
                    args, j = _parse_args(toks, after + 2)
                    info.global_tweens.append(Call("gsap", method, args, t.line, _source_text(toks, i, j)))
                    i = j
                    continue
            i = max(after, i + 1)
            continue
        i += 1
    info.other_code = any(t.kind == "ident" and t.value in DRAWING_NAMES for t in toks)
    return info


def _chain(toks: Sequence[Token], i: int, owner: str, info: ScriptInfo) -> int:
    """Record ``.method(args)`` calls chained from index *i* (after the owner expression)."""
    n = len(toks)
    while i + 2 < n and toks[i].value == "." and toks[i + 1].kind == "ident" and toks[i + 2].value == "(":
        method = toks[i + 1].value
        start = i
        args, i = _parse_args(toks, i + 2)
        info.calls.append(Call(owner, method, args, toks[start].line, _source_text(toks, start, i)))
    return i


# ---------------------------------------------------------------------------
# Building the timeline (GSAP position rules)
# ---------------------------------------------------------------------------

@dataclass
class Tween:
    """One tween on a composition's timeline, with GSAP's placement resolved."""

    method: str                     # to | from | fromTo | set
    target: Optional[str]           # the selector string; None when the target is not a literal string
    start: Optional[float]          # timeline seconds (None when the position could not be resolved)
    duration: float
    ease: Any                       # the ease string, or a Ref
    to_vars: Dict[str, Any]
    from_vars: Dict[str, Any]
    immediate_render: bool
    line: int
    text: str = ""
    reasons: List[str] = field(default_factory=list)

    @property
    def end(self) -> Optional[float]:
        return None if self.start is None else self.start + self.duration

    @property
    def parsed(self) -> bool:
        return not self.reasons

    @property
    def is_spacer(self) -> bool:
        """``tl.set({}, {}, t)`` / ``tl.to({}, {duration})``: lengthens the timeline, animates nothing."""
        return self.target == ""


@dataclass
class Timeline:
    """A composition's GSAP timeline as far as it could be read."""

    variable: Optional[str]
    tweens: List[Tween] = field(default_factory=list)
    labels: Dict[str, float] = field(default_factory=dict)
    duration: float = 0.0           # end of the last child (seconds)
    exact_duration: bool = True     # False when an unparsed child may make it longer
    reasons: List[str] = field(default_factory=list)   # whole-timeline problems (timeScale, nested timelines)
    callbacks: int = 0

    def tweens_for(self, matcher: Callable[[str], Optional[bool]]) -> List[Tween]:
        """Tweens whose target selector *matcher* says may match (True, or None = cannot tell)."""
        out = []
        for tw in self.tweens:
            if tw.is_spacer:
                continue
            if tw.target is None:
                out.append(tw)
                continue
            hit = matcher(tw.target)
            if hit is None or hit:
                out.append(tw)
        return out


_VAR_KEYS_IGNORED = {"overwrite", "id", "lazy", "data", "inherit", "callbackScope"}
_VAR_KEYS_TIMING = {"duration", "delay", "ease", "immediateRender"}
_VAR_KEYS_UNSUPPORTED = {"repeat", "yoyo", "repeatDelay", "stagger", "keyframes", "onStart", "onUpdate",
                         "onComplete", "onRepeat", "onReverseComplete", "startAt", "runBackwards", "yoyoEase",
                         "repeatRefresh", "motionPath", "morphSVG", "drawSVG", "scrollTrigger", "modifiers",
                         "snap", "endArray", "paused", "reversed"}


def _position(raw: Any, tl_end: float, prev: Optional[Tuple[float, float]], labels: Dict[str, float]
              ) -> Tuple[Optional[float], Optional[str]]:
    """(start time, problem) for a GSAP position parameter."""
    if raw is None:
        return tl_end, None
    if isinstance(raw, bool):
        return None, "position %r is not a time" % raw
    if isinstance(raw, (int, float)):
        return float(raw), None
    if isinstance(raw, Ref):
        return None, "computed position (%s)" % raw.text
    if not isinstance(raw, str):
        return None, "position %r is not a time" % (raw,)
    text = raw.strip()
    m = re.match(r"^([+-])=\s*(-?\d*\.?\d+)$", text)
    if m:
        offset = float(m.group(2))
        return tl_end + (offset if m.group(1) == "+" else -offset), None
    m = re.match(r"^([<>])\s*(?:([+-])=)?\s*(-?\d*\.?\d+)?$", text)
    if m:
        if prev is None:
            base = 0.0
        else:
            base = prev[0] if m.group(1) == "<" else prev[1]
        offset = float(m.group(3)) if m.group(3) else 0.0
        if m.group(2) == "-":
            offset = -offset
        return base + offset, None
    m = re.match(r"^([A-Za-z_$][\w$\-]*)\s*(?:([+-])=\s*(-?\d*\.?\d+))?$", text)
    if m:
        name = m.group(1)
        if name not in labels:
            return None, "label %r is not defined with addLabel" % name
        offset = float(m.group(3)) if m.group(3) else 0.0
        return labels[name] + (offset if m.group(2) != "-" else -offset), None
    if "%" in text:
        return None, "percentage position %r" % raw
    try:
        return float(text), None
    except ValueError:
        return None, "position %r" % raw


def _target_of(arg: Any) -> Tuple[Optional[str], Optional[str]]:
    """(selector, problem) of a tween target argument; "" for an empty-object spacer."""
    if isinstance(arg, str):
        return arg.strip(), None
    if isinstance(arg, dict) and not arg:
        return "", None
    if isinstance(arg, list) and arg and all(isinstance(a, str) for a in arg):
        return ", ".join(a.strip() for a in arg), None
    if isinstance(arg, Ref):
        return None, "target is computed (%s)" % arg.text
    return None, "target %r is not a selector" % (arg,)


def build_timeline(info: ScriptInfo, composition_id: str) -> Timeline:
    """The timeline registered for *composition_id* (or the only timeline), with tweens placed."""
    var = info.registrations.get(composition_id)
    if var is None and len(info.timelines) == 1:
        var = next(iter(info.timelines))
    tl = Timeline(variable=var)
    if var is None:
        if info.timelines:
            tl.reasons.append("no timeline is registered as window.__timelines[%r]" % composition_id)
        return tl
    tl_vars = info.timelines.get(var) or {}
    defaults = tl_vars.get("defaults") if isinstance(tl_vars, dict) else None
    defaults = defaults if isinstance(defaults, dict) else {}
    if isinstance(tl_vars, dict):
        for key in ("repeat", "yoyo", "timeScale"):
            if key in tl_vars:
                tl.reasons.append("the timeline sets %s" % key)
    tl_end = 0.0
    prev: Optional[Tuple[float, float]] = None
    for call in info.calls:
        if call.owner != var:
            continue
        m = call.method
        if m in QUIET_METHODS:
            continue
        if m in ("addLabel", "add") and call.args and isinstance(call.args[0], str):
            start, problem = _position(call.args[1] if len(call.args) > 1 else None, tl_end, prev, tl.labels)
            if problem:
                tl.reasons.append("label %r: %s" % (call.args[0], problem))
            else:
                tl.labels[call.args[0]] = float(start or 0.0)
            continue
        if m in ("add", "addPause"):
            tl.reasons.append("line %d: %s() of something Zenvi cannot read (%s)" % (call.line, m, call.text[:80]))
            tl.exact_duration = False
            continue
        if m == "call":
            tl.callbacks += 1
            start, _problem = _position(call.args[2] if len(call.args) > 2 else None, tl_end, prev, tl.labels)
            if start is not None:
                tl_end = max(tl_end, start)
            continue
        if m in ("timeScale", "duration", "totalDuration"):
            if call.args:
                tl.reasons.append("line %d: the timeline's %s is changed by script" % (call.line, m))
                tl.exact_duration = False
            continue
        if m not in TWEEN_METHODS:
            tl.reasons.append("line %d: timeline.%s() is not understood" % (call.line, m))
            tl.exact_duration = False
            continue
        tween = _tween(call, defaults, tl_end, prev, tl.labels)
        tl.tweens.append(tween)
        if tween.start is None:
            tl.exact_duration = False
            continue
        prev = (tween.start, tween.end or tween.start)
        tl_end = max(tl_end, tween.end or tween.start)
    tl.duration = tl_end
    return tl


def _literal_object(value: Any, what: str, reasons: List[str]) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    reasons.append("%s are not a literal object (%s)" % (what, getattr(value, "text", value)))
    return {}


def _tween(call: Call, defaults: dict, tl_end: float, prev: Optional[Tuple[float, float]],
           labels: Dict[str, float]) -> Tween:
    """One to/from/fromTo/set call placed on the timeline.

    ``to_vars`` are the values the tween ends at (empty for ``from``: it ends
    at the element's current value); ``from_vars`` the values it starts at
    (empty for ``to`` / ``set``: it starts at the current value). Timing keys
    (duration, ease, delay) come from the vars object GSAP reads them from.
    """
    args = list(call.args)
    reasons: List[str] = []
    method = call.method
    target_arg = args[0] if args else None
    if method == "fromTo":
        from_vars = _literal_object(args[1] if len(args) > 1 else {}, "from-vars", reasons)
        to_vars = _literal_object(args[2] if len(args) > 2 else {}, "vars", reasons)
        pos = args[3] if len(args) > 3 else None
        timing = to_vars
    elif method == "from":
        from_vars = _literal_object(args[1] if len(args) > 1 else {}, "vars", reasons)
        to_vars = {}
        pos = args[2] if len(args) > 2 else None
        timing = from_vars
    else:
        from_vars = {}
        to_vars = _literal_object(args[1] if len(args) > 1 else {}, "vars", reasons)
        pos = args[2] if len(args) > 2 else None
        timing = to_vars
    target, problem = _target_of(target_arg)
    if problem:
        reasons.append(problem)
    merged = dict(defaults)
    merged.update(timing)
    duration_raw = 0.0 if method == "set" else merged.get("duration", DEFAULT_DURATION)
    if isinstance(duration_raw, (int, float)) and not isinstance(duration_raw, bool) and duration_raw >= 0:
        duration = float(duration_raw)
    else:
        reasons.append("duration %s is not a number" % getattr(duration_raw, "text", repr(duration_raw)))
        duration = DEFAULT_DURATION
    ease = merged.get("ease", DEFAULT_EASE)
    if isinstance(ease, Ref):
        reasons.append("ease is computed (%s)" % ease.text)
    elif ease is not None and not isinstance(ease, str):
        reasons.append("ease %r is not a name" % (ease,))
    start, pproblem = _position(pos, tl_end, prev, labels)
    if pproblem:
        reasons.append(pproblem)
    delay = merged.get("delay", 0.0)
    if isinstance(delay, (int, float)) and not isinstance(delay, bool):
        if start is not None:
            start += float(delay)
    else:
        reasons.append("delay is computed")
    immediate = merged.get("immediateRender", method in ("from", "fromTo"))
    for key in list(to_vars) + list(from_vars) + list(defaults):
        if key in _VAR_KEYS_UNSUPPORTED:
            reasons.append("uses %s" % key)
    skip = _VAR_KEYS_TIMING | _VAR_KEYS_IGNORED | _VAR_KEYS_UNSUPPORTED
    props_to = {k: v for k, v in to_vars.items() if k not in skip}
    props_from = {k: v for k, v in from_vars.items() if k not in skip}
    for key, value in list(props_to.items()) + list(props_from.items()):
        if has_ref(value):
            reasons.append("%s is computed (%s)" % (key, getattr(value, "text", "")))
    return Tween(method=method, target=target, start=start, duration=duration, ease=ease, to_vars=props_to,
                 from_vars=props_from, immediate_render=bool(immediate) if isinstance(immediate, bool) else True,
                 line=call.line, text=call.text, reasons=sorted(set(reasons)))


# ---------------------------------------------------------------------------
# Property values
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PropValue:
    """A tween property value: an absolute number or a ``+=`` / ``-=`` offset, with its unit."""

    value: float
    relative: bool = False
    unit: str = ""


_VALUE_RE = re.compile(r"^\s*(?:([+\-*/])=)?\s*(-?\d*\.?\d+(?:e[+-]?\d+)?)\s*([a-z%]*)\s*$", re.I)


def prop_value(raw: Any) -> Optional[PropValue]:
    """A number, ``"40px"``, ``"+=20"``, ``"-=0.5"``, ``"45deg"`` ... as a PropValue; None when not numeric."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return PropValue(float(raw)) if math.isfinite(float(raw)) else None
    if not isinstance(raw, str):
        return None
    m = _VALUE_RE.match(raw)
    if not m:
        return None
    op, num, unit = m.group(1), float(m.group(2)), (m.group(3) or "").lower()
    if op in ("*", "/"):
        return None
    if op:
        return PropValue(num if op == "+" else -num, True, unit)
    return PropValue(num, False, unit)


# ---------------------------------------------------------------------------
# Eases
# ---------------------------------------------------------------------------

EaseFn = Callable[[float], float]


@dataclass(frozen=True)
class EaseSpec:
    """An ease: its function on [0, 1] and, when it has one, its exact pieces.

    ``pieces`` cover [0, 1] as ``(t0, t1, shape)`` with shape ``"linear"`` or a
    CSS ``(x1, y1, x2, y2)`` cubic-bezier of that piece (normalized to the
    piece); None when the ease is not a cubic bezier (sample it).
    """

    name: str
    fn: EaseFn
    pieces: Optional[Tuple[Tuple[float, float, Any], ...]] = None


def _from_in(ease_in: EaseFn) -> Tuple[EaseFn, EaseFn]:
    def out(p: float) -> float:
        return 1.0 - ease_in(1.0 - p)

    def in_out(p: float) -> float:
        return ease_in(p * 2.0) / 2.0 if p < 0.5 else 1.0 - ease_in((1.0 - p) * 2.0) / 2.0
    return out, in_out


def _from_out(ease_out: EaseFn) -> EaseFn:
    def in_out(p: float) -> float:
        return (1.0 - ease_out(1.0 - p * 2.0)) / 2.0 if p < 0.5 else ease_out(p * 2.0 - 1.0) / 2.0 + 0.5
    return in_out


def _power(power: float) -> Tuple[EaseFn, EaseFn, EaseFn]:
    def ease_in(p: float) -> float:
        return p ** power

    def ease_out(p: float) -> float:
        return 1.0 - (1.0 - p) ** power

    def in_out(p: float) -> float:
        return (p * 2.0) ** power / 2.0 if p < 0.5 else 1.0 - ((1.0 - p) * 2.0) ** power / 2.0
    return ease_in, ease_out, in_out


def _linear(p: float) -> float:
    return p


def _sine_in(p: float) -> float:
    return 1.0 if p == 1 else 1.0 - math.cos(p * math.pi / 2.0)


def _expo_in(p: float) -> float:
    return 2.0 ** (10.0 * (p - 1.0)) if p else 0.0


def _circ_in(p: float) -> float:
    return -(math.sqrt(max(0.0, 1.0 - p * p)) - 1.0)


def _bounce_out(p: float) -> float:
    n, c = 7.5625, 2.75
    n1 = 1.0 / c
    if p < n1:
        return n * p * p
    if p < 2.0 * n1:
        return n * (p - 1.5 / c) ** 2 + 0.75
    if p < 2.5 * n1:
        return n * (p - 2.25 / c) ** 2 + 0.9375
    return n * (p - 2.625 / c) ** 2 + 0.984375


def _back(overshoot: float) -> EaseFn:
    def ease_in(p: float) -> float:
        return p * p * ((overshoot + 1.0) * p - overshoot) if p else 0.0
    return ease_in


def _elastic(kind: str, amplitude: float, period: Optional[float]) -> EaseFn:
    p1 = amplitude if amplitude >= 1 else 1.0
    p2 = (period or (0.3 if kind else 0.45)) / (amplitude if amplitude < 1 else 1.0)
    try:
        p3 = p2 / (2 * math.pi) * (math.asin(1.0 / p1) or 0.0)
    except ValueError:
        p3 = 0.0
    p2 = 2 * math.pi / p2

    def ease_out(p: float) -> float:
        return 1.0 if p == 1 else p1 * (2.0 ** (-10.0 * p)) * math.sin((p - p3) * p2) + 1.0
    if kind == "out":
        return ease_out
    if kind == "in":
        return lambda p: 1.0 - ease_out(1.0 - p)
    return _from_out(ease_out)


def _steps(count: int) -> EaseFn:
    count = max(1, int(count))
    p2 = 1.0 / count
    p3 = count + 1
    upper = 1.0 - 1e-8

    def ease(p: float) -> float:
        return int(p3 * max(0.0, min(upper, p))) * p2
    return ease


_POWER_NAMES = {"power0": 1, "linear": 1, "none": 1, "power1": 2, "quad": 2, "power2": 3, "cubic": 3,
                "power3": 4, "quart": 4, "power4": 5, "quint": 5, "strong": 5}
_EXACT = {  # cubic-bezier pieces of the eases that are cubic polynomials of t
    ("2", "in"): ((0.0, 1.0, (1 / 3, 0.0, 2 / 3, 1 / 3)),),
    ("2", "out"): ((0.0, 1.0, (1 / 3, 2 / 3, 2 / 3, 1.0)),),
    ("2", "inout"): ((0.0, 0.5, (1 / 3, 0.0, 2 / 3, 1 / 3)), (0.5, 1.0, (1 / 3, 2 / 3, 2 / 3, 1.0))),
    ("3", "in"): ((0.0, 1.0, (1 / 3, 0.0, 2 / 3, 0.0)),),
    ("3", "out"): ((0.0, 1.0, (1 / 3, 1.0, 2 / 3, 1.0)),),
    ("3", "inout"): ((0.0, 0.5, (1 / 3, 0.0, 2 / 3, 0.0)), (0.5, 1.0, (1 / 3, 1.0, 2 / 3, 1.0))),
}
_LEGACY_SUFFIX = {"easein": "in", "easeout": "out", "easeinout": "inout"}


def ease_spec(name: Any) -> Optional[EaseSpec]:
    """The GSAP ease named *name* (``"power2.out"``, ``"back.out(1.7)"``, ``"steps(4)"``, ``"none"``...).

    None when the name is not one of GSAP's standard eases (a CustomEase, a
    plugin ease, a function).
    """
    if name is None:
        name = DEFAULT_EASE
    if not isinstance(name, str):
        return None
    raw = name.strip()
    text = raw.lower().replace(" ", "")
    if text in ("", "none", "linear", "power0", "power0.in", "power0.out", "power0.inout", "linear.none",
                "linear.easenone", "none.none"):
        return EaseSpec(raw or "none", _linear, ((0.0, 1.0, "linear"),))
    m = re.match(r"^steps\((\d+)(?:,\w+)?\)$", text)
    if m:
        return EaseSpec(raw, _steps(int(m.group(1))))
    m = re.match(r"^([a-z0-9]+)(?:\.([a-z]+))?(?:\(([^)]*)\))?$", text)
    if not m:
        return None
    family, kind, params = m.group(1), (m.group(2) or "out"), m.group(3)
    kind = _LEGACY_SUFFIX.get(kind, kind)
    if kind not in ("in", "out", "inout"):
        return None
    nums: List[float] = []
    if params:
        try:
            nums = [float(p) for p in params.split(",") if p.strip()]
        except ValueError:
            return None
    if family in _POWER_NAMES:
        power = _POWER_NAMES[family]
        if power == 1:
            return EaseSpec(raw, _linear, ((0.0, 1.0, "linear"),))
        ease_in, ease_out, in_out = _power(power)
        fn = {"in": ease_in, "out": ease_out, "inout": in_out}[kind]
        return EaseSpec(raw, fn, _EXACT.get((str(power), kind)))
    if family == "sine":
        ease_in = _sine_in
    elif family == "expo":
        ease_in = _expo_in
    elif family == "circ":
        ease_in = _circ_in
    elif family == "back":
        ease_in = _back(nums[0] if nums else 1.70158)
    elif family == "bounce":
        out = _bounce_out
        fn = {"out": out, "in": lambda p: 1.0 - out(1.0 - p), "inout": _from_out(out)}[kind]
        return EaseSpec(raw, fn)
    elif family == "elastic":
        return EaseSpec(raw, _elastic(kind, nums[0] if nums else 1.0, nums[1] if len(nums) > 1 else None))
    else:
        return None
    out, in_out = _from_in(ease_in)
    return EaseSpec(raw, {"in": ease_in, "out": out, "inout": in_out}[kind])


__all__ = [
    "Token", "tokenize", "Ref", "parse_value", "has_ref", "Call", "ScriptInfo", "scan_script", "Tween",
    "Timeline", "build_timeline", "PropValue", "prop_value", "EaseSpec", "ease_spec", "DEFAULT_DURATION",
    "DEFAULT_EASE",
]
