"""Linked clips rendered by HyperFrames (``zenvi_link.kind == "hyperframes"``).

A HyperFrames link records what it renders in ``source`` (``project_dir``,
``entry`` -- the html file --, ``composition``, ``file`` / ``line`` to open)
and in its own ``hyperframes`` block (unknown keys survive save/load):

* ``role: "project"`` -- the whole project (``index.html``), as HyperFrames'
  own ``render`` makes it: H.264 MP4 with its sound.
* ``role: "composition"`` -- one nested composition on a transparent
  background, ``host`` naming its host element in ``index.html``. A
  composition loaded from a file renders through a one-host wrapper; an
  inline one through a copy of ``index.html`` where everything but it (and
  the elements around it, which keep their layout) is hidden.
* ``role: "layer"`` -- the root composition's graphics: ``index.html`` with
  the clips Zenvi rebuilt natively hidden (``exclude``: element ids, or
  ``@<path>`` element paths for elements without an id), transparent. Hidden,
  not removed: the root's scripts still find them and the layout stays.

``props`` hold only the values changed in Zenvi. A render reads the
variables as the HTML sets them NOW (the declared defaults; for a
composition, its mount's ``data-variable-values`` on top) and puts the props
over them, so edits made in HyperFrames keep coming through
(:func:`current_values`); a prop equal to the HTML's value is not an
override and is dropped when the clip renders.

``hyperframes.fps`` is the frame rate it renders at (the Zenvi project's).
Renders are SDR (``--sdr``: libopenshot has no HDR path) and are not
colour-converted (SPEC section 5, 2026-10-05: linked media is treated like any
other media). Transparent renders are ProRes 4444 (HyperFrames' MOV, video
only); a composition that turns out fully opaque on its first, middle and
last frames is re-encoded to H.264 with the same YUV values to save space
(SPEC 3.6 codec rule).

Freshness (:meth:`HyperFramesProvider.fingerprint`) hashes the project's
html (structurally: HyperFrames Studio re-serializes files and stamps
``data-hf-id`` attributes, which change nothing), its other sources and
assets (also inside symlinked folders), and the link's role / entry /
composition / exclusions / props / fps -- never what a render fills in, so
a fresh import reads fresh.
"""

from __future__ import annotations

import copy
import hashlib
import html.parser
import json
import os
import subprocess
import threading
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from classes.handoff import linked_media as lm
from classes.handoff.hyperframes import cli as hf_cli
from classes.handoff.hyperframes import parser as hfp
from classes.handoff.hyperframes import wrappers
from classes.logger import log

KIND = "hyperframes"
BLOCK = "hyperframes"
ROLES = ("project", "composition", "layer")
ASSET_SUFFIXES = (".css", ".js", ".mjs", ".cjs", ".json", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                  ".avif", ".bmp", ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".mp3", ".wav", ".m4a", ".aac",
                  ".ogg", ".oga", ".opus", ".flac", ".woff", ".woff2", ".ttf", ".otf", ".lottie", ".glsl",
                  ".frag", ".vert", ".srt", ".vtt")
HTML_SUFFIXES = (".html", ".htm")
EXCLUDED_DIRS = frozenset(lm.DEFAULT_EXCLUDED_DIRS | {".hyperframes", "renders", "snapshots", ".zenvi"})
IGNORED_ATTRS = frozenset({"data-hf-id"})


class HyperFramesLinkError(lm.LinkError):
    """A HyperFrames link cannot be rendered as recorded; the message says what to do."""


# ---------------------------------------------------------------------------
# Source fingerprints
# ---------------------------------------------------------------------------

class _Canon(html.parser.HTMLParser):
    """HTML as a stream of structural events (attribute order, quoting, entities and data-hf-id ignored)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.digest = hashlib.sha256()
        self._raw = 0

    def _put(self, *parts: str) -> None:
        self.digest.update(json.dumps(parts, ensure_ascii=False).encode("utf-8"))

    def handle_starttag(self, tag, attrs):
        items = sorted((k.lower(), v or "") for k, v in attrs if k.lower() not in IGNORED_ATTRS)
        self._put("<", tag.lower(), json.dumps(items, ensure_ascii=False))
        if tag.lower() in ("script", "style"):
            self._raw += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in ("script", "style"):
            self._raw -= 1

    def handle_endtag(self, tag):
        self._put(">", tag.lower())
        if tag.lower() in ("script", "style") and self._raw:
            self._raw -= 1

    def handle_data(self, data):
        text = data.strip() if self._raw else " ".join(data.split())
        if text:
            self._put("t", text)


_canon_cache: Dict[str, Tuple[int, int, str]] = {}
_canon_lock = threading.Lock()


def canonical_html_digest(path: str) -> str:
    """sha256 of an html file's structure (cached by size + mtime)."""
    try:
        st = os.stat(path)
    except OSError:
        return "missing"
    key = os.path.abspath(path)
    with _canon_lock:
        hit = _canon_cache.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return "unreadable"
    canon = _Canon()
    canon.feed(text)
    canon.close()
    value = canon.digest.hexdigest()
    with _canon_lock:
        if len(_canon_cache) > 4096:
            _canon_cache.clear()
        _canon_cache[key] = (st.st_size, st.st_mtime_ns, value)
    return value


def html_digests(root: str) -> Dict[str, str]:
    """{project-relative html file: structural digest} for the html files a render can read."""
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".render-"))
        for name in sorted(filenames):
            if name.lower().endswith(HTML_SUFFIXES):
                full = os.path.join(dirpath, name)
                out[os.path.relpath(full, root).replace(os.sep, "/")] = canonical_html_digest(full)
    return out


def linked_dir_stats(root: str) -> Dict[str, List[Any]]:
    """{project-relative file: [size, mtime]} for files inside symlinked folders of the project.

    ``fingerprint_sources`` (and the walk above) do not follow symlinks, so a
    ``compositions/`` or ``assets/`` folder that is a link would never make a
    clip stale; its files are listed here instead.
    """
    out: Dict[str, List[Any]] = {}
    seen = set()
    for dirpath, dirnames, _files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".render-"))
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if not os.path.islink(full):
                continue
            real = os.path.realpath(full)
            if real in seen or not os.path.isdir(real):
                continue
            seen.add(real)
            for sub, subdirs, files in os.walk(real):
                subdirs[:] = sorted(x for x in subdirs if x not in EXCLUDED_DIRS)
                for name in sorted(files):
                    if not name.lower().endswith(ASSET_SUFFIXES + HTML_SUFFIXES):
                        continue
                    path = os.path.join(sub, name)
                    rel = os.path.join(os.path.relpath(full, root), os.path.relpath(path, real)).replace(os.sep, "/")
                    if name.lower().endswith(HTML_SUFFIXES):
                        out[rel] = ["html", canonical_html_digest(path)]
                        continue
                    try:
                        st = os.stat(path)
                        out[rel] = [st.st_size, st.st_mtime_ns]
                    except OSError:
                        out[rel] = ["missing"]
    return out


def link_identity(link: dict) -> dict:
    """What a render is made FROM (stable before and after rendering)."""
    source = link.get("source") or {}
    block = _block(link)
    return {"role": block.get("role") or "project", "entry": source.get("entry") or hfp.INDEX,
            "composition": source.get("composition"), "host": block.get("host"),
            "exclude": sorted(str(x) for x in (block.get("exclude") or [])), "fps": block.get("fps"),
            "props": lm.link_props(link)}


# ---------------------------------------------------------------------------
# Probing and codecs (ffmpeg, off the GUI thread)
# ---------------------------------------------------------------------------

def _ffprobe() -> str:
    from classes import ffmpeg_cli
    exe = ffmpeg_cli.find_ffmpeg("ffprobe")
    if not exe:
        raise HyperFramesLinkError("ffprobe was not found; install ffmpeg (brew install ffmpeg) or set "
                                   "ZENVI_FFMPEG_DIR")
    return exe


def probe_render(path: str) -> dict:
    """{codec_name, pix_fmt, width, height, fps (Fraction), frames, duration, has_audio} of a render."""
    from classes import ffmpeg_cli
    cmd = [_ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", path]
    try:
        proc = ffmpeg_cli.run_ffmpeg(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise HyperFramesLinkError(f"cannot read the render {os.path.basename(path)}: {exc}") from None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise HyperFramesLinkError(f"the render {os.path.basename(path)} has no video")
    try:
        num, den = str(video.get("r_frame_rate") or "30/1").split("/")
        fps = Fraction(int(num), int(den or 1))
    except (ValueError, ZeroDivisionError):
        fps = Fraction(30, 1)
    duration = float(video.get("duration") or (data.get("format") or {}).get("duration") or 0.0)
    frames = int(video.get("nb_frames") or 0) or int(round(duration * float(fps)))
    return {"codec_name": video.get("codec_name"), "pix_fmt": video.get("pix_fmt"),
            "width": int(video.get("width") or 0), "height": int(video.get("height") or 0), "fps": fps,
            "frames": frames, "duration": duration,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def opaque_everywhere(path: str, duration: float) -> bool:
    """True when the first, middle and last frame of a render have no transparent pixel."""
    from classes.handoff import alpha
    times = sorted({0.0, max(0.0, duration / 2.0), max(0.0, duration - 0.05)})
    try:
        return not any(alpha.has_transparency(path, at_seconds=t) for t in times)
    except alpha.AlphaError as exc:
        log.warning("alpha check of %s failed (%s); keeping ProRes 4444", path, exc)
        return False


# An opaque ProRes render re-encoded to H.264 keeps its YUV values: no colour conversion (SPEC section 5,
# 2026-10-05). HyperFrames writes its ProRes with BT.601 coefficients, untagged; the H.264 says so.
H264_ARGS = ["-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p", "-colorspace", "smpte170m",
             "-color_primaries", "bt709", "-color_trc", "bt709", "-movflags", "+faststart"]


def to_h264(src: str, dst: str, *, has_audio: bool, should_cancel=None) -> str:
    """Re-encode an opaque render to H.264 (CRF 16, yuv420p, the same YUV values), sound as AAC."""
    from classes import ffmpeg_cli
    ffmpeg = ffmpeg_cli.find_ffmpeg("ffmpeg")
    if not ffmpeg:
        raise HyperFramesLinkError("ffmpeg was not found; install it (brew install ffmpeg)")
    cmd = [ffmpeg, "-y", "-nostdin", "-i", src] + H264_ARGS
    cmd += ["-c:a", "aac", "-b:a", "192k"] if has_audio else ["-an"]
    cmd.append(dst)
    result = ffmpeg_cli.run_ffmpeg_with_progress(cmd, should_cancel=should_cancel)
    if result.returncode != 0 or not os.path.isfile(dst):
        if should_cancel is not None and should_cancel():
            from classes.handoff.jobs import JobCancelled
            raise JobCancelled("the H.264 encode was cancelled")
        raise HyperFramesLinkError(f"re-encoding the render to H.264 failed (ffmpeg exit {result.returncode})")
    return dst


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------

def _block(link: dict) -> dict:
    block = link.get(BLOCK)
    return dict(block) if isinstance(block, dict) else {}


def _project_dir(link: dict) -> str:
    root = (link.get("source") or {}).get("project_dir")
    if not root or not os.path.isdir(root):
        raise lm.SourceMissing(f"the HyperFrames project {root!r} no longer exists; relink it or unlink the clip")
    return str(root)


def _find_element(doc: hfp.Document, scope: hfp.Element, ref: str) -> Optional[hfp.Element]:
    """An element of *scope* by id, or by ``@i/j/k`` child-index path (elements without ids)."""
    ref = str(ref or "")
    if ref.startswith("@"):
        node = scope
        try:
            for part in ref[1:].split("/"):
                node = node.children[int(part)]
        except (ValueError, IndexError):
            return None
        return node
    return next((e for e in scope.iter() if e.id == ref and e is not scope), None)


def element_signature(el: hfp.Element) -> str:
    """What an element without an id looks like (tag, class, media), to find it again when an edit to
    ``index.html`` moved its ``@i/j`` path."""
    return json.dumps([el.tag, el.attrs.get("class", ""),
                       el.attrs.get("src") or el.attrs.get("data-composition-src") or ""])


def _find_excluded(index: hfp.Document, scope: hfp.Element, ref: str, signature: Optional[str]
                   ) -> Optional[hfp.Element]:
    el = _find_element(index, scope, ref)
    if not ref.startswith("@") or not signature or (el is not None and element_signature(el) == signature):
        return el
    same = [e for e in scope.iter() if e is not scope and not e.id and element_signature(e) == signature]
    return same[0] if len(same) == 1 else None


def element_ref(el: hfp.Element, scope: hfp.Element) -> str:
    """How a link names *el* inside *scope*: its id, else its child-index path ``@i/j``."""
    if el.id:
        return el.id
    path: List[str] = []
    node = el
    while node is not None and node is not scope:
        parent = node.parent
        if parent is None:
            break
        path.append(str(parent.children.index(node)))
        node = parent
    return "@" + "/".join(reversed(path))


def _load(root: str) -> hfp.Project:
    """The project as the importer reads it: media without data-duration measured with ffprobe, so
    ``data-start="<id>"`` references to them resolve (review C5-1 #2)."""
    from classes.handoff.hyperframes import importer  # importer imports this module
    return hfp.load_project(root, probe=importer._probe_duration)


def _root_defaults(root: str) -> Dict[str, Any]:
    """The variables ``index.html`` declares (``<html data-composition-variables>``) with their defaults."""
    try:
        doc = hfp.read_document(root, hfp.INDEX)
    except lm.LinkError:
        return {}
    html_el = next((e for e in doc.iter() if e.tag == "html"), None)
    raw = html_el.attrs.get("data-composition-variables") if html_el is not None else None
    try:
        items = json.loads(raw) if raw else []
    except ValueError:
        return {}
    return {str(v.get("id")): v.get("default") for v in (items if isinstance(items, list) else [])
            if isinstance(v, dict) and v.get("id")}


def _host_clip(project: hfp.Project, link: dict) -> Optional[hfp.Clip]:
    """The composition host *link* renders.

    Its recorded ``host`` (an id, or an ``@i/j`` path for a host without one) counts only while that
    element still mounts the same composition (``data-composition-id``, and ``data-composition-src`` for
    one loaded from a file): an element added above an id-less host moves its path onto another host.
    Otherwise the host Studio stamped with the recorded ``data-hf-id``, else the one host that mounts it;
    None when that is not unique (never a wrong one).
    """
    block = _block(link)
    source = link.get("source") or {}
    comp_id = str(source.get("composition") or "")
    comp_id = "" if comp_id.startswith("@") else comp_id
    entry = str(source.get("entry") or "")
    src = entry if entry and entry != hfp.INDEX else None
    hosts = [c for c in project.root.clips if c.kind == "composition"]

    def mounts(c: hfp.Clip) -> bool:
        return (not comp_id or (c.composition_id or "") == comp_id) and (src is None or c.composition_src == src)

    hf_id = str(block.get("host_hf_id") or "")
    if hf_id:
        stamped = [c for c in hosts if c.element.attrs.get("data-hf-id") == hf_id and mounts(c)]
        if len(stamped) == 1:
            return stamped[0]
    ref = str(block.get("host") or "")
    by_ref = next((c for c in hosts if ref and element_ref(c.element, project.root.element) == ref), None)
    if by_ref is not None and mounts(by_ref):
        return by_ref
    same = [c for c in hosts if mounts(c)] if (comp_id or src) else []
    return same[0] if len(same) == 1 else None


def current_values(link: dict, project: Optional[hfp.Project] = None) -> Dict[str, Any]:
    """The variables as the project's HTML sets them now, for what *link* renders.

    The whole project and the root's layer: the root's declared defaults. A
    composition: its declared defaults with its mount's ``data-variable-values``
    on top (what HyperFrames uses when nothing overrides them).
    """
    role = _block(link).get("role") or "project"
    root = _project_dir(link)
    if role != "composition":
        return dict(project.root.variable_defaults()) if project is not None else _root_defaults(root)
    project = project if project is not None else _load(root)
    host = _host_clip(project, link)
    comp = project.composition_for(host) if host is not None else None
    values: Dict[str, Any] = dict(comp.variable_defaults()) if comp is not None else {}
    if host is not None:
        values.update(host.variable_values)
    return values


def overrides(props: Optional[dict], current: Dict[str, Any]) -> Dict[str, Any]:
    """The props that change something: those the HTML does not set to the same value."""
    return {k: copy.deepcopy(v) for k, v in (props or {}).items() if k not in current or current[k] != v}


def _with_ancestors(el: hfp.Element, root: hfp.Element) -> List[hfp.Element]:
    """*el* and its ancestors up to (not including) *root*, innermost first."""
    out = []
    node: Optional[hfp.Element] = el
    while node is not None and node is not root:
        out.append(node)
        node = node.parent
    return out


class HyperFramesProvider:
    kind = KIND
    label = "HyperFrames"
    supports_studio = True

    # -- freshness -------------------------------------------------------------
    def fingerprint(self, link: dict) -> str:
        root = _project_dir(link)
        entry = (link.get("source") or {}).get("entry") or hfp.INDEX
        if not os.path.isfile(os.path.join(root, *str(entry).split("/"))):
            raise lm.SourceMissing(f"{entry} is gone from the HyperFrames project {root}")
        extra = {"html": html_digests(root), "link": link_identity(link), "linked": linked_dir_stats(root)}
        return lm.fingerprint_sources(root, include=ASSET_SUFFIXES, exclude_dirs=EXCLUDED_DIRS, extra=extra)

    # -- rendering ---------------------------------------------------------------
    def render(self, link: dict, out_dir: str, *, on_progress, should_cancel) -> lm.RenderResult:
        root = _project_dir(link)
        block = _block(link)
        role = str(block.get("role") or "project")
        if role not in ROLES:
            raise HyperFramesLinkError(f"unknown HyperFrames link role {role!r}; re-import the project")
        fps = str(block.get("fps") or "") or None
        on_progress(None, "Preparing HyperFrames")
        cli = hf_cli.resolve_cli(root)
        # a whole-project render needs no reading of the HTML: HyperFrames resolves it
        project = _load(root) if role != "project" else None
        changed = overrides(lm.link_props(link), current_values(link, project))
        warnings: List[str] = []
        fmt = "mp4" if role == "project" else "mov"
        output = os.path.join(out_dir, "render." + fmt)

        def progress(fraction, message):
            on_progress(None if fraction is None else 0.92 * fraction, "HyperFrames: " + message)

        with wrappers.wrapper_folder(root) as folder:
            entry, variables = self._entry(project, link, role, changed, folder, fps, warnings)
            hf_cli.render(root, entry, output, fmt=fmt, cli=cli, variables=variables, fps=fps, sdr=True,
                          on_progress=progress, should_cancel=should_cancel)
        info = probe_render(output)
        codec = "h264"
        if fmt == "mov":
            codec = "prores4444"
            if opaque_everywhere(output, info["duration"]):
                on_progress(0.95, "Encoding H.264 (the render has no transparency)")
                mp4 = os.path.join(out_dir, "render.mp4")
                to_h264(output, mp4, has_audio=info["has_audio"], should_cancel=should_cancel)
                os.unlink(output)
                output, codec = mp4, "h264"
                info = probe_render(output)
        on_progress(1.0, "Rendered")
        # stored props = only what Zenvi changes, so later edits made in HyperFrames come through
        return lm.RenderResult(path=output, codec=codec, width=info["width"], height=info["height"], fps=info["fps"],
                               duration_frames=info["frames"], warnings=warnings, props=changed)

    def _entry(self, project: Optional[hfp.Project], link: dict, role: str, changed: dict, folder: str,
               fps: Optional[str], warnings: List[str]) -> Tuple[str, Optional[dict]]:
        """(entry file to render, --variables) for a link, writing its wrapper into *folder*.

        *changed*: the props that override what the HTML sets (:func:`overrides`).
        """
        if role == "project" or project is None:
            return (link.get("source") or {}).get("entry") or hfp.INDEX, changed or None
        root, index = project.root_dir, project.index
        block = _block(link)
        target = os.path.join(folder, wrappers.WRAPPER_FILE)
        if role == "layer":
            hidden = []
            raw_signatures = block.get("signatures")
            signatures: Dict[str, str] = raw_signatures if isinstance(raw_signatures, dict) else {}
            for ref in block.get("exclude") or []:
                el = _find_excluded(index, project.root.element, str(ref), signatures.get(str(ref)))
                if el is None:
                    warnings.append(f"the layer no longer has {ref!r}; it may now show more than before")
                    continue
                hidden.append(el)
            text = wrappers.document_wrapper(index, hide=hidden)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text)
            return wrappers.rel_entry(root, target), changed or None
        host_clip = _host_clip(project, link)
        if host_clip is None:
            raise HyperFramesLinkError(f"index.html no longer mounts the composition "
                                       f"{(link.get('source') or {}).get('composition') or block.get('host') or '?'!r} "
                                       "(or mounts it more than once); re-import the project or unlink the clip")
        host = host_clip.element
        values = dict(host_clip.variable_values)
        values.update(changed)
        if host.attrs.get("data-composition-src"):
            comp = project.root
            text = wrappers.composition_wrapper(index, host, width=comp.width, height=comp.height,
                                                variables=values or None, fps=fps)
        else:  # inline: index.html showing only this composition (and the elements around it), from 0
            rel = project.root.element
            path = _with_ancestors(host, rel)
            on_path = {id(e) for e in path} | {id(rel)}
            hidden = [c for anc in [rel] + path[1:] for c in anc.children
                      if id(c) not in on_path and c.tag not in hfp.NON_VISUAL_TAGS]
            overrides_: Dict[int, Dict[str, Optional[str]]] = {id(host): {"data-start": "0"}}
            if changed:
                overrides_[id(host)]["data-variable-values"] = json.dumps(values, ensure_ascii=False, sort_keys=True)
            duration = host_clip.duration if host_clip is not None else None
            overrides_[id(rel)] = {"data-duration": ("%g" % duration) if duration else None}
            text = wrappers.document_wrapper(index, hide=hidden, clear=path[1:], overrides=overrides_)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
        return wrappers.rel_entry(root, target), None

    # -- opening -----------------------------------------------------------------
    def open_source(self, link: dict) -> None:
        from classes.handoff import open_source
        root = _project_dir(link)
        source = link.get("source") or {}
        rel = source.get("file") or source.get("entry") or hfp.INDEX
        path = os.path.join(root, *str(rel).split("/"))
        try:
            open_source.open_in_editor(path, source.get("line"), folder=root)
        except open_source.EditorError as exc:
            raise HyperFramesLinkError(str(exc)) from None

    def open_studio(self, link: dict) -> None:
        hf_cli.open_studio(_project_dir(link))

    def editable_props(self, link: dict) -> dict:
        """The variables as the HTML sets them now, with the props changed in Zenvi on top."""
        props = lm.link_props(link)
        try:
            current = current_values(link)
        except lm.LinkError:
            return dict(props)
        out = copy.deepcopy(current)
        out.update(overrides(props, current))
        return out


def register() -> HyperFramesProvider:
    provider = HyperFramesProvider()
    lm.register_provider(provider)
    return provider


__all__ = [
    "KIND", "BLOCK", "ROLES", "HyperFramesProvider", "HyperFramesLinkError", "register", "link_identity",
    "html_digests", "canonical_html_digest", "linked_dir_stats", "probe_render", "opaque_everywhere", "to_h264",
    "element_ref", "element_signature", "current_values", "overrides",
]
