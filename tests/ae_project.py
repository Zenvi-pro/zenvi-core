"""Zenvi project dicts for the After Effects export tests (no Qt, no app).

Files, clips and effects are built from the libopenshot 1.0 JSON captured in
``tests/fixtures/editor_tools/`` (the same shapes the editor stores), so a
snapshot of a ``ProjectBuilder`` project looks like a real one.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURES = os.path.join(_HERE, "fixtures", "editor_tools")
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))

BEZIER, LINEAR, CONSTANT = 0, 1, 2


def _load(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


_CLIP = _load("clip.json")
_FILES = _load("files.json")
_EFFECTS = _load("effects.json")


def point(x: float, y: float, interp: int = BEZIER, handle_left=(0.5, 1.0), handle_right=(0.5, 0.0)) -> dict:
    return {"co": {"X": float(x), "Y": float(y)}, "interpolation": int(interp), "handle_type": 0,
            "handle_left": {"X": handle_left[0], "Y": handle_left[1]},
            "handle_right": {"X": handle_right[0], "Y": handle_right[1]}}


def kf(*points: Sequence[float], interp: int = BEZIER) -> dict:
    """``kf((1, 0), (31, 1))``: points (X, Y[, interpolation]) with the editor's default handles."""
    out = []
    for p in points:
        out.append(point(p[0], p[1], int(p[2]) if len(p) > 2 else interp))
    return {"Points": out}


def const(value: float) -> dict:
    return kf((1, value))


class ProjectBuilder:
    """A project dict with tracks, files, clips, transitions and markers."""

    def __init__(self, fps=(30, 1), width=1920, height=1080, tracks=3):
        with open(os.path.join(_SRC, "settings", "_default.project"), encoding="utf-8") as fh:
            self.data: Dict[str, Any] = json.load(fh)
        self.data.update({"fps": {"num": fps[0], "den": fps[1]}, "width": width, "height": height,
                          "files": [], "clips": [], "effects": [], "markers": []})
        self.data["layers"] = [{"id": f"L{i + 1}", "number": (i + 1) * 1000000, "label": "", "lock": False, "y": 0}
                               for i in range(tracks)]
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def layer(self, track: int) -> int:
        """Layer number of UI track *track* (1 = bottom)."""
        return int(self.data["layers"][track - 1]["number"])

    def name_track(self, track: int, label: str = "", lock: bool = False) -> None:
        self.data["layers"][track - 1].update(label=label, lock=lock)

    def add_file(self, kind: str = "video", path: Optional[str] = None, **overrides) -> str:
        data = copy.deepcopy(_FILES[kind])
        fid = self._id("F")
        data.update(id=fid)
        if path:
            data["path"] = path
            data["name"] = os.path.basename(path)
        data.update(overrides)
        self.data["files"].append(data)
        return fid

    def add_title(self, path: str, width: int = 1920, height: int = 1080) -> str:
        return self.add_file("image", path=path, width=width, height=height, duration=3600.0)

    def file(self, fid: str) -> dict:
        return next(f for f in self.data["files"] if f["id"] == fid)

    def add_clip(self, fid: str, *, track: int = 1, position: float = 0.0, start: float = 0.0,
                 end: Optional[float] = None, **overrides) -> str:
        f = self.file(fid)
        data = copy.deepcopy(_CLIP)
        cid = self._id("C")
        duration = float(f.get("duration") or 10.0)
        if f.get("media_type") == "image":
            duration = 10.0
        data.update(id=cid, file_id=fid, title=os.path.basename(f["path"]), reader=copy.deepcopy(f),
                    layer=self.layer(track), position=float(position), start=float(start),
                    end=float(end if end is not None else duration), duration=duration, effects=[])
        if f.get("media_type") == "audio":
            data["has_video"] = kf((1, 0.0, CONSTANT))
        data.update(overrides)
        self.data["clips"].append(data)
        return cid

    def clip(self, cid: str) -> dict:
        return next(c for c in self.data["clips"] if c["id"] == cid)

    def add_effect(self, cid: str, class_name: str, **props) -> str:
        effect = copy.deepcopy(_EFFECTS[class_name])
        effect["id"] = self._id("E")
        effect.update(props)
        self.clip(cid)["effects"].append(effect)
        return effect["id"]

    def add_transition(self, *, track: int, position: float, duration: float, mask: str,
                       brightness: Optional[dict] = None, contrast: float = 3.0, fps: float = 30.0, **extra) -> str:
        tid = self._id("T")
        frames = int(round(duration * fps))
        data = {"id": tid, "layer": self.layer(track), "position": float(position), "start": 0.0,
                "end": float(duration), "title": "Transition", "type": "Mask",
                "brightness": brightness or kf((1, 1.0), (frames + 1, -1.0)),
                "contrast": const(contrast),
                "reader": {"path": mask, "type": "QtImageReader", "width": 720, "height": 576,
                           "has_single_image": True},
                "replace_image": False}
        data.update(extra)
        self.data["effects"].append(data)
        return tid

    def add_marker(self, position: float, name: str = "", color: str = "blue") -> str:
        mid = self._id("M")
        self.data["markers"].append({"id": mid, "position": float(position), "name": name, "icon": color + ".png",
                                     "vector": color})
        return mid

    def snapshot(self, project_path: Optional[str] = "/projects/Trip.zvn"):
        from classes.handoff.timeline_view import TimelineSnapshot
        return TimelineSnapshot.from_project(self.data, project_path)


__all__ = ["ProjectBuilder", "kf", "const", "point", "BEZIER", "LINEAR", "CONSTANT"]
