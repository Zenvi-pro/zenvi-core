"""View and selection tools: set_timeline_view_tool, select_timeline_items_tool,
get_playhead_and_selection_tool."""

import json

import pytest

from classes import track_ops
from tracks_nav_fakes import FakeSelection, add_transition, install_player


def receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def view(editor):
    """A 300 s timeline, 30 s of clips, a zoom slider showing 0..30 s of a 1000 px view."""
    f = editor.add_file("video", duration=30.0)
    editor.add_clip(f, position=0.0)
    slider = editor.window.sliderZoomWidget
    slider.zoom_factor = 3.0
    slider.min_distance = 0.002
    slider.scrollbar_position = [0.0, 0.1, 3000.0, 1000.0]
    for name, checked in (("actionSnappingTool", True), ("actionRazorTool", False), ("actionTimingTool", False)):
        action = getattr(editor.window, name)
        action.isChecked.return_value = checked
        action.setChecked.side_effect = (lambda a: lambda v: setattr(a.isChecked, "return_value", v))(action)
    editor.player = install_player(editor, last_frame=899)
    editor.mark()
    return editor


# --- set_timeline_view_tool -----------------------------------------------------------

def test_zoom_in_and_out_by_steps(view):
    receipt(view.call("set_timeline_view_tool", zoom="in", steps=3))
    assert view.window.actionTimelineZoomIn_trigger.call_count == 3
    receipt(view.call("set_timeline_view_tool", zoom="out"))
    assert view.window.actionTimelineZoomOut_trigger.call_count == 1
    assert view.undo_steps_since_mark() == 0


def test_zoom_to_fit_shows_every_clip(view):
    f = view.add_file("video", duration=30.0)
    view.add_clip(f, position=30.0, layer=2000000)       # content now ends at 60 s
    out = view.call("set_timeline_view_tool", zoom="fit")
    receipt(out)
    left, right = view.window.sliderZoomWidget.scrollbar_position[:2]
    assert left == 0.0 and right == pytest.approx(60 * 1.02 / 300)
    view.window.sliderZoomWidget.delayed_resize_callback.assert_called_once()
    assert "fit every clip" in out


def test_show_n_seconds_around_the_playhead_or_a_range(view):
    view.player.position = 601                                # 20 s
    data = receipt(view.call("set_timeline_view_tool", visible_seconds=10))
    left, right = view.window.sliderZoomWidget.scrollbar_position[:2]
    assert (left * 300, right * 300) == (pytest.approx(15.0), pytest.approx(25.0))
    assert data["zoom"]["visible_range"] == [15.0, 25.0]
    receipt(view.call("set_timeline_view_tool", start_seconds=30, end_seconds=60))
    left, right = view.window.sliderZoomWidget.scrollbar_position[:2]
    assert (left, right) == (pytest.approx(0.1), pytest.approx(0.2))


def test_center_on_playhead(view):
    receipt(view.call("set_timeline_view_tool", center_on_playhead=True))
    view.window.actionCenterOnPlayhead_trigger.assert_called_once()


def test_modes_use_the_toolbar_actions(view):
    out = view.call("set_timeline_view_tool", snapping=False, razor=True)
    data = receipt(out)
    view.window.actionSnappingTool.setChecked.assert_called_with(False)
    view.window.actionSnappingTool_trigger.assert_called_with(False)
    view.window.actionRazorTool_trigger.assert_called_with(True)
    assert data["modes"] == {"snapping": False, "razor": True, "timing": False}
    out = view.call("set_timeline_view_tool", snapping=False)
    assert "snapping already off" in out
    assert view.window.actionSnappingTool_trigger.call_count == 1
    assert view.undo_steps_since_mark() == 0


def test_view_refusals(view):
    for args, needle in (
        ({}, "nothing to change"),
        ({"zoom": "fit", "visible_seconds": 5}, "only one of"),
        ({"start_seconds": 5}, "both"),
        ({"start_seconds": 9, "end_seconds": 4}, "after"),
        ({"start_seconds": 400, "end_seconds": 500}, "past the end"),
        ({"zoom": "fit", "steps": 2}, "steps only"),
        ({"zoom": "sideways"}, "zoom"),
    ):
        out = view.call("set_timeline_view_tool", **args)
        assert out.startswith("Error") and needle in out, (args, out)
    view.window.sliderZoomWidget.delayed_resize_callback.assert_not_called()


# --- select_timeline_items_tool ----------------------------------------------------------

@pytest.fixture
def stage(editor):
    """Track 1: clips at 0 and 10; track 2: clips at 5 and 20 plus a transition at 19."""
    editor.sel = FakeSelection(editor)
    f = editor.add_file("video", duration=5.0)
    editor.a = editor.add_clip(f, layer=1000000, position=0.0)
    editor.b = editor.add_clip(f, layer=1000000, position=10.0)
    editor.c = editor.add_clip(f, layer=2000000, position=5.0)
    editor.d = editor.add_clip(f, layer=2000000, position=20.0)
    editor.t = add_transition(editor, layer=2000000, position=19.0)
    editor.mark()
    return editor


def test_select_every_clip_on_a_track(stage):
    data = receipt(stage.call("select_timeline_items_tool", track="1"))
    assert stage.sel.pairs == [(stage.a, "clip"), (stage.b, "clip")]
    assert [c["timeline_clip_id"] for c in data["selection"]["clips"]] == [stage.a, stage.b]
    assert data["selection"]["clips"][0]["track"] == 1
    # The first pick replaces the old selection, like a plain click.
    first = stage.window.timeline.AddSelectionJS.call_args_list[0]
    assert first.args == (stage.a, "clip", True)
    assert stage.undo_steps_since_mark() == 0


def test_select_add_remove_and_none(stage):
    receipt(stage.call("select_timeline_items_tool", timeline_clip_ids=[stage.a]))
    receipt(stage.call("select_timeline_items_tool", mode="add", timeline_clip_ids=[stage.c]))
    assert stage.sel.pairs == [(stage.a, "clip"), (stage.c, "clip")]
    receipt(stage.call("select_timeline_items_tool", mode="remove", timeline_clip_ids=[stage.a]))
    assert stage.sel.pairs == [(stage.c, "clip")]
    out = stage.call("select_timeline_items_tool", mode="remove", timeline_clip_ids=[stage.b])
    assert "none of those were selected" in out
    data = receipt(stage.call("select_timeline_items_tool", mode="none"))
    assert stage.sel.pairs == [] and data["selection"]["count"] == 0


def test_select_by_time_range_and_with_transitions(stage):
    receipt(stage.call("select_timeline_items_tool", start_seconds=4, end_seconds=12))
    assert stage.sel.pairs == [(stage.a, "clip"), (stage.c, "clip"), (stage.b, "clip")]
    receipt(stage.call("select_timeline_items_tool", start_seconds=12))   # clips under that moment
    assert stage.sel.pairs == [(stage.b, "clip")]
    data = receipt(stage.call("select_timeline_items_tool", track="2", start_seconds=18, end_seconds=30,
                              include_transitions=True))
    assert set(stage.sel.pairs) == {(stage.t, "transition"), (stage.d, "clip")}
    assert data["selection"]["transitions"][0]["transition_id"] == stage.t


def test_select_moment_without_a_clip_is_refused(stage):
    out = stage.call("select_timeline_items_tool", start_seconds=26)
    assert out.startswith("Error") and "nothing to select" in out


def test_select_all_and_ripple(stage):
    data = receipt(stage.call("select_timeline_items_tool", mode="all"))
    assert data["selection"]["count"] == 5
    receipt(stage.call("select_timeline_items_tool", timeline_clip_ids=[stage.c]))
    receipt(stage.call("select_timeline_items_tool", mode="ripple"))
    assert set(stage.sel.pairs) == {(stage.c, "clip"), (stage.d, "clip"), (stage.t, "transition")}
    receipt(stage.call("select_timeline_items_tool", mode="none"))
    receipt(stage.call("select_timeline_items_tool", mode="ripple", timeline_clip_ids=[stage.a]))
    assert set(stage.sel.pairs) == {(stage.a, "clip"), (stage.b, "clip")}


def test_select_an_effect_and_a_clip_by_query(stage):
    eid = stage.add_effect(stage.b, "Blur")
    data = receipt(stage.call("select_timeline_items_tool", effect_ids=[eid], show_properties=True))
    assert data["selection"]["effects"] == [{"effect_id": eid, "class_name": "Blur",
                                             "timeline_clip_id": stage.b}]
    stage.window.actionProperties_trigger.assert_called_once()
    g = stage.add_file("video", path="/media/interview_take2.mp4")
    e = stage.add_clip(g, layer=3000000, position=2.0)
    receipt(stage.call("select_timeline_items_tool", clip_query="interview", track="3"))
    assert stage.sel.pairs == [(e, "clip")]


def test_selection_marks_clips_on_locked_tracks(stage):
    stage.lock_track(1000000)
    data = receipt(stage.call("select_timeline_items_tool", track="1"))
    assert all(c["locked"] for c in data["selection"]["clips"])


def test_selection_refusals_change_nothing(stage):
    receipt(stage.call("select_timeline_items_tool", timeline_clip_ids=[stage.a]))
    for args, needle in (
        ({"timeline_clip_ids": [stage.b, "nope"]}, "nope"),
        ({}, "needs something"),
        ({"mode": "all", "track": "1"}, "takes no targets"),
        ({"end_seconds": 5}, "needs start_seconds"),
        ({"start_seconds": 9, "end_seconds": 2}, "before"),
        ({"transition_ids": ["zz"]}, "no transition"),
        ({"effect_ids": ["zz"]}, "no effect"),
        ({"track": "7"}, "7"),
    ):
        out = stage.call("select_timeline_items_tool", **args)
        assert out.startswith("Error") and needle in out, (args, out)
    assert stage.sel.pairs == [(stage.a, "clip")]


def test_web_backends_that_select_asynchronously_still_get_the_selection(editor):
    sel = FakeSelection(editor, native=False)      # AddSelectionJS does nothing right away
    f = editor.add_file("video")
    a = editor.add_clip(f)
    editor.mark()
    receipt(editor.call("select_timeline_items_tool", timeline_clip_ids=[a]))
    assert sel.pairs == [(a, "clip")]
    editor.window.addSelection.assert_called_with(a, "clip", True)


# --- get_playhead_and_selection_tool --------------------------------------------------------

def test_read_playhead_selection_and_view(view):
    sel = FakeSelection(view)
    clip_id = view.clips()[0]["id"]
    sel.add(clip_id, "clip")
    track_ops.add_marker(5.0, "A")
    track_ops.add_marker(15.0, "B")
    view.player.position = 301                     # 10 s
    receipt(view.call("play_tool", action="play"))
    view.mark()
    out = view.call("get_playhead_and_selection_tool")
    data = receipt(out)
    assert data["playhead"] == {"frame": 301, "seconds": 10.0, "timecode": "00:00:10,00"}
    assert data["playback"] == {"playing": True, "speed": 1}
    assert data["selection"]["clips"][0]["timeline_clip_id"] == clip_id
    assert data["view"]["zoom"]["visible_seconds"] == 30.0
    assert data["view"]["zoom"]["visible_range"] == [0.0, 30.0]
    assert data["view"]["modes"]["snapping"] is True
    assert data["timeline"]["previous_marker"]["name"] == "A"
    assert data["timeline"]["next_marker"]["name"] == "B"
    assert data["timeline"]["content_end"]["seconds"] == 30.0
    assert "playing" in out and view.undo_steps_since_mark() == 0
