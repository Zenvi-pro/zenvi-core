"""prepare_recording_tool: the Recording dock set up like a person would, never recording.

The dock is the real AudioRecordingDockContent class (its own set_recording_context,
refresh_tracks, _source_toggled, _set_channels, _select_full_screen, preview and button
logic) on fake widgets; only device discovery and capture are faked.
"""

import ast
import json
import os
from unittest.mock import MagicMock

import pytest

from ai_generation_fakes import install_recording
from classes.recording_placement import recording_track_for_clip

TOOL = "prepare_recording_tool"
L1, L2, L3, L4, L5 = 1000000, 2000000, 3000000, 4000000, 5000000
NO_TRACK = "__no_recording_track__"


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def rec(editor, monkeypatch):
    return install_recording(editor, monkeypatch)


def test_voiceover_on_a_track_at_a_time(editor, rec):
    out = editor.call(TOOL, sources=["mic"], track="3", start_seconds=5)
    r = _receipt(out)
    assert rec.shown == [(5.0, L3)]
    rec.window.SeekSignal.emit.assert_called_once_with(151)          # 5 s at 30 fps
    assert r["sources"]["mic"]["selected"] is True and r["sources"]["screen"]["selected"] is False
    assert r["track"] == {"track": 3, "name": "Track 3", "layer": L3}
    assert r["start_seconds"] == 5.0 and r["ready_to_record"] is True
    assert r["mic"]["devices"] == ["Default input", "Built-in Microphone", "USB Mic"]
    assert rec.dock.monitoring is True, "the mic card starts the level meter, as clicking it does"
    assert out.startswith("Recording panel ready: mic -> track 3 (Track 3), start 5.00 s. Press Start Recording")
    assert r["record_button"] == "Start Recording" and rec.dock._recording is False
    assert editor.undo_steps_since_mark() == 0


def test_a_later_call_keeps_the_earlier_start_and_track(editor, rec):
    _receipt(editor.call(TOOL, sources=["mic"], track="3", start_seconds=5))
    r = _receipt(editor.call(TOOL, channels="stereo"))
    assert rec.shown == [(5.0, L3), (5.0, L3)]
    assert r["start_seconds"] == 5.0 and r["track"]["layer"] == L3 and r["mic"]["channels"] == "stereo"
    rec.window.SeekSignal.emit.assert_called_once_with(151)       # only the explicit start moves the playhead


def test_no_arguments_just_opens_the_panel(editor, rec):
    r = _receipt(editor.call(TOOL))
    assert rec.shown == [(None, None)]
    assert r["ready_to_record"] is False and r["next_step"] == "Select at least one available source."
    assert r["start"] == "playhead when Record is pressed"
    rec.window.SeekSignal.emit.assert_not_called()


def test_screen_with_webcam_corner(editor, rec):
    r = _receipt(editor.call(TOOL, sources=["screen", "webcam"], screen="DELL", screen_fps=60, show_cursor=False,
                             webcam_layout="top-left", webcam_size=0.2, webcam_corners="oval",
                             webcam_resolution="1920x1080"))
    assert r["screen"]["screen"].startswith("DELL") and r["screen"]["fps"] == 60
    assert r["screen"]["area"] == {"x": 0, "y": 0, "width": 3840, "height": 2160}
    assert r["screen"]["show_cursor"] is False and r["screen"]["mode"] == "full_screen"
    assert r["webcam"]["layout"] == "top-left" and r["webcam"]["size"] == 0.2 and r["webcam"]["corners"] == "oval"
    assert r["webcam"]["resolution"] == "1920x1080" and r["webcam"]["devices"] == ["FaceTime HD Camera"]
    assert r["preview"] == "off", "screen capture forces the timeline preview off"
    assert rec.dock.webcam_preview is True


def test_screen_region_and_hide_zenvi(editor, rec):
    r = _receipt(editor.call(TOOL, sources=["screen"], screen_region={"x": 100, "y": 50, "width": 800, "height": 600},
                             hide_zenvi=True, system_audio=False))
    assert r["screen"]["mode"] == "region"
    assert r["screen"]["area"] == {"x": 100, "y": 50, "width": 800, "height": 600}
    assert r["screen"]["hide_zenvi"] is True and r["screen"]["system_audio"] is False


def test_mic_formats_channels_and_device(editor, rec):
    r = _receipt(editor.call(TOOL, sources=["mic"], mic_device="usb", channels="stereo", audio_format="wav",
                             sample_rate=44100))
    assert r["mic"] == {"device": "USB Mic", "devices": ["Default input", "Built-in Microphone", "USB Mic"],
                        "channels": "stereo", "format": "wav", "sample_rate": 44100}
    assert rec.dock._sample_rate == 44100 and rec.dock._preferred_format == "wav"


def test_record_over_a_clip_uses_the_clip_menu_rule(editor, rec):
    video = editor.add_file("video")
    clip = editor.add_clip(video, position=3.0, layer=L3)
    editor.add_clip(video, position=0.0, layer=L2)            # L2 busy under the clip -> L1
    r = _receipt(editor.call(TOOL, sources=["mic"], timeline_clip_id=clip))
    assert rec.shown == [(3.0, L1)]
    assert r["track"]["layer"] == L1 and r["start_seconds"] == 3.0 and r["over_clip"] == clip


def test_explicit_track_wins_over_the_clip_rule(editor, rec):
    video = editor.add_file("video")
    clip = editor.add_clip(video, position=3.0, layer=L3)
    _receipt(editor.call(TOOL, sources=["mic"], timeline_clip_id=clip, track="5", start_seconds=4))
    assert rec.shown == [(4.0, L5)]


def test_project_files_only(editor, rec):
    r = _receipt(editor.call(TOOL, sources=["mic"], track="none"))
    assert rec.shown == [(None, NO_TRACK)]
    assert r["track"]["track"] == "none"
    assert "Project Files only" in editor.call(TOOL, sources=["mic"], track="none").split("\n")[0]


def test_recording_view_switch(editor, rec):
    _receipt(editor.call(TOOL, sources=["mic"], recording_view=True))
    rec.window.actionAudio_Recording_View_trigger.assert_called_once_with()


@pytest.mark.parametrize("args, message", [
    ({"sources": ["screen"], "preview": "full"}, "preview is always off while the screen is recorded"),
    ({"sources": ["mic"], "mic_device": "Blue Yeti"}, "no microphone matches 'Blue Yeti'"),
    ({"sources": ["screen"], "screen": "Projector"}, "no screen matches 'Projector'"),
    ({"sources": ["mic"], "webcam_layout": "top-left"}, "webcam settings need the webcam source"),
    ({"sources": ["screen"], "channels": "mono"}, "need the mic source"),
    ({"sources": ["mic"], "screen_fps": 30}, "screen settings need the screen source"),
    ({"sources": ["mic"], "sample_rate": 22050}, "sample_rate must be one of 44100, 48000, 96000"),
    ({"sources": ["screen"], "screen_fps": 50}, "screen_fps must be one of 15, 24, 30, 60"),
    ({"sources": ["webcam", "screen"], "webcam_size": 0.25}, "webcam_size must be 0.2, 0.3 or 0.4"),
    ({"sources": ["screen"], "screen_region": {"x": 0, "y": 0, "width": 8, "height": 600}}, "at least 16x16"),
    ({"sources": ["mic"], "track": "Nope"}, "Error"),
    ({"sources": ["webcam"], "webcam_fps": 60}, "does not offer 60 fps"),
    ({"sources": ["mic"], "timeline_clip_id": "missing"}, "Error"),
    ({"sources": ["telepathy"]}, "must be one of"),
])
def test_refusals(editor, rec, args, message):
    out = editor.call(TOOL, **args)
    assert out.startswith("Error") and message in out, out
    assert editor.undo_steps_since_mark() == 0


def test_validation_happens_before_the_panel_changes(editor, rec):
    assert editor.call(TOOL, sources=["mic"], mic_device="Blue Yeti").startswith("Error")
    assert rec.shown == [] and not rec.dock.mic_card.isChecked()


def test_unavailable_source_is_refused_with_the_docks_reason(editor, monkeypatch):
    rec = install_recording(editor, monkeypatch, available=("mic",))
    out = editor.call(TOOL, sources=["screen"])
    assert out == "Error: screen: Screen recording is not available for this platform or libopenshot build."
    assert rec.shown == []
    r = _receipt(editor.call(TOOL, sources=["mic"]))
    assert r["sources"]["screen"] == {"selected": False, "available": False,
                                      "reason": "Screen recording is not available for this platform or libopenshot build."}


def test_system_audio_unavailable(editor, monkeypatch):
    install_recording(editor, monkeypatch, system_audio=False)
    out = editor.call(TOOL, sources=["screen"], system_audio=True)
    assert out.startswith("Error: system audio recording is not available")


def test_stereo_on_a_mono_only_microphone(editor, monkeypatch):
    install_recording(editor, monkeypatch, mono_only=True)
    out = editor.call(TOOL, sources=["mic"], channels="stereo")
    assert out.startswith("Error: this microphone does not record in stereo")


def test_locked_track_is_refused(editor, rec):
    editor.lock_track(L2)
    out = editor.call(TOOL, sources=["mic"], track="2")
    assert out.startswith("Error: track 2 is locked")


def test_a_recording_in_progress_is_left_alone(editor, rec):
    rec.dock._recording = True
    out = editor.call(TOOL, sources=["screen"])
    assert out.startswith("Error: a recording is in progress")
    assert not rec.dock.screen_card.isChecked()


# --- the Clip > Audio > Record rule and its handler -------------------------------------------------

def _track(n, lock=False):
    return {"number": n, "lock": lock}


def test_recording_track_for_clip_rule():
    clip = {"layer": L3, "position": 10.0, "start": 0.0, "end": 5.0}
    tracks = [_track(L1), _track(L2), _track(L3)]
    assert recording_track_for_clip(clip, tracks, []) == L2
    busy = [{"layer": L2, "position": 12.0, "start": 0.0, "end": 1.0}]
    assert recording_track_for_clip(clip, tracks, busy) == L1
    assert recording_track_for_clip(clip, [_track(L1, True), _track(L2, True), _track(L3)], []) == L3
    assert recording_track_for_clip({"layer": L1, "position": 0, "end": 1}, tracks, []) == L1
    assert recording_track_for_clip({"layer": "x"}, tracks, []) == 1


_TIMELINE = os.path.join(os.path.dirname(__file__), "..", "src", "windows", "views", "timeline.py")


def _timeline_methods(names, namespace):
    with open(_TIMELINE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), _TIMELINE)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TimelineView")
    funcs = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    exec(compile(ast.Module(body=[funcs[n] for n in names], type_ignores=[]), _TIMELINE, "exec"), namespace)
    return {n: namespace[n] for n in names}


def test_clip_menu_record_moves_the_playhead_and_picks_the_free_lower_track(editor):
    """Regression: PlayheadMoved(frame, True) raised TypeError (swallowed by guarded_slot): no seek."""
    from classes.query import Clip, Track
    methods = _timeline_methods(["_recording_track_for_clip", "_record_from_clip"],
                                {"get_app": lambda: editor.app, "Track": Track, "Clip": Clip})

    class View:
        _recording_track_for_clip = methods["_recording_track_for_clip"]
        _record_from_clip = methods["_record_from_clip"]

        def PlayheadMoved(self, position_frames):          # guarded_slot(int): one argument
            self.seeks.append(position_frames)

    view = View()
    view.seeks = []
    view._show_recording_dock_deferred = MagicMock()
    video = editor.add_file("video")
    clip_id = editor.add_clip(video, position=2.0, layer=L3)
    view._record_from_clip(Clip.get(id=clip_id))
    assert view.seeks == [61]
    view._show_recording_dock_deferred.assert_called_once_with(start_time=2.0, track_number=L2)


def test_post_recording_seek_uses_the_one_argument_signal():
    """Regression: SeekSignal is pyqtSignal(int); emit(frame, True) raised and the seek never happened."""
    src = os.path.join(os.path.dirname(__file__), "..", "src", "windows", "audio_recording.py")
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "SeekSignal.emit(frame_number, True)" not in text
    assert "SeekSignal.emit(frame_number)" in text
