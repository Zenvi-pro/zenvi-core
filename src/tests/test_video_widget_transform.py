"""
 @file
 @brief This file contains unit tests for VideoWidget transform and location geometry
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

import openshot
from qt_api import QApplication, QRect


PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from qt_test_app import ensure_app_state, get_or_create_app


class DummySettings:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, False)

    def set(self, key, value):
        self.values[key] = value


class DummyApp(QApplication):
    def __init__(self):
        super().__init__([])
        self.settings = DummySettings()


app, _owns_app = get_or_create_app(DummyApp)
ensure_app_state(app, DummySettings, extra_attrs={"window": types.SimpleNamespace()})

from windows.video_widget import VideoWidget


def clip_with(scale_mode, gravity=openshot.GRAVITY_CENTER):
    return types.SimpleNamespace(data={"scale": scale_mode, "gravity": gravity})


def props(location_x=0.0, location_y=0.0, scale_x=1.0, scale_y=1.0):
    return {
        "scale_x": {"value": scale_x},
        "scale_y": {"value": scale_y},
        "location_x": {"value": location_x},
        "location_y": {"value": location_y},
        "parentObjectId": {"memo": ""},
    }


class VideoWidgetTransformTests(unittest.TestCase):
    def setUp(self):
        self.widget = VideoWidget.__new__(VideoWidget)
        self.viewport = QRect(0, 0, 160, 90)

    def rect_for(self, scale_mode, location_x=0.0, location_y=0.0, scale_x=1.0, scale_y=1.0):
        return VideoWidget._clip_display_rect(
            self.widget,
            40,
            40,
            clip_with(scale_mode),
            props(location_x, location_y, scale_x, scale_y),
            self.viewport,
        )

    def test_square_clip_location_y_endpoints_are_offscreen_for_fit_and_crop(self):
        for scale_mode in (openshot.SCALE_FIT, openshot.SCALE_CROP):
            with self.subTest(scale_mode=scale_mode):
                top = self.rect_for(scale_mode, location_y=-1.0)
                bottom = self.rect_for(scale_mode, location_y=1.0)

                self.assertLessEqual(top.y() + top.height(), 0.0)
                self.assertGreaterEqual(bottom.y(), self.viewport.height())

    def test_square_clip_location_x_endpoints_are_offscreen_for_fit_and_crop(self):
        for scale_mode in (openshot.SCALE_FIT, openshot.SCALE_CROP):
            with self.subTest(scale_mode=scale_mode):
                left = self.rect_for(scale_mode, location_x=-1.0)
                right = self.rect_for(scale_mode, location_x=1.0)

                self.assertLessEqual(left.x() + left.width(), 0.0)
                self.assertGreaterEqual(right.x(), self.viewport.width())

    def test_location_offset_inverse_round_trips_drag_motion(self):
        # Crop square in a 16:9 viewport renders as 160x160, centered at y=-35.
        source_w, source_h, scaled_w, scaled_h, anchor_x, anchor_y = (
            VideoWidget._clip_location_geometry(
                self.widget,
                40,
                40,
                clip_with(openshot.SCALE_CROP),
                props(),
                self.viewport,
            )
        )
        self.assertEqual((source_w, source_h, scaled_w, scaled_h, anchor_x, anchor_y),
                         (160.0, 160.0, 160.0, 160.0, 0.0, -35.0))

        for location in (-1.0, -0.5, 0.0, 0.5, 1.0):
            with self.subTest(location=location):
                offset = VideoWidget._location_offset(location, anchor_y, self.viewport.height(), scaled_h)
                restored = VideoWidget._location_value_from_offset(
                    offset, anchor_y, self.viewport.height(), scaled_h)
                self.assertAlmostEqual(restored, location, places=6)

    def test_scale_none_uses_project_to_viewport_pixel_ratio(self):
        fake_app = types.SimpleNamespace(
            project=types.SimpleNamespace(get={"width": 320, "height": 180}.get)
        )
        with patch("windows.video_widget.get_app", return_value=fake_app):
            center = self.rect_for(openshot.SCALE_NONE)
            self.assertAlmostEqual(center.width(), 20.0)
            self.assertAlmostEqual(center.height(), 20.0)
            self.assertAlmostEqual(center.x(), 70.0)
            self.assertAlmostEqual(center.y(), 35.0)

            top = self.rect_for(openshot.SCALE_NONE, location_y=-1.0)
            bottom = self.rect_for(openshot.SCALE_NONE, location_y=1.0)
            self.assertLessEqual(top.y() + top.height(), 0.0)
            self.assertGreaterEqual(bottom.y(), self.viewport.height())


if __name__ == "__main__":
    unittest.main()
