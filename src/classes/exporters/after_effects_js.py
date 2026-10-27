"""ES3-safe JavaScript literals for the After Effects export, and an ES3 lint.

After Effects runs scripts in ExtendScript, an ECMAScript 3 engine: no
``let``/``const``, arrow functions, template strings, trailing commas,
``JSON`` or ES5 array methods, and reserved words (``default``, ``class``,
``in``, ``int``, ``float`` ...) cannot be property names after a dot or
unquoted object keys. ExtendScript reads a file without a byte-order mark in
the platform encoding, so everything after the first comment line is plain
ASCII: strings carry ``\\uXXXX`` escapes for anything else.

``js_value`` writes data (dicts with quoted keys, lists, numbers, strings,
booleans, None) and ``es3_problems`` lists violations in a script; the
exporter runs it on every script it writes, and the tests run it on the
golden files.
"""

from __future__ import annotations

import math
import re
from typing import Any, List

# ECMAScript 3 reserved words and future reserved words (ECMA-262 3rd edition 7.5.1-7.5.3).
RESERVED = frozenset("""
break case catch continue default delete do else finally for function if in instanceof new return switch
this throw try typeof var void while with abstract boolean byte char class const debugger double enum
export extends final float goto implements import int interface long native package private protected
public short static super synchronized throws transient volatile null true false
""".split())


def js_str(text: Any) -> str:
    """A double-quoted ES3 string literal, ASCII only."""
    out = ['"']
    for ch in str(text):
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 32 <= code < 127:
            out.append(ch)
        elif code <= 0xFFFF:
            out.append("\\u%04x" % code)
        else:  # outside the BMP: a UTF-16 surrogate pair
            code -= 0x10000
            out.append("\\u%04x\\u%04x" % (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)))
    out.append('"')
    return "".join(out)


def js_num(value: Any, digits: int = 6) -> str:
    """A finite number literal with at most *digits* decimals (non-finite values become 0)."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(x):
        return "0"
    rounded = round(x, digits)
    if rounded == 0:
        return "0"
    if rounded == int(rounded) and abs(rounded) < 1e15:
        return str(int(rounded))
    text = ("%." + str(digits) + "f") % rounded
    text = text.rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


_TIME_KEYS = frozenset({"t", "inp", "outp", "start", "dur"})


def js_value(value: Any, *, digits: int = 6, indent: int = 0, width: int = 110, _key: str = "") -> str:
    """*value* as an ES3 literal. Times (keys ``t``, ``inp``, ``outp``, ``start``, ``dur``) keep 9 decimals."""
    pad = " " * indent
    places = 9 if _key in _TIME_KEYS else digits
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return js_num(value, places)
    if isinstance(value, str):
        return js_str(value)
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [js_str(k) + ": " + js_value(v, digits=digits, indent=indent + 4, width=width, _key=str(k))
                 for k, v in value.items()]
        flat = "{" + ", ".join(items) + "}"
        if len(flat) + indent <= width and "\n" not in flat:
            return flat
        inner = (",\n" + pad + "    ").join(items)
        return "{\n" + pad + "    " + inner + "\n" + pad + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = [js_value(v, digits=digits, indent=indent + 4, width=width, _key=_key) for v in value]
        flat = "[" + ", ".join(items) + "]"
        if len(flat) + indent <= width and "\n" not in flat:
            return flat
        if all("\n" not in item for item in items) and all(not isinstance(v, (dict, list, tuple)) for v in value):
            # long arrays of numbers: wrap at the width instead of one item per line
            lines, line = [], ""
            for item in items:
                piece = item + ", "
                if line and len(line) + len(piece) + indent + 4 > width:
                    lines.append(line.rstrip())
                    line = ""
                line += piece
            if line:
                lines.append(line.rstrip().rstrip(","))
            lines = [ln.rstrip(",") if i == len(lines) - 1 else ln for i, ln in enumerate(lines)]
            return "[\n" + pad + "    " + ("\n" + pad + "    ").join(lines) + "\n" + pad + "]"
        inner = (",\n" + pad + "    ").join(items)
        return "[\n" + pad + "    " + inner + "\n" + pad + "]"
    raise TypeError(f"cannot write {type(value).__name__} as a script literal")


# ---------------------------------------------------------------------------
# ES3 lint
# ---------------------------------------------------------------------------

_TOKEN_KEYWORDS = re.compile(r"\b(let|const|class|import|export|yield|async|await)\b")
_PROPERTY_AFTER_DOT = re.compile(r"\.\s*([A-Za-z_$][\w$]*)")
_UNQUOTED_KEY = re.compile(r"[{,]\s*([A-Za-z_$][\w$]*)\s*:")
_TRAILING_COMMA = re.compile(r",\s*[}\]]")
_FOR_OF = re.compile(r"\bfor\s*\([^;)]*\bof\b")


def _code_only(script: str) -> str:
    """The script with string literals, comments and the ``#target`` directive blanked out.

    Keeps offsets (each removed character becomes a space, newlines stay),
    so problems can be reported by line. The export never writes regular
    expression literals, so ``/`` is division or a comment.
    """
    out = list(script)
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if ch in "\"'":
            quote = ch
            j = i + 1
            while j < n and script[j] != quote:
                if script[j] == "\\":
                    j += 1
                elif script[j] == "\n":
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            continue
        if script.startswith("//", i):
            j = script.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if script.startswith("/*", i):
            j = script.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if ch == "#" and (i == 0 or script[i - 1] == "\n"):
            j = script.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def es3_problems(script: str) -> List[str]:
    """ES3 / ExtendScript problems in *script* as ``line N: what`` strings (empty when clean)."""
    problems: List[str] = []

    def line_of(offset: int) -> int:
        return script.count("\n", 0, offset) + 1

    first_newline = script.find("\n")
    for offset, ch in enumerate(script):
        if ord(ch) > 126 and offset > first_newline >= 0:
            problems.append(f"line {line_of(offset)}: non-ASCII character {ch!r} (use a \\u escape)")
            break
    code = _code_only(script)
    checks = [
        (re.compile(r"=>"), "arrow function"),
        (re.compile(r"`"), "template string"),
        (_TOKEN_KEYWORDS, "ES2015+ keyword"),
        (_TRAILING_COMMA, "trailing comma"),
        (re.compile(r"\.\.\."), "spread syntax"),
        (_FOR_OF, "for...of loop"),
    ]
    for pattern, what in checks:
        for m in pattern.finditer(code):
            problems.append(f"line {line_of(m.start())}: {what}: {code[m.start():m.end()].strip()!r}")
    for m in _PROPERTY_AFTER_DOT.finditer(code):
        name = m.group(1)
        before = code[:m.start()].rstrip()
        if before and (before[-1].isdigit() and not re.search(r"[A-Za-z_$][\w$]*$", before)):
            continue  # a number literal such as 1.5
        if name in RESERVED:
            problems.append(f"line {line_of(m.start())}: reserved word as a property name: .{name}")
    for m in _UNQUOTED_KEY.finditer(code):
        if m.group(1) in RESERVED:
            problems.append(f"line {line_of(m.start())}: reserved word as an object key: {m.group(1)}")
    return problems


__all__ = ["RESERVED", "js_str", "js_num", "js_value", "es3_problems"]
