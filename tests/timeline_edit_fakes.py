"""Test doubles for the timeline-edit tools under the headless stub.

``fake_openshot()`` stands in for the few libopenshot calls the Add to Timeline
placement makes (Clip JSON, Point/Keyframe JSON, the transition image reader).

``FakeTimeline`` stands in for ``win.timeline``: its Slice/Time/Repeat/Split
audio/waveform handlers make the same kind of project edits as the real menu
handlers (through ``classes.query`` saves, so they join the tool's undo step) and
record every call. The real handlers run in tests/test_timeline_edit_real_qt.py.
"""

from __future__ import annotations

import copy
import json
import os
import types

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "editor_tools")


def _point(x, y, interpolation=0):
    return {"co": {"X": float(x), "Y": float(y)}, "handle_left": {"X": 0.5, "Y": 1.0},
            "handle_right": {"X": 0.5, "Y": 0.0}, "handle_type": 0, "interpolation": int(interpolation)}


def fake_openshot():
    with open(os.path.join(_FIXTURES, "clip.json"), encoding="utf-8") as fh:
        clip_template = json.load(fh)

    class Clip:
        def __init__(self, path):
            self.path = path

        def Json(self):
            data = copy.deepcopy(clip_template)
            data.pop("id", None)
            data.update({"position": 0.0, "layer": 0, "start": 0.0, "end": 0.0, "effects": []})
            return json.dumps(data)

    class Point:
        def __init__(self, x, y, interpolation=0):
            self.p = _point(x, y, interpolation)

        def Json(self):
            return json.dumps(self.p)

    class Keyframe:
        def __init__(self, value=None):
            self.points = [] if value is None else [_point(1, value)]

        def AddPoint(self, x, y, interpolation=0):
            self.points.append(_point(x, y, interpolation))

        def Json(self):
            return json.dumps({"Points": self.points})

    class QtImageReader:
        def __init__(self, path):
            self.path = path

        def Json(self):
            return json.dumps({"path": self.path, "type": "QtImageReader", "has_single_image": True})

    return types.SimpleNamespace(Clip=Clip, Point=Point, Keyframe=Keyframe, QtImageReader=QtImageReader,
                                 BEZIER=0, LINEAR=1, CONSTANT=2, GRAVITY_CENTER=4, SCALE_NONE=3)


def _span(data):
    pos = float(data.get("position") or 0.0)
    return pos, pos + float(data.get("end") or 0.0) - float(data.get("start") or 0.0)


class FakeTimeline:
    """Records handler calls and applies representative edits through the update manager."""

    def __init__(self, editor):
        self.editor = editor
        self.calls = []

    # -- helpers --------------------------------------------------------------
    def _fps(self):
        fps = self.editor.get("fps")
        return float(fps["num"]) / float(fps["den"])

    def _extend_timeline_to_fit_items(self):
        self.extended = getattr(self, "extended", 0) + 1

    # -- Slice ----------------------------------------------------------------
    def Slice_Triggered(self, action, clip_ids, trans_ids, playhead_position=0, ripple=False):
        from classes.query import Clip, Transition
        name = getattr(action, "name", str(action))
        self.calls.append(("slice", name, list(clip_ids), list(trans_ids), playhead_position, ripple))
        fps = self._fps()
        t = round(playhead_position * fps) / fps
        if name == "KEEP_LEFT":
            t += 1.0 / fps
        locked = {layer["number"] for layer in self.editor.get("layers") if layer.get("lock")}
        for kind, ids in ((Clip, clip_ids), (Transition, trans_ids)):
            for item_id in ids:
                item = kind.get(id=item_id)
                if not item or item.data.get("layer") in locked:
                    continue
                pos, start, end = item.data["position"], item.data["start"], item.data["end"]
                cut = start + (t - pos)
                if name == "KEEP_LEFT":
                    item.data["end"] = cut
                    item.save()
                    if ripple:
                        self._ripple(t, item.data["layer"], end - cut)
                elif name == "KEEP_RIGHT":
                    item.data.update({"start": cut, "position": pos if ripple else t})
                    item.save()
                    if ripple:
                        self._ripple(t, item.data["layer"], cut - start, exclude=item.id)
                else:
                    item.data["end"] = cut
                    item.save()
                    right = kind()
                    right.data = copy.deepcopy(item.data)
                    right.data.pop("id", None)
                    right.data.update({"position": t, "start": cut, "end": end})
                    right.save()

    def _ripple(self, start, layer, amount, exclude=None):
        from classes.query import Clip, Transition
        for item in Clip.filter(layer=layer) + Transition.filter(layer=layer):
            if item.id != exclude and item.data.get("position", 0.0) >= start:
                item.data["position"] -= amount
                item.save()

    # -- Speed / freeze -------------------------------------------------------
    def Time_Triggered(self, action, clip_ids, speed="1X", playhead_position=0.0):
        from classes.query import Clip
        name = getattr(action, "name", str(action))
        self.calls.append(("time", name, list(clip_ids), speed, playhead_position))
        fps = self._fps()
        for cid in clip_ids:
            clip = Clip.get(id=cid)
            data = clip.data
            start, end = float(data["start"]), float(data["end"])
            pts = sorted(data["time"]["Points"], key=lambda p: p["co"]["X"])
            if name in ("FREEZE", "FREEZE_ZOOM"):
                data["end"] = end + float(speed)
            elif name == "NONE":
                from classes.timeline_ops import repeat_is_active
                cache = data.get("repeat_cache")
                if repeat_is_active(data):
                    start = cache["start"]
                    data["start"] = start
                if "repeat_cache" in data:
                    data["repeat_cache"] = {}
                data["end"] = min(float(data["reader"]["duration"]), start + float(data["reader"]["duration"]))
                data["time"] = {"Points": [_point(1, 1, 1)]}
            else:
                factor = 1.0
                if name in ("FORWARD", "BACKWARD"):
                    label = speed.replace("X", "")
                    num, _, den = label.partition("/")
                    factor = float(num) / float(den or 1)
                new_end = start + round((end - start) / factor * fps) / fps
                x0 = round(start * fps) + 1
                x1 = x0 + round((new_end - start) * fps)
                if len(pts) >= 2:
                    y0, y1 = pts[0]["co"]["Y"], pts[-1]["co"]["Y"]
                else:
                    y0, y1 = x0, round(end * fps)
                if name in ("BACKWARD", "REVERSE"):
                    y0, y1 = y1, y0
                data["time"] = {"Points": [_point(x0, y0, 1), _point(x1, y1, 1)]}
                data["end"] = new_end
            data["duration"] = data["end"] - data["start"]
            clip.save()

    def Repeat_Triggered(self, pattern, direction, passes, clip_ids, delay_frames=0, ramp=0.0):
        from classes.query import Clip
        self.calls.append(("repeat", pattern, direction, passes, list(clip_ids), delay_frames, ramp))
        fps = self._fps()
        for cid in clip_ids:
            clip = Clip.get(id=cid)
            data = clip.data
            d = data["end"] - data["start"]
            from classes.timeline_ops import repeat_is_active
            if not repeat_is_active(data):
                data["repeat_cache"] = {"start": data["start"], "end": data["end"], "duration": d, "properties": {}}
            span_frames = max(1, round(d * fps))
            total = sum(max(1, round(span_frames / abs((1 + ramp) ** k))) for k in range(passes))
            total += (passes - 1) * delay_frames
            data.update({"start": 0.0, "end": total / fps, "duration": total / fps})
            clip.save()

    # -- Audio ----------------------------------------------------------------
    def Split_Audio_Triggered(self, action, clip_ids):
        from classes.query import Clip
        name = getattr(action, "name", str(action))
        self.calls.append(("split_audio", name, list(clip_ids)))
        numbers = sorted(layer["number"] for layer in self.editor.get("layers"))
        for cid in clip_ids:
            clip = Clip.get(id=cid)
            channels = int(clip.data["reader"].get("channels") or 1) if name == "MULTIPLE" else 1
            layer = clip.data["layer"]
            for ch in range(channels):
                below = [n for n in numbers if n < layer]
                if below:
                    layer = below[-1]
                else:
                    layer = max(1, layer // 2)
                    self.editor.add_track(layer)
                    numbers = sorted(n["number"] for n in self.editor.get("layers"))
                new = Clip()
                new.data = copy.deepcopy(clip.data)
                new.data.pop("id", None)
                new.data.update({"layer": layer, "title": clip.data["title"] + " (audio)",
                                 "has_video": {"Points": [_point(1, 0, 2)]},
                                 "has_audio": {"Points": [_point(1, -1, 2)]}})
                new.save()
            clip.data["has_audio"] = {"Points": [_point(1, 0, 2)]}
            clip.save()

    def Show_Waveform_Triggered(self, clip_ids, transaction_id=None):
        from classes.query import Clip
        self.calls.append(("show_waveform", list(clip_ids), transaction_id))
        for cid in clip_ids:
            clip = Clip.get(id=cid)
            clip.data = {"ui": {"audio_data": [0.1, 0.5, 0.2]}}
            clip.save()

    def Hide_Waveform_Triggered(self, clip_ids):
        from classes.query import Clip
        self.calls.append(("hide_waveform", list(clip_ids)))
        for cid in clip_ids:
            clip = Clip.get(id=cid)
            clip.data = {"ui": {"audio_data": []}}
            clip.save()
