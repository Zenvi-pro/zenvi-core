"""Bring a HyperFrames project into the open Zenvi project, as ONE undo step.

Two halves, both blocking (run them off the GUI thread):

* :func:`inspect_project` reads the project (HyperFrames' own resolved
  timeline from ``hyperframes timeline --json`` when Node is there, Zenvi's
  parser otherwise), measures its media and decides what each part becomes:

  - ``native``: every ``<video>`` / ``<img>`` / ``<audio>`` of the root is a
    Zenvi clip (placement from its CSS, parsed GSAP tweens as keyframes,
    volume / fades / automation as volume keyframes) on tracks mapped from
    ``data-track-index``; each nested composition is a linked clip rendered
    on its own (transparent); what else the root draws (titles, shapes,
    scripted DOM) is one linked *layer* -- or two, when some of it paints
    under the media and some over it;
  - ``flatten``: the whole project is one linked clip (HyperFrames' own render);
  - ``auto``: a Zenvi export is restored; otherwise ``native``, unless the
    root has no media primitive or a primitive has animation or layout
    Zenvi cannot rebuild exactly -- then ``flatten`` (``reason`` says why);
  - a project Zenvi exported (``zenvi-timeline`` JSON) comes back
    losslessly (:mod:`.restore`); whatever was added in HyperFrames since
    comes in as above.

* :func:`run_import` renders the linked parts (the provider; nothing is
  committed until every render succeeded), probes everything, then adds
  tracks, files and clips in ONE GUI-thread hop. Without *track* the
  import gets new tracks above the existing ones (an empty timeline reuses
  its tracks from the bottom); with *track* its first track lands there and
  the rest on the tracks above it, which must be free.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from classes.handoff import linked_media as lm
from classes.handoff.hyperframes import cli as hf_cli
from classes.handoff.hyperframes import mapping
from classes.handoff.hyperframes import parser as hfp
from classes.handoff.hyperframes import provider as hfprov
from classes.handoff.hyperframes import restore as hfrestore
from classes.logger import log

MODES = ("auto", "native", "flatten")
TRACK_STEP = 1000000
TRACK_LABEL = "HyperFrames"
ProgressFn = Callable[[Optional[float], str], None]


# ---------------------------------------------------------------------------
# Media measurement (ffprobe; off the GUI thread)
# ---------------------------------------------------------------------------

def svg_size(path: str) -> Tuple[int, int]:
    """(width, height) of an SVG from its width / height or viewBox (0, 0 when unknown)."""
    import re
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return 0, 0

    def length(value: Optional[str]) -> float:
        m = re.match(r"^\s*([\d.]+)\s*(px)?\s*$", value or "")
        return float(m.group(1)) if m else 0.0
    w, h = length(root.get("width")), length(root.get("height"))
    if not (w and h):
        box = [float(v) for v in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if v][:4]
        if len(box) == 4:
            w, h = (w or box[2]), (h or box[3])
    return int(round(w)), int(round(h))


def ffprobe_media(path: str) -> dict:
    """{width, height, duration, frames, has_video, has_audio} of a media file (zeros when unreadable)."""
    from classes import ffmpeg_cli
    out = {"width": 0, "height": 0, "duration": 0.0, "frames": 0, "has_video": False, "has_audio": False}
    if path and str(path).lower().endswith((".svg", ".svgz")) and os.path.isfile(path):
        w, h = svg_size(path)
        out.update(width=w, height=h, has_video=bool(w and h))
        return out
    exe = ffmpeg_cli.find_ffmpeg("ffprobe")
    if not exe or not path or not os.path.isfile(path):
        return out
    try:
        proc = ffmpeg_cli.run_ffmpeg([exe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                                     capture_output=True, text=True, timeout=60)
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        log.warning("ffprobe failed for %s", path, exc_info=True)
        return out
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and not out["has_video"]:
            out["has_video"] = True
            out["width"], out["height"] = int(s.get("width") or 0), int(s.get("height") or 0)
            try:
                out["frames"] = int(s.get("nb_frames") or 0)
            except (TypeError, ValueError):
                out["frames"] = 0
            if out["duration"] <= 0:
                try:
                    out["duration"] = float(s.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
        elif s.get("codec_type") == "audio":
            out["has_audio"] = True
    try:
        out["duration"] = float((data.get("format") or {}).get("duration") or out["duration"] or 0.0)
    except (TypeError, ValueError):
        pass
    return out


def _probe_duration(path: str) -> Optional[float]:
    return ffprobe_media(path)["duration"] or None


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """One thing the import places: a native clip or a linked render."""

    kind: str                       # native | linked
    name: str
    start: float                    # HyperFrames timeline seconds
    lane: Tuple[int, int]           # (group: -1 under the media, 0, +1 over it; data-track-index)
    order: int = 0                  # document order (ties on a lane)
    clip: Optional[hfp.Clip] = None
    plan: Optional[mapping.NativePlan] = None
    media: Optional[dict] = None
    link: Optional[dict] = None
    role: str = ""
    expected_duration: Optional[float] = None
    trim: Tuple[float, Optional[float]] = (0.0, None)   # window inside a linked render (start, end)

    @property
    def end(self) -> float:
        if self.plan is not None:
            return self.start + self.plan.duration
        if self.expected_duration:
            return self.start + self.expected_duration
        return self.start


@dataclass
class Inspection:
    project_dir: str
    project: hfp.Project
    requested: str
    mode: str                       # native | flatten | restore
    reason: str
    items: List[Item] = field(default_factory=list)
    restore: Optional[hfrestore.Restore] = None
    warnings: List[str] = field(default_factory=list)
    problems: Dict[str, List[str]] = field(default_factory=dict)
    cli_version: Optional[str] = None
    fps: float = 30.0

    @property
    def native(self) -> List[Item]:
        return [i for i in self.items if i.kind == "native"]

    @property
    def linked(self) -> List[Item]:
        return [i for i in self.items if i.kind == "linked"]

    def summary(self) -> dict:
        root = self.project.root
        return {
            "project_dir": self.project_dir, "composition": root.id, "width": root.width, "height": root.height,
            "duration": root.duration, "mode": self.mode, "requested_mode": self.requested, "reason": self.reason,
            "media_clips": len(self.native), "linked_clips": len(self.linked),
            "compositions": sum(1 for i in self.linked if i.role == "composition"),
            "layers": sum(1 for i in self.linked if i.role == "layer"),
            "restored_clips": self.restore.clip_count if self.restore else 0,
            "renders_needed": len(self.linked), "zenvi_export": self.project.zenvi_timeline is not None,
            "timing_from": "hyperframes" if self.project.cli_timeline_used else "zenvi",
            "hyperframes_cli": self.cli_version, "problems": self.problems, "warnings": self.warnings,
        }


def _fps_text(fps: float) -> str:
    frac = Fraction(fps).limit_denominator(1001)
    return str(frac.numerator) if frac.denominator == 1 else "%d/%d" % (frac.numerator, frac.denominator)


def _base_link(project: hfp.Project, *, role: str, entry: str, composition: str, file: str, line: Optional[int],
               props: Optional[dict], block: dict, fps: float) -> dict:
    data = {"role": role, "fps": _fps_text(fps)}
    data.update(block)
    return {"kind": hfprov.KIND,
            "source": {"project_dir": project.root_dir, "entry": entry, "composition": composition, "file": file,
                       "line": line},
            "props": dict(props or {}), hfprov.BLOCK: data}


def _top_child(el: hfp.Element, root: hfp.Element) -> Optional[hfp.Element]:
    node = el
    while node.parent is not None and node.parent is not root:
        node = node.parent
    return node if node.parent is root else None


def _paint_key(el: hfp.Element, comp: hfp.Composition, index: int) -> Tuple[int, int]:
    style = hfp.computed_style(el, comp.rules)
    z = 0
    if style.get("position", "").strip().lower() in ("absolute", "fixed", "relative", "sticky"):
        try:
            z = int(style.get("z-index", "0").strip())
        except ValueError:
            z = 0
    return z, index


def _paint_order(project: hfp.Project, clips: Sequence[hfp.Clip]) -> Dict[int, Tuple[int, int, int]]:
    """id(clip) -> where HyperFrames paints it: CSS z-index of its top element, then document order.

    The runtime never stacks by ``data-track-index`` (a lane in the Studio
    timeline); CSS and the DOM decide what is in front.
    """
    root = project.root
    rel = root.element
    children = [c for c in rel.children if c.tag not in hfp.NON_VISUAL_TAGS]
    index_of = {id(c): i for i, c in enumerate(children)}
    order = {id(e): i for i, e in enumerate(rel.iter())}
    out: Dict[int, Tuple[int, int, int]] = {}
    for c in clips:
        top = _top_child(c.element, rel) or c.element
        z, idx = _paint_key(top, root, index_of.get(id(top), 0))
        out[id(c)] = (z, idx, order.get(id(c.element), 0))
    return out


def _track_ranks(clips: Sequence[hfp.Clip], paint: Dict[int, Tuple[int, int, int]]) -> Dict[int, int]:
    """data-track-index -> track order bottom-up, by the paint order of each lane's lowest element."""
    low: Dict[int, Tuple[int, int, int]] = {}
    for c in clips:
        key = paint.get(id(c), (0, 0, 0))
        low[c.track_index] = min(low.get(c.track_index, key), key)
    return {t: i for i, t in enumerate(sorted(low, key=lambda t: (low[t], t)))}


def _stacking_notes(items: Sequence["Item"], paint: Dict[int, Tuple[int, int, int]]) -> List[str]:
    """Overlapping clips whose tracks stack them the other way round from HyperFrames' paint order."""
    notes = []
    group = [i for i in items if i.clip is not None and i.lane[0] == 0]
    for a in group:
        for b in group:
            if a is b or a.lane[1] >= b.lane[1]:
                continue
            if paint.get(id(a.clip), (0, 0, 0)) > paint.get(id(b.clip), (0, 0, 0)) and \
                    a.start < b.end - 1e-6 and b.start < a.end - 1e-6:
                notes.append(f"{a.name} is painted over {b.name} in HyperFrames but lands on a track under it (their "
                             "data-track-index lanes hold clips that stack both ways); move one of them to another track")
    return notes


def _layers(project: hfp.Project, natives: Sequence[hfp.Clip], hosts: Sequence[hfp.Clip]
            ) -> List[Tuple[int, List[str], Dict[str, str], List[str]]]:
    """[(group, hidden refs, signatures of the id-less ones, what it shows)] for the root's graphics left once
    *natives* / *hosts* are out."""
    root = project.root
    rel = root.element
    removed_ids = {id(c.element) for c in list(natives) + list(hosts)}
    left = hfp.remaining_visuals(root, removed_ids)
    if not left:
        return []
    children = [c for c in rel.children if c.tag not in hfp.NON_VISUAL_TAGS]
    native_tops = {id(t) for t in (_top_child(c.element, rel) for c in natives) if t is not None}
    host_tops = {id(t) for t in (_top_child(c.element, rel) for c in hosts) if t is not None}
    keys = {id(c): _paint_key(c, root, i) for i, c in enumerate(children)}
    first_native = min((keys[id(c)] for c in children if id(c) in native_tops), default=None)
    below: List[hfp.Element] = []
    above: List[hfp.Element] = []
    for c in children:
        if id(c) in host_tops or not hfp.remaining_visuals_in(root, c, removed_ids):
            continue  # a composition host renders on its own; a wrapper left empty draws nothing
        (below if first_native is not None and keys[id(c)] < first_native else above).append(c)
    if not below and not above:
        above = [rel]  # content drawn by script only
    base = [c.element for c in list(natives) + list(hosts)]
    out = []
    for group, mine, other in ((-1, below, above), (1, above, below)):
        if not mine:
            continue
        hidden = base + [o for o in other if o is not rel]
        refs = {hfprov.element_ref(e, rel): e for e in hidden}
        signatures = {ref: hfprov.element_signature(e) for ref, e in refs.items() if ref.startswith("@")}
        shows = hfp.remaining_visuals(root, removed_ids | {id(o) for o in other})
        out.append((group, sorted(refs), signatures, shows or left))
    return out


def inspect_project(project_dir: str, mode: str = "auto", *, fps: float = 30.0, canvas: Tuple[int, int] = (1920, 1080),
                    use_cli: bool = True, should_cancel: Optional[Callable[[], bool]] = None) -> Inspection:
    """Read *project_dir* and plan its import in *mode* (``auto`` / ``native`` / ``flatten``). Blocking."""
    mode = str(mode or "auto").strip().lower()
    if mode not in MODES:
        raise hfp.HyperFramesError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    root_dir = os.path.abspath(os.path.expanduser(str(project_dir or "")))
    warnings: List[str] = []
    cli_json = None
    cli_version = None
    if use_cli and os.path.isfile(os.path.join(root_dir, hfp.INDEX)):
        try:
            cli = hf_cli.resolve_cli(root_dir)
            cli_version = cli.version
            cli_json = hf_cli.timeline(root_dir, cli=cli, should_cancel=should_cancel)
        except hf_cli.CliError as exc:
            warnings.append(f"HyperFrames CLI unavailable ({exc}); Zenvi read the timing itself")
    project = hfp.load_project(root_dir, probe=_probe_duration, cli_timeline=cli_json)
    warnings.extend(project.warnings)
    root = project.root
    insp = Inspection(project_dir=root_dir, project=project, requested=mode, mode="native", reason="",
                      warnings=warnings, cli_version=cli_version, fps=float(fps))
    if project.zenvi_timeline is not None and mode != "flatten":
        insp.restore = hfrestore.plan_restore(project, target_fps=Fraction(fps).limit_denominator(1001))
        insp.mode = "restore"
        insp.reason = "a Zenvi export: its clips are restored natively from the embedded timeline"
        warnings.extend(insp.restore.warnings)
        extras = [c for c in root.clips if not c.zenvi.get("clip-id")]
        _plan_parts(insp, extras, natives_allowed=True, canvas=canvas)
        return insp
    if mode == "flatten":
        insp.mode = "flatten"
        insp.reason = "the whole project as one linked clip (as asked)"
        _plan_flatten(insp)
        return insp
    primitives = [c for c in root.clips if c.is_primitive]
    plans = _native_plans(insp, primitives, canvas)
    usable = [c for c in primitives if c.media_path]
    if mode == "auto":
        if not usable:
            insp.mode, insp.reason = "flatten", "the project has no local media clips to rebuild natively"
            _plan_flatten(insp)
            return insp
        blocked = {label: probs for label, probs in insp.problems.items()}
        if blocked:
            first = next(iter(blocked.items()))
            insp.mode = "flatten"
            insp.reason = ("%d clip(s) have animation or layout Zenvi cannot rebuild exactly (%s: %s); the project "
                           "comes in as one linked clip -- import with mode='native' to get editable clips without "
                           "those animations" % (len(blocked), first[0], first[1][0]))
            _plan_flatten(insp)
            return insp
    insp.mode = "native"
    insp.reason = ("media clips as native clips, compositions and scripted graphics as linked clips"
                   if mode == "auto" else "native clips as asked")
    _plan_parts(insp, list(root.clips), natives_allowed=True, canvas=canvas, plans=plans)
    return insp


def _native_plans(insp: Inspection, clips: Sequence[hfp.Clip], canvas: Tuple[int, int]
                  ) -> Dict[int, Tuple[mapping.NativePlan, dict]]:
    plans: Dict[int, Tuple[mapping.NativePlan, dict]] = {}
    for c in clips:
        if not c.is_primitive:
            continue
        if not c.media_path:
            insp.problems.setdefault(c.label, []).extend(c.problems or ["its media is missing"])
            continue
        media = ffprobe_media(c.media_path)
        plan = mapping.native_plan(c, insp.project.root, media, fps=insp.fps, canvas=canvas)
        plans[id(c)] = (plan, media)
        insp.warnings.extend(plan.warnings)
        if plan.problems:
            insp.problems.setdefault(c.label, []).extend(plan.problems)
    return plans


def _plan_flatten(insp: Inspection) -> None:
    project = insp.project
    root = project.root
    link = _base_link(project, role="project", entry=hfp.INDEX, composition=root.id, file=hfp.INDEX,
                      line=root.element.line, props={}, block={}, fps=insp.fps)
    name = os.path.basename(project.root_dir.rstrip(os.sep)) or root.id
    insp.items = [Item("linked", name, 0.0, (0, 0), link=link, role="project", expected_duration=root.duration)]


_CLEAR = ("", "none", "transparent", "initial", "unset", "inherit", "#000", "#000000", "black", "rgb(0, 0, 0)",
          "rgb(0,0,0)")


def _background_note(project: hfp.Project) -> Optional[str]:
    """A warning when the page / root paints a background colour (a native import has none: black)."""
    root = project.root
    for el in (next((e for e in project.index.iter() if e.tag == "html"), None),
               next((e for e in project.index.iter() if e.tag == "body"), None), root.element):
        if el is None:
            continue
        style = hfp.computed_style(el, root.rules)
        value = (style.get("background-color") or style.get("background") or "").strip().lower()
        if value and value not in _CLEAR and "url(" not in value:
            return (f"the composition's background ({value}) is not imported: Zenvi's timeline shows black behind "
                    "the clips (add a colour clip on the bottom track to match)")
    return None


def _plan_parts(insp: Inspection, clips: Sequence[hfp.Clip], *, natives_allowed: bool, canvas: Tuple[int, int],
                plans: Optional[Dict[int, Tuple[mapping.NativePlan, dict]]] = None) -> None:
    project = insp.project
    root = project.root
    note = _background_note(project)
    if note and any(c.is_primitive for c in clips):
        insp.warnings.append(note)
    plans = plans if plans is not None else _native_plans(insp, [c for c in clips if c.is_primitive], canvas)
    placed_clips = [c for c in clips if (c.is_primitive and natives_allowed and id(c) in plans)
                    or c.kind == "composition"]
    paint = _paint_order(project, placed_clips)
    # a Zenvi export maps data-track-index to its own tracks; anything else stacks by paint order
    ranks = _track_ranks(placed_clips, paint) if insp.restore is None else {}
    natives: List[hfp.Clip] = []
    hosts: List[hfp.Clip] = []
    for order, c in enumerate(root.clips):
        if c not in clips:
            if c.is_primitive and c.zenvi.get("clip-id"):
                natives.append(c)  # a restored clip: keep it out of any layer render
            continue
        if c.is_primitive and natives_allowed and id(c) in plans:
            plan, media = plans[id(c)]
            insp.items.append(Item("native", c.id or os.path.basename(c.media_path or c.src), plan.position,
                                   (0, ranks.get(c.track_index, c.track_index)), order, clip=c, plan=plan,
                                   media=media))
            natives.append(c)
        elif c.kind == "composition":
            hosts.append(c)
            comp = project.composition_for(c)
            ref = hfprov.element_ref(c.element, root.element)
            entry = c.composition_src or hfp.INDEX
            # no props: a render reads the variables from the HTML (defaults + this mount's values), so edits
            # made in HyperFrames come through; props hold only what is changed in Zenvi (Edit Props)
            link = _base_link(project, role="composition", entry=entry, composition=c.composition_id or ref,
                              file=entry, line=(comp.element.line if comp is not None and comp.file == entry
                                                else c.line),
                              props={}, block={"host": ref}, fps=insp.fps)
            duration = c.duration if c.duration else (comp.duration if comp is not None else None)
            insp.items.append(Item("linked", c.composition_id or ref, c.start or 0.0,
                                   (0, ranks.get(c.track_index, c.track_index)), order,
                                   clip=c, link=link, role="composition", expected_duration=duration))
            if c.problems:
                insp.problems.setdefault(c.label, []).extend(c.problems)
    if ranks:
        insp.warnings.extend(_stacking_notes(insp.items, paint))
    for group, exclude, signatures, shows in _layers(project, natives, hosts):
        block: Dict[str, Any] = {"exclude": exclude}
        if signatures:
            block["signatures"] = signatures
        link = _base_link(project, role="layer", entry=hfp.INDEX, composition=root.id, file=hfp.INDEX,
                          line=root.element.line, props={}, block=block, fps=insp.fps)
        name = "%s graphics%s" % (root.id, " (under the media)" if group < 0 else "")
        insp.items.append(Item("linked", name, 0.0, (group, 0 if group < 0 else 1 << 20), len(insp.items),
                               link=link, role="layer", expected_duration=root.duration))
        insp.warnings.append("%s render as one linked layer%s: %s" % (
            "the root's graphics" if group > 0 else "graphics under the media",
            "" if group > 0 else " below the media clips", ", ".join(shows[:6]) + ("..." if len(shows) > 6 else "")))


# ---------------------------------------------------------------------------
# Running the import
# ---------------------------------------------------------------------------

def _scaled(progress: Optional[ProgressFn], lo: float, hi: float) -> ProgressFn:
    def report(fraction, message):
        if progress is not None:
            progress(None if fraction is None else lo + (hi - lo) * float(fraction), message)
    return report


def _discard(paths: Sequence[str]) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            log.warning("could not remove the unused render %s", p, exc_info=True)


def project_identity(app) -> Tuple[int, str]:
    """Which project is open: its data object (replaced when another project opens) and its id."""
    data = getattr(app.project, "_data", None)
    return id(data), str((data or {}).get("id") or "") if isinstance(data, dict) else ""


def _start(position: Optional[float]) -> Tuple[float, Tuple[int, str]]:
    """(where the project's 0 lands, the open project) read once on the GUI thread before rendering.

    *position*, or the playhead when the import starts: a render takes a minute
    or more, and the clips land where the user asked even if they move the
    playhead meanwhile -- and in the project that was open, or not at all.
    """
    from classes.editor_tools._base import ToolError, get_app, playhead_seconds
    from classes.editor_tools.titles_text_common import precheck_on_main

    def read():
        return (playhead_seconds() if position is None else float(position)), project_identity(get_app())
    try:
        got = precheck_on_main(read)
    except ToolError as exc:
        raise lm.LinkError(str(exc)) from None
    if not got:
        raise lm.LinkError("the editor did not report the playhead; try again")
    return float(got[0] or 0.0), got[1]


def run_import(insp: Inspection, *, position: Optional[float] = None, track: str = "",
               on_progress: Optional[ProgressFn] = None, should_cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Render the linked parts, probe everything and add it all as ONE undo step. Blocking."""
    from classes.editor_tools.titles_text_common import CommitTimeout, commit_on_main
    from classes.handoff.jobs import JobCancelled
    cancel = should_cancel or (lambda: False)
    lm.precheck_placement(position, track or None, None)
    position, project_key = _start(position)
    renders: List[str] = []
    linked = insp.linked
    try:
        for n, item in enumerate(linked):
            if cancel():
                raise JobCancelled("the HyperFrames import was cancelled")
            span = (0.05 + 0.8 * n / max(1, len(linked)), 0.05 + 0.8 * (n + 1) / max(1, len(linked)))
            report = _scaled(on_progress, *span)
            report(0.0, "Rendering %s" % item.name)
            path, completed = lm.render_link(item.link or {}, on_progress=report, should_cancel=should_cancel)
            renders.append(path)
            item.link = completed
            item.media = {"path": path}
        if on_progress is not None:
            on_progress(0.88, "Reading the media")
        readers: Dict[int, dict] = {}
        for item in insp.items:
            if cancel():
                raise JobCancelled("the HyperFrames import was cancelled")
            path = item.clip.media_path if item.kind == "native" and item.clip is not None else (item.media or {}).get("path")
            readers[id(item)] = lm.probe_media(str(path))
        restore_readers = _restore_readers(insp)
        if on_progress is not None:
            on_progress(0.95, "Adding the clips")
        result = commit_on_main(_commit, insp, readers, restore_readers, position, track, project_key)
    except CommitTimeout:
        raise  # it may still land: keep the renders it uses
    except BaseException:
        _discard(renders)
        raise
    if not isinstance(result, dict):
        raise lm.LinkError("the editor did not finish adding the HyperFrames clips; check the timeline before "
                           "trying again")
    if on_progress is not None:
        on_progress(1.0, "Imported")
    result["warnings"] = list(dict.fromkeys(insp.warnings + result.get("warnings", [])))
    for item in linked:
        result["warnings"].extend(w for w in ((item.link or {}).get("warnings") or []) if w not in result["warnings"])
    return result


def _restore_readers(insp: Inspection) -> Dict[str, dict]:
    """file id -> probed reader for restored files whose media moved (blocking)."""
    if insp.restore is None:
        return {}
    out: Dict[str, dict] = {}
    for f in insp.restore.project.get("files") or []:
        path = f.get("path") if isinstance(f, dict) else None
        if path and os.path.isfile(str(path)):
            continue
        if path:
            insp.warnings.append(f"{os.path.basename(str(path))} is missing; its clips will show as missing media")
    return out


def _new_id(app, taken: Set[str]) -> str:
    while True:
        candidate = app.project.generate_id()
        if candidate not in taken:
            taken.add(candidate)
            return candidate


@dataclass
class _Lane:
    key: Tuple[int, int, int]      # (section, group, track index); section 0 = restored tracks
    label: str
    items: List[Any] = field(default_factory=list)
    original: Optional[dict] = None   # a restored track's layer record


def _overlaps(a: Tuple[float, float], b: Tuple[float, float], tol: float) -> bool:
    return a[0] < b[1] - tol and b[0] < a[1] - tol


def _rollback(updates, start: int, redo: list) -> None:
    """Undo the actions a failed commit recorded after *start* and drop them from the history (GUI thread)."""
    added = list(updates.actionHistory[start:])
    for action in reversed(added):
        try:
            updates.dispatch_action(updates.get_reverse_action(action))
        except Exception:
            log.error("could not take back a step of the failed HyperFrames import", exc_info=True)
    del updates.actionHistory[start:]
    updates.redoHistory[:] = redo
    try:
        updates.update_watchers()
    except Exception:
        log.debug("undo watchers not refreshed", exc_info=True)


def _commit(insp: Inspection, readers: Dict[int, dict], restore_readers: Dict[str, dict],
            position: Optional[float], track: str, project_key: Optional[Tuple[int, str]] = None) -> dict:
    """GUI thread: tracks, files and clips for the whole import, in one transaction.

    Refuses when another project was opened while the import rendered. A
    failure half-way takes back what it added (no partial import in the
    history), so "nothing changed" stays true.
    """
    from classes.editor_tools._base import ToolError, get_app
    app = get_app()
    if project_key is not None and project_identity(app) != tuple(project_key):
        raise ToolError("another project was opened while the HyperFrames import was rendering; nothing was "
                        "added -- import it again into this project")
    updates = app.updates
    start, redo = len(updates.actionHistory), list(updates.redoHistory)
    batch = {"open": False}
    try:
        return _commit_all(app, insp, readers, restore_readers, position, track, batch)
    except BaseException:
        _rollback(updates, start, redo)
        if batch["open"]:
            from classes.editor_tools.titles_text_common import end_clip_batch
            end_clip_batch()
        raise


def _commit_all(app, insp: Inspection, readers: Dict[int, dict], restore_readers: Dict[str, dict],
                position: Optional[float], track: str, batch: Dict[str, bool]) -> dict:
    from classes.editor_tools._base import ToolError, is_locked, layers, playhead_seconds, resolve_layer
    from classes.editor_tools.timeline_edit_common import extend_timeline
    from classes.editor_tools.titles_text_common import clips_in_window, create_track, place_clip
    from classes.query import File
    from classes.updates import nested_transaction
    fps = float(insp.fps)
    frame = 1.0 / fps
    offset = round(max(0.0, playhead_seconds() if position is None else float(position)) * fps) / fps
    data = app.project._data
    busy = bool(data.get("clips")) or bool(data.get("effects"))
    warnings: List[str] = []

    # -- lanes (bottom -> top) ------------------------------------------------
    lanes: List[_Lane] = []
    restore_layer_lane: Dict[int, _Lane] = {}
    restored = insp.restore
    if restored is not None:
        zp = restored.project
        records = {int(ly.get("number") or 0): ly for ly in zp.get("layers") or [] if isinstance(ly, dict)}
        for i, number in enumerate(hfrestore.track_numbers(zp)):
            rec = records.get(number, {"number": number, "label": ""})
            lane = _Lane((0, 0, i), str(rec.get("label") or ""), original=dict(rec))
            lanes.append(lane)
            restore_layer_lane[number] = lane
        # restored clips first, so what was added in HyperFrames can share their tracks without overlapping
        for c in zp.get("clips") or []:
            number = int(c.get("layer") or 0)
            lane = restore_layer_lane.get(number)
            if lane is None:
                lane = _Lane((0, 0, len(restore_layer_lane)), "")
                restore_layer_lane[number] = lane
                lanes.append(lane)
            begin = offset + float(c.get("position") or 0.0)
            length = max(frame, float(c.get("end") or 0.0) - float(c.get("start") or 0.0))
            lane.items.append((c, (begin, begin + length)))
    by_key: Dict[Tuple[int, int], List[_Lane]] = {}
    track_lanes = [lane for lane in lanes if lane.original is not None]
    for item in sorted(insp.items, key=lambda i: (i.lane, i.order)):
        start = offset + item.start
        window = (start, offset + item.end if item.end > item.start else start + frame)
        group, tindex = item.lane
        if restored is not None and group == 0 and 0 <= tindex < len(track_lanes):
            candidates = [track_lanes[tindex]] + by_key.get((group, tindex), [])
        else:
            candidates = by_key.get((group, tindex), [])
        target = None
        for lane in candidates:
            if not any(_overlaps(window, w, frame / 2) for _it, w in lane.items):
                target = lane
                break
        if target is None:
            label = TRACK_LABEL if item.role != "layer" else "HyperFrames graphics"
            target = _Lane((1, group, tindex), label)
            by_key.setdefault((group, tindex), []).append(target)
            lanes.append(target)
        target.items.append((item, window))
    lanes.sort(key=lambda ln: ln.key)
    lanes = [ln for ln in lanes if ln.items or ln.original is not None]

    # -- tracks for the lanes -------------------------------------------------
    existing = sorted(int(t.get("number") or 0) for t in layers())
    top = existing[-1] if existing else 0
    plan_numbers: List[Tuple[_Lane, int, bool]] = []
    if str(track or "").strip():
        first = resolve_layer(track)
        above = [n for n in existing if n >= first]
        extra = 0
        for i, lane in enumerate(lanes):
            if i < len(above):
                plan_numbers.append((lane, above[i], False))
            else:
                extra += 1
                plan_numbers.append((lane, top + TRACK_STEP * extra, True))
    elif not busy:
        extra = 0
        for i, lane in enumerate(lanes):
            if i < len(existing):
                plan_numbers.append((lane, existing[i], False))
            else:
                extra += 1
                plan_numbers.append((lane, top + TRACK_STEP * extra, True))
    else:
        plan_numbers = [(lane, top + TRACK_STEP * (i + 1), True) for i, lane in enumerate(lanes)]
    # validate before the first change
    for lane, number, new in plan_numbers:
        if new:
            continue
        if is_locked(number):
            raise ToolError(f"track {number} is locked; unlock it or leave track empty for new tracks")
        for item, window in lane.items:
            busy_clips = clips_in_window(number, window[0], window[1])
            if busy_clips:
                d = busy_clips[0].data
                what = item.get("title") if isinstance(item, dict) else getattr(item, "name", "a clip")
                raise ToolError(f"the track for {what!r} already has "
                                f"{d.get('title') or busy_clips[0].id!r} during {window[0]:.2f}-{window[1]:.2f}s; "
                                "pick another track or leave track empty to add new tracks")

    taken: Set[str] = set()
    for key in ("files", "clips", "effects", "markers", "layers"):
        for rec in data.get(key) or []:
            if isinstance(rec, dict) and rec.get("id"):
                taken.add(str(rec["id"]))
    placed: List[dict] = []
    created: List[dict] = []
    with nested_transaction(app.updates):
        layer_of: Dict[int, int] = {}
        for lane, number, new in plan_numbers:
            label = (lane.original or {}).get("label") or lane.label
            if new:
                create_track(number, label)
                created.append({"layer": number, "label": label})
            elif lane.original is not None and label:
                _relabel(number, label)
            layer_of[id(lane)] = number
        # restored project (raw records, lossless)
        if restored is not None:
            placed.extend(_commit_restore(app, restored, offset, layer_of, lanes, taken, warnings))
        todo = [(item, number, window) for lane, number, _new in plan_numbers for item, window in lane.items
                if isinstance(item, Item)]
        for k, (item, number, window) in enumerate(todo):
            # one preview refresh for the batch: the last clip's (C1 place_clip ignore_refresh)
            quiet = k < len(todo) - 1
            batch["open"] = quiet
            reader = readers.get(id(item)) or {}
            if item.kind == "native":
                placed.append(_place_native(item, reader, number, window[0], File, place_clip, quiet))
            else:
                placed.append(_place_linked(item, reader, number, window[0], fps, File, place_clip, quiet))
        batch["open"] = False
    extend_timeline()
    return {"mode": insp.mode, "reason": insp.reason, "position": round(offset, 3), "clips": placed,
            "created_tracks": created, "native": sum(1 for p in placed if p["kind"] == "native"),
            "linked": sum(1 for p in placed if p["kind"] == "linked"),
            "restored": sum(1 for p in placed if p["kind"] == "restored"), "warnings": warnings}


def _relabel(number: int, label: str) -> None:
    """Give an existing, unlabelled track a restored track's label (GUI thread, in the transaction)."""
    from classes import query
    for t in getattr(query, "Track").filter():
        if int(t.data.get("number") or 0) == int(number):
            if not str(t.data.get("label") or ""):
                t.data = dict(t.data, label=label)
                t.save()
            return


def _place_native(item: Item, reader: dict, layer: int, position: float, File, place_clip,
                  quiet: bool = False) -> dict:
    assert item.plan is not None and item.clip is not None
    path = str(item.clip.media_path)
    f = File.get(path=path)
    if not f:
        f = File()
        rec = copy.deepcopy(reader)
        rec["name"] = os.path.basename(path)
        f.data = rec
        f.save()
    props = dict(item.plan.props)
    props["start"], props["end"] = item.plan.start, item.plan.end
    if item.clip.kind in ("video", "audio"):
        # a clip's "duration" is its media's length (how far it can be trimmed out), not what it shows
        try:
            media_length = float(reader.get("duration") or 0.0)
        except (TypeError, ValueError):
            media_length = 0.0
        props["duration"] = max(media_length, item.plan.end)
    clip = place_clip(f.id, position, item.plan.duration, layer, title=item.clip.id or os.path.basename(path),
                      props=props, ignore_refresh=quiet)
    return {"kind": "native", "name": item.name, "timeline_clip_id": clip.get("id"), "file_id": f.id,
            "layer": layer, "position": round(position, 3), "end": round(position + item.plan.duration, 3),
            "element": item.clip.id, "problems": item.plan.problems}


def _place_linked(item: Item, reader: dict, layer: int, position: float, fps: float, File, place_clip,
                  quiet: bool = False) -> dict:
    link = dict(item.link or {})
    link.pop("warnings", None)
    stored = lm.normalize_link(link)
    try:
        duration = float(reader.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    frames = max(1, int(round(duration * fps)))
    length = frames / fps
    if length > duration + 1e-6:
        length = max(1.0 / fps, (frames - 1) / fps)
    f = File()
    rec = copy.deepcopy(reader)
    rec["name"] = item.name
    rec[lm.LINK_KEY] = stored
    f.data = rec
    f.save()
    clip = place_clip(f.id, position, length, layer, title=item.name, ignore_refresh=quiet)
    return {"kind": "linked", "name": item.name, "role": item.role, "timeline_clip_id": clip.get("id"),
            "file_id": f.id, "layer": layer, "position": round(position, 3), "end": round(position + length, 3),
            "path": rec.get("path"), "state": stored.get("state"),
            "codec": (stored.get("render") or {}).get("codec")}


def _commit_restore(app, restored: hfrestore.Restore, offset: float, layer_of: Dict[int, int], lanes: List[_Lane],
                    taken: Set[str], warnings: List[str]) -> List[dict]:
    from classes import query
    File: Any = getattr(query, "File")  # untyped: its get(**kwargs) has no self
    zp = restored.project
    lane_number: Dict[int, int] = {}
    for lane in lanes:
        if lane.original is not None:
            lane_number[int(lane.original.get("number") or 0)] = layer_of[id(lane)]
    for lane in lanes:  # restored clips on layers the record list lacked
        for item, _w in lane.items:
            if isinstance(item, dict) and id(lane) in layer_of:
                lane_number.setdefault(int(item.get("layer") or 0), layer_of[id(lane)])
    id_map: Dict[str, str] = {}

    def fresh(old: Any) -> str:
        old = str(old or "")
        if old and old not in taken:
            taken.add(old)
            id_map[old] = old
            return old
        new = _new_id(app, taken)
        if old:
            id_map[old] = new
        return new

    reused: Dict[str, str] = {}
    for f in zp.get("files") or []:
        if not isinstance(f, dict):
            continue
        existing = File.get(path=str(f.get("path"))) if f.get("path") else None
        if existing:
            reused[str(f.get("id"))] = existing.id
            continue
        rec = copy.deepcopy(f)
        rec["id"] = fresh(f.get("id"))
        app.updates.insert(["files"], rec)
    out: List[dict] = []
    clips = []
    for c in zp.get("clips") or []:
        if not isinstance(c, dict):
            continue
        rec = copy.deepcopy(c)
        rec["id"] = fresh(c.get("id"))
        fid = str(c.get("file_id") or "")
        rec["file_id"] = reused.get(fid) or id_map.get(fid, fid)
        if isinstance(rec.get("reader"), dict):
            rec["reader"]["id"] = rec["file_id"]
        rec["layer"] = lane_number.get(int(c.get("layer") or 0), int(c.get("layer") or 0))
        rec["position"] = round(float(c.get("position") or 0.0) + offset, 6)
        for e in rec.get("effects") or []:
            if isinstance(e, dict):
                e["id"] = fresh(e.get("id"))
        clips.append(rec)
    for rec in clips:
        parent = str(rec.get("parentObjectId") or "")
        if parent:
            rec["parentObjectId"] = id_map.get(parent, parent)
        app.updates.insert(["clips"], rec)
        out.append({"kind": "restored", "name": rec.get("title"), "timeline_clip_id": rec["id"],
                    "file_id": rec["file_id"], "layer": rec["layer"], "position": round(rec["position"], 3),
                    "end": round(rec["position"] + float(rec.get("end") or 0) - float(rec.get("start") or 0), 3)})
    for t in zp.get("effects") or []:
        if isinstance(t, dict):
            rec = copy.deepcopy(t)
            rec["id"] = fresh(t.get("id"))
            rec["layer"] = lane_number.get(int(t.get("layer") or 0), int(t.get("layer") or 0))
            rec["position"] = round(float(t.get("position") or 0.0) + offset, 6)
            app.updates.insert(["effects"], rec)
    for m in zp.get("markers") or []:
        if isinstance(m, dict):
            rec = copy.deepcopy(m)
            rec["id"] = fresh(m.get("id"))
            rec["position"] = round(float(m.get("position") or 0.0) + offset, 6)
            app.updates.insert(["markers"], rec)
    if reused:
        warnings.append("%d file(s) were already in the project and are shared" % len(reused))
    return out


__all__ = ["MODES", "Item", "Inspection", "inspect_project", "run_import", "ffprobe_media"]
