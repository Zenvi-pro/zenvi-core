"""A Zenvi-exported HyperFrames project back as native Zenvi clips (lossless; pure planning).

``export_to_hyperframes_tool`` embeds the Zenvi project (files, clips,
effects, tracks, markers) in ``index.html`` as ``<script type="application/json"
id="zenvi-timeline">``, plus what it wrote for each clip (``elements``:
start, duration, in point, track, src). Restoring reads that block, so the
clips that come back are the clips that went out, and reads the HTML for
what was changed in HyperFrames since:

* an exported element (``data-zenvi-clip-id``) whose ``data-start`` /
  ``data-duration`` / ``data-media-start`` / ``data-track-index`` changed
  moves / trims its clip accordingly;
* an exported element that was deleted drops its clip;
* everything new (elements without ``data-zenvi-clip-id``, compositions) is
  left to the importer, which brings it in like any HyperFrames content.

Media resolve to the original files when they still exist (same size as the
copy), else to the copies in ``assets/``. Keyframes are rescaled when the
project now runs at another frame rate.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional, Set

from classes.handoff.hyperframes.parser import Clip, HyperFramesError, Project

SUPPORTED_VERSION = 1
TRACK_STEP = 1000000
TIME_TOLERANCE = 1e-3


class RestoreError(HyperFramesError):
    """The embedded Zenvi timeline cannot be restored; the message says why."""


@dataclass
class Restore:
    """What restoring a Zenvi export adds: a project-shaped dict with paths and edits applied."""

    project: Dict[str, Any]                 # files, clips, effects, layers, markers (+ fps, size)
    warnings: List[str] = field(default_factory=list)
    edited: List[str] = field(default_factory=list)      # clip ids moved / trimmed in HyperFrames
    dropped: List[str] = field(default_factory=list)     # clip ids deleted in HyperFrames
    exported_ids: Set[str] = field(default_factory=set)  # clip ids of the export (data-zenvi-clip-id)

    @property
    def clip_count(self) -> int:
        return len(self.project.get("clips") or [])


def timeline_block(project: Project) -> dict:
    """The checked ``zenvi`` block of the embedded timeline."""
    data = project.zenvi_timeline
    if not isinstance(data, dict):
        raise RestoreError("index.html has no zenvi-timeline JSON, so it is not a Zenvi export")
    try:
        version = int(str(data.get("zenvi_timeline")))
    except (TypeError, ValueError):
        raise RestoreError("the zenvi-timeline JSON has an unreadable version") from None
    if version > SUPPORTED_VERSION:
        raise RestoreError(f"this HyperFrames project was exported by a newer Zenvi (timeline version {version}); "
                           "update Zenvi to restore it natively, or import it with mode='flatten'")
    block = data.get("zenvi")
    if not isinstance(block, dict):
        raise RestoreError("the zenvi-timeline JSON holds no Zenvi project; import it with mode='native' instead")
    zp = block.get("project")
    if not isinstance(zp, dict):
        raise RestoreError("the zenvi-timeline JSON holds no Zenvi project; import it with mode='native' instead")
    for key in ("files", "clips"):
        if not isinstance(zp.get(key, []), list):
            raise RestoreError(f"the zenvi-timeline JSON's project.{key} is not a list")
    return block


def _fps(value: Any) -> Fraction:
    try:
        return Fraction(int(value.get("num") or 30), int(value.get("den") or 1))
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return Fraction(30, 1)


def _media_paths(block: dict, root: str, warnings: List[str]) -> Dict[str, str]:
    """file id -> the media to use (original when it still exists and matches the copy, else the copy)."""
    out: Dict[str, str] = {}
    assets = block.get("assets") or {}
    originals = block.get("originals") or {}
    for fid, rel in assets.items():
        copy_path = os.path.normpath(os.path.join(root, *str(rel).split("/")))
        original = originals.get(fid)
        if isinstance(original, str) and os.path.isfile(original):
            try:
                same = not os.path.exists(copy_path) or os.path.getsize(original) == os.path.getsize(copy_path)
            except OSError:
                same = False
            if same:
                out[str(fid)] = original
                continue
        if os.path.exists(copy_path):
            out[str(fid)] = os.path.realpath(copy_path) if os.path.islink(copy_path) else copy_path
        elif isinstance(original, str) and os.path.isfile(original):
            out[str(fid)] = original
        else:
            warnings.append(f"the media of file {fid} is missing (neither {rel} nor {original})")
    return out


def _rescale(value: Any, ratio: Fraction, *, time_values: bool = False) -> Any:
    """Keyframe X (and a time curve's Y) moved to another frame rate: x' = (x - 1) * ratio + 1."""
    if isinstance(value, dict):
        if isinstance(value.get("Points"), list):
            out = dict(value)
            pts = []
            for p in value["Points"]:
                if isinstance(p, dict) and isinstance(p.get("co"), dict):
                    p = copy.deepcopy(p)
                    for axis in (("X", "Y") if time_values else ("X",)):
                        v = p["co"].get(axis)
                        if isinstance(v, (int, float)):
                            p["co"][axis] = round((float(v) - 1.0) * float(ratio) + 1.0, 6)
                pts.append(p)
            out["Points"] = pts
            return out
        return {k: _rescale(v, ratio, time_values=(k == "time")) for k, v in value.items()}
    if isinstance(value, list):
        return [_rescale(v, ratio) for v in value]
    return value


def _layer_numbers(zp: dict) -> List[int]:
    numbers = sorted({int(ly.get("number") or 0) for ly in zp.get("layers") or [] if isinstance(ly, dict)})
    used = sorted({int(c.get("layer") or 0) for c in (zp.get("clips") or []) + (zp.get("effects") or [])
                   if isinstance(c, dict)})
    return sorted(set(numbers) | set(used))


def _apply_edit(clip: dict, element: Clip, written: dict, layers: List[int], fps: Fraction,
                warnings: List[str]) -> bool:
    """Move / trim *clip* by what changed on its HyperFrames element; True when something changed."""
    changed = False
    rate = float(fps)
    if element.start is not None and abs(element.start - float(written.get("start", 0.0))) > TIME_TOLERANCE:
        clip["position"] = round(element.start * rate) / rate
        changed = True
    new_duration = element.duration
    old_duration = float(written.get("duration", 0.0))
    new_media = element.media_start
    old_media = float(written.get("media_start", 0.0))
    start, end = float(clip.get("start") or 0.0), float(clip.get("end") or 0.0)
    if element.kind in ("video", "audio") and abs(new_media - old_media) > TIME_TOLERANCE:
        if clip.get("time") and isinstance(clip.get("time"), dict) and len(clip["time"].get("Points") or []) > 1:
            warnings.append(f"{clip.get('title')!r}: its new data-media-start was not applied (the clip is retimed)")
        else:
            shift = round((new_media - old_media) * rate) / rate
            start, end = max(0.0, start + shift), max(0.0, end + shift)
            changed = True
    if new_duration is not None and abs(new_duration - old_duration) > TIME_TOLERANCE:
        end = start + round(new_duration * rate) / rate
        changed = True
    clip["start"], clip["end"] = start, max(start + 1.0 / rate, end)
    track = element.track_index
    if track != int(written.get("track", track)):
        if 0 <= track < len(layers):
            clip["layer"] = layers[track]
        else:
            clip["layer"] = (layers[-1] if layers else 0) + TRACK_STEP * (track - len(layers) + 1)
        changed = True
    return changed


def plan_restore(project: Project, *, target_fps: Optional[Fraction] = None) -> Restore:
    """The Zenvi project to add back, with media resolved and HyperFrames-side edits applied (pure)."""
    block = timeline_block(project)
    warnings: List[str] = []
    zp = copy.deepcopy(block["project"])
    paths = _media_paths(block, project.root_dir, warnings)
    for f in zp.get("files") or []:
        if isinstance(f, dict) and str(f.get("id")) in paths:
            f["path"] = paths[str(f["id"])]
    by_file = {str(f.get("id")): f for f in zp.get("files") or [] if isinstance(f, dict)}
    for c in zp.get("clips") or []:
        if not isinstance(c, dict):
            continue
        fid = str(c.get("file_id") or "")
        reader = c.get("reader")
        if fid in paths and isinstance(reader, dict):
            reader["path"] = paths[fid]
        elif isinstance(reader, dict) and fid in by_file:
            reader["path"] = by_file[fid].get("path", reader.get("path"))
    raw_elements = block.get("elements")
    elements: Dict[str, Any] = raw_elements if isinstance(raw_elements, dict) else {}
    fps = _fps(zp.get("fps"))
    layers = _layer_numbers(zp)
    present: Dict[str, Clip] = {}
    for clip in project.root.clips:
        cid = clip.zenvi.get("clip-id")
        if cid:
            present[cid] = clip
    restore = Restore(project=zp, warnings=warnings, exported_ids=set(elements) | set(present))
    kept = []
    for c in zp.get("clips") or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        if cid in elements and cid not in present:
            restore.dropped.append(cid)
            continue
        el = present.get(cid)
        if el is not None and cid in elements and _apply_edit(c, el, elements[cid], layers, fps, warnings):
            restore.edited.append(cid)
        kept.append(c)
    zp["clips"] = kept
    if restore.dropped:
        warnings.append("%d clip(s) deleted in HyperFrames were left out" % len(restore.dropped))
    if restore.edited:
        warnings.append("%d clip(s) moved or trimmed in HyperFrames came back with the new timing" %
                        len(restore.edited))
    if target_fps is not None and target_fps != fps:
        ratio = target_fps / fps
        zp["clips"] = [_rescale(c, ratio) for c in zp["clips"]]
        zp["effects"] = [_rescale(e, ratio) for e in zp.get("effects") or []]
        warnings.append(f"the export ran at {float(fps):g} fps and this project at {float(target_fps):g} fps; "
                        "keyframes were rescaled")
    return restore


def track_numbers(zp: dict) -> List[int]:
    return _layer_numbers(zp)


__all__ = ["RestoreError", "Restore", "plan_restore", "timeline_block", "track_numbers", "SUPPORTED_VERSION"]
