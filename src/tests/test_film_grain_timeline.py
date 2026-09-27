"""
 @file
 @brief This file contains unit tests for timeline helper logic
 @author Jonathan Thomas <jonathan@openshot.org>

 @section LICENSE

 Copyright (c) 2008-2026 OpenShot Studios, LLC
 (http://www.openshotstudios.com). This file is part of
 OpenShot Video Editor (http://www.openshot.org), an open-source project
 dedicated to delivering high quality video editing and animation solutions
 to the world.

 OpenShot Video Editor is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 OpenShot Video Editor is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.

 You should have received a copy of the GNU General Public License
 along with OpenShot Library.  If not, see <http://www.gnu.org/licenses/>.
 """

# Zenvi note: these are the tests OpenShot PR #6017 added to src/tests/test_timeline_helpers.py
# (Motion presets, the Audio menu waveform toggle, and the Film Grain presets). Zenvi does not carry
# upstream's full timeline helper suite, so only the new cases are kept here, on the same harness.

import copy
import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

import openshot


PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from qt_api import QCoreApplication, Qt  # noqa: E402
from qt_api import QApplication  # noqa: E402
from qt_test_app import ensure_app_state as ensure_qt_app_state, get_or_create_app  # noqa: E402

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)


class DummySettings:
    def __init__(self):
        self.values = {
            "default-profile": "HD 720p 30 fps",
            "default-samplerate": 48000,
            "default-channels": 2,
        }

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


class DummyApp(QApplication):
    def __init__(self):
        super().__init__([])
        self.settings = DummySettings()

    def get_settings(self):
        return self.settings

    def _tr(self, text):
        return text


def ensure_app_state(app):
    return ensure_qt_app_state(app, DummySettings)


class FilmGrainTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app, cls._owns_app = get_or_create_app(DummyApp)
        cls.app = ensure_app_state(app)
        sys.modules.pop("windows.views.timeline", None)
        cls.timeline_module = importlib.import_module("windows.views.timeline")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_owns_app", False) and cls.app:
            cls.app.quit()

    def make_motion_clip(self):
        def kf(value):
            return {"Points": [{"co": {"X": 1, "Y": value}, "interpolation": openshot.BEZIER}]}

        data = {
            "id": "C1",
            "position": 0.0,
            "start": 0.0,
            "end": 3.0,
            "scale": openshot.SCALE_FIT,
            "scale_x": kf(1.0),
            "scale_y": kf(1.0),
            "location_x": kf(0.0),
            "location_y": kf(0.0),
            "rotation": kf(0.0),
            "shear_x": kf(0.0),
            "shear_y": kf(0.0),
            "alpha": kf(1.0),
            "origin_x": kf(0.5),
            "origin_y": kf(0.5),
            "effects": [],
        }
        return types.SimpleNamespace(id="C1", data=data)

    def make_motion_app(self):
        return types.SimpleNamespace(
            updates=types.SimpleNamespace(transaction_id=None),
            project=types.SimpleNamespace(
                get=lambda key: {"num": 30, "den": 1} if key == "fps" else None,
                generate_id=lambda: "FX1",
            ),
        )

    def make_motion_helper(self):
        timeline_module = self.timeline_module

        class Helper:
            def __init__(self):
                self.updated = []
                self.show_wait_spinner = False
                self.window = types.SimpleNamespace(
                    timeline_sync=types.SimpleNamespace(
                        timeline=types.SimpleNamespace(GetClip=lambda _clip_id: None)
                    ),
                    preview_thread=types.SimpleNamespace(current_frame=None),
                )

            def get_uuid(self):
                return "tx-motion-1"

            def AddPoint(self, keyframe, new_point):
                return timeline_module.TimelineView.AddPoint(self, keyframe, new_point)

            def _remove_keypoints_in_range(self, points_data, frame_start, frame_end):
                return timeline_module.TimelineView._remove_keypoints_in_range(
                    self, points_data, frame_start, frame_end)

            def update_clip_data(self, clip_data, **kwargs):
                self.updated.append((copy.deepcopy(clip_data), dict(kwargs)))

            def _get_transition_reader_json(self, _path):
                return {"path": "/tmp/wipe.svg", "has_single_image": True}

        return Helper()

    def test_motion_wipe_mask_uses_high_static_contrast(self):
        helper = self.make_motion_helper()
        clip = self.make_motion_clip()

        with patch.object(self.timeline_module, "get_app", return_value=self.make_motion_app()), \
                patch.object(self.timeline_module.Clip, "get", return_value=clip):
            self.timeline_module.TimelineView.Animate_Triggered(
                helper,
                self.timeline_module.MenuAnimate.WIPE_IN_LEFT,
                ["C1"],
                transaction_id="tx-motion-test",
            )

        self.assertEqual(len(clip.data["effects"]), 1)
        effect = clip.data["effects"][0]
        self.assertEqual(effect["class_name"], "Mask")
        self.assertEqual(effect["contrast"]["Points"][0]["co"]["Y"], 20.0)

    def test_motion_blur_wipe_in_produces_blur_then_mask_effects(self):
        helper = self.make_motion_helper()
        clip = self.make_motion_clip()

        with patch.object(self.timeline_module, "get_app", return_value=self.make_motion_app()), \
                patch.object(self.timeline_module.Clip, "get", return_value=clip):
            self.timeline_module.TimelineView.Animate_Triggered(
                helper,
                self.timeline_module.MenuAnimate.BLUR_WIPE_IN_LEFT,
                ["C1"],
                transaction_id="tx-blur-wipe-test",
            )

        self.assertEqual(len(clip.data["effects"]), 2)
        blur_fx, mask_fx = clip.data["effects"]
        self.assertEqual(blur_fx["class_name"], "Blur")
        self.assertEqual(blur_fx["horizontal_radius"]["Points"][0]["co"]["Y"], 50.0)
        self.assertEqual(blur_fx["horizontal_radius"]["Points"][-1]["co"]["Y"], 0.0)
        self.assertEqual(mask_fx["class_name"], "Mask")
        self.assertEqual(mask_fx["brightness"]["Points"][0]["co"]["Y"], 1.0)
        self.assertEqual(mask_fx["brightness"]["Points"][-1]["co"]["Y"], -1.0)
        self.assertEqual(mask_fx["contrast"]["Points"][0]["co"]["Y"], 10.0)

    def test_motion_blur_wipe_out_produces_blur_then_mask_effects(self):
        helper = self.make_motion_helper()
        clip = self.make_motion_clip()

        with patch.object(self.timeline_module, "get_app", return_value=self.make_motion_app()), \
                patch.object(self.timeline_module.Clip, "get", return_value=clip):
            self.timeline_module.TimelineView.Animate_Triggered(
                helper,
                self.timeline_module.MenuAnimate.BLUR_WIPE_OUT_LEFT,
                ["C1"],
                transaction_id="tx-blur-wipe-test",
            )

        self.assertEqual(len(clip.data["effects"]), 2)
        blur_fx, mask_fx = clip.data["effects"]
        self.assertEqual(blur_fx["class_name"], "Blur")
        self.assertEqual(blur_fx["horizontal_radius"]["Points"][0]["co"]["Y"], 0.0)
        self.assertEqual(blur_fx["horizontal_radius"]["Points"][-1]["co"]["Y"], 50.0)
        self.assertEqual(mask_fx["class_name"], "Mask")
        self.assertEqual(mask_fx["brightness"]["Points"][0]["co"]["Y"], -1.0)
        self.assertEqual(mask_fx["brightness"]["Points"][-1]["co"]["Y"], 1.0)
        self.assertEqual(mask_fx["contrast"]["Points"][0]["co"]["Y"], 10.0)

    def test_motion_bounce_emphasis_uses_frame_relative_offsets(self):
        helper = self.make_motion_helper()
        clip = self.make_motion_clip()
        app = types.SimpleNamespace(
            updates=types.SimpleNamespace(transaction_id=None),
            project=types.SimpleNamespace(
                get=lambda key: {"num": 30, "den": 1} if key == "fps" else None,
                generate_id=lambda: "FX1",
            ),
        )

        with patch.object(self.timeline_module, "get_app", return_value=app), \
                patch.object(self.timeline_module.Clip, "get", return_value=clip):
            self.timeline_module.TimelineView.Animate_Triggered(
                helper,
                self.timeline_module.MenuAnimate.BOUNCE,
                ["C1"],
                transaction_id="tx-motion-test",
            )

        points = {
            point["co"]["X"]: point["co"]["Y"]
            for point in clip.data["location_y"]["Points"]
        }
        self.assertAlmostEqual(points[13], -0.25)
        self.assertAlmostEqual(points[14], -0.25)
        self.assertAlmostEqual(points[22], -0.125)
        self.assertAlmostEqual(points[28], -0.033333, places=6)
        self.assertAlmostEqual(points[31], 0.0)

    def test_motion_emphasis_uses_playhead_in_clip_local_frame_space(self):
        helper = self.make_motion_helper()
        helper.window.preview_thread.current_frame = 331
        clip = self.make_motion_clip()
        clip.data["position"] = 10.0
        app = types.SimpleNamespace(
            updates=types.SimpleNamespace(transaction_id=None),
            project=types.SimpleNamespace(
                get=lambda key: {"num": 30, "den": 1} if key == "fps" else None,
                generate_id=lambda: "FX1",
            ),
        )

        with patch.object(self.timeline_module, "get_app", return_value=app), \
                patch.object(self.timeline_module.Clip, "get", return_value=clip):
            self.timeline_module.TimelineView.Animate_Triggered(
                helper,
                self.timeline_module.MenuAnimate.BOUNCE,
                ["C1"],
                transaction_id="tx-motion-test",
            )

        points = {
            point["co"]["X"]: point["co"]["Y"]
            for point in clip.data["location_y"]["Points"]
        }
        self.assertIn(31, points)
        self.assertIn(43, points)
        self.assertIn(61, points)
        self.assertNotIn(13, points)
        self.assertAlmostEqual(points[43], -0.25)
        self.assertAlmostEqual(points[61], 0.0)

    def test_motion_bounce_in_down_uses_frame_relative_rebound_offsets(self):
        helper = self.make_motion_helper()
        clip = self.make_motion_clip()
        app = types.SimpleNamespace(
            updates=types.SimpleNamespace(transaction_id=None),
            project=types.SimpleNamespace(
                get=lambda key: {"num": 30, "den": 1} if key == "fps" else None,
                generate_id=lambda: "FX1",
            ),
        )

        with patch.object(self.timeline_module, "get_app", return_value=app), \
                patch.object(self.timeline_module.Clip, "get", return_value=clip):
            self.timeline_module.TimelineView.Animate_Triggered(
                helper,
                self.timeline_module.MenuAnimate.BOUNCE_IN_DOWN,
                ["C1"],
                transaction_id="tx-motion-test",
            )

        points = {
            point["co"]["X"]: point["co"]["Y"]
            for point in clip.data["location_y"]["Points"]
        }
        self.assertAlmostEqual(points[1], -3.0)
        self.assertAlmostEqual(points[19], 0.25)
        self.assertAlmostEqual(points[24], -0.1)
        self.assertAlmostEqual(points[28], 0.05)
        self.assertAlmostEqual(points[31], 0.0)

    def test_motion_bounce_out_up_uses_frame_relative_rebound_offsets(self):
        helper = self.make_motion_helper()
        clip = self.make_motion_clip()
        app = types.SimpleNamespace(
            updates=types.SimpleNamespace(transaction_id=None),
            project=types.SimpleNamespace(
                get=lambda key: {"num": 30, "den": 1} if key == "fps" else None,
                generate_id=lambda: "FX1",
            ),
        )

        with patch.object(self.timeline_module, "get_app", return_value=app), \
                patch.object(self.timeline_module.Clip, "get", return_value=clip):
            self.timeline_module.TimelineView.Animate_Triggered(
                helper,
                self.timeline_module.MenuAnimate.BOUNCE_OUT_UP,
                ["C1"],
                transaction_id="tx-motion-test",
            )

        points = {
            point["co"]["X"]: point["co"]["Y"]
            for point in clip.data["location_y"]["Points"]
        }
        self.assertAlmostEqual(points[67], -0.125)
        self.assertAlmostEqual(points[73], 0.25)
        self.assertAlmostEqual(points[74], 0.25)
        self.assertAlmostEqual(points[91], -3.0)

    def test_motion_ken_burns_direction_sets_distinct_scale_and_location(self):
        helper = self.make_motion_helper()
        clip = self.make_motion_clip()
        clip.data["reader"] = {"width": 3840, "height": 1080}
        app = types.SimpleNamespace(
            updates=types.SimpleNamespace(transaction_id=None),
            project=types.SimpleNamespace(
                get=lambda key: {
                    "fps": {"num": 30, "den": 1},
                    "width": 1920,
                    "height": 1080,
                }.get(key),
                generate_id=lambda: "FX1",
            ),
        )

        with patch.object(self.timeline_module, "get_app", return_value=app), \
                patch.object(self.timeline_module.Clip, "get", return_value=clip):
            self.timeline_module.TimelineView.Animate_Triggered(
                helper,
                self.timeline_module.MenuAnimate.KEN_BURNS_IN,
                ["C1"],
                transaction_id="tx-motion-test",
            )

        self.assertEqual(clip.data["scale"], openshot.SCALE_CROP)
        self.assertAlmostEqual(clip.data["scale_x"]["Points"][-1]["co"]["Y"], 1.22)
        self.assertAlmostEqual(clip.data["scale_y"]["Points"][-1]["co"]["Y"], 1.22)
        self.assertGreater(clip.data["location_x"]["Points"][0]["co"]["Y"], 0.0)
        self.assertLess(clip.data["location_x"]["Points"][-1]["co"]["Y"], 0.0)
        self.assertAlmostEqual(clip.data["location_y"]["Points"][-1]["co"]["Y"], 0.0)

    def test_video_clip_with_audio_can_toggle_waveform(self):
        helper = types.SimpleNamespace()
        clip = types.SimpleNamespace(data={"reader": {"has_video": True, "has_audio": True}})

        self.assertTrue(self.timeline_module.TimelineView._clip_has_audio(helper, clip))

    def test_video_clip_without_audio_cannot_toggle_waveform(self):
        helper = types.SimpleNamespace()
        clip = types.SimpleNamespace(data={"reader": {"has_video": True, "has_audio": False}})

        self.assertFalse(self.timeline_module.TimelineView._clip_has_audio(helper, clip))

    def test_film_grain_trigger_adds_preset_effect(self):
        timeline_module = self.timeline_module
        clip = types.SimpleNamespace(id="C1", data={
            "id": "C1",
            "reader": {"has_video": True},
            "effects": [],
        })

        class Helper:
            def __init__(self):
                self.updates = []

            def _clip_has_video(self, candidate):
                return timeline_module.TimelineView._clip_has_video(self, candidate)

            def _clip_has_visual(self, candidate):
                return timeline_module.TimelineView._clip_has_visual(self, candidate)

            def _create_film_grain_effect_json(self):
                return {"class_name": "FilmGrain", "id": "FG-1", "seed": 77}

            def update_clip_data(self, clip_data, **kwargs):
                self.updates.append((copy.deepcopy(clip_data), dict(kwargs)))

        history = []
        fake_app = types.SimpleNamespace(
            updates=types.SimpleNamespace(
                apply_last_action_to_history=lambda original: history.append(copy.deepcopy(original))
            )
        )
        helper = Helper()

        with patch.object(timeline_module.Clip, "get", return_value=clip), \
             patch.object(timeline_module, "get_app", return_value=fake_app):
            timeline_module.TimelineView.Film_Grain_Triggered(
                helper,
                timeline_module.FILM_GRAIN_PRESET_SUPER_8,
                ["C1"],
            )

        self.assertEqual(len(clip.data["effects"]), 1)
        effect = clip.data["effects"][0]
        self.assertEqual(effect["class_name"], "FilmGrain")
        self.assertEqual(effect["id"], "FG-1")
        self.assertEqual(effect["seed"], 77)
        self.assertEqual(effect["amount"]["Points"][0]["co"]["Y"], 0.62)
        self.assertEqual(effect["size"]["Points"][0]["co"]["Y"], 0.72)
        self.assertEqual(len(helper.updates), 1)
        self.assertEqual(helper.updates[0][1], {"only_basic_props": False, "ignore_reader": True})
        self.assertEqual(len(history), 1)

    def test_film_grain_trigger_replaces_duplicate_existing_effects(self):
        timeline_module = self.timeline_module
        clip = types.SimpleNamespace(id="C1", data={
            "id": "C1",
            "reader": {"has_video": True},
            "effects": [
                {"class_name": "FilmGrain", "id": "KEEP", "order": 3, "seed": 222},
                {"class_name": "FilmGrain", "id": "DROP", "seed": 333},
            ],
        })

        class Helper:
            def __init__(self):
                self.updates = []

            def _clip_has_video(self, candidate):
                return timeline_module.TimelineView._clip_has_video(self, candidate)

            def _clip_has_visual(self, candidate):
                return timeline_module.TimelineView._clip_has_visual(self, candidate)

            def update_clip_data(self, clip_data, **kwargs):
                self.updates.append(copy.deepcopy(clip_data))

        fake_app = types.SimpleNamespace(
            updates=types.SimpleNamespace(apply_last_action_to_history=lambda _original: None)
        )
        helper = Helper()

        with patch.object(timeline_module.Clip, "get", return_value=clip), \
             patch.object(timeline_module, "get_app", return_value=fake_app):
            timeline_module.TimelineView.Film_Grain_Triggered(
                helper,
                timeline_module.FILM_GRAIN_PRESET_35MM_FINE,
                ["C1"],
            )

        self.assertEqual(len(clip.data["effects"]), 1)
        effect = clip.data["effects"][0]
        self.assertEqual(effect["id"], "KEEP")
        self.assertEqual(effect["order"], 3)
        self.assertEqual(effect["seed"], 222)
        self.assertEqual(effect["amount"]["Points"][0]["co"]["Y"], 0.14)

    def test_film_grain_trigger_none_removes_existing_effects(self):
        timeline_module = self.timeline_module
        clip = types.SimpleNamespace(id="C1", data={
            "id": "C1",
            "reader": {"has_video": True},
            "effects": [
                {"class_name": "FilmGrain", "id": "FG-1"},
                {"class_name": "Brightness", "id": "B-1"},
                {"class_name": "FilmGrain", "id": "FG-2"},
            ],
        })

        class Helper:
            def __init__(self):
                self.updates = []

            def _clip_has_video(self, candidate):
                return timeline_module.TimelineView._clip_has_video(self, candidate)

            def _clip_has_visual(self, candidate):
                return timeline_module.TimelineView._clip_has_visual(self, candidate)

            def update_clip_data(self, clip_data, **kwargs):
                self.updates.append((copy.deepcopy(clip_data), dict(kwargs)))

        history = []
        fake_app = types.SimpleNamespace(
            updates=types.SimpleNamespace(
                apply_last_action_to_history=lambda original: history.append(copy.deepcopy(original))
            )
        )
        helper = Helper()

        with patch.object(timeline_module.Clip, "get", return_value=clip), \
             patch.object(timeline_module, "get_app", return_value=fake_app):
            timeline_module.TimelineView.Film_Grain_Triggered(
                helper,
                timeline_module.FILM_GRAIN_PRESET_NONE,
                ["C1"],
            )

        self.assertEqual(clip.data["effects"], [{"class_name": "Brightness", "id": "B-1"}])
        self.assertEqual(len(helper.updates), 1)
        self.assertEqual(len(history), 1)


    # Zenvi additions: helpers the ported menu code calls that Zenvi did not previously define.

    def test_transition_reader_json_is_loaded_and_cached(self):
        helper = types.SimpleNamespace()
        helper._load_transition_reader_data = types.MethodType(
            self.timeline_module.TimelineView._load_transition_reader_data, helper)
        svg_path = os.path.join(PATH, "transitions", "common", "wipe_left_to_right.svg")

        first = self.timeline_module.TimelineView._get_transition_reader_json(helper, svg_path)
        second = self.timeline_module.TimelineView._get_transition_reader_json(helper, svg_path)

        self.assertIsInstance(first, dict)
        self.assertEqual(first.get("path"), svg_path)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)  # callers get their own copy
        self.assertIn(os.path.abspath(svg_path), helper._transition_reader_json_cache)
        self.assertIsNone(self.timeline_module.TimelineView._get_transition_reader_json(helper, ""))

    def test_clip_has_visible_waveform_requires_audio_samples(self):
        view = self.timeline_module.TimelineView
        self.assertFalse(view._clip_has_visible_waveform(None, None))
        self.assertFalse(view._clip_has_visible_waveform(None, types.SimpleNamespace(data={})))
        self.assertFalse(view._clip_has_visible_waveform(
            None, types.SimpleNamespace(data={"ui": {"audio_data": []}})))
        self.assertTrue(view._clip_has_visible_waveform(
            None, types.SimpleNamespace(data={"ui": {"audio_data": [0.1, 0.5]}})))


if __name__ == "__main__":
    unittest.main()
