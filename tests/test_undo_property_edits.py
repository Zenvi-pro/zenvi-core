"""Undoing an effect or property edit is reported as a change, not as "the timeline did not change"."""

from classes.query import Clip


def _blur(editor, clip_id):
    clip = Clip.get(id=clip_id)
    effect = editor.effect_fixture("Blur")
    effect["id"] = "BLUR1"
    clip.data = {"effects": [effect]}
    clip.save()


def test_undo_of_an_effect_edit_reports_what_changed(editor):
    f = editor.add_file("video")
    c = editor.add_clip(f)
    _blur(editor, c)
    out = editor.call("undo_tool", steps=1)
    assert not out.startswith("Error"), out
    assert "changed effects/properties of 1 clip" in out
    assert editor.clip(c)["effects"] == []
    out = editor.call("redo_tool", steps=1)
    assert not out.startswith("Error"), out
    assert editor.clip(c)["effects"][0]["id"] == "BLUR1"


def test_undo_of_a_keyframe_edit_reports_what_changed(editor):
    f = editor.add_file("video")
    c = editor.add_clip(f)
    clip = Clip.get(id=c)
    clip.data = {"alpha": {"Points": [{"co": {"X": 1, "Y": 0.0}, "interpolation": 0},
                                      {"co": {"X": 30, "Y": 1.0}, "interpolation": 0}]}}
    clip.save()
    out = editor.call("undo_tool", steps=1)
    assert not out.startswith("Error"), out
    assert "changed effects/properties" in out


def test_a_waveform_cache_refresh_is_not_a_content_change():
    from classes.tool_handlers import _clip_content_digest

    base = {"id": "c", "layer": 1, "alpha": {"Points": []}}
    assert _clip_content_digest(base) == _clip_content_digest(dict(base, ui={"audio_data": [1, 2, 3]}))
    assert _clip_content_digest(base) != _clip_content_digest(dict(base, alpha={"Points": [1]}))


def test_undo_of_a_relink_reports_a_change(editor):
    """Regression: a relink changes only the clips' readers; undo called it 'did not change'."""
    f = editor.add_file("video")
    c = editor.add_clip(f)
    clip = Clip.get(id=c)
    clip.data = {"reader": dict(clip.data["reader"], path="/moved/sample_video.mp4")}
    clip.save()
    out = editor.call("undo_tool", steps=1)
    assert not out.startswith("Error"), out
    assert editor.clip(c)["reader"]["path"] == "/media/sample_video.mp4"
    from classes.tool_handlers import _clip_content_digest
    base = {"id": "c", "reader": {"path": "/a.mp4", "duration": 5.0, "metadata": {"x": 1}}}
    assert _clip_content_digest(base) == _clip_content_digest(
        dict(base, reader=dict(base["reader"], metadata={"x": 2})))
