"""Tool rows in the assistant chat read as sentences, and raw args stay in Python.

A row's title says what the tool does for the user ("Add clip to timeline:
beach.mp4 at 1:12 on track 2"). Raw args and results are only sent to the page
when the user expands the row.
"""

import json
import types

import pytest


@pytest.fixture(scope="module")
def chat_ui():
    from windows import ai_chat_ui
    return ai_chat_ui


def test_path_args_show_a_file_name_times_m_ss_and_track_a_number(chat_ui):
    text = chat_ui.describe_tool_call(
        "add_clip_to_timeline_tool",
        {"file_id": "C:/x/beach.mp4", "position_seconds": 72, "track": 2},
    )
    assert text == "Add clip to timeline: beach.mp4 at 1:12 on track 2"


def test_windows_and_posix_paths_both_give_the_file_name(chat_ui):
    for path in ("C:\\x\\beach.mp4", "/home/me/beach.mp4"):
        assert chat_ui.describe_tool_call("add_clip_to_timeline_tool", {"file_id": path}) == (
            "Add clip to timeline: beach.mp4"
        )


def test_bare_ids_are_not_shown_as_if_they_were_names(chat_ui):
    assert chat_ui.describe_tool_call("add_clip_to_timeline_tool", {"file_id": "3f9a2c"}) == (
        "Add clip to timeline"
    )


def test_unknown_args_are_skipped_not_dumped(chat_ui):
    text = chat_ui.describe_tool_call(
        "add_clip_to_timeline_tool",
        {"chat_session_id": "s-1", "mystery": {"a": [1, 2]}, "flag": True},
    )
    assert text == "Add clip to timeline"


def test_no_more_than_three_key_args_are_shown(chat_ui):
    text = chat_ui.describe_tool_call(
        "add_clip_to_timeline_tool",
        {"file_id": "/m/a.mp4", "query": "sun", "position_seconds": 5, "track": 1},
    )
    assert text == 'Add clip to timeline: a.mp4 "sun" at 0:05'


def test_seconds_args_read_as_clock_time(chat_ui):
    label = chat_ui.humanize_tool_name("add_clip_to_timeline_tool")
    assert chat_ui.describe_tool_call("add_clip_to_timeline_tool", {"start_seconds": 3725}) == (
        label + ": at 62:05"
    )
    assert chat_ui.describe_tool_call("add_clip_to_timeline_tool", {"position_seconds": 0}) == (
        label + ": at 0:00"
    )


def test_a_non_numeric_seconds_arg_is_skipped(chat_ui):
    label = chat_ui.humanize_tool_name("add_clip_to_timeline_tool")
    assert chat_ui.describe_tool_call(
        "add_clip_to_timeline_tool", {"position_seconds": "soon"}
    ) == label


def test_the_sentence_is_capped_at_90_characters(chat_ui):
    text = chat_ui.describe_tool_call(
        "add_clip_to_timeline_tool", {"file_id": "/media/" + "n" * 150 + ".mp4"}
    )
    assert len(text) == 90 and text.endswith("…")


def test_missing_or_non_dict_args_give_just_the_label(chat_ui):
    label = chat_ui.humanize_tool_name("add_clip_to_timeline_tool")
    assert chat_ui.describe_tool_call("add_clip_to_timeline_tool", None) == label
    assert chat_ui.describe_tool_call("add_clip_to_timeline_tool", "not a dict") == label


def _tool_window(chat_ui):
    """A stand-in carrying the real tool-start method, recording what reaches the page."""

    class W:
        _use_web_ui = True
        _active_sid = "s1"
        _on_tool_started = chat_ui.AIChatWindow._on_tool_started
        _record_tool_started = lambda self, *a: None
        _flush_token_buffer = lambda self: None
        _commit_streaming_segment = lambda self, sid: None

        def __init__(self):
            self.js = []

        def sender(self):
            return types.SimpleNamespace()

        def _run_js(self, code):
            self.js.append(code)

    return W()


def _added_payloads(win):
    out = []
    marker = "window.addToolBlock("
    for code in win.js:
        if marker in code:
            literal = code[code.index(marker) + len(marker):code.rindex(")")]
            out.append(json.loads(json.loads(literal)))
    return out


def test_the_row_title_is_the_sentence_and_no_command_dump_is_sent(chat_ui):
    win = _tool_window(chat_ui)
    win._on_tool_started(
        "call-1",
        "add_clip_to_timeline_tool",
        json.dumps({"file_id": "C:/x/beach.mp4", "position_seconds": 72, "track": 2}),
    )
    [payload] = _added_payloads(win)
    assert payload["title"] == "Add clip to timeline: beach.mp4 at 1:12 on track 2"
    assert not payload.get("cmd")
