"""From a located object to the masking tool's arguments."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index import handoff as H  # noqa: E402

HIT = {"file_id": "F9", "label": "kettle", "t": 2.0, "box": [0.5, 0.4, 0.1, 0.2]}


def test_a_hit_becomes_a_padded_pixel_box_a_centre_point_and_the_frame_at_its_time():
    out = H.mask_handoff(HIT, 1920, 1080, 25.0, frames=250)
    a = out["args"]
    assert out["tool"] == "enhance_file_with_comfyui_tool" and out["frame_size"] == [1920, 1080]
    assert a["boxes"] == [{"x1": 950, "y1": 421, "x2": 1162, "y2": 659}], "0.495-0.605 of 1920 and 0.39-0.61 of 1080: each side grown by 5% of the box"
    assert a["points"] == [{"x": 1056, "y": 540}] and a["seed_frame"] == 51 and a["prompt"] == "kettle" and a["file_id"] == "F9" and "action" not in a


def test_the_box_stays_inside_the_frame_and_the_point_on_a_pixel():
    edge = H.mask_handoff({**HIT, "box": [0.0, 0.0, 1.0, 1.0]}, 640, 360, 30.0)["args"]
    assert edge["boxes"] == [{"x1": 0, "y1": 0, "x2": 640, "y2": 360}] and edge["points"] == [{"x": 320, "y": 180}]
    corner = H.mask_handoff({**HIT, "box": [0.9, 0.9, 0.1, 0.1]}, 100, 100, 30.0)["args"]
    assert corner["boxes"][0]["x2"] == 100 and corner["points"][0]["x"] <= 99


def test_the_seed_frame_is_one_based_and_never_past_the_end():
    assert H.mask_handoff({**HIT, "t": 0.0}, 640, 360, 30.0)["args"]["seed_frame"] == 1
    assert H.mask_handoff({**HIT, "t": 100.0}, 640, 360, 30.0, frames=90)["args"]["seed_frame"] == 90
    assert H.mask_handoff({**HIT, "t": 1.0}, 640, 360, 0.0)["args"]["seed_frame"] == 1, "no frame rate known"


@pytest.mark.parametrize("hit,w,h", [({**HIT, "box": None}, 640, 360), ({**HIT, "box": [0.1, 0.1]}, 640, 360), ({**HIT, "box": [0.1, 0.1, 0.0, 0.2]}, 640, 360),
                                      ({**HIT, "box": ["a", 0, 1, 1]}, 640, 360), (HIT, 0, 360), (HIT, 640, 0)])
def test_nothing_is_made_without_a_usable_box_and_frame_size(hit, w, h):
    assert H.mask_handoff(hit, w, h, 25.0) is None


def test_the_action_is_added_when_it_is_one_the_masking_tool_has():
    assert H.mask_handoff(HIT, 640, 360, 25.0, action="blur_object")["args"]["action"] == "blur_object"
    with pytest.raises(ValueError, match="action must be one of"):
        H.mask_handoff(HIT, 640, 360, 25.0, action="explode")
