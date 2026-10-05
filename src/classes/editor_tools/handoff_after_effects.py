"""Workstream: handoff-after-effects -- the timeline as an After Effects comp.

``export_to_after_effects_tool`` writes the script (File > Export Project >
After Effects Script); ``send_to_after_effects_tool`` builds the comp in the
After Effects that runs the Zenvi Link panel (File > Send To > After
Effects). Both are background-safe: the project is snapshotted on the GUI
thread (a deep copy), then media copies, title and wipe rendering, file
writes and the Zenvi Link call run on the calling worker thread, registered
as a handoff job. Neither changes the Zenvi project, so neither adds an undo
step. In After Effects the import is one undo step: "Import Zenvi project"
after File > Scripts, "Zenvi: Run JSX file" when Zenvi Link ran it.
"""

from __future__ import annotations

from classes.editor_tools._base import ToolError, boolean, obj, ok, on_main, string
from classes.editor_tools._registry import editor_tool


def _snapshot():
    from classes.handoff.timeline_view import TimelineSnapshot
    snapshot = on_main(TimelineSnapshot.from_app)
    if snapshot is None:
        raise ToolError("the project is not open yet")
    if not snapshot.clips:
        raise ToolError("the timeline has no clips to export; add clips first")
    return snapshot


def _titles(titles: list) -> list:
    return [{"title": t.get("title"), "mode": t.get("mode"), "detail": t.get("detail")} for t in titles]


@editor_tool(
    "export_to_after_effects_tool",
    label="Export to After Effects",
    schema=obj({
        "output_dir": string("Folder to write <project>.jsx, README.txt, titles/, masks/ and (with collect_media) "
                             "media/ into; created when missing. Empty = '<project name>_AfterEffects' next to the "
                             "saved project, or in Downloads for an unsaved one.", ""),
        "collect_media": boolean("Copy every media file into media/ next to the script so the folder works on "
                                 "another computer. false = the script imports the media from where it is now "
                                 "(faster; this computer only).", True),
        "open_folder": boolean("Reveal the script in Finder / Explorer when it is written.", False),
    }),
    background_safe=True,
    covers=("handoff.ae_export",),
)
def export_to_after_effects(output_dir="", collect_media=True, open_folder=False):
    """Export the timeline as an After Effects script (.jsx) that rebuilds it as a composition (File > Export Project > After Effects).

    Use for "open this edit in After Effects", "make an AE comp of this
    timeline", or to hand the edit to someone with After Effects. When After
    Effects is open with the Zenvi Link panel, prefer send_to_after_effects_tool,
    which also builds it. The script imports every media file once (a
    placeholder when one is missing), adds a folder "Zenvi - <project>" and a
    comp with the project's size and frame rate, and makes one layer per
    clip: timing and speed changes (stretch or time remap), position, scale,
    rotation and opacity with the same eased keyframes, audio levels in dB,
    titles (simple templates as editable text layers, others as images),
    mapped effects (blur, brightness/contrast, saturation, hue, invert,
    mosaic, sharpen, chroma key, crop, colour grade), fade and wipe
    transitions, and markers. Running it in After Effects is one undo step.
    Changes nothing in the Zenvi project. Returns the script path, counts, how
    each title was exported and warnings for anything After Effects has no
    equivalent for (also written to README.txt next to the script).
    """
    from classes.handoff import jobs
    from classes.handoff.after_effects_export import (
        AeCancelled, AeHandoffError, default_output_dir, export_after_effects, reveal,
    )
    snapshot = _snapshot()
    folder = str(output_dir or "").strip() or default_output_dir(snapshot.project_path, snapshot.name)
    try:
        with jobs.track_job("Export to After Effects", kind="after-effects") as job:
            result = export_after_effects(snapshot, folder, collect_media=bool(collect_media), interactive=True,
                                          progress=job.report, should_cancel=job.should_cancel)
    except AeCancelled:
        raise ToolError("the After Effects export was cancelled; nothing was written") from None
    except AeHandoffError as exc:
        raise ToolError(str(exc)) from None
    revealed = False
    if open_folder:
        try:
            reveal(result.script_path)
            revealed = True
        except AeHandoffError:
            revealed = False
    stats = result.stats
    native = sum(1 for t in result.titles if t.get("mode") == "native")
    summary = (f"Exported {stats.get('layers', 0)} layer(s) for After Effects to {result.script_path}"
               + (f"; {len(result.titles)} title(s), {native} as editable text" if result.titles else "")
               + (f"; {len(result.missing)} missing media file(s) become placeholders" if result.missing else "")
               + (f"; {len(result.warnings)} warning(s)" if result.warnings else "")
               + ". Run it in After Effects with File > Scripts > Run Script File.")
    return ok(summary, script=result.script_path, folder=result.folder, readme=result.readme_path,
              collected_media=result.collected, media_files=len(result.media), layers=stats.get("layers", 0),
              footage=stats.get("footage", 0), titles=_titles(result.titles), missing_media=result.missing,
              warnings=result.warnings, stats=stats, revealed=revealed)


@editor_tool(
    "send_to_after_effects_tool",
    label="Send to After Effects",
    schema=obj({
        "collect_media": boolean("Copy the media into the export folder first (only needed when After Effects "
                                 "runs where the original media paths are not reachable). Default: import the "
                                 "media from where it is.", False),
    }),
    background_safe=True,
    covers=("handoff.ae_send",),
)
def send_to_after_effects(collect_media=False):
    """Build the timeline as a comp in the open After Effects through the Zenvi Link panel (File > Send To > After Effects).

    Use for "send this to After Effects" / "build it in AE" when After Effects
    is running with Zenvi Link (list_link_hosts_tool shows it connected). It
    exports the same script as export_to_after_effects_tool into the
    project's assets folder (titles and wipe images stay there for After
    Effects to read), runs it in After Effects, and returns After Effects'
    own summary: the comp name and id, layers built, placeholders for missing
    media and any warnings. In After Effects the import is one undo step
    (Edit > Undo "Zenvi: Run JSX file"); the Zenvi project is not changed.
    Refuses with how to connect when After Effects is not connected.
    """
    from classes.handoff import adobe_link, jobs
    from classes.handoff.after_effects_export import AeCancelled, AeHandoffError, undo_hint
    from classes.handoff.after_effects_export import send_to_after_effects as send
    snapshot = _snapshot()
    try:
        with jobs.track_job("Send to After Effects", kind="after-effects") as job:
            result = send(snapshot, collect_media=bool(collect_media), progress=job.report,
                          should_cancel=job.should_cancel)
    except adobe_link.HostNotConnected as exc:
        raise ToolError(f"{exc} Or write a script with export_to_after_effects_tool and run it in After Effects "
                        "(File > Scripts > Run Script File...).") from None
    except AeCancelled:
        raise ToolError("sending to After Effects was cancelled") from None
    except adobe_link.LinkHostError as exc:
        raise ToolError(f"After Effects did not finish building the comp ({exc}); the script is still on disk "
                        "and can be run with File > Scripts > Run Script File") from None
    except AeHandoffError as exc:
        raise ToolError(str(exc)) from None
    summary = result.summary or {}
    export = result.export
    comp = summary.get("comp") or export.stats.get("comp") or ""
    layers = summary.get("layers", export.stats.get("layers", 0))
    placeholders = list(summary.get("placeholders") or [])
    ae_warnings = [str(w) for w in (summary.get("warnings") or [])]
    line = (f"Built comp {comp!r} in After Effects with {layers} layer(s)" if comp else
            "After Effects ran the export script")
    line += (f"; {len(placeholders)} placeholder(s) for missing media" if placeholders else "")
    line += (f"; {len(ae_warnings) + len(export.warnings)} warning(s)" if ae_warnings or export.warnings else "")
    line += ". " + undo_hint("zenvi-link")
    return ok(line, comp=comp, comp_id=summary.get("comp_id"), folder=summary.get("folder"), layers=layers,
              placeholders=placeholders, ae_warnings=ae_warnings, export_warnings=export.warnings,
              titles=_titles(export.titles), script=export.script_path, collected_media=export.collected,
              host_summary=result.host_receipt.get("summary"))
