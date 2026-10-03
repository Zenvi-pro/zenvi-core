"""
 @file
 @brief Unit tests for project keyframe frame-number scaling
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
import unittest


PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from classes.keyframe_scaler import KeyframeScaler  # noqa: E402


def keyframe(*frames):
    return {
        "Points": [
            {"co": {"X": float(frame), "Y": float(index)}, "interpolation": 0}
            for index, frame in enumerate(frames)
        ]
    }


def frame_numbers(data):
    return [point["co"]["X"] for point in data["Points"]]


class KeyframeScalerTests(unittest.TestCase):
    def test_scales_clip_effect_transition_colorgrade_and_time_keyframes(self):
        data = {
            "clips": [{
                "id": "clip-1",
                "alpha": keyframe(1, 30),
                "location": {
                    "red": keyframe(1, 30),
                    "green": keyframe(1, 30),
                    "blue": keyframe(1, 30),
                    "alpha": keyframe(1, 30),
                },
                "time": {
                    "Points": [
                        {"co": {"X": 1.0, "Y": 1.0}, "interpolation": 0},
                        {"co": {"X": 30.0, "Y": 30.0}, "interpolation": 0},
                    ]
                },
                "effects": [{
                    "class_name": "ColorGrade",
                    "wheels": {
                        "enabled_keyframes": keyframe(1, 30),
                        "global": {
                            "color_keyframes": {
                                "red": keyframe(1, 30),
                                "green": keyframe(1, 30),
                                "blue": keyframe(1, 30),
                                "alpha": keyframe(1, 30),
                            },
                            "amount_keyframes": keyframe(1, 30),
                            "luma_keyframes": keyframe(1, 30),
                        },
                    },
                    "curve": {
                        "enabled": keyframe(1, 30),
                        "nodes": [{
                            "id": 1,
                            "x": keyframe(1, 30),
                            "y": {
                                "Points": [
                                    {"co": {"X": 1.0, "Y": 0.2}, "interpolation": 0},
                                    {"co": {"X": 30.0, "Y": 0.8}, "interpolation": 0},
                                ]
                            },
                            "left_handle_x": keyframe(1, 30),
                            "right_handle_y": keyframe(1, 30),
                        }],
                    },
                }],
            }],
            "effects": [{
                "id": "transition-1",
                "brightness": keyframe(1, 30),
            }],
        }

        KeyframeScaler(2.0)(data)

        clip = data["clips"][0]
        self.assertEqual(frame_numbers(clip["alpha"]), [1.0, 60])
        self.assertEqual(frame_numbers(clip["location"]["red"]), [1.0, 60])
        self.assertEqual(frame_numbers(clip["time"]), [1.0, 60])
        self.assertEqual([point["co"]["Y"] for point in clip["time"]["Points"]], [1.0, 60])

        effect = clip["effects"][0]
        wheels = effect["wheels"]
        self.assertEqual(frame_numbers(wheels["enabled_keyframes"]), [1.0, 60])
        self.assertEqual(frame_numbers(wheels["global"]["color_keyframes"]["red"]), [1.0, 60])
        self.assertEqual(frame_numbers(wheels["global"]["amount_keyframes"]), [1.0, 60])
        self.assertEqual(frame_numbers(wheels["global"]["luma_keyframes"]), [1.0, 60])

        curve = effect["curve"]
        node = curve["nodes"][0]
        self.assertEqual(frame_numbers(curve["enabled"]), [1.0, 60])
        self.assertEqual(frame_numbers(node["x"]), [1.0, 60])
        self.assertEqual(frame_numbers(node["y"]), [1.0, 60])
        self.assertEqual([point["co"]["Y"] for point in node["y"]["Points"]], [0.2, 0.8])
        self.assertEqual(frame_numbers(node["left_handle_x"]), [1.0, 60])
        self.assertEqual(frame_numbers(node["right_handle_y"]), [1.0, 60])

        self.assertEqual(frame_numbers(data["effects"][0]["brightness"]), [1.0, 60])

    def test_scales_nested_colorgrade_keyframes_without_color_channel_shortcut(self):
        data = {
            "clips": [{
                "effects": [{
                    "wheels": {
                        "highlights": {
                            "color_keyframes": {
                                "red": keyframe(1, 30),
                                "green": keyframe(1, 30),
                                "blue": keyframe(1, 30),
                                "alpha": keyframe(1, 30),
                            },
                        },
                    },
                    "curve": {
                        "nodes": [
                            {"id": 0, "right_handle_x": keyframe(1, 30)},
                            {"id": 1, "left_handle_y": keyframe(1, 30)},
                        ],
                    },
                }],
            }],
        }

        KeyframeScaler(2.0)(data)

        effect = data["clips"][0]["effects"][0]
        color = effect["wheels"]["highlights"]["color_keyframes"]
        self.assertEqual(frame_numbers(color["red"]), [1.0, 60])
        self.assertEqual(frame_numbers(color["green"]), [1.0, 60])
        self.assertEqual(frame_numbers(color["blue"]), [1.0, 60])
        self.assertEqual(frame_numbers(color["alpha"]), [1.0, 60])
        self.assertEqual(frame_numbers(effect["curve"]["nodes"][0]["right_handle_x"]), [1.0, 60])
        self.assertEqual(frame_numbers(effect["curve"]["nodes"][1]["left_handle_y"]), [1.0, 60])


if __name__ == "__main__":
    unittest.main()
