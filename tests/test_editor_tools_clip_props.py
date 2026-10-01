"""clip-props editor tools: read/set clip properties, keyframes, copy keyframes (headless editor)."""

import json
import os

import pytest

from classes.editor_tools import clip_props_model

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "editor_tools")
FPS = 30


@pytest.fixture(autouse=True)
def libopenshot_schema(monkeypatch):
    """libopenshot 1.0's Clip.PropertiesJSON / Clip.Json, captured in fixtures (no libopenshot here)."""
    with open(os.path.join(FIXTURES, "clip_properties.json"), encoding="utf-8") as fh:
        props = json.load(fh)
    with open(os.path.join(FIXTURES, "clip.json"), encoding="utf-8") as fh:
        shapes = json.load(fh)
    monkeypatch.setattr(clip_props_model, "_load_libopenshot_schema", lambda: (props, shapes))
    clip_props_model.reset_schema_cache()
    yield
    clip_props_model.reset_schema_cache()


@pytest.fixture(autouse=True)
def clip_utils_sees_the_editor(editor, monkeypatch):
    """clamp_timing_to_media (time curves) reads the project fps through clip_utils.get_app."""
    from classes import clip_utils
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)


def receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1]) if "\n" in out else {}


def pts(*pairs, interp=1):
    return {"Points": [{"co": {"X": float(x), "Y": float(y)}, "interpolation": interp} for x, y in pairs]}


def xs(curve):
    return [p["co"]["X"] for p in curve["Points"]]


def ys(curve):
    return [p["co"]["Y"] for p in curve["Points"]]


# ---------------------------------------------------------------------------
# get_clip_properties_tool
# ---------------------------------------------------------------------------

def test_get_reports_values_ranges_and_choices(editor):
    c = editor.add_clip(editor.add_file("video"), position=2.0)
    r = receipt(editor.call("get_clip_properties_tool", timeline_clip_id=c))
    props = r["properties"]
    assert props["alpha"]["value"] == 1.0 and props["alpha"]["keyframable"] is True
    assert props["alpha"]["min"] == 0.0 and props["alpha"]["max"] == 1.0
    assert props["scale"]["value"] == "Best Fit" and "Crop" in props["scale"]["choices"]
    assert props["gravity"]["value"] == "Center"
    assert "Multiply" in props["composite"]["choices"] and props["composite"]["keyframable"] is False
    assert props["volume"]["max"] == 1.3  # the Volume menu's 130%
    assert props["scale_x"]["max"] == 52 and props["scale_x"]["min"] == -52  # properties dock clamp, 1920x1080
    assert props["wave_color"]["value"] == "#007bffff"
    for hidden in ("position", "start", "end", "layer", "id", "perspective_c1_x"):
        assert hidden not in props
    assert r["timeline_clip_id"] == c and r["first_frame"] == 1
    assert editor.undo_steps_since_mark() == 0


def test_get_evaluates_keyframes_on_a_trimmed_clip(editor):
    # Trimmed 5 s into the source and placed at 10 s: the first visible frame is X=151.
    c = editor.add_clip(editor.add_file("video"), position=10.0, start=5.0, end=15.0,
                        scale_x=pts((151, 1.0), (271, 2.0)))
    r = receipt(editor.call("get_clip_properties_tool", timeline_clip_id=c, at_seconds=12.0,
                            properties=["Scale X", "alpha"]))
    assert set(r["properties"]) == {"scale_x", "alpha"}
    assert r["frame"] == 211 and r["clip_seconds"] == 2.0
    scale = r["properties"]["scale_x"]
    assert scale["value"] == pytest.approx(1.5) and scale["animated"] is True
    assert [k["clip_seconds"] for k in scale["keyframes"]] == [0.0, 4.0]
    assert [k["timeline_seconds"] for k in scale["keyframes"]] == [10.0, 14.0]
    assert "keyframes" not in r["properties"]["alpha"]


def test_get_refuses_a_time_outside_the_clip_and_unknown_names(editor):
    c = editor.add_clip(editor.add_file("video"), position=10.0, end=5.0)
    assert editor.call("get_clip_properties_tool", timeline_clip_id=c, at_seconds=2.0).startswith("Error")
    out = editor.call("get_clip_properties_tool", timeline_clip_id=c, properties=["sparkle"])
    assert out.startswith("Error") and "unknown clip property" in out


# ---------------------------------------------------------------------------
# set_clip_properties_tool
# ---------------------------------------------------------------------------

def test_set_accepts_names_labels_and_choice_names(editor):
    c = editor.add_clip(editor.add_file("video"))
    out = editor.call("set_clip_properties_tool", timeline_clip_ids=[c],
                      properties={"opacity": 0.5, "scale": "fit", "gravity": "top right", "Blend Mode": "multiply",
                                  "Rotation": 15, "wave_color": "#ff8800"})
    r = receipt(out)
    d = editor.clip(c)
    assert ys(d["alpha"]) == [0.5] and d["scale"] == 1 and d["gravity"] == 2 and d["composite"] == 13
    assert ys(d["rotation"]) == [15.0]
    assert ys(d["wave_color"]["red"]) == [255.0] and ys(d["wave_color"]["green"]) == [136.0]
    assert r["clips"][0]["changed"]["composite"] == {"before": "Normal", "after": "Multiply"}
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    d = editor.clip(c)
    assert ys(d["alpha"]) == [1.0] and d["composite"] == 0 and d["gravity"] == 4


def test_set_many_clips_in_one_undo_step(editor):
    f = editor.add_file("video")
    a, b = editor.add_clip(f, position=0.0), editor.add_clip(f, position=30.0)
    receipt(editor.call("set_clip_properties_tool", scope="all", properties={"alpha": 0.25}))
    assert ys(editor.clip(a)["alpha"]) == [0.25] and ys(editor.clip(b)["alpha"]) == [0.25]
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert ys(editor.clip(a)["alpha"]) == [1.0] and ys(editor.clip(b)["alpha"]) == [1.0]
    editor.redo()
    assert ys(editor.clip(b)["alpha"]) == [0.25]


def test_a_static_value_flattens_an_animation_and_says_so(editor):
    c = editor.add_clip(editor.add_file("video"), alpha=pts((1, 0.0), (31, 1.0)))
    r = receipt(editor.call("set_clip_properties_tool", timeline_clip_ids=[c], properties={"alpha": 0.5}))
    assert ys(editor.clip(c)["alpha"]) == [0.5]
    assert r["clips"][0]["flattened_animation"] == {"alpha": 2}


def test_at_seconds_keyframes_the_value_at_the_visible_time(editor):
    c = editor.add_clip(editor.add_file("video"), position=10.0, start=4.0, end=14.0,
                        scale_x=pts((121, 1.0), (421, 1.0)))
    receipt(editor.call("set_clip_properties_tool", timeline_clip_ids=[c], properties={"scale_x": 1.4},
                        at_seconds=12.0))
    curve = editor.clip(c)["scale_x"]
    assert xs(curve) == [121.0, 181.0, 421.0] and ys(curve)[1] == 1.4


@pytest.mark.parametrize("props,fragment", [
    ({"position": 3}, "timeline editing tools"),
    ({"layer": 2}, "timeline editing tools"),
    ({"id": "X"}, "read-only"),
    ({"perspective_c1_x": 0.2}, "does not render"),
    ({"sparkle": 1}, "unknown clip property"),
    ({"alpha": 2}, "between 0 and 1"),
    ({"alpha": "lots"}, "must be a number"),
    ({"gravity": "sideways"}, "must be one of"),
    ({"scale_x": 60}, "between -52 and 52"),
    ({"volume": 1.4}, "between 0 and 1.3"),
    ({"channel_filter": 1.5}, "whole number"),
    ({"time": 10}, "speed"),
    ({"wave_color": "not-a-color"}, "unrecognised color"),
])
def test_set_refusals_leave_history_untouched(editor, props, fragment):
    c = editor.add_clip(editor.add_file("video"))
    out = editor.call("set_clip_properties_tool", timeline_clip_ids=[c], properties=props)
    assert out.startswith("Error") and fragment in out, out
    assert editor.undo_steps_since_mark() == 0


def test_static_properties_take_no_at_seconds(editor):
    c = editor.add_clip(editor.add_file("video"))
    out = editor.call("set_clip_properties_tool", timeline_clip_ids=[c], properties={"composite": "screen"},
                      at_seconds=1.0)
    assert out.startswith("Error") and "cannot be animated" in out
    assert editor.undo_steps_since_mark() == 0


def test_mirror_percent_and_bool_values(editor):
    c = editor.add_clip(editor.add_file("video"))
    receipt(editor.call("set_clip_properties_tool", timeline_clip_ids=[c],
                        properties={"scale_x": -1, "volume": "80%", "waveform": True}))
    d = editor.clip(c)
    assert ys(d["scale_x"]) == [-1.0] and ys(d["volume"]) == [0.8] and d["waveform"] is True


def test_a_negative_scale_on_an_edge_anchored_clip_is_set_but_warned(editor):
    c = editor.add_clip(editor.add_file("video"), gravity=2)
    r = receipt(editor.call("set_clip_properties_tool", timeline_clip_ids=[c], properties={"scale_x": -0.5}))
    assert ys(editor.clip(c)["scale_x"]) == [-0.5]
    assert "off-screen" in r["clips"][0]["warnings"][0]
    r = receipt(editor.call("set_clip_properties_tool", timeline_clip_ids=[c],
                            properties={"gravity": "Top Center"}))
    assert "warnings" not in r["clips"][0]


def test_locked_tracks_are_refused_by_id_and_skipped_by_scope(editor):
    f = editor.add_file("video")
    editor.add_track(2000000, "Top")
    a = editor.add_clip(f, layer=1000000)
    b = editor.add_clip(f, layer=2000000)
    editor.lock_track(2000000)
    out = editor.call("set_clip_properties_tool", timeline_clip_ids=[b], properties={"alpha": 0.5})
    assert out.startswith("Error") and "locked" in out
    assert editor.undo_steps_since_mark() == 0
    r = receipt(editor.call("set_clip_properties_tool", scope="all", properties={"alpha": 0.5}))
    assert [s["timeline_clip_id"] for s in r["skipped"]] == [b]
    assert ys(editor.clip(a)["alpha"]) == [0.5] and ys(editor.clip(b)["alpha"]) == [1.0]


def test_setting_what_is_already_there_adds_no_undo_step(editor):
    c = editor.add_clip(editor.add_file("video"))
    r = receipt(editor.call("set_clip_properties_tool", timeline_clip_ids=[c],
                            properties={"alpha": 1.0, "scale": "Best Fit"}))
    assert r["changed"] is False
    assert editor.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# set_keyframes_tool
# ---------------------------------------------------------------------------

def test_keyframes_accept_the_pr183_arguments(editor):
    c = editor.add_clip(editor.add_file("video"), position=3.0)
    out = editor.call("set_keyframes_tool", timeline_clip_id=c, property="alpha", occurrence="0",
                      position_near=None, points=[{"frame": 1, "value": 0}, {"frame": 31, "value": 1}])
    r = receipt(out)
    curve = editor.clip(c)["alpha"]
    assert xs(curve) == [1.0, 31.0] and ys(curve) == [0.0, 1.0]
    assert r["property"] == "alpha" and [p["frame"] for p in r["points"]] == [1, 31]
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert ys(editor.clip(c)["alpha"]) == [1.0]


def test_seconds_count_from_the_visible_start_of_a_trimmed_clip(editor):
    c = editor.add_clip(editor.add_file("video"), position=20.0, start=4.0, end=12.0)
    receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="scale_x",
                        points=[{"seconds": 0, "value": 1}, {"seconds": 8, "value": 1.3}]))
    assert xs(editor.clip(c)["scale_x"]) == [121.0, 361.0]  # round(4*30)+1 .. round(12*30)+1


def test_timeline_and_source_seconds_and_frames_agree(editor):
    c = editor.add_clip(editor.add_file("video"), position=20.0, start=4.0, end=12.0)
    receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="location_x", points=[
        {"timeline_seconds": 21.0, "value": -0.5}, {"source_seconds": 7.0, "value": 0.0},
        {"frame": 301, "value": 0.5}]))
    assert xs(editor.clip(c)["location_x"]) == [151.0, 211.0, 301.0]


@pytest.mark.parametrize("point", [{"seconds": 9}, {"frame": 1}, {"timeline_seconds": 5},
                                   {"source_seconds": 1}])
def test_points_outside_the_clip_are_refused_with_the_valid_ranges(editor, point):
    c = editor.add_clip(editor.add_file("video"), position=20.0, start=4.0, end=12.0)
    out = editor.call("set_keyframes_tool", timeline_clip_id=c, property="alpha", points=[dict(point, value=1)])
    assert out.startswith("Error") and "outside clip" in out and "frames 121-361" in out
    assert editor.undo_steps_since_mark() == 0


def test_merge_and_replace_range_keep_the_other_points(editor):
    c = editor.add_clip(editor.add_file("video"), end=10.0, scale_x=pts((1, 1.0), (91, 1.2), (181, 1.0), (271, 1.5)))
    receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="scale_x", mode="merge",
                        points=[{"frame": 91, "value": 2.0}, {"frame": 121, "value": 1.1}]))
    assert xs(editor.clip(c)["scale_x"]) == [1.0, 91.0, 121.0, 181.0, 271.0]
    assert ys(editor.clip(c)["scale_x"])[1] == 2.0
    receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="scale_x", mode="replace_range",
                        points=[{"frame": 80, "value": 1.0}, {"frame": 200, "value": 1.0}]))
    assert xs(editor.clip(c)["scale_x"]) == [1.0, 80.0, 200.0, 271.0]
    assert editor.undo_steps_since_mark() == 2


def test_ease_presets_shape_the_segment_like_the_properties_dock(editor):
    c = editor.add_clip(editor.add_file("video"))
    receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="scale_y", points=[
        {"seconds": 0, "value": 1}, {"seconds": 2, "value": 1.5, "ease": "ease_in_out"},
        {"seconds": 3, "value": 1.5, "interpolation": "constant"}, {"seconds": 4, "value": 1, "interpolation": "linear"}]))
    p = editor.clip(c)["scale_y"]["Points"]
    assert p[0]["handle_right"] == {"X": 0.42, "Y": 0.0}
    assert p[1]["handle_left"] == {"X": 0.58, "Y": 1.0} and p[1]["interpolation"] == 0
    assert p[2]["interpolation"] == 2 and p[3]["interpolation"] == 1
    r = receipt(editor.call("get_clip_properties_tool", timeline_clip_id=c, properties=["scale_y"]))
    assert r["properties"]["scale_y"]["keyframes"][1]["ease"] == "ease_in_out"


def test_ease_needs_bezier(editor):
    c = editor.add_clip(editor.add_file("video"))
    out = editor.call("set_keyframes_tool", timeline_clip_id=c, property="alpha",
                      points=[{"seconds": 0, "value": 0}, {"seconds": 1, "value": 1, "ease": "ease_in",
                                                           "interpolation": "linear"}])
    assert out.startswith("Error") and "bezier" in out


def test_a_point_without_value_pins_the_current_value(editor):
    c = editor.add_clip(editor.add_file("video"), alpha=pts((1, 0.0), (61, 1.0)))
    receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="alpha", mode="merge",
                        points=[{"frame": 31}]))
    curve = editor.clip(c)["alpha"]
    assert xs(curve) == [1.0, 31.0, 61.0] and ys(curve)[1] == pytest.approx(0.5)


def test_choice_properties_step_and_accept_names(editor):
    c = editor.add_clip(editor.add_file("video"))
    receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="has_audio",
                        points=[{"seconds": 0, "value": "On"}, {"seconds": 2, "value": "off"}]))
    p = editor.clip(c)["has_audio"]["Points"]
    assert [q["co"]["Y"] for q in p] == [1.0, 0.0] and all(q["interpolation"] == 2 for q in p)


def test_color_keyframes_write_every_channel(editor):
    c = editor.add_clip(editor.add_file("video"))
    receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="wave_color",
                        points=[{"seconds": 0, "value": "red"}, {"seconds": 1, "value": "#0000ff"}]))
    wc = editor.clip(c)["wave_color"]
    assert ys(wc["red"]) == [255.0, 0.0] and ys(wc["blue"]) == [0.0, 255.0] and ys(wc["alpha"]) == [255.0, 255.0]


@pytest.mark.parametrize("prop,points,fragment", [
    ("composite", [{"seconds": 0, "value": 1}], "cannot be animated"),
    ("alpha", [], "at least one"),
    ("alpha", [{"value": 1}], "exactly one of"),
    ("alpha", [{"seconds": 0, "frame": 1, "value": 1}], "exactly one of"),
    ("alpha", [{"seconds": 0, "value": 1}, {"frame": 1, "value": 0}], "same frame"),
    ("alpha", [{"seconds": 0, "value": 5}], "between 0 and 1"),
    ("position", [{"seconds": 0, "value": 5}], "timeline editing tools"),
])
def test_keyframe_refusals(editor, prop, points, fragment):
    c = editor.add_clip(editor.add_file("video"))
    out = editor.call("set_keyframes_tool", timeline_clip_id=c, property=prop, points=points)
    assert out.startswith("Error") and fragment in out, out
    assert editor.undo_steps_since_mark() == 0


def test_keyframes_on_a_locked_track_are_refused(editor):
    c = editor.add_clip(editor.add_file("video"))
    editor.lock_track(1000000)
    out = editor.call("set_keyframes_tool", timeline_clip_id=c, property="alpha", points=[{"seconds": 0, "value": 1}])
    assert out.startswith("Error") and "locked" in out


def test_time_keyframes_are_clamped_and_saved_with_the_timing(editor):
    c = editor.add_clip(editor.add_file("video", duration=10.0), end=10.0)
    r = receipt(editor.call("set_keyframes_tool", timeline_clip_id=c, property="time",
                            points=[{"seconds": 0, "value": 1}, {"seconds": 10, "value": 150}]))
    assert ys(editor.clip(c)["time"]) == [1.0, 150.0]
    assert "start" in r and "end" in r
    out = editor.call("set_keyframes_tool", timeline_clip_id=c, property="time",
                      points=[{"seconds": 0, "value": 1}, {"seconds": 10, "value": 5000}])
    assert out.startswith("Error") and "between 1 and 300" in out


def test_same_keyframes_again_is_a_noop(editor):
    c = editor.add_clip(editor.add_file("video"))
    args = dict(timeline_clip_id=c, property="alpha", points=[{"frame": 1, "value": 0}, {"frame": 31, "value": 1}])
    receipt(editor.call("set_keyframes_tool", **args))
    editor.mark()
    r = receipt(editor.call("set_keyframes_tool", **args))
    assert r["changed"] is False and editor.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# remove_keyframes_tool
# ---------------------------------------------------------------------------

def test_removing_every_point_falls_back_to_the_dock_default(editor):
    c = editor.add_clip(editor.add_file("video"), scale_x=pts((1, 1.0), (301, 1.2)), alpha=pts((1, 0.0), (31, 1.0)))
    r = receipt(editor.call("remove_keyframes_tool", timeline_clip_ids=[c], properties=["scale_x"]))
    assert ys(editor.clip(c)["scale_x"]) == [1.0] and ys(editor.clip(c)["alpha"]) == [0.0, 1.0]
    assert r["clips"][0]["properties"]["scale_x"] == {"removed": 2, "remaining": 1, "reset_to": 1.0}
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert ys(editor.clip(c)["scale_x"]) == [1.0, 1.2]


def test_removing_a_timeline_range_keeps_the_rest(editor):
    c = editor.add_clip(editor.add_file("video"), position=10.0,
                        location_x=pts((1, 0.0), (61, 0.5), (121, 0.0), (181, 0.5)))
    receipt(editor.call("remove_keyframes_tool", timeline_clip_ids=[c], properties=["Location X"],
                        start_seconds=11.5, end_seconds=14.0))
    assert xs(editor.clip(c)["location_x"]) == [1.0, 181.0]


def test_all_clears_every_animated_property_only(editor):
    c = editor.add_clip(editor.add_file("video"), scale_x=pts((1, 0.5)), alpha=pts((1, 0.0), (31, 1.0)),
                        rotation=pts((1, 0.0), (91, 90.0)))
    r = receipt(editor.call("remove_keyframes_tool", timeline_clip_ids=[c], properties=["all"]))
    d = editor.clip(c)
    assert ys(d["alpha"]) == [1.0] and ys(d["rotation"]) == [0.0] and ys(d["scale_x"]) == [0.5]
    assert set(r["clips"][0]["properties"]) == {"alpha", "rotation"}


def test_nothing_to_remove_is_an_error_without_an_undo_step(editor):
    c = editor.add_clip(editor.add_file("video"))
    out = editor.call("remove_keyframes_tool", timeline_clip_ids=[c], properties=["all"])
    assert out.startswith("Error") and "no keyframes" in out
    assert editor.call("remove_keyframes_tool", timeline_clip_ids=[c], properties=["time"]).startswith("Error")
    assert editor.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# copy_clip_keyframes_tool
# ---------------------------------------------------------------------------

def test_copy_scale_stretches_a_whole_clip_zoom_onto_a_shorter_trimmed_clip(editor):
    f = editor.add_file("video")
    src = editor.add_clip(f, end=10.0, gravity=2, scale_x=pts((1, 1.0), (301, 1.2)), scale_y=pts((1, 1.0), (301, 1.2)))
    dst = editor.add_clip(f, position=20.0, start=2.0, end=7.0)
    r = receipt(editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst], groups=["scale"]))
    d = editor.clip(dst)
    assert xs(d["scale_x"]) == [61.0, 211.0] and ys(d["scale_x"]) == [1.0, 1.2]
    assert d["gravity"] == 2 and ys(d["alpha"]) == [1.0]
    assert r["clips"][0]["properties"] == ["gravity", "scale_x", "scale_y"]
    assert editor.undo_steps_since_mark() == 1


def test_copy_offset_and_exact_timing(editor):
    f = editor.add_file("video")
    src = editor.add_clip(f, end=10.0, alpha=pts((1, 0.0), (31, 1.0)))
    dst = editor.add_clip(f, position=20.0, start=2.0, end=7.0)
    receipt(editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst], groups=["alpha"],
                        timing="offset"))
    assert xs(editor.clip(dst)["alpha"]) == [61.0, 91.0]
    receipt(editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst], groups=["alpha"],
                        timing="exact"))
    assert xs(editor.clip(dst)["alpha"]) == [1.0, 31.0]


def test_copying_a_fade_from_a_trimmed_clip_keeps_its_start(editor):
    """Found live: the trimmed source's default point at X=1 (alpha 1, before its visible start) and
    its fade start at X=61 (alpha 0) both stretched onto the target's first frame, and the wrong one
    won, so the copied fade-in vanished."""
    f = editor.add_file("video")
    src = editor.add_clip(f, start=2.0, end=12.5, alpha=pts((1, 1.0), (61, 0.0), (121, 1.0), (316, 1.0), (376, 0.0)))
    dst = editor.add_clip(f, position=20.0, end=6.0)
    receipt(editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst], groups=["alpha"]))
    curve = editor.clip(dst)["alpha"]
    assert xs(curve)[0] == 1.0 and ys(curve)[0] == 0.0
    assert ys(curve) == [0.0, 1.0, 1.0, 0.0] and xs(curve)[-1] == 181.0


def test_points_outside_the_source_range_become_exact_edge_values(editor):
    f = editor.add_file("video")
    src = editor.add_clip(f, start=2.0, end=12.0, scale_x=pts((1, 1.0), (661, 2.0)))  # visible 61-361
    dst = editor.add_clip(f, position=20.0, end=10.0)
    receipt(editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst], groups=["scale"],
                        timing="offset"))
    curve = editor.clip(dst)["scale_x"]
    assert xs(curve) == [1.0, 301.0] and ys(curve) == [pytest.approx(1 + 60 / 660), pytest.approx(1 + 360 / 660)]


def test_copy_all_leaves_the_speed_curve_unless_exact(editor):
    f = editor.add_file("video")
    src = editor.add_clip(f, end=10.0, time=pts((1, 1.0), (301, 150.0)), volume=pts((1, 0.2)))
    dst = editor.add_clip(f, position=20.0, end=10.0)
    r = receipt(editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst]))
    assert ys(editor.clip(dst)["time"]) == [1.0] and ys(editor.clip(dst)["volume"]) == [0.2]
    assert r["not_copied"] == ["time"]
    out = editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst], groups=["time"])
    assert out.startswith("Error") and "timing='exact'" in out
    receipt(editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst], groups=["time"],
                        timing="exact"))
    assert ys(editor.clip(dst)["time"]) == [1.0, 150.0]


def test_copy_refuses_bad_targets(editor):
    f = editor.add_file("video")
    src = editor.add_clip(f, end=10.0)
    out = editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[src])
    assert out.startswith("Error") and "source clip itself" in out
    assert editor.call("copy_clip_keyframes_tool", source_clip_id=src).startswith("Error")
    dst = editor.add_clip(f, position=20.0)
    editor.lock_track(1000000)
    out = editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst])
    assert out.startswith("Error") and "locked" in out
    assert editor.undo_steps_since_mark() == 0


def test_copying_identical_keyframes_is_a_noop(editor):
    f = editor.add_file("video")
    src = editor.add_clip(f, end=10.0)
    dst = editor.add_clip(f, position=20.0, end=10.0)
    r = receipt(editor.call("copy_clip_keyframes_tool", source_clip_id=src, timeline_clip_ids=[dst]))
    assert r["changed"] is False and editor.undo_steps_since_mark() == 0
