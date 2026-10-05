"""Linked clips: rendered media that keeps a live link to the code or comp it came from.

A linked clip is an ordinary project file whose ``path`` is a rendered movie
(libopenshot reads it like any other file) plus a ``zenvi_link`` object on
the file record (SPEC §3.6) naming its source -- a Remotion or HyperFrames
project, or an After Effects comp -- the props it was rendered with and a
fingerprint of what it was rendered from. Every clip of that file shares it.

* Providers (:class:`LinkProvider`) know how to fingerprint, render and open
  one kind of source. ``register_provider`` makes a kind live; the
  Remotion / HyperFrames / After Effects packages register theirs.
* The project operations (:func:`add_linked_media`, :func:`swap_linked_media`,
  :func:`update_link`, :func:`unlink`, :func:`rerender_linked`) block: call
  them OFF the GUI thread. They probe and render on the calling thread and
  hop to the GUI thread only for the File/Clip change, which is exactly one
  undo step; anything refused or failed leaves history untouched.
* Rendered media lives in ``<project>_assets/links/<kind>/`` (an unsaved
  project uses ``~/.openshot_qt/links/<kind>/``; ``project_data.save``
  adopts those into the assets folder via :func:`adopt_linked_renders`).
  Renders are staged in a hidden folder next to the target and moved into
  place only when complete.

Alpha (SPEC §3.6): libopenshot 1.0 drops alpha from VP9 WebM but keeps it for
ProRes 4444 and qtrle, so linked media is never WebM -- see ``handoff.alpha``.

Storage gotcha: on save/load the project file rewrites every JSON string
under a key named ``path``, ``image``, ``resource``, ``protobuf_data_path``
or ``lut_path``, at any depth. ``zenvi_link`` therefore never uses those
names (``output``, ``project_dir``, ``file``, ``aep`` instead), and props
that contain one are stored JSON-encoded (``{"$zenvi_json": "..."}``) --
always read them through :func:`read_link` / :func:`link_props`.
"""

from __future__ import annotations

import copy
import datetime
import errno
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple, runtime_checkable

from classes.assets import path_is_under
from classes.logger import log

LINK_KEY = "zenvi_link"
LINK_VERSION = 1
KINDS = ("remotion", "hyperframes", "aftereffects")
KIND_LABELS = {"remotion": "Remotion", "hyperframes": "HyperFrames", "aftereffects": "After Effects"}
STATES = ("fresh", "stale", "rendering", "error", "missing_source")
CODECS = ("prores4444", "h264", "qtrle")
CODEC_EXTENSIONS = {"prores4444": ".mov", "qtrle": ".mov", "h264": ".mp4"}
# Keys the project file rewrites as paths on save/load (json_data.path_regex).
PATH_LIKE_KEYS = frozenset({"path", "image", "resource", "protobuf_data_path", "lut_path"})
PROPS_ESCAPE_KEY = "$zenvi_json"
SOURCE_KEYS = ("project_dir", "entry", "composition", "composition_key", "file", "line", "aep")
RENDER_KEYS = ("codec", "width", "height", "fps", "duration_frames", "output", "rendered_at", "fingerprint")
LINKS_FOLDER = "links"
STAGING_PREFIX = ".render-"
LINKED_TRACK_LABEL = "Linked"
FINGERPRINT_SMALL_FILE = 2 * 1024 * 1024
FINGERPRINT_MAX_FILES = 20000
DEFAULT_EXCLUDED_DIRS = frozenset({"node_modules", ".git", ".hg", ".svn", "out", "dist", "build", ".next",
                                   ".cache", "__pycache__", ".turbo", ".remotion", "renders"})


class LinkError(ValueError):
    """Bad link metadata, or a linked-clip operation that cannot run; the message says what to do."""


class SourceMissing(LinkError):
    """The link's source (project folder, entry file, .aep) is gone."""


# ---------------------------------------------------------------------------
# The zenvi_link schema
# ---------------------------------------------------------------------------

def _has_path_like_key(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in PATH_LIKE_KEYS:
                return str(k)
            found = _has_path_like_key(v)
            if found:
                return found
    elif isinstance(value, list):
        for v in value:
            found = _has_path_like_key(v)
            if found:
                return found
    return None


def encode_props(props: Any) -> Any:
    """Props as stored: unchanged, or JSON-encoded when a key would be rewritten as a path on save."""
    if isinstance(props, dict) and PROPS_ESCAPE_KEY in props and len(props) == 1:
        return copy.deepcopy(props)
    if _has_path_like_key(props):
        return {PROPS_ESCAPE_KEY: json.dumps(props, ensure_ascii=False, sort_keys=True)}
    return copy.deepcopy(props)


def decode_props(stored: Any) -> dict:
    """Props as the source sees them (undoes :func:`encode_props`)."""
    if isinstance(stored, dict) and set(stored) == {PROPS_ESCAPE_KEY}:
        try:
            value = json.loads(stored[PROPS_ESCAPE_KEY])
        except (TypeError, ValueError):
            raise LinkError("the linked clip's stored props are not valid JSON") from None
        return value if isinstance(value, dict) else {}
    return copy.deepcopy(stored) if isinstance(stored, dict) else {}


def _fps_dict(value: Any) -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, Fraction):
        return {"num": value.numerator, "den": value.denominator}
    if isinstance(value, dict):
        try:
            num, den = int(value.get("num") or 0), int(value.get("den") or 1)
        except (TypeError, ValueError):
            raise LinkError(f"render.fps must be {{num, den}}, got {value!r}") from None
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        frac = Fraction(value).limit_denominator(1001)
        num, den = frac.numerator, frac.denominator
    elif isinstance(value, (tuple, list)) and len(value) == 2:
        num, den = int(value[0]), int(value[1] or 1)
    else:
        raise LinkError(f"render.fps must be {{num, den}} or a number, got {value!r}")
    if num <= 0 or den <= 0:
        raise LinkError(f"render.fps must be positive, got {value!r}")
    return {"num": num, "den": den}


def _opt_int(value: Any, what: str) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise LinkError(f"{what} must be a whole number, got {value!r}")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise LinkError(f"{what} must be a whole number, got {value!r}") from None
    if f != int(f):
        raise LinkError(f"{what} must be a whole number, got {value!r}")
    return int(f)


def _opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def normalize_link(link: Any) -> dict:
    """``link`` checked and completed into the stored ``zenvi_link`` shape (a new dict).

    Fills defaults (version 1, empty source/props/render, state ``fresh``),
    keeps unknown keys (other tools' data must survive), makes
    ``source.project_dir`` / ``source.aep`` absolute, encodes props that need
    it, and raises LinkError for anything an exporter or the save path would
    mangle.
    """
    if not isinstance(link, dict):
        raise LinkError("a link must be an object with at least a 'kind'")
    out = copy.deepcopy(link)
    kind = str(out.get("kind") or "").strip().lower()
    if kind not in KINDS and kind not in _PROVIDERS:
        raise LinkError(f"link kind must be one of {', '.join(sorted(set(KINDS) | set(_PROVIDERS)))}, "
                        f"got {link.get('kind')!r}")
    out["kind"] = kind
    out["version"] = LINK_VERSION if out.get("version") in (None, "") else _opt_int(out.get("version"), "version")
    if out["version"] != LINK_VERSION:
        raise LinkError(f"this Zenvi reads link version {LINK_VERSION}, got {out['version']}")

    source = out.get("source") or {}
    if not isinstance(source, dict):
        raise LinkError("link.source must be an object")
    source = dict(source)
    for key in SOURCE_KEYS:
        source.setdefault(key, None)
    for key in ("project_dir", "aep"):
        if source.get(key):
            source[key] = os.path.normpath(os.path.abspath(os.path.expanduser(str(source[key]))))
        else:
            source[key] = None
    for key in ("entry", "composition", "file"):
        source[key] = _opt_str(source.get(key))
    if source.get("file") and source.get("project_dir") and os.path.isabs(str(source["file"])):
        rel = os.path.relpath(str(source["file"]), source["project_dir"])
        if not rel.startswith(".."):
            source["file"] = rel.replace(os.sep, "/")
    source["line"] = _opt_int(source.get("line"), "source.line")
    if source["line"] is not None and source["line"] < 1:
        raise LinkError("source.line is 1-based")
    key = source.get("composition_key")
    if key is not None and not isinstance(key, (int, str)):
        raise LinkError("source.composition_key must be a number or string")
    out["source"] = source

    props = out.get("props")
    if props is None:
        props = {}
    if not isinstance(props, dict):
        raise LinkError("link.props must be an object")
    out["props"] = encode_props(decode_props(props) if set(props) == {PROPS_ESCAPE_KEY} else props)

    render = out.get("render") or {}
    if not isinstance(render, dict):
        raise LinkError("link.render must be an object")
    render = dict(render)
    for k in RENDER_KEYS:
        render.setdefault(k, None)
    codec = render.get("codec")
    if codec is not None:
        codec = str(codec).strip().lower()
        if codec not in CODECS:
            raise LinkError(f"render.codec must be one of {', '.join(CODECS)} (linked media is never WebM: "
                            f"libopenshot drops its alpha), got {render.get('codec')!r}")
    render["codec"] = codec
    render["width"] = _opt_int(render.get("width"), "render.width")
    render["height"] = _opt_int(render.get("height"), "render.height")
    render["fps"] = _fps_dict(render.get("fps"))
    render["duration_frames"] = _opt_int(render.get("duration_frames"), "render.duration_frames")
    for k in ("output", "rendered_at", "fingerprint"):
        render[k] = _opt_str(render.get(k))
    out["render"] = render

    state = str(out.get("state") or "fresh").strip().lower()
    if state not in STATES:
        raise LinkError(f"link.state must be one of {', '.join(STATES)}, got {out.get('state')!r}")
    out["state"] = state
    out["error"] = _opt_str(out.get("error"))

    bad = _has_path_like_key({k: v for k, v in out.items() if k != "props"})
    if bad:
        raise LinkError(f"zenvi_link must not use the key {bad!r}: the project file rewrites it as a media path "
                        "on save. Use 'output', 'project_dir', 'file' or 'aep'")
    return out


def _file_data(file_like: Any) -> dict:
    data = getattr(file_like, "data", file_like)
    if isinstance(data, dict):
        return data
    try:
        return dict(data)  # MappingProxyType (FileView.data)
    except (TypeError, ValueError):
        return {}


def read_link(file_like: Any) -> Optional[dict]:
    """The decoded link of a file (dict, ``classes.query.File`` or ``FileView``), or None.

    Props come back decoded; the result is a copy.
    """
    data = _file_data(file_like)
    raw = data.get(LINK_KEY)
    if not isinstance(raw, dict):
        raw = getattr(file_like, "zenvi_link", None)
    if not isinstance(raw, dict) and raw is not None:
        try:
            raw = dict(raw)  # FileView.zenvi_link (mapping proxy)
        except (TypeError, ValueError):
            raw = None
    if not isinstance(raw, dict):
        return None
    link = copy.deepcopy(raw)
    link["props"] = decode_props(link.get("props"))
    return link


def link_props(link: dict) -> dict:
    return decode_props((link or {}).get("props"))


def is_linked(file_like: Any) -> bool:
    return isinstance(_file_data(file_like).get(LINK_KEY), dict)


def kind_label(kind: str) -> str:
    provider = _PROVIDERS.get(kind)
    return getattr(provider, "label", None) or KIND_LABELS.get(kind, kind.title())


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@dataclass
class RenderResult:
    """What a provider rendered into the ``out_dir`` it was given."""

    path: str
    codec: str
    width: int
    height: int
    fps: Any                      # Fraction, number, (num, den) or {"num", "den"}
    duration_frames: int
    warnings: List[str] = field(default_factory=list)
    source_updates: Dict[str, Any] = field(default_factory=dict)   # e.g. {"composition_key": 12}
    props: Optional[dict] = None  # the props actually used (defaults filled), when they differ


ProgressFn = Callable[[Optional[float], str], None]
CancelFn = Callable[[], bool]


@runtime_checkable
class LinkProvider(Protocol):
    """One kind of linked source (Remotion, HyperFrames, After Effects).

    ``fingerprint`` and ``render`` block and run off the GUI thread.
    ``fingerprint`` hashes everything a render depends on (watched source
    files, props, render settings) -- ``"sha256:<hex>"``; it raises
    SourceMissing when the source is gone. ``render`` writes ONE finished
    media file inside *out_dir* (a staging folder Zenvi owns and cleans up),
    calls ``on_progress(fraction or None, message)``, polls
    ``should_cancel()`` and returns a RenderResult; it raises LinkError (or
    ``jobs.JobCancelled``) with a message for the user. ``open_source`` /
    ``open_studio`` may block briefly (they start other programs); call them
    off the GUI thread. ``editable_props`` returns the props a user may edit
    with their current values.
    """

    kind: str
    label: str
    supports_studio: bool

    def fingerprint(self, link: dict) -> str: ...

    def render(self, link: dict, out_dir: str, *, on_progress: ProgressFn,
               should_cancel: CancelFn) -> RenderResult: ...

    def open_source(self, link: dict) -> None: ...

    def open_studio(self, link: dict) -> None: ...

    def editable_props(self, link: dict) -> dict: ...


_PROVIDERS: Dict[str, Any] = {}
_providers_lock = threading.Lock()


def register_provider(provider: Any) -> None:
    """Make *provider* handle links of ``provider.kind`` (replaces an earlier one)."""
    kind = str(getattr(provider, "kind", "") or "").strip().lower()
    if not kind:
        raise ValueError("a link provider needs a kind")
    for attr in ("fingerprint", "render", "open_source", "editable_props"):
        if not callable(getattr(provider, attr, None)):
            raise ValueError(f"link provider {kind!r} lacks {attr}()")
    with _providers_lock:
        _PROVIDERS[kind] = provider


def unregister_provider(kind: str) -> None:
    with _providers_lock:
        _PROVIDERS.pop(str(kind).strip().lower(), None)


def provider_for(kind: str) -> Optional[Any]:
    with _providers_lock:
        return _PROVIDERS.get(str(kind or "").strip().lower())


def registered_kinds() -> List[str]:
    with _providers_lock:
        return sorted(_PROVIDERS)


def supports_studio(kind: str) -> bool:
    provider = provider_for(kind)
    return bool(provider is not None and getattr(provider, "supports_studio", False)
                and callable(getattr(provider, "open_studio", None)))


def require_provider(kind: str) -> Any:
    provider = provider_for(kind)
    if provider is None:
        raise LinkError(f"this Zenvi build cannot render {kind_label(kind)} links (no {kind} provider is "
                        "installed); the clip keeps playing its last render")
    return provider


# ---------------------------------------------------------------------------
# Fingerprints (helpers for providers)
# ---------------------------------------------------------------------------

def fingerprint_value(value: Any) -> str:
    """sha256 of *value* as canonical JSON (props, render settings)."""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprint_sources(root: str, *, include: Optional[Iterable[str]] = None,
                        exclude_dirs: Iterable[str] = DEFAULT_EXCLUDED_DIRS, extra: Any = None,
                        max_files: int = FINGERPRINT_MAX_FILES) -> str:
    """``"sha256:<hex>"`` of the files under *root* plus *extra* (props, settings).

    Small files (< 2 MB) are hashed by content, so re-saving a file unchanged
    does not make a clip stale; bigger ones (media) by size and mtime.
    *include* limits it to these file suffixes (``(".ts", ".tsx", ".json")``).
    Blocking disk walk: call off the GUI thread. SourceMissing when *root*
    is not a folder.
    """
    if not root or not os.path.isdir(root):
        raise SourceMissing(f"the source folder {root!r} no longer exists; relink it or unlink the clip")
    suffixes = tuple(s.lower() for s in include) if include else None
    excluded = set(exclude_dirs)
    digest = hashlib.sha256()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded and not d.startswith(".render-"))
        for name in sorted(filenames):
            if suffixes and not name.lower().endswith(suffixes):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            digest.update(rel.encode("utf-8") + b"\0")
            if st.st_size <= FINGERPRINT_SMALL_FILE:
                try:
                    with open(full, "rb") as fh:
                        digest.update(hashlib.sha256(fh.read()).digest())
                except OSError:
                    digest.update(b"unreadable")
            else:
                digest.update(("%d:%d" % (st.st_size, st.st_mtime_ns)).encode("ascii"))
            count += 1
            if count >= max_files:
                log.warning("fingerprint of %s stopped at %d files", root, max_files)
                break
        if count >= max_files:
            break
    digest.update(fingerprint_value(extra).encode("ascii"))
    return "sha256:" + digest.hexdigest()


def short_fingerprint(fingerprint: Optional[str]) -> str:
    hexpart = str(fingerprint or "").split(":", 1)[-1]
    return re.sub(r"[^0-9a-fA-F]", "", hexpart)[:8] or "render"


# ---------------------------------------------------------------------------
# Where renders live
# ---------------------------------------------------------------------------

_CURRENT = object()


def _current_project_path() -> Optional[str]:
    try:
        from classes.app import get_app
        app = get_app()
        path = getattr(getattr(app, "project", None), "current_filepath", None)
        return path if isinstance(path, str) and path else None
    except Exception:
        return None


def links_root(project_path: Any = _CURRENT) -> str:
    """``<project>_assets/links`` (``~/.openshot_qt/links`` while the project is unsaved)."""
    from classes import info
    from classes.assets import get_assets_path
    path = _current_project_path() if project_path is _CURRENT else project_path
    assets = get_assets_path(path, create_paths=False) if path else info.USER_PATH
    return os.path.join(assets or info.USER_PATH, LINKS_FOLDER)


def links_dir(kind: str, project_path: Any = _CURRENT) -> str:
    """Folder for *kind*'s renders (not created)."""
    return os.path.join(links_root(project_path), str(kind))


def output_token(abs_path: str, project_path: Any = _CURRENT) -> str:
    """``@assets/links/<kind>/<file>`` for a render inside the links folder, else the absolute path."""
    from classes import info
    from classes.assets import get_assets_path
    path = _current_project_path() if project_path is _CURRENT else project_path
    assets = (get_assets_path(path, create_paths=False) if path else info.USER_PATH) or info.USER_PATH
    abs_path = os.path.abspath(abs_path)
    try:
        rel = os.path.relpath(abs_path, assets)
    except ValueError:  # another drive
        return abs_path
    if rel.startswith("..") or os.path.isabs(rel):
        return abs_path
    return "@assets/" + rel.replace(os.sep, "/")


def _safe_name(text: str, fallback: str = "render") -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-._")
    return (base or fallback)[:60]


def render_file_name(link: dict, fingerprint: Optional[str], ext: str) -> str:
    """``<composition>-<8 hex of the fingerprint><ext>`` (SPEC §3.6: ``Intro-3f9a1c2b.mov``)."""
    source = link.get("source") or {}
    base = _safe_name(source.get("composition") or link.get("kind") or "render")
    ext = ext if ext.startswith(".") else "." + ext
    return f"{base}-{short_fingerprint(fingerprint)}{ext.lower()}"


def _unique_path(folder: str, name: str) -> str:
    candidate = os.path.join(folder, name)
    stem, ext = os.path.splitext(name)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{stem}-{n}{ext}")
        n += 1
    return candidate


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_RENDER_ERRORS: Dict[str, str] = {}
_errors_lock = threading.Lock()


def set_render_error(file_id: str, message: str) -> None:
    """Remember that the last render of *file_id* failed (session only; never an undo step)."""
    with _errors_lock:
        _RENDER_ERRORS[str(file_id)] = str(message)


def clear_render_error(file_id: str) -> None:
    with _errors_lock:
        _RENDER_ERRORS.pop(str(file_id), None)


def render_error(file_id: str) -> Optional[str]:
    with _errors_lock:
        return _RENDER_ERRORS.get(str(file_id))


@dataclass(frozen=True)
class LinkCheck:
    """A linked file's freshness and why."""

    file_id: str
    kind: str
    state: str
    detail: str = ""
    fingerprint: Optional[str] = None          # current, when it could be computed
    stored_fingerprint: Optional[str] = None   # what the media was rendered from

    def as_dict(self) -> dict:
        return {"file_id": self.file_id, "kind": self.kind, "state": self.state, "detail": self.detail,
                "fingerprint": self.fingerprint, "rendered_fingerprint": self.stored_fingerprint}


def _source_gone(link: dict) -> Optional[str]:
    source = link.get("source") or {}
    project_dir = source.get("project_dir")
    if project_dir and not os.path.isdir(project_dir):
        return f"the source folder {project_dir} no longer exists"
    aep = source.get("aep")
    if aep and not os.path.isfile(aep):
        return f"the After Effects project {aep} no longer exists"
    return None


def check_link(file_like: Any, *, compute: bool = True) -> Optional[LinkCheck]:
    """Freshness of a linked file; None when it is not linked.

    Precedence: ``rendering`` (a job is re-rendering it) > ``error`` (its last
    render failed this session) > ``missing_source`` > ``stale`` (the
    provider's fingerprint differs from the one it was rendered from, or the
    rendered media is gone) > the stored state (``fresh``). With *compute*
    the source, the media and the provider's fingerprint are checked on disk:
    blocking, call off the GUI thread. ``compute=False`` answers from memory
    only (running job, session error, stored state) and is safe anywhere.
    """
    data = _file_data(file_like)
    link = read_link(data)
    if link is None:
        return None
    file_id = str(data.get("id") or getattr(file_like, "id", "") or "")
    kind = str(link.get("kind") or "")
    stored = (link.get("render") or {}).get("fingerprint")
    from classes.handoff import jobs
    running = jobs.job_for(file_id) if file_id else None
    if running is not None:
        return LinkCheck(file_id, kind, "rendering", running.message or running.label, None, stored)
    err = render_error(file_id)
    if err:
        return LinkCheck(file_id, kind, "error", err, None, stored)
    provider = provider_for(kind)
    if not compute:  # in memory only: no stat of the source, the media or the provider
        stored_state = link.get("state")
        state = stored_state if isinstance(stored_state, str) and stored_state in STATES else "fresh"
        return LinkCheck(file_id, kind, "stale" if state == "rendering" else state, "", None, stored)
    gone = _source_gone(link)
    if gone:
        return LinkCheck(file_id, kind, "missing_source", gone, None, stored)
    current = None
    if compute and provider is not None:
        try:
            current = provider.fingerprint(link)
        except SourceMissing as exc:
            return LinkCheck(file_id, kind, "missing_source", str(exc), None, stored)
        except Exception as exc:
            log.warning("fingerprint of linked file %s failed", file_id, exc_info=True)
            return LinkCheck(file_id, kind, "error", f"could not check the source: {exc}", None, stored)
    media = str(data.get("path") or "")
    if media and "%" not in media and not os.path.exists(media):
        return LinkCheck(file_id, kind, "stale", "the rendered media is missing; re-render it", current, stored)
    if current and stored and current != stored:
        return LinkCheck(file_id, kind, "stale", "the source changed since the last render", current, stored)
    stored_state = link.get("state")
    state = stored_state if isinstance(stored_state, str) and stored_state in STATES else "fresh"
    if state == "rendering":  # a render that died with the app
        state = "stale"
    detail = str(link.get("error") or "") if state == "error" else ""
    if provider is None and state == "fresh":
        detail = f"no {kind} provider in this build: freshness is not checked"
    return LinkCheck(file_id, kind, state, detail, current, stored)


def link_state(file_like: Any, *, compute: bool = True) -> Optional[str]:
    """``fresh`` / ``stale`` / ``missing_source`` / ``rendering`` / ``error`` (None if not linked)."""
    check = check_link(file_like, compute=compute)
    return check.state if check else None


# ---------------------------------------------------------------------------
# Media probing (off the GUI thread)
# ---------------------------------------------------------------------------

def probe_media(path: str) -> dict:
    """libopenshot's reader JSON for *path* plus ``media_type`` and a content ``fingerprint``.

    Video and audio go through FFmpeg and are probed on the calling thread;
    stills and SVGs paint with Qt, so they are probed on the GUI thread.
    """
    from classes.image_types import get_media_type
    from classes.media_fingerprint import fingerprint
    from classes.timeline_ops import paints_with_qt

    def _inspect():
        from windows.models.files_model import inspect_media
        reader, _duration = inspect_media(path)
        return reader

    try:
        if paints_with_qt(path):
            from classes.qt_main_thread import call_on_gui
            reader = call_on_gui(_inspect, timeout=60)
        else:
            reader = _inspect()
    except Exception as exc:
        raise LinkError(f"libopenshot cannot read {os.path.basename(path)}: {exc}") from exc
    if not isinstance(reader, dict):
        raise LinkError(f"libopenshot cannot read {os.path.basename(path)} as media")
    reader = dict(reader)
    reader["path"] = path
    reader["media_type"] = get_media_type(reader)
    fp = fingerprint(path)
    if fp:
        reader["fingerprint"] = fp
    return reader


def _media_duration(reader: dict) -> float:
    try:
        return float(reader.get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _check_media_path(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(str(path or "")))
    if not os.path.isfile(path):
        raise LinkError(f"no rendered media at {path}")
    if path.lower().endswith(".webm"):
        raise LinkError("linked media cannot be WebM: libopenshot 1.0 drops its alpha. Re-encode it to ProRes "
                        "4444 (handoff.alpha.reencode_to_prores4444) and link the .mov")
    return path


# ---------------------------------------------------------------------------
# GUI-thread hops (the editor's own helpers: they carry the caller's undo transaction)
# ---------------------------------------------------------------------------

def _commit_on_gui(func: Callable[..., Any], *args: Any) -> Any:
    from classes.editor_tools.titles_text_common import commit_on_main
    return commit_on_main(func, *args)


def _read_on_gui(func: Callable[..., Any], *args: Any) -> Any:
    from classes.editor_tools.titles_text_common import precheck_on_main
    return precheck_on_main(func, *args)


def _app():
    from classes.app import get_app
    return get_app()


def _project_fps() -> Fraction:
    fps = _app().project.get("fps") or {"num": 30, "den": 1}
    try:
        return Fraction(int(fps.get("num") or 30), int(fps.get("den") or 1))
    except (TypeError, ValueError, ZeroDivisionError):
        return Fraction(30, 1)


def _snap(seconds: float, fps: Fraction) -> float:
    from classes import frame_time
    return frame_time.snap(max(0.0, float(seconds)), fps)


def _query(name: str) -> Any:
    """``classes.query.File`` / ``Clip`` (untyped; looked up per call so tests can stand in)."""
    from classes import query
    return getattr(query, name)


def _get_file(file_id: str):
    f = _query("File").get(id=str(file_id or "").strip())
    if not f:
        raise LinkError(f"no project file with id={file_id!r}")
    return f


def _seed_absent_keys(obj: Any, defaults: Dict[str, Any]) -> None:
    """Write absent keys with a neutral value outside undo history (``media_files.seed_missing_keys``).

    Project updates merge, so an undo cannot remove a key an edit added; a key
    that already exists (as None) is restored to None instead. GUI thread.
    """
    missing = {k: v for k, v in defaults.items() if k not in obj.data}
    if not missing or not obj.key:
        return
    updates = _app().updates
    previous = updates.ignore_history
    updates.ignore_history = True
    try:
        updates.update(list(obj.key), copy.deepcopy(missing))
    finally:
        updates.ignore_history = previous
    obj.data.update(copy.deepcopy(missing))


def _linked_file(file_id: str):
    f = _get_file(file_id)
    if not isinstance(f.data.get(LINK_KEY), dict):
        name = f.data.get("name") or os.path.basename(str(f.data.get("path") or ""))
        raise LinkError(f"{name!r} is not a linked clip (it has no source link)")
    return f


def file_id_for_clip(clip_id: str) -> str:
    """The file id behind a timeline clip (reads project data; any thread)."""
    c = _query("Clip").get(id=str(clip_id or "").strip())
    if not c:
        raise LinkError(f"no timeline clip with id={clip_id!r}")
    file_id = str(c.data.get("file_id") or "")
    if not file_id:
        raise LinkError(f"clip {clip_id} has no project file")
    return file_id


def _clip_rows(file_id: str) -> List[dict]:
    rows = []
    for c in _query("Clip").filter(file_id=file_id):
        d = c.data
        position, start, end = float(d.get("position") or 0), float(d.get("start") or 0), float(d.get("end") or 0)
        rows.append({"timeline_clip_id": c.id, "layer": int(d.get("layer") or 0), "position": round(position, 3),
                     "end": round(position + max(0.0, end - start), 3)})
    return rows


# ---------------------------------------------------------------------------
# Project operations
# ---------------------------------------------------------------------------

def add_linked_media(path: str, link: dict, *, position: Optional[float] = None, track: Optional[str] = None,
                     name: str = "") -> dict:
    """Add rendered *path* with *link* to Project Files and place it, as ONE undo step.

    *position* (timeline seconds, default the playhead) is snapped to the
    frame grid; without *track* the clip goes on the lowest free track above
    the video in its window (a new "Linked" track on top when none is free,
    like titles). Probes the media on the calling thread: call off the GUI
    thread. Returns receipt data: file_id, timeline_clip_id, position,
    duration, end, layer, new_track, path, kind, state.
    """
    path = _check_media_path(path)
    stored = normalize_link(link)
    reader = probe_media(path)
    duration = _media_duration(reader)
    if duration <= 0:
        raise LinkError(f"{os.path.basename(path)} has no duration; is the render complete?")
    source = stored.get("source") or {}
    display = str(name or "").strip() or str(source.get("composition") or "") or os.path.basename(path)

    def _commit():
        from classes.editor_tools.titles_text_common import create_track, place_clip, plan_overlay_track
        from classes.updates import nested_transaction
        File = _query("File")
        from classes.editor_tools._base import playhead_seconds
        app = _app()
        fps = _project_fps()
        start = _snap(playhead_seconds() if position is None else position, fps)
        frames = max(1, int(round(duration * float(fps))))
        length = frames / float(fps)
        if length > duration + 1e-6:
            length = max(1.0 / float(fps), (frames - 1) / float(fps))
        layer, created = plan_overlay_track(start, start + length, str(track or ""))
        with nested_transaction(app.updates):
            existing = File.get(path=path)
            if existing:
                # updates merge and undo cannot remove a key an edit added: give the
                # file a neutral link (None = not linked) outside history first
                _seed_absent_keys(existing, {LINK_KEY: None})
                existing.data = dict(existing.data, **{LINK_KEY: stored})
                existing.save()
                f = existing
            else:
                f = File()
                data = copy.deepcopy(reader)
                data["name"] = display
                data[LINK_KEY] = stored
                f.data = data
                f.save()
            if created:
                create_track(layer, LINKED_TRACK_LABEL)
            clip = place_clip(f.id, start, length, layer, title=display)
        return f.id, clip, layer, created, start, length

    file_id, clip, layer, created, start, length = _commit_on_gui(_commit)
    return {"file_id": file_id, "timeline_clip_id": (clip or {}).get("id"), "position": round(start, 3),
            "duration": round(length, 3), "end": round(start + length, 3), "layer": layer, "new_track": created,
            "path": path, "kind": stored["kind"], "name": display, "state": stored["state"]}


def swap_linked_media(file_id: str, new_path: str, link_update: Optional[dict] = None) -> dict:
    """Point linked file *file_id* at newly rendered *new_path* (and merge *link_update*), as ONE undo step.

    Every clip of the file follows (reader, duration). A clip whose out-point
    lies past the end of a shorter new render is shortened to it, and one
    that would start after it moves its window back -- each reported in
    ``warnings``. A longer render leaves clips as they are (``notes`` says
    so). Probes on the calling thread: call off the GUI thread.
    """
    new_path = _check_media_path(new_path)
    reader = probe_media(new_path)
    duration = _media_duration(reader)
    if duration <= 0:
        raise LinkError(f"{os.path.basename(new_path)} has no duration; is the render complete?")
    update = dict(link_update or {})

    def _commit():
        from classes import project_files
        from classes.updates import nested_transaction
        f = _linked_file(file_id)
        old = copy.deepcopy(f.data)
        link = read_link(old) or {}
        merged = dict(link, **{k: v for k, v in update.items() if k not in ("source", "render")})
        for sub in ("source", "render"):
            if isinstance(update.get(sub), dict):
                merged[sub] = dict(link.get(sub) or {}, **update[sub])
        stored = normalize_link(merged)
        new = project_files.relinked_file_data(old, reader, reader.get("media_type"), reader.get("fingerprint"))
        new[LINK_KEY] = stored
        new["name"] = old.get("name") or new.get("name")
        warnings, notes = [], []
        fps = _project_fps()
        frame = 1.0 / float(fps)
        old_duration = float(old.get("duration") or 0.0)
        with nested_transaction(_app().updates):
            # the last whole project frame of the new render (clip ends stay on the frame grid)
            end_limit = math.floor(duration * float(fps) + 1e-6) / float(fps)
            for c in _query("Clip").filter(file_id=f.id):
                cs, ce = float(c.data.get("start") or 0.0), float(c.data.get("end") or 0.0)
                title = c.data.get("title") or c.id
                if end_limit - cs < frame - 1e-9:  # less than one whole frame would remain
                    length = min(ce - cs, end_limit)
                    c.data["start"] = _snap(max(0.0, end_limit - length), fps)
                    c.data["end"] = end_limit
                    c.save()
                    warnings.append(f"clip {title!r} started past the end of the new {duration:.2f}s render; it "
                                    f"now shows its last {end_limit - c.data['start']:.2f}s")
                elif ce > end_limit + 1e-6:
                    c.data["end"] = end_limit
                    c.save()
                    warnings.append(f"clip {title!r} was shortened from {ce - cs:.2f}s to {end_limit - cs:.2f}s: "
                                    f"the new render is only {duration:.2f}s long")
            if duration > old_duration + frame / 2 and old_duration > 0:
                notes.append(f"the new render is {duration:.2f}s (was {old_duration:.2f}s); clips keep their "
                             "length -- trim them longer to show the rest")
            removed = [k for k in old if k not in new]
            f.data = new
            updated = project_files.save_file_and_sync_clips(f, removed)
        return stored, updated, warnings, notes

    stored, updated, warnings, notes = _commit_on_gui(_commit)
    clear_render_error(file_id)
    return {"file_id": str(file_id), "path": new_path, "duration": round(duration, 3),
            "updated_clip_ids": list(updated), "warnings": warnings, "notes": notes, "kind": stored["kind"],
            "state": stored["state"], "fingerprint": (stored.get("render") or {}).get("fingerprint")}


def update_link(file_id: str, changes: dict) -> dict:
    """Merge *changes* into a file's link (metadata only, e.g. props without a re-render), ONE undo step.

    ``source`` and ``render`` merge key by key; ``props`` replaces the props
    (pass the full new props). A change that leaves the link as it was adds
    no undo step (``changed: false``).
    """
    if not isinstance(changes, dict) or not changes:
        raise LinkError("say what to change in the link")

    def _commit():
        from classes import project_files
        from classes.updates import nested_transaction
        f = _linked_file(file_id)
        link = read_link(f.data) or {}
        merged = dict(link, **{k: v for k, v in changes.items() if k not in ("source", "render")})
        for sub in ("source", "render"):
            if isinstance(changes.get(sub), dict):
                merged[sub] = dict(link.get(sub) or {}, **changes[sub])
        stored = normalize_link(merged)
        if stored == f.data.get(LINK_KEY):
            return stored, False
        with nested_transaction(_app().updates):
            f.data = dict(f.data, **{LINK_KEY: stored})
            project_files.save_file_and_sync_clips(f)
        return stored, True

    stored, changed = _commit_on_gui(_commit)
    return {"file_id": str(file_id), "changed": changed, "link": read_link({LINK_KEY: stored})}


def unlink(file_id: str) -> dict:
    """Drop a file's link and keep its rendered media, as ONE undo step (``updates.delete``)."""

    def _commit():
        from classes import project_files
        from classes.updates import nested_transaction
        f = _linked_file(file_id)
        old_link = read_link(f.data)
        new = {k: v for k, v in f.data.items() if k != LINK_KEY}
        with nested_transaction(_app().updates):
            f.data = new
            clip_ids = project_files.save_file_and_sync_clips(f, removed_keys=(LINK_KEY,))
        return old_link, clip_ids, str(f.data.get("path") or "")

    old_link, clip_ids, media = _commit_on_gui(_commit)
    clear_render_error(file_id)
    return {"file_id": str(file_id), "kind": (old_link or {}).get("kind"), "path": media,
            "clip_ids": list(clip_ids)}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_link(link: dict, *, on_progress: Optional[ProgressFn] = None,
                should_cancel: Optional[CancelFn] = None, project_path: Any = _CURRENT) -> Tuple[str, dict]:
    """Render *link* with its provider into the links folder; returns (media path, completed link).

    The provider renders into a hidden staging folder next to the target;
    the finished file is moved into ``links/<kind>/<composition>-<hash>.<ext>``
    and the staging folder removed, so a failed or cancelled render leaves
    nothing behind. The returned link carries ``render`` (codec, size, fps,
    frames, output, rendered_at, fingerprint), ``state: fresh`` and any
    ``source_updates`` / effective props from the provider. Blocking.
    """
    stored = normalize_link(link)
    kind = stored["kind"]
    provider = require_provider(kind)
    gone = _source_gone(stored)
    if gone:
        raise SourceMissing(gone + "; relink it or unlink the clip")
    decoded = read_link({LINK_KEY: stored}) or stored
    fingerprint = provider.fingerprint(decoded)
    folder = links_dir(kind, project_path)
    os.makedirs(folder, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=folder)
    progress = on_progress or (lambda _f, _m: None)
    cancel = should_cancel or (lambda: False)
    from classes.handoff.jobs import JobCancelled
    try:
        try:
            result = provider.render(decoded, staging, on_progress=progress, should_cancel=cancel)
        except JobCancelled:
            raise
        except Exception:
            if cancel():  # whatever the provider raised on its way out of a cancel, it is a cancel
                raise JobCancelled(f"{kind_label(kind)} render cancelled") from None
            raise
        if cancel():
            raise JobCancelled(f"{kind_label(kind)} render cancelled")
        if not isinstance(result, RenderResult):
            raise LinkError(f"the {kind} provider returned no render result")
        produced = os.path.abspath(result.path)
        if not os.path.isfile(produced) or os.path.getsize(produced) == 0:
            raise LinkError(f"the {kind_label(kind)} render produced no file")
        codec = str(result.codec or "").lower()
        if codec not in CODECS or produced.lower().endswith(".webm"):
            raise LinkError(f"the {kind_label(kind)} render is {codec or os.path.splitext(produced)[1]}; linked "
                            "media must be ProRes 4444, H.264 or qtrle (libopenshot drops WebM alpha)")
        ext = os.path.splitext(produced)[1] or CODEC_EXTENSIONS[codec]
        # Check everything about the result BEFORE the file is installed: a bad fps or size
        # must not leave a render behind in the links folder.
        completed = dict(stored)
        source = dict(stored.get("source") or {})
        source.update(result.source_updates or {})
        completed["source"] = source
        completed["props"] = result.props if result.props is not None else (decoded.get("props") or {})
        try:
            render_meta: Dict[str, Any] = {
                "codec": codec, "width": int(result.width), "height": int(result.height),
                "fps": _fps_dict(result.fps), "duration_frames": int(result.duration_frames),
                "rendered_at": _utc_now(), "fingerprint": fingerprint,
            }
        except (TypeError, ValueError) as exc:
            raise LinkError(f"the {kind} provider returned a bad render result: {exc}") from None
        render_meta["output"] = None
        completed["render"] = render_meta
        completed["state"] = "fresh"
        completed["error"] = None
        normalize_link(completed)  # raises LinkError before anything is installed
        target = _unique_path(folder, render_file_name(stored, fingerprint, ext))
        if path_is_under(produced, staging):
            os.replace(produced, target)
        else:
            # a host (After Effects) rendered elsewhere: bring it into the staging folder first, so
            # a failed copy is cleaned up with it; files in the temp folder are ours to take
            staged = os.path.join(staging, "incoming" + ext)
            if path_is_under(produced, tempfile.gettempdir()):
                shutil.move(produced, staged)
            else:
                shutil.copy2(produced, staged)
            os.replace(staged, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    completed["render"]["output"] = output_token(target, project_path)
    final = normalize_link(completed)
    final_decoded = read_link({LINK_KEY: final}) or final
    final_decoded["warnings"] = list(result.warnings or [])
    return target, final_decoded


def _strip_runtime(link: dict) -> dict:
    out = dict(link)
    out.pop("warnings", None)
    return out


def precheck_placement(position: Optional[float] = None, track: Optional[str] = None,
                       duration: Optional[float] = None) -> None:
    """Refuse a placement that cannot work BEFORE anything is rendered (GUI-thread read, blocking hop).

    Checks *position* and that an explicit *track* exists and is unlocked; with
    an expected *duration* also that the track is free for that window.
    Raises LinkError with the reason.
    """
    if position is not None:
        try:
            if float(position) < 0:
                raise LinkError(f"position must be 0 or later, got {position}")
        except (TypeError, ValueError):
            raise LinkError(f"position must be a number of seconds, got {position!r}") from None
    if not str(track or "").strip():
        return

    def _check():
        from classes.editor_tools._base import ToolError, ensure_unlocked, resolve_layer
        from classes.editor_tools.titles_text_common import plan_overlay_track
        try:
            layer = resolve_layer(str(track))
            ensure_unlocked(layer)
            if duration and position is not None:
                plan_overlay_track(float(position), float(position) + float(duration), str(track))
        except ToolError as exc:
            raise LinkError(str(exc)) from None

    _read_on_gui(_check)


def _discard_render(path: str) -> None:
    """Remove a render nothing references (a failed add or swap)."""
    try:
        os.unlink(path)
    except OSError:
        log.warning("Could not remove the unused render %s", path, exc_info=True)


def import_linked(link: dict, *, position: Optional[float] = None, track: Optional[str] = None, name: str = "",
                  on_progress: Optional[ProgressFn] = None, should_cancel: Optional[CancelFn] = None,
                  expected_duration: Optional[float] = None) -> dict:
    """Render *link* and add the result as a linked clip (render + :func:`add_linked_media`). Blocking.

    The placement is checked before rendering (*expected_duration*, when the
    provider knows it, also checks the track is free); a render that cannot
    be added is deleted, so a refused import leaves nothing behind.
    """
    normalize_link(link)
    precheck_placement(position, track, expected_duration)
    path, completed = render_link(link, on_progress=on_progress, should_cancel=should_cancel)
    warnings = list(completed.get("warnings") or [])
    try:
        receipt = add_linked_media(path, _strip_runtime(completed), position=position, track=track, name=name)
    except Exception as exc:
        from classes.editor_tools.titles_text_common import CommitTimeout
        if not isinstance(exc, CommitTimeout):  # a timed-out commit may still land: keep its file
            _discard_render(path)
        raise
    receipt["warnings"] = warnings
    receipt["fingerprint"] = (completed.get("render") or {}).get("fingerprint")
    return receipt


_rendering: set = set()          # file ids between the start of a re-render and its media swap
_rendering_lock = threading.Lock()


def rerender_linked(file_id: str, *, props: Optional[dict] = None, on_progress: Optional[ProgressFn] = None,
                    should_cancel: Optional[CancelFn] = None, replace_props: bool = False) -> dict:
    """Re-render a linked file from its source (with *props* merged in) and swap the media: ONE undo step.

    *replace_props* uses *props* as the complete new props instead of merging
    (the props dialog, where keys can be removed).

    Registers a ``handoff.jobs`` job keyed by the file id while it renders
    (``link_state`` reports ``rendering``; the toolbar pill shows it). A failed
    render leaves the project and history untouched and is remembered as
    the file's ``error`` state for the session. Blocking: call off the GUI
    thread.
    """
    from classes.handoff import jobs
    f = _read_on_gui(_linked_file, file_id)
    link = read_link(f.data) or {}
    if props is not None:
        if not isinstance(props, dict):
            raise LinkError("props must be an object")
        link["props"] = dict(props) if replace_props else dict(link_props(link), **props)
    kind = str(link.get("kind") or "")
    label = "Rendering %s" % ((link.get("source") or {}).get("composition") or kind_label(kind))
    with _rendering_lock:
        if str(file_id) in _rendering or jobs.job_for(str(file_id)) is not None:
            raise LinkError("that linked clip is already rendering; wait for it to finish or cancel it")
        _rendering.add(str(file_id))

    try:
        with jobs.track_job(label, key=str(file_id), kind=kind) as job:
            def _progress(fraction, message):
                job.report(fraction, message)
                if on_progress is not None:
                    on_progress(fraction, message)

            def _cancel():
                return job.should_cancel() or bool(should_cancel and should_cancel())

            path, completed = render_link(link, on_progress=_progress, should_cancel=_cancel)
    except jobs.JobCancelled:
        with _rendering_lock:
            _rendering.discard(str(file_id))
        raise
    except Exception as exc:
        with _rendering_lock:
            _rendering.discard(str(file_id))
        if job.should_cancel() or bool(should_cancel and should_cancel()):
            raise jobs.JobCancelled("render cancelled") from None  # a cancel, not a failed render
        set_render_error(str(file_id), str(exc))
        raise
    try:
        warnings = list(completed.get("warnings") or [])
        receipt = swap_linked_media(str(file_id), path, _strip_runtime(completed))
    except LinkError as exc:  # the render is fine but the project could not take it
        set_render_error(str(file_id), str(exc))
        _discard_render(path)
        raise
    finally:
        with _rendering_lock:
            _rendering.discard(str(file_id))
    receipt["warnings"] = warnings + list(receipt.get("warnings") or [])
    receipt["props"] = link_props(completed)
    return receipt


# ---------------------------------------------------------------------------
# Save: adopt renders of an unsaved project into its assets folder
# ---------------------------------------------------------------------------

def adopt_linked_renders(files: List[dict], clips: List[dict], project_file_path: str,
                         previous_path: Optional[str] = None) -> List[Tuple[str, str]]:
    """Bring linked renders into ``<project>_assets/links`` when a project is saved (``project_data.save``).

    Renders of a never-saved project (``~/.openshot_qt/links``) are MOVED
    (renamed, like generated media); renders in a previous project's assets
    folder (Save As) are HARD-LINKED, so the old project keeps working and no
    bytes are copied. Save runs on the GUI thread, so only these instant
    operations are used: a render on another volume (or a file system without
    hard links) keeps its old, still-valid path (logged; Collect Media copies
    it) -- never a slow or failed save. File paths and clip readers are
    updated in memory; ``render.output`` tokens (``@assets/links/...``) stay
    valid. Returns the move ledger ``[(src, dest)]`` for
    ``assets.reverse_media_moves``.
    """
    from classes import info
    from classes.assets import get_assets_path
    moves: List[Tuple[str, str]] = []
    if not project_file_path:
        return moves
    new_assets = get_assets_path(project_file_path, create_paths=False)
    if not new_assets:
        return moves
    new_root = os.path.join(new_assets, LINKS_FOLDER)
    sources = [(os.path.join(info.USER_PATH, LINKS_FOLDER), "move")]
    if previous_path and os.path.abspath(previous_path) != os.path.abspath(project_file_path):
        old_assets = get_assets_path(previous_path, create_paths=False)
        if old_assets:
            sources.append((os.path.join(old_assets, LINKS_FOLDER), "copy"))
    remap: Dict[str, str] = {}
    id_to_new: Dict[str, str] = {}
    for f in files or []:
        if not isinstance(f, dict) or not isinstance(f.get(LINK_KEY), dict):
            continue
        src = str(f.get("path") or "")
        abs_src = os.path.abspath(src) if src else ""
        if abs_src in remap:  # another file record of the same render
            f["path"] = remap[abs_src]
            id_to_new[str(f.get("id"))] = remap[abs_src]
            continue
        if not src or "%" in src or not os.path.isfile(src) or path_is_under(src, new_root):
            continue
        match = next(((root, m) for root, m in sources if path_is_under(src, root)), None)
        if match is None:
            continue
        root, mode = match
        dest = os.path.join(new_root, os.path.relpath(abs_src, root))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                dest = _unique_path(os.path.dirname(dest), os.path.basename(dest))
            # Save runs on the GUI thread: only instant operations here. A rename (same
            # volume) or a hard link (Save As) costs nothing; a render that would need its
            # bytes copied (another volume) keeps its current, still valid path.
            if mode == "move":
                os.rename(abs_src, dest)
                moves.append((abs_src, dest))
            else:
                os.link(abs_src, dest)
        except OSError as exc:
            if exc.errno in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
                log.info("Linked render %s stays where it is (it would need a copy to reach %s); File > Collect "
                         "Media copies it into the project", abs_src, new_root)
            else:
                log.warning("Could not bring linked render %s into %s; it keeps its current path", abs_src,
                            new_root, exc_info=True)
            continue
        remap[abs_src] = dest
        id_to_new[str(f.get("id"))] = dest
        f["path"] = dest
        log.info("Linked render %s -> %s (%s)", abs_src, dest, mode)
    for c in clips or []:
        reader = c.get("reader") if isinstance(c, dict) else None
        if not isinstance(reader, dict):
            continue
        fid = str(c.get("file_id") or "")
        rpath = os.path.abspath(str(reader.get("path") or "")) if reader.get("path") else ""
        if fid in id_to_new:
            reader["path"] = id_to_new[fid]
        elif rpath in remap:
            reader["path"] = remap[rpath]
    return moves


__all__ = [
    "LINK_KEY", "LINK_VERSION", "KINDS", "STATES", "CODECS", "LinkError", "SourceMissing", "LinkProvider",
    "RenderResult", "LinkCheck", "normalize_link", "read_link", "link_props", "encode_props", "decode_props",
    "is_linked", "kind_label", "register_provider", "unregister_provider", "provider_for", "registered_kinds",
    "supports_studio", "require_provider", "fingerprint_value", "fingerprint_sources", "short_fingerprint",
    "links_root", "links_dir", "output_token", "render_file_name", "check_link", "link_state", "probe_media",
    "add_linked_media", "swap_linked_media", "update_link", "unlink", "render_link", "import_linked",
    "rerender_linked", "adopt_linked_renders", "set_render_error", "clear_render_error", "render_error",
    "file_id_for_clip",
]
