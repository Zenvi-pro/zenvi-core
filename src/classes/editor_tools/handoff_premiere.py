"""Workstream: handoff-premiere -- Zenvi <-> Adobe Premiere Pro through FCP7 XML.

``export_to_premiere_tool`` writes the timeline as Final Cut Pro 7 XML tuned
for Premiere's importer, ``send_to_premiere_tool`` opens it in a connected
Premiere Pro (Zenvi Link), ``import_timeline_xml_tool`` brings a Premiere /
FCP7 XML sequence back as one undo step. The conversions live in
``classes.exporters.final_cut_pro`` / ``classes.importers.final_cut_pro``;
Send To in ``classes.handoff.premiere``.
"""

from __future__ import annotations

import os
from typing import Any, cast

from classes.editor_tools._base import ToolError, boolean, enum, obj, ok, string
from classes.editor_tools._registry import editor_tool


def _snapshot() -> Any:
    from classes.editor_tools.titles_text_common import precheck_on_main
    from classes.handoff.timeline_view import TimelineSnapshot
    return cast(TimelineSnapshot, precheck_on_main(TimelineSnapshot.from_app))


def _default_output() -> str:
    from classes.exporters.final_cut_pro import default_export_path
    from classes.qt_main_thread import call_on_gui
    path = str(call_on_gui(default_export_path, ".xml", " (Premiere)", timeout=30))
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = f"{stem} {n}{ext}"
        n += 1
    return path


def _export_summary(result) -> str:
    c = result.counts
    parts = [f"{c.get('clips', 0)} clip(s) on {c.get('video_tracks', 0)} video / {c.get('audio_tracks', 0)} audio "
             "track(s)"]
    if c.get("transitions"):
        parts.append(f"{c['transitions']} transition(s)")
    if c.get("markers"):
        parts.append(f"{c['markers']} marker(s)")
    if result.stills:
        parts.append(f"{len(result.stills)} title(s) as PNG")
    if result.copied:
        parts.append(f"{len(result.copied)} media file(s) copied")
    text = ", ".join(parts)
    if result.warnings:
        text += f"; {len(result.warnings)} warning(s) (see warnings)"
    return text


@editor_tool(
    "export_to_premiere_tool",
    label="Export to Premiere Pro",
    schema=obj({
        "output_path": string("The .xml file to write. Empty = next to the project as '<name> (Premiere).xml' "
                              "(a free name is picked if it exists).", ""),
        "collect_media": boolean("Also copy every media file into '<xml name>_media/' next to the XML and point "
                                 "the XML at the copies (to move the edit to another computer).", False),
        "overwrite": boolean("Allow replacing an existing output_path.", False),
    }),
    background_safe=True,
    covers=("handoff.premiere_export",),
)
def export_to_premiere(output_path="", collect_media=False, overwrite=False):
    """Export the timeline as Final Cut Pro 7 XML tuned for Adobe Premiere Pro's importer (File > Export Project > Premiere Pro).

    Use for "send this edit to Premiere", "make a Premiere XML". Premiere
    opens it with File > Import. Clips keep their trims, speed and reverse,
    motion (position, scale, rotation, anchor), opacity and volume keyframes
    (eased curves become extra linear keys so they look the same), crossfades
    and fades become Cross Dissolves, markers keep their names and colours,
    track names and locks carry over, and titles are rendered to transparent
    PNG stills in '<xml name>_media/titles/' (Premiere cannot read SVG). Effects
    without a Premiere equivalent are listed in warnings. Not a video render
    (export_video_tool) and changes nothing in the project. For a connected
    Premiere use send_to_premiere_tool. Example: {"output_path":
    "/Users/me/Desktop/Promo.xml"}.
    """
    from classes.exporters import final_cut_pro as fcp
    from classes.handoff import jobs
    path = os.path.abspath(os.path.expanduser(str(output_path or "").strip())) if str(output_path or "").strip() \
        else ""
    if path:
        if os.path.isdir(path):
            raise ToolError(f"{path} is a folder; give a file name ending in .xml")
        if not path.lower().endswith(".xml"):
            path += ".xml"
        if os.path.exists(path) and not overwrite:
            raise ToolError(f"{path} already exists; pass overwrite=true to replace it or choose another output_path")
        folder = os.path.dirname(path)
        if os.path.exists(folder) and not os.path.isdir(folder):
            raise ToolError(f"{folder} is a file, not a folder; choose another output_path")
    snapshot = _snapshot()
    if not snapshot.clips:
        raise ToolError("the timeline has no clips to export")
    path = path or _default_output()
    try:
        with jobs.track_job("Exporting to Premiere Pro", kind="premiere-export") as job:
            result = fcp.export_timeline(snapshot, path, collect_media=bool(collect_media),
                                         on_progress=lambda f, m: job.report(f, m), should_cancel=job.should_cancel)
    except jobs.JobCancelled:
        raise ToolError("the export was cancelled; nothing was written") from None
    except fcp.ExportError as exc:
        raise ToolError(str(exc)) from None
    except OSError as exc:
        raise ToolError(f"could not write {path}: {exc}") from None
    return ok(f"Exported the timeline for Premiere Pro: {os.path.basename(result.path)} -- {_export_summary(result)}.",
              **result.as_dict())


@editor_tool(
    "send_to_premiere_tool",
    label="Send to Premiere Pro",
    schema=obj({}),
    background_safe=True,
    covers=("handoff.premiere_send",),
)
def send_to_premiere():
    """Open the timeline as a new sequence in the connected Adobe Premiere Pro (File > Send To > Premiere Pro).

    Use for "open this in Premiere", "send it to Premiere Pro". Exports the
    timeline as Premiere-tuned FCP7 XML into the project's assets folder
    ('premiere/<time> <name>/', titles as PNG stills there) and asks Premiere
    (Zenvi Link panel) to import it into the "Zenvi Imports" bin and open it.
    Returns the new sequence's name and id, media Premiere could not find,
    and conversion warnings. Refused when Premiere is not connected
    (list_link_hosts_tool says how to connect). Changes nothing in the Zenvi
    project; the import is one step in Premiere's own undo history.
    """
    from classes.exporters import final_cut_pro as fcp
    from classes.handoff import adobe_link, jobs
    from classes.handoff import premiere as ppro
    host = adobe_link.get_host(ppro.APP)
    if not host.connected:
        raise ToolError(f"Premiere Pro is not connected ({host.reason}). {adobe_link.connect_hint(ppro.APP)}")
    snapshot = _snapshot()
    if not snapshot.clips:
        raise ToolError("the timeline has no clips to send")
    try:
        with jobs.track_job("Sending to Premiere Pro", kind="premiere-send") as job:
            result = ppro.send_to_premiere(snapshot, on_progress=lambda f, m: job.report(f, m),
                                           should_cancel=job.should_cancel)
    except jobs.JobCancelled:
        raise ToolError("sending was cancelled") from None
    except (ppro.SendError, fcp.ExportError) as exc:
        raise ToolError(str(exc)) from None
    offline = result["offline"]
    extra = f"; Premiere could not find {len(offline)} media file(s)" if offline else ""
    return ok(f"Opened “{result['sequence_name']}” in Premiere Pro{extra}.", **result)


@editor_tool(
    "import_timeline_xml_tool",
    label="Import Premiere / FCP XML",
    schema=obj({
        "path": string("The .xml file: a Premiere Pro sequence exported as Final Cut Pro XML (Premiere File > "
                       "Export > Final Cut Pro XML, or premiere_export_xml), or any FCP7 xmeml file."),
        "placement": enum(["new_tracks", "at_playhead"], "new_tracks = keep the sequence's own timing on new "
                          "tracks above the existing ones; at_playhead = the same, but starting at the playhead.",
                          "new_tracks"),
        "sequence": string("Which sequence to import when the XML holds several (its name, as listed in the "
                           "warnings). Empty = the first top-level sequence.", ""),
    }, required=["path"]),
    background_safe=True,
    covers=("handoff.timeline_xml_import",),
)
def import_timeline_xml(path, placement="new_tracks", sequence=""):
    """Bring a Premiere Pro / Final Cut Pro 7 XML sequence into this project on new tracks, as one undo step (File > Import Project > Premiere Pro XML).

    Use for "bring my Premiere edit back", "import this XML". Clips keep their
    timing, trims, speed / reverse, motion, opacity and volume keyframes;
    disabled clips come in hidden or muted; Cross Dissolves become fades,
    audio cross fades volume ramps; markers keep names and colours; nested
    sequences are flattened onto extra tracks. Media not already in Project
    Files is added; media that cannot be found is skipped and listed
    (missing_media). Generators, titles made in Premiere, image sequences and
    effects Zenvi lacks are listed in warnings, as are the other sequences of a
    multi-sequence XML (import one of them with sequence). One undo step
    removes everything it added. Example: {"path": "/Users/me/Desktop/Edit.xml"}.
    """
    from classes.editor_tools.titles_text_common import commit_on_main, precheck_on_main
    from classes.handoff import jobs
    from classes.importers import final_cut_pro as importer
    target = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not os.path.isfile(target):
        raise ToolError(f"no XML file at {target}")
    info = precheck_on_main(importer.read_project_info)
    try:
        with jobs.track_job("Importing %s" % os.path.basename(target), kind="xml-import") as job:
            plan = importer.plan_import(target, placement=placement, info=info, should_cancel=job.should_cancel,
                                        sequence=str(sequence or ""))
    except jobs.JobCancelled:
        raise ToolError("the import was cancelled; nothing changed") from None
    except importer.XmlImportError as exc:
        raise ToolError(str(exc)) from None
    try:
        summary = cast(dict, commit_on_main(importer.commit_import, plan))
    except importer.XmlImportError as exc:
        raise ToolError(str(exc)) from None
    clips, tracks = summary["clip_ids"], summary["track_numbers"]
    note = f"; {len(plan.missing)} missing media skipped" if plan.missing else ""
    return ok(f"Imported {len(clips)} clip(s) of “{plan.sequence_name}” onto {len(tracks)} new track(s)"
              f"{f' at {plan.offset:.2f}s' if plan.placement == 'at_playhead' else ''}{note}.",
              sequence_name=plan.sequence_name, timeline_clip_ids=clips, layers=tracks,
              transition_ids=summary["transition_ids"], marker_ids=summary["marker_ids"],
              file_ids=summary["file_ids"], missing_media=plan.missing, warnings=plan.warnings,
              placement=plan.placement, offset=round(plan.offset, 3))
