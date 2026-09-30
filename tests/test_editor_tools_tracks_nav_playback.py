"""Playback tools: play_tool (transport) and seek_playhead_tool (playhead moves)."""

import json

import pytest

from classes import track_ops
from tracks_nav_fakes import FakeSelection, install_player


def receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def timeline(editor):
    """40 s of video on track 1 (30 fps project) and a player at frame 1."""
    f = editor.add_file("video", duration=40.0)
    editor.clip_id = editor.add_clip(f, position=0.0)
    editor.player = install_player(editor, last_frame=1199)
    editor.mark()
    return editor


# --- play_tool -------------------------------------------------------------------

def test_play_then_pause(timeline):
    out = timeline.call("play_tool", action="play")
    data = receipt(out)
    assert data["playing"] is True and data["speed"] == 1 and data["changed"] is True
    assert timeline.player.playing and "Playing at 1x" in out
    timeline.window.actionPlay_trigger.assert_called_once()

    out = timeline.call("play_tool", action="play")
    assert receipt(out)["changed"] is False and "Already playing" in out
    assert timeline.window.actionPlay_trigger.call_count == 1

    data = receipt(timeline.call("play_tool", action="pause"))
    assert data["playing"] is False and not timeline.player.playing
    data = receipt(timeline.call("play_tool", action="pause"))
    assert data["changed"] is False
    assert timeline.undo_steps_since_mark() == 0


def test_toggle_is_the_space_bar(timeline):
    assert receipt(timeline.call("play_tool"))["playing"] is True
    assert receipt(timeline.call("play_tool", action="toggle"))["playing"] is False


def test_play_at_a_speed_and_backwards(timeline):
    data = receipt(timeline.call("play_tool", action="play", speed=2))
    assert data["playing"] is True and data["speed"] == 2
    timeline.player.position = 600
    data = receipt(timeline.call("play_tool", action="play", speed=-1))
    assert data["speed"] == -1
    assert "backwards at 1x" in timeline.call("play_tool", action="play", speed=-1)


def test_stop_pauses_and_returns_to_the_start(timeline):
    timeline.player.position = 500
    receipt(timeline.call("play_tool", action="play"))
    out = timeline.call("play_tool", action="stop")
    data = receipt(out)
    assert data["playing"] is False and data["frame"] == 1 and "start" in out
    timeline.window.actionJumpStart_trigger.assert_called_once()


def test_fast_forward_and_rewind_step_the_speed_like_l_and_j(timeline):
    timeline.player.position = 300
    assert receipt(timeline.call("play_tool", action="fast_forward"))["speed"] == 1
    assert receipt(timeline.call("play_tool", action="fast_forward"))["speed"] == 2
    assert receipt(timeline.call("play_tool", action="rewind"))["speed"] == 1
    receipt(timeline.call("play_tool", action="pause"))
    assert receipt(timeline.call("play_tool", action="rewind"))["speed"] == -1


def test_step_frames(timeline):
    timeline.player.position = 100
    data = receipt(timeline.call("play_tool", action="step_forward", frames=5))
    assert data["frame"] == 105 and data["playing"] is False
    timeline.window.step_frames.assert_called_with(5)
    data = receipt(timeline.call("play_tool", action="step_back"))
    assert data["frame"] == 104
    timeline.player.position = 1
    assert "first frame" in timeline.call("play_tool", action="step_back")


def test_play_refusals(timeline):
    # Where playback parks when it reaches the end (PreviewParent seeks to GetMaxFrame).
    timeline.player.position = 1200
    out = timeline.call("play_tool", action="play")
    assert out.startswith("Error") and "end of the timeline" in out and "seek_playhead_tool" in out
    assert "speed 0" in timeline.call("play_tool", action="play", speed=0)
    assert "speed only applies" in timeline.call("play_tool", action="pause", speed=2)
    assert "frames only applies" in timeline.call("play_tool", action="play", frames=3)
    assert timeline.call("play_tool", action="dance").startswith("Error")
    assert not timeline.player.playing


def test_play_refuses_an_empty_timeline(editor):
    install_player(editor, last_frame=1)
    out = editor.call("play_tool", action="play")
    assert out.startswith("Error") and "empty" in out


def test_a_player_that_never_answers_is_an_error(timeline):
    timeline.player.frozen = True
    out = timeline.call("play_tool", action="play")
    assert out.startswith("Error") and "reports paused" in out


# --- seek_playhead_tool ------------------------------------------------------------

def test_seek_to_seconds_and_frame(timeline):
    out = timeline.call("seek_playhead_tool", seconds=32)
    data = receipt(out)
    assert data["frame"] == 961 and data["timecode"] == "00:00:32,00" and data["seconds"] == 32.0
    timeline.window.SeekSignal.emit.assert_called_with(961)
    assert "00:00:32,00" in out
    assert receipt(timeline.call("seek_playhead_tool", frame=100))["frame"] == 100
    assert timeline.undo_steps_since_mark() == 0


def test_seek_past_the_content_or_the_timeline(timeline):
    data = receipt(timeline.call("seek_playhead_tool", seconds=50))
    assert "past the end of the last clip" in data["note"]
    out = timeline.call("seek_playhead_tool", seconds=400)
    assert out.startswith("Error") and "past the end of the timeline" in out


def test_seek_to_start_and_end(timeline):
    timeline.player.position = 500
    assert receipt(timeline.call("seek_playhead_tool", to="start"))["frame"] == 1
    timeline.window.actionJumpStart_trigger.assert_called_once()
    assert receipt(timeline.call("seek_playhead_tool", to="end"))["frame"] == 1199
    timeline.window.actionJumpEnd_trigger.assert_called_once()


def test_seek_to_a_marker_by_name_or_id(timeline):
    drop = track_ops.add_marker(12.0, "Drop")
    track_ops.add_marker(20.0, "Outro")
    data = receipt(timeline.call("seek_playhead_tool", marker="Drop"))
    assert data["frame"] == 361 and data["marker"]["name"] == "Drop"
    assert receipt(timeline.call("seek_playhead_tool", marker=drop.id))["frame"] == 361
    assert receipt(timeline.call("seek_playhead_tool", marker="out"))["frame"] == 601  # part of a name
    out = timeline.call("seek_playhead_tool", marker="Bridge")
    assert out.startswith("Error") and "Drop" in out and "Outro" in out


def test_seek_to_a_shared_marker_name_goes_to_the_first(timeline):
    track_ops.add_marker(20.0, "Beat")
    track_ops.add_marker(8.0, "Beat")
    data = receipt(timeline.call("seek_playhead_tool", marker="beat"))
    assert data["frame"] == 241 and "went to the first" in data["note"]


def test_next_and_previous_marker(timeline):
    track_ops.add_marker(5.0, "A")
    track_ops.add_marker(15.0, "B")
    timeline.player.position = 181  # 6 s
    data = receipt(timeline.call("seek_playhead_tool", to="next_marker"))
    assert data["frame"] == 451 and data["marker"]["name"] == "B"
    data = receipt(timeline.call("seek_playhead_tool", to="previous_marker"))
    assert data["frame"] == 151
    assert receipt(timeline.call("seek_playhead_tool", to="previous_marker"))["frame"] == 1
    out = timeline.call("seek_playhead_tool", to="previous_marker")
    assert out.startswith("Error") and "before" in out
    # With nothing selected the end of the content is a stop too.
    timeline.player.position = 452
    assert receipt(timeline.call("seek_playhead_tool", to="next_marker"))["frame"] == 1199
    assert "no marker" in timeline.call("seek_playhead_tool", to="next_marker")


def test_next_marker_uses_the_selected_clip_edges(timeline):
    sel = FakeSelection(timeline)
    f = timeline.add_file("video", duration=10.0)
    c = timeline.add_clip(f, layer=2000000, position=20.0)
    sel.add(c, "clip")
    timeline.player.position = 1
    assert receipt(timeline.call("seek_playhead_tool", to="next_marker"))["frame"] == 601


def test_next_and_previous_edit(timeline):
    f = timeline.add_file("video", duration=10.0)
    timeline.add_clip(f, layer=2000000, position=12.0)   # edges at 12 and 22 on track 2
    timeline.player.position = 1
    assert receipt(timeline.call("seek_playhead_tool", to="next_edit"))["frame"] == 361
    assert receipt(timeline.call("seek_playhead_tool", to="next_edit"))["frame"] == 661
    assert receipt(timeline.call("seek_playhead_tool", to="next_edit"))["frame"] == 1199  # clamped end
    assert "no clip edge" in timeline.call("seek_playhead_tool", to="next_edit")
    timeline.player.position = 1
    data = receipt(timeline.call("seek_playhead_tool", to="next_edit", track="1"))
    assert data["frame"] == 1199   # track 1 has one 40 s clip: its end
    assert receipt(timeline.call("seek_playhead_tool", to="previous_edit", track="2"))["frame"] == 661


def test_seek_and_play(timeline):
    timeline.player.position = 1200
    data = receipt(timeline.call("seek_playhead_tool", to="start", play=True))
    assert data["playing"] is True and data["started_playing"] is True and timeline.player.position == 1


def test_seek_centers_the_timeline_when_the_target_is_off_screen(timeline):
    slider = timeline.window.sliderZoomWidget
    slider.scrollbar_position = [0.0, 0.1, 3000.0, 1000.0]   # 0..30 s of the 300 s timeline
    receipt(timeline.call("seek_playhead_tool", seconds=10))
    timeline.window.actionCenterOnPlayhead_trigger.assert_not_called()
    receipt(timeline.call("seek_playhead_tool", seconds=35))
    timeline.window.timeline.movePlayhead.assert_called_with(1051)
    timeline.window.actionCenterOnPlayhead_trigger.assert_called_once()


def test_seek_refusals(timeline):
    assert "say where to go" in timeline.call("seek_playhead_tool")
    assert "only one of" in timeline.call("seek_playhead_tool", seconds=1, marker="A")
    assert "track only applies" in timeline.call("seek_playhead_tool", to="start", track="1")
    assert timeline.call("seek_playhead_tool", seconds=-2).startswith("Error")
    assert timeline.call("seek_playhead_tool", frame=0).startswith("Error")
    assert timeline.call("seek_playhead_tool", to="middle").startswith("Error")
    timeline.window.SeekSignal.emit.assert_not_called()
