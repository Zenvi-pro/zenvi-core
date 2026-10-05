"""A Zenvi-generated Remotion project back as native Zenvi clips (lossless).

``export_to_remotion_tool`` stores the original project (files, clips,
effects, transitions, tracks, markers, settings -- everything but undo
history and waveform caches) under ``zenvi.project`` in
``src/zenvi/timeline.json``. Restoring reads that block, so the clips that
come back are the clips that went out:

* :func:`write_project_file` writes it as a new ``.zvn`` (File > Import
  Project > Remotion Project... > "Open as an editable Zenvi project");
* :func:`insert_native` adds its tracks, files, clips, transitions and
  markers to the open project as ONE undo step (``import_remotion_project_tool``).

Media paths point at the originals when they still exist, else at the
copies in ``public/zenvi-media/``. The readable part of timeline.json is
hashed at export; when it was edited since, the restore says so (those
edits are not read back -- the ``zenvi`` block is the source of truth).
"""

from __future__ import annotations

import copy
import json
import os
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from classes.handoff.linked_media import LinkError
from classes.handoff.remotion.exporter import TIMELINE_REL, readable_hash

SUPPORTED_VERSION = 1
TRACK_STEP = 1000000
ID_COLLECTIONS = ("files", "clips", "effects", "markers")


class RestoreError(LinkError):
    """The timeline.json cannot be restored; the message says why."""


def timeline_path(project_root: str) -> str:
    return os.path.join(project_root, *TIMELINE_REL.split("/"))


def load_timeline(path: str) -> dict:
    """The parsed timeline.json of a Zenvi export (checked)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise RestoreError(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise RestoreError(f"{path} is not valid JSON ({exc}); re-export it from Zenvi") from None
    if not isinstance(data, dict) or "zenvi_timeline" not in data:
        raise RestoreError(f"{path} is not a Zenvi timeline (no zenvi_timeline header)")
    try:
        version = int(str(data.get("zenvi_timeline")))
    except (TypeError, ValueError):
        raise RestoreError(f"{path} has an unreadable zenvi_timeline version") from None
    if version > SUPPORTED_VERSION:
        raise RestoreError(f"{path} was written by a newer Zenvi (timeline version {version}); update Zenvi")
    block = data.get("zenvi")
    project = block.get("project") if isinstance(block, dict) else None
    if not isinstance(project, dict):
        raise RestoreError(f"{path} has no 'zenvi.project' block, so its clips cannot be restored natively; import "
                           "the ZenviTimeline composition as a linked clip instead")
    for key in ("files", "clips"):
        if not isinstance(project.get(key, []), list):
            raise RestoreError(f"{path}: zenvi.project.{key} must be a list")
    return data


def timeline_edited(timeline: dict) -> bool:
    stored = (timeline.get("zenvi") or {}).get("readable_sha256")
    return bool(stored) and stored != readable_hash(timeline)


def asset_remap(timeline: dict, project_root: str) -> Tuple[Dict[str, str], List[str]]:
    """original media path -> the copy in public/ (only for originals that are gone), and warnings."""
    remap: Dict[str, str] = {}
    warnings: List[str] = []
    assets = (timeline.get("zenvi") or {}).get("assets") or {}
    for original, src in assets.items():
        if not isinstance(original, str) or not isinstance(src, str):
            continue
        if os.path.exists(original):
            continue
        copy_path = os.path.join(project_root, "public", *src.split("/"))
        if os.path.exists(copy_path):
            remap[original] = os.path.realpath(copy_path) if os.path.islink(copy_path) else copy_path
        else:
            warnings.append(f"{os.path.basename(original)} is missing (neither {original} nor {copy_path} exists)")
    return remap, warnings


def remap_paths(value: Any, remap: Dict[str, str]) -> Any:
    """*value* with every ``"path"`` string found in *remap* replaced (at any depth)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "path" and isinstance(v, str) and v in remap:
                out[k] = remap[v]
            else:
                out[k] = remap_paths(v, remap)
        return out
    if isinstance(value, list):
        return [remap_paths(v, remap) for v in value]
    return value


def restored_project(timeline: dict, project_root: str) -> Tuple[dict, List[str]]:
    """The original project dict with media pointing at files that exist, and warnings."""
    project = copy.deepcopy((timeline.get("zenvi") or {}).get("project") or {})
    remap, warnings = asset_remap(timeline, project_root)
    project = remap_paths(project, remap)
    if timeline_edited(timeline):
        warnings.append("src/zenvi/timeline.json was edited after Zenvi exported it; the native restore uses the "
                        "timeline as exported (import ZenviTimeline as a linked clip to see the edits)")
    return project, warnings


def _new_id(taken: set) -> str:
    import random
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    while True:
        candidate = "".join(random.choice(chars) for _ in range(10))
        if candidate not in taken:
            taken.add(candidate)
            return candidate


def write_project_file(timeline: dict, project_root: str, zvn_path: str) -> Tuple[str, List[str]]:
    """Write the restored project as a new ``.zvn`` (staged, then renamed); returns (path, warnings)."""
    project, warnings = restored_project(timeline, project_root)
    project["history"] = {"undo": [], "redo": []}
    project["id"] = _new_id(set())  # a new project, not the exported one's twin (cloud identity)
    zvn_path = os.path.abspath(os.path.expanduser(zvn_path))
    if not zvn_path.lower().endswith(".zvn"):
        zvn_path += ".zvn"
    folder = os.path.dirname(zvn_path)
    if not os.path.isdir(folder):
        raise RestoreError(f"the folder {folder} does not exist")
    partial = zvn_path + ".partial"
    with open(partial, "w", encoding="utf-8") as fh:
        json.dump(project, fh, indent=1, ensure_ascii=False)
    os.replace(partial, zvn_path)
    return zvn_path, warnings


def _rescale_points(value: Any, ratio: Fraction) -> Any:
    """Keyframe X (and time-curve Y) scaled to another frame rate: x' = (x - 1) * ratio + 1."""
    if isinstance(value, dict):
        if isinstance(value.get("Points"), list):
            out = dict(value)
            pts = []
            for p in value["Points"]:
                if isinstance(p, dict) and isinstance(p.get("co"), dict):
                    p = copy.deepcopy(p)
                    x = p["co"].get("X")
                    if isinstance(x, (int, float)):
                        p["co"]["X"] = round((float(x) - 1.0) * float(ratio) + 1.0, 4)
                pts.append(p)
            out["Points"] = pts
            return out
        return {k: _rescale_points(v, ratio) for k, v in value.items()}
    if isinstance(value, list):
        return [_rescale_points(v, ratio) for v in value]
    return value


def _rescale_time_values(clip: dict, ratio: Fraction) -> None:
    time = clip.get("time")
    if isinstance(time, dict) and isinstance(time.get("Points"), list):
        for p in time["Points"]:
            co = p.get("co") if isinstance(p, dict) else None
            if isinstance(co, dict) and isinstance(co.get("Y"), (int, float)):
                co["Y"] = round((float(co["Y"]) - 1.0) * float(ratio) + 1.0, 4)


def plan_native(project: dict, current: dict, *, offset: float = 0.0) -> Tuple[dict, List[str]]:
    """What :func:`insert_native` adds to *current* (a project dict): new ids, track numbers, shifted times.

    Pure: returns ``{"layers": [...], "files": [...], "clips": [...], "effects": [...], "markers": [...],
    "layer_map": {old: new}, "id_map": {old: new}}`` and warnings.
    """
    warnings: List[str] = []
    src_fps = Fraction(int((project.get("fps") or {}).get("num") or 30), int((project.get("fps") or {}).get("den") or 1))
    dst_fps = Fraction(int((current.get("fps") or {}).get("num") or 30), int((current.get("fps") or {}).get("den") or 1))
    ratio = dst_fps / src_fps
    if ratio != 1:
        warnings.append(f"the exported project ran at {float(src_fps):g} fps and this one at {float(dst_fps):g} fps; "
                        "keyframes were rescaled to the new frame rate")
    if (project.get("width"), project.get("height")) != (current.get("width"), current.get("height")):
        warnings.append(f"the exported project was {project.get('width')}x{project.get('height')} and this one is "
                        f"{current.get('width')}x{current.get('height')}; positions are relative, so clips keep their "
                        "place in the frame")
    current_layers = sorted(int(ly.get("number") or 0) for ly in current.get("layers") or [] if isinstance(ly, dict))
    busy = bool(current.get("clips")) or bool(current.get("effects"))
    src_layers = sorted((ly for ly in project.get("layers") or [] if isinstance(ly, dict)),
                        key=lambda ly: int(ly.get("number") or 0))
    used = {int(c.get("layer") or 0) for c in (project.get("clips") or []) + (project.get("effects") or [])
            if isinstance(c, dict)}
    for number in sorted(used - {int(ly.get("number") or 0) for ly in src_layers}):
        src_layers.append({"number": number, "label": "", "lock": False, "y": 0})
    src_layers.sort(key=lambda ly: int(ly.get("number") or 0))
    layer_map: Dict[int, int] = {}
    new_layers: List[dict] = []
    taken_ids = set()
    for key in ID_COLLECTIONS + ("layers",):
        for item in current.get(key) or []:
            if isinstance(item, dict) and item.get("id"):
                taken_ids.add(str(item["id"]))
    top = current_layers[-1] if current_layers else 0
    for ly in src_layers:
        number = int(ly.get("number") or 0)
        if number in layer_map:
            continue
        if not busy:  # an empty timeline: the tracks keep their numbers
            layer_map[number] = number
            if number not in current_layers:
                new_layers.append(dict(copy.deepcopy(ly), id=_new_id(taken_ids)))
        elif number in used:  # a busy timeline: new tracks on top, only for tracks that hold something
            target = top + TRACK_STEP * (len(new_layers) + 1)
            layer_map[number] = target
            new_layers.append(dict(copy.deepcopy(ly), number=target, id=_new_id(taken_ids)))
    id_map: Dict[str, str] = {}

    def _fresh(old: Any) -> str:
        old = str(old or "")
        if old and old not in taken_ids:
            taken_ids.add(old)
            id_map[old] = old
            return old
        new = _new_id(taken_ids)
        if old:
            id_map[old] = new
        return new

    files = []
    for f in project.get("files") or []:
        if isinstance(f, dict):
            f = copy.deepcopy(f)
            f["id"] = _fresh(f.get("id"))
            files.append(f)
    shift = float(offset or 0.0)
    clips = []
    for c in project.get("clips") or []:
        if not isinstance(c, dict):
            continue
        c = copy.deepcopy(c)
        c["id"] = _fresh(c.get("id"))
        c["file_id"] = id_map.get(str(c.get("file_id") or ""), c.get("file_id"))
        c["layer"] = layer_map.get(int(c.get("layer") or 0), int(c.get("layer") or 0))
        c["position"] = round(float(c.get("position") or 0.0) + shift, 6)
        if ratio != 1:
            _rescale_time_values(c, ratio)
            c = _rescale_points(c, ratio)
        for e in c.get("effects") or []:
            if isinstance(e, dict):
                e["id"] = _fresh(e.get("id"))
        clips.append(c)
    for c in clips:  # parents restored after every clip got its id
        parent = str(c.get("parentObjectId") or "")
        if parent:
            c["parentObjectId"] = id_map.get(parent, parent)
    effects = []
    for t in project.get("effects") or []:
        if isinstance(t, dict):
            t = copy.deepcopy(t)
            t["id"] = _fresh(t.get("id"))
            t["layer"] = layer_map.get(int(t.get("layer") or 0), int(t.get("layer") or 0))
            t["position"] = round(float(t.get("position") or 0.0) + shift, 6)
            if ratio != 1:
                t = _rescale_points(t, ratio)
            effects.append(t)
    markers = []
    for m in project.get("markers") or []:
        if isinstance(m, dict):
            m = copy.deepcopy(m)
            m["id"] = _fresh(m.get("id"))
            m["position"] = round(float(m.get("position") or 0.0) + shift, 6)
            markers.append(m)
    return ({"layers": new_layers, "files": files, "clips": clips, "effects": effects, "markers": markers,
             "layer_map": layer_map, "id_map": id_map}, warnings)


def insert_native(timeline: dict, project_root: str, *, position: Optional[float] = None) -> dict:
    """Add the exported timeline to the open project, natively, as ONE undo step. Blocking (GUI hop).

    Its 0 lands at *position* (default the playhead). Into a project
    without clips the tracks keep their numbers (ids too, when free), so the
    restored clips equal the exported ones; otherwise they go on new tracks
    above the existing ones. Files already in the project (same path) are
    reused.
    """
    project, warnings = restored_project(timeline, project_root)

    def _commit():
        from classes.editor_tools._base import get_app, playhead_seconds, snap_seconds
        from classes.editor_tools.timeline_edit_common import extend_timeline
        from classes.query import File
        from classes.updates import nested_transaction
        app = get_app()
        current = app.project._data
        start = snap_seconds(playhead_seconds() if position is None else max(0.0, float(position)))
        plan, plan_warnings = plan_native(project, current, offset=start)
        reused: Dict[str, str] = {}
        with nested_transaction(app.updates):
            for layer in plan["layers"]:
                app.updates.insert(["layers"], layer)
            for f in plan["files"]:
                existing = File.get(path=f.get("path")) if f.get("path") else None
                if existing:
                    reused[f["id"]] = existing.id
                    continue
                app.updates.insert(["files"], f)
            for c in plan["clips"]:
                if c.get("file_id") in reused:
                    c["file_id"] = reused[c["file_id"]]
                    reader = c.get("reader")
                    if isinstance(reader, dict):
                        reader["id"] = c["file_id"]
                app.updates.insert(["clips"], c)
            for t in plan["effects"]:
                app.updates.insert(["effects"], t)
            for m in plan["markers"]:
                app.updates.insert(["markers"], m)
        extend_timeline()
        return plan, plan_warnings, reused, start

    from classes.editor_tools.titles_text_common import commit_on_main
    done = commit_on_main(_commit)
    if not done:
        raise RestoreError("the editor did not finish restoring the clips; check the timeline before trying again")
    plan, plan_warnings, reused, start = done
    return {"position": round(start, 3), "tracks": len(plan["layers"]), "files": len(plan["files"]) - len(reused),
            "reused_files": len(reused), "clips": [c["id"] for c in plan["clips"]],
            "transitions": len(plan["effects"]), "markers": len(plan["markers"]),
            "layer_map": {str(k): v for k, v in plan["layer_map"].items()}, "warnings": warnings + plan_warnings}


__all__ = ["load_timeline", "timeline_edited", "restored_project", "write_project_file", "insert_native",
           "plan_native", "asset_remap", "remap_paths", "timeline_path", "RestoreError"]
