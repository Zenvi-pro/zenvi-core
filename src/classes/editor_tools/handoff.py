"""Workstream: handoff -- Zenvi <-> After Effects / Premiere Pro / Remotion / HyperFrames, and linked clips.

This module registers the shared handoff tools: the Adobe hosts Zenvi Link
exposes (``list_link_hosts_tool``, ``call_link_host_tool``) and linked clips
(rendered media that keeps a link to its source: import, inspect, update,
re-render, open the source, unlink). The core lives in ``classes.handoff``.

Each handoff package registers its own tools in a sibling module that is
loaded at the bottom of this file when it is present in the build:

=====================================  ====================================
``editor_tools.handoff_after_effects``  C2: export/send to After Effects
``editor_tools.handoff_premiere``       C3: Premiere XML export/send/import
``editor_tools.handoff_remotion``       C4: Remotion list/import/export
``editor_tools.handoff_hyperframes``    C5: HyperFrames import/export
=====================================  ====================================

Each sibling's workstream (``handoff-after-effects`` ...) is mapped in
``editor_tools.WORKSTREAM_OF_MODULE`` and owns its ``handoff.*`` capability
ids in ``coverage.py``.

Long work (renders) blocks the tool call on its worker thread, like
``add_animated_title_tool``: the tools are background-safe, register a
``handoff.jobs`` job while they render (the toolbar pill shows it, and
``get_linked_clip_tool`` reports ``rendering``), and hop to the GUI thread
only for the one-undo-step media swap.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from classes.editor_tools._base import (
    ToolError, array, boolean, enum, mapping, nullable, number, obj, ok, string,
)
from classes.assets import path_is_under
from classes.editor_tools._registry import editor_tool
from classes.logger import log

HOSTS = ("aftereffects", "premiere")
HOST_PREFIX = {"aftereffects": "ae_", "premiere": "premiere_"}

_FILE_TARGET = {
    "clip_id": string("A timeline clip of the linked file (timeline_clip_id from get_timeline_state_tool). "
                      "Give this or file_id.", ""),
    "file_id": string("The linked file's Project Files id instead of a clip.", ""),
}


def _lm():
    from classes.handoff import linked_media
    return linked_media


def _link_error(exc: Exception) -> ToolError:
    return ToolError(str(exc))


def _resolve_file_id(clip_id: str = "", file_id: str = "") -> str:
    """The linked file id named by clip_id or file_id (they must agree when both are given)."""
    lm = _lm()
    clip_id, file_id = str(clip_id or "").strip(), str(file_id or "").strip()
    if not clip_id and not file_id:
        raise ToolError("say which linked clip: clip_id (a timeline clip) or file_id (its Project Files id)")
    try:
        from_clip = lm.file_id_for_clip(clip_id) if clip_id else ""
    except lm.LinkError as exc:
        raise _link_error(exc) from None
    if from_clip and file_id and from_clip != file_id:
        raise ToolError(f"clip {clip_id} uses file {from_clip}, not {file_id}; pass one of them")
    return from_clip or file_id


def _query(name: str) -> Any:
    """``classes.query.File`` / ``Clip`` (untyped; looked up per call)."""
    from classes import query
    return getattr(query, name)


def _linked_file(file_id: str):
    f = _query("File").get(id=file_id)
    if not f:
        raise ToolError(f"no project file with id={file_id!r} (list_project_files_tool lists them)")
    try:
        linked = _lm().read_link(f.data) is not None
    except _lm().LinkError:
        linked = True  # damaged, but linked: the caller reports it
    if not linked:
        name = f.data.get("name") or os.path.basename(str(f.data.get("path") or ""))
        raise ToolError(f"{name!r} is not a linked clip (it has no source link); linked clips come from "
                        "import_remotion_project_tool, import_hyperframes_project_tool or import_linked_media_tool")
    return f


def _clip_rows(file_id: str) -> List[dict]:
    from classes.editor_tools._base import describe_clip
    return [describe_clip(c) for c in _query("Clip").filter(file_id=file_id)]


# ---------------------------------------------------------------------------
# Adobe hosts
# ---------------------------------------------------------------------------

# Full tool entries per reply. The Assistant's harness cuts a tool result at 51,200 bytes, and a receipt is
# ONE JSON line, so an oversized reply arrives as nothing: entries past this budget come back as deferred_tools.
TOOL_DETAILS_BUDGET = 32_000


def _tool_names(tools: Any) -> List[str]:
    """The distinct, non-blank names in *tools*, in the order given."""
    names: List[str] = []
    for name in tools or ():
        name = str(name or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _json_bytes(value: Any) -> int:
    """Bytes *value* takes in the receipt the Assistant reads (``ToolReceipt.to_json``)."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _host_tools(catalog: List[dict], include_tools: bool, wanted: List[str], budget: int) -> Tuple[dict, str, int]:
    """One connected host's tools for the listing: (row keys, summary note, details budget left).

    ``tools`` is every tool as ``{name, title}`` when *include_tools*, the
    named ones in full (``adobe_link.tool_details``) either way; names the
    host lacks go to ``unknown_tools``, entries past the budget to
    ``deferred_tools``.
    """
    from classes.handoff import adobe_link
    rows = [r for r in catalog if isinstance(r, dict) and r.get("name")]
    by_name = {str(r["name"]): r for r in rows}
    details: Dict[str, dict] = {}
    deferred: List[str] = []
    for name in wanted:
        if name in by_name:
            entry = adobe_link.tool_details(by_name[name])
            size = _json_bytes(entry)
            if size <= budget:
                details[name] = entry
                budget -= size
            else:
                deferred.append(name)
    if include_tools:
        tools = [details.get(str(r["name"])) or adobe_link.tool_brief(r) for r in rows]
    else:
        tools = [details[n] for n in wanted if n in details]
    part: Dict[str, Any] = {"tools": tools}
    unknown = [n for n in wanted if n not in by_name]
    if unknown:
        part["unknown_tools"] = unknown
    if deferred:
        part["deferred_tools"] = deferred
    counts = (["%d tools" % len(rows)] if include_tools else []) + (["%d described" % len(details)] if wanted else [])
    return part, " (%s)" % ", ".join(counts), budget


@editor_tool(
    "list_link_hosts_tool",
    label="List Adobe hosts",
    schema=obj({
        "include_tools": boolean("List each CONNECTED app's tools by name and title (a few KB). Call once with "
                                 "true to see what an app can do.", False),
        "tools": array({"type": "string"}, "Host tool names to describe in full -- description, inputSchema, "
                       "annotations -- for each CONNECTED app, e.g. [\"ae_add_text\"]. Ask only for the few you "
                       "will call; names an app lacks come back in its unknown_tools."),
    }),
    read_only=True,
    covers=("handoff.link_hosts",),
)
def list_link_hosts(include_tools=False, tools=None):
    """List the Adobe apps Zenvi Link connects -- After Effects and Premiere Pro -- and which one is active.

    Use before call_link_host_tool or a Send To After Effects / Premiere
    handoff, or when the user asks "is After Effects connected?". Each host
    reports connected, active (the one used most recently), app version and
    open project; a host that is not connected says why and how to connect it
    (open the app with the Zenvi Link panel; `zenvi adobe install` installs the
    panel). Before call_link_host_tool, call once with include_tools=true to
    see what an app can do (its tools by name and title), then with
    tools=[...] for the few you will call: those come back with description,
    inputSchema and annotations, so you pass arguments the schema accepts.
    Names an app does not have come back in its unknown_tools; details too
    long for one reply come back in deferred_tools (ask again for those); a
    host whose tool list fails says so in tools_error. Read-only; changes
    nothing.
    """
    from classes.handoff import adobe_link
    wanted = _tool_names(tools)
    hosts = adobe_link.list_hosts()
    rows = []
    notes: Dict[str, str] = {}
    parts: List[dict] = []
    failed = False
    budget = TOOL_DETAILS_BUDGET
    for host in hosts:
        row = host.as_dict()
        if host.connected and (include_tools or wanted):
            try:
                catalog = adobe_link.host_catalog(host.app)
            except adobe_link.LinkHostError as exc:
                row["tools_error"] = {"code": exc.code, "message": str(exc)}
                notes[host.app] = " (its tool list failed)"
                failed = True
            else:
                part, notes[host.app], budget = _host_tools(catalog, include_tools, wanted, budget)
                row.update(part)
                parts.append(part)
        rows.append(row)
    live = [h for h in hosts if h.connected]
    active = next((h.id for h in hosts if h.active), None)
    if live:
        summary = "Connected: %s%s." % (", ".join(
            "%s %s%s" % (h.label, h.app_version or "", notes.get(h.app, "")) for h in live),
            "; active: %s" % next(h.label for h in hosts if h.active) if active else "")
        unknown = [n for n in wanted if parts and not failed and all(n in p.get("unknown_tools", ()) for p in parts)]
        deferred = [n for p in parts for n in p.get("deferred_tools", ())]
        if unknown:
            summary += " No connected app has %s." % ", ".join(unknown)
        if deferred:
            summary += " Too long for one reply: ask again with tools=[%s]." % ", ".join(deferred)
    else:
        summary = "No Adobe app is connected. " + adobe_link.connect_hint("aftereffects")
    return ok(summary, hosts=rows, active=active)


@editor_tool(
    "call_link_host_tool",
    label="Use After Effects / Premiere",
    schema=obj({
        "host": enum(list(HOSTS), "Which Adobe app: aftereffects (ae_* tools) or premiere (premiere_* tools)."),
        "tool": string("The host tool to run, e.g. 'ae_get_state', 'ae_add_text', 'premiere_get_sequence'. "
                       "list_link_hosts_tool(include_tools=true) lists an app's tools."),
        "arguments": mapping("The host tool's arguments as an object, exactly as its inputSchema describes "
                             "(list_link_hosts_tool with tools=[name] gives it; times in seconds, ids from earlier "
                             "receipts). {} for none."),
    }, required=["host", "tool"]),
    background_safe=True,
    covers=("handoff.call_host",),
)
def call_link_host(host, tool, arguments=None):
    """Run one Zenvi Link tool inside a connected After Effects or Premiere Pro and return its receipt.

    Use to drive After Effects or Premiere from Zenvi: read state
    (ae_get_state, premiere_get_sequence), build or edit comps and sequences,
    capture frames to look at (ae_capture_frame / premiere_capture_frame come
    back as images), render, save. host + tool + arguments are passed through
    unchanged; the result carries the host's contract-3 receipt (status,
    summary, data, warnings, undoSteps) and any frame images. Changes happen
    in the Adobe app (its own undo), never in the Zenvi project. Refused when
    the app is not connected -- list_link_hosts_tool says how to connect.
    Example: {"host": "aftereffects", "tool": "ae_add_text", "arguments":
    {"text": "Launch day", "placement": "lower-third"}}.
    """
    from classes.agent_tools.output import ImageBlock, ToolOutput, trim_to_budget
    from classes.agent_tools.receipt import ToolReceipt
    from classes.handoff import adobe_link
    tool = str(tool or "").strip()
    prefix = HOST_PREFIX[host]
    if not tool:
        raise ToolError("name the host tool to run, e.g. ae_get_state")
    if not tool.startswith(prefix):
        other = next((h for h, p in HOST_PREFIX.items() if tool.startswith(p)), None)
        hint = f"; {tool} runs in {adobe_link.APP_LABELS[other]} (host={other!r})" if other else ""
        raise ToolError(f"{adobe_link.APP_LABELS[host]} tools start with {prefix!r}{hint}")
    args = dict(arguments or {})
    try:
        result = adobe_link.call_host_tool(host, tool, args)  # the tool's own catalog timeout
    except adobe_link.HostNotConnected as exc:
        raise ToolError(str(exc)) from None
    except adobe_link.LinkHostError as exc:
        if exc.code in ("INVALID_ARGUMENT", "UNSUPPORTED"):
            raise ToolError(f"{tool}: {exc}") from None
        raise RuntimeError(f"{adobe_link.APP_LABELS[host]} {tool} failed ({exc.code}): {exc}") from None
    receipt = dict(result.receipt)
    status = str(receipt.get("status") or "")
    data = {"host": host, "host_tool": tool, "host_status": status, "receipt": receipt}
    summary = result.summary or f"{tool} finished"
    warnings = [str(w) for w in (receipt.get("warnings") or [])]
    if status in ("error", "refused") or result.is_error:
        maker = ToolReceipt.refused if status == "refused" else ToolReceipt.error
        return ToolOutput(receipt=maker("call_link_host_tool", summary, data=data, warnings=warnings))
    images, trimmed = trim_to_budget([ImageBlock(data=img.data, mime_type=img.mime_type) for img in result.images])
    out = ToolReceipt(status="applied", tool="call_link_host_tool",
                      summary=f"{adobe_link.APP_LABELS[host]}: {summary}", undoSteps=0, data=data,
                      warnings=warnings + trimmed)
    return ToolOutput(receipt=out, images=images)


# ---------------------------------------------------------------------------
# Linked clips
# ---------------------------------------------------------------------------

def _staged_webm(path: str, kind: str) -> str:
    """A WebM re-encoded to ProRes 4444 in the links folder (libopenshot drops WebM alpha)."""
    from classes.handoff import alpha
    folder = _lm().links_dir(kind)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, os.path.splitext(os.path.basename(path))[0] + ".mov")
    n = 2
    while os.path.exists(target):
        target = os.path.join(folder, "%s-%d.mov" % (os.path.splitext(os.path.basename(path))[0], n))
        n += 1
    try:
        return alpha.reencode_to_prores4444(path, target)
    except alpha.AlphaError as exc:
        raise ToolError(f"could not convert the WebM to ProRes 4444 (libopenshot drops WebM alpha): {exc}") from None


def _adopt_media(path: str, kind: str) -> tuple:
    """(media path, undo) for a new linked clip: moved from the temp folder, else copied, into
    ``<project>_assets/links/<kind>/``. ``undo()`` puts things back if the clip cannot be added."""
    """Media for a new linked clip, inside <project>_assets/links/<kind>/ (moved from temp, else copied)."""
    lm = _lm()
    root = lm.links_root()
    if path_is_under(path, root):
        return path, (lambda: None)
    folder = lm.links_dir(kind)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, os.path.basename(path))
    stem, ext = os.path.splitext(target)
    n = 2
    while os.path.exists(target):
        target = "%s-%d%s" % (stem, n, ext)
        n += 1
    moved = path_is_under(path, tempfile.gettempdir())
    try:
        if moved:
            shutil.move(path, target + ".partial")
        else:
            shutil.copy2(path, target + ".partial")
        os.replace(target + ".partial", target)
    except OSError as exc:
        raise ToolError(f"could not bring {os.path.basename(path)} into the project's links folder: {exc}") from None

    def undo():
        try:
            if moved:
                shutil.move(target, path)  # the caller's file is back where it was (a retry finds it)
            else:
                os.unlink(target)
        except OSError:
            log.warning("could not put %s back after a refused import", target, exc_info=True)

    return target, undo


@editor_tool(
    "import_linked_media_tool",
    label="Import linked media",
    schema=obj({
        "path": string("The rendered media file (ProRes 4444 .mov with alpha, H.264 .mp4, qtrle .mov; a WebM is "
                       "re-encoded to ProRes 4444 because Zenvi drops WebM alpha)."),
        "link": mapping("The source link (zenvi_link): {kind: 'aftereffects'|'remotion'|'hyperframes', source: "
                        "{project_dir, entry, composition, composition_key, file, line, aep}, props: {...}, render: "
                        "{codec, width, height, fps, duration_frames}}. Never use keys named path/image/resource."),
        "position": nullable(number("Timeline second where the clip starts. Empty = the playhead.", minimum=0)),
        "track": string("UI track number (1 = bottom), name or layer. Empty = the lowest free track above the "
                        "video (a new 'Linked' track when none is free).", ""),
        "name": string("Name in Project Files. Empty = the composition name.", ""),
    }, required=["path", "link"]),
    background_safe=True,
    covers=("handoff.linked_import",),
)
def import_linked_media(path, link, position=None, track="", name=""):
    """Add a rendered file that keeps a live link to its source as a linked clip, as one undo step.

    Use for "bring this After Effects comp into Zenvi" after ae_render_for_zenvi
    rendered it, or any render whose source should stay editable (Remotion and
    HyperFrames imports use their own tools). The media is moved (from the temp
    folder) or copied into the project's links folder, added to Project Files
    with the link and placed at position (default the playhead) on the lowest
    free track above the video. Later: get_linked_clip_tool (freshness),
    update_linked_clip_tool / rerender_linked_clip_tool, open_linked_source_tool,
    unlink_clip_tool. One undo step removes the clip and the file.
    Example: {"path": "/tmp/Promo.mov", "link": {"kind": "aftereffects", "source":
    {"aep": "/Users/me/promo.aep", "composition": "Promo", "composition_key": 12}}}.
    """
    lm = _lm()
    src = os.path.abspath(os.path.expanduser(str(path or "").strip()))
    if not os.path.isfile(src):
        raise ToolError(f"no media file at {path!r}")
    try:
        stored = lm.normalize_link(link or {})
        # refuse a bad placement before the caller's file is moved anywhere
        lm.precheck_placement(position, str(track or ""))
    except lm.LinkError as exc:
        raise _link_error(exc) from None
    kind = stored["kind"]
    provider = lm.provider_for(kind)
    if provider is not None and not (stored.get("render") or {}).get("fingerprint"):
        # without a fingerprint the clip could never read "stale" (e.g. a comp rendered by AE)
        try:
            stored["render"] = dict(stored.get("render") or {},
                                    fingerprint=provider.fingerprint(lm.read_link({lm.LINK_KEY: stored})))
        except lm.SourceMissing as exc:
            raise _link_error(exc) from None
        except Exception:
            log.warning("could not fingerprint the %s source at import", kind, exc_info=True)
    if src.lower().endswith(".webm"):
        media = _staged_webm(src, kind)

        def undo():
            try:
                os.unlink(media)
            except OSError:
                pass
    else:
        media, undo = _adopt_media(src, kind)
    try:
        receipt = lm.add_linked_media(media, stored, position=position, track=str(track or ""), name=str(name or ""))
    except Exception as exc:
        from classes.editor_tools.titles_text_common import CommitTimeout
        if not isinstance(exc, CommitTimeout):  # it may still land: then the file is in use
            undo()
        if isinstance(exc, lm.LinkError):
            raise _link_error(exc) from None
        raise
    where = f"{receipt['position']:.2f}-{receipt['end']:.2f}s"
    return ok(f"Added linked {lm.kind_label(kind)} clip {receipt['name']!r} at {where}"
              + (" on a new track" if receipt["new_track"] else "") + ".", **receipt)


def _link_report(file_id: str, *, compute: bool = True) -> dict:
    lm = _lm()
    f = _linked_file(file_id)
    try:
        link = lm.read_link(f.data) or {}
    except lm.LinkError as exc:
        raise ToolError(f"the clip's link data is damaged ({exc}); unlink_clip_tool keeps its media") from None
    check = lm.check_link(f.data, compute=compute)
    provider = lm.provider_for(link.get("kind", ""))
    editable: Dict[str, Any] = {}
    if provider is not None:
        try:
            editable = dict(provider.editable_props(link) or {})
        except Exception as exc:
            log.warning("editable_props failed for %s", file_id, exc_info=True)
            editable = {"_error": str(exc)}
    return {
        "file_id": f.id, "name": f.data.get("name") or os.path.basename(str(f.data.get("path") or "")),
        "path": f.data.get("path"), "kind": link.get("kind"), "kind_label": lm.kind_label(str(link.get("kind"))),
        "state": check.state if check else None, "detail": check.detail if check else "",
        "fingerprint": check.fingerprint if check else None,
        "rendered_fingerprint": check.stored_fingerprint if check else None,
        "source": link.get("source"), "props": link.get("props"), "render": link.get("render"),
        "provider_available": provider is not None, "can_open_studio": lm.supports_studio(str(link.get("kind"))),
        "editable_props": editable, "clips": _clip_rows(f.id),
    }


@editor_tool(
    "get_linked_clip_tool",
    label="Linked clip info",
    schema=obj(dict(_FILE_TARGET)),
    read_only=True,
    covers=("handoff.linked_info",),
)
def get_linked_clip(clip_id="", file_id=""):
    """Read a linked clip's source link, props, render settings and whether it is up to date.

    Use before editing or re-rendering a linked clip (Remotion / HyperFrames
    composition or After Effects comp) or when the user asks "is this clip up
    to date?". state is fresh (rendered from the current source), stale (the
    code / comp / props changed: rerender_linked_clip_tool), rendering, error
    (the last render failed; detail says why) or missing_source (the project
    folder or .aep is gone). Also lists editable_props with current values and
    the timeline clips that use the file. Read-only.
    """
    fid = _resolve_file_id(clip_id, file_id)
    report = _link_report(fid)
    return ok(f"{report['kind_label']} clip {report['name']!r} is {report['state']}"
              + (f": {report['detail']}" if report["detail"] else "") + ".", **report)


def _merged_props(file_id: str, props: Optional[dict]) -> dict:
    lm = _lm()
    current = lm.link_props(lm.read_link(_linked_file(file_id).data) or {})
    return dict(current, **(props or {}))


@editor_tool(
    "update_linked_clip_tool",
    label="Update linked clip",
    schema=obj({
        **_FILE_TARGET,
        "props": mapping("Props to change, merged into the current ones, e.g. {'title': 'Launch day', "
                         "'accent': '#FF5A36'}. Names from get_linked_clip_tool editable_props."),
        "rerender": boolean("Re-render from source now (default). false = only store the props; the clip stays "
                            "stale until re-rendered.", True),
    }),
    background_safe=True,
    covers=("handoff.linked_update",),
)
def update_linked_clip(clip_id="", file_id="", props=None, rerender=True):
    """Change a linked clip's props (Remotion input props / HyperFrames variables) and re-render it.

    Use for "change the title text in the intro to Launch day", "make the
    accent orange" on a clip that came from code. Props merge into the current
    ones; the composition is re-rendered with its own toolchain (seconds to
    minutes) and every clip of the file switches to the new render as one undo
    step (a shorter render shortens clips and says so in warnings). A failed
    render changes nothing. With rerender=false only the props are stored (one
    undo step) and the clip reads stale. Example: {"clip_id": "A1B2", "props":
    {"title": "Launch day"}}.
    """
    lm = _lm()
    fid = _resolve_file_id(clip_id, file_id)
    _linked_file(fid)
    if props is not None and not isinstance(props, dict):
        raise ToolError("props must be an object of prop names to values")
    if not props and not rerender:
        raise ToolError("nothing to change: pass props, or rerender=true to re-render as is")
    if not rerender:
        try:
            res = lm.update_link(fid, {"props": _merged_props(fid, props)})
        except lm.LinkError as exc:
            raise _link_error(exc) from None
        if not res["changed"]:
            return ok("No change: the clip already has those props.", changed=False, file_id=fid,
                      props=res["link"]["props"])
        return ok("Stored the new props; the clip is stale until it is re-rendered (rerender_linked_clip_tool).",
                  changed=True, file_id=fid, props=res["link"]["props"], state="stale")
    return _rerender(fid, props or None)


def _rerender(file_id: str, props: Optional[dict]) -> str:
    lm = _lm()
    from classes.handoff import jobs
    try:
        receipt = lm.rerender_linked(file_id, props=props)
    except jobs.JobCancelled:
        raise ToolError("the render was cancelled; nothing changed") from None
    except lm.LinkError as exc:
        raise _link_error(exc) from None
    except ToolError:
        raise  # e.g. CommitTimeout: the swap may still land -- its message says not to retry
    except Exception as exc:
        raise RuntimeError(f"the render failed: {exc}. Nothing changed; get_linked_clip_tool shows the error") from None
    extra = " " + " ".join(receipt.get("warnings") or []) if receipt.get("warnings") else ""
    return ok(f"Re-rendered the linked clip ({receipt['duration']:.2f}s) and updated "
              f"{len(receipt.get('updated_clip_ids') or [])} clip(s).{extra}", **receipt)


@editor_tool(
    "rerender_linked_clip_tool",
    label="Re-render linked clip",
    schema=obj(dict(_FILE_TARGET)),
    background_safe=True,
    covers=("handoff.linked_update",),
)
def rerender_linked_clip(clip_id="", file_id=""):
    """Re-render a linked clip from its source after the code, comp or assets changed.

    Use when get_linked_clip_tool says stale, or the user says "I edited the
    Remotion composition, update it in Zenvi". Renders with the source's own
    toolchain (Remotion / HyperFrames CLI, or After Effects through Zenvi Link)
    with the stored props, then swaps the media of every clip using the file as
    one undo step. A failed or cancelled render changes nothing (the error is
    reported and get_linked_clip_tool shows it).
    """
    fid = _resolve_file_id(clip_id, file_id)
    _linked_file(fid)
    return _rerender(fid, None)


@editor_tool(
    "open_linked_source_tool",
    label="Open linked source",
    schema=obj({
        **_FILE_TARGET,
        "target": enum(["code", "studio"], "code = the source file at its line in the user's code editor "
                       "(Cursor / VS Code / Preferences > Code Editor); studio = Remotion Studio / HyperFrames "
                       "preview / After Effects.", "code"),
    }),
    read_only=True,
    covers=("handoff.linked_open",),
)
def open_linked_source(clip_id="", file_id="", target="code"):
    """Open a linked clip's source for the user: the code file at its line, or the composition's studio.

    Use for "show me the code for this intro", "open it in Remotion Studio",
    "open the After Effects project". Opens an app on the user's screen; the
    project is not changed. After they edit, rerender_linked_clip_tool brings
    the change in.
    """
    lm = _lm()
    from classes.handoff import open_source
    fid = _resolve_file_id(clip_id, file_id)
    link = lm.read_link(_linked_file(fid).data) or {}
    kind = str(link.get("kind") or "")
    provider = lm.provider_for(kind)
    source = link.get("source") or {}
    try:
        if target == "studio":
            if provider is None or not lm.supports_studio(kind):
                raise ToolError(f"{lm.kind_label(kind)} links have no studio to open here; use target='code'")
            provider.open_studio(link)
            return ok(f"Opened {lm.kind_label(kind)} studio for {source.get('composition') or 'the clip'}.",
                      file_id=fid, target="studio", kind=kind)
        if provider is not None:
            provider.open_source(link)
            return ok(f"Opened the {lm.kind_label(kind)} source.", file_id=fid, target="code", kind=kind)
        # no provider in this build: open the recorded file directly
        file_rel, root = source.get("file"), source.get("project_dir")
        path = os.path.join(root, file_rel) if root and file_rel else (source.get("aep") or root)
        if not path:
            raise ToolError("this link records no source file to open")
        opened = open_source.open_in_editor(path, source.get("line"))
        return ok(f"Opened {os.path.basename(path)} in {opened['editor']}.", file_id=fid, target="code", kind=kind,
                  file=opened["file"], line=opened["line"])
    except (lm.LinkError, open_source.EditorError) as exc:
        raise _link_error(exc) from None


@editor_tool(
    "unlink_clip_tool",
    label="Unlink clip",
    schema=obj(dict(_FILE_TARGET)),
    covers=("handoff.linked_unlink",),
)
def unlink_clip(clip_id="", file_id=""):
    """Drop a linked clip's source link and keep its rendered media as an ordinary file.

    Use for "stop linking this to the Remotion project", "make it a normal
    clip". Every clip of the file keeps playing the last render; it can no
    longer be re-rendered or opened at its source. Never deletes clips or
    media. One undo step restores the link.
    """
    lm = _lm()
    fid = _resolve_file_id(clip_id, file_id)
    _linked_file(fid)
    try:
        res = lm.unlink(fid)
    except lm.LinkError as exc:
        raise _link_error(exc) from None
    return ok(f"Unlinked the {lm.kind_label(str(res.get('kind')))} clip; it keeps its rendered media.", **res)


# ---------------------------------------------------------------------------
# The handoff packages' tools
# ---------------------------------------------------------------------------

PACKAGE_TOOL_MODULES = ("handoff_after_effects", "handoff_premiere", "handoff_remotion", "handoff_hyperframes")
_package_tool_errors: Dict[str, str] = {}


def _package_failed(name: str, exc: BaseException) -> None:
    """A broken package must not take every editor tool down with it: log it and leave it out.

    tests/test_editor_tools_handoff.py imports each present package module
    directly, so a broken one still fails the suite loudly.
    """
    _package_tool_errors[name] = "%s: %s" % (type(exc).__name__, exc)
    log.error("Handoff tools %s failed to load; its tools are unavailable", name, exc_info=True)


def package_tool_errors() -> Dict[str, str]:
    """Package tool modules that failed to import this session, with the error."""
    return dict(_package_tool_errors)


def _load_package_tools() -> None:
    """Import the handoff packages' tool modules that exist in this build (written out for frozen builds)."""
    try:
        import classes.editor_tools.handoff_after_effects  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "classes.editor_tools.handoff_after_effects":
            _package_failed("handoff_after_effects", exc)
    except Exception as exc:
        _package_failed("handoff_after_effects", exc)
    try:
        import classes.editor_tools.handoff_premiere  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "classes.editor_tools.handoff_premiere":
            _package_failed("handoff_premiere", exc)
    except Exception as exc:
        _package_failed("handoff_premiere", exc)
    try:
        import classes.editor_tools.handoff_remotion  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "classes.editor_tools.handoff_remotion":
            _package_failed("handoff_remotion", exc)
    except Exception as exc:
        _package_failed("handoff_remotion", exc)
    try:
        import classes.editor_tools.handoff_hyperframes  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "classes.editor_tools.handoff_hyperframes":
            _package_failed("handoff_hyperframes", exc)
    except Exception as exc:
        _package_failed("handoff_hyperframes", exc)


_load_package_tools()
