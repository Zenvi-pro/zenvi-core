"""Unit tests for audio mix / duck math (pure, no Qt or libopenshot)."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from classes import audio_mix as am  # noqa: E402

FPS30 = {"num": 30, "den": 1}
FPS2997 = {"num": 30000, "den": 1001}


def _clip(**over):
    data = {
        "id": "C1",
        "position": 0.0,
        "start": 0.0,
        "end": 10.0,
        "layer": 1000000,
        "file_id": "F1",
        "reader": {"has_audio": True, "channels": 2, "has_video": True},
    }
    data.update(over)
    return data


def _xs(points):
    return [p["co"]["X"] for p in points]


def _ys(points):
    return [round(p["co"]["Y"], 6) for p in points]


# --------------------------------------------------------------------------
# dB / fps
# --------------------------------------------------------------------------

def test_db_to_gain_round_trips():
    assert abs(am.db_to_gain(-12) - 0.251188) < 1e-5
    assert am.db_to_gain(0) == 1.0
    assert am.db_to_gain(6) > 1.0
    assert abs(am.gain_to_db(am.db_to_gain(-9)) - (-9)) < 1e-9
    # Garbage in is treated as unity, never a crash.
    assert am.db_to_gain("not-a-number") == 1.0


def test_gain_to_db_floors_at_silence():
    assert am.gain_to_db(0.0) == -120.0
    assert am.gain_to_db(-1.0) == -120.0


def test_fps_float_handles_dict_number_and_zero_den():
    assert am.fps_float(FPS30) == 30.0
    assert abs(am.fps_float(FPS2997) - 29.97002997) < 1e-6
    assert am.fps_float(24) == 24.0
    assert am.fps_float({"num": 30, "den": 0}) == 30.0


# --------------------------------------------------------------------------
# Frame conversion
# --------------------------------------------------------------------------

def test_source_frame_range_matches_volume_triggered_formula():
    clip = _clip(start=2.0, end=6.0)
    assert am.clip_source_frame_range(clip, FPS30) == (61, 181)


def test_timeline_to_source_frame_offsets_by_position_and_start():
    # Clip trimmed from 2s of the source, placed at 5s on the timeline.
    clip = _clip(position=5.0, start=2.0, end=8.0)
    # Timeline 5.0s == source 2.0s == frame 61.
    assert am.timeline_to_source_frame(5.0, clip, FPS30) == 61
    # One second later on the timeline is one second later in the source.
    assert am.timeline_to_source_frame(6.0, clip, FPS30) == 91


def test_timeline_to_source_frame_clamps_outside_the_clip():
    clip = _clip(position=5.0, start=2.0, end=8.0)
    first, last = am.clip_source_frame_range(clip, FPS30)
    assert am.timeline_to_source_frame(-100.0, clip, FPS30) == first
    assert am.timeline_to_source_frame(1e6, clip, FPS30) == last


def test_timeline_to_source_frame_non_integer_fps():
    clip = _clip(start=0.0, end=10.0)
    assert am.timeline_to_source_frame(1.0, clip, FPS2997) == 31


def test_clip_timeline_extent_uses_trim_length_not_end():
    clip = _clip(position=12.0, start=4.0, end=9.0)
    assert am.clip_timeline_extent(clip) == (12.0, 17.0)


def test_source_to_timeline_seconds_is_inverse_of_the_trim():
    clip = _clip(position=5.0, start=2.0, end=8.0)
    assert am.source_to_timeline_seconds(2.0, clip) == 5.0
    assert am.source_to_timeline_seconds(3.5, clip) == 6.5


# --------------------------------------------------------------------------
# Window algebra
# --------------------------------------------------------------------------

def test_merge_windows_merges_inside_gap_and_keeps_beyond_it():
    windows = [(0.0, 1.0), (1.5, 2.0), (5.0, 6.0)]
    assert am.merge_windows(windows, gap=1.0) == [(0.0, 2.0), (5.0, 6.0)]
    assert am.merge_windows(windows, gap=0.1) == [(0.0, 1.0), (1.5, 2.0), (5.0, 6.0)]


def test_merge_windows_drops_empty_and_inverted_and_sorts():
    windows = [(4.0, 5.0), (2.0, 2.0), (3.0, 1.0), (0.0, 0.5)]
    assert am.merge_windows(windows) == [(0.0, 0.5), (4.0, 5.0)]


def test_merge_windows_absorbs_a_fully_contained_window():
    assert am.merge_windows([(0.0, 10.0), (2.0, 3.0)]) == [(0.0, 10.0)]


def test_merge_windows_tolerates_junk_entries():
    assert am.merge_windows([(0.0, 1.0), None, (), ("a", "b")]) == [(0.0, 1.0)]


def test_clamp_windows_clips_and_drops():
    windows = [(-5.0, 2.0), (4.0, 6.0), (20.0, 22.0)]
    assert am.clamp_windows(windows, 0.0, 10.0) == [(0.0, 2.0), (4.0, 6.0)]


def test_invert_windows_returns_the_restore_gaps():
    assert am.invert_windows([(2.0, 4.0), (6.0, 8.0)], 0.0, 10.0) == [
        (0.0, 2.0),
        (4.0, 6.0),
        (8.0, 10.0),
    ]
    assert am.invert_windows([(0.0, 10.0)], 0.0, 10.0) == []


# --------------------------------------------------------------------------
# Role detection
# --------------------------------------------------------------------------

_ANALYZED = {"analyzed": True}


def _meta(cues=None, **over):
    meta = {"analyzed": True}
    if cues is not None:
        meta["transcript_cues"] = cues
    meta.update(over)
    return meta


def test_role_talking_head_video_is_speech():
    clip = _clip()
    meta = _meta(cues=[{"start": 0.0, "end": 2.0, "text": "hello"}])
    assert am.classify_clip_audio_role(clip, {"media_type": "video"}, meta) == am.ROLE_SPEECH


def test_role_long_audio_only_with_no_cues_is_music():
    clip = _clip(end=90.0, reader={"has_audio": True, "channels": 2, "has_video": False})
    assert am.classify_clip_audio_role(clip, {"media_type": "audio"}, _ANALYZED) == am.ROLE_MUSIC


def test_role_short_audio_only_is_sfx():
    clip = _clip(end=1.5, reader={"has_audio": True, "channels": 2, "has_video": False})
    assert am.classify_clip_audio_role(clip, {"media_type": "audio"}, _ANALYZED) == am.ROLE_SFX


def test_role_mixed_clip_with_cues_is_speech_never_a_bed():
    """A song with vocals / a talking head over score is a speech source."""
    clip = _clip(end=180.0, reader={"has_audio": True, "channels": 2, "has_video": False})
    meta = _meta(cues=[{"start": 5.0, "end": 9.0, "text": "verse"}], sounds="driving music")
    role = am.classify_clip_audio_role(clip, {"media_type": "audio"}, meta)
    assert role == am.ROLE_SPEECH
    assert role not in am.BED_ROLES


def test_role_silent_when_no_audio_stream_or_no_channels():
    no_audio = _clip(reader={"has_audio": False, "channels": 2})
    no_channels = _clip(reader={"has_audio": True, "channels": 0})
    assert am.classify_clip_audio_role(no_audio, {}, _ANALYZED) == am.ROLE_SILENT
    assert am.classify_clip_audio_role(no_channels, {}, _ANALYZED) == am.ROLE_SILENT


def test_role_unknown_when_source_is_not_indexed():
    assert am.classify_clip_audio_role(_clip(), {"media_type": "video"}, {}) == am.ROLE_UNKNOWN
    assert am.classify_clip_audio_role(_clip(), {"media_type": "video"}, None) == am.ROLE_UNKNOWN


def test_role_video_without_speech_is_ambient():
    assert am.classify_clip_audio_role(_clip(), {"media_type": "video"}, _ANALYZED) == am.ROLE_AMBIENT


def test_role_has_speech_flag_alone_is_enough():
    meta = {"analyzed": True, "has_speech": True, "transcript_cues": []}
    assert am.classify_clip_audio_role(_clip(), {}, meta) == am.ROLE_SPEECH


def test_has_audio_stream_falls_back_to_file_data_when_reader_was_popped():
    clip = _clip()
    clip.pop("reader")
    assert am.has_audio_stream(clip, {"has_audio": True, "channels": 2}) is True
    assert am.has_audio_stream(clip, {"has_audio": False, "channels": 2}) is False


def test_audio_only_detected_from_extension_when_reader_lacks_has_video():
    clip = _clip(end=60.0, reader={"has_audio": True, "channels": 2, "path": "/m/bed.mp3"})
    assert am.classify_clip_audio_role(clip, {}, _ANALYZED) == am.ROLE_MUSIC


def test_cover_art_mp3_is_still_a_music_bed():
    """libopenshot sets has_video=True on MP3s carrying album art. Trusting it
    classified the bed as 'ambient', so ducking silently skipped it."""
    reader = {"has_audio": True, "channels": 2, "has_video": True, "path": "/m/theme.mp3"}
    clip = _clip(end=90.0, reader=reader)
    assert am.classify_clip_audio_role(clip, {}, _ANALYZED) == am.ROLE_MUSIC
    assert am.classify_clip_audio_role(clip, {}, _ANALYZED) in am.BED_ROLES


def test_cover_art_mp3_shorter_than_the_music_threshold_is_sfx():
    reader = {"has_audio": True, "channels": 2, "has_video": True, "path": "/m/sting.mp3"}
    clip = _clip(end=1.5, reader=reader)
    assert am.classify_clip_audio_role(clip, {}, _ANALYZED) == am.ROLE_SFX


def test_media_type_audio_beats_has_video_when_the_path_is_opaque():
    reader = {"has_audio": True, "channels": 2, "has_video": True, "path": "/tmp/blob"}
    clip = _clip(end=90.0, reader=reader)
    file_data = {"media_type": "audio", "path": "/tmp/blob"}
    assert am.classify_clip_audio_role(clip, file_data, _ANALYZED) == am.ROLE_MUSIC


def test_real_video_with_audio_is_still_ambient_not_a_bed():
    reader = {"has_audio": True, "channels": 2, "has_video": True, "path": "/m/broll.mp4"}
    clip = _clip(end=90.0, reader=reader)
    role = am.classify_clip_audio_role(clip, {"media_type": "video"}, _ANALYZED)
    assert role == am.ROLE_AMBIENT
    assert role not in am.BED_ROLES


# --------------------------------------------------------------------------
# Speech windows from cues
# --------------------------------------------------------------------------

def test_cue_windows_rebase_to_timeline_seconds():
    clip = _clip(position=5.0, start=2.0, end=8.0)
    meta = _meta(cues=[{"source_start": 3.0, "source_end": 4.0, "text": "hi"}])
    assert am.speech_windows_from_cues(clip, meta) == [(6.0, 7.0)]


def test_cue_windows_exclude_cues_outside_the_trim_window():
    clip = _clip(position=0.0, start=2.0, end=8.0)
    meta = _meta(
        cues=[
            {"source_start": 0.0, "source_end": 1.0},   # before the trim
            {"source_start": 3.0, "source_end": 4.0},   # inside
            {"source_start": 20.0, "source_end": 21.0}, # after the trim
        ]
    )
    assert am.speech_windows_from_cues(clip, meta) == [(1.0, 2.0)]


def test_cue_windows_clip_a_cue_straddling_the_boundary():
    clip = _clip(position=0.0, start=2.0, end=8.0)
    meta = _meta(cues=[{"source_start": 1.0, "source_end": 3.0}])
    assert am.speech_windows_from_cues(clip, meta) == [(0.0, 1.0)]


def test_cue_windows_accept_clip_local_cues_without_source_fields():
    clip = _clip(position=10.0, start=0.0, end=5.0)
    meta = _meta(cues=[{"start": 1.0, "end": 2.0}])
    assert am.speech_windows_from_cues(clip, meta) == [(11.0, 12.0)]


def test_cue_windows_empty_without_cues():
    assert am.speech_windows_from_cues(_clip(), _ANALYZED) == []
    assert am.speech_windows_from_cues(_clip(), None) == []


def test_cue_windows_drop_zero_length_cues():
    meta = _meta(cues=[{"source_start": 2.0, "source_end": 2.0}])
    assert am.speech_windows_from_cues(_clip(), meta) == []


# --------------------------------------------------------------------------
# Speech windows from waveform energy (secondary signal)
# --------------------------------------------------------------------------

def _energy_clip(samples, **over):
    clip = _clip(**over)
    clip["ui"] = {"audio_data": samples}
    return clip


def test_energy_windows_find_the_loud_run():
    # 10s clip, 10 samples -> 1s each. Loud from sample 3 to 6.
    samples = [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    windows = am.speech_windows_from_energy(_energy_clip(samples))
    assert len(windows) == 1
    assert abs(windows[0][0] - 3.0) < 1e-6
    assert abs(windows[0][1] - 6.0) < 1e-6


def test_energy_windows_empty_without_a_cached_waveform():
    assert am.speech_windows_from_energy(_clip()) == []
    assert am.speech_windows_from_energy(_energy_clip([])) == []
    assert am.speech_windows_from_energy(_energy_clip([0.0] * 10)) == []


def test_energy_windows_drop_runs_shorter_than_min_run():
    samples = [0.0] * 100
    samples[50] = 1.0  # 0.1s at 10s/100 samples
    assert am.speech_windows_from_energy(_energy_clip(samples), min_run=0.25) == []


def test_energy_windows_respect_the_threshold():
    samples = [0.02, 0.02, 1.0, 1.0, 0.02, 0.02]
    loud = am.speech_windows_from_energy(_energy_clip(samples), threshold=0.5)
    assert len(loud) == 1
    everything = am.speech_windows_from_energy(_energy_clip(samples), threshold=0.001)
    assert everything == [(0.0, 10.0)]


def test_refine_windows_trims_dead_lead_and_tail_inside_a_cue():
    # Cue says 0-10s; the waveform is only loud from 3-6s.
    samples = [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    refined = am.refine_windows_with_energy([(0.0, 10.0)], _energy_clip(samples))
    assert len(refined) == 1
    assert abs(refined[0][0] - 3.0) < 1e-6
    assert abs(refined[0][1] - 6.0) < 1e-6


def test_refine_windows_keeps_cues_when_no_waveform_is_cached():
    assert am.refine_windows_with_energy([(1.0, 2.0)], _clip()) == [(1.0, 2.0)]


def test_refine_windows_keeps_a_cue_the_energy_gate_would_erase():
    """Cues stay authoritative - a quiet but real line is never dropped."""
    samples = [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0]
    refined = am.refine_windows_with_energy([(0.0, 2.0)], _energy_clip(samples))
    assert refined == [(0.0, 2.0)]


# --------------------------------------------------------------------------
# Curve evaluation
# --------------------------------------------------------------------------

def test_evaluate_curve_interpolates_clamps_and_defaults():
    points = [am.make_point(1, 0.0), am.make_point(11, 1.0)]
    assert am.evaluate_volume_curve(points, 1) == 0.0
    assert am.evaluate_volume_curve(points, 11) == 1.0
    assert abs(am.evaluate_volume_curve(points, 6) - 0.5) < 1e-9
    assert am.evaluate_volume_curve(points, -50) == 0.0     # before the first point
    assert am.evaluate_volume_curve(points, 5000) == 1.0    # after the last
    assert am.evaluate_volume_curve([], 5, default=0.7) == 0.7


def test_current_static_level_distinguishes_flat_from_automated():
    assert am.current_static_level(_clip()) == 1.0
    flat = _clip(volume={"Points": [am.make_point(1, 0.5)]})
    assert am.current_static_level(flat) == 0.5
    automated = _clip(volume={"Points": [am.make_point(1, 0.0), am.make_point(30, 1.0)]})
    assert am.current_static_level(automated) is None


def test_curve_points_sorts_and_ignores_malformed_entries():
    clip = _clip(volume={"Points": [am.make_point(30, 1.0), "junk", am.make_point(1, 0.0)]})
    assert _xs(am.curve_points(clip)) == [1.0, 30.0]


# --------------------------------------------------------------------------
# Duck envelope
# --------------------------------------------------------------------------

def test_duck_points_are_four_per_window_ascending_and_at_the_right_levels():
    clip = _clip(end=30.0)
    points = am.build_duck_points(
        clip, FPS30, [(10.0, 15.0)], duck_gain=am.db_to_gain(-12)
    )
    xs = _xs(points)
    assert len(points) == 4
    assert xs == sorted(xs) and len(set(xs)) == 4
    ys = _ys(points)
    assert ys[0] == 1.0 and ys[3] == 1.0
    assert abs(ys[1] - 0.251188) < 1e-5
    assert abs(ys[2] - 0.251188) < 1e-5


def test_duck_points_apply_padding_and_ramps():
    clip = _clip(end=30.0)
    points = am.build_duck_points(
        clip, FPS30, [(10.0, 15.0)], duck_gain=0.25,
        attack=0.1, release=0.2, pad_before=0.5, pad_after=0.5,
    )
    xs = _xs(points)
    # duck starts at 10.0-0.5 = 9.5s -> frame 286; ramp begins 0.1s earlier.
    assert xs[1] == 286.0
    assert xs[0] == 283.0
    # duck ends at 15.0+0.5 = 15.5s -> frame 466; restored 0.2s later.
    assert xs[2] == 466.0
    assert xs[3] == 472.0


def test_duck_points_merge_windows_that_are_too_close_to_restore_between():
    clip = _clip(end=30.0)
    close = am.build_duck_points(clip, FPS30, [(10.0, 11.0), (11.3, 12.0)], duck_gain=0.25)
    assert len(close) == 4  # one merged duck, no pumping
    far = am.build_duck_points(clip, FPS30, [(10.0, 11.0), (20.0, 21.0)], duck_gain=0.25)
    assert len(far) == 8


def test_duck_points_empty_when_speech_does_not_overlap_the_clip():
    clip = _clip(position=0.0, end=10.0)
    assert am.build_duck_points(clip, FPS30, [(50.0, 60.0)], duck_gain=0.25) == []
    assert am.build_duck_points(clip, FPS30, [], duck_gain=0.25) == []


def test_duck_points_clamp_inside_the_clip_when_speech_covers_everything():
    clip = _clip(position=0.0, end=10.0)
    points = am.build_duck_points(clip, FPS30, [(-5.0, 50.0)], duck_gain=0.25)
    first, last = am.clip_source_frame_range(clip, FPS30)
    xs = _xs(points)
    assert points and xs == sorted(xs)
    assert min(xs) >= first and max(xs) <= last
    assert all(abs(y - 0.25) < 1e-9 for y in _ys(points))


def test_duck_points_respect_the_clips_position_offset():
    clip = _clip(position=20.0, start=0.0, end=30.0)
    points = am.build_duck_points(
        clip, FPS30, [(25.0, 26.0)], duck_gain=0.25,
        attack=0.0, release=0.0, pad_before=0.0, pad_after=0.0,
    )
    # Timeline 25s on a clip placed at 20s is source 5s -> frame 151.
    assert _xs(points)[1] == 151.0


def test_duck_points_preserve_an_existing_fade_in():
    """A music bed that already fades in keeps its fade; the dip rides on top."""
    clip = _clip(end=30.0, volume={"Points": [am.make_point(1, 0.0), am.make_point(91, 1.0)]})
    points = am.build_duck_points(clip, FPS30, [(10.0, 15.0)], duck_gain=0.5)
    xs, ys = _xs(points), _ys(points)
    assert xs == sorted(xs)
    # The fade's own endpoints survive.
    assert (1.0, 0.0) in list(zip(xs, ys))
    assert (91.0, 1.0) in list(zip(xs, ys))
    # And the duck floor sits at half the base, not half of nothing. Speech runs
    # 10.0-15.0s; with default padding the bed is held down over 9.8-15.3s.
    by_x = dict(zip(xs, ys))
    assert by_x[295.0] == 0.5   # duck floor at 9.8s, base 1.0 halved
    assert by_x[460.0] == 0.5   # still down at 15.3s
    assert by_x[472.0] == 1.0   # restored after the release


def test_duck_points_scale_existing_automation_that_falls_inside_a_duck():
    clip = _clip(
        end=30.0,
        volume={"Points": [am.make_point(1, 1.0), am.make_point(390, 0.8), am.make_point(900, 1.0)]},
    )
    points = am.build_duck_points(
        clip, FPS30, [(10.0, 20.0)], duck_gain=0.5,
        attack=0.0, release=0.0, pad_before=0.0, pad_after=0.0,
    )
    by_x = {p["co"]["X"]: round(p["co"]["Y"], 6) for p in points}
    assert by_x[390.0] == 0.4    # 0.8 base scaled by the duck
    assert by_x[900.0] == 1.0    # outside the duck, untouched


def test_ducked_windows_reports_what_build_duck_points_will_do():
    clip = _clip(end=30.0)
    windows = am.ducked_windows(
        clip, [(10.0, 15.0)], attack=0.1, release=0.2, pad_before=0.5, pad_after=0.5
    )
    assert windows == [(9.5, 15.5)]


def test_duck_with_zero_gain_mutes_the_bed_under_speech():
    clip = _clip(end=30.0)
    points = am.build_duck_points(clip, FPS30, [(10.0, 15.0)], duck_gain=0.0)
    assert _ys(points)[1] == 0.0


# --------------------------------------------------------------------------
# Static level points
# --------------------------------------------------------------------------

def test_static_level_whole_clip_is_one_point():
    points = am.build_static_level_points(_clip(start=2.0, end=8.0), FPS30, 0.4)
    assert len(points) == 1
    assert points[0]["co"] == {"X": 61.0, "Y": 0.4}


def test_static_level_window_is_four_points_with_ramps():
    clip = _clip(end=30.0)
    points = am.build_static_level_points(
        clip, FPS30, 0.3, start_seconds=10.0, end_seconds=15.0, fade=0.2
    )
    xs, ys = _xs(points), _ys(points)
    assert len(points) == 4 and xs == sorted(xs)
    assert ys[0] == 1.0 and ys[3] == 1.0
    assert ys[1] == 0.3 and ys[2] == 0.3


def test_static_level_clamps_to_the_menu_range():
    loud = am.build_static_level_points(_clip(), FPS30, 99.0)
    quiet = am.build_static_level_points(_clip(), FPS30, -3.0)
    assert loud[0]["co"]["Y"] == am.MAX_LEVEL
    assert quiet[0]["co"]["Y"] == 0.0


def test_static_level_scale_mode_multiplies_the_existing_curve():
    clip = _clip(volume={"Points": [am.make_point(1, 0.8)]})
    scaled = am.build_static_level_points(clip, FPS30, 0.5, scale=True)
    assert abs(scaled[0]["co"]["Y"] - 0.4) < 1e-9
    replaced = am.build_static_level_points(clip, FPS30, 0.5, scale=False)
    assert replaced[0]["co"]["Y"] == 0.5


def test_static_level_window_outside_the_clip_writes_nothing():
    clip = _clip(position=0.0, end=10.0)
    assert am.build_static_level_points(
        clip, FPS30, 0.5, start_seconds=50.0, end_seconds=60.0
    ) == []


def test_make_point_rounds_x_to_a_whole_frame_and_floors_y():
    p = am.make_point(12.6, -0.5)
    assert p["co"]["X"] == 13.0
    assert p["co"]["Y"] == 0.0
    assert p["interpolation"] == am.LINEAR


# --------------------------------------------------------------------------
# Auto ducking: the amount must depend on the material
# --------------------------------------------------------------------------

def test_two_unity_clips_get_the_classic_duck():
    assert am.auto_duck_db(1.0, 1.0) == pytest.approx(am.AUTO_DUCK_HEADROOM_DB)


def test_an_already_quiet_bed_is_barely_touched():
    """A bed sitting 12 dB down already has the headroom; do not cut it again."""
    quiet = am.db_to_gain(-12.0)
    assert am.auto_duck_db(quiet, 1.0) == pytest.approx(am.AUTO_DUCK_MAX_DB)


def test_a_bed_slamming_over_the_voice_is_cut_hard():
    loud_bed = am.db_to_gain(2.0)
    quiet_speech = am.db_to_gain(-6.0)
    duck = am.auto_duck_db(loud_bed, quiet_speech)
    assert duck < am.AUTO_DUCK_HEADROOM_DB
    assert duck == pytest.approx(-20.0)


def test_a_loud_bed_and_a_quiet_bed_do_not_get_the_same_duck():
    loud = am.auto_duck_db(1.2, 1.0)
    quiet = am.auto_duck_db(0.3, 1.0)
    assert loud != quiet
    assert loud < quiet


def test_auto_duck_is_clamped_to_a_usable_range():
    assert am.auto_duck_db(100.0, 0.001) == pytest.approx(am.AUTO_DUCK_MIN_DB)
    assert am.auto_duck_db(0.0001, 1.0) == pytest.approx(am.AUTO_DUCK_MAX_DB)


def test_auto_duck_treats_an_unknown_level_as_unity():
    assert am.auto_duck_db(None, None) == pytest.approx(am.AUTO_DUCK_HEADROOM_DB)
    assert am.auto_duck_db(0.0, 0.0) == pytest.approx(am.AUTO_DUCK_HEADROOM_DB)


def test_boosting_the_speech_lets_the_bed_off_more_lightly():
    with_boost = am.auto_duck_db(1.0, 1.3)
    without = am.auto_duck_db(1.0, 1.0)
    assert with_boost > without
