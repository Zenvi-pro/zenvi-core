"""``RemotionProvider``: linked clips rendered by a Remotion project's own Remotion (SPEC §3.6).

The link (``zenvi_link`` on the file) names the project folder, entry and
composition id. ``props`` holds the composition's full input props as
imported (its ``defaultProps`` merged with what the user set), so the
Linked Source props dialog shows every prop; Zenvi passes them all as
input props, which Remotion merges over ``defaultProps`` -- a prop added in
code later still takes its default, but changing a default in code does
not change clips that were already imported (edit their props instead).
The composition's defaults are cached in ``source.default_props``
(refreshed by every render) and the requested codec lives in the link's
own ``remotion`` block: ``{"codec": "auto" | "prores4444" | "h264" |
"qtrle", "frames": null | "0-89"}``.

Rendering (blocking, off the GUI thread):

1. stills of the first, middle and last frame (``helper.mjs still``) --
   they validate the composition and props cheaply and decide ``auto``:
   any pixel with alpha < 255 renders ProRes 4444 (libopenshot keeps its
   alpha), otherwise H.264 (CRF 18, yuv420p, BT.709). Never WebM.
2. the movie (``helper.mjs render``), into Zenvi's staging folder;
   ``qtrle`` is ProRes 4444 re-encoded by ffmpeg, and a ``<Still>``
   (one frame) becomes a 5 s clip of that frame.

The fingerprint covers the project's code and assets (content hash for
small files, size + mtime for big media), its package/lock/config files,
the props, the composition, the entry and the requested render settings.
Cancelling raises ``jobs.JobCancelled`` (never a LinkError), so a cancel
is not remembered as a render error.
"""

from __future__ import annotations

import os
import re
import shutil
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional

from classes.handoff import alpha
from classes.handoff.linked_media import (
    DEFAULT_EXCLUDED_DIRS, LinkError, RenderResult, SourceMissing, decode_props, encode_props,
    fingerprint_sources, fingerprint_value, link_props,
)
from classes.handoff.remotion import detect, helper, sources
from classes.logger import log

KIND = "remotion"
CODEC_CHOICES = ("auto", "prores4444", "h264", "qtrle")
SETTINGS_KEY = "remotion"
STILL_SECONDS = 5.0
STILL_FRAMES = "first,middle,last"
# What a render can depend on: code, styles, data and every kind of asset staticFile() serves.
WATCHED_SUFFIXES = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".json", ".css", ".scss", ".sass", ".less",
    ".mdx", ".glsl", ".frag", ".vert", ".wgsl", ".html", ".csv", ".srt", ".vtt", ".xml", ".yaml", ".yml",
    ".lock", ".lockb", ".env",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico", ".mp4", ".mov", ".webm", ".mkv",
    ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".ttf", ".otf", ".woff", ".woff2",
    ".lottie", ".riv", ".glb", ".gltf", ".hdr", ".exr", ".pdf",
)
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

ProgressFn = Callable[[Optional[float], str], None]


# ---------------------------------------------------------------------------
# Link helpers
# ---------------------------------------------------------------------------

def settings(link: dict) -> dict:
    value = (link or {}).get(SETTINGS_KEY)
    return dict(value) if isinstance(value, dict) else {}


def requested_codec(link: dict) -> str:
    """The codec the user asked for (``auto`` unless set)."""
    codec = str(settings(link).get("codec") or "auto").strip().lower()
    return codec if codec in CODEC_CHOICES else "auto"


def default_props(link: dict) -> dict:
    """The composition's defaultProps as of the last render or listing."""
    return decode_props(((link or {}).get("source") or {}).get("default_props"))


def make_link(project: detect.RemotionProject, composition: str, *, props: Optional[dict] = None,
              codec: str = "auto", defaults: Optional[dict] = None,
              source: Optional[sources.CompositionSource] = None, frames: Optional[str] = None) -> dict:
    """A new link for *composition* of *project* (validated by ``linked_media.normalize_link`` later)."""
    codec = str(codec or "auto").strip().lower()
    if codec not in CODEC_CHOICES:
        raise LinkError(f"codec must be one of {', '.join(CODEC_CHOICES)}, got {codec!r} (linked media is never "
                        "WebM: libopenshot drops its alpha)")
    src: Dict[str, Any] = {"project_dir": project.root, "entry": project.entry, "composition": composition,
                           "composition_key": None, "file": None, "line": None, "aep": None,
                           "default_props": encode_props(dict(defaults or {})),
                           "remotion_version": project.version}
    if source is not None:
        src.update(file=source.best_file, line=source.best_line, folder=source.folder)
    full = dict(defaults or {})
    full.update(props or {})
    return {"version": 1, "kind": KIND, "source": src, "props": full,
            SETTINGS_KEY: {"codec": codec, "frames": frames or None}, "state": "fresh", "error": None}


def _safe(name: str) -> str:
    return (_SAFE_RE.sub("-", str(name or "")).strip("-._") or "render")[:60]


def fps_fraction(value: Any) -> Fraction:
    try:
        return Fraction(float(value)).limit_denominator(1001)
    except (TypeError, ValueError, ZeroDivisionError):
        return Fraction(30, 1)


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def sources_fingerprint(project: detect.RemotionProject) -> str:
    """What a bundle of *project* depends on: code, assets and package/lock/config files. Blocking."""
    return fingerprint_sources(project.root, include=WATCHED_SUFFIXES, exclude_dirs=DEFAULT_EXCLUDED_DIRS,
                               extra={"entry": project.entry, "public": project.public_dir})


def _project_of(link: dict) -> detect.RemotionProject:
    project_dir = ((link or {}).get("source") or {}).get("project_dir")
    if not project_dir:
        raise SourceMissing("the linked clip does not name its Remotion project folder")
    if not os.path.isdir(project_dir):
        raise SourceMissing(f"the Remotion project {project_dir} no longer exists; relink it or unlink the clip")
    return detect.inspect_project(project_dir)


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------

def _scaled(report: ProgressFn, lo: float, hi: float) -> ProgressFn:
    def _report(fraction: Optional[float], message: str) -> None:
        report(None if fraction is None else lo + (hi - lo) * max(0.0, min(1.0, fraction)), message)
    return _report


class RemotionProvider:
    """Linked clips from Remotion compositions (kind ``remotion``)."""

    kind = KIND
    label = "Remotion"
    supports_studio = True

    def fingerprint(self, link: dict) -> str:
        project = _project_of(link)
        source = link.get("source") or {}
        return fingerprint_value({
            "sources": sources_fingerprint(project),
            "entry": source.get("entry") or project.entry,
            "composition": source.get("composition"),
            "props": link_props(link),
            "codec": requested_codec(link),
            "frames": settings(link).get("frames"),
            "helper": helper.HELPER_VERSION,
        })

    def render(self, link: dict, out_dir: str, *, on_progress: ProgressFn,
               should_cancel: Callable[[], bool]) -> RenderResult:
        source = link.get("source") or {}
        composition = str(source.get("composition") or "").strip()
        if not composition:
            raise LinkError("the linked clip does not name its Remotion composition")
        project = detect.require_ready(_project_of(link))
        entry = str(source.get("entry") or project.entry or "")
        if not entry or not os.path.isfile(os.path.join(project.root, *entry.split("/"))):
            entry = project.entry or ""
        if not entry:
            raise SourceMissing(f"the entry point of {project.name} is gone")
        props = link_props(link)
        codec = requested_codec(link)
        frames = settings(link).get("frames") or None
        bundle = helper.bundle_dir(project.root, entry, sources_fingerprint(project), project.version)
        try:
            helper.prune_bundles()
        except OSError:
            log.debug("Remotion bundle cache cleanup failed", exc_info=True)

        stills_dir = os.path.join(out_dir, "stills")
        still = helper.run_helper("still", project_dir=project.root, entry=entry, props=props,
                                  options={"composition": composition, "frames": STILL_FRAMES,
                                           "out_dir": stills_dir, "bundle": bundle},
                                  on_progress=_scaled(on_progress, 0.0, 0.15), should_cancel=should_cancel)
        meta = still.result
        warnings: List[str] = list(still.warnings)
        pngs = [str(s.get("output")) for s in (meta.get("stills") or []) if s.get("output")]
        if not pngs:
            raise LinkError(f"Remotion rendered no still of {composition}")
        if codec == "auto":
            try:
                transparent = any(alpha.has_transparency(p) for p in pngs)
            except alpha.AlphaError as exc:
                raise LinkError(f"could not check {composition} for transparency: {exc}") from None
            chosen = "prores4444" if transparent else "h264"
        else:
            chosen = codec
        fps = fps_fraction(meta.get("fps") or 30)
        total = int(meta.get("durationInFrames") or 0)
        if total <= 1:
            path = _still_movie(pngs[0], out_dir, composition, chosen, fps, should_cancel)
            duration_frames = max(1, int(round(STILL_SECONDS * float(fps))))
            warnings.append(f"{composition} is a still: Zenvi rendered it as a {STILL_SECONDS:g} s clip")
        else:
            helper_codec = "h264" if chosen == "h264" else "prores4444"
            output = os.path.join(out_dir, _safe(composition) + (".mp4" if helper_codec == "h264" else ".mov"))
            rendered = helper.run_helper(
                "render", project_dir=project.root, entry=entry, props=props,
                options={"composition": composition, "codec": helper_codec, "output": output, "frames": frames,
                         "concurrency": helper.default_concurrency(), "bundle": bundle},
                on_progress=_scaled(on_progress, 0.15, 0.9 if chosen == "qtrle" else 1.0),
                should_cancel=should_cancel)
            warnings += [w for w in rendered.warnings if w not in warnings]
            path = str(rendered.result.get("output") or output)
            duration_frames = int(rendered.result.get("durationInFrames") or total)
            if chosen == "qtrle":
                path = _reencode(path, os.path.join(out_dir, _safe(composition) + "-qtrle.mov"), "qtrle",
                                 _scaled(on_progress, 0.9, 1.0), should_cancel)
        shutil.rmtree(stills_dir, ignore_errors=True)

        updates: Dict[str, Any] = {"entry": entry, "default_props": encode_props(dict(meta.get("defaultProps") or {})),
                                   "remotion_version": still.remotion_version or project.version}
        try:
            found = sources.source_for(project.root, composition, entry)
        except OSError:
            found = None
        if found is not None:
            updates.update(file=found.best_file, line=found.best_line, folder=found.folder)
        return RenderResult(path=path, codec=chosen, width=int(meta.get("width") or 0),
                            height=int(meta.get("height") or 0), fps=fps, duration_frames=duration_frames,
                            warnings=warnings, source_updates=updates)

    def open_source(self, link: dict) -> None:
        from classes.handoff import open_source
        project = _project_of(link)
        source = link.get("source") or {}
        file, line = source.get("file"), source.get("line")
        target = os.path.join(project.root, *str(file).split("/")) if file else ""
        if not target or not os.path.isfile(target):
            found = sources.source_for(project.root, str(source.get("composition") or ""), project.entry)
            if found is not None:
                target, line = os.path.join(project.root, *found.best_file.split("/")), found.best_line
            elif project.entry_path:
                target, line = project.entry_path, None
            else:
                target = ""
        try:
            open_source.open_in_editor(target, line, folder=project.root)
        except open_source.EditorError as exc:
            raise LinkError(str(exc)) from None

    def open_studio(self, link: dict) -> None:
        from classes.handoff.remotion import studio
        project = detect.require_ready(_project_of(link))
        source = link.get("source") or {}
        studio.open_studio(project, str(source.get("composition") or "") or None)

    def editable_props(self, link: dict) -> dict:
        merged = default_props(link)
        merged.update(link_props(link))
        return merged


def _ffmpeg_movie(cmd_tail: List[str], output: str, on_progress: Optional[Callable[[float], None]],
                  should_cancel: Callable[[], bool]) -> str:
    from classes import ffmpeg_cli
    from classes.handoff.jobs import JobCancelled
    exe = ffmpeg_cli.find_ffmpeg("ffmpeg")
    if not exe:
        raise LinkError("ffmpeg was not found; install it (brew install ffmpeg) or set ZENVI_FFMPEG_DIR")
    partial = os.path.splitext(output)[0] + ".partial" + os.path.splitext(output)[1]
    result = ffmpeg_cli.run_ffmpeg_with_progress([exe, "-y", "-nostdin"] + cmd_tail + [partial],
                                                 on_progress=on_progress, should_cancel=should_cancel)
    if should_cancel():
        _remove(partial)
        raise JobCancelled("Remotion render cancelled")
    if result.returncode != 0 or not os.path.isfile(partial):
        _remove(partial)
        raise LinkError(f"ffmpeg could not write {os.path.basename(output)} (exit {result.returncode})")
    os.replace(partial, output)
    return output


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _still_movie(png: str, out_dir: str, composition: str, codec: str, fps: Fraction,
                 should_cancel: Callable[[], bool]) -> str:
    """A ``<Still>``: hold its frame for STILL_SECONDS in *codec*."""
    ext = ".mp4" if codec == "h264" else ".mov"
    output = os.path.join(out_dir, _safe(composition) + ext)
    rate = f"{fps.numerator}/{fps.denominator}"
    tail = ["-loop", "1", "-framerate", rate, "-t", f"{STILL_SECONDS:g}", "-i", png] + alpha.codec_args(codec)
    return _ffmpeg_movie(tail, output, None, should_cancel)


def _reencode(src: str, dst: str, codec: str, on_progress: ProgressFn, should_cancel: Callable[[], bool]) -> str:
    out = _ffmpeg_movie(["-i", src] + alpha.codec_args(codec) + ["-c:a", "copy"], dst,
                        lambda f: on_progress(f, "Encoding QuickTime Animation"), should_cancel)
    _remove(src)
    return out


__all__ = ["RemotionProvider", "make_link", "requested_codec", "default_props", "settings",
           "sources_fingerprint", "fps_fraction", "KIND", "CODEC_CHOICES", "SETTINGS_KEY", "STILL_SECONDS"]
