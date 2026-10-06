"""effects-color editor tools: catalog, add/update/remove/copy effects (classes.editor_tools.effects_color)."""

import pytest

from classes import effect_ops
from effects_color_helpers import effect_of, install_effect_fixtures, receipt, x, y


@pytest.fixture
def fx(editor, monkeypatch):
    return install_effect_fixtures(editor, monkeypatch)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_list_effects_counts_and_filters(fx):
    r = receipt(fx.call("list_effects_tool"))
    assert len(r["effects"]) == 43
    video = receipt(fx.call("list_effects_tool", kind="video"))["effects"]
    audio = receipt(fx.call("list_effects_tool", kind="audio"))["effects"]
    assert len(video) == 34 and len(audio) == 9
    assert {"Compressor", "Echo", "ParametricEQ"} <= {e["effect"] for e in audio}
    blur = receipt(fx.call("list_effects_tool", query="blur"))["effects"]
    assert "Blur" in {e["effect"] for e in blur}
    assert fx.undo_steps_since_mark() == 0


def test_list_effects_describes_one_effect_by_alias(fx):
    r = receipt(fx.call("list_effects_tool", effect="greenscreen"))
    props = r["effect"]["properties"]
    assert r["effect"]["effect"] == "ChromaKey"
    assert props["fuzz"]["min"] == 0 and props["fuzz"]["max"] == 125 and props["fuzz"]["default"] == 20
    assert {"name": "HSV/HSL hue", "value": 1} in props["keymethod"]["choices"]
    assert "id" not in props and "position" not in props


def test_list_effects_unknown_name_suggests(fx):
    out = fx.call("list_effects_tool", effect="Blurr")
    assert out.startswith("Error") and "Blur" in out


# ---------------------------------------------------------------------------
# add_effect_tool
# ---------------------------------------------------------------------------

def test_add_effect_with_properties_is_one_undo_step(fx):
    c = fx.add_clip(fx.add_file("video"))
    r = receipt(fx.call("add_effect_tool", timeline_clip_id=c, effect="Blur",
                        properties={"horizontal_radius": 12, "sigma": "4"}))
    blur = effect_of(fx, c, "Blur")[0]
    assert r["effect_id"] == blur["id"] and r["clips"][0]["action"] == "added"
    assert y(blur["horizontal_radius"]) == [12.0] and y(blur["sigma"]) == [4.0]
    assert fx.undo_steps_since_mark() == 1
    fx.undo()
    assert effect_of(fx, c, "Blur") == []
    fx.redo()
    assert effect_of(fx, c, "Blur")[0]["id"] == blur["id"]


def test_add_effect_accepts_pr183_arguments(fx):
    f = fx.add_file("video")
    fx.add_clip(f, position=0.0)
    c2 = fx.add_clip(f, position=30.0)
    r = receipt(fx.call("add_effect_tool", clip_query="sample_video", effect_name="Crop", occurrence=2))
    assert r["timeline_clip_id"] == c2 and effect_of(fx, c2, "Crop")


def test_add_effect_keyframes_use_clip_frames_including_trim(fx):
    c = fx.add_clip(fx.add_file("video"), position=10.0, start=2.0, end=8.0)
    fx.call("add_effect_tool", timeline_clip_id=c, effect="Pixelate",
            properties={"pixelization": [{"time": 10.0, "value": 0.9},
                                         {"time": 12.0, "value": 0.0, "interpolation": "linear"}]})
    kf = effect_of(fx, c, "Pixelate")[0]["pixelization"]
    assert x(kf) == [61.0, 121.0] and y(kf) == [0.9, 0.0]
    assert kf["Points"][1]["interpolation"] == 1


def test_add_effect_scope_all_skips_clips_that_cannot_take_it(fx):
    v = fx.add_clip(fx.add_file("video"))
    a = fx.add_clip(fx.add_file("audio"), layer=2000000)
    r = receipt(fx.call("add_effect_tool", scope="all", effect="Negate"))
    assert effect_of(fx, v, "Negate") and not effect_of(fx, a, "Negate")
    assert r["skipped"][0]["timeline_clip_id"] == a


def test_add_effect_updates_existing_by_default_and_stacks_on_request(fx):
    c = fx.add_clip(fx.add_file("video"))
    fx.call("add_effect_tool", timeline_clip_id=c, effect="Blur", properties={"sigma": 2})
    fx.mark()
    r = receipt(fx.call("add_effect_tool", timeline_clip_id=c, effect="Blur", properties={"sigma": 5}))
    assert r["clips"][0]["action"] == "updated" and len(effect_of(fx, c, "Blur")) == 1
    assert y(effect_of(fx, c, "Blur")[0]["sigma"]) == [5.0]
    fx.call("add_effect_tool", timeline_clip_id=c, effect="Blur", if_exists="add")
    assert len(effect_of(fx, c, "Blur")) == 2


def test_add_effect_same_values_is_a_no_op(fx):
    c = fx.add_clip(fx.add_file("video"))
    fx.call("add_effect_tool", timeline_clip_id=c, effect="Blur", properties={"sigma": 2})
    fx.mark()
    r = receipt(fx.call("add_effect_tool", timeline_clip_id=c, effect="Blur", properties={"sigma": 2}))
    assert r["changed"] is False and fx.undo_steps_since_mark() == 0


@pytest.mark.parametrize("kwargs, needle", [
    ({"effect": "Blur", "properties": {"horizontal_radius": 500}}, "between 0 and 100"),
    ({"effect": "Blur", "properties": {"radius": 3}}, "no property"),
    ({"effect": "ChromaKey", "properties": {"keymethod": "magic"}}, "must be one of"),
    ({"effect": "Blur", "properties": {"id": "x"}}, "not settable"),
    ({"effect": "Blur", "properties": {"sigma": [{"time": 99, "value": 1}]}}, "outside the clip"),
    ({"effect": "NotAnEffect"}, "unknown effect"),
    ({"effect": "Compressor"}, "has no sound"),
    ({"effect": ""}, "needs effect"),
])
def test_add_effect_refusals_leave_history_untouched(fx, kwargs, needle):
    c = fx.add_clip(fx.add_file("video"))
    out = fx.call("add_effect_tool", timeline_clip_id=c, **kwargs)
    assert out.startswith("Error") and needle in out, out
    assert fx.undo_steps_since_mark() == 0 and fx.clip(c)["effects"] == []


def test_add_effect_refuses_locked_track_and_audio_only_clip(fx):
    c = fx.add_clip(fx.add_file("video"))
    fx.lock_track(1000000)
    out = fx.call("add_effect_tool", timeline_clip_id=c, effect="Blur")
    assert out.startswith("Error") and "locked" in out
    a = fx.add_clip(fx.add_file("audio"), layer=2000000)
    out = fx.call("add_effect_tool", timeline_clip_id=a, effect="Sharpen")
    assert out.startswith("Error") and "audio-only" in out
    assert fx.undo_steps_since_mark() == 0


def test_audio_effect_on_clip_with_sound_and_visualizer_on_audio_clip(fx):
    a = fx.add_clip(fx.add_file("audio"))
    fx.call("add_effect_tool", timeline_clip_id=a, effect="Echo", properties={"mix": 0.2, "echo_time": "0.3s"})
    echo = effect_of(fx, a, "Echo")[0]
    assert y(echo["mix"]) == [0.2] and y(echo["echo_time"]) == [0.3]
    assert not fx.call("add_effect_tool", timeline_clip_id=a, effect="AudioVisualization").startswith("Error")


def test_add_effect_colors_choices_and_curves(fx):
    c = fx.add_clip(fx.add_file("video"))
    fx.call("add_effect_tool", timeline_clip_id=c, effect="ChromaKey",
            properties={"color": "#00ff00", "keymethod": "hsv/hsl hue", "fuzz": 30})
    ck = effect_of(fx, c, "ChromaKey")[0]
    assert y(ck["color"]["green"]) == [255.0] and y(ck["color"]["red"]) == [0.0]
    assert ck["keymethod"] == 1 and y(ck["fuzz"]) == [30.0]
    fx.call("add_effect_tool", timeline_clip_id=c, effect="ColorGrade",
            properties={"curve_all": [[0, 0], [0.5, 0.6], [1, 1]], "saturation": 0})
    cg = effect_of(fx, c, "ColorGrade")[0]
    assert len(cg["curve_all"]["nodes"]) == 3 and y(cg["saturation"]) == [0.0]


# ---------------------------------------------------------------------------
# get / update / remove / copy
# ---------------------------------------------------------------------------

def test_get_clip_effects_lists_ids_and_values(fx):
    c = fx.add_clip(fx.add_file("video"), position=5.0)
    eid = fx.add_effect(c, "Blur")
    r = receipt(fx.call("get_clip_effects_tool", timeline_clip_id=c))
    row = r["clips"][0]["effects"][0]
    assert row["effect_id"] == eid and row["effect"] == "Blur"
    assert row["values"]["horizontal_radius"] == 6.0
    assert fx.undo_steps_since_mark() == 0


def test_update_effect_by_id_and_by_class_with_keyframes(fx):
    c = fx.add_clip(fx.add_file("video"), position=5.0)
    eid = fx.add_effect(c, "Blur")
    fx.mark()
    r = receipt(fx.call("update_effect_tool", effect_id=eid, properties={"sigma": 8}))
    assert r["effects"][0]["changed"] == ["sigma"] and fx.undo_steps_since_mark() == 1
    fx.mark()
    fx.call("update_effect_tool", timeline_clip_id=c, effect="blur",
            properties={"sigma": [{"time": 5, "value": 8}, {"time": 7, "value": 0}]})
    blur = effect_of(fx, c, "Blur")[0]
    assert x(blur["sigma"]) == [1.0, 61.0] and y(blur["sigma"]) == [8.0, 0.0]
    fx.undo()
    assert y(effect_of(fx, c, "Blur")[0]["sigma"]) == [8.0]
    values = receipt(fx.call("get_clip_effects_tool", timeline_clip_id=c))["clips"][0]["effects"][0]["values"]
    assert values["sigma"] == 8.0


def test_update_effect_refusals(fx):
    c = fx.add_clip(fx.add_file("video"))
    eid = fx.add_effect(c, "Blur")
    for kwargs, needle in (({"effect_id": "nope", "properties": {"sigma": 1}}, "no effect with id"),
                           ({"effect_id": eid, "properties": {"sigma": -1}}, "between"),
                           ({"timeline_clip_id": c, "effect": "Sharpen", "properties": {"amount": 2}}, "no Sharpen"),
                           ({"effect_id": eid}, "missing required")):
        out = fx.call("update_effect_tool", **kwargs)
        assert out.startswith("Error") and needle in out, out
    r = receipt(fx.call("update_effect_tool", effect_id=eid, properties={"sigma": 3}))
    assert r["changed"] is False
    assert fx.undo_steps_since_mark() == 0


def test_remove_effect_by_id_class_and_all_never_removes_clips(fx):
    c = fx.add_clip(fx.add_file("video"))
    b = fx.add_effect(c, "Blur")
    fx.add_effect(c, "Sharpen")
    fx.add_effect(c, "Negate")
    fx.mark()
    receipt(fx.call("remove_effect_tool", effect_ids=[b]))
    assert [e["class_name"] for e in fx.clip(c)["effects"]] == ["Sharpen", "Negate"]
    receipt(fx.call("remove_effect_tool", timeline_clip_id=c, effect="Sharpen"))
    receipt(fx.call("remove_effect_tool", timeline_clip_id=c, all_effects=True))
    assert fx.clip(c)["effects"] == [] and len(fx.clips()) == 1
    assert fx.undo_steps_since_mark() == 3
    fx.undo()
    assert [e["class_name"] for e in fx.clip(c)["effects"]] == ["Negate"]
    fx.mark()
    r = receipt(fx.call("remove_effect_tool", timeline_clip_id=c, effect="Blur"))
    assert r["changed"] is False and fx.undo_steps_since_mark() == 0


def test_copy_effects_merge_replace_append(fx):
    f = fx.add_file("video")
    src = fx.add_clip(f)
    dst = fx.add_clip(f, position=30.0)
    fx.add_effect(src, "Blur", sigma={"Points": [{"co": {"X": 1, "Y": 7}}]})
    fx.add_effect(src, "Sharpen")
    old_blur = fx.add_effect(dst, "Blur")
    fx.add_effect(dst, "Negate")
    fx.mark()
    receipt(fx.call("copy_effects_tool", source_clip_id=src, timeline_clip_ids=[dst]))
    classes = [e["class_name"] for e in fx.clip(dst)["effects"]]
    assert classes == ["Blur", "Negate", "Sharpen"]
    assert y(effect_of(fx, dst, "Blur")[0]["sigma"]) == [7]
    ids = {e["id"] for e in fx.clip(dst)["effects"]} | {e["id"] for e in fx.clip(src)["effects"]}
    assert len(ids) == 5 and old_blur not in ids
    assert fx.undo_steps_since_mark() == 1
    fx.call("copy_effects_tool", source_clip_id=src, timeline_clip_ids=[dst], mode="replace", effects=["Blur"])
    assert [e["class_name"] for e in fx.clip(dst)["effects"]] == ["Blur"]
    fx.call("copy_effects_tool", source_clip_id=src, timeline_clip_ids=[dst], mode="append")
    assert [e["class_name"] for e in fx.clip(dst)["effects"]] == ["Blur", "Blur", "Sharpen"]
    out = fx.call("copy_effects_tool", source_clip_id=dst, timeline_clip_ids=[src], effects=["ChromaKey"])
    assert out.startswith("Error")


def test_merge_effects_by_class_matches_editor_paste():
    from unittest.mock import patch
    ids = iter(["N1", "N2"])
    with patch("classes.effect_ops._new_id", lambda: next(ids)):
        out = effect_ops.merge_effects_by_class(
            [{"id": "A", "class_name": "Blur", "sigma": 1}, {"id": "B", "class_name": "Negate"}],
            [{"id": "S1", "class_name": "Blur", "sigma": 9}, {"id": "S2", "class_name": "Hue"}])
    assert out == [{"id": "N1", "class_name": "Blur", "sigma": 9}, {"id": "B", "class_name": "Negate"},
                   {"id": "N2", "class_name": "Hue"}]


def test_gui_thread_timeout_is_an_error_and_changes_nothing(fx, monkeypatch):
    from classes import tool_handlers
    from classes.editor_tools import effects_color as ec
    c = fx.add_clip(fx.add_file("video"))

    def busy_on_main(func, *args, timeout=None):
        raise tool_handlers.MainThreadTimeout("MAIN_THREAD_TIMEOUT: nothing was changed; safe to retry")

    monkeypatch.setattr(ec, "on_main", busy_on_main)
    out = fx.call("add_effect_tool", timeline_clip_id=c, effect="Blur")
    assert out.startswith("Error") and "MAIN_THREAD_TIMEOUT" in out
    assert fx.clip(c)["effects"] == [] and fx.undo_steps_since_mark() == 0
