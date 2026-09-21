"""JSON-only visible clip listing for inspect (no libopenshot)."""

from __future__ import annotations

from fractions import Fraction

from classes.agent_tools.visible_clips import mid_bin_frames, visible_clips_at


def test_mid_bin_frames_example():
    assert mid_bin_frames(0, 60, 6) == [5, 15, 25, 35, 45, 55]


def test_visible_clips_top_layer_first_hides_audio_and_hidden():
    project = {
        "layers": [
            {"number": 0, "y": 1},
            {"number": 1, "y": 1},
            {"number": 2, "y": 0},  # hidden
        ],
        "clips": [
            {
                "id": "bottom",
                "layer": 0,
                "position": 0.0,
                "start": 0.0,
                "end": 5.0,
                "scale_x": 1.0,
            },
            {
                "id": "top",
                "layer": 1,
                "position": 0.0,
                "start": 0.0,
                "end": 5.0,
                "effects": [{"class_name": "Brightness"}],
            },
            {
                "id": "hidden",
                "layer": 2,
                "position": 0.0,
                "start": 0.0,
                "end": 5.0,
            },
            {
                "id": "audio",
                "layer": 0,
                "position": 0.0,
                "start": 0.0,
                "end": 5.0,
                "has_video": False,
            },
        ],
    }
    seen = visible_clips_at(project, seconds=1.0, fps=Fraction(30, 1))
    assert [c["id"] for c in seen] == ["top", "bottom"]
    assert seen[0]["effects"] == ["Brightness"]
    assert "landedFrame" in seen[0]
