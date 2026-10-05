"""List and import Remotion projects (the tools and File > Import Project > Remotion Project...).

* :func:`list_compositions` -- the project's compositions with size, fps,
  duration, default props and where their code lives (helper ``compositions``
  for the numbers; the static scan for file:line). When ``node_modules`` is
  missing only the static scan is available (``installed: false``).
* :func:`import_project` -- renders the chosen compositions with the
  project's own Remotion (linked clips, SPEC §3.6) and/or restores a
  Zenvi-generated project's timeline natively, then adds everything in ONE
  undo step: the clips land together at the playhead on free tracks above
  the video, opaque renders below transparent ones. Placement is validated
  before anything renders; renders that never made it into the project are
  deleted (except one whose commit timed out: it may still land). The
  clips go into the project that was open when the import started; if
  another one was opened meanwhile, nothing is added. If adding stops
  part-way, :class:`PartialImport` says exactly what was added.

Reading compositions runs the project's own code (remotion.config.*, the
bundle, Chrome) -- like ``npx remotion compositions``. ``static=True``
lists them from the code without running anything (the import dialog asks
whether to trust the project first).

Blocking (Node, renders, disk): call off the GUI thread.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from classes.handoff import linked_media
from classes.handoff.linked_media import LinkError
from classes.handoff.remotion import detect, helper, provider, restore, sources
from classes.logger import log

ProgressFn = Callable[[Optional[float], str], None]


@dataclass(frozen=True)
class Composition:
    """One composition of a Remotion project."""

    id: str
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]
    duration_frames: Optional[int]
    default_props: Dict[str, Any] = field(default_factory=dict)
    kind: str = "composition"                      # composition | still
    source: Optional[sources.CompositionSource] = None

    @property
    def seconds(self) -> Optional[float]:
        if not self.fps or not self.duration_frames:
            return None
        return self.duration_frames / float(self.fps)

    def as_dict(self) -> dict:
        out: Dict[str, Any] = {"id": self.id, "kind": self.kind, "width": self.width, "height": self.height,
                               "fps": self.fps, "duration_frames": self.duration_frames,
                               "duration": round(self.seconds, 3) if self.seconds is not None else None,
                               "default_props": self.default_props}
        if self.source is not None:
            out.update(file=self.source.best_file, line=self.source.best_line, folder=self.source.folder)
        else:
            out.update(file=None, line=None, folder=None)
        return out


@dataclass
class Listing:
    project: detect.RemotionProject
    compositions: List[Composition]
    warnings: List[str] = field(default_factory=list)
    static_only: bool = False

    def by_id(self) -> Dict[str, Composition]:
        return {c.id: c for c in self.compositions}

    def as_dict(self) -> dict:
        return {"project": self.project.as_dict(), "compositions": [c.as_dict() for c in self.compositions],
                "static_only": self.static_only, "warnings": list(self.warnings)}


def list_compositions(project_dir: str, *, on_progress: Optional[ProgressFn] = None,
                      should_cancel: Optional[Callable[[], bool]] = None, allow_static: bool = False,
                      static: bool = False) -> Listing:
    """The project's compositions. Without node_modules: LinkError, or (allow_static) the static scan only.

    *static*: never run the project's code -- ids and code locations from the
    static scan only (``static_only``), whether or not it is installed.
    """
    project = detect.inspect_project(project_dir)
    scanned = sources.scan_project(project.root, project.entry)
    if static:
        comps = [Composition(id=s.id, width=None, height=None, fps=None, duration_frames=None,
                             kind=s.kind, source=s) for s in scanned.values()]
        return Listing(project, comps, [], static_only=True)
    if not project.installed or not project.entry:
        if not allow_static:
            detect.require_ready(project)
        comps = [Composition(id=s.id, width=None, height=None, fps=None, duration_frames=None,
                             kind=s.kind, source=s) for s in scanned.values()]
        why = (f"run `{project.install_command()}` in {project.root} to read sizes, durations and props"
               if project.entry else "the project has no entry point")
        return Listing(project, comps, [f"read from the code only: {why}"], static_only=True)
    detect.require_ready(project)
    bundle = helper.bundle_dir(project.root, project.entry or "", provider.sources_fingerprint(project), project.version)
    run = helper.run_helper("compositions", project_dir=project.root, entry=project.entry,
                            options={"bundle": bundle}, on_progress=on_progress, should_cancel=should_cancel)
    comps = []
    for c in run.result.get("compositions") or []:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        scan = scanned.get(cid)
        frames = int(c.get("durationInFrames") or 0)
        comps.append(Composition(
            id=cid, width=int(c.get("width") or 0), height=int(c.get("height") or 0), fps=float(c.get("fps") or 30),
            duration_frames=frames, default_props=dict(c.get("defaultProps") or {}),
            kind="still" if (scan is not None and scan.kind == "still") or frames == 1 else "composition",
            source=scan))
    return Listing(project, comps, list(run.warnings))


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@dataclass
class ImportPlan:
    project: detect.RemotionProject
    native: bool
    linked: List[Composition]
    props: Dict[str, dict]
    timeline: Optional[dict] = None
    warnings: List[str] = field(default_factory=list)


def _clean_ids(compositions: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    for c in compositions or []:
        cid = str(c or "").strip()
        if cid and cid not in out:
            out.append(cid)
    return out


def plan_import(project_dir: str, *, compositions: Optional[Sequence[str]] = None, props: Optional[dict] = None,
                codec: str = "auto", restore_native: bool = True, listing: Optional[Listing] = None,
                on_progress: Optional[ProgressFn] = None,
                should_cancel: Optional[Callable[[], bool]] = None) -> ImportPlan:
    """Check everything an import needs before anything renders (LinkError says what is wrong)."""
    codec = str(codec or "auto").strip().lower()
    if codec not in provider.CODEC_CHOICES:
        raise LinkError(f"codec must be one of {', '.join(provider.CODEC_CHOICES)}, got {codec!r} (never WebM: "
                        "libopenshot drops its alpha)")
    if props is not None and not isinstance(props, dict):
        raise LinkError('props must be an object of composition id -> props, e.g. {"TitleCard": {"title": "Hi"}}')
    for cid, value in (props or {}).items():
        if not isinstance(value, dict):
            raise LinkError(f"props for {cid!r} must be an object of prop names to values")
    project = listing.project if listing is not None else detect.inspect_project(project_dir)
    wanted = _clean_ids(compositions)
    zenvi_id = detect.ZENVI_COMPOSITION
    native = bool(restore_native and project.is_zenvi_generated and (not wanted or zenvi_id in wanted))
    timeline = None
    warnings: List[str] = []
    if native:
        timeline = restore.load_timeline(project.zenvi_timeline or restore.timeline_path(project.root))
        if (props or {}).get(zenvi_id):
            warnings.append("props for ZenviTimeline are ignored when it is restored as native clips")
    linked_ids = [c for c in wanted if not (native and c == zenvi_id)]
    need_listing = bool(linked_ids) or (not wanted and not native)
    linked: List[Composition] = []
    if need_listing:
        if listing is None:
            listing = list_compositions(project.root, on_progress=on_progress, should_cancel=should_cancel)
        warnings += listing.warnings
        by_id = listing.by_id()
        if not wanted:
            linked = [c for c in listing.compositions if not (native and c.id == zenvi_id)]
        else:
            unknown = [c for c in linked_ids if c not in by_id]
            if unknown:
                raise LinkError(f"{project.name} has no composition {', '.join(repr(u) for u in unknown)}; it has "
                                f"{', '.join(sorted(by_id)) or 'none'} (list_remotion_compositions_tool)")
            linked = [by_id[c] for c in linked_ids]
        if any(c.width is None for c in linked):
            detect.require_ready(project)
    known = {c.id for c in linked} | ({zenvi_id} if native else set())
    stray = sorted(set(props or {}) - known)
    if stray:
        raise LinkError(f"props name composition(s) {', '.join(stray)} that are not being imported; import them too "
                        "or drop their props")
    if not linked and not native:
        raise LinkError(f"{project.name} has no compositions to import")
    return ImportPlan(project=project, native=native, linked=linked,
                      props={k: dict(v) for k, v in (props or {}).items()}, timeline=timeline, warnings=warnings)


def _resolve_position(position: Optional[float]) -> float:
    """The timeline second the clips land at: *position*, or the playhead when the call starts (snapped).

    Reading the playhead once, before a render of a minute or more, puts the
    clips where the user asked even if they move the playhead meanwhile.
    """
    if position is not None:
        if float(position) < 0:
            raise LinkError("position must be 0 or later (timeline seconds)")
        return float(position)

    def _playhead():
        from classes.editor_tools._base import playhead_seconds, snap_seconds
        return snap_seconds(playhead_seconds())

    from classes.editor_tools._base import ToolError
    from classes.editor_tools.titles_text_common import precheck_on_main
    try:
        return float(precheck_on_main(_playhead) or 0.0)
    except ToolError as exc:
        raise LinkError(str(exc)) from None


def _precheck_placement(plan: ImportPlan, position: float, track: str) -> None:
    """Refuse a bad track / window before anything renders (C1's precheck; read-only GUI hop)."""
    if str(track or "").strip() and (len(plan.linked) > 1 or (plan.linked and plan.native)):
        raise LinkError("track can only be given when importing one composition; leave it empty and Zenvi stacks the "
                        "clips on free tracks above the video")
    duration = None
    if len(plan.linked) == 1:
        duration = plan.linked[0].seconds or provider.STILL_SECONDS
    linked_media.precheck_placement(position, track or None, duration)


def _scaled(report: ProgressFn, lo: float, hi: float) -> ProgressFn:
    def _report(fraction: Optional[float], message: str) -> None:
        report(None if fraction is None else lo + (hi - lo) * max(0.0, min(1.0, fraction)), message)
    return _report


def _discard(paths: List[str]) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            log.debug("could not remove unused render %s", path, exc_info=True)


class PartialImport(LinkError):
    """Adding stopped part-way: ``receipt`` lists what is in the project (one undo step) and what may still land."""

    def __init__(self, message: str, receipt: dict):
        super().__init__(message)
        self.receipt = receipt


def _project_identity() -> Tuple[str, str]:
    """(project id, file path) of the open project, read on the GUI thread."""
    def _read():
        from classes.editor_tools._base import get_app
        project = get_app().project
        return str(project.get("id") or ""), str(getattr(project, "current_filepath", "") or "")

    from classes.editor_tools._base import ToolError
    from classes.editor_tools.titles_text_common import precheck_on_main
    try:
        found = precheck_on_main(_read)
    except ToolError as exc:
        raise LinkError(str(exc)) from None
    return (str(found[0]), str(found[1])) if isinstance(found, (tuple, list)) and len(found) == 2 else ("", "")


def _end_batch() -> None:
    """The preview refresh skipped by ``ignore_refresh`` adds, when the batch stopped before its last clip."""
    try:
        from classes.editor_tools.titles_text_common import end_clip_batch, precheck_on_main
        precheck_on_main(end_clip_batch)
    except Exception:
        log.debug("could not refresh the preview after a partial Remotion import", exc_info=True)


def _is_commit_timeout(exc: BaseException) -> bool:
    from classes.editor_tools.titles_text_common import CommitTimeout
    return isinstance(exc, CommitTimeout)


def import_project(project_dir: str, *, compositions: Optional[Sequence[str]] = None, props: Optional[dict] = None,
                   codec: str = "auto", position: Optional[float] = None, track: str = "",
                   restore_native: bool = True, listing: Optional[Listing] = None,
                   on_progress: Optional[ProgressFn] = None,
                   should_cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Render and place compositions as linked clips (and/or restore a Zenvi export natively): ONE undo step."""
    from classes.handoff.jobs import JobCancelled
    report = on_progress or (lambda _f, _m: None)
    cancel = should_cancel or (lambda: False)
    plan = plan_import(project_dir, compositions=compositions, props=props, codec=codec,
                       restore_native=restore_native, listing=listing, on_progress=_scaled(report, 0.0, 0.1),
                       should_cancel=cancel)
    position = _resolve_position(position)
    _precheck_placement(plan, position, track)
    identity = _project_identity()
    project = plan.project
    rendered: List[Tuple[Composition, str, dict]] = []
    added: List[str] = []
    in_flight: Optional[str] = None
    batch_open = False  # clips placed without a preview refresh, waiting for the last one's
    native_receipt = None
    native_started = False
    clips: List[dict] = []
    total = max(1, len(plan.linked))
    try:
        for i, comp in enumerate(plan.linked):
            if cancel():
                raise JobCancelled("Remotion import cancelled")
            user_props = plan.props.get(comp.id) or {}
            link = provider.make_link(project, comp.id, props=user_props, codec=codec, defaults=comp.default_props,
                                      source=comp.source)
            label = f"Rendering {comp.id} ({i + 1} of {total})"
            lo, hi = 0.1 + 0.85 * i / total, 0.1 + 0.85 * (i + 1) / total
            path, completed = linked_media.render_link(
                link, on_progress=lambda f, m, lo=lo, hi=hi, label=label: report(
                    None if f is None else lo + (hi - lo) * f, f"{label}: {m}" if m else label),
                should_cancel=cancel)
            rendered.append((comp, path, completed))
        for _comp, path, _completed in rendered:  # every render must be readable before anything changes
            linked_media.probe_media(path)
        if cancel():
            raise JobCancelled("Remotion import cancelled")
        if _project_identity() != identity:
            raise LinkError("another project was opened while Remotion was rendering, so nothing was added; import "
                            "again into the project you want the clips in")
        report(0.96, "Adding the clips")
        # opaque renders first: they take the lower tracks, transparent titles stack above them
        ordered = sorted(rendered, key=lambda r: 0 if (r[2].get("render") or {}).get("codec") == "h264" else 1)
        from classes.editor_tools._base import get_app
        from classes.updates import nested_transaction
        with nested_transaction(get_app().updates):
            if plan.native and plan.timeline is not None:
                native_started = True
                native_receipt = restore.insert_native(plan.timeline, project.root, position=position)
            for index, (comp, path, completed) in enumerate(ordered):
                stored = {k: v for k, v in completed.items() if k != "warnings"}
                in_flight = path
                last = index == len(ordered) - 1
                batch_open = batch_open or not last
                # one preview refresh for the batch: the last clip's (end_clip_batch if it stops early)
                receipt = linked_media.add_linked_media(path, stored, position=position, track=track or None,
                                                        name=comp.id, ignore_refresh=not last)
                in_flight = None
                batch_open = batch_open and not last
                added.append(path)
                render = completed.get("render") or {}
                clips.append({
                    "composition": comp.id, "file_id": receipt["file_id"],
                    "timeline_clip_id": receipt["timeline_clip_id"], "position": receipt["position"],
                    "end": receipt["end"], "duration": receipt["duration"], "layer": receipt["layer"],
                    "new_track": receipt["new_track"], "codec": render.get("codec"), "width": render.get("width"),
                    "height": render.get("height"), "fps": render.get("fps"),
                    "duration_frames": render.get("duration_frames"), "output": render.get("output"),
                    "props": linked_media.link_props(completed),
                    "source": {k: (completed.get("source") or {}).get(k) for k in ("file", "line", "folder")},
                    "warnings": list(completed.get("warnings") or []),
                })
    except BaseException as exc:
        timed_out = _is_commit_timeout(exc)
        keep = set(added) | ({in_flight} if in_flight and timed_out else set())
        _discard([p for _c, p, _l in rendered if p not in keep])
        if batch_open:
            _end_batch()
        pending = [c.id for c, p, _l in rendered if p == in_flight] if timed_out else []
        landed_native = native_receipt is not None
        if not (added or landed_native or pending or (timed_out and native_started)):
            raise
        names = [c["composition"] for c in clips]
        parts = []
        if landed_native:
            parts.append(f"restored the Zenvi timeline ({len((native_receipt or {}).get('clips') or [])} clip(s))")
        if names:
            parts.append(f"added {', '.join(names)}")
        if pending:
            parts.append(f"{', '.join(pending)} may still be added (the editor was busy)")
        elif timed_out and native_started and not landed_native:
            parts.append("the restored timeline may still be added (the editor was busy)")
        summary = "; ".join(parts)
        log.warning("Remotion import stopped part-way (%s): %s", summary, exc)
        receipt_so_far = {"project_dir": project.root, "project": project.name, "native": native_receipt,
                          "linked": clips, "pending": pending, "error": str(exc)}
        undo = ("What was added is one undo step (Edit > Undo removes it)" if names or landed_native
                else "If it lands, it is one undo step")
        raise PartialImport(f"the Remotion import stopped part-way: {summary}, then {exc}. {undo}; check the "
                            "timeline before importing again", receipt_so_far) from exc
    warnings = list(plan.warnings)
    if native_receipt:
        warnings += native_receipt.get("warnings") or []
    for c in clips:
        warnings += c.pop("warnings")
    report(1.0, "Imported")
    return {"project_dir": project.root, "project": project.name, "entry": project.entry,
            "remotion_version": project.version, "native": native_receipt, "linked": clips,
            "warnings": [w for i, w in enumerate(warnings) if w and w not in warnings[:i]]}


__all__ = ["Composition", "Listing", "ImportPlan", "PartialImport", "list_compositions", "plan_import",
           "import_project"]
