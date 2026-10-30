"""Timeline listing (get_timeline_state_tool) outputs, on the headless editor."""


def _timeline_text(editor, **args):
    return editor.call_receipt("get_timeline_state_tool", **args)["data"]["legacy_text"]


def test_timeline_state_gives_source_window_and_summary(editor):
    f = editor.add_file("video", duration=60.0,
                        ai_metadata={"analyzed": True, "short_summary": "Tennis rally on a court"})
    editor.add_clip(f, position=0.0, start=0.0, end=30.0)
    text = _timeline_text(editor)
    assert "source=0.0-30.0s" in text
    assert "summary_preview='Tennis rally on a court'" in text


def test_timeline_state_numbers_repeated_placements_of_one_file(editor):
    f = editor.add_file("video", duration=60.0)
    editor.add_clip(f, position=0.0, start=0.0, end=10.0)
    editor.add_clip(f, position=20.0, start=0.0, end=10.0)
    single = editor.add_file("audio", duration=60.0)
    editor.add_clip(single, position=40.0, start=0.0, end=10.0)
    text = _timeline_text(editor)
    assert text.count("occurrence_hint=") == 2
    assert "occurrence_hint=1" in text and "occurrence_hint=2" in text


def test_timeline_state_lists_empty_and_locked_tracks(editor):
    editor.add_track(2, label="Music", lock=True)
    text = _timeline_text(editor)
    assert "label='Music' | lock=True:\n  (empty)" in text


def test_full_detail_carries_each_clips_trimmed_scenes(editor):
    f = editor.add_file("video", duration=60.0, ai_metadata={
        "analyzed": True,
        "scene_descriptions": [{"time": 2.0, "description": "early-serve"},
                               {"time": 15.0, "description": "late-volley"}],
    })
    editor.add_clip(f, position=0.0, start=10.0, end=20.0)
    assert "effective_scenes:" not in _timeline_text(editor)
    full = _timeline_text(editor, detail_level="full")
    assert "[0:05] late-volley" in full
    assert "early-serve" not in full


if __name__ == "__main__":
    print("test_tool_handlers_timeline_search: run with pytest")
