"""HyperFrames on this computer: motion graphics, product demos and captions.

Workstream: ai-generation. The assistant authors an HTML composition in a
project folder under ``~/.openshot_qt/hyperframes/projects``, checks it with the
HyperFrames CLI, renders it here and imports the result from disk. Nothing is
uploaded and no render server is involved (``classes.hyperframes_local`` owns
the install and the list of what may run).

Threading: every tool does its disk and subprocess work on the calling worker
thread; only the import and the metadata stamp hop to the GUI thread.
"""

from __future__ import annotations

import json
import os
import time

from classes import hyperframes_local as hf
from classes.editor_tools._base import (
    ToolError,
    array,
    boolean,
    enum,
    integer,
    obj,
    ok,
    on_main,
    string,
    th,
)
from classes.editor_tools._registry import editor_tool

MAX_READ_LINES = 400
MAX_WRITE_BYTES = 2 * 1024 * 1024
_PROJECT = string("Composition project name: short, lowercase, a-z 0-9 - _ (e.g. 'intro-title'). One folder per "
                  "graphic; reuse the name to keep editing the same one.")


def _guard(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except hf.HyperframesError as exc:
        raise ToolError(str(exc))


def _project_media() -> list:
    """Absolute paths of the project's media, the only files outside the folder a command may read."""
    from classes.query import File

    paths = []
    for f in File.filter():
        try:
            paths.append(f.absolute_path())
        except Exception:
            continue
    return [p for p in paths if p]


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def _ask_install_consent(steps) -> bool:
    def _ask():
        from PyQt5.QtWidgets import QMessageBox

        from classes.app import get_app

        _ = get_app()._tr
        lines = "\n".join("  • " + s["label"] for s in steps)
        answer = QMessageBox.question(
            get_app().window, _("Set up motion graphics"),
            _("Zenvi renders motion graphics, product demos and captions on this computer with HyperFrames. "
              "This needs a one-time install:") + "\n\n" + lines + "\n\n" + _("Install now?"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        return answer == QMessageBox.Yes

    return bool(on_main(_ask, timeout=600))


def _status_line(st: dict) -> str:
    parts = ["Node.js " + ("found" if st["node"] else "MISSING"),
             "HyperFrames CLI " + (st["cli"] or "MISSING"),
             "Chrome " + ("ready" if st["browser"] else "MISSING"),
             "%d skills" % st["skills"],
             "ffmpeg " + ("found" if st["ffmpeg"] else "MISSING"),
             "bash " + ("found" if st["bash"] else "missing (skill .sh scripts unavailable)"),
             "caption helpers " + ("installed" if st["caption_helpers"] else "not installed")]
    return "; ".join(parts)


@editor_tool(
    "hyperframes_setup_tool",
    label="Set up motion graphics",
    schema=obj({
        "install": boolean("true installs whatever is missing (the user is asked to confirm first). "
                           "false only reports.", False),
        "captions": boolean("Also install the caption preview helpers the embedded-captions skill needs.", False),
    }),
    background_safe=True,
    covers=("ai.hyperframes",),
)
def hyperframes_setup(install=False, captions=False):
    """Check, and with install=true install, HyperFrames on this computer (Node.js, the CLI, Chrome, the skills).

    Call it first whenever another hyperframes tool says HyperFrames is not set
    up. The install is one-time and can take a few minutes; the paths are saved
    so later sessions start instantly. The user sees a confirmation dialog
    before anything is installed -- if they decline, stop and tell them motion
    graphics need it.
    """
    st = hf.status()
    steps = hf.install_plan(st, captions=bool(captions))
    if not steps:
        return ok("HyperFrames is ready. " + _status_line(st), **st)
    if not install:
        return ok("HyperFrames is NOT ready: " + _status_line(st) + ". Call hyperframes_setup_tool with "
                  "install=true to install: " + "; ".join(s["label"] for s in steps) + ".", **st)
    if not _ask_install_consent(steps):
        raise ToolError("the user declined the HyperFrames install; motion graphics, product demos and "
                        "HyperFrames captions are unavailable until it is installed")
    st = _guard(hf.install, captions=bool(captions))
    left = hf.install_plan(st, captions=bool(captions))
    if left:
        raise ToolError("setup finished but is still missing: " + "; ".join(s["label"] for s in left))
    return ok("HyperFrames installed and ready. " + _status_line(st), **st)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@editor_tool(
    "hyperframes_read_file_tool",
    label="Read composition file",
    schema=obj({
        "project": _PROJECT,
        "path": string("File to read, relative to the project folder, or under skills/ for an installed "
                       "skill (e.g. skills/motion-graphics/SKILL.md)."),
        "offset": integer("First line to return (1-based).", 1, minimum=1),
        "limit": integer("How many lines to return.", MAX_READ_LINES, minimum=1, maximum=2000),
    }, required=["project", "path"]),
    read_only=True,
    covers=("ai.hyperframes",),
)
def hyperframes_read_file(project, path, offset=1, limit=MAX_READ_LINES):
    """Read a text file from a HyperFrames project, or from the installed skills, with line numbers.

    Read skills/hyperframes/SKILL.md first for any new motion graphic, product
    demo or caption request: it routes to the workflow skill to follow. Long
    files come back in pages; pass offset to continue.
    """
    full = _guard(hf.resolve_path, project, path)
    if not os.path.isfile(full):
        raise ToolError("no such file: %s (hyperframes_list_files_tool shows what exists)" % path)
    with open(full, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    first = int(offset)
    chunk = lines[first - 1:first - 1 + int(limit)]
    body = "\n".join("%5d\t%s" % (first + i, line) for i, line in enumerate(chunk))
    more = len(lines) - (first - 1 + len(chunk))
    return body + ("\n[%d more lines; continue with offset=%d]" % (more, first + len(chunk)) if more > 0 else "")


@editor_tool(
    "hyperframes_write_file_tool",
    label="Write composition file",
    schema=obj({
        "project": _PROJECT,
        "path": string("File to create or replace, relative to the project folder (e.g. index.html)."),
        "content": string("The whole file content."),
    }, required=["project", "path", "content"]),
    background_safe=True,
    covers=("ai.hyperframes",),
)
def hyperframes_write_file(project, path, content):
    """Create or replace a text file in a HyperFrames project (HTML composition, JSON plan, CSS).

    For a small change to an existing file use hyperframes_edit_file_tool instead
    of rewriting it. The installed skills are read-only.
    """
    full = _guard(hf.resolve_path, project, path, writable=True)
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError("content is %d bytes; the limit is %d" % (len(data), MAX_WRITE_BYTES))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)
    return ok("Wrote %s (%d bytes)." % (path, len(data)), project=project, path=path, bytes=len(data))


@editor_tool(
    "hyperframes_edit_file_tool",
    label="Edit composition file",
    schema=obj({
        "project": _PROJECT,
        "path": string("File to edit, relative to the project folder."),
        "old_text": string("Exact text to replace. Must occur once unless replace_all is true."),
        "new_text": string("Replacement text (empty deletes old_text).", ""),
        "replace_all": boolean("Replace every occurrence.", False),
    }, required=["project", "path", "old_text"]),
    background_safe=True,
    covers=("ai.hyperframes",),
)
def hyperframes_edit_file(project, path, old_text, new_text="", replace_all=False):
    """Replace an exact piece of text in a HyperFrames project file.

    Refused, with the file untouched, when old_text is missing or ambiguous.
    """
    if not old_text:
        raise ToolError("old_text must not be empty")
    full = _guard(hf.resolve_path, project, path, writable=True)
    if not os.path.isfile(full):
        raise ToolError("no such file: %s" % path)
    with open(full, encoding="utf-8") as fh:
        text = fh.read()
    count = text.count(old_text)
    if count == 0:
        raise ToolError("old_text was not found in %s; read the file and copy the text exactly" % path)
    if count > 1 and not replace_all:
        raise ToolError("old_text occurs %d times in %s; add surrounding text or pass replace_all=true"
                        % (count, path))
    with open(full, "w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(old_text, new_text))
    return ok("Edited %s (%d replacement%s)." % (path, count, "" if count == 1 else "s"),
              project=project, path=path, replacements=count)


@editor_tool(
    "hyperframes_list_files_tool",
    label="List composition files",
    schema=obj({
        "project": _PROJECT,
        "pattern": string("Glob relative to the project folder ('**/*.html'), or under skills/ "
                          "('skills/*/SKILL.md' lists the installed skills).", "**/*"),
    }, required=["project"]),
    read_only=True,
    covers=("ai.hyperframes",),
)
def hyperframes_list_files(project, pattern="**/*"):
    """List files in a HyperFrames project, or the installed skills and their references."""
    files = _guard(hf.list_files, project, pattern)
    return "\n".join(files) if files else "(no files match %s)" % pattern


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

@editor_tool(
    "hyperframes_run_tool",
    label="Run HyperFrames",
    schema=obj({
        "project": _PROJECT,
        "program": enum(hf.PROGRAMS, "hyperframes = the CLI; node / bash = a script shipped in an installed "
                                     "skill; ffmpeg / ffprobe = media probes and frame grabs."),
        "args": array(string("One argument."), "Arguments, one per item (no shell: no pipes, &&, quotes or "
                                               "redirects). For node/bash the first is the skill script, e.g. "
                                               "skills/embedded-captions/scripts/prepare.sh."),
        "timeout_seconds": integer("Give up after this long.", hf.DEFAULT_TIMEOUT, minimum=5, maximum=3600),
    }, required=["project", "program", "args"]),
    background_safe=True,
    covers=("ai.hyperframes",),
)
def hyperframes_run(project, program, args, timeout_seconds=hf.DEFAULT_TIMEOUT):
    """Run the HyperFrames CLI, a skill script, ffmpeg or ffprobe inside a project folder on this computer.

    The working directory is the project folder. hyperframes subcommands:
    init, add, catalog, capture, lint, check, validate, inspect, snapshot,
    render, docs, info, compositions, beats, keyframes, transcribe,
    remove-background, doctor (uploading and account commands are not
    available). Path arguments must stay inside the project; media in Project
    Files may be read by its absolute path, and a URL argument is accepted only
    by 'hyperframes capture <https url>'. (A composition's own HTML still loads
    what it references, e.g. GSAP or fonts from a CDN, when Chrome renders it.)
    To put a render on the timeline use
    render_motion_graphic_tool or import_motion_graphic_tool, not this tool.

    Example: {"project": "intro-title", "program": "hyperframes", "args": ["lint", ".", "--json"]}.
    """
    code, out = _guard(hf.run, project, program, list(args), timeout=int(timeout_seconds),
                       inputs=_project_media())
    if code != 0:
        raise ToolError("%s %s failed (exit code %d):\n%s" % (program, args[0] if args else "", code, out))
    return out or "(done, no output)"


# ---------------------------------------------------------------------------
# Render and import
# ---------------------------------------------------------------------------

def _import(project, full, label, transparent):
    if not os.path.isfile(full) or os.path.getsize(full) <= 0:
        raise ToolError("the rendered file is missing or empty: %s" % os.path.basename(full))
    handlers = th()
    # With preserve_alpha the import re-encodes, probes the decoded pixels and fails closed
    # BEFORE adding anything to the project, so a render without alpha leaves no file behind.
    f, err = handlers._import_generated_video(full, preserve_alpha=bool(transparent))
    if err or not f:
        hint = ("; make the composition's background transparent, or pass transparent=false for a "
                "full-frame card" if transparent else "")
        raise ToolError("import failed: %s%s" % (err or "no project file was created", hint))
    on_main(handlers._stamp_motion_graphics_file_metadata, f, label, bool(transparent))
    where = ("a transparent overlay: place it with place_motion_graphic_tool mode=overlay on a track above "
             "the footage" if transparent else
             "an opaque clip: place it with place_motion_graphic_tool mode=gap or cut_in")
    return ok("Imported '%s' into Project Files as %s." % (label, where), file_id=f.id, project=project,
              transparent=bool(transparent), path=f.absolute_path(),
              size_mb=round(os.path.getsize(full) / 1048576.0, 2))


def _check_problems(project) -> list:
    """What 'hyperframes check' says must be fixed before a render is worth making.

    Errors from any pass, plus anything painted outside the canvas: the model
    cannot see its frame, and an off-canvas lower third renders "successfully".
    Best effort -- a check that cannot run or parse does not block the render.
    """
    try:
        _code, out = hf.run(project, "hyperframes", ["check", ".", "--json"], timeout=600)
        report, _end = json.JSONDecoder().raw_decode(out[out.index("{"):])
    except (hf.HyperframesError, ValueError):
        return []
    problems = []
    for section in report.values() if isinstance(report, dict) else ():
        for finding in (section.get("findings") or []) if isinstance(section, dict) else ():
            off_canvas = finding.get("overflow")
            if finding.get("severity") != "error" and not off_canvas:
                continue
            where = " ".join(str(finding[k]) for k in ("sourceFile", "selector") if finding.get(k))
            detail = "off canvas by %s px" % json.dumps(off_canvas) if off_canvas else ""
            problems.append("; ".join(filter(None, (
                finding.get("code") or "error", where, "t=%ss" % finding["time"] if "time" in finding else "",
                detail, str(finding.get("message") or ""), str(finding.get("fixHint") or "")))))
    return problems[:12]


_LABEL = string("What the graphic is, in a few words; it becomes the clip's name and searchable summary "
                "(e.g. 'Tokyo Day 1 lower third').")
_TRANSPARENT = boolean("true = transparent overlay to sit above footage (WebM with alpha). false = opaque "
                       "full-frame clip (MP4).", True)


@editor_tool(
    "render_motion_graphic_tool",
    label="Render motion graphic",
    schema=obj({
        "project": _PROJECT,
        "label": _LABEL,
        "transparent": _TRANSPARENT,
        "composition": string("Composition file to render instead of index.html (e.g. compositions/intro.html).",
                              ""),
        "fps": integer("Frame rate.", 30, minimum=1, maximum=120),
        "quality": enum(["draft", "standard", "high"], "Render quality.", "standard"),
    }, required=["project", "label"]),
    background_safe=True,
    covers=("ai.hyperframes",),
)
def render_motion_graphic(project, label, transparent=True, composition="", fps=30, quality="standard"):
    """Render a HyperFrames project on this computer and import the video into Project Files.

    It first runs 'hyperframes check' and refuses to render while that reports
    an error or anything painted outside the canvas -- you cannot see the frame,
    so fix what it lists and call again (an intentional off-canvas entrance is
    marked with data-layout-allow-overflow). The render takes from a few seconds
    to a few minutes. The result carries a file_id: place it next with
    place_motion_graphic_tool. Nothing is uploaded.
    """
    root = _guard(hf.project_dir, project)
    entry = _guard(hf.resolve_path, project, composition or "index.html")
    if not os.path.isfile(entry):
        raise ToolError("%s has no %s; write the composition first" % (project, composition or "index.html"))
    problems = _check_problems(project)
    if problems:
        raise ToolError("not rendered: 'hyperframes check' found %d problem(s) to fix first:\n- %s"
                        % (len(problems), "\n- ".join(problems)))
    fmt = "webm" if transparent else "mp4"
    out_rel = "renders/%s-%d.%s" % (project, time.time_ns() // 1000000, fmt)
    args = ["render", ".", "--format", fmt, "-o", out_rel, "--fps", str(int(fps)), "--quality", quality, "--quiet"]
    if composition:
        args[2:2] = ["-c", composition]
    code, out = _guard(hf.run, project, "hyperframes", args, timeout=1800)
    if code != 0:
        raise ToolError("the render failed (exit code %d):\n%s" % (code, out))
    return _import(project, os.path.join(root, out_rel.replace("/", os.sep)), label, transparent)


@editor_tool(
    "import_motion_graphic_tool",
    label="Import motion graphic",
    schema=obj({
        "project": _PROJECT,
        "path": string("Rendered video inside the project folder (e.g. final.mp4)."),
        "label": _LABEL,
        "transparent": _TRANSPARENT,
    }, required=["project", "path", "label"]),
    background_safe=True,
    covers=("ai.hyperframes",),
)
def import_motion_graphic(project, path, label, transparent=True):
    """Import a video a skill script already rendered in a HyperFrames project into Project Files.

    For outputs that hyperframes_run_tool produced (a captions skill's
    final.mp4, a demo's composite). For a plain composition use
    render_motion_graphic_tool, which renders and imports in one call.
    """
    return _import(project, _guard(hf.resolve_path, project, path), label, transparent)
