"""Read a HyperFrames project from disk: compositions, timed clips, timing, styles and scripts.

Pure Python (stdlib ``html.parser``): no Qt, no node. What it reads, per the
HyperFrames schema (``@hyperframes/core`` docs, CLI 0.8.126):

* the root composition -- the ``[data-composition-id][data-root="true"]``
  element of ``index.html``, else its first ``data-composition-id`` element
  without a composition ancestor -- with ``data-width`` / ``data-height`` /
  ``data-fps`` / ``data-duration`` and the variables declared on ``<html
  data-composition-variables>``;
* its timed clips (``data-start``): ``<video>`` / ``<img>`` / ``<audio>``
  primitives, nested composition hosts (``data-composition-id``, loaded from
  ``data-composition-src`` -- a ``<template>``, ``<body>`` or bare fragment --
  or defined inline), and other timed elements (graphics: ``<h1 class="clip">``);
* timing: ``data-start`` as seconds or ``<id>`` / ``<id> + n`` / ``<id> - n``
  (the end of another clip of the same composition; cycles and unknown ids
  are errors), ``data-duration`` (images default to 3 s, media to their
  length from ``data-media-start`` over ``data-playback-rate``), tracks
  (``data-track-index``), ``data-volume``, ``data-fade-in`` /
  ``data-fade-out``, ``data-automation`` volume lanes, ``muted`` /
  ``data-has-audio``, ``data-variable-values``, ``data-zenvi-*``;
* CSS: the inline style plus ``<style>`` rules with simple selectors
  (``#id``, ``.class``, tags, attributes, descendant / child) -- enough for
  the absolute layouts HyperFrames projects use;
* each composition's GSAP timeline (:mod:`.gsap`);
* the embedded ``<script type="application/json" id="zenvi-timeline">`` of a
  Zenvi export.

The HyperFrames CLI's own resolver (``hyperframes timeline --json``) is the
source of truth for timing when it is available: pass its JSON as
*cli_timeline* and its absolute starts and durations win for the root
composition's clips. The Python resolver covers the same rules for when
Node is not installed (and for tests).
"""

from __future__ import annotations

import html.parser
import json
import os
import posixpath
import re
import urllib.parse
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from classes.handoff.hyperframes import gsap
from classes.handoff.linked_media import LinkError

INDEX = "index.html"
TIMELINE_SCRIPT_ID = "zenvi-timeline"
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                       "source", "track", "wbr"})
MEDIA_TAGS = ("video", "img", "audio")
NON_VISUAL_TAGS = frozenset({"script", "style", "template", "meta", "link", "noscript", "title", "head", "base"})
IMAGE_DEFAULT_DURATION = 3.0
DEFAULT_SIZE = (1920, 1080)
MAX_COMPOSITION_DEPTH = 16
_START_REF = re.compile(r"^\s*([A-Za-z_][\w:.\-]*)\s*(?:([+-])\s*(\d*\.?\d+))?\s*$")

ProbeFn = Callable[[str], Optional[float]]


class HyperFramesError(LinkError):
    """The HyperFrames project cannot be read as asked; the message says why and what to do."""


# ---------------------------------------------------------------------------
# HTML tree
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class Element:
    tag: str
    attrs: Dict[str, str]
    parent: Optional["Element"]
    line: int
    children: List["Element"] = field(default_factory=list)
    text_parts: List[str] = field(default_factory=list)
    nodes: List[Any] = field(default_factory=list)      # text and elements in document order

    @property
    def id(self) -> str:
        return self.attrs.get("id", "").strip()

    @property
    def classes(self) -> Tuple[str, ...]:
        return tuple(c for c in self.attrs.get("class", "").split() if c)

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.attrs.get(name, default)

    @property
    def text(self) -> str:
        """Direct text (scripts and styles: their source)."""
        return "".join(self.text_parts)

    def all_text(self) -> str:
        parts = [self.text]
        for c in self.children:
            if c.tag not in ("script", "style"):
                parts.append(c.all_text())
        return "".join(parts)

    def iter(self) -> Iterator["Element"]:
        """This element and every descendant, in document order."""
        yield self
        for c in self.children:
            yield from c.iter()

    def ancestors(self) -> Iterator["Element"]:
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def is_inside(self, other: "Element") -> bool:
        return any(a is other for a in self.ancestors())

    def __repr__(self) -> str:
        ident = ("#" + self.id) if self.id else ""
        return "<%s%s line %d>" % (self.tag, ident, self.line)


class _Builder(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, None, 1)
        self.stack: List[Element] = [self.root]

    def _make(self, tag: str, attrs) -> Element:
        parent = self.stack[-1]
        el = Element(tag.lower(), {str(k).lower(): ("" if v is None else str(v)) for k, v in attrs}, parent,
                     self.getpos()[0])
        parent.children.append(el)
        parent.nodes.append(el)
        return el

    def handle_starttag(self, tag, attrs):
        el = self._make(tag, attrs)
        if el.tag not in VOID_TAGS:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self._make(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].text_parts.append(data)
        self.stack[-1].nodes.append(data)


@dataclass
class Document:
    """One parsed HTML file of the project."""

    path: str            # absolute
    rel: str             # project-relative, "/" separated
    root: Element        # the #document node
    source: str = ""

    @property
    def html(self) -> Optional[Element]:
        return next((e for e in self.root.iter() if e.tag == "html"), None)

    def iter(self) -> Iterator[Element]:
        return self.root.iter()

    def by_id(self, ident: str) -> Optional[Element]:
        return next((e for e in self.root.iter() if e.id == ident), None)


def parse_html(text: str, *, path: str = "", rel: str = "") -> Document:
    builder = _Builder()
    builder.feed(text)
    builder.close()
    return Document(path=path, rel=rel, root=builder.root, source=text)


def read_document(project_dir: str, rel: str) -> Document:
    path = os.path.join(project_dir, *rel.split("/"))
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise HyperFramesError(f"cannot read {rel} in {project_dir}: {exc}") from None
    return parse_html(text, path=path, rel=rel)


# ---------------------------------------------------------------------------
# Selectors (CSS rules and GSAP targets)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Compound:
    tag: Optional[str] = None
    ids: Tuple[str, ...] = ()
    classes: Tuple[str, ...] = ()
    attrs: Tuple[Tuple[str, Optional[str], str], ...] = ()   # (name, operator or None, value)

    def matches(self, el: Element) -> bool:
        if self.tag and self.tag != "*" and el.tag != self.tag:
            return False
        if any(el.id != i for i in self.ids):
            return False
        if any(c not in el.classes for c in self.classes):
            return False
        for name, op, value in self.attrs:
            if name not in el.attrs:
                return False
            actual = el.attrs[name]
            if op == "=" and actual != value:
                return False
            if op == "~=" and value not in actual.split():
                return False
            if op == "^=" and not actual.startswith(value):
                return False
            if op == "$=" and not actual.endswith(value):
                return False
            if op == "*=" and value not in actual:
                return False
            if op == "|=" and not (actual == value or actual.startswith(value + "-")):
                return False
        return True


@dataclass(frozen=True)
class Selector:
    """A complex selector: compounds joined by descendant (" ") or child (">") combinators."""

    parts: Tuple[Tuple[str, Compound], ...]   # (combinator before this compound, compound); first is ""
    text: str = ""

    @property
    def specificity(self) -> Tuple[int, int, int]:
        ids = sum(len(c.ids) for _comb, c in self.parts)
        cls = sum(len(c.classes) + len(c.attrs) for _comb, c in self.parts)
        tags = sum(1 for _comb, c in self.parts if c.tag and c.tag != "*")
        return ids, cls, tags

    def matches(self, el: Element) -> bool:
        return _match_from(self.parts, len(self.parts) - 1, el)


def _match_from(parts, index: int, el: Element) -> bool:
    comb, compound = parts[index]
    if not compound.matches(el):
        return False
    if index == 0:
        return True
    if comb == ">":
        return el.parent is not None and _match_from(parts, index - 1, el.parent)
    p = el.parent
    while p is not None:
        if _match_from(parts, index - 1, p):
            return True
        p = p.parent
    return False


_ATTR_SEL = re.compile(r"""\[\s*([\w\-:]+)\s*(?:([~^$*|]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]*)))?\s*\]""")
_COMPOUND_TOKEN = re.compile(r"(\*|[A-Za-z][\w-]*)|#([\w\-]+)|\.([\w\-]+)|(\[[^\]]*\])")


def _parse_compound(text: str) -> Optional[Compound]:
    pos, tag = 0, None
    ids: List[str] = []
    classes: List[str] = []
    attrs: List[Tuple[str, Optional[str], str]] = []
    while pos < len(text):
        m = _COMPOUND_TOKEN.match(text, pos)
        if not m or m.end() == pos:
            return None  # pseudo-classes, escapes, ...: not supported
        if m.group(1):
            if pos != 0:
                return None
            tag = m.group(1).lower()
        elif m.group(2):
            ids.append(m.group(2))
        elif m.group(3):
            classes.append(m.group(3))
        else:
            am = _ATTR_SEL.fullmatch(m.group(4))
            if not am:
                return None
            value = next((v for v in am.group(3, 4, 5) if v is not None), "")
            attrs.append((am.group(1).lower(), am.group(2), value))
        pos = m.end()
    return Compound(tag, tuple(ids), tuple(classes), tuple(attrs))


def parse_selector_list(text: str) -> Optional[List[Selector]]:
    """``"#a, .b > img"`` as Selectors; None when it uses syntax this reader does not support."""
    out: List[Selector] = []
    for raw in _split_commas(text):
        raw = raw.strip()
        if not raw:
            return None
        spaced = re.sub(r"\s*>\s*", " > ", raw)
        tokens = _split_selector(spaced)
        if tokens is None:
            return None
        parts: List[Tuple[str, Compound]] = []
        comb = ""
        for tok in tokens:
            if tok == ">":
                if not parts or comb == ">":
                    return None
                comb = ">"
                continue
            compound = _parse_compound(tok)
            if compound is None:
                return None
            parts.append((comb if parts else "", compound))
            comb = " "
        if not parts or comb == ">":
            return None
        out.append(Selector(tuple(parts), raw))
    return out or None


def _split_commas(text: str) -> List[str]:
    out, depth, cur = [], 0, []
    quote = ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def _split_selector(text: str) -> Optional[List[str]]:
    """Whitespace-separated tokens of a selector, keeping [attr="a b"] together; None for + and ~."""
    tokens, cur, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'" and depth:
            quote = ch
            cur.append(ch)
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch.isspace() and depth == 0:
            if cur:
                tokens.append("".join(cur))
                cur = []
            continue
        if ch in "+~" and depth == 0:
            return None
        cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    return tokens


def selector_matches(text: str, el: Element) -> Optional[bool]:
    """Whether the selector list *text* matches *el*; None when it cannot be read."""
    sels = parse_selector_list(text)
    if sels is None:
        return None
    return any(s.matches(el) for s in sels)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    selectors: Tuple[Selector, ...]
    declarations: Dict[str, str]
    order: int
    supported: bool = True        # False: a selector this reader cannot evaluate


def parse_declarations(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in _split_semicolons(text):
        if ":" not in part:
            continue
        name, value = part.split(":", 1)
        name = name.strip().lower()
        value = re.sub(r"\s*!important\s*$", "", value.strip(), flags=re.I)
        if name:
            out[name] = value
    return out


def _split_semicolons(text: str) -> List[str]:
    out, cur, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def parse_css(text: str, start_order: int = 0) -> List[Rule]:
    """Style rules of a stylesheet (comments, @keyframes and other at-rule blocks skipped; @media flattened)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules: List[Rule] = []
    i, n, order = 0, len(text), start_order
    while i < n:
        brace = text.find("{", i)
        if brace < 0:
            break
        prelude = text[i:brace].strip()
        depth, j = 1, brace + 1
        while j < n and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        body = text[brace + 1:j - 1]
        i = j
        if prelude.startswith("@"):
            if re.match(r"@(media|supports|layer|container)\b", prelude, re.I):
                inner = parse_css(body, order)
                rules.extend(inner)
                order += len(inner)
            continue
        sels = parse_selector_list(prelude)
        decls = parse_declarations(body)
        rules.append(Rule(tuple(sels or ()), decls, order, sels is not None))
        order += 1
    return rules


def computed_style(el: Element, rules: Sequence[Rule]) -> Dict[str, str]:
    """The declarations that apply to *el*: matching rules by specificity and order, then its style attribute."""
    hits = []
    for rule in rules:
        if not rule.supported:
            continue
        best = None
        for sel in rule.selectors:
            if sel.matches(el):
                spec = sel.specificity
                best = spec if best is None or spec > best else best
        if best is not None:
            hits.append((best, rule.order, rule.declarations))
    out: Dict[str, str] = {}
    for _spec, _order, decls in sorted(hits, key=lambda h: (h[0], h[1])):
        out.update(decls)
    out.update(parse_declarations(el.attrs.get("style", "")))
    return out


# ---------------------------------------------------------------------------
# Project model
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class Clip:
    """A timed element of a composition (``data-start``), resolved."""

    id: str
    tag: str
    kind: str                    # video | img | audio | composition | graphics
    element: Element
    composition: str             # id of the composition that declares it
    file: str                    # project-relative html file that declares it
    line: int
    start_attr: str
    start: Optional[float] = None            # composition-local seconds
    duration: Optional[float] = None
    duration_source: str = ""                # authored | media | default | timeline | cli | until-end
    track_index: int = 0
    media_start: float = 0.0
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    playback_rate: float = 1.0
    muted: bool = False
    has_audio: Optional[bool] = None
    src: str = ""
    media_path: Optional[str] = None         # local file (absolute), None when remote / missing
    remote: bool = False
    composition_src: Optional[str] = None    # host: project-relative composition file
    composition_id: Optional[str] = None     # host: data-composition-id
    variable_values: Dict[str, Any] = field(default_factory=dict)
    automation: Optional[dict] = None
    zenvi: Dict[str, str] = field(default_factory=dict)
    style: Dict[str, str] = field(default_factory=dict)
    tweens: List[gsap.Tween] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)     # why Zenvi cannot rebuild it natively

    @property
    def end(self) -> Optional[float]:
        if self.start is None or self.duration is None:
            return None
        return self.start + self.duration

    @property
    def is_primitive(self) -> bool:
        return self.kind in MEDIA_TAGS

    @property
    def label(self) -> str:
        return self.id or "%s (line %d)" % (self.tag, self.line)


@dataclass(eq=False)
class Composition:
    id: str
    file: str                    # project-relative file holding it
    element: Element
    document: Document
    width: int
    height: int
    fps: Optional[Fraction]
    authored_duration: Optional[float]
    variables: List[dict]
    is_root: bool
    inline: bool                 # defined inside its parent's document (no own file)
    rules: List[Rule] = field(default_factory=list)
    clips: List[Clip] = field(default_factory=list)
    timeline: gsap.Timeline = field(default_factory=lambda: gsap.Timeline(variable=None))
    script: gsap.ScriptInfo = field(default_factory=gsap.ScriptInfo)
    parent: Optional["Composition"] = None
    host: Optional[Clip] = None

    @property
    def duration(self) -> Optional[float]:
        """``data-duration`` when authored, else the end of its GSAP timeline and its clips (the
        framework adds its primitives to the timeline); None when that cannot be read exactly."""
        if self.authored_duration is not None:
            return self.authored_duration
        tl = self.timeline
        if not tl.exact_duration or tl.reasons or any(t.start is None for t in tl.tweens):
            return None
        ends = [c.end for c in self.clips]
        if any(e is None for e in ends):
            return None
        value = max([tl.duration] + [float(e) for e in ends if e is not None])
        return value if value > 0 else None

    def clip(self, ident: str) -> Optional[Clip]:
        return next((c for c in self.clips if c.id == ident), None)

    def variable_defaults(self) -> Dict[str, Any]:
        return {str(v.get("id")): v.get("default") for v in self.variables if isinstance(v, dict) and v.get("id")}


@dataclass(eq=False)
class Project:
    root_dir: str
    index: Document
    root: Composition
    compositions: Dict[str, Composition]
    zenvi_timeline: Optional[dict]
    warnings: List[str] = field(default_factory=list)
    cli_timeline_used: bool = False

    @property
    def clips(self) -> List[Clip]:
        return list(self.root.clips)

    def primitives(self) -> List[Clip]:
        return [c for c in self.root.clips if c.is_primitive]

    def hosts(self) -> List[Clip]:
        return [c for c in self.root.clips if c.kind == "composition"]

    def graphics(self) -> List[Clip]:
        return [c for c in self.root.clips if c.kind == "graphics"]

    def composition_for(self, host: Clip) -> Optional[Composition]:
        for comp in self.compositions.values():
            if comp.host is host:
                return comp
        return None


def _float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        out = float(str(value).strip().rstrip("s"))
    except (TypeError, ValueError):
        return default
    return out if out == out and out not in (float("inf"), float("-inf")) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def parse_fps(value: Any) -> Optional[Fraction]:
    """``"30"``, ``"30000/1001"``, ``"29.97"`` as a Fraction; None when absent or invalid."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            fps = Fraction(int(num), int(den))
        else:
            fps = Fraction(text).limit_denominator(1001)
    except (ValueError, ZeroDivisionError):
        return None
    return fps if 0 < fps <= 240 else None


def _json_attr(value: Optional[str], what: str, warnings: List[str]) -> Any:
    if value is None or not value.strip():
        return None
    try:
        return json.loads(value)
    except ValueError as exc:
        warnings.append(f"{what} is not valid JSON ({exc}); it was ignored")
        return None


def resolve_src(raw: str, project_dir: str, base_rel: str = "") -> Tuple[Optional[str], bool]:
    """(local absolute path or None, is remote) for a media ``src``.

    HyperFrames serves the project root as the base URL; renders resolve a
    sub-composition's relative paths against its own file, so ``../`` paths
    are tried against *base_rel*'s folder too.
    """
    text = str(raw or "").strip()
    if not text:
        return None, False
    if re.match(r"^(https?:|data:|blob:)", text, re.I) or text.startswith("//"):
        return None, True
    if text.lower().startswith("file://"):
        path = urllib.parse.unquote(urllib.parse.urlparse(text).path)
        return (path if os.path.isfile(path) else None), False
    text = urllib.parse.unquote(text.split("#", 1)[0].split("?", 1)[0])
    candidates = []
    if text.startswith("/"):
        candidates.append(os.path.join(project_dir, text.lstrip("/")))
    else:
        candidates.append(os.path.join(project_dir, text))
        base = os.path.dirname(base_rel.replace("/", os.sep))
        if base:
            candidates.append(os.path.join(project_dir, base, text))
    for cand in candidates:
        cand = os.path.normpath(cand)
        if os.path.isfile(cand):
            return cand, False
    return None, False


def _root_composition(doc: Document) -> Optional[Element]:
    comps = [e for e in doc.iter() if "data-composition-id" in e.attrs]
    explicit = next((e for e in comps if e.attrs.get("data-root", "").strip().lower() == "true"), None)
    if explicit is not None:
        return explicit
    for e in comps:
        if not any("data-composition-id" in a.attrs for a in e.ancestors()):
            return e
    return None


def _composition_element(doc: Document) -> Optional[Element]:
    """The composition element of a sub-composition file: in <template>, <body> or a bare fragment."""
    template = next((e for e in doc.iter() if e.tag == "template"), None)
    scope = template if template is not None else doc.root
    for e in scope.iter():
        if e is not scope and "data-composition-id" in e.attrs:
            return e
    return None


def _content_scope(doc: Document, el: Element) -> Element:
    """Where a composition's clips live: the element itself, or ``<body>`` for the
    quickstart template's ``<meta data-composition-id ...>`` form."""
    if el.tag in VOID_TAGS or any(a.tag == "head" for a in el.ancestors()):
        body = next((e for e in doc.iter() if e.tag == "body"), None)
        if body is not None:
            return body
    return el


def _scripts_in(scope: Element, skip: Sequence[Element]) -> List[Element]:
    out = []
    for e in scope.iter():
        if e.tag != "script":
            continue
        if any(e is s or e.is_inside(s) for s in skip):
            continue
        kind = e.attrs.get("type", "").strip().lower()
        if kind and kind not in ("text/javascript", "module", "application/javascript"):
            continue
        if e.attrs.get("src"):
            continue
        out.append(e)
    return out


def _scan_scripts(scripts: Sequence[Element]) -> gsap.ScriptInfo:
    merged = gsap.ScriptInfo()
    for s in scripts:
        info = gsap.scan_script(s.text, s.line)
        merged.timelines.update(info.timelines)
        merged.registrations.update(info.registrations)
        merged.calls.extend(info.calls)
        merged.global_tweens.extend(info.global_tweens)
        merged.other_code = merged.other_code or info.other_code
    return merged


def _styles_in(doc: Document) -> List[Rule]:
    rules: List[Rule] = []
    for e in doc.iter():
        if e.tag == "style":
            rules.extend(parse_css(e.text, len(rules)))
    return rules


def _zenvi_attrs(el: Element) -> Dict[str, str]:
    return {k[len("data-zenvi-"):]: v for k, v in el.attrs.items() if k.startswith("data-zenvi-")}


def _media_src(el: Element) -> str:
    src = el.attrs.get("src", "").strip()
    if src:
        return src
    for c in el.children:
        if c.tag == "source" and c.attrs.get("src", "").strip():
            return c.attrs["src"].strip()
    return ""


class _Loader:
    def __init__(self, project_dir: str, probe: Optional[ProbeFn]):
        self.root_dir = project_dir
        self.probe = probe
        self.warnings: List[str] = []
        self.compositions: Dict[str, Composition] = {}
        self._docs: Dict[str, Document] = {}

    def document(self, rel: str) -> Document:
        rel = posixpath.normpath(rel.replace("\\", "/").lstrip("/"))
        if rel.startswith("../") or rel == "..":
            raise HyperFramesError(f"{rel} is outside the project folder; HyperFrames only serves files inside it")
        if rel not in self._docs:
            self._docs[rel] = read_document(self.root_dir, rel)
        return self._docs[rel]

    # -- compositions -------------------------------------------------------
    def build(self, doc: Document, el: Element, *, is_root: bool, inline: bool, parent: Optional[Composition],
              host: Optional[Clip], chain: Tuple[str, ...]) -> Composition:
        comp_id = el.attrs.get("data-composition-id", "").strip() or (host.composition_id if host else "") or "main"
        html_el = doc.html
        variables = _json_attr(html_el.attrs.get("data-composition-variables") if html_el is not None else None,
                               f"data-composition-variables in {doc.rel}", self.warnings) if not inline else None
        width = _int(el.attrs.get("data-width"), 0) or (parent.width if parent else DEFAULT_SIZE[0])
        height = _int(el.attrs.get("data-height"), 0) or (parent.height if parent else DEFAULT_SIZE[1])
        comp = Composition(
            id=comp_id, file=doc.rel, element=el, document=doc, width=width, height=height,
            fps=parse_fps(el.attrs.get("data-fps")), authored_duration=_float(el.attrs.get("data-duration")),
            variables=[v for v in variables if isinstance(v, dict)] if isinstance(variables, list) else [],
            is_root=is_root, inline=inline, rules=_styles_in(doc), parent=parent, host=host)
        key = comp_id
        n = 2
        while key in self.compositions:
            key = "%s#%d" % (comp_id, n)
            n += 1
        self.compositions[key] = comp
        nested_hosts: List[Element] = []
        self._collect_clips(comp, _content_scope(doc, el), nested_hosts)
        # the root and a composition file own every script of their document; an inline
        # composition the scripts inside its host (scripts of deeper inline hosts are theirs)
        comp.script = _scan_scripts(_scripts_in(el if inline else doc.root, skip=nested_hosts))
        comp.timeline = gsap.build_timeline(comp.script, comp_id)
        # nested compositions first: a host's length is its composition's, and siblings may start after it
        for clip in comp.clips:
            if clip.kind != "composition":
                continue
            if len(chain) >= MAX_COMPOSITION_DEPTH:
                raise HyperFramesError("compositions are nested more than %d deep (%s)" % (
                    MAX_COMPOSITION_DEPTH, " -> ".join(chain)))
            if clip.composition_src:
                if clip.composition_src in chain:
                    raise HyperFramesError("composition files include each other: %s" % " -> ".join(
                        chain + (clip.composition_src,)))
                try:
                    sub_doc = self.document(clip.composition_src)
                except HyperFramesError:
                    clip.problems.append(f"its composition file {clip.composition_src} is missing")
                    self.warnings.append(f"{clip.label}: {clip.composition_src} does not exist")
                    continue
                sub_el = _composition_element(sub_doc)
                if sub_el is None:
                    clip.problems.append(f"{clip.composition_src} has no element with data-composition-id")
                    self.warnings.append(f"{clip.label}: {clip.composition_src} has no data-composition-id element")
                    continue
                sub = self.build(sub_doc, sub_el, is_root=False, inline=False, parent=comp, host=clip,
                                 chain=chain + (clip.composition_src,))
            else:
                sub = self.build(doc, clip.element, is_root=False, inline=True, parent=comp, host=clip,
                                 chain=chain)
            if clip.duration is None and _float(clip.element.attrs.get("data-duration")) is None \
                    and sub.duration is not None:
                clip.duration = sub.duration
                clip.duration_source = "timeline"
        self._resolve_timing(comp)
        return comp

    def _collect_clips(self, comp: Composition, scope: Element, nested_hosts: List[Element]) -> None:
        def walk(el: Element) -> None:
            for child in el.children:
                if child.tag in NON_VISUAL_TAGS and child.tag != "template":
                    continue
                if "data-composition-id" in child.attrs:
                    nested_hosts.append(child)
                    comp.clips.append(self._clip(comp, child, "composition"))
                    continue
                if "data-start" in child.attrs:
                    # what is inside a timed element belongs to it (and is rendered with it)
                    comp.clips.append(self._clip(comp, child, child.tag if child.tag in MEDIA_TAGS else "graphics"))
                    continue
                if child.tag in ("video", "audio") and "data-start" not in child.attrs:
                    self.warnings.append(f"<{child.tag}> at {comp.file}:{child.line} has no data-start, so "
                                         "HyperFrames does not play it; it was not imported")
                walk(child)
        walk(scope)

    def _clip(self, comp: Composition, el: Element, kind: str) -> Clip:
        warnings = self.warnings
        clip = Clip(id=el.id, tag=el.tag, kind=kind, element=el, composition=comp.id, file=comp.file, line=el.line,
                    start_attr=el.attrs.get("data-start", "0"))
        clip.track_index = _int(el.attrs.get("data-track-index", el.attrs.get("data-layer")), 0)
        clip.zenvi = _zenvi_attrs(el)
        clip.style = computed_style(el, comp.rules)
        if kind == "composition":
            clip.composition_id = el.attrs.get("data-composition-id", "").strip()
            src = el.attrs.get("data-composition-src", "").strip()
            clip.composition_src = posixpath.normpath(src.replace("\\", "/").lstrip("/")) if src else None
            values = _json_attr(el.attrs.get("data-variable-values"), f"data-variable-values on {clip.label}",
                                warnings)
            clip.variable_values = values if isinstance(values, dict) else {}
        if kind in MEDIA_TAGS:
            clip.src = _media_src(el)
            clip.media_path, clip.remote = resolve_src(clip.src, self.root_dir, comp.file)
            if not clip.src:
                clip.problems.append("it has no src")
            elif clip.remote:
                clip.problems.append(f"its media is not a local file ({clip.src})")
            elif clip.media_path is None:
                clip.problems.append(f"its media {clip.src} is missing")
            clip.media_start = max(0.0, _float(el.attrs.get("data-media-start"), 0.0) or 0.0)
            clip.playback_rate = _float(el.attrs.get("data-playback-rate"), 1.0) or 1.0
            if clip.playback_rate <= 0:
                clip.problems.append("its data-playback-rate is not positive")
                clip.playback_rate = 1.0
            if kind in ("video", "audio"):
                clip.volume = max(0.0, _float(el.attrs.get("data-volume"), 1.0) or 0.0)
                clip.fade_in = max(0.0, _float(el.attrs.get("data-fade-in"), 0.0) or 0.0)
                clip.fade_out = max(0.0, _float(el.attrs.get("data-fade-out"), 0.0) or 0.0)
                clip.muted = "muted" in el.attrs
                has_audio = el.attrs.get("data-has-audio")
                if has_audio is not None:
                    clip.has_audio = has_audio.strip().lower() not in ("false", "0", "no")
                automation = _json_attr(el.attrs.get("data-automation"), f"data-automation on {clip.label}",
                                        warnings)
                if isinstance(automation, dict):
                    clip.automation = automation
        return clip

    # -- timing ---------------------------------------------------------------
    def _resolve_timing(self, comp: Composition) -> None:
        by_id = {c.id: c for c in comp.clips if c.id}
        resolving: List[Clip] = []

        def duration_of(c: Clip) -> Optional[float]:
            if c.duration is not None:
                return c.duration
            authored = _float(c.element.attrs.get("data-duration"))
            if authored is not None and authored >= 0:
                c.duration, c.duration_source = authored, "authored"
                return c.duration
            end_attr = _float(c.element.attrs.get("data-end"))
            if end_attr is not None:
                start = start_of(c)
                if start is not None and end_attr > start:
                    c.duration, c.duration_source = end_attr - start, "authored"
                    return c.duration
            if c.kind == "img":
                c.duration, c.duration_source = IMAGE_DEFAULT_DURATION, "default"
                return c.duration
            if c.kind in ("video", "audio") and c.media_path and self.probe is not None:
                length = self.probe(c.media_path)
                if length:
                    c.duration = max(0.0, (float(length) - c.media_start) / (c.playback_rate or 1.0))
                    c.duration_source = "media"
                    return c.duration
            return None

        def start_of(c: Clip) -> Optional[float]:
            if c.start is not None:
                return c.start
            raw = (c.start_attr or "0").strip()
            number = _float(raw) if not re.search(r"[A-Za-z_]", raw) else None
            if number is not None:
                c.start = max(0.0, number)
                return c.start
            if raw.lower() == "interactive":
                c.problems.append("it starts interactively (data-start=\"interactive\")")
                return None
            m = _START_REF.match(raw)
            if not m:
                raise HyperFramesError(f"{comp.file}:{c.line}: data-start={raw!r} is not a time or a clip id")
            ref = by_id.get(m.group(1))
            if ref is None:
                raise HyperFramesError(f"{comp.file}:{c.line}: {c.label} starts after {m.group(1)!r}, which is not a "
                                       f"clip of composition {comp.id!r}")
            if ref in resolving or ref is c:
                chain = [x.label for x in resolving[resolving.index(ref):]] if ref in resolving else [c.label]
                raise HyperFramesError("data-start references form a cycle in %s: %s" % (
                    comp.file, " -> ".join(chain + [ref.label])))
            resolving.append(c)
            try:
                ref_start = start_of(ref)
                ref_duration = duration_of(ref)
            finally:
                resolving.pop()
            if ref_start is None or ref_duration is None:
                raise HyperFramesError(f"{comp.file}:{c.line}: {c.label} starts after {ref.label}, whose duration is "
                                       "unknown (give it a data-duration)")
            offset = float(m.group(3) or 0.0)
            c.start = max(0.0, ref_start + ref_duration + (offset if m.group(2) != "-" else -offset))
            return c.start

        for c in comp.clips:
            start_of(c)
        for c in comp.clips:
            duration_of(c)
        total = comp.authored_duration
        for c in comp.clips:
            if c.duration is None and c.kind == "graphics" and c.start is not None and total is not None:
                c.duration, c.duration_source = max(0.0, total - c.start), "until-end"


def _apply_cli_timeline(project: Project, cli_timeline: Any) -> bool:
    """Take the CLI's resolved starts / durations for the root composition's clips; True when applied."""
    if not isinstance(cli_timeline, dict):
        return False
    timeline = cli_timeline.get("timeline") if isinstance(cli_timeline.get("timeline"), dict) else cli_timeline
    tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
    if not isinstance(tracks, list):
        return False
    rows: Dict[str, dict] = {}
    for track in tracks:
        for row in (track.get("rows") or []) if isinstance(track, dict) else []:
            if not isinstance(row, dict) or row.get("nested"):
                continue
            if str(row.get("file") or INDEX).replace("\\", "/") != project.root.file:
                continue
            ident = str(row.get("elementId") or row.get("id") or "")
            if ident:
                rows[ident] = row
    used = False
    for clip in project.root.clips:
        row = rows.get(clip.id)
        if row is None:
            continue
        start = _float(row.get("absStart"), None)
        if start is not None:
            clip.start = max(0.0, start)
            used = True
        source = row.get("durationSource")
        duration = _float(row.get("duration"), None)
        if source in ("authored", "media", "default") and duration is not None and duration > 0:
            clip.duration = duration
            clip.duration_source = "cli"
            used = True
        if row.get("trackIndex") is not None:
            clip.track_index = _int(row.get("trackIndex"), clip.track_index)
        volume = _float(row.get("volume"), None)
        if volume is not None and clip.kind in ("video", "audio"):
            clip.volume = volume
        rate = _float(row.get("playbackRate"), None)
        if rate:
            clip.playback_rate = rate
    return used


def _embedded_timeline(doc: Document, warnings: List[str]) -> Optional[dict]:
    for e in doc.iter():
        if e.tag == "script" and e.id == TIMELINE_SCRIPT_ID:
            try:
                data = json.loads(e.text)
            except ValueError as exc:
                warnings.append(f"the embedded zenvi-timeline JSON is damaged ({exc}); the project is read as "
                                "plain HyperFrames")
                return None
            if isinstance(data, dict) and data.get("zenvi_timeline") is not None:
                return data
            warnings.append("the zenvi-timeline script has no zenvi_timeline header; it was ignored")
            return None
    return None


def load_project(project_dir: str, *, probe: Optional[ProbeFn] = None, cli_timeline: Any = None) -> Project:
    """Read the HyperFrames project in *project_dir* (``index.html`` and the compositions it mounts).

    *probe(path) -> seconds* measures media without ``data-duration`` (call
    off the GUI thread; ffprobe). *cli_timeline* is ``hyperframes timeline
    --json`` output: when given, its resolved times win for the root's clips.
    Raises HyperFramesError for a folder that is not a HyperFrames project,
    timing cycles and unknown references.
    """
    root_dir = os.path.abspath(os.path.expanduser(str(project_dir or "")))
    if not os.path.isdir(root_dir):
        raise HyperFramesError(f"{project_dir!r} is not a folder")
    if not os.path.isfile(os.path.join(root_dir, INDEX)):
        raise HyperFramesError(f"{root_dir} has no {INDEX}, so it is not a HyperFrames project (create one with "
                               "`npx hyperframes init`)")
    loader = _Loader(root_dir, probe)
    index = loader.document(INDEX)
    root_el = _root_composition(index)
    if root_el is None:
        raise HyperFramesError(f"{INDEX} has no element with data-composition-id, so HyperFrames cannot play it; "
                               "run `npx hyperframes lint` in the project")
    root = loader.build(index, root_el, is_root=True, inline=False, parent=None, host=None, chain=(INDEX,))
    project = Project(root_dir=root_dir, index=index, root=root, compositions=loader.compositions,
                      zenvi_timeline=_embedded_timeline(index, loader.warnings), warnings=loader.warnings)
    if cli_timeline is not None:
        project.cli_timeline_used = _apply_cli_timeline(project, cli_timeline)
    attach_tweens(project)
    return project


# ---------------------------------------------------------------------------
# Tweens per clip
# ---------------------------------------------------------------------------

def attach_tweens(project: Project) -> None:
    """Give every root clip the tweens that may touch it; an animated ancestor is a problem for primitives."""
    root = project.root
    tweens = [t for t in root.timeline.tweens if not t.is_spacer]
    clip_els = {id(c.element): c for c in root.clips}
    for clip in root.clips:
        clip.tweens = []
    for tw in tweens:
        for clip in root.clips:
            el = clip.element
            if tw.target is None:
                clip.tweens.append(tw)
                continue
            hit = selector_matches(tw.target, el)
            if hit is None or hit:
                clip.tweens.append(tw)
                continue
            if clip.is_primitive:
                for anc in el.ancestors():
                    if anc is root.element:
                        break
                    if id(anc) in clip_els:
                        break
                    anc_hit = selector_matches(tw.target, anc)
                    if anc_hit is None or anc_hit:
                        clip.problems.append(f"its wrapper <{anc.tag}{('#' + anc.id) if anc.id else ''}> is "
                                             f"animated by script (line {tw.line})")
                        break
    for gt in root.script.global_tweens:
        target = gt.args[0] if gt.args else None
        for clip in root.clips:
            if not clip.is_primitive:
                continue
            hit = selector_matches(target, clip.element) if isinstance(target, str) else None
            if hit is None or hit:
                clip.problems.append(f"a global gsap.{gt.method}() (line {gt.line}) may animate it outside the "
                                     "timeline")
    for reason in root.timeline.reasons:
        for clip in root.clips:
            if clip.is_primitive and clip.tweens:
                clip.problems.append("the timeline is not fully readable: " + reason)


def _visual_style(el: Element, comp: Composition) -> bool:
    style = computed_style(el, comp.rules)
    for key in ("background", "background-color", "background-image", "border", "border-width", "box-shadow",
                "outline", "border-top", "border-bottom", "border-left", "border-right"):
        value = style.get(key, "").strip().lower()
        if value and value not in ("none", "transparent", "0", "0px", "initial", "unset", "inherit"):
            return True
    return False


def _visuals(comp: Composition, nodes: Sequence[Element], removed: Set[int], out: List[str]) -> None:
    for child in nodes:
        if id(child) in removed or child.tag in NON_VISUAL_TAGS:
            continue
        label = "<%s%s>" % (child.tag, ("#" + child.id) if child.id else "")
        if child.tag in ("svg", "canvas", "iframe", "picture", "object", "embed", "img", "video"):
            out.append(label)
        elif "data-composition-id" in child.attrs or "data-start" in child.attrs:
            out.append(label)
        elif child.text.strip() or _visual_style(child, comp):
            out.append(label)
        else:
            _visuals(comp, child.children, removed, out)


def remaining_visuals(comp: Composition, removed: Set[int]) -> List[str]:
    """What still draws in *comp* once the elements in *removed* (``id(element)``) are gone (a layer render)."""
    out: List[str] = []
    _visuals(comp, comp.element.children, removed, out)
    if comp.script.other_code and not out:
        out.append("content drawn by script")
    return out


def remaining_visuals_in(comp: Composition, element: Element, removed: Set[int]) -> List[str]:
    """:func:`remaining_visuals` for one subtree of *comp* (the element included)."""
    out: List[str] = []
    _visuals(comp, [element], removed, out)
    return out


__all__ = [
    "HyperFramesError", "Element", "Document", "parse_html", "read_document", "Selector", "parse_selector_list",
    "selector_matches", "Rule", "parse_css", "parse_declarations", "computed_style", "Clip", "Composition",
    "Project", "load_project", "resolve_src", "parse_fps", "attach_tweens", "remaining_visuals",
    "remaining_visuals_in", "INDEX",
    "TIMELINE_SCRIPT_ID", "MEDIA_TAGS",
]
