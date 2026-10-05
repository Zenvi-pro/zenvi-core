"""Where each Remotion composition lives in the project's code (a static scan; no Node).

"Open Code" on a linked clip opens the component a composition renders, at
the line that defines it. The scan reads the project's source files, finds
``<Composition id="..." component={X}>`` / ``lazyComponent={() => import('./X')}``
and ``<Still ...>`` elements (inside any ``<Folder name="...">`` nesting),
follows ``X`` through the file's imports (named, aliased, default,
namespace, one-hop re-exports and local definitions) and reports
``{file, line}`` of the definition. When it cannot follow (a dynamic id,
an import from a package or a path alias), the ``<Composition`` line itself
is the answer.

Pure text processing with a small JSX-attribute reader: comments are
blanked out first (line numbers kept), strings and ``{...}`` expressions are
skipped by matching quotes and braces. Blocking disk reads -- off the GUI
thread.
"""

from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

SOURCE_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs")
SKIP_DIRS = frozenset({"node_modules", ".git", ".hg", "out", "build", "dist", ".next", ".cache", ".remotion",
                       "public", "coverage", "__pycache__", ".turbo"})
MAX_FILES = 3000
MAX_FILE_BYTES = 1024 * 1024
MAX_REEXPORT_HOPS = 4

_ELEMENT_RE = re.compile(r"<\s*(?:[A-Za-z_$][\w$]*\s*\.\s*)?(Composition|Still|Folder)\b")
_FOLDER_CLOSE_RE = re.compile(r"<\s*/\s*(?:[A-Za-z_$][\w$]*\s*\.\s*)?Folder\s*>")
_IMPORT_RE = re.compile(r"""\bimport\s+(?!\()([\s\S]*?)\s*\bfrom\s*(['"])([^'"]+)\2""")
_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*(['"`])([^'"`]+)\1\s*\)""")
_IDENT = r"[A-Za-z_$][\w$]*"


@dataclass(frozen=True)
class CompositionSource:
    """Where one composition is declared and which code renders it."""

    id: str
    kind: str                    # "composition" | "still"
    root_file: str               # file holding the <Composition> element (relative, posix)
    root_line: int               # 1-based line of "<Composition"
    component: Optional[str]     # the component identifier (None for lazy/unknown)
    file: Optional[str]          # file defining the component (relative, posix)
    line: Optional[int]          # 1-based line of the definition
    folder: Optional[str]        # "Graphics" or "Graphics/Lower thirds"
    lazy: bool = False

    @property
    def best_file(self) -> str:
        return self.file or self.root_file

    @property
    def best_line(self) -> int:
        return int(self.line or self.root_line)

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "file": self.best_file, "line": self.best_line,
                "declared_in": self.root_file, "declared_line": self.root_line, "component": self.component,
                "folder": self.folder}


# ---------------------------------------------------------------------------
# Low-level text helpers
# ---------------------------------------------------------------------------

def _skip_string(text: str, i: int) -> int:
    """Index just past the string literal starting at text[i] (', " or `)."""
    quote = text[i]
    n = len(text)
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if quote == "`" and c == "$" and j + 1 < n and text[j + 1] == "{":
            j = _skip_braces(text, j + 1)
            continue
        if c == quote:
            return j + 1
        if c == "\n" and quote != "`":
            return j  # unterminated ('Don't' in JSX text): stop at the line end
        j += 1
    return n


def _skip_braces(text: str, i: int) -> int:
    """Index just past the balanced {...} starting at text[i] == '{' (strings and nested braces skipped)."""
    depth = 0
    n = len(text)
    j = i
    while j < n:
        c = text[j]
        if c in "'\"`":
            j = _skip_string(text, j)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def strip_comments(text: str) -> str:
    """*text* with // and /* */ comments blanked (newlines and string contents kept)."""
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = _skip_string(text, i)
            out.append(text[i:j])
            i = j
            continue
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i)
                j = n if j < 0 else j
                out.append(" " * (j - i))
                i = j
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                j = n if j < 0 else j + 2
                out.append(re.sub(r"[^\n]", " ", text[i:j]))
                i = j
                continue
        out.append(c)
        i += 1
    return "".join(out)


class _Lines:
    def __init__(self, text: str):
        self._starts = [0] + [m.end() for m in re.finditer(r"\n", text)]

    def line_of(self, index: int) -> int:
        return bisect.bisect_right(self._starts, index)


def _string_value(raw: str) -> Optional[str]:
    """The text of a string literal or a {'literal'} expression; None when dynamic."""
    s = raw.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"`":
        inner = s[1:-1]
        if s[0] == "`" and "${" in inner:
            return None
        return inner
    return None


def parse_attributes(text: str, i: int) -> Tuple[Dict[str, str], int, bool]:
    """JSX attributes of the opening tag whose name ends at *i*: ({name: raw value}, end index, self-closing).

    Raw values keep their quotes / braces; boolean attributes are ``"true"``.
    """
    attrs: Dict[str, str] = {}
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        if text.startswith("/>", i):
            return attrs, i + 2, True
        if text[i] == ">":
            return attrs, i + 1, False
        if text[i] == "{":  # {...spread}
            i = _skip_braces(text, i)
            continue
        m = re.compile(r"[A-Za-z_$][\w$:.-]*").match(text, i)
        if not m:
            i += 1
            continue
        name = m.group(0)
        i = m.end()
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == "=":
            i += 1
            while i < n and text[i].isspace():
                i += 1
            if i < n and text[i] in "'\"":
                j = _skip_string(text, i)
            elif i < n and text[i] == "{":
                j = _skip_braces(text, i)
            else:
                j = i
                while j < n and not text[j].isspace() and text[j] not in "/>":
                    j += 1
            attrs[name] = text[i:j]
            i = j
        else:
            attrs[name] = "true"
    return attrs, n, False


# ---------------------------------------------------------------------------
# Imports and definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Binding:
    module: Optional[str]   # import source ('./TitleCard'), None for a local definition
    name: str               # exported name in the module ('default' for default imports, '*' for a namespace)


def _parse_imports(code: str) -> Dict[str, _Binding]:
    """local name -> binding for every ``import ... from '...'`` in *code* (comments already stripped)."""
    out: Dict[str, _Binding] = {}
    for m in _IMPORT_RE.finditer(code):
        clause, module = m.group(1).strip(), m.group(3)
        if clause.startswith("type ") or clause.startswith("type{"):
            continue
        named = re.search(r"\{([\s\S]*?)\}", clause)
        if named:
            for part in named.group(1).split(","):
                part = part.strip()
                if not part or part.startswith("type "):
                    continue
                pm = re.match(rf"({_IDENT})(?:\s+as\s+({_IDENT}))?$", part)
                if pm:
                    out[pm.group(2) or pm.group(1)] = _Binding(module, pm.group(1))
            clause = clause[:named.start()] + clause[named.end():]
        ns = re.search(rf"\*\s*as\s+({_IDENT})", clause)
        if ns:
            out[ns.group(1)] = _Binding(module, "*")
            clause = clause[:ns.start()] + clause[ns.end():]
        default = re.match(rf"\s*({_IDENT})\s*(?:,|$)", clause)
        if default and default.group(1) != "type":
            out[default.group(1)] = _Binding(module, "default")
    return out


def _definition_index(code: str, name: str) -> Optional[int]:
    """Index of the line defining *name* (const/let/var/function/class, exported or not)."""
    n = re.escape(name)
    patterns = (
        rf"(?m)^[ \t]*export\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*{n}\b",
        rf"(?m)^[ \t]*export\s+(?:const|let|var|class)\s+{n}\b",
        rf"(?m)^[ \t]*(?:async\s+)?function\s*\*?\s*{n}\b",
        rf"(?m)^[ \t]*(?:const|let|var|class)\s+{n}\b",
        rf"(?m)\b(?:const|let|var|class|function)\s+{n}\b",
    )
    for pattern in patterns:
        m = re.search(pattern, code)
        if m:
            return m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
    return None


def _default_export(code: str) -> Tuple[Optional[int], Optional[str]]:
    """(index of the default export's definition, or the local name it re-exports)."""
    m = re.search(r"(?m)^[ \t]*export\s+default\s+(?:async\s+)?(?:function|class)\b", code)
    if m:
        return m.start() + (len(m.group(0)) - len(m.group(0).lstrip())), None
    m = re.search(rf"\bexport\s+default\s+({_IDENT})\s*;?\s*(?:$|\n)", code)
    if m:
        return None, m.group(1)
    m = re.search(rf"\bexport\s*\{{[^}}]*?\b({_IDENT})\s+as\s+default\b", code)
    if m:
        return None, m.group(1)
    m = re.search(r"\bexport\s+default\b", code)
    if m:
        return m.start(), None
    return None, None


def _reexport(code: str, name: str) -> Optional[Tuple[Optional[str], str]]:
    """``export {A as name} from './x'`` / ``export {name} from './x'`` / ``export {A as name}`` / ``export * from``."""
    for m in re.finditer(r"""\bexport\s*\{([^}]*)\}\s*(?:from\s*(['"])([^'"]+)\2)?""", code):
        for part in m.group(1).split(","):
            pm = re.match(rf"\s*(?:type\s+)?({_IDENT})(?:\s+as\s+({_IDENT}))?\s*$", part)
            if pm and (pm.group(2) or pm.group(1)) == name:
                return m.group(3), pm.group(1)
    return None


def _star_reexports(code: str) -> List[str]:
    return [m.group(2) for m in re.finditer(r"""\bexport\s*\*\s*from\s*(['"])([^'"]+)\1""", code)]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

class _Files:
    """Cached, comment-stripped source files under one project root."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._code: Dict[str, Optional[str]] = {}

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def code(self, path: str) -> Optional[str]:
        if path not in self._code:
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    self._code[path] = None
                else:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        self._code[path] = strip_comments(fh.read())
            except OSError:
                self._code[path] = None
        return self._code[path]

    def resolve_module(self, from_file: str, module: str) -> Optional[str]:
        """Absolute file a relative import refers to (None for packages and aliases)."""
        if not module.startswith("."):
            return None
        base = os.path.normpath(os.path.join(os.path.dirname(from_file), module))
        candidates = [base] if os.path.splitext(base)[1] in SOURCE_SUFFIXES else []
        candidates += [base + s for s in SOURCE_SUFFIXES]
        candidates += [os.path.join(base, "index" + s) for s in SOURCE_SUFFIXES]
        stem, ext = os.path.splitext(base)
        if ext in (".js", ".jsx", ".mjs"):  # TS projects import './X.js' for X.ts
            candidates += [stem + s for s in (".ts", ".tsx")]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    def walk(self) -> Iterator[str]:
        count = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if name.endswith(SOURCE_SUFFIXES) and not name.endswith(".d.ts"):
                    yield os.path.join(dirpath, name)
                    count += 1
                    if count >= MAX_FILES:
                        return


def _find_export(files: _Files, path: str, name: str, hops: int = 0) -> Optional[Tuple[str, int]]:
    """(file, index) defining export *name* of module file *path* (following re-exports)."""
    code = files.code(path)
    if code is None or hops > MAX_REEXPORT_HOPS:
        return None
    if name == "default":
        index, local = _default_export(code)
        if index is not None:
            return path, index
        if local:
            return _find_local(files, path, local, hops + 1)
        return None
    m = re.search(rf"(?m)^[ \t]*export\s+(?:declare\s+)?(?:async\s+)?(?:const|let|var|function\s*\*?|class)\s+"
                  rf"{re.escape(name)}\b", code)
    if m:
        return path, m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
    re_exported = _reexport(code, name)
    if re_exported is not None:
        module, original = re_exported
        if module is None:
            return _find_local(files, path, original, hops + 1)
        target = files.resolve_module(path, module)
        return _find_export(files, target, original, hops + 1) if target else None
    for module in _star_reexports(code):
        target = files.resolve_module(path, module)
        found = _find_export(files, target, name, hops + 1) if target else None
        if found:
            return found
    return None


def _find_local(files: _Files, path: str, name: str, hops: int = 0) -> Optional[Tuple[str, int]]:
    """(file, index) of identifier *name* as seen from *path*: a local definition or an import."""
    code = files.code(path)
    if code is None or hops > MAX_REEXPORT_HOPS:
        return None
    binding = _parse_imports(code).get(name)
    if binding is not None and binding.module is not None:
        target = files.resolve_module(path, binding.module)
        if target is None or binding.name == "*":
            return None
        return _find_export(files, target, binding.name, hops + 1)
    index = _definition_index(code, name)
    return (path, index) if index is not None else None


def _resolve_component(files: _Files, path: str, expr: str) -> Tuple[Optional[str], Optional[Tuple[str, int]]]:
    """(component name, (file, index)) for a ``component={...}`` expression."""
    s = expr.strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    m = re.fullmatch(rf"({_IDENT})(?:\s*\.\s*({_IDENT}))?(?:\s+as\s+[\s\S]+)?", s)
    if not m:
        return None, None
    head, member = m.group(1), m.group(2)
    code = files.code(path) or ""
    if member:
        binding = _parse_imports(code).get(head)
        if binding is not None and binding.name == "*" and binding.module:
            target = files.resolve_module(path, binding.module)
            return member, (_find_export(files, target, member) if target else None)
        return member, None
    return head, _find_local(files, path, head)


def _resolve_lazy(files: _Files, path: str, expr: str) -> Optional[Tuple[str, int]]:
    m = _DYNAMIC_IMPORT_RE.search(expr)
    if not m:
        return None
    target = files.resolve_module(path, m.group(2))
    if target is None:
        return None
    member = re.search(rf"\.then\s*\(\s*\(?\s*{_IDENT}\s*\)?\s*=>\s*\(?\s*\{{\s*default\s*:\s*{_IDENT}\s*\.\s*({_IDENT})",
                       expr)
    found = _find_export(files, target, member.group(1) if member else "default")
    return found or (target, 0)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------

def scan_file(files: _Files, path: str) -> List[CompositionSource]:
    """Compositions and stills declared in one file."""
    code = files.code(path)
    if not code or ("Composition" not in code and "Still" not in code):
        return []
    lines = _Lines(code)
    events: List[Tuple[int, str, object]] = []
    for m in _ELEMENT_RE.finditer(code):
        events.append((m.start(), "open", m))
    for m in _FOLDER_CLOSE_RE.finditer(code):
        events.append((m.start(), "close", m))
    events.sort(key=lambda e: e[0])
    folders: List[str] = []
    out: List[CompositionSource] = []
    rel = files.rel(path)
    for _pos, kind, m in events:
        assert isinstance(m, re.Match)
        if kind == "close":
            if folders:
                folders.pop()
            continue
        tag = m.group(1)
        attrs, _end, self_closing = parse_attributes(code, m.end())
        if tag == "Folder":
            if not self_closing:
                folders.append(_string_value(attrs.get("name", "")) or "?")
            continue
        comp_id = _string_value(attrs.get("id", ""))
        if not comp_id:
            continue
        component, found, lazy = None, None, False
        if "component" in attrs:
            component, found = _resolve_component(files, path, attrs["component"])
        elif "lazyComponent" in attrs:
            lazy = True
            found = _resolve_lazy(files, path, attrs["lazyComponent"])
        file = line = None
        if found:
            fpath, index = found
            file = files.rel(fpath)
            line = _Lines(files.code(fpath) or "").line_of(index)
        out.append(CompositionSource(id=comp_id, kind="still" if tag == "Still" else "composition", root_file=rel,
                                     root_line=lines.line_of(m.start()), component=component, file=file, line=line,
                                     folder="/".join(folders) or None, lazy=lazy))
    return out


def scan_project(root: str, entry: Optional[str] = None) -> Dict[str, CompositionSource]:
    """Composition id -> where it is declared and defined, for every source file of the project.

    Files reachable as the entry's siblings come first (``src/`` for
    ``src/index.ts``); the first declaration of an id wins.
    """
    files = _Files(root)
    ordered: List[str] = []
    if entry:
        entry_dir = os.path.dirname(os.path.join(files.root, *entry.split("/")))
        if os.path.isdir(entry_dir) and entry_dir != files.root:
            ordered += [p for p in _Files(entry_dir).walk()]
    seen = set(ordered)
    ordered += [p for p in files.walk() if p not in seen]
    found: Dict[str, CompositionSource] = {}
    for path in ordered:
        for source in scan_file(files, path):
            found.setdefault(source.id, source)
    return found


def source_for(root: str, composition: str, entry: Optional[str] = None) -> Optional[CompositionSource]:
    return scan_project(root, entry).get(composition)


__all__ = ["CompositionSource", "scan_project", "scan_file", "source_for", "strip_comments", "parse_attributes"]
