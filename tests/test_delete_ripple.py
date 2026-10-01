"""delete_from_timeline_tool(ripple=true): delete and close the gap, one undo step."""


def _three_clips(editor):
    f = editor.add_file("video", duration=20.0)
    ids = [editor.add_clip(f, position=p, start=0.0, end=5.0) for p in (0.0, 5.0, 10.0)]
    editor.mark()
    return ids


def test_ripple_delete_closes_the_gap_in_one_undo_step(editor):
    a, b, c = _three_clips(editor)
    out = editor.call("delete_from_timeline_tool", timeline_clip_id=b, ripple="true")
    assert "closed the gap" in out, out
    assert editor.clip(b) is None
    assert editor.clip(c)["position"] == 5.0
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert editor.clip(b) is not None and editor.clip(c)["position"] == 10.0


def test_plain_delete_still_leaves_the_gap(editor):
    a, b, c = _three_clips(editor)
    out = editor.call("delete_from_timeline_tool", timeline_clip_id=b)
    assert "gap left" in out, out
    assert editor.clip(c)["position"] == 10.0
