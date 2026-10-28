"""Render wrappers: small HyperFrames entry files that render one part of a project on its own.

A linked clip renders a part of the user's project, not always its
``index.html``:

* a composition loaded from a file (``data-composition-src``) renders
  through a one-host wrapper (:func:`composition_wrapper`) -- HyperFrames can
  only render template sub-compositions mounted from an entry file;
* the graphics *layer* of the root (what stays once the clips Zenvi rebuilt
  natively are taken out) and an *inline* composition render through a copy
  of ``index.html`` with elements hidden (:func:`document_wrapper`). Hidden,
  not removed: the root's scripts still find them by id (a missing element
  would throw before the timeline is registered) and the layout around them
  stays. ``opacity: 0 !important`` hides an element and everything in it
  whatever its scripts set (checked with HyperFrames 0.8.126: a hidden
  ``<video>`` does not appear in the render).

Wrappers live in a hidden ``.render-zenvi-<hex>/`` folder inside the
project (Zenvi's fingerprints skip ``.render-*`` folders, so a render never
makes clips look stale) and are deleted when the render ends
(:func:`wrapper_folder`). HyperFrames resolves ``data-composition-src``
against the project root, but other relative URLs of an entry file against
its own folder, so a document wrapper rebases them with ``../``.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import re
import shutil
import uuid
from typing import Dict, Iterable, Iterator, List, Optional

from classes.handoff.hyperframes.parser import VOID_TAGS, Document, Element

WRAPPER_PREFIX = ".render-zenvi-"
WRAPPER_FILE = "index.html"
WRAPPER_ROOT_ID = "zenvi-wrap"
DEFAULT_GSAP = "https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"
TRANSPARENT_CSS = ("html, body { background: transparent !important; }\n"
                   "[data-composition-id] { background-color: transparent; }")
HIDE_ATTR = "data-zenvi-hidden"
CLEAR_ATTR = "data-zenvi-clear"
HIDE_CSS = ("[%s] { opacity: 0 !important; }\n"
            "[%s] { background: transparent !important; border-color: transparent !important; "
            "box-shadow: none !important; outline: none !important; }") % (HIDE_ATTR, CLEAR_ATTR)
_URL_ATTRS = ("src", "href", "poster", "data", "xlink:href")
_ABSOLUTE = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//|/|#)", re.I)
_CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.I)
_CSS_IMPORT = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.I)


@contextlib.contextmanager
def wrapper_folder(project_dir: str) -> Iterator[str]:
    """A fresh hidden folder in *project_dir* for one render's wrapper; removed afterwards."""
    folder = os.path.join(project_dir, WRAPPER_PREFIX + uuid.uuid4().hex[:8])
    os.makedirs(folder)
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def rel_entry(project_dir: str, path: str) -> str:
    return os.path.relpath(path, project_dir).replace(os.sep, "/")


# ---------------------------------------------------------------------------
# URL rebasing (a document moved one folder down)
# ---------------------------------------------------------------------------

def rebase_url(url: str, prefix: str = "../") -> str:
    text = url.strip()
    if not text or _ABSOLUTE.match(text):
        return url
    return prefix + text


def rebase_css(text: str, prefix: str = "../") -> str:
    def _url(m):
        return "url(%s%s%s)" % (m.group(1), rebase_url(m.group(2), prefix), m.group(1))

    def _import(m):
        return "@import %s%s%s" % (m.group(1), rebase_url(m.group(2), prefix), m.group(1))
    return _CSS_IMPORT.sub(_import, _CSS_URL.sub(_url, text))


def rebase_srcset(text: str, prefix: str = "../") -> str:
    parts = []
    for item in text.split(","):
        bits = item.strip().split()
        if bits:
            bits[0] = rebase_url(bits[0], prefix)
        parts.append(" ".join(bits))
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _attrs(el: Element, overrides: Dict[str, Optional[str]], rebase: bool) -> str:
    attrs = dict(el.attrs)
    for k, v in overrides.items():
        if v is None:
            attrs.pop(k, None)
        else:
            attrs[k] = v
    out = []
    for name, value in attrs.items():
        if rebase:
            if name in _URL_ATTRS and not (el.tag == "a" and name == "href"):
                value = rebase_url(value)
            elif name == "srcset":
                value = rebase_srcset(value)
            elif name == "style":
                value = rebase_css(value)
        out.append(' %s="%s"' % (name, html.escape(value, quote=True)))
    return "".join(out)


def serialize(node: Element, *, skip: Iterable[Element] = (), overrides: Optional[Dict[int, Dict[str, Optional[str]]]] = None,
              rebase: bool = False, head_extra: str = "") -> str:
    """HTML for *node* (a #document or an element), leaving out *skip* and applying attribute *overrides*.

    *rebase* prefixes relative URLs with ``../`` (src, href, poster, srcset,
    CSS ``url()`` / ``@import``), never ``data-composition-src``. *head_extra*
    is inserted at the end of ``<head>``.
    """
    skipped = {id(e) for e in skip}
    over = overrides or {}
    parts: List[str] = []

    def emit(el: Element) -> None:
        if id(el) in skipped:
            return
        if el.tag == "#document":
            for n in el.nodes:
                if isinstance(n, Element):
                    emit(n)
                elif n.strip():
                    parts.append(html.escape(n, quote=False))
            return
        parts.append("<%s%s>" % (el.tag, _attrs(el, over.get(id(el), {}), rebase)))
        if el.tag in VOID_TAGS:
            return
        raw = el.tag in ("script", "style")
        for n in el.nodes:
            if isinstance(n, Element):
                emit(n)
            elif raw:
                parts.append(rebase_css(n) if (rebase and el.tag == "style") else n)
            else:
                parts.append(html.escape(n, quote=False))
        if el.tag == "head" and head_extra:
            parts.append(head_extra)
        parts.append("</%s>" % el.tag)

    emit(node)
    text = "".join(parts)
    if node.tag == "#document":
        text = "<!doctype html>\n" + text
    return text


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------

def head_assets(index: Document) -> str:
    """The root document's script / stylesheet / style tags (GSAP, fonts), rebased one folder down."""
    head = next((e for e in index.iter() if e.tag == "head"), None)
    out: List[str] = []
    have_gsap = False
    for el in (head.children if head is not None else []):
        if el.tag == "script" and el.attrs.get("src"):
            have_gsap = have_gsap or "gsap" in el.attrs["src"].lower()
            out.append(serialize(el, rebase=True))
        elif el.tag == "link" and "stylesheet" in el.attrs.get("rel", "").lower():
            out.append(serialize(el, rebase=True))
        elif el.tag == "style":
            out.append(serialize(el, rebase=True))
    if not have_gsap:
        out.insert(0, '<script src="%s"></script>' % DEFAULT_GSAP)
    return "\n    ".join(out)


def composition_wrapper(index: Document, host: Element, *, width: int, height: int,
                        variables: Optional[dict] = None, fps: Optional[str] = None) -> str:
    """An entry file mounting the composition behind *host* (``data-composition-src``) at 0, on transparent."""
    host_attrs = {k: v for k, v in host.attrs.items()
                  if k not in ("data-start", "data-track-index", "data-duration", "data-variable-values")}
    host_attrs["data-start"] = "0"
    host_attrs["data-track-index"] = "0"
    if variables:
        host_attrs["data-variable-values"] = json.dumps(variables, ensure_ascii=False, sort_keys=True)
    attr_text = "".join(' %s="%s"' % (k, html.escape(v, quote=True)) for k, v in host_attrs.items())
    fps_attr = ' data-fps="%s"' % html.escape(fps, quote=True) if fps else ""
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={w}, height={h}" />
    {assets}
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: {w}px; height: {h}px; overflow: hidden; background: transparent !important; }}
      #{root} {{ position: relative; width: {w}px; height: {h}px; }}
    </style>
  </head>
  <body>
    <div id="{root}" data-composition-id="{root}" data-root="true" data-start="0" data-width="{w}" data-height="{h}"{fps}>
      <{tag}{attrs}></{tag}>
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      window.__timelines["{root}"] = gsap.timeline({{ paused: true }});
    </script>
  </body>
</html>
""".format(w=int(width), h=int(height), assets=head_assets(index), root=WRAPPER_ROOT_ID, fps=fps_attr,
           tag=host.tag if host.tag not in VOID_TAGS else "div", attrs=attr_text)


def document_wrapper(index: Document, *, remove: Iterable[Element] = (), hide: Iterable[Element] = (),
                     clear: Iterable[Element] = (),
                     overrides: Optional[Dict[int, Dict[str, Optional[str]]]] = None) -> str:
    """A copy of ``index.html`` on a transparent background: *hide* (and what is in them) invisible,
    *clear* painting nothing of their own (backgrounds, borders, shadows), *remove* left out, attribute
    *overrides* applied."""
    over: Dict[int, Dict[str, Optional[str]]] = {k: dict(v) for k, v in (overrides or {}).items()}
    for el in hide:
        over.setdefault(id(el), {})[HIDE_ATTR] = ""
    for el in clear:
        over.setdefault(id(el), {})[CLEAR_ATTR] = ""
    return serialize(index.root, skip=remove, overrides=over, rebase=True,
                     head_extra="<style>%s\n%s</style>" % (TRANSPARENT_CSS, HIDE_CSS))


__all__ = [
    "WRAPPER_PREFIX", "WRAPPER_FILE", "WRAPPER_ROOT_ID", "DEFAULT_GSAP", "HIDE_ATTR", "CLEAR_ATTR", "HIDE_CSS",
    "wrapper_folder", "rel_entry",
    "rebase_url", "rebase_css", "rebase_srcset", "serialize", "head_assets", "composition_wrapper",
    "document_wrapper",
]
