"""
 @file
 @brief Unit tests for camera motion framing helpers
 @author OpenShot Studios

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
 """

import os
import sys
import unittest


PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from classes.camera_motion import (
    KEN_BURNS_AUTO,
    KEN_BURNS_LEFT_TO_RIGHT,
    KEN_BURNS_TOP_TO_BOTTOM,
    PAN_AUTO,
    PAN_BOTTOM_TO_TOP,
    PAN_RIGHT,
    PAN_LEFT_TO_RIGHT,
    PAN_UP,
    camera_pan_keyframes,
    ken_burns_keyframes,
    push_pull_keyframes,
    source_dimensions_from_reader,
)


class CameraMotionTests(unittest.TestCase):
    def test_wide_media_pans_horizontally_without_extra_zoom(self):
        values = camera_pan_keyframes(PAN_LEFT_TO_RIGHT, 1920, 1080, 3840, 1080)

        self.assertEqual(values.scale_x, (1.0, 1.0))
        self.assertEqual(values.scale_y, (1.0, 1.0))
        self.assertGreater(values.location_x[0], 0.0)
        self.assertLess(values.location_x[1], 0.0)
        self.assertEqual(values.location_y, (0.0, 0.0))

    def test_tall_media_pans_vertically_without_extra_zoom(self):
        values = camera_pan_keyframes(PAN_UP, 1920, 1080, 1080, 3840)

        self.assertEqual(values.scale_x, (1.0, 1.0))
        self.assertEqual(values.scale_y, (1.0, 1.0))
        self.assertLess(values.location_y[0], 0.0)
        self.assertGreater(values.location_y[1], 0.0)
        self.assertGreater(abs(values.location_y[0]), 0.4)
        self.assertEqual(values.location_x, (0.0, 0.0))

    def test_tall_two_by_three_image_pans_to_crop_edges(self):
        values = camera_pan_keyframes(PAN_BOTTOM_TO_TOP, 1920, 1080, 1024, 1536)

        self.assertEqual(values.scale_y, (1.0, 1.0))
        self.assertAlmostEqual(abs(values.location_y[0]), 0.452272, places=5)
        self.assertAlmostEqual(abs(values.location_y[1]), 0.452272, places=5)

    def test_auto_pan_chooses_natural_direction(self):
        values = camera_pan_keyframes(PAN_AUTO, 1920, 1080, 1024, 1536)

        self.assertEqual(values.location_x, (0.0, 0.0))
        self.assertLess(values.location_y[0], 0.0)
        self.assertGreater(values.location_y[1], 0.0)

    def test_cross_axis_pan_adds_only_needed_zoom(self):
        values = camera_pan_keyframes(PAN_RIGHT, 1920, 1080, 1080, 1920)

        self.assertGreater(values.scale_x[0], 1.0)
        self.assertEqual(values.scale_x, values.scale_y)
        self.assertGreater(values.location_x[0], 0.0)
        self.assertLess(values.location_x[1], 0.0)
        self.assertEqual(values.location_y, (0.0, 0.0))

    def test_push_pull_are_centered_zoom_only(self):
        push = push_pull_keyframes(zoom_in=True)
        pull = push_pull_keyframes(zoom_in=False)

        self.assertEqual(push.scale_x[0], 1.0)
        self.assertGreater(push.scale_x[1], 1.0)
        self.assertGreater(pull.scale_x[0], 1.0)
        self.assertEqual(pull.scale_x[1], 1.0)
        self.assertEqual(push.location_x, (0.0, 0.0))
        self.assertEqual(pull.location_y, (0.0, 0.0))

    def test_auto_ken_burns_chooses_wide_axis(self):
        values = ken_burns_keyframes(True, KEN_BURNS_AUTO, 1920, 1080, 3840, 1080)

        self.assertEqual(values.scale_x[0], 1.0)
        self.assertGreater(values.scale_x[1], 1.0)
        self.assertGreater(values.location_x[0], 0.0)
        self.assertLess(values.location_x[1], 0.0)
        self.assertEqual(values.location_y, (0.0, 0.0))

    def test_auto_ken_burns_chooses_tall_axis(self):
        values = ken_burns_keyframes(True, KEN_BURNS_AUTO, 1920, 1080, 1080, 3840)

        self.assertEqual(values.location_x, (0.0, 0.0))
        self.assertLess(values.location_y[0], 0.0)
        self.assertGreater(values.location_y[1], 0.0)

    def test_forced_ken_burns_direction_uses_requested_axis(self):
        values = ken_burns_keyframes(True, KEN_BURNS_TOP_TO_BOTTOM, 1920, 1080, 1080, 3840)

        self.assertEqual(values.location_x, (0.0, 0.0))
        self.assertGreater(values.location_y[0], 0.0)
        self.assertLess(values.location_y[1], 0.0)

    def test_ken_burns_out_reverses_zoom_but_keeps_requested_travel(self):
        values = ken_burns_keyframes(False, KEN_BURNS_LEFT_TO_RIGHT, 1920, 1080, 3840, 1080)

        self.assertGreater(values.scale_x[0], 1.0)
        self.assertEqual(values.scale_x[1], 1.0)
        self.assertGreater(values.location_x[0], 0.0)
        self.assertLess(values.location_x[1], 0.0)

    def test_source_dimensions_from_reader_accepts_display_dimensions(self):
        self.assertEqual(
            source_dimensions_from_reader({"display_width": 800, "display_height": 600}),
            (800.0, 600.0),
        )
        self.assertEqual(source_dimensions_from_reader({}), (None, None))


if __name__ == "__main__":
    unittest.main()
