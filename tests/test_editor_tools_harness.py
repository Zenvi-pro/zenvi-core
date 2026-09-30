"""The headless editor harness behaves like the running app's data layer."""

from classes.query import Clip


def test_harness_builds_production_shaped_clips(editor):
    f = editor.add_file("video", duration=12.0)
    c = editor.add_clip(f, position=3.0, end=5.0)
    data = editor.clip(c)
    assert data["file_id"] == f and data["position"] == 3.0 and data["end"] == 5.0
    assert "Points" in data["alpha"] and "Points" in data["scale_x"]
    assert Clip.get(id=c).data["reader"]["path"].endswith(".mp4")


def test_harness_undo_reverts_a_tool_style_update(editor):
    f = editor.add_file("video")
    c = editor.add_clip(f, position=1.0)
    clip = Clip.get(id=c)
    clip.data = {"position": 4.0}
    clip.save()
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(c)["position"] == 1.0
    editor.redo()
    assert editor.clip(c)["position"] == 4.0


def test_harness_effects_and_tracks(editor):
    f = editor.add_file("video")
    c = editor.add_clip(f)
    eid = editor.add_effect(c, "Blur", horizontal_radius={"Points": [{"co": {"X": 1, "Y": 5}}]})
    assert editor.clip(c)["effects"][0]["id"] == eid
    assert editor.clip(c)["effects"][0]["class_name"] == "Blur"
    editor.lock_track(1000000)
    assert editor.get("layers")[0]["lock"] is True
