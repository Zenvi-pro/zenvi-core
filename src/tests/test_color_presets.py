"""
 @file
 @brief Unit tests for Color Grade preset helpers
"""

import os
import sys
import unittest

PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from classes.color_presets import (  # noqa: E402
    COLOR_PRESET_AUTO_CONTRAST,
    COLOR_PRESET_BOOST_COLOR,
    COLOR_PRESET_LIFT_SHADOWS,
    COLOR_PRESET_WARM_UP,
    apply_color_grade_preset,
    default_curve_data,
    default_wheels_data,
)


class ColorPresetTests(unittest.TestCase):
    def test_auto_contrast_replaces_defaults_with_mild_s_curve(self):
        payload = apply_color_grade_preset({}, COLOR_PRESET_AUTO_CONTRAST)
        self.assertEqual(payload["contrast"]["Points"][0]["co"]["Y"], 0.18)
        self.assertEqual(payload["highlights"]["Points"][0]["co"]["Y"], -0.08)
        self.assertEqual(payload["shadows"]["Points"][0]["co"]["Y"], 0.08)
        self.assertEqual(payload["curve_all"]["nodes"][1]["id"], 1)

    def test_lift_shadows_uses_fresh_neutral_channel_curves_and_wheels(self):
        payload = apply_color_grade_preset({
            "curve_red": {"enabled": False, "points": [{"x": 0.0, "y": 1.0}, {"x": 1.0, "y": 0.0}]},
            "wheels": {"enabled": False},
        }, COLOR_PRESET_LIFT_SHADOWS)
        self.assertEqual(payload["curve_red"], default_curve_data())
        self.assertEqual(payload["curve_green"], default_curve_data())
        self.assertEqual(payload["curve_blue"], default_curve_data())
        self.assertEqual(payload["wheels"], default_wheels_data())
        self.assertEqual(payload["shadows"]["Points"][0]["co"]["Y"], 0.22)

    def test_warm_up_and_boost_color_set_expected_primary_controls(self):
        warm = apply_color_grade_preset({}, COLOR_PRESET_WARM_UP)
        boost = apply_color_grade_preset({}, COLOR_PRESET_BOOST_COLOR)
        self.assertEqual(warm["temperature"]["Points"][0]["co"]["Y"], 0.18)
        self.assertEqual(warm["tint"]["Points"][0]["co"]["Y"], 0.03)
        self.assertEqual(boost["saturation"]["Points"][0]["co"]["Y"], 1.18)
        self.assertEqual(boost["vibrance"]["Points"][0]["co"]["Y"], 0.22)


if __name__ == "__main__":
    unittest.main()
