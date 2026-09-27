"""
 @file
 @brief Unit tests for timeline retime keyframe scaling
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

import os
import sys
import types
import unittest
from unittest.mock import patch

try:
    import openshot
except ModuleNotFoundError:
    openshot = types.SimpleNamespace(LINEAR=1)
    sys.modules["openshot"] = openshot

classes_app = types.ModuleType("classes.app")
classes_app.get_app = lambda: None
sys.modules.setdefault("classes.app", classes_app)


PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from windows.views.retime import retime_clip  # noqa: E402


def keyframe(*frames):
    return {
        "Points": [
            {"co": {"X": float(frame), "Y": float(index)}, "interpolation": openshot.LINEAR}
            for index, frame in enumerate(frames)
        ]
    }


def frame_numbers(data):
    return [point["co"]["X"] for point in data["Points"]]


class DummyClip:
    def __init__(self, data):
        self.data = data


class RetimeTests(unittest.TestCase):
    def test_retime_scales_nested_colorgrade_wheel_and_curve_keyframes(self):
        clip = DummyClip({
            "id": "clip-1",
            "position": 0.0,
            "start": 0.0,
            "end": 2.0,
            "duration": 2.0,
            "alpha": keyframe(1, 30, 60),
            "time": keyframe(1, 60),
            "effects": [{
                "class_name": "ColorGrade",
                "wheels": {
                    "enabled_keyframes": keyframe(1, 30, 60),
                    "global": {
                        "color_keyframes": {
                            "red": keyframe(1, 30, 60),
                            "green": keyframe(1, 30, 60),
                            "blue": keyframe(1, 30, 60),
                            "alpha": keyframe(1, 30, 60),
                        },
                        "amount_keyframes": keyframe(1, 30, 60),
                        "luma_keyframes": keyframe(1, 30, 60),
                    },
                },
                "curve": {
                    "enabled": keyframe(1, 30, 60),
                    "nodes": [{
                        "id": 1,
                        "x": keyframe(1, 30, 60),
                        "y": {
                            "Points": [
                                {"co": {"X": 1.0, "Y": 0.2}, "interpolation": openshot.LINEAR},
                                {"co": {"X": 30.0, "Y": 0.4}, "interpolation": openshot.LINEAR},
                                {"co": {"X": 60.0, "Y": 0.8}, "interpolation": openshot.LINEAR},
                            ]
                        },
                        "left_handle_x": keyframe(1, 30, 60),
                        "right_handle_y": keyframe(1, 30, 60),
                    }],
                },
            }],
        })

        with patch("windows.views.retime._project_fps_float", return_value=30.0):
            self.assertTrue(retime_clip(clip, 4.0, 0.0, direction=1))

        self.assertEqual(frame_numbers(clip.data["alpha"]), [1.0, 60, 121])
        self.assertEqual(frame_numbers(clip.data["time"]), [1, 121])

        effect = clip.data["effects"][0]
        wheels = effect["wheels"]
        self.assertEqual(frame_numbers(wheels["enabled_keyframes"]), [1.0, 60, 121])
        self.assertEqual(frame_numbers(wheels["global"]["color_keyframes"]["red"]), [1.0, 60, 121])
        self.assertEqual(frame_numbers(wheels["global"]["color_keyframes"]["green"]), [1.0, 60, 121])
        self.assertEqual(frame_numbers(wheels["global"]["amount_keyframes"]), [1.0, 60, 121])
        self.assertEqual(frame_numbers(wheels["global"]["luma_keyframes"]), [1.0, 60, 121])

        curve = effect["curve"]
        node = curve["nodes"][0]
        self.assertEqual(frame_numbers(curve["enabled"]), [1.0, 60, 121])
        self.assertEqual(frame_numbers(node["x"]), [1.0, 60, 121])
        self.assertEqual(frame_numbers(node["y"]), [1.0, 60, 121])
        self.assertEqual([point["co"]["Y"] for point in node["y"]["Points"]], [0.2, 0.4, 0.8])
        self.assertEqual(frame_numbers(node["left_handle_x"]), [1.0, 60, 121])
        self.assertEqual(frame_numbers(node["right_handle_y"]), [1.0, 60, 121])
        self.assertEqual(clip.data["end"], 4.0)
        self.assertEqual(clip.data["duration"], 4.0)


if __name__ == "__main__":
    unittest.main()
