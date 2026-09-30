"""timeline-edit timing and sound tools: speed, repeat, freeze, separate audio, audio/video on-off.

The Clip menu handlers are FakeTimeline doubles here (they make the same kind of
edit through the update manager); tests/test_timeline_edit_real_qt.py runs the
real ones.
"""

import json

import pytest

from classes.query import Clip
from timeline_edit_fakes import FakeTimeline, fake_openshot

T1, T2, T3 = 1000000, 2000000, 3000000


@pytest.fixture
def ed(editor, monkeypatch):
    from classes import clip_utils, timeline_ops
    monkeypatch.setattr(timeline_ops, "openshot", fake_openshot())
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)
    editor.fake = FakeTimeline(editor)
    editor.window.timeline = editor.fake
    return editor


def receipt(out):
    assert not out.startswith("Error"), out
    _head, _, body = out.partition("\n")
    return json.loads(body) if body else {}


def refused(editor, out, *fragments):
    assert out.startswith("Error"), out
    for frag in fragments:
        assert frag in out, out
    assert editor.undo_steps_since_mark() == 0


def one_step(editor):
    assert editor.undo_steps_since_mark() == 1


def dur(editor, cid):
    c = editor.clip(cid)
    return round(c["end"] - c["start"], 3)


def pos(editor, cid):
    return round(editor.clip(cid)["position"], 3)


def _two(ed, d=6.0, gap=0.0):
    f = ed.add_file("video", duration=20.0, has_audio=True, channels=2)
    a = ed.add_clip(f, position=0.0, end=d, layer=T2)
    b = ed.add_clip(f, position=d + gap, end=d, layer=T2)
    return a, b


# ---------------------------------------------------------------------------
# set_clip_speed_tool
# ---------------------------------------------------------------------------

def test_speed_up_2x(ed):
    a, b = _two(ed)
    r = receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=2))
    assert ed.fake.calls[-1][:4] == ("time", "FORWARD", [a], "2X")
    assert dur(ed, a) == 3.0
    c = r["clips"][0]
    assert (c["duration_before"], c["duration_after"], c["effective_speed"], c["direction"]) == (6.0, 3.0, 2.0,
                                                                                                 "forward")
    one_step(ed)
    ed.undo()
    assert dur(ed, a) == 6.0
    ed.redo()
    assert dur(ed, a) == 3.0


def test_slow_motion_runs_into_the_next_clip_unless_rippled_or_allowed(ed):
    a, b = _two(ed)
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=0.5), "would overlap", "ripple=true")
    assert ed.fake.calls == []
    r = receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=0.5, ripple=True))
    assert dur(ed, a) == 12.0 and pos(ed, b) == 12.0 and r["shifted"] == [b]
    one_step(ed)
    ed.undo()
    assert dur(ed, a) == 6.0 and pos(ed, b) == 6.0


def test_slow_motion_with_allow_overlap_reports_it(ed):
    a, b = _two(ed)
    r = receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=0.5, allow_overlap=True))
    assert r["overlaps"][0]["overlaps"] == b and pos(ed, b) == 6.0


def test_reverse_and_reverse_with_speed(ed):
    a, _b = _two(ed)
    r = receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], reverse=True))
    assert ed.fake.calls[-1][1] == "REVERSE" and r["clips"][0]["direction"] == "backward"
    ed.mark()
    receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=3, reverse=True))
    assert ed.fake.calls[-1][1:4] == ("BACKWARD", [a], "3X")
    assert dur(ed, a) == 2.0


def test_target_duration_and_absolute_speed(ed):
    a, _b = _two(ed)
    receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], target_duration_seconds=4))
    assert ed.fake.calls[-1][3] == "1.5X" and dur(ed, a) == 4.0
    ed.mark()
    # back to normal speed, same part of the source
    r = receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=1, absolute=True, ripple=True))
    assert dur(ed, a) == 6.0 and abs(r["clips"][0]["effective_speed"] - 1.0) < 0.01


def test_looped_clip_needs_reset_first(ed):
    a, _b = _two(ed, gap=30)
    receipt(ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=2))
    ed.mark()
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=2), "looped", "reset=true")
    receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=2, reset=True, allow_overlap=True))
    assert [c[1] for c in ed.fake.calls[-2:]] == ["NONE", "FORWARD"]
    assert not ed.clip(a).get("repeat_cache")
    one_step(ed)


def test_speed_refusals(ed):
    a, _b = _two(ed)
    refused(ed, ed.call("set_clip_speed_tool", speed=2), "say which clips")
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a]), "nothing to change")
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=2, target_duration_seconds=3), "not both")
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], absolute=True, reverse=True), "needs a speed")
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=1), "already plays")
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], target_duration_seconds=0.1), "1/16x to 16x")
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=40), "<= 16")
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed="fast"), "must be a number")
    ed.lock_track(T2)
    refused(ed, ed.call("set_clip_speed_tool", timeline_clip_ids=[a], speed=2), "locked")
    assert ed.fake.calls == []


def test_speed_string_arguments_are_coerced(ed):
    a, _b = _two(ed)
    receipt(ed.call("set_clip_speed_tool", timeline_clip_ids=json.dumps([a]), speed="2", reverse="true"))
    assert ed.fake.calls[-1][1] == "BACKWARD"


# ---------------------------------------------------------------------------
# repeat_clip_tool
# ---------------------------------------------------------------------------

def test_loop_three_times(ed):
    a, b = _two(ed, d=2.0, gap=10)
    r = receipt(ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=3))
    assert ed.fake.calls[-1] == ("repeat", "loop", 1, 3, [a], 0, 0.0)
    assert dur(ed, a) == 6.0 and r["clips"][0]["direction"] == "looped"
    one_step(ed)
    ed.undo()
    assert dur(ed, a) == 2.0
    # the merged-in cache outlives the undo, but the clip no longer counts as looped
    ed.mark()
    receipt(ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=2))
    assert dur(ed, a) == 4.0


def test_ping_pong_reverse_with_delay_and_ramp(ed):
    a, _b = _two(ed, d=2.0, gap=20)
    receipt(ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=2, pattern="ping_pong", reverse=True,
                    delay_seconds=0.5, speed_ramp_percent=100))
    assert ed.fake.calls[-1] == ("repeat", "pingpong", -1, 2, [a], 15, 1.0)
    assert dur(ed, a) == 3.5  # 2 s + 1 s (twice as fast) + 0.5 s pause


def test_repeat_refusals_and_ripple(ed):
    a, b = _two(ed, d=2.0)
    refused(ed, ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=4), "would overlap")
    refused(ed, ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=1), ">= 2")
    receipt(ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=4, ripple=True))
    assert pos(ed, b) == 8.0
    ed.mark()
    refused(ed, ed.call("repeat_clip_tool", timeline_clip_ids=[a], times=2, ripple=True), "already looped")


# ---------------------------------------------------------------------------
# freeze_frame_tool
# ---------------------------------------------------------------------------

def test_freeze_on_the_last_frame(ed):
    a, _b = _two(ed, gap=10)
    r = receipt(ed.call("freeze_frame_tool", timeline_clip_id=a, frame="last", hold_seconds=2))
    name, ids, speed, at = ed.fake.calls[-1][1:]
    assert (name, ids) == ("FREEZE", [a]) and float(speed) == 2.0
    assert abs(at - (6.0 - 1 / 30)) < 1e-9
    assert dur(ed, a) == 8.0 and r["clip"]["duration_after"] == 8.0
    one_step(ed)
    ed.undo()
    assert dur(ed, a) == 6.0


def test_freeze_at_a_time_with_zoom_and_ripple(ed):
    a, b = _two(ed)
    receipt(ed.call("freeze_frame_tool", timeline_clip_id=a, at_seconds=2.5, hold_seconds=1, zoom=True, ripple=True))
    assert ed.fake.calls[-1][1] == "FREEZE_ZOOM" and ed.fake.calls[-1][4] == 2.5
    assert pos(ed, b) == 7.0


def test_freeze_first_frame_uses_the_clip_start_and_playhead_default(ed):
    a, _b = _two(ed, gap=10)
    ed.call("move_clips_tool", timeline_clip_ids=[a], position_seconds=1.0)
    receipt(ed.call("freeze_frame_tool", timeline_clip_id=a, frame="first"))
    assert ed.fake.calls[-1][4] == 1.0
    ed.window.preview_thread.player.Position.return_value = 91  # 3.0 s
    receipt(ed.call("freeze_frame_tool", timeline_clip_id=a))
    assert ed.fake.calls[-1][4] == 3.0


def test_freeze_refusals(ed):
    a, _b = _two(ed)
    refused(ed, ed.call("freeze_frame_tool", timeline_clip_id=a, at_seconds=9.0), "outside")
    refused(ed, ed.call("freeze_frame_tool", timeline_clip_id=a, frame="last"), "would overlap")
    refused(ed, ed.call("freeze_frame_tool", timeline_clip_id="nope", frame="last"), "No timeline clip")
    ed.lock_track(T2)
    refused(ed, ed.call("freeze_frame_tool", timeline_clip_id=a, frame="first", ripple=True), "locked")


# ---------------------------------------------------------------------------
# separate_clip_audio_tool
# ---------------------------------------------------------------------------

def test_separate_audio_onto_the_track_below(ed):
    f = ed.add_file("video", duration=8.0, has_audio=True, channels=2)
    a = ed.add_clip(f, position=1.0, end=8.0, layer=T2)
    r = receipt(ed.call("separate_clip_audio_tool", timeline_clip_ids=[a]))
    assert ed.fake.calls[-1] == ("split_audio", "SINGLE", [a])
    new = r["audio_clips"][0]
    assert new["track"] == 1 and new["video"] == "off" and new["position"] == 1.0
    assert r["originals"][0]["audio"] == "off"
    one_step(ed)
    ed.undo()
    assert len(Clip.filter()) == 1


def test_separate_audio_to_a_chosen_track(ed):
    f = ed.add_file("video", duration=8.0, has_audio=True, channels=2)
    a = ed.add_clip(f, position=0.0, end=8.0, layer=T2)
    r = receipt(ed.call("separate_clip_audio_tool", timeline_clip_ids=[a], to_track="3"))
    assert r["audio_clips"][0]["track"] == 3


def test_separate_audio_refusals(ed):
    silent = ed.add_file("video", duration=8.0)          # fixture video has no audio
    a = ed.add_clip(silent, position=0.0, end=8.0, layer=T2)
    refused(ed, ed.call("separate_clip_audio_tool", timeline_clip_ids=[a]), "no audio")
    song = ed.add_file("audio")
    m = ed.add_clip(song, position=0.0, end=5.0, layer=T3)
    refused(ed, ed.call("separate_clip_audio_tool", timeline_clip_ids=[m]), "already audio-only")
    f = ed.add_file("video", duration=8.0, has_audio=True, channels=2)
    v = ed.add_clip(f, position=20.0, end=8.0, layer=T2)
    ed.add_clip(f, position=22.0, end=2.0, layer=T1)      # something already under it
    refused(ed, ed.call("separate_clip_audio_tool", timeline_clip_ids=[v]), "would overlap", "allow_overlap")
    ed.lock_track(T1)
    refused(ed, ed.call("separate_clip_audio_tool", timeline_clip_ids=[v]), "track 1 is locked")
    ed.lock_track(T1, False)
    receipt(ed.call("separate_clip_audio_tool", timeline_clip_ids=[v], allow_overlap=True))
    ed.mark()
    refused(ed, ed.call("separate_clip_audio_tool", timeline_clip_ids=[v], allow_overlap=True), "already has its audio off")
    assert ed.fake.calls[-1][2] == [v] and len([c for c in ed.fake.calls if c[0] == "split_audio"]) == 1


def test_separate_audio_per_channel(ed):
    song = ed.add_file("audio")
    m = ed.add_clip(song, position=0.0, end=5.0, layer=T3)
    r = receipt(ed.call("separate_clip_audio_tool", timeline_clip_ids=[m], per_channel=True))
    assert ed.fake.calls[-1][1] == "MULTIPLE" and len(r["audio_clips"]) == 2
    ed.mark()
    refused(ed, ed.call("separate_clip_audio_tool", timeline_clip_ids=[m], per_channel=True, to_track="1"),
            "to_track works")


# ---------------------------------------------------------------------------
# set_clip_audio_video_tool
# ---------------------------------------------------------------------------

def _y(kf):
    return kf["Points"][0]["co"]["Y"]


def test_mute_and_unmute(ed):
    a, _b = _two(ed)
    r = receipt(ed.call("set_clip_audio_video_tool", timeline_clip_ids=[a], audio="off"))
    assert _y(ed.clip(a)["has_audio"]) == 0.0 and r["clips"][0]["audio"] == "off"
    one_step(ed)
    ed.undo()
    assert _y(ed.clip(a)["has_audio"]) == -1.0
    ed.mark()
    r = receipt(ed.call("set_clip_audio_video_tool", timeline_clip_ids=[a], audio="auto"))
    assert r["changed"] is False and ed.undo_steps_since_mark() == 0


def test_audio_only_and_video_refusals(ed):
    a, _b = _two(ed)
    receipt(ed.call("set_clip_audio_video_tool", timeline_clip_ids=[a], video="off"))
    assert _y(ed.clip(a)["has_video"]) == 0.0
    song = ed.add_file("audio")
    m = ed.add_clip(song, position=0.0, end=5.0, layer=T3)
    refused(ed, ed.call("set_clip_audio_video_tool", timeline_clip_ids=[m], video="on"), "no picture")
    silent = ed.add_file("video", duration=8.0)
    s = ed.add_clip(silent, position=0.0, end=4.0, layer=T1)
    refused(ed, ed.call("set_clip_audio_video_tool", timeline_clip_ids=[s], audio="on"), "no audio")
    refused(ed, ed.call("set_clip_audio_video_tool", timeline_clip_ids=[s], waveform="show"), "no waveform")
    refused(ed, ed.call("set_clip_audio_video_tool", timeline_clip_ids=[s]), "nothing to change")
    ed.lock_track(T3)
    refused(ed, ed.call("set_clip_audio_video_tool", timeline_clip_ids=[m], audio="off"), "locked")


def test_waveform_show_joins_the_undo_step_and_hide(ed):
    a, _b = _two(ed)
    r = receipt(ed.call("set_clip_audio_video_tool", timeline_clip_ids=[a], waveform="show"))
    call = ed.fake.calls[-1]
    assert call[0] == "show_waveform" and call[1] == [a] and call[2]  # the tool's transaction id
    assert r["waveform_requested"] == [a]
    one_step(ed)
    ed.mark()
    r = receipt(ed.call("set_clip_audio_video_tool", timeline_clip_ids=[a], waveform="hide"))
    assert ed.fake.calls[-1] == ("hide_waveform", [a]) and r["waveform_hidden"] == [a]
    one_step(ed)
    ed.mark()
    r = receipt(ed.call("set_clip_audio_video_tool", timeline_clip_ids=[a], waveform="hide"))
    assert r["changed"] is False and ed.undo_steps_since_mark() == 0
