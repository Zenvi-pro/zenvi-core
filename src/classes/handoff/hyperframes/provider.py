"""Linked clips rendered by HyperFrames (``zenvi_link.kind == "hyperframes"``).

A HyperFrames link records what it renders in ``source`` (``project_dir``,
``entry`` -- the html file --, ``composition``, ``file`` / ``line`` to open)
and in its own ``hyperframes`` block (unknown keys survive save/load):

* ``role: "project"`` -- the whole project (``index.html``), as HyperFrames'
  own ``render`` makes it: H.264 MP4 with its sound. ``props`` are the root's
  variables (``--variables``).
* ``role: "composition"`` -- one nested composition on a transparent
  background, ``host`` naming its host element in ``index.html``.
  ``props`` are that mount's ``data-variable-values``. A composition loaded
  from a file renders through a one-host wrapper; an inline one through a
  copy of ``index.html`` holding only it.
* ``role: "layer"`` -- the root composition's graphics: ``index.html``
  without the clips Zenvi rebuilt natively (``exclude``: element ids, or
  ``@<path>`` element paths for elements without an id), transparent.

``hyperframes.fps`` is the frame rate it renders at (the Zenvi project's).
Transparent renders are ProRes 4444 (HyperFrames' MOV); a composition that
turns out fully opaque on its first, middle and last frames is re-encoded
to H.264 to save space (SPEC 3.6 codec rule).

Freshness (:meth:`HyperFramesProvider.fingerprint`) hashes the project's
html (structurally: HyperFrames Studio re-serializes files and stamps
``data-hf-id`` attributes, which change nothing), its other sources and
assets, and the link's role / entry / composition / exclusions / props /
fps -- never what a render fills in, so a fresh import reads fresh.
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


def to_h264(src: str, dst: str, *, has_audio: bool, should_cancel=None) -> str:
    """Re-encode an opaque ProRes render to H.264 (CRF 18, yuv420p, BT.709) with AAC sound."""
    from classes import ffmpeg_cli
    from classes.handoff import alpha
    ffmpeg = ffmpeg_cli.find_ffmpeg("ffmpeg")
    if not ffmpeg:
        raise HyperFramesLinkError("ffmpeg was not found; install it (brew install ffmpeg)")
    cmd = [ffmpeg, "-y", "-nostdin", "-i", src] + alpha.codec_args("h264")
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
        extra = {"html": html_digests(root), "link": link_identity(link)}
        return lm.fingerprint_sources(root, include=ASSET_SUFFIXES, exclude_dirs=EXCLUDED_DIRS, extra=extra)

    # -- rendering ---------------------------------------------------------------
    def render(self, link: dict, out_dir: str, *, on_progress, should_cancel) -> lm.RenderResult:
        root = _project_dir(link)
        block = _block(link)
        role = str(block.get("role") or "project")
        if role not in ROLES:
            raise HyperFramesLinkError(f"unknown HyperFrames link role {role!r}; re-import the project")
        props = lm.link_props(link)
        fps = str(block.get("fps") or "") or None
        on_progress(None, "Preparing HyperFrames")
        cli = hf_cli.resolve_cli(root)
        project = hfp.load_project(root)
        warnings: List[str] = []
        fmt = "mp4" if role == "project" else "mov"
        output = os.path.join(out_dir, "render." + fmt)

        def progress(fraction, message):
            on_progress(None if fraction is None else 0.92 * fraction, "HyperFrames: " + message)

        with wrappers.wrapper_folder(root) as folder:
            entry, variables = self._entry(project, link, role, props, folder, fps, warnings)
            hf_cli.render(root, entry, output, fmt=fmt, cli=cli, variables=variables, fps=fps,
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
        return lm.RenderResult(path=output, codec=codec, width=info["width"], height=info["height"], fps=info["fps"],
                               duration_frames=info["frames"], warnings=warnings)

    def _entry(self, project: hfp.Project, link: dict, role: str, props: dict, folder: str, fps: Optional[str],
               warnings: List[str]) -> Tuple[str, Optional[dict]]:
        """(entry file to render, --variables) for a link, writing its wrapper into *folder*."""
        root, index = project.root_dir, project.index
        block = _block(link)
        target = os.path.join(folder, wrappers.WRAPPER_FILE)
        if role == "project":
            return (link.get("source") or {}).get("entry") or hfp.INDEX, props or None
        if role == "layer":
            removed = []
            for ref in block.get("exclude") or []:
                el = _find_element(index, project.root.element, str(ref))
                if el is None:
                    warnings.append(f"the layer no longer has {ref!r}; it may now show more than before")
                    continue
                removed.append(el)
            text = wrappers.document_wrapper(index, remove=removed)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text)
            return wrappers.rel_entry(root, target), props or None
        host_ref = str(block.get("host") or "")
        host = _find_element(index, project.root.element, host_ref) if host_ref else None
        if host is None:
            raise HyperFramesLinkError(f"index.html no longer mounts the composition {host_ref or '?'!r}; re-import "
                                       "the project or unlink the clip")
        if host.attrs.get("data-composition-src"):
            comp = project.root
            text = wrappers.composition_wrapper(index, host, width=comp.width, height=comp.height,
                                                variables=dict(props) if props else None, fps=fps)
        else:  # inline: index.html holding only this composition, from 0
            keep = {id(host)}
            removed = [c for c in project.root.element.children
                       if id(c) not in keep and c.tag not in ("script", "style", "template")]
            host_clip = next((c for c in project.root.clips if c.element is host), None)
            overrides: Dict[int, Dict[str, Optional[str]]] = {id(host): {"data-start": "0"}}
            if props:
                overrides[id(host)]["data-variable-values"] = json.dumps(props, ensure_ascii=False, sort_keys=True)
            duration = host_clip.duration if host_clip is not None else None
            overrides[id(project.root.element)] = {"data-duration": ("%g" % duration) if duration else None}
            text = wrappers.document_wrapper(index, remove=removed, overrides=overrides)
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
        """The composition's declared variables with their current values (props win over defaults)."""
        props = lm.link_props(link)
        try:
            root = _project_dir(link)
            project = hfp.load_project(root)
        except lm.LinkError:
            return dict(props)
        block = _block(link)
        declared: Dict[str, Any] = dict(project.root.variable_defaults())
        if (block.get("role") or "project") == "composition":
            host_ref = str(block.get("host") or "")
            host = next((c for c in project.root.clips if c.kind == "composition" and
                         element_ref(c.element, project.root.element) == host_ref), None)
            comp = project.composition_for(host) if host is not None else None
            declared = dict(comp.variable_defaults()) if comp is not None else {}
            if host is not None:
                declared.update(host.variable_values)
        out = copy.deepcopy(declared)
        out.update(props)
        return out


def register() -> HyperFramesProvider:
    provider = HyperFramesProvider()
    lm.register_provider(provider)
    return provider


__all__ = [
    "KIND", "BLOCK", "ROLES", "HyperFramesProvider", "HyperFramesLinkError", "register", "link_identity",
    "html_digests", "canonical_html_digest", "probe_render", "opaque_everywhere", "to_h264", "element_ref",
]
