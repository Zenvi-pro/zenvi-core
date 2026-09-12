"""Agent-facing audio mix / ducking handlers (headless, no Qt event loop)."""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub Qt before importing tool_handlers (headless CI).
_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

import pytest  # noqa: E402

from classes import audio_mix as am  # noqa: E402
from classes import tool_handlers  # noqa: E402
from classes.clip_resolver import ResolveResult  # noqa: E402

MUSIC_TRACK = 1000000
SPEECH_TRACK = 2000000

CUES = [{"start": 10.0, "end": 15.0, "text": "hello"}]


class _FakeClip:
    """Stands in for classes.query.Clip: .data is replaced then .save()d."""

    def __init__(self, clip_id, data):
        self.id = clip_id
        self.data = data
        self.saved = []

    def save(self):
        self.saved.append(dict(self.data))


def _speech_clip(clip_id="SPEECH1", position=0.0, end=30.0, layer=SPEECH_TRACK):
    return _FakeClip(
        clip_id,
        {
            "id": clip_id,
            "title": "Interview",
            "position": position,
            "start": 0.0,
            "end": end,
            "layer": layer,
            "file_id": "F_SPEECH",
            "reader": {"has_audio": True, "channels": 2, "has_video": True},
        },
    )


def _music_clip(clip_id="MUSIC1", position=0.0, end=60.0, layer=MUSIC_TRACK, **over):
    data = {
        "id": clip_id,
        "title": "Bed",
        "position": position,
        "start": 0.0,
        "end": end,
        "layer": layer,
        "file_id": "F_MUSIC",
        "reader": {"has_audio": True, "channels": 2, "has_video": False},
    }
    data.update(over)
    return _FakeClip(clip_id, data)


def _mock_app(locked_layers=()):
    app = MagicMock()

    def _project_get(key):
        if key == "layers":
            return [
                {"number": MUSIC_TRACK, "id": "L1", "label": "Music",
                 "lock": MUSIC_TRACK in locked_layers},
                {"number": SPEECH_TRACK, "id": "L2", "label": "Voice",
                 "lock": SPEECH_TRACK in locked_layers},
            ]
        if key == "fps":
            return {"num": 30, "den": 1}
        return None

    app.project.get.side_effect = _project_get
    app.updates.transaction_id = None
    app.thread.return_value = "main"
    return app


class _TxRecorder:
    """Records every value assigned to app.updates.transaction_id."""

    def __init__(self, app):
        self.app = app
        self.seen = []

    def install(self):
        recorder = self

        class _Updates:
            @property
            def transaction_id(self):
                return recorder.seen[-1] if recorder.seen else None

            @transaction_id.setter
            def transaction_id(self, value):
                recorder.seen.append(value)

        self.app.updates = _Updates()
        return self


def _run(handler, clips, *, app=None, file_meta=None, **kwargs):
    """Invoke a handler against a fake timeline."""
    app = app or _mock_app()
    files = file_meta or {
        "F_SPEECH": {
            "media_type": "video",
            "ai_metadata": {"analyzed": True, "transcript_cues": CUES},
        },
        "F_MUSIC": {"media_type": "audio", "ai_metadata": {"analyzed": True}},
    }

    def _file_get(id=None, **_kw):
        data = files.get(str(id))
        if data is None:
            return None
        obj = MagicMock()
        obj.data = data
        return obj

    fake_query = MagicMock()
    fake_query.Clip.filter.return_value = clips
    fake_query.File.get.side_effect = _file_get

    # QThread=None keeps the handler inline even if a earlier test imported real Qt.
    with patch.object(tool_handlers, "QThread", None):
        with patch.dict(sys.modules, {"classes.query": fake_query}):
            with patch.object(tool_handlers, "_get_app", return_value=app):
                with patch.object(tool_handlers, "_refresh_audio_ui") as refresh:
                    out = handler(**kwargs)
    return out, app, refresh


def _points_of(clip):
    assert clip.saved, f"{clip.id} was never saved"
    return clip.saved[-1]["volume"]["Points"]


# --------------------------------------------------------------------------
# duck_under_speech
# --------------------------------------------------------------------------

def test_duck_writes_volume_on_the_bed_only():
    music, speech = _music_clip(), _speech_clip()
    out, _app, _r = _run(tool_handlers.duck_under_speech, [music, speech])
    assert not out.startswith("Error"), out
    assert music.saved, "bed should have been ducked"
    assert not speech.saved, "speech clip must not be touched"
    points = _points_of(music)
    xs = [p["co"]["X"] for p in points]
    assert xs == sorted(xs)
    ys = [p["co"]["Y"] for p in points]
    assert min(ys) < 0.3 and max(ys) == 1.0  # dips and restores


def test_duck_never_writes_layer_position_or_trim():
    music, speech = _music_clip(), _speech_clip()
    _run(tool_handlers.duck_under_speech, [music, speech])
    saved = music.saved[-1]
    assert set(saved) == {"volume"}
    for forbidden in ("layer", "position", "start", "end"):
        assert forbidden not in saved


def test_duck_uses_one_transaction_and_clears_it():
    music, speech = _music_clip(), _speech_clip()
    app = _mock_app()
    recorder = _TxRecorder(app).install()
    _run(tool_handlers.duck_under_speech, [music, speech], app=app)
    assert len(recorder.seen) == 2
    assert isinstance(recorder.seen[0], str) and recorder.seen[0]
    assert recorder.seen[1] is None  # cleared in finally


def test_duck_dry_run_writes_nothing_but_reports_the_plan():
    music, speech = _music_clip(), _speech_clip()
    out, _app, _r = _run(
        tool_handlers.duck_under_speech, [music, speech], dry_run="true"
    )
    assert not music.saved and not speech.saved
    assert "dry_run=true" in out
    assert "Would duck" in out
    assert "MUSIC1" in out


def test_duck_result_names_clips_windows_and_levels():
    music, speech = _music_clip(), _speech_clip()
    out, _app, _r = _run(tool_handlers.duck_under_speech, [music, speech])
    assert "timeline_clip_id=MUSIC1" in out
    assert "base=1.00 -> 0.25" in out
    assert "ducked (timeline s):" in out
    assert "restored (timeline s):" in out
    assert "speech sources: SPEECH1 (cues, 1 windows)" in out
    assert "-12.0 dB" in out


def test_duck_refuses_a_locked_bed_track():
    music, speech = _music_clip(), _speech_clip()
    out, _app, _r = _run(
        tool_handlers.duck_under_speech,
        [music, speech],
        app=_mock_app(locked_layers=(MUSIC_TRACK,)),
    )
    assert out.startswith("Error:") and "locked" in out
    assert not music.saved


def test_duck_without_speech_explains_instead_of_failing():
    music = _music_clip()
    out, _app, _r = _run(tool_handlers.duck_under_speech, [music])
    assert not out.startswith("Error")
    assert "No speech detected" in out
    assert "set_clip_volume_tool" in out
    assert not music.saved


def test_duck_names_unindexed_clips_rather_than_mixing_them_silently():
    unknown = _music_clip("UNK1")
    files = {
        "F_SPEECH": {"media_type": "video", "ai_metadata": {"analyzed": True, "transcript_cues": CUES}},
        "F_MUSIC": {"media_type": "audio", "ai_metadata": {}},
    }
    out, _app, _r = _run(tool_handlers.duck_under_speech, [unknown], file_meta=files)
    assert "not indexed" in out
    assert "UNK1" in out
    assert not unknown.saved


def test_duck_skips_a_bed_that_does_not_overlap_the_speech():
    # Speech runs 10-15s; this bed lives at 40-60s.
    music, speech = _music_clip(position=40.0, end=20.0), _speech_clip()
    out, _app, _r = _run(tool_handlers.duck_under_speech, [music, speech])
    assert not music.saved
    assert "no ducking was needed" in out or "do not overlap" in out


def test_duck_honours_explicit_bed_ids_and_warns_on_a_speech_bed():
    music, speech = _music_clip(), _speech_clip()
    out, _app, _r = _run(
        tool_handlers.duck_under_speech,
        [music, speech],
        bed_clip_ids="SPEECH1",
    )
    assert "WARNING" in out and "carries speech" in out
    assert speech.saved and not music.saved


def test_duck_amount_is_configurable():
    music, speech = _music_clip(), _speech_clip()
    _run(tool_handlers.duck_under_speech, [music, speech], duck_db="-6")
    ys = [p["co"]["Y"] for p in _points_of(music)]
    assert abs(min(ys) - am.db_to_gain(-6)) < 1e-4


def test_duck_boosts_speech_only_when_asked():
    music, speech = _music_clip(), _speech_clip()
    _run(tool_handlers.duck_under_speech, [music, speech], boost_speech_db="1")
    assert speech.saved, "boost should write a level on the speech clip"
    boosted = _points_of(speech)
    assert len(boosted) == 1
    assert abs(boosted[0]["co"]["Y"] - am.db_to_gain(1)) < 1e-4


def test_duck_boost_is_capped_at_the_volume_menu_ceiling():
    music, speech = _music_clip(), _speech_clip()
    _run(tool_handlers.duck_under_speech, [music, speech], boost_speech_db="12")
    assert _points_of(speech)[0]["co"]["Y"] == am.MAX_LEVEL


def test_duck_warns_about_overlapping_speech_clips():
    a = _speech_clip("SP_A", position=0.0, end=30.0)
    b = _speech_clip("SP_B", position=5.0, end=30.0, layer=MUSIC_TRACK)
    out, _app, _r = _run(tool_handlers.duck_under_speech, [a, b, _music_clip()])
    assert "overlap" in out
    assert "SP_A" in out and "SP_B" in out


def test_duck_explains_when_a_declared_speech_clip_yields_no_windows():
    """Named as speech but no cues and no waveform — say so, don't guess."""
    voice = _music_clip("VO1", end=10.0)
    bed = _music_clip("BED1", end=10.0)
    files = {"F_MUSIC": {"media_type": "audio", "ai_metadata": {"analyzed": True}}}
    out, _app, _r = _run(
        tool_handlers.duck_under_speech,
        [voice, bed],
        file_meta=files,
        speech_clip_ids="VO1",
        bed_clip_ids="BED1",
    )
    assert "Could not resolve any speech time windows" in out
    assert "VO1" in out
    assert not bed.saved and not voice.saved


def test_duck_rejects_an_unknown_explicit_id():
    out, _app, _r = _run(
        tool_handlers.duck_under_speech,
        [_music_clip(), _speech_clip()],
        speech_clip_ids="NOPE",
    )
    assert out.startswith("Error:") and "NOPE" in out


def test_duck_falls_back_to_waveform_energy_for_a_declared_speech_clip():
    """An un-indexed clip the agent names as speech still yields windows."""
    voice = _music_clip("VO1", end=10.0)
    voice.data["ui"] = {"audio_data": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]}
    bed = _music_clip("BED1", end=10.0)
    files = {"F_MUSIC": {"media_type": "audio", "ai_metadata": {"analyzed": True}}}
    out, _app, _r = _run(
        tool_handlers.duck_under_speech,
        [voice, bed],
        file_meta=files,
        speech_clip_ids="VO1",
        bed_clip_ids="BED1",
    )
    assert not out.startswith("Error"), out
    assert bed.saved
    assert "energy" in out


def test_duck_refreshes_waveforms_only_for_clips_that_have_them():
    music, speech = _music_clip(), _speech_clip()
    music.data["ui"] = {"audio_data": [0.5] * 10}
    _out, _app, refresh = _run(tool_handlers.duck_under_speech, [music, speech])
    files_arg = refresh.call_args[0][1]
    assert files_arg == {"F_MUSIC": ["MUSIC1"]}


# --------------------------------------------------------------------------
# set_clip_volume
# --------------------------------------------------------------------------

def _run_set(clip, **kwargs):
    app = _mock_app(kwargs.pop("locked_layers", ()))
    resolved = ResolveResult(ok=True, clip=clip)
    file_obj = MagicMock()
    file_obj.data = {"media_type": "audio", "reader": {"has_audio": True, "channels": 2}}
    with patch.object(tool_handlers, "QThread", None):
        with patch.object(tool_handlers, "_get_app", return_value=app):
            with patch.object(tool_handlers, "_resolve_timeline_clip_for_tool", return_value=resolved):
                with patch.object(tool_handlers, "_get_source_file_for_clip", return_value=file_obj):
                    with patch.object(tool_handlers, "_refresh_audio_ui"):
                        return tool_handlers.set_clip_volume(**kwargs), app


def test_set_volume_whole_clip_writes_one_point():
    clip = _music_clip()
    out, _app = _run_set(clip, timeline_clip_id="MUSIC1", level="0.4")
    assert not out.startswith("Error"), out
    points = _points_of(clip)
    assert len(points) == 1
    assert points[0]["co"]["Y"] == 0.4
    assert "1.00 -> 0.400" in out


def test_set_volume_accepts_decibels():
    clip = _music_clip()
    _run_set(clip, timeline_clip_id="MUSIC1", level_db="-6")
    assert abs(_points_of(clip)[0]["co"]["Y"] - am.db_to_gain(-6)) < 1e-6


def test_set_volume_window_writes_four_points_with_ramps():
    clip = _music_clip(end=60.0)
    out, _app = _run_set(
        clip, timeline_clip_id="MUSIC1", level="0.3",
        start_seconds="10", end_seconds="20", fade_ms="200",
    )
    points = _points_of(clip)
    xs = [p["co"]["X"] for p in points]
    ys = [p["co"]["Y"] for p in points]
    assert len(points) == 4 and xs == sorted(xs)
    assert ys[0] == 1.0 and ys[3] == 1.0 and ys[1] == 0.3 and ys[2] == 0.3
    assert "10.00s-20.00s (timeline)" in out


def test_set_volume_requires_exactly_one_level_arg():
    clip = _music_clip()
    both, _ = _run_set(clip, timeline_clip_id="MUSIC1", level="0.5", level_db="-6")
    assert both.startswith("Error:") and "not both" in both
    neither, _ = _run_set(_music_clip(), timeline_clip_id="MUSIC1")
    assert neither.startswith("Error:") and "requires level_db or level" in neither


def test_set_volume_requires_a_target_clip():
    out = tool_handlers.set_clip_volume(level="0.5")
    assert out.startswith("Error:") and "timeline_clip_id or clip_query" in out


def test_set_volume_refuses_a_locked_track():
    clip = _music_clip()
    out, _app = _run_set(
        clip, timeline_clip_id="MUSIC1", level="0.4", locked_layers=(MUSIC_TRACK,)
    )
    assert out.startswith("Error:") and "locked" in out
    assert not clip.saved


def test_set_volume_rejects_a_window_outside_the_clip():
    clip = _music_clip(position=0.0, end=10.0)
    out, _app = _run_set(
        clip, timeline_clip_id="MUSIC1", level="0.4",
        start_seconds="50", end_seconds="60",
    )
    assert out.startswith("Error:") and "does not overlap" in out
    assert not clip.saved


def test_set_volume_scale_mode_multiplies_the_existing_level():
    clip = _music_clip(volume={"Points": [am.make_point(1, 0.8)]})
    _run_set(clip, timeline_clip_id="MUSIC1", level="0.5", mode="scale")
    assert abs(_points_of(clip)[0]["co"]["Y"] - 0.4) < 1e-6


def test_set_volume_uses_one_transaction():
    clip = _music_clip()
    _out, app = _run_set(clip, timeline_clip_id="MUSIC1", level="0.4")
    assert app.updates.transaction_id is None  # cleared in finally


# --------------------------------------------------------------------------
# analyze_timeline_audio
# --------------------------------------------------------------------------

def test_analyze_reports_roles_and_levels():
    out, _app, _r = _run(
        tool_handlers.analyze_timeline_audio, [_music_clip(), _speech_clip()]
    )
    assert "audio_role=speech" in out
    assert "audio_role=music" in out
    assert "level=1.00" in out
    assert "1 speech, 1 music/sfx bed(s)" in out


def test_analyze_lists_speech_windows_on_demand():
    out, _app, _r = _run(
        tool_handlers.analyze_timeline_audio,
        [_music_clip(), _speech_clip()],
        detail="windows",
    )
    assert "speech windows (cues, timeline s): 10.00-15.00" in out


def test_analyze_skips_clips_without_audio():
    silent = _music_clip("SILENT1")
    silent.data["reader"] = {"has_audio": False, "channels": 0}
    out, _app, _r = _run(tool_handlers.analyze_timeline_audio, [silent, _speech_clip()])
    assert "SILENT1" not in out


def test_analyze_flags_a_bed_stacked_above_speech():
    # Lower layer number == drawn below; this bed sits ABOVE the voice.
    bed = _music_clip("HIGHBED", layer=SPEECH_TRACK)
    voice = _speech_clip("VOICE", layer=MUSIC_TRACK)
    out, _app, _r = _run(tool_handlers.analyze_timeline_audio, [bed, voice])
    assert "sits above speech" in out
    assert "instead of restacking tracks" in out


def test_analyze_reports_an_empty_timeline_plainly():
    out, _app, _r = _run(tool_handlers.analyze_timeline_audio, [])
    assert out == "No timeline clips with audio."


def test_analyze_can_target_one_clip():
    out, _app, _r = _run(
        tool_handlers.analyze_timeline_audio,
        [_music_clip(), _speech_clip()],
        timeline_clip_id="MUSIC1",
    )
    assert "MUSIC1" in out and "SPEECH1" not in out
    missing, _app2, _r2 = _run(
        tool_handlers.analyze_timeline_audio,
        [_music_clip()],
        timeline_clip_id="NOPE",
    )
    assert missing.startswith("Error:")


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def test_tools_are_registered_and_labelled():
    for name in (
        "analyze_timeline_audio_tool",
        "set_clip_volume_tool",
        "duck_under_speech_tool",
    ):
        assert name in tool_handlers.AGENT_TOOL_HANDLERS
        assert name in tool_handlers.TOOL_DISPLAY_LABELS


def test_only_the_analyze_tool_is_read_only():
    assert "analyze_timeline_audio_tool" in tool_handlers.READ_ONLY_TOOLS
    assert "set_clip_volume_tool" not in tool_handlers.READ_ONLY_TOOLS
    assert "duck_under_speech_tool" not in tool_handlers.READ_ONLY_TOOLS
    assert "duck_under_speech_tool" not in tool_handlers.BACKGROUND_SAFE_TOOLS


# --------------------------------------------------------------------------
# Auto ducking: the envelope must depend on the material, not be a fixed -12
# --------------------------------------------------------------------------

def _volume_points(clip):
    """The volume curve the handler wrote on this clip."""
    assert clip.saved, f"{clip.id} was never saved"
    return clip.saved[-1]["volume"]["Points"]


def _duck_floor(clip):
    """Lowest Y in the written curve - the ducked level."""
    return min(p["co"]["Y"] for p in _volume_points(clip))


def _with_level(clip, level):
    clip.data["volume"] = {
        "Points": [{"co": {"X": 1.0, "Y": level}, "interpolation": 1}]
    }
    return clip


def test_auto_duck_cuts_a_loud_bed_further_than_a_quiet_one():
    """The reported bug: every mix sounded the same regardless of the sources."""
    loud = _with_level(_music_clip(), 1.2)
    _run(tool_handlers.duck_under_speech, [loud, _speech_clip()])
    loud_floor = _duck_floor(loud)

    quiet = _with_level(_music_clip(), 0.35)
    _run(tool_handlers.duck_under_speech, [quiet, _speech_clip()])
    quiet_floor = _duck_floor(quiet)

    # The attenuation applied differs a lot: the loud bed is cut hard, the quiet
    # one is nudged. Both then sit near the same target under the speech, which
    # is the point - the ENVELOPE is no longer identical for every source.
    loud_db = am.gain_to_db(loud_floor / 1.2)
    quiet_db = am.gain_to_db(quiet_floor / 0.35)
    assert loud_db < quiet_db - 6.0
    assert quiet_db == pytest.approx(am.AUTO_DUCK_MAX_DB, abs=0.1)
    assert loud_db == pytest.approx(am.AUTO_DUCK_HEADROOM_DB - am.gain_to_db(1.2), abs=0.1)


def test_auto_duck_barely_touches_a_bed_that_is_already_out_of_the_way():
    quiet = _with_level(_music_clip(), am.db_to_gain(-12.0))
    _run(tool_handlers.duck_under_speech, [quiet, _speech_clip()])
    ratio = _duck_floor(quiet) / am.db_to_gain(-12.0)
    assert ratio == pytest.approx(am.db_to_gain(am.AUTO_DUCK_MAX_DB), rel=1e-3)


def test_an_explicit_duck_db_still_overrides_auto():
    bed = _with_level(_music_clip(), 1.2)
    _run(tool_handlers.duck_under_speech, [bed, _speech_clip()], duck_db="-6")
    assert _duck_floor(bed) == pytest.approx(1.2 * am.db_to_gain(-6.0), rel=1e-3)


def test_auto_is_reported_as_auto_and_per_bed_in_the_result():
    out, _app, _r = _run(
        tool_handlers.duck_under_speech, [_music_clip(), _speech_clip()]
    )
    assert "duck=auto" in out
    assert "dB)" in out, "each bed must report the dB it was ducked by"


def test_analyze_reports_the_level_in_db_as_well_as_linear():
    out, _app, _r = _run(
        tool_handlers.analyze_timeline_audio,
        [_with_level(_music_clip(), 0.5), _speech_clip()],
    )
    assert "level=0.50" in out
    assert "-6.0 dB" in out
