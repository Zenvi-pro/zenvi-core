"""The Mask transition rules shared by the timeline and the transition tools (classes.transition_ops)."""

import copy
import os

from classes import transition_ops as ops

FPS = 30.0


def _pts(kf):
    return [(p["co"]["X"], p["co"]["Y"]) for p in kf["Points"]]


def _static(duration=1.0, position=10.0, **extra):
    data = ops.new_mask_transition("T1", {"path": "/t/fade.svg", "has_single_image": True},
                                   position=position, layer=3, duration=duration, fps_float=FPS)
    data.update(extra)
    return data


def test_new_mask_transition_matches_the_timeline_defaults():
    data = _static(duration=1.0)
    assert data["type"] == "Mask" and data["start"] == 0 and data["end"] == 1.0
    assert _pts(data["brightness"]) == [(1.0, 1.0), (31.0, -1.0)]
    assert _pts(data["contrast"]) == [(1.0, 3.0)]
    assert data["brightness"]["Points"][0]["handle_left"] == {"X": 0.5, "Y": 1.0}
    assert "fade_audio_hint" not in data and "resource" not in data
    assert ops.new_mask_transition("X", {}, 0, 1, 1, FPS, fade_audio=True, resource="/r")["fade_audio_hint"]


def test_default_keyframes_constant_curve_has_one_point():
    b, c = ops.default_keyframes(2.0, 0.0, 0.0, 0.0, FPS)
    assert _pts(b) == [(1.0, 0.0)] and _pts(c) == [(1.0, 0.0)]


def test_reverse_mirrors_points_and_swaps_handles():
    kf = {"Points": [
        {"co": {"X": 1, "Y": 1.0}, "handle_left": {"X": 0.1, "Y": 0.1}, "handle_right": {"X": 0.9, "Y": 0.9}},
        {"co": {"X": 31, "Y": -1.0}, "handle_left": {"X": 0.2, "Y": 0.2}, "handle_right": {"X": 0.8, "Y": 0.8}},
    ]}
    ops.reverse_keyframes(kf)
    assert _pts(kf) == [(1, -1.0), (31, 1.0)]
    assert kf["Points"][0]["handle_left"] == {"X": 0.8, "Y": 0.8}
    data = _static()
    ops.reverse_transition_data(data)
    assert ops.direction_of(data) == "reversed"
    ops.reverse_transition_data(data)
    assert ops.direction_of(data) == "default"


def test_prepare_update_rescales_static_curves_to_the_new_length():
    old = _static(duration=1.0)
    new = copy.deepcopy(old)
    new["end"] = 2.0
    new.pop("brightness")
    out = ops.prepare_update(new, old, FPS, only_basic_props=True)
    assert _pts(out["brightness"]) == [(1.0, 1.0), (61.0, -1.0)]


def test_prepare_update_resets_curves_when_the_reader_changes():
    old = _static(duration=1.0)
    ops.reverse_transition_data(old)
    new = copy.deepcopy(old)
    new["reader"] = {"path": "/t/wipe.svg", "has_single_image": True}
    out = ops.prepare_update(new, old, FPS, only_basic_props=False)
    assert ops.direction_of(out) == "default"


def test_prepare_update_new_basic_transition_keeps_only_basic_keys():
    new = _static()
    out = ops.prepare_update(new, {}, FPS, only_basic_props=True)
    assert set(out) == {"id", "layer", "position", "start", "end", "brightness", "contrast"}


def test_auto_orient_flips_only_on_a_right_edge():
    clip = {"position": 10.0, "start": 0.0, "end": 5.0}
    left = _static(position=10.0)
    ops.auto_orient_keyframes(left, [clip])
    assert ops.direction_of(left) == "default"
    right = _static(position=14.0)
    ops.auto_orient_keyframes(right, [clip])
    assert ops.direction_of(right) == "reversed"


def test_rescale_curves_ignores_animated_masks():
    data = _static(duration=1.0)
    data["reader"] = {"path": "/m/wipe.mp4", "has_single_image": False, "media_type": "video"}
    before = copy.deepcopy(data["brightness"])
    ops.rescale_curves(data, 1.0, 2.0, FPS)
    assert data["brightness"] == before
    static = _static(duration=1.0)
    ops.rescale_curves(static, 1.0, 0.5, FPS)
    assert _pts(static["brightness"])[-1] == (16.0, -1.0)


def test_find_transition_exact_alias_partial_and_ambiguous():
    entries = [
        {"name": "Fade", "key": "fade", "filename": "fade.svg", "category": "common", "path": "/c/fade.svg"},
        {"name": "Wipe left to right", "key": "wipe_left_to_right", "filename": "w.svg", "category": "common",
         "path": "/c/wipe_left_to_right.svg"},
        {"name": "Wipe right to left", "key": "wipe_right_to_left", "filename": "w2.svg", "category": "common",
         "path": "/c/wipe_right_to_left.svg"},
        {"name": "Fractal 1", "key": "fractal_1", "filename": "fractal_1.jpg", "category": "extra",
         "path": "/e/fractal_1.jpg"},
    ]
    assert ops.find_transition("Fade", entries)[0]["key"] == "fade"
    assert ops.find_transition("crossfade", entries)[0]["key"] == "fade"
    assert ops.find_transition("wipe right", entries)[0]["key"] == "wipe_left_to_right"
    assert ops.find_transition("Wipe_Left_To_Right.svg", entries)[0]["key"] == "wipe_left_to_right"
    assert ops.find_transition("fractal", entries)[0]["key"] == "fractal_1"
    entry, candidates = ops.find_transition("wipe", entries)
    assert entry is None and set(candidates) == {"wipe_left_to_right", "wipe_right_to_left"}
    assert ops.find_transition("nothing-like-it", entries) == (None, [])


def test_catalog_lists_common_extra_and_the_user_folder(tmp_path, monkeypatch):
    from classes import info
    user = tmp_path / "transitions"
    user.mkdir()
    (user / "my_wipe.png").write_bytes(b"x")
    (user / ".hidden.png").write_bytes(b"x")
    (user / "notes.txt").write_text("x")
    monkeypatch.setattr(info, "TRANSITIONS_PATH", str(user))
    entries = ops.catalog()
    cats = {e["category"] for e in entries}
    assert {"common", "extra", "user"} <= cats
    user_keys = [e["key"] for e in entries if e["category"] == "user"]
    assert user_keys == ["my_wipe"]
    assert any(e["key"] == "fade" and os.path.isfile(e["path"]) for e in entries)
    assert ops.find_transition("my wipe", entries)[0]["category"] == "user"


def test_legacy_list_and_search_tools_include_the_user_folder(tmp_path, monkeypatch):
    """Regression: list_transitions_tool / search_transitions_tool skipped ~/.openshot_qt/transitions."""
    import json
    from classes import info, tool_handlers
    user = tmp_path / "transitions"
    user.mkdir()
    (user / "brand_swipe.png").write_bytes(b"x")
    monkeypatch.setattr(info, "TRANSITIONS_PATH", str(user))
    listed = json.loads(tool_handlers.list_transitions(category="user"))
    assert listed["total"] == 1 and listed["transitions"][0]["filename"] == "brand_swipe.png"
    found = json.loads(tool_handlers.search_transitions(query="brand"))
    assert found["matches"] == 1 and found["transitions"][0]["category"] == "user"
    common = json.loads(tool_handlers.list_transitions(category="common"))
    assert common["total"] == 7 and {t["category"] for t in common["transitions"]} == {"common"}
