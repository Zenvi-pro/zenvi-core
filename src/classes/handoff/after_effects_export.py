"""Write a Zenvi -> After Effects export to disk, and run it in After Effects.

``export_after_effects(snapshot, output_dir, ...)`` writes ``<name>.jsx``,
``README.txt``, rendered title images (``titles/``), wipe images
(``masks/``) and, with ``collect_media``, copies of the media (``media/``)
so the folder can move to another machine. Everything is staged in a
hidden folder inside *output_dir* and moved into place only when complete;
a file that already exists there with other content is never overwritten
(the new one gets the next free name), so comps built from an earlier
export keep their footage.

``send_to_after_effects(snapshot, ...)`` exports into the project's
``<project>_assets/after_effects/<name>`` folder (``~/.openshot_qt/after_effects``
while unsaved; not a temp folder, because After Effects keeps referencing the
files) and runs the script in the connected After Effects through Zenvi Link
(``ae_run_jsx_file``). ``run_with_applescript`` is the macOS fallback when
After Effects is installed but Zenvi Link is not connected; it prefers the
After Effects that is running and can be cancelled. A run Zenvi starts on
an export written for people (``interactive``) goes through a small runner
script (``quiet_runner``) so the script's closing alert cannot block it.

Everything here blocks on the disk, Qt rendering threads, the network or a
subprocess: call it off the Qt GUI thread (``classes.handoff.jobs`` or a
background-safe editor tool).
"""

from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from classes.exporters import after_effects as AE
from classes.exporters.after_effects_js import js_str
from classes.exporters.after_effects_titles import NotNative, parse_title_svg
from classes.logger import log

ProgressFn = Optional[Callable[[Optional[float], str], None]]
CancelFn = Optional[Callable[[], bool]]
RenderSvg = Callable[[str, str, int, int], None]
AnalyzeMask = Callable[[str], "MaskInfo"]

AE_APP = "aftereffects"
RUN_TOOL = "ae_run_jsx_file"
# Zenvi Link gives ae_run_jsx_file 30 minutes (adobe-link e77b816: a long, heavily keyed timeline takes
# that long to build). Wait a minute more so its own answer -- result or timeout -- arrives first. The
# AppleScript route waits as long.
AE_RUN_TIMEOUT = 1860.0
# Edit > Undo names: Zenvi Link wraps every tool call in "Zenvi: <tool title>" and After Effects shows the
# outermost group's name; File > Scripts (and AppleScript's DoScriptFile) show the script's own group.
ZENVI_LINK_UNDO = "Zenvi: Run JSX file"
MAX_TITLE_PIXELS = 8192
COPY_CHUNK = 1 << 20
HASH_LIMIT = 64 << 20


class AeHandoffError(RuntimeError):
    """An export or send failed; the message says what to do next."""


class AeCancelled(AeHandoffError):
    pass


@dataclass
class MaskInfo:
    """What a wipe image looks like to libopenshot's Mask: one grey level (uniform) or an image."""

    uniform: bool
    gray: int = 0
    alpha: int = 255
    width: int = 0
    height: int = 0


@dataclass
class ExportResult:
    script_path: str
    folder: str
    readme_path: str
    collected: bool
    media: List[str] = field(default_factory=list)       # files copied or reused under media/ (relative)
    missing: List[str] = field(default_factory=list)     # media not found when exporting
    titles: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"script": self.script_path, "folder": self.folder, "readme": self.readme_path,
                "collected_media": self.collected, "media_files": len(self.media), "missing_media": self.missing,
                "titles": self.titles, "warnings": self.warnings, "stats": self.stats}


def undo_hint(via: str) -> str:
    """How to undo a build in After Effects, for a run through *via* ("zenvi-link", "applescript" or "script")."""
    name = ZENVI_LINK_UNDO if via == "zenvi-link" else AE.UNDO_NAME
    return f'In After Effects, Edit > Undo "{name}" removes it.'


def describe_import(summary: Optional[dict], via: str, fallback: str = "") -> str:
    """The export script's summary line plus the undo hint for the route it ran through."""
    if summary and summary.get("summary"):
        text = str(summary["summary"])
        if summary.get("zenvi_ae_import") and summary.get("status") != "error":
            text += " " + undo_hint(via)
        return text
    return fallback or "After Effects ran the script."


@dataclass
class SendResult:
    export: ExportResult
    summary: Optional[dict]
    host_receipt: dict
    via: str = "zenvi-link"

    @property
    def message(self) -> str:
        return describe_import(self.summary, self.via, str(self.host_receipt.get("summary") or ""))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _report(progress: ProgressFn, fraction: Optional[float], message: str) -> None:
    if progress is not None:
        try:
            progress(fraction, message)
        except Exception:
            log.debug("export progress callback failed", exc_info=True)


def _check_cancel(should_cancel: CancelFn) -> None:
    if should_cancel is not None and should_cancel():
        raise AeCancelled("the After Effects export was cancelled")


def safe_name(text: str, fallback: str = "Untitled") -> str:
    """A file-system safe base name (``title_svg.safe_base_name``, the Title Editor's rule)."""
    from classes import title_svg
    return title_svg.safe_base_name(text, fallback=fallback)


def default_output_dir(project_path: Optional[str], name: str) -> str:
    """``<project folder>/<name>_AfterEffects``, or ``~/Downloads/<name>_AfterEffects`` for an unsaved project."""
    from classes import info
    base = os.path.dirname(os.path.abspath(project_path)) if project_path else info.DOWNLOADS_PATH
    return os.path.join(base, safe_name(name) + "_AfterEffects")


def send_folder(project_path: Optional[str], name: str) -> str:
    """Where Send To keeps its exports: ``<project>_assets/after_effects/<name>`` (``~/.openshot_qt/...`` unsaved)."""
    from classes import info
    root = None
    if project_path:
        try:
            from classes.assets import get_assets_path
            root = get_assets_path(project_path, create_paths=False)
        except Exception:
            log.debug("assets folder unavailable for %s", project_path, exc_info=True)
    return os.path.join(root or info.USER_PATH, "after_effects", safe_name(name))


def _same_file(a: str, b: str) -> bool:
    """True when *a* and *b* hold the same bytes (size first; a hash up to 64 MB, else size + mtime)."""
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    if sa.st_size != sb.st_size:
        return False
    if abs(sa.st_mtime - sb.st_mtime) < 1.0:
        return True
    if sa.st_size > HASH_LIMIT:
        return False

    def digest(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(COPY_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    return digest(a) == digest(b)


class _Layout:
    """Names inside the export folder: never two sources on one name, never overwriting other content."""

    def __init__(self, folder: str):
        self.folder = folder
        self.claimed: Dict[str, str] = {}   # relative path -> source it holds

    def place(self, source: str, subdir: str, name: str) -> Tuple[str, bool]:
        """(relative path, already there) for *source* under *subdir* named like *name*."""
        stem, ext = os.path.splitext(name)
        n = 1
        while True:
            candidate = name if n == 1 else f"{stem}-{n}{ext}"
            rel = f"{subdir}/{candidate}"
            holder = self.claimed.get(rel)
            if holder is not None:
                if os.path.abspath(holder) == os.path.abspath(source):
                    return rel, True
                n += 1
                continue
            dest = os.path.join(self.folder, subdir, candidate)
            if os.path.exists(dest) and not _same_file(source, dest):
                n += 1
                continue
            self.claimed[rel] = source
            return rel, os.path.exists(dest)


def _copy(src: str, dst: str, progress: Callable[[int], None], should_cancel: CancelFn) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            _check_cancel(should_cancel)
            chunk = fin.read(COPY_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            progress(len(chunk))
    shutil.copystat(src, dst)


_SEQUENCE = re.compile(r"%(0?)(\d*)d")


def sequence_frames(pattern_path: str) -> List[str]:
    """The files of an image sequence named like ``shot_%04d.png``, in frame order."""
    folder, pattern = os.path.split(pattern_path)
    m = _SEQUENCE.search(pattern)
    if not m:
        return [pattern_path] if os.path.isfile(pattern_path) else []
    prefix, suffix = pattern[:m.start()], pattern[m.end():]
    width = int(m.group(2) or 0)
    digits = r"\d{%d}" % width if m.group(1) and width else r"\d+"
    regex = re.compile(re.escape(prefix) + "(" + digits + ")" + re.escape(suffix) + "$")
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return []
    found = sorted(((int(mm.group(1)), n) for n in names for mm in [regex.match(n)] if mm), key=lambda x: x[0])
    return [os.path.join(folder, n) for _, n in found]


# ---------------------------------------------------------------------------
# Qt work (on a QThread via jobs.run_on_qthread)
# ---------------------------------------------------------------------------

def render_svg_png(svg_path: str, png_path: str, width: int, height: int) -> None:
    """Rasterize a title / wipe SVG to a transparent PNG with QSvgRenderer, as libopenshot does."""
    from classes.agent_tools.titles import _rasterize_svg
    from classes.handoff.jobs import run_on_qthread
    run_on_qthread(lambda: _rasterize_svg(svg_path, png_path, width, height), timeout_seconds=120)
    if not os.path.isfile(png_path):
        raise AeHandoffError(f"could not render {os.path.basename(svg_path)} to an image")


def analyze_mask_image(path: str) -> MaskInfo:
    """Grey level(s) libopenshot's Mask reads from an image ((11 r + 16 g + 5 b) >> 5, Mask.cpp)."""
    from classes.handoff.jobs import run_on_qthread

    def _scan() -> MaskInfo:
        from qt_api import QImage
        image = QImage(path)
        if image.isNull():
            raise AeHandoffError(f"could not read the wipe image {os.path.basename(path)}")
        width, height = image.width(), image.height()
        grays, alphas = set(), set()
        steps = 96
        for j in range(steps):
            y = min(height - 1, int((j + 0.5) * height / steps))
            for i in range(steps):
                x = min(width - 1, int((i + 0.5) * width / steps))
                c = image.pixelColor(x, y)
                a = c.alpha()
                # the Mask kernel reads premultiplied bytes: gray = (11 r + 16 g + 5 b) >> 5
                r, g, b = (c.red() * a) // 255, (c.green() * a) // 255, (c.blue() * a) // 255
                grays.add((r * 11 + g * 16 + b * 5) >> 5)
                alphas.add(a)
        uniform = max(grays) - min(grays) <= 1 and max(alphas) - min(alphas) <= 1
        return MaskInfo(uniform=uniform, gray=min(grays), alpha=min(alphas), width=width, height=height)

    return run_on_qthread(_scan, timeout_seconds=60)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _title_png_size(snapshot, f) -> Tuple[int, int]:
    """Comp-resolution PNG size for a title: the artboard fitted to the comp, sharper for zoomed clips."""
    aw = float(f.width or snapshot.width)
    ah = float(f.height or snapshot.height)
    zoom = 1.0
    for clip in snapshot.clips_of_file(f.id):
        for key in ("scale_x", "scale_y"):
            curve = clip.curves.get(key)
            if curve is not None and curve.points:
                zoom = max(zoom, max(p.value for p in curve.points))
    scale = max(1.0, min(snapshot.width / aw, snapshot.height / ah) * zoom)
    scale = min(scale, MAX_TITLE_PIXELS / max(aw, ah))
    return max(4, int(round(aw * scale))), max(4, int(round(ah * scale)))


def _readme(snapshot, result: ExportResult, export: AE.AeExport, generator: str, created: str) -> str:
    lines = [
        "Zenvi → After Effects export",
        "=" * 30,
        "",
        f"Project:  {snapshot.name}" + (f" ({snapshot.project_path})" if snapshot.project_path else " (unsaved)"),
        f"Exported: {created} by {generator}",
        f"Script:   {os.path.basename(result.script_path)}",
        ("Media:    copied into media/ next to the script" if result.collected
         else "Media:    imported from where it is on this computer"),
        "",
        "How to run it",
        "-------------",
        f"1. In After Effects choose File > Scripts > Run Script File... and pick {os.path.basename(result.script_path)}.",
        "2. Or from Zenvi: File > Send To > After Effects. This needs the Zenvi Link panel in After Effects",
        "   (Window > Extensions > Zenvi Link; install it with `zenvi adobe install`).",
        "3. On a Mac without Zenvi Link, Zenvi's export dialog can ask After Effects to run the script",
        "   through AppleScript (macOS asks once whether Zenvi may control After Effects).",
        "",
        f"The script adds a folder \"{AE.FOLDER_PREFIX}{snapshot.name}\" with the footage, titles and the comp",
        f"\"{snapshot.name}\" ({snapshot.width}x{snapshot.height}, {float(snapshot.fps):g} fps) and opens it. It is one undo step:",
        f"Edit > Undo \"{AE.UNDO_NAME}\" removes everything it made (when Zenvi runs it through Zenvi Link,",
        f"the step is called \"{ZENVI_LINK_UNDO}\"). Keep this folder where it is: After Effects reads the",
        "media, title images and wipe images from it.",
        "",
        f"Layers: {export.stats.get('layers', 0)}, footage items: {export.stats.get('footage', 0)}.",
    ]
    if result.titles:
        lines += ["", "Titles", "------"]
        for t in result.titles:
            mode = "editable text" if t.get("mode") == "native" else "image (PNG)"
            lines.append(f"- {t.get('title')}: {mode} - {t.get('detail')}")
    if result.missing:
        lines += ["", "Missing media (placeholders in After Effects; relink with File > Replace Footage > File)",
                  "-" * 40]
        lines += [f"- {m}" for m in result.missing]
    if result.warnings:
        lines += ["", "Notes", "-----"]
        lines += [f"- {w}" for w in result.warnings]
    lines += ["", "Font note: titles use the fonts installed in After Effects; a missing font is replaced and",
              "reported when the script runs.", ""]
    return "\n".join(lines)


def export_after_effects(snapshot, output_dir: str, *, collect_media: bool = True, include_audio: bool = True,
                         interactive: bool = True, progress: ProgressFn = None, should_cancel: CancelFn = None,
                         render_svg: Optional[RenderSvg] = None, analyze_mask: Optional[AnalyzeMask] = None,
                         generator: Optional[str] = None, created: Optional[str] = None,
                         script_name: Optional[str] = None) -> ExportResult:
    """Write the After Effects export of *snapshot* into *output_dir* (created if needed). Blocking."""
    from classes import info
    render_svg = render_svg or render_svg_png
    analyze_mask = analyze_mask or analyze_mask_image
    generator = generator or f"{info.PRODUCT_NAME} {info.VERSION}"
    created = created if created is not None else datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if not snapshot.clips:
        raise AeHandoffError("the timeline has no clips to export")
    folder = os.path.abspath(os.path.expanduser(output_dir))
    if os.path.exists(folder) and not os.path.isdir(folder):
        raise AeHandoffError(f"{folder} is a file; choose a folder for the After Effects export")
    try:
        os.makedirs(folder, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".zenvi-ae-", dir=folder)
    except OSError as exc:
        raise AeHandoffError(f"cannot write to {folder}: {exc.strerror or exc}") from None
    name = safe_name(script_name or snapshot.name)
    layout = _Layout(folder)
    staged: List[str] = []      # relative paths written into the staging folder
    try:
        media_map, media_rel, missing, copies = _plan_media(snapshot, layout, collect_media)
        total = sum(os.path.getsize(src) for src, _ in copies) or 1
        done = [0]

        def advance(n: int) -> None:
            done[0] += n
            _report(progress, 0.05 + 0.75 * done[0] / total, "Copying media")

        for src, rel in copies:
            _check_cancel(should_cancel)
            _report(progress, 0.05 + 0.75 * done[0] / total, f"Copying {os.path.basename(src)}")
            _copy(src, os.path.join(staging, rel), advance, should_cancel)
            staged.append(rel)
        notes: List[str] = []
        _report(progress, 0.82, "Preparing titles")
        title_assets = _prepare_titles(snapshot, layout, staging, staged, render_svg, should_cancel, notes)
        _report(progress, 0.88, "Preparing transitions")
        mask_assets = _prepare_masks(snapshot, layout, staging, staged, render_svg, analyze_mask, should_cancel,
                                     notes)
        _check_cancel(should_cancel)
        _report(progress, 0.92, "Writing the script")
        try:
            export = AE.build_ae_script(snapshot, media_map=media_map, title_assets=title_assets,
                                        mask_assets=mask_assets,
                                        options=AE.AeExportOptions(include_audio=include_audio,
                                                                   interactive=interactive, generator=generator,
                                                                   created=created))
        except AE.AeExportError as exc:
            raise AeHandoffError(str(exc)) from None
        result = ExportResult(script_path=os.path.join(folder, name + ".jsx"), folder=folder,
                              readme_path=os.path.join(folder, "README.txt"), collected=collect_media,
                              media=media_rel, missing=missing, titles=export.titles,
                              warnings=notes + list(export.warnings), stats=export.stats)
        with open(os.path.join(staging, name + ".jsx"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(export.jsx)
        staged.append(name + ".jsx")
        with open(os.path.join(staging, "README.txt"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_readme(snapshot, result, export, generator, created))
        staged.append("README.txt")
        _check_cancel(should_cancel)
        _report(progress, 0.97, "Moving the export into place")
        for rel in staged:
            dest = os.path.join(folder, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(os.path.join(staging, rel), dest)
        _report(progress, 1.0, "Done")
        return result
    except OSError as exc:
        raise AeHandoffError(f"the After Effects export could not be written to {folder}: "
                             f"{exc.strerror or exc}") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _plan_media(snapshot, layout: _Layout, collect: bool):
    media_map: Dict[str, AE.MediaRef] = {}
    rels: List[str] = []
    missing: List[str] = []
    copies: List[Tuple[str, str]] = []
    for f in snapshot.used_files():
        if f.is_title:
            continue
        path = f.path
        if f.is_image_sequence:
            frames = sequence_frames(path)
            if not frames:
                missing.append(path)
                media_map[f.id] = AE.MediaRef(abs=path, missing=True)
                continue
            if not collect:
                media_map[f.id] = AE.MediaRef(abs=frames[0])
                continue
            stem = safe_name(os.path.splitext(os.path.basename(path))[0].replace("%", ""), "sequence")
            first_rel = None
            for frame in frames:
                rel, present = layout.place(frame, "media/" + stem, os.path.basename(frame))
                first_rel = first_rel or rel
                if not present:
                    copies.append((frame, rel))
            rels.append("media/" + stem + "/")
            media_map[f.id] = AE.MediaRef(abs=os.path.join(layout.folder, first_rel or ""), rel=first_rel)
            continue
        if not path or not os.path.isfile(path):
            missing.append(path or f.name)
            media_map[f.id] = AE.MediaRef(abs=path, missing=True)
            continue
        if not collect:
            media_map[f.id] = AE.MediaRef(abs=path)
            continue
        rel, present = layout.place(path, "media", safe_media_name(os.path.basename(path)))
        if not present:
            copies.append((path, rel))
        rels.append(rel)
        media_map[f.id] = AE.MediaRef(abs=os.path.join(layout.folder, rel), rel=rel)
    return media_map, rels, missing, copies


def safe_media_name(name: str) -> str:
    stem, ext = os.path.splitext(name)
    return safe_name(stem, "media") + ext


def _stage_generated(layout: _Layout, staging: str, staged: List[str], produced: str, subdir: str,
                     name: str) -> Tuple[str, str]:
    """Claim a name for a file made in the staging area; (relative path, absolute final path)."""
    rel, present = layout.place(produced, subdir, name)
    final = os.path.join(layout.folder, rel)
    if not present:
        target = os.path.join(staging, rel)
        if os.path.abspath(target) != os.path.abspath(produced):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(produced, target)
        staged.append(rel)
    return rel, final


def _prepare_titles(snapshot, layout: _Layout, staging: str, staged: List[str], render_svg: RenderSvg,
                    should_cancel: CancelFn, notes: List[str]) -> Dict[str, AE.TitleAsset]:
    assets: Dict[str, AE.TitleAsset] = {}
    scratch = os.path.join(staging, ".render")
    for f in snapshot.used_files():
        if not f.is_title:
            continue
        _check_cancel(should_cancel)
        try:
            with open(f.path, "r", encoding="utf-8") as fh:
                svg = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("title %s unreadable for the AE export: %s", f.path, exc)
            notes.append(f"Title {os.path.splitext(f.name)[0]} could not be read ({f.path}); it is left out")
            continue
        try:
            assets[f.id] = AE.TitleAsset(mode="native", layout=parse_title_svg(svg))
            continue
        except NotNative as exc:
            reason = str(exc)
        width, height = _title_png_size(snapshot, f)
        os.makedirs(scratch, exist_ok=True)
        produced = os.path.join(scratch, safe_name(os.path.splitext(f.name)[0], "Title") + ".png")
        try:
            render_svg(f.path, produced, width, height)
        except AeCancelled:
            raise
        except Exception as exc:  # a broken title must not stop the rest of the export
            log.warning("title %s could not be rendered for After Effects", f.path, exc_info=True)
            notes.append(f"Title {os.path.splitext(f.name)[0]} could not be rendered ({exc}); it is left out")
            continue
        rel, final = _stage_generated(layout, staging, staged, produced, "titles", os.path.basename(produced))
        assets[f.id] = AE.TitleAsset(mode="png", reason=reason, image=AE.MediaRef(abs=final, rel=rel), width=width,
                                     height=height)
    shutil.rmtree(scratch, ignore_errors=True)
    return assets


def _prepare_masks(snapshot, layout: _Layout, staging: str, staged: List[str], render_svg: RenderSvg,
                   analyze_mask: AnalyzeMask, should_cancel: CancelFn, notes: List[str]) -> Dict[str, AE.MaskAsset]:
    assets: Dict[str, AE.MaskAsset] = {}
    scratch = os.path.join(staging, ".render")
    for tv in snapshot.transitions:
        path = tv.mask_path
        if not path or path in assets:
            continue
        _check_cancel(should_cancel)
        if not os.path.isfile(path):
            assets[path] = AE.MaskAsset("missing")
            continue
        image = path
        try:
            if path.lower().endswith((".svg", ".svgz")):
                os.makedirs(scratch, exist_ok=True)
                image = os.path.join(scratch, safe_name(os.path.splitext(os.path.basename(path))[0], "wipe") + ".png")
                render_svg(path, image, int(snapshot.width), int(snapshot.height))
            info = analyze_mask(image)
        except AeCancelled:
            raise
        except Exception as exc:
            log.warning("wipe image %s could not be prepared for After Effects", path, exc_info=True)
            notes.append(f"Wipe image {os.path.basename(path)} could not be read ({exc})")
            assets[path] = AE.MaskAsset("missing")
            continue
        if info.uniform:
            assets[path] = AE.MaskAsset("uniform", gray=info.gray, alpha=info.alpha)
            continue
        name = os.path.basename(image)
        if image == path:
            rel, present = layout.place(path, "masks", safe_media_name(name))
            if not present:
                target = os.path.join(staging, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(path, target)
                staged.append(rel)
            final = os.path.join(layout.folder, rel)
        else:
            rel, final = _stage_generated(layout, staging, staged, image, "masks", safe_media_name(name))
        assets[path] = AE.MaskAsset("image", image=AE.MediaRef(abs=final, rel=rel), width=info.width,
                                    height=info.height)
    shutil.rmtree(scratch, ignore_errors=True)
    return assets


# ---------------------------------------------------------------------------
# Running the script in After Effects
# ---------------------------------------------------------------------------

def parse_import_summary(host_result) -> Optional[dict]:
    """The JSON summary the export script returns, wherever Zenvi Link put it in its receipt."""
    receipt = getattr(host_result, "receipt", None) or {}
    candidates: List[Any] = []
    data = receipt.get("data")
    if isinstance(data, dict):
        if data.get("zenvi_ae_import"):
            return data
        for key in ("result", "value", "output", "return", "returned", "summary"):
            candidates.append(data.get(key))
    else:
        candidates.append(data)
    candidates += [receipt.get("summary"), getattr(host_result, "text", None)]
    for c in candidates:
        if isinstance(c, dict) and c.get("zenvi_ae_import"):
            return c
        if isinstance(c, str) and "zenvi_ae_import" in c:
            start = c.find("{")
            try:
                parsed = json.loads(c[start:]) if start >= 0 else None
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("zenvi_ae_import"):
                return parsed
    return None


QUIET_FLAG = "ZENVI_AE_QUIET"


def quiet_runner(script_path: str) -> str:
    """A small script next to *script_path* that runs it with its closing alert off; returns its path.

    For an export written for people (``interactive``) that Zenvi itself
    runs: an alert would hold Zenvi Link's call or AppleScript's
    DoScriptFile until someone clicks it. The flag lives only for the run
    (the export runtime's ``quietRun``). Delete the runner afterwards.
    """
    target = os.path.abspath(script_path)
    runner = os.path.join(os.path.dirname(target), ".zenvi-run-%s.jsx" % uuid.uuid4().hex[:12])
    body = "\n".join([
        "// Zenvi: runs the export next to this file without its closing alert (removed after the run)",
        "(function () {",
        "    $.global.%s = true;" % QUIET_FLAG,
        "    try {",
        # File() reads %XX as an escape
        "        return $.evalFile(new File(%s));" % js_str(target.replace("%", "%25")),
        "    } finally {",
        "        delete $.global.%s;" % QUIET_FLAG,
        "    }",
        "}());",
        ""])
    with open(runner, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return runner


def _drop_runner(runner: Optional[str]) -> None:
    if runner:
        try:
            os.remove(runner)
        except OSError:
            log.debug("could not remove %s", runner, exc_info=True)


def run_in_after_effects(script_path: str, *, quiet: bool = False, timeout: float = AE_RUN_TIMEOUT,
                         base_dir: Optional[str] = None) -> Tuple[Optional[dict], dict]:
    """Run *script_path* in the connected After Effects (Zenvi Link ``ae_run_jsx_file``). Blocking.

    *quiet*: run it through ``quiet_runner`` (for an interactive export).
    Raises ``adobe_link.HostNotConnected`` (with how to connect) or
    AeHandoffError when After Effects reports a failure.
    """
    from classes.handoff import adobe_link
    runner = quiet_runner(script_path) if quiet else None
    try:
        result = adobe_link.call_host_tool(AE_APP, RUN_TOOL, {"path": runner or script_path}, timeout=timeout,
                                           base_dir=base_dir)
    except adobe_link.HostNotConnected:
        _drop_runner(runner)
        raise
    # (after a timeout or a dropped connection the runner stays: After Effects may still read it)
    _drop_runner(runner)
    summary = parse_import_summary(result)
    if result.is_error:
        raise AeHandoffError("After Effects could not run the script: " + str(result.receipt.get("summary") or
                                                                              result.text or "no details"))
    if summary is not None and summary.get("status") == "error":
        raise AeHandoffError(str(summary.get("summary") or "After Effects stopped the import"))
    return summary, result.receipt


def send_to_after_effects(snapshot, *, collect_media: bool = False, include_audio: bool = True,
                          progress: ProgressFn = None, should_cancel: CancelFn = None,
                          timeout: float = AE_RUN_TIMEOUT, base_dir: Optional[str] = None,
                          render_svg: Optional[RenderSvg] = None, analyze_mask: Optional[AnalyzeMask] = None,
                          output_dir: Optional[str] = None) -> SendResult:
    """Export *snapshot* and build it in the connected After Effects. Blocking."""
    from classes.handoff import adobe_link
    _report(progress, 0.0, "Looking for After Effects")
    host = adobe_link.get_host(AE_APP, base_dir)
    if not host.connected:
        raise adobe_link.HostNotConnected(f"After Effects is not connected ({host.reason}). "
                                          f"{adobe_link.connect_hint(AE_APP)}")
    folder = output_dir or send_folder(snapshot.project_path, snapshot.name)

    def scaled(fraction: Optional[float], message: str) -> None:
        _report(progress, None if fraction is None else fraction * 0.6, message)

    export = export_after_effects(snapshot, folder, collect_media=collect_media, include_audio=include_audio,
                                  interactive=False, progress=scaled, should_cancel=should_cancel,
                                  render_svg=render_svg, analyze_mask=analyze_mask)
    _check_cancel(should_cancel)
    _report(progress, 0.65, "Building the comp in After Effects")
    summary, receipt = run_in_after_effects(export.script_path, timeout=timeout, base_dir=base_dir)
    _report(progress, 1.0, "Done")
    return SendResult(export=export, summary=summary, host_receipt=receipt)


# ---------------------------------------------------------------------------
# macOS fallback: AppleScript DoScriptFile
# ---------------------------------------------------------------------------

def _app_rank(path: str) -> Tuple[int, int, str]:
    name = os.path.basename(path)
    years = [int(y) for y in re.findall(r"(?:19|20)\d\d", name)]
    beta = 1 if "beta" in name.lower() else 0
    return (max(years) if years else 0, -beta, name)


_RUNNING_APP_RE = re.compile(r"^(/.*?/Adobe After Effects[^/]*\.app)/Contents/MacOS/")


def running_after_effects_apps() -> List[str]:
    """The ``.app`` bundles of the After Effects processes running now (``ps``: no Automation prompt)."""
    try:
        proc = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    apps: List[str] = []
    for line in (proc.stdout or "").splitlines():
        match = _RUNNING_APP_RE.match(line.strip())
        if match and match.group(1) not in apps:
            apps.append(match.group(1))
    return apps


def find_after_effects_app(applications: Sequence[str] = ("/Applications",), platform: Optional[str] = None,
                           running: Optional[Callable[[], List[str]]] = None) -> Optional[str]:
    """The After Effects to ask on macOS: the running one (the newest, if several), else the newest installed.

    Asking an installed release that is not running would start it and
    build the comp in an empty project beside the one the user has open.
    """
    if (platform or sys.platform) != "darwin":
        return None
    live = [p for p in (running or running_after_effects_apps)() if os.path.isdir(p)]
    if live:
        return max(live, key=_app_rank)
    found: List[str] = []
    for root in applications:
        found += glob.glob(os.path.join(root, "Adobe After Effects*", "Adobe After Effects*.app"))
    found = [p for p in found if os.path.isdir(p)]
    return max(found, key=_app_rank) if found else None


def applescript_command(app_path: str, script_path: str, timeout: float = AE_RUN_TIMEOUT) -> List[str]:
    """``osascript`` argv running *script_path* in the After Effects at *app_path* (DoScriptFile).

    The script path travels as an argument, never inside the AppleScript
    source, so no quoting can break it; DoScriptFile gets a file, not a
    string.
    """
    app_name = os.path.splitext(os.path.basename(app_path))[0].replace("\\", "\\\\").replace('"', '\\"')
    lines = ["on run argv", "set f to POSIX file (item 1 of argv)", f'tell application "{app_name}"', "activate",
             f"with timeout of {int(timeout)} seconds", "DoScriptFile f", "end timeout", "end tell", "end run"]
    argv = ["osascript"]
    for line in lines:
        argv += ["-e", line]
    argv.append(script_path)
    return argv


def _duration(seconds: float) -> str:
    if seconds >= 120:
        return f"{int(round(seconds / 60.0))} minutes"
    whole = max(1, int(round(seconds)))
    return f"{whole} second{'' if whole == 1 else 's'}"


def run_with_applescript(script_path: str, app_path: str, *, quiet: bool = False, timeout: float = AE_RUN_TIMEOUT,
                         should_cancel: CancelFn = None, poll: float = 0.25) -> Optional[dict]:
    """Run the export in After Effects through AppleScript (macOS). Blocking; returns the script's summary.

    Waits at most *timeout* seconds and gives up early when *should_cancel*
    says so (After Effects may still finish the comp: Zenvi only stops
    waiting). *quiet*: run it through ``quiet_runner`` (for an interactive
    export, whose alert would hold DoScriptFile).
    """
    name = os.path.basename(script_path)
    runner = quiet_runner(script_path) if quiet else None
    finished = False
    try:
        try:
            proc = subprocess.Popen(applescript_command(app_path, runner or script_path, timeout),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True)
        except FileNotFoundError:
            raise AeHandoffError("osascript is not available on this Mac") from None
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=poll)
                break
            except subprocess.TimeoutExpired:
                if should_cancel is not None and should_cancel():
                    proc.kill()
                    proc.communicate()
                    raise AeCancelled("Zenvi stopped waiting for After Effects; it may still finish building the "
                                      "comp") from None
                if time.monotonic() >= deadline:
                    proc.kill()
                    proc.communicate()
                    raise AeHandoffError(
                        f"After Effects did not report back within {_duration(timeout)}. It may "
                        f"still be building the comp, or DoScriptFile is stuck (a known After Effects 2024 "
                        f"problem): check After Effects, or run {name} there with File > Scripts > Run Script "
                        f"File.") from None
        finished = True
    finally:
        if finished:
            _drop_runner(runner)
    err = (err or "").strip()
    if proc.returncode != 0:
        if "-1743" in err or "not authorized" in err.lower() or "not allowed" in err.lower():
            raise AeHandoffError("macOS did not let Zenvi control After Effects. Allow it in System Settings > "
                                 "Privacy & Security > Automation, then try again.")
        if "-1712" in err:
            raise AeHandoffError(f"After Effects did not answer in time (DoScriptFile may be stuck): check After "
                                 f"Effects, or run {name} there with File > Scripts > Run Script File.")
        raise AeHandoffError("After Effects could not run the script: " + (err[-400:] or f"osascript exit {proc.returncode}"))
    out = (out or "").strip()
    start = out.find("{")
    if start >= 0:
        try:
            parsed = json.loads(out[start:])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return None


def reveal(path: str) -> None:
    """Show *path* in Finder / Explorer / the file manager (spawns and returns at once)."""
    path = os.path.abspath(path)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select," + path])
        else:
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            subprocess.Popen(["xdg-open", folder], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise AeHandoffError(f"could not open the folder: {exc.strerror or exc}") from None


__all__ = [
    "AeHandoffError", "AeCancelled", "ExportResult", "SendResult", "MaskInfo", "export_after_effects",
    "send_to_after_effects", "run_in_after_effects", "parse_import_summary", "find_after_effects_app",
    "running_after_effects_apps", "applescript_command", "run_with_applescript", "quiet_runner", "reveal",
    "default_output_dir", "send_folder", "sequence_frames", "render_svg_png", "analyze_mask_image",
    "undo_hint", "describe_import", "AE_RUN_TIMEOUT", "RUN_TOOL", "ZENVI_LINK_UNDO", "QUIET_FLAG",
]
