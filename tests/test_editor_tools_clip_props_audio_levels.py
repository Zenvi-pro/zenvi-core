"""audio.levels: set_clip_volume_tool / duck_under_speech_tool join the caller's undo transaction.

They used to mint their own id and then set updates.transaction_id = None,
which detached everything after them in a composite operation (each later
mutation became its own undo step) and split the caller's group.
"""

from classes import tool_handlers

CUES = [{"start": 2.0, "end": 6.0, "text": "hello there"}]


def _speech_and_music(editor):
    editor.add_track(2000000, "Voice")
    speech = editor.add_file("video", has_audio=True, channels=2,
                             ai_metadata={"analyzed": True, "transcript_cues": CUES})
    music = editor.add_file("audio", ai_metadata={"analyzed": True})
    s = editor.add_clip(speech, layer=2000000, end=10.0)
    m = editor.add_clip(music, layer=1000000, end=10.0)
    return s, m


def test_set_clip_volume_keeps_the_callers_transaction(editor):
    _s, m = _speech_and_music(editor)
    with tool_handlers._transaction(editor.app) as outer:
        out = editor.call("set_clip_volume_tool", timeline_clip_id=m, level="0.5")
        assert not out.startswith("Error"), out
        assert editor.app.updates.transaction_id == outer
        out = editor.call("set_clip_volume_tool", timeline_clip_id=m, level_db="-12", start_seconds="4",
                          end_seconds="6")
        assert not out.startswith("Error"), out
        assert editor.app.updates.transaction_id == outer
    assert editor.undo_steps_since_mark() == 1
    editor.undo()
    assert [p["co"]["Y"] for p in editor.clip(m)["volume"]["Points"]] == [1.0]


def test_set_clip_volume_alone_is_one_step_and_clears_its_own_id(editor):
    _s, m = _speech_and_music(editor)
    out = editor.call("set_clip_volume_tool", timeline_clip_id=m, level="0.3")
    assert not out.startswith("Error"), out
    assert editor.undo_steps_since_mark() == 1
    assert editor.app.updates.transaction_id is None


def test_duck_under_speech_keeps_the_callers_transaction(editor):
    s, m = _speech_and_music(editor)
    with tool_handlers._transaction(editor.app) as outer:
        out = editor.call("duck_under_speech_tool", bed_clip_ids=m, speech_clip_ids=s)
        assert not out.startswith("Error"), out
        assert editor.app.updates.transaction_id == outer
        editor.app.updates.update(["clips", {"id": s}], {"alpha": {"Points": [{"co": {"X": 1, "Y": 0.5}}]}})
    assert editor.undo_steps_since_mark() == 1  # the duck and the later edit are one step
    ducked = [p["co"]["Y"] for p in editor.clip(m)["volume"]["Points"]]
    assert min(ducked) < 1.0
    editor.undo()
    assert [p["co"]["Y"] for p in editor.clip(m)["volume"]["Points"]] == [1.0]
