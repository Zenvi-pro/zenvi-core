"""
 @file
 @brief Unit tests for Film Grain preset helpers
"""

import os
import sys
import unittest

PATH = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if PATH not in sys.path:
    sys.path.append(PATH)

from classes.film_grain_presets import (  # noqa: E402
    FILM_GRAIN_PRESET_16MM_CLASSIC,
    FILM_GRAIN_PRESET_35MM_CLASSIC,
    FILM_GRAIN_PRESET_35MM_FINE,
    FILM_GRAIN_PRESET_35MM_GRITTY,
    FILM_GRAIN_PRESET_HIGH_ISO,
    FILM_GRAIN_PRESET_SUPER_8,
    apply_film_grain_preset,
    is_film_grain_effect,
)


class FilmGrainPresetTests(unittest.TestCase):
    def test_presets_initialize_all_visible_keyframe_controls(self):
        for preset in (
            FILM_GRAIN_PRESET_35MM_FINE,
            FILM_GRAIN_PRESET_35MM_CLASSIC,
            FILM_GRAIN_PRESET_35MM_GRITTY,
            FILM_GRAIN_PRESET_16MM_CLASSIC,
            FILM_GRAIN_PRESET_SUPER_8,
            FILM_GRAIN_PRESET_HIGH_ISO,
        ):
            payload = apply_film_grain_preset({"class_name": "FilmGrain"}, preset)
            for key in (
                "amount",
                "size",
                "softness",
                "clump",
                "shadows",
                "midtones",
                "highlights",
                "color_amount",
                "color_variation",
                "evolution",
                "coherence",
            ):
                self.assertIn("Points", payload[key])
                self.assertEqual(payload[key]["Points"][0]["co"]["X"], 1.0)
            self.assertEqual(payload["seed"], 1)

    def test_presets_get_progressively_stronger_and_coarser(self):
        fine = apply_film_grain_preset({}, FILM_GRAIN_PRESET_35MM_FINE)
        classic = apply_film_grain_preset({}, FILM_GRAIN_PRESET_35MM_CLASSIC)
        gritty = apply_film_grain_preset({}, FILM_GRAIN_PRESET_35MM_GRITTY)
        sixteen = apply_film_grain_preset({}, FILM_GRAIN_PRESET_16MM_CLASSIC)
        super8 = apply_film_grain_preset({}, FILM_GRAIN_PRESET_SUPER_8)

        self.assertLess(fine["amount"]["Points"][0]["co"]["Y"], classic["amount"]["Points"][0]["co"]["Y"])
        self.assertLess(classic["amount"]["Points"][0]["co"]["Y"], gritty["amount"]["Points"][0]["co"]["Y"])
        self.assertLess(gritty["amount"]["Points"][0]["co"]["Y"], sixteen["amount"]["Points"][0]["co"]["Y"])
        self.assertLess(sixteen["amount"]["Points"][0]["co"]["Y"], super8["amount"]["Points"][0]["co"]["Y"])
        self.assertLess(fine["size"]["Points"][0]["co"]["Y"], super8["size"]["Points"][0]["co"]["Y"])
        self.assertLess(fine["clump"]["Points"][0]["co"]["Y"], super8["clump"]["Points"][0]["co"]["Y"])

    def test_seed_is_plain_int_and_preserved_when_existing(self):
        payload = apply_film_grain_preset({"class_name": "FilmGrain", "seed": 1234}, FILM_GRAIN_PRESET_16MM_CLASSIC)
        self.assertEqual(payload["seed"], 1234)
        self.assertIsInstance(payload["seed"], int)

    def test_is_film_grain_effect(self):
        self.assertTrue(is_film_grain_effect({"class_name": "FilmGrain"}))
        self.assertFalse(is_film_grain_effect({"class_name": "ColorGrade"}))
        self.assertFalse(is_film_grain_effect(None))


if __name__ == "__main__":
    unittest.main()
