"""Clip menu preset handlers (Fade / Volume / Motion / Rotate / Layout / Crop) on real TimelineView code.

Real-Qt test (ZENVI_REAL_QT=1 and libopenshot on PYTHONPATH); the headless
stub skips it. Covers the fixes made for the agent tools: combined fade in and
out on short clips (OpenShot 0b5db6493) and when re-applied over an older
fade, custom fade/zone lengths, the emphasis start time, the Wipe Out circle
directions (OpenShot ac9621a02) and one undo step per menu click.
"""

from __future__ import annotations

import copy
import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")
openshot = pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402


class _TestApp(QApplication):
    def _tr(self, text):
        return text


@pytest.fixture(scope="module")
def tl():
    importlib.import_module("qt_api")
    app = QApplication.instance()
    if app is None:
        app = _TestApp([])
    elif not hasattr(app, "_tr"):
        app._tr = lambda text: text
    return importlib.import_module("windows.views.timeline")


def _curve(*pairs, interp=openshot.BEZIER):
    return {"Points": [json_point(x, y, interp) for x, y in pairs]}


def json_point(x, y, interp=openshot.BEZIER):
    import json
    return json.loads(openshot.Point(x, y, interp).Json())


def _clip(clip_id, end, start=0.0, **over):
    data = {"id": clip_id, "position": 0.0, "start": start, "end": end, "duration": end - start, "layer": 1,
            "reader": {"has_video": True, "has_audio": True, "path": "/media/x.mp4", "width": 1280, "height": 720},
            "alpha": _curve((1, 1.0)), "volume": _curve((1, 1.0)), "scale_x": _curve((1, 1.0)),
            "scale_y": _curve((1, 1.0)), "location_x": _curve((1, 0.0)), "location_y": _curve((1, 0.0)),
            "rotation": _curve((1, 0.0)), "shear_x": _curve((1, 0.0)), "shear_y": _curve((1, 0.0)),
            "origin_x": _curve((1, 0.5)), "origin_y": _curve((1, 0.5)), "gravity": 4, "scale": 1,
            "effects": [], "ui": {}}
    data.update(over)
    return types.SimpleNamespace(id=clip_id, data=data)


class _LiveClip:
    """What timeline_sync.timeline.GetClip returns: curves evaluated like libopenshot."""

    def __init__(self, clip):
        self._clip = clip

    def __getattr__(self, name):
        import json
        kf = openshot.Keyframe()
        kf.SetJson(json.dumps(self._clip.data[name]))
        return kf


class _Helper:
    def __init__(self, tl, clips, playhead_frame=1):
        self._tl = tl
        self.clips = clips
        self.saved_tids = []
        self.window = types.SimpleNamespace(
            timeline_sync=types.SimpleNamespace(timeline=types.SimpleNamespace(
                GetClip=lambda cid: _LiveClip(clips[cid]) if cid in clips else None)),
            preview_thread=types.SimpleNamespace(current_frame=playhead_frame),
            clearSelections=lambda: None,
            KeyFrameTransformSignal=types.SimpleNamespace(emit=lambda *a: None),
        )

    def get_uuid(self):
        return "menu-tid"

    def AddPoint(self, keyframe, new_point):
        return self._tl.TimelineView.AddPoint(self, keyframe, new_point)

    def _remove_keypoints_in_range(self, points_data, a, b):
        return self._tl.TimelineView._remove_keypoints_in_range(self, points_data, a, b)

    def _get_transition_reader_json(self, path):
        return {"path": path, "type": "QtImageReader"}

    def update_clip_data(self, clip_data, **_kw):
        self.saved_tids.append(self._app.updates.transaction_id)

    def Show_Waveform_Triggered(self, clip_ids, transaction_id=None):
        pass

    def addSelection(self, *a, **k):
        pass

    def Rotate_Triggered(self, *a, **k):
        return self._tl.TimelineView.Rotate_Triggered(self, *a, **k)

    def Crop_Triggered(self, *a, **k):
        return self._tl.TimelineView.Crop_Triggered(self, *a, **k)

    def Layout_Triggered(self, *a, **k):
        return self._tl.TimelineView.Layout_Triggered(self, *a, **k)

    def show_all_clips(self, *a, **k):
        return self._tl.TimelineView.show_all_clips(self, *a, **k)


def _run(tl, clips, method, *args, playhead_frame=1, **kwargs):
    helper = _Helper(tl, clips, playhead_frame)
    app = types.SimpleNamespace(
        project=types.SimpleNamespace(get=lambda key: {"fps": {"num": 30, "den": 1}, "width": 1920,
                                                        "height": 1080}.get(key),
                                      generate_id=lambda: "FX%d" % len(helper.saved_tids)),
        updates=types.SimpleNamespace(transaction_id=None),
        window=helper.window,
    )
    helper._app = app
    with patch.object(tl.Clip, "get", side_effect=lambda id=None: clips.get(id)), \
            patch.object(tl.Clip, "filter", side_effect=lambda **_k: list(clips.values())), \
            patch.object(tl, "get_app", return_value=app):
        getattr(tl.TimelineView, method)(helper, *args, **kwargs)
    return helper, app


def _xy(curve):
    return [(p["co"]["X"], round(p["co"]["Y"], 4)) for p in sorted(curve["Points"], key=lambda p: p["co"]["X"])]


def test_fast_fade_in_and_out_keeps_four_keyframes_on_a_four_second_clip(tl):
    clips = {"C1": _clip("C1", 4.0)}
    _run(tl, clips, "Fade_Triggered", tl.MenuFade.IN_OUT_FAST, ["C1"])
    assert _xy(clips["C1"].data["alpha"]) == [(1, 0.0), (31, 1.0), (91, 1.0), (121, 0.0)]
    assert _xy(clips["C1"].data["volume"]) == [(1, 0.0), (31, 1.0), (91, 1.0), (121, 0.0)]


def test_fast_fade_over_a_slow_one_goes_back_to_full_opacity(tl):
    clips = {"C1": _clip("C1", 10.0)}
    _run(tl, clips, "Fade_Triggered", tl.MenuFade.IN_OUT_SLOW, ["C1"])
    assert _xy(clips["C1"].data["alpha"]) == [(1, 0.0), (91, 1.0), (211, 1.0), (301, 0.0)]
    _run(tl, clips, "Fade_Triggered", tl.MenuFade.IN_OUT_FAST, ["C1"])
    # Before the fix the new fade aimed at the old ramp's value at 1 s (~0.26) and left the clip dim.
    assert _xy(clips["C1"].data["alpha"]) == [(1, 0.0), (31, 1.0), (271, 1.0), (301, 0.0)]


def test_combined_fade_respects_a_lower_steady_level(tl):
    clips = {"C1": _clip("C1", 10.0, alpha=_curve((1, 0.6)))}
    _run(tl, clips, "Fade_Triggered", tl.MenuFade.IN_OUT_FAST, ["C1"])
    assert _xy(clips["C1"].data["alpha"]) == [(1, 0.0), (31, 0.6), (271, 0.6), (301, 0.0)]


def test_fade_seconds_sets_the_fade_length_on_a_trimmed_clip(tl):
    clips = {"C1": _clip("C1", 14.0, start=4.0)}
    _run(tl, clips, "Fade_Triggered", tl.MenuFade.IN_FAST, ["C1"], "Start of Clip", fade_seconds=2.0)
    # The original point at X=1 sits before the visible start (frame 121) and stays.
    assert _xy(clips["C1"].data["alpha"]) == [(1, 1.0), (121, 0.0), (181, 1.0)]
    _run(tl, clips, "Fade_Triggered", tl.MenuFade.OUT_FAST, ["C1"], "End of Clip", fade_seconds=2.0)
    assert _xy(clips["C1"].data["alpha"])[-2:] == [(361, 1.0), (421, 0.0)]


def test_volume_fade_in_and_out_on_a_short_clip_keeps_both_fades(tl):
    clips = {"C1": _clip("C1", 3.0)}
    _run(tl, clips, "Volume_Triggered", tl.MenuVolume.FADE_IN_OUT_FAST, ["C1"], "Entire Clip")
    assert _xy(clips["C1"].data["volume"]) == [(1, 0.0), (31, 1.0), (61, 1.0), (91, 0.0)]
    assert clips["C1"].data["alpha"] == _curve((1, 1.0))  # the Volume menu only touches volume


def test_zone_seconds_lengthens_an_in_motion(tl):
    clips = {"C1": _clip("C1", 10.0)}
    _run(tl, clips, "Animate_Triggered", tl.MenuAnimate.SLIDE_IN_LEFT, ["C1"], zone_seconds=2.0)
    assert _xy(clips["C1"].data["location_x"]) == [(1, -1.0), (61, 0.0)]


def test_emphasis_seconds_replaces_the_playhead(tl):
    clips = {"C1": _clip("C1", 10.0)}
    _run(tl, clips, "Animate_Triggered", tl.MenuAnimate.TADA, ["C1"], emphasis_seconds=4.0, playhead_frame=1)
    frames = [x for x, _y in _xy(clips["C1"].data["rotation"])]
    assert min(f for f in frames if f > 1) >= 121 and max(frames) <= 151


def test_wipe_out_circle_expand_uses_the_out_to_in_mask(tl):
    clips = {"C1": _clip("C1", 5.0)}
    _run(tl, clips, "Animate_Triggered", tl.MenuAnimate.WIPE_OUT_CIRCLE_EXPAND, ["C1"])
    mask = clips["C1"].data["effects"][-1]
    assert mask["class_name"] == "Mask" and mask["reader"]["path"].endswith("circle_out_to_in.svg")


@pytest.mark.parametrize("method,args", [
    ("Rotate_Triggered", ("RIGHT_90",)),
    ("Layout_Triggered", ("TOP_RIGHT",)),
    ("Crop_Triggered", ()),
    ("No_Transform_Triggered", ()),
])
def test_transform_menu_items_are_one_undo_step_for_all_selected_clips(tl, method, args):
    clips = {"C1": _clip("C1", 5.0), "C2": _clip("C2", 5.0)}
    if method == "Rotate_Triggered":
        call_args = (tl.MenuRotate[args[0]], ["C1", "C2"])
    elif method == "Layout_Triggered":
        call_args = (tl.MenuLayout[args[0]], ["C1", "C2"])
    elif method == "Crop_Triggered":
        call_args = (["C1", "C2"], "resize")
    else:
        for c in clips.values():
            c.data["effects"] = [{"id": "CR" + c.id, "class_name": "Crop"}]
        call_args = (["C1", "C2"],)
    helper, app = _run(tl, clips, method, *call_args)
    assert helper.saved_tids and len(set(helper.saved_tids)) == 1 and helper.saved_tids[0]
    assert app.updates.transaction_id is None  # nothing left behind


def test_transform_menu_items_join_an_agent_tools_transaction(tl):
    clips = {"C1": _clip("C1", 5.0, rotation=_curve((1, 90.0)))}
    helper = _Helper(tl, clips)
    app = types.SimpleNamespace(
        project=types.SimpleNamespace(get=lambda key: {"fps": {"num": 30, "den": 1}}.get(key)),
        updates=types.SimpleNamespace(transaction_id="agent-call"), window=helper.window)
    helper._app = app
    with patch.object(tl.Clip, "get", side_effect=lambda id=None: clips.get(id)), \
            patch.object(tl.Clip, "filter", side_effect=lambda **_k: list(clips.values())), \
            patch.object(tl, "get_app", return_value=app):
        tl.TimelineView.No_Transform_Triggered(helper, ["C1"])
    assert set(helper.saved_tids) == {"agent-call"}
    assert app.updates.transaction_id == "agent-call"


def test_copy_keyframes_menu_uses_the_shared_groups(tl):
    from classes.keyframe_rules import COPY_KEYFRAME_GROUPS
    assert set(tl._COPY_KEYFRAME_GROUP.values()) == set(COPY_KEYFRAME_GROUPS)
    clip = _clip("C1", 5.0)
    captured = {}

    class _Clipboard:
        def setMimeData(self, mime):
            captured["mime"] = mime

    app = types.SimpleNamespace(clipboard=lambda: _Clipboard())
    with patch.object(tl.Clip, "get", return_value=copy.deepcopy(clip)), \
            patch.object(tl, "get_app", return_value=app), \
            patch.object(tl.ClipboardManager, "to_mime", side_effect=lambda objs: [o.data for o in objs]):
        tl.TimelineView.Copy_Triggered(None, tl.MenuCopy.KEYFRAMES_LOCATION, ["C1"], [], [])
    assert set(captured["mime"][0]) == {"gravity", "location_x", "location_y"}
