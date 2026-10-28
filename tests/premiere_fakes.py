"""Builders for the Premiere (FCP7 XML) export / import tests.

``project(...)`` returns a project dict shaped like ``get_app().project._data``
(files, layers, clips, transitions in ``effects``, markers), so the exporter
runs on a real :class:`TimelineSnapshot` without Qt or libopenshot. Keyframes
use libopenshot's JSON (``kf([(x, y), ...])``), transitions are built by the
timeline's own ``transition_ops.new_mask_transition``.

``fake_stills`` stands in for the QSvgRenderer step: it writes a tiny PNG at
each still's target, so tests see the files the XML points at.
"""

from __future__ import annotations

import copy
import os
import struct
import zlib

L1, L2, L3, L4 = 1000000, 2000000, 3000000, 4000000

LINEAR, BEZIER, CONSTANT = 1, 0, 2


def kf(points, interpolation=LINEAR):
    """[(x, y)] or [(x, y, interpolation)] -> libopenshot keyframe JSON."""
    out = []
    for p in points:
        interp = p[2] if len(p) > 2 else interpolation
        point = {"co": {"X": float(p[0]), "Y": float(p[1])}, "interpolation": int(interp)}
        if interp == BEZIER:
            point.update({"handle_left": {"X": 0.5, "Y": 1.0}, "handle_right": {"X": 0.5, "Y": 0.0},
                          "handle_type": 0})
        out.append(point)
    return {"Points": out}


def const(value):
    return kf([(1, value)], BEZIER)


def video_file(fid, path, *, width=1920, height=1080, duration=10.0, fps=(30, 1), has_audio=True, channels=2):
    return {"id": fid, "path": path, "name": os.path.basename(path), "media_type": "video", "has_video": True,
            "has_audio": has_audio, "width": width, "height": height, "duration": float(duration),
            "fps": {"num": fps[0], "den": fps[1]}, "channels": channels if has_audio else 0,
            "sample_rate": 48000 if has_audio else 0, "pixel_ratio": {"num": 1, "den": 1},
            "video_length": str(int(round(duration * fps[0] / fps[1]))), "has_single_image": False}


def audio_file(fid, path, *, duration=30.0, channels=2):
    return {"id": fid, "path": path, "name": os.path.basename(path), "media_type": "audio", "has_video": False,
            "has_audio": True, "width": 0, "height": 0, "duration": float(duration), "fps": {"num": 30, "den": 1},
            "channels": channels, "sample_rate": 48000, "has_single_image": False}


def image_file(fid, path, *, width=1000, height=1000):
    return {"id": fid, "path": path, "name": os.path.basename(path), "media_type": "image", "has_video": True,
            "has_audio": False, "width": width, "height": height, "duration": 3600.0,
            "fps": {"num": 30, "den": 1}, "has_single_image": True, "channels": 0, "sample_rate": 0}


def clip(cid, fid, *, layer=L1, position=0.0, start=0.0, end=5.0, title=None, **props):
    data = {"id": cid, "file_id": fid, "layer": layer, "position": float(position), "start": float(start),
            "end": float(end), "duration": float(end), "title": title or cid, "scale": 1, "gravity": 4}
    data.update(copy.deepcopy(props))
    return data


def fade(tid, layer, position, duration, *, fps=30.0, reverse=False, mask="fade"):
    from classes import transition_ops as ops
    entry, _ = ops.find_transition(mask)
    data = ops.new_mask_transition(tid, {"path": entry["path"], "has_single_image": True}, position=position,
                                   layer=layer, duration=duration, fps_float=fps, title=entry["key"])
    if reverse:
        ops.reverse_transition_data(data)
    return data


def project(*, fps=(30, 1), width=1920, height=1080, files=(), clips=(), transitions=(), markers=(), layers=None):
    used = sorted({c["layer"] for c in clips} | {t["layer"] for t in transitions})
    if layers is None:
        layers = [{"id": f"L{i + 1}", "number": n, "label": "", "lock": False, "y": 0} for i, n in enumerate(used)]
    return {"fps": {"num": fps[0], "den": fps[1]}, "width": width, "height": height,
            "pixel_ratio": {"num": 1, "den": 1}, "sample_rate": 48000, "channels": 2, "layers": layers,
            "files": [copy.deepcopy(f) for f in files], "clips": [copy.deepcopy(c) for c in clips],
            "effects": [copy.deepcopy(t) for t in transitions], "markers": [copy.deepcopy(m) for m in markers]}


def snapshot(proj, project_path=None):
    from classes.handoff.timeline_view import TimelineSnapshot
    return TimelineSnapshot.from_project(proj, project_path)


def tiny_png(width=2, height=2) -> bytes:
    """A valid RGBA PNG (fully transparent)."""
    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FakeStills:
    """Stand-in for the QSvgRenderer step: records the jobs and writes a small PNG at each target."""

    def __init__(self):
        self.jobs = []

    def __call__(self, stills):
        for job in stills:
            self.jobs.append(job)
            os.makedirs(os.path.dirname(job.target), exist_ok=True)
            with open(job.target, "wb") as fh:
                fh.write(tiny_png())


def parse(path):
    import xml.etree.ElementTree as ET
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return ET.fromstring(text.split("<!DOCTYPE xmeml>", 1)[-1])


def tracks(root, kind):
    return root.findall(f"sequence/media/{kind}/track")


def items(track):
    return [el for el in track if el.tag in ("clipitem", "transitionitem")]


def param(clipitem, effectid, parameterid):
    for effect in clipitem.iter("effect"):
        if effect.findtext("effectid") == effectid:
            for p in effect.findall("parameter"):
                if p.findtext("parameterid") == parameterid:
                    return p
    return None


def keys(parameter, point=False):
    out = []
    for k in parameter.findall("keyframe"):
        value = k.find("value")
        if point:
            out.append((int(k.findtext("when")), (float(value.findtext("horiz")), float(value.findtext("vert")))))
        else:
            out.append((int(k.findtext("when")), float(value.text)))
    return out


class FakeMediaProbe:
    """Stands in for ``linked_media.probe_media``: reader JSON from a {path: file dict} table."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def add(self, file_dict):
        self.table[file_dict["path"]] = file_dict

    def __call__(self, path):
        self.calls.append(path)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        data = copy.deepcopy(self.table.get(path) or video_file("", path))
        data.pop("id", None)
        data["path"] = path
        return data


def media_on_disk(folder, *file_dicts):
    """Create the files behind *file_dicts* (absolute paths rewritten into *folder*); returns the new dicts."""
    out = []
    for f in file_dicts:
        f = copy.deepcopy(f)
        f["path"] = os.path.join(str(folder), os.path.basename(f["path"]))
        with open(f["path"], "wb") as fh:
            fh.write(b"media")
        out.append(f)
    return out
