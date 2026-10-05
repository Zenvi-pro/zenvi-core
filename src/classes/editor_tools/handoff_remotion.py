"""Workstream: handoff-remotion -- Remotion projects in and out of Zenvi (C4).

* ``list_remotion_compositions_tool`` -- a Remotion project's compositions
  (size, fps, duration, default props, the code that renders each).
* ``import_remotion_project_tool`` -- render compositions with the project's
  own Remotion as linked clips (Edit Props / Open Code / Open in Studio /
  Re-render), or restore a project Zenvi exported as native clips; one undo
  step for everything one call adds.
* ``export_to_remotion_tool`` -- the timeline as a working Remotion project
  that comes back losslessly.

The work lives in ``classes.handoff.remotion``; these tools validate, block
on their worker thread while Node renders (registered as ``handoff.jobs``
jobs, so the toolbar pill shows them with Cancel) and hop to the GUI thread
only for the project snapshot and the one-undo-step insert.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from classes.editor_tools._base import (
    ToolError, array, boolean, enum, mapping, nullable, number, obj, ok, string,
)
from classes.editor_tools._registry import editor_tool

CODECS = ["auto", "prores4444", "h264", "qtrle"]


def _remotion():
    from classes.handoff import remotion  # registers the provider (load_plugins does too)
    return remotion


def _fail(exc: Exception) -> ToolError:
    return ToolError(str(exc))


@editor_tool(
    "list_remotion_compositions_tool",
    label="List Remotion compositions",
    schema=obj({
        "project_dir": string("The Remotion project's folder (the one with package.json; a folder inside it works "
                              "too), e.g. /Users/me/code/my-video."),
    }, required=["project_dir"]),
    read_only=True,
    covers=("handoff.remotion_list",),
)
def list_remotion_compositions(project_dir):
    """List a Remotion project's compositions before importing them into Zenvi.

    Use for "what's in this Remotion project", or before import_remotion_project_tool to pick
    composition ids and see which props they take. Returns each composition's id, kind
    (composition or still), width x height, fps, duration (frames and seconds), defaultProps and the
    file:line of the component that renders it, plus the project's entry point, Remotion version,
    whether node_modules is installed and whether Zenvi exported it (zenvi_generated: its timeline
    restores as native clips). Reading sizes and props bundles the project with its own Remotion
    (seconds to a minute the first time); without node_modules only ids and code locations are listed.
    Changes nothing. Example: {"project_dir": "/Users/me/code/promo"}.
    """
    from classes.handoff import jobs
    from classes.handoff.linked_media import LinkError
    _remotion()
    from classes.handoff.remotion import importer
    try:
        with jobs.track_job("Reading Remotion compositions", kind="remotion") as job:
            listing = importer.list_compositions(project_dir, on_progress=job.report,
                                                 should_cancel=job.should_cancel, allow_static=True)
    except jobs.JobCancelled:
        raise ToolError("reading the compositions was cancelled") from None
    except LinkError as exc:
        raise _fail(exc) from None
    data = listing.as_dict()
    names = ", ".join(c.id for c in listing.compositions) or "none"
    extra = " (from the code only: install the project to read sizes and props)" if listing.static_only else ""
    native = " Zenvi exported it: import restores its timeline as native clips." \
        if listing.project.is_zenvi_generated else ""
    return ok(f"{listing.project.name} has {len(listing.compositions)} composition(s): {names}{extra}.{native}",
              **data)


@editor_tool(
    "import_remotion_project_tool",
    label="Import Remotion project",
    schema=obj({
        "project_dir": string("The Remotion project's folder (the one with package.json)."),
        "compositions": array({"type": "string"}, "Composition ids to import (list_remotion_compositions_tool). "
                              "Empty = every composition, or just the Zenvi timeline for a project Zenvi exported."),
        "props": nullable(mapping("Input props per composition id, merged over its defaultProps, e.g. "
                                  "{\"TitleCard\": {\"title\": \"Launch day\", \"accent\": \"#FF5A36\"}}.")),
        "codec": enum(CODECS, "How to render: auto = ProRes 4444 when any pixel is transparent (titles, overlays), "
                      "else H.264; prores4444 keeps alpha; h264 is opaque and small; qtrle keeps alpha but is "
                      "large. Never WebM (Zenvi drops its alpha).", "auto"),
        "position": nullable(number("Timeline seconds where the clips start. Omit for the playhead.", minimum=0)),
        "track": string("Track for the clip (UI track number, name or layer number) when importing ONE "
                        "composition. Empty = the lowest free track above the video (a new track when none is "
                        "free); several compositions stack, opaque ones below transparent ones.", ""),
        "restore_native": boolean("For a project Zenvi exported (it has src/zenvi/timeline.json): bring its "
                                  "timeline back as native, editable Zenvi clips (true) instead of one rendered "
                                  "linked clip (false).", True),
    }, required=["project_dir"]),
    background_safe=True,
    covers=("handoff.remotion_import",),
)
def import_remotion_project(project_dir, compositions=None, props=None, codec="auto", position=None, track="",
                            restore_native=True):
    """Bring a Remotion project into the timeline: compositions as linked clips, or a Zenvi export as native clips.

    Use for "put my Remotion intro on the timeline", "import the TitleCard composition with the
    title Launch day", "bring back the project I exported to Remotion". Each composition renders with
    the project's own installed Remotion (Zenvi never ships it; node_modules must be installed) --
    seconds to minutes -- and becomes a linked clip that remembers its source: change props with
    update_linked_clip_tool, open the code or Remotion Studio with open_linked_source_tool, re-render
    after code edits with rerender_linked_clip_tool. Clips land together at position (default the
    playhead) on free tracks above the video, transparent ones on top. A project Zenvi exported comes
    back as native clips (lossless) when restore_native is true. Everything one call adds is ONE
    undo step; a failed or cancelled render changes nothing. Example: {"project_dir":
    "/Users/me/code/promo", "compositions": ["TitleCard"], "props": {"TitleCard": {"title": "Hi"}}}.
    """
    from classes.handoff import jobs
    from classes.handoff.linked_media import LinkError
    _remotion()
    from classes.handoff.remotion import importer
    if props is not None and not isinstance(props, dict):
        raise ToolError('props must be an object of composition id -> props, e.g. {"TitleCard": {"title": "Hi"}}')
    ids: List[str] = [str(c) for c in (compositions or [])]
    try:
        with jobs.track_job("Importing Remotion project", kind="remotion") as job:
            receipt = importer.import_project(project_dir, compositions=ids, props=props, codec=codec,
                                              position=position, track=track or "", restore_native=restore_native,
                                              on_progress=job.report, should_cancel=job.should_cancel)
    except jobs.JobCancelled:
        raise ToolError("the Remotion import was cancelled; nothing changed") from None
    except LinkError as exc:
        raise _fail(exc) from None
    except ToolError:
        raise
    except Exception as exc:
        raise RuntimeError(f"the Remotion import failed: {exc}. Nothing was added") from None
    parts = []
    native: Dict[str, Any] = receipt.get("native") or {}
    if native:
        edited = len(native.get("edits") or [])
        parts.append(f"restored {len(native.get('clips') or [])} native clip(s) on {native.get('tracks', 0)} new "
                     f"track(s) at {native.get('position', 0):.2f}s"
                     + (f", with {edited} edit(s) made in the Remotion project" if edited else ""))
    linked = receipt.get("linked") or []
    for c in linked:
        parts.append(f"added {c['composition']} ({c['codec']}, {c['duration']:.2f}s) at {c['position']:.2f}s on "
                     f"layer {c['layer']}")
    warn = (" Warnings: " + " ".join(receipt["warnings"])) if receipt.get("warnings") else ""
    return ok(f"Remotion project {receipt.get('project')}: " + "; ".join(parts) + "." + warn, **receipt)


@editor_tool(
    "export_to_remotion_tool",
    label="Export to Remotion",
    schema=obj({
        "output_dir": string("Folder to create the Remotion project in. Zenvi creates it; it must not exist yet, "
                             "be empty, or be an earlier Zenvi export of a project (updated in place, keeping "
                             "node_modules)."),
        "copy_media": boolean("Copy the media into public/zenvi-media (true: works on any computer) or symlink it "
                              "(false: saves disk, only works on this computer).", True),
        "install": boolean("Run npm install in the new project afterwards (needs Node.js and internet; minutes).",
                           False),
    }, required=["output_dir"]),
    background_safe=True,
    covers=("handoff.remotion_export",),
)
def export_to_remotion(output_dir, copy_media=True, install=False):
    """Write the timeline as a working Remotion project that renders it and comes back to Zenvi losslessly.

    Use for "export this to Remotion", "give me this edit as React/Remotion code", "hand the timeline
    to a developer". Writes package.json (Remotion 4.0.532 pinned), src/Root.tsx with one composition
    (ZenviTimeline, props editable in Remotion Studio), src/zenvi/timeline.json (tracks, clips,
    keyframes with easing, transitions, markers, plus the original project for the round trip), a
    generic renderer built on Remotion primitives, the media in public/zenvi-media and a README with
    the studio/render commands and what is approximated (wipes play as fades; some effects are CSS
    filters, others are listed). Re-importing the folder with import_remotion_project_tool restores
    the native clips. Changes nothing in the project. Example: {"output_dir": "/Users/me/code/trip-remotion"}.
    """
    from classes.handoff import jobs
    from classes.handoff.linked_media import LinkError
    from classes.editor_tools.titles_text_common import precheck_on_main
    _remotion()
    from classes.handoff.remotion import exporter
    if not str(output_dir or "").strip():
        raise ToolError("output_dir is required: the folder to create the Remotion project in")
    try:
        exporter.target_mode(str(output_dir))
    except LinkError as exc:
        raise _fail(exc) from None
    taken = precheck_on_main(exporter.snapshot_from_app)
    if not taken:
        raise ToolError("could not read the open project; try again")
    snapshot, data = taken
    try:
        with jobs.track_job("Exporting to Remotion", kind="remotion") as job:
            receipt = exporter.export_project(snapshot, data, str(output_dir), copy_media=bool(copy_media),
                                              install=bool(install), on_progress=job.report,
                                              should_cancel=job.should_cancel)
    except jobs.JobCancelled:
        raise ToolError("the Remotion export was cancelled; no project was written") from None
    except LinkError as exc:
        raise _fail(exc) from None
    except OSError as exc:
        raise ToolError(f"could not write the Remotion project: {exc}") from None
    notes: Optional[List[str]] = receipt.get("notes")
    extra = f" {len(notes)} note(s) on what Remotion approximates are in README.md." if notes else ""
    return ok(f"Exported {receipt['clips']} clip(s) and {receipt['media_files']} media file(s) to "
              f"{receipt['output_dir']} (ZenviTimeline, {receipt['composition']['width']}x"
              f"{receipt['composition']['height']} @ {receipt['composition']['fps']} fps).{extra} Next: "
              + "; ".join(receipt.get("next_steps") or []), **receipt)
