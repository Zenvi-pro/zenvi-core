"""Workstream: handoff-hyperframes -- HyperFrames <-> Zenvi (import a project, export the timeline).

The work lives in ``classes.handoff.hyperframes``; these tools validate,
read the project settings / snapshot on the GUI thread, then run on the
calling worker thread under a ``handoff.jobs`` job (the toolbar pill shows
progress and offers Cancel). Renders and file copies never touch the GUI
thread; the import's clips land in ONE GUI hop (one undo step).
"""

from __future__ import annotations

import os

from classes.editor_tools._base import ToolError, boolean, enum, nullable, number, obj, ok, string
from classes.editor_tools._registry import editor_tool

MODES = ["auto", "native", "flatten"]
LINT_TIMEOUT = 60.0


def _project_settings():
    from classes.editor_tools._base import get_app
    p = get_app().project
    fps = p.get("fps") or {"num": 30, "den": 1}
    try:
        rate = float(fps.get("num") or 30) / float(fps.get("den") or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        rate = 30.0
    return rate, (int(p.get("width") or 1920), int(p.get("height") or 1080))


def _hf_dir(project_dir: str) -> str:
    path = os.path.abspath(os.path.expanduser(str(project_dir or "").strip()))
    if not str(project_dir or "").strip():
        raise ToolError("say which HyperFrames project: project_dir (the folder with index.html)")
    if not os.path.isdir(path):
        raise ToolError(f"{path} is not a folder")
    if not os.path.isfile(os.path.join(path, "index.html")):
        raise ToolError(f"{path} has no index.html, so it is not a HyperFrames project (create one with "
                        "`npx hyperframes init`)")
    return path


@editor_tool(
    "import_hyperframes_project_tool",
    label="Import HyperFrames project",
    schema=obj({
        "project_dir": string("The HyperFrames project folder (the one holding index.html)."),
        "mode": enum(MODES, "auto (default) = native, but the whole project as one linked clip when the root has "
                     "no media clips or a clip has animation Zenvi cannot rebuild exactly; native = <video>/<img>/"
                     "<audio> as editable Zenvi clips (CSS placement + GSAP tweens as keyframes), compositions and "
                     "scripted graphics as linked clips; flatten = the whole project as one linked clip. A project "
                     "Zenvi exported is always restored natively unless mode='flatten'.", "auto"),
        "position": nullable(number("Timeline second where the project's 0 lands. Empty = the playhead.",
                                    minimum=0)),
        "track": string("UI track number (1 = bottom), name or layer for the import's first track; its other "
                        "tracks go on the tracks above it (they must be free). Empty = new tracks on top (an empty "
                        "timeline reuses its tracks).", ""),
    }, required=["project_dir"]),
    background_safe=True,
    covers=("handoff.hyperframes_import",),
)
def import_hyperframes_project(project_dir, mode="auto", position=None, track=""):
    """Bring a HyperFrames (HTML video) project into the timeline as one undo step: media as editable clips, compositions as linked clips.

    Use for "open this HyperFrames project in Zenvi", "import my hyperframes
    folder", "bring the HyperFrames video back". Zenvi reads HyperFrames' own
    resolved timeline (`hyperframes timeline --json`), then: <video>/<img>/
    <audio> primitives become native clips on tracks from data-track-index
    (CSS position and size, GSAP opacity/x/y/scale/rotation tweens as
    keyframes, data-volume and fades as volume keyframes); each nested
    composition (data-composition-src or inline) becomes a linked clip
    rendered on its own with transparency; titles and other scripted DOM of
    the root become one linked graphics layer. Linked clips keep their
    variables as props (update_linked_clip_tool), open their code or the
    HyperFrames Studio (open_linked_source_tool) and re-render when the code
    changes. A Zenvi export comes back natively and losslessly, with timing
    edits made in HyperFrames applied. Renders take seconds to minutes; a
    failed or cancelled render adds nothing. Needs Node.js 22+ for renders.
    The receipt lists every clip with its kind (native / linked / restored),
    the mode used and why, and what could not be rebuilt exactly.
    Example: {"project_dir": "/Users/me/videos/launch", "mode": "auto"}.
    """
    from classes.editor_tools.titles_text_common import precheck_on_main
    from classes.handoff import jobs
    from classes.handoff.hyperframes import cli as hf_cli
    from classes.handoff.hyperframes import importer
    from classes.handoff.linked_media import LinkError
    path = _hf_dir(project_dir)
    settings = precheck_on_main(_project_settings)
    if not settings:
        raise ToolError("the editor did not report the project settings; try again")
    fps, canvas = settings
    try:
        with jobs.track_job("Importing HyperFrames project %s" % os.path.basename(path), kind="hyperframes") as job:
            insp = importer.inspect_project(path, mode, fps=fps, canvas=canvas, should_cancel=job.should_cancel)
            receipt = importer.run_import(insp, position=position, track=str(track or ""),
                                          on_progress=job.report, should_cancel=job.should_cancel)
    except jobs.JobCancelled:
        raise ToolError("the HyperFrames import was cancelled; nothing changed") from None
    except (LinkError, hf_cli.CliError) as exc:
        raise ToolError(str(exc)) from None
    summary = insp.summary()
    receipt.update({"project": {k: summary[k] for k in ("project_dir", "composition", "width", "height", "duration",
                                                        "timing_from", "hyperframes_cli", "requested_mode")},
                    "problems": insp.problems})
    parts = []
    for kind, label in (("native", "native"), ("linked", "linked"), ("restored", "restored")):
        if receipt.get(kind):
            parts.append("%d %s" % (receipt[kind], label))
    return ok("Imported HyperFrames project %s (%s mode): %s clip(s) at %.2fs%s." % (
        os.path.basename(path), receipt["mode"], ", ".join(parts) or "no", receipt["position"],
        "; %d warning(s)" % len(receipt["warnings"]) if receipt["warnings"] else ""), **receipt)


@editor_tool(
    "export_to_hyperframes_tool",
    label="Export to HyperFrames",
    schema=obj({
        "output_dir": string("A new or empty folder for the HyperFrames project (an earlier Zenvi export there is "
                             "updated in place; other files in it are kept)."),
        "copy_media": boolean("Copy the media into assets/ (default). false = link to the original files (symlinks) "
                              "to save disk; the project then only works on this computer.", True),
    }, required=["output_dir"]),
    read_only=True,
    covers=("handoff.hyperframes_export",),
)
def export_to_hyperframes(output_dir, copy_media=True):
    """Write the timeline as a HyperFrames project that previews, lints and renders, and comes back to Zenvi losslessly.

    Use for "export this edit to HyperFrames", "make a HyperFrames project
    from my timeline", "hand this to HyperFrames / Framey". Every clip becomes
    a HyperFrames primitive (<video>/<img>/<audio>; titles as <img> SVG) with
    its timing, track, placement (CSS from libopenshot's own geometry),
    keyframes (a GSAP timeline whose eases match Zenvi frame for frame),
    opacity transitions, constant speed (data-playback-rate) and audio levels.
    The Zenvi project is embedded in index.html, so
    import_hyperframes_project_tool on the folder restores the clips natively.
    Effects, rounded corners, blend modes, reversed / ramped speed and wipe
    transitions do not translate; the receipt and README.md list each one.
    When the HyperFrames CLI is available the result is linted and the lint
    findings come back in the receipt. Changes nothing in the Zenvi project.
    Example: {"output_dir": "/Users/me/exports/launch-hyperframes"}.
    """
    from classes.editor_tools.titles_text_common import precheck_on_main
    from classes.handoff import jobs
    from classes.handoff.hyperframes import cli as hf_cli
    from classes.handoff.hyperframes import exporter
    from classes.handoff.linked_media import LinkError
    try:
        target = exporter.check_output_dir(output_dir)
    except LinkError as exc:
        raise ToolError(str(exc)) from None

    def _snapshot():
        from classes.editor_tools._base import get_app
        from classes.handoff.timeline_view import TimelineSnapshot
        snap = TimelineSnapshot.from_app()
        return snap, exporter.raw_project(get_app().project._data)

    taken = precheck_on_main(_snapshot)
    if not taken:
        raise ToolError("the editor did not hand over the timeline; try again")
    snapshot, raw = taken
    try:
        with jobs.track_job("Exporting to HyperFrames", kind="hyperframes") as job:
            result = exporter.export_project(snapshot, raw, target, copy_media=bool(copy_media),
                                             on_progress=job.report, should_cancel=job.should_cancel)
    except jobs.JobCancelled:
        raise ToolError("the HyperFrames export was cancelled; nothing was written") from None
    except LinkError as exc:
        raise ToolError(str(exc)) from None
    lint = None
    try:
        report = hf_cli.lint(result.output_dir, timeout=LINT_TIMEOUT)
        lint = {"ok": bool(report.get("ok")), "errors": report.get("errorCount"),
                "warnings": report.get("warningCount"),
                "findings": [{"code": f.get("code"), "severity": f.get("severity"), "message": f.get("message")}
                             for f in (report.get("findings") or [])][:20]}
    except (hf_cli.CliError, OSError) as exc:
        lint = {"ok": None, "skipped": str(exc)}
    except jobs.JobCancelled:
        lint = {"ok": None, "skipped": "cancelled"}
    return ok("Exported %d clip(s) (%.2fs) to the HyperFrames project %s%s." % (
        result.clips, result.duration, result.output_dir,
        "; lint clean" if lint and lint.get("ok") else ("; lint: %s error(s)" % lint.get("errors")
                                                        if lint and lint.get("ok") is False else "")),
        output_dir=result.output_dir, index=result.index, files=result.files, clips=result.clips,
        duration=result.duration, warnings=result.warnings, lint=lint, copy_media=bool(copy_media))
