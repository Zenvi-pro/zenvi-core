"""Zenvi -> FCP7 XML tuned for Premiere Pro (classes.exporters.final_cut_pro), headless.

Snapshots come from project dicts (premiere_fakes.project), so every case runs
the real TimelineSnapshot / geometry code without Qt or libopenshot. Expected
values follow Premiere's own exports (see the module docstring): retimed
in/out, -1 transition edges, centre in source pixels, pproColor markers.
Set ZENVI_UPDATE_GOLDEN=1 to rewrite tests/fixtures/premiere/export_golden.xml.
"""

import ast
import os
import pathlib
import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest

from classes.exporters import final_cut_pro as fcp
from premiere_fakes import (BEZIER, L1, L2, L3, FakeStills, audio_file, clip, const, fade, image_file, items, keys, kf,
                            param, parse, project, snapshot, tracks, video_file)

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "premiere" / "export_golden.xml"


def build(proj, path="/exports/Edit.xml", **kw):
    kw.setdefault("sequence_uuid", "00000000-0000-4000-8000-000000000001")
    return fcp.build_xmeml(snapshot(proj), path, **kw)


def clipitems(result, kind="video"):
    return [el for t in tracks(result.root, kind) for el in t.findall("clipitem")]


def by_name(result, name, kind="video"):
    found = [el for el in clipitems(result, kind) if el.findtext("name") == name]
    assert found, f"no {kind} clipitem {name!r}"
    return found[0]


def ints(el, *tags):
    return tuple(int(el.findtext(t)) for t in tags)


V = video_file("F1", "/media/My Clip.mp4", duration=20.0)
V4K = video_file("F2", "/media/drone 4k.mov", width=3840, height=2160, duration=20.0)


# --- conventions ------------------------------------------------------------------------------------

@pytest.mark.parametrize("fps, timebase, ntsc, counted", [
    (Fraction(30000, 1001), 30, True, Fraction(30000, 1001)),
    (Fraction(24000, 1001), 24, True, Fraction(24000, 1001)),
    (Fraction(60000, 1001), 60, True, Fraction(60000, 1001)),
    (Fraction(25), 25, False, Fraction(25)),
    (Fraction(30), 30, False, Fraction(30)),
    (Fraction(25, 2), 25, False, Fraction(25)),     # 12.5 fps: two xmeml frames per project frame
])
def test_rate_is_a_whole_timebase_with_ntsc(fps, timebase, ntsc, counted):
    rate = fcp.rate_for(fps)
    assert (rate.timebase, rate.ntsc, rate.fps) == (timebase, ntsc, counted)


def test_ntsc_sequence_counts_frames_at_the_true_rate():
    proj = project(fps=(30000, 1001), files=[V], clips=[clip("A", "F1", position=10.010, start=1.001, end=3.003)])
    result = build(proj)
    seq = result.root.find("sequence")
    assert (seq.findtext("rate/timebase"), seq.findtext("rate/ntsc")) == ("30", "TRUE")
    assert seq.findtext("timecode/displayformat") == "DF" and seq.findtext("timecode/string") == "00;00;00;00"
    a = by_name(result, "A")
    assert ints(a, "start", "end", "in", "out") == (300, 360, 30, 90)


@pytest.mark.parametrize("path, url", [
    ("/media/My Clip.mp4", "file://localhost/media/My%20Clip.mp4"),
    ("/Users/me/Vidéos/plage été.mov", "file://localhost/Users/me/Vid%C3%A9os/plage%20%C3%A9t%C3%A9.mov"),
    ("C:\\Users\\me\\My Clip.mp4", "file://localhost/C%3a/Users/me/My%20Clip.mp4"),
    ("D:/media/a#1.mov", "file://localhost/D%3a/media/a%231.mov"),
    ("\\\\server\\share\\cut 1.mov", "file://server/share/cut%201.mov"),
])
def test_pathurl_is_premiere_style_and_reads_back(path, url):
    from classes.importers.final_cut_pro import path_from_pathurl
    assert fcp.pathurl(path) == url
    back = path_from_pathurl(url)
    assert back.replace("\\", "/") == path.replace("\\", "/").replace("//server", "//server")


def test_module_imports_without_qt_or_libopenshot():
    """The stub has no openshot constants: nothing at module level may need them (or Qt)."""
    for rel in ("exporters/final_cut_pro.py", "importers/final_cut_pro.py"):
        source = (pathlib.Path(fcp.__file__).parents[1] / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = {a.name for n in top for a in n.names} | {getattr(n, "module", "") or "" for n in top}
        assert "openshot" not in names and "qt_api" not in names, rel
        module_level = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        used = {node.id for stmt in module_level for node in ast.walk(stmt) if isinstance(node, ast.Name)}
        assert "openshot" not in used, rel     # the old exporter's module-level openshot.LINEAR broke the stub


# --- timing and keyframes ----------------------------------------------------------------------------

def test_trimmed_clip_keyframes_are_in_media_frames():
    """when lives in the in/out space: a clip trimmed to start at 2 s keys at 60.. (not raw X, not clamped)."""
    proj = project(files=[V], clips=[clip("A", "F1", position=1.0, start=2.0, end=7.0,
                                          alpha=kf([(61, 0.0), (91, 1.0)]))])
    a = by_name(build(proj), "A")
    assert ints(a, "start", "end", "in", "out", "duration") == (30, 180, 60, 210, 600)
    assert keys(param(a, "opacity", "opacity")) == [(60, 0.0), (90, 100.0)]


def test_eased_keyframes_are_baked_into_linear_keys_within_tolerance():
    proj = project(files=[V], clips=[clip("A", "F1", start=0.0, end=4.0,
                                          alpha=kf([(1, 0.0, BEZIER), (46, 1.0, BEZIER)]),
                                          location_x=kf([(1, -0.2, BEZIER), (91, 0.2, BEZIER)]))])
    snap = snapshot(proj)
    result = fcp.build_xmeml(snap, "/x/Edit.xml")
    a = by_name(result, "A")
    c = snap.clip("A")
    op = keys(param(a, "opacity", "opacity"))
    assert 2 < len(op) < 46, op            # baked, but not one key per frame
    for k in range(0, 120):
        before = max((w, v) for w, v in op if w <= k) if any(w <= k for w, _ in op) else op[0]
        after = min(((w, v) for w, v in op if w >= k), default=op[-1])
        f = 0.0 if after[0] == before[0] else (k - before[0]) / (after[0] - before[0])
        value = before[1] + (after[1] - before[1]) * f
        assert abs(value - 100.0 * c.curve("alpha").value_at(k / 30.0)) <= fcp.TOL_OPACITY + 1e-6
    center = keys(param(a, "basic", "center"), point=True)
    assert center[0][0] == 0 and len(center) > 2


def test_static_clip_writes_no_keyframes_and_identity_has_no_motion():
    proj = project(files=[V], clips=[clip("A", "F1", end=3.0, alpha=const(0.5))])
    a = by_name(build(proj), "A")
    assert param(a, "basic", "scale") is None                     # 1080p in 1080p, centred: no Basic Motion
    opacity = param(a, "opacity", "opacity")
    assert opacity.findtext("value") == "50" and not opacity.findall("keyframe")


# --- motion -----------------------------------------------------------------------------------------------

def test_center_is_normalised_by_the_source_media_size():
    proj = project(files=[V4K, image_file("F3", "/media/photo.jpg")], clips=[
        clip("Drone", "F2", end=3.0, location_x=const(0.25)),
        clip("Photo", "F3", layer=L2, end=3.0, location_x=const(-0.25))])
    result = build(proj)
    drone = by_name(result, "Drone")
    assert param(drone, "basic", "scale").findtext("value") == "50"
    assert param(drone, "basic", "center").findtext("value/horiz") == "0.125"     # 480 px / 3840
    photo = by_name(result, "Photo")
    assert param(photo, "basic", "scale").findtext("value") == "108"             # FIT 1000 -> 1080
    assert param(photo, "basic", "center").findtext("value/horiz") == "-0.48"    # -480 px / 1000


def test_anchor_point_and_rotation():
    proj = project(files=[V], clips=[clip("A", "F1", end=3.0, origin_x=const(0.0), origin_y=const(0.0),
                                          rotation=const(30.0))])
    a = by_name(build(proj), "A")
    assert param(a, "basic", "rotation").findtext("value") == "30"
    assert (param(a, "basic", "centerOffset").findtext("value/horiz"),
            param(a, "basic", "centerOffset").findtext("value/vert")) == ("-0.5", "-0.5")
    assert (param(a, "basic", "center").findtext("value/horiz"),
            param(a, "basic", "center").findtext("value/vert")) == ("0", "0")


def test_non_uniform_scale_becomes_distort_aspect():
    proj = project(files=[V], clips=[clip("A", "F1", end=3.0, scale_x=const(0.5), scale_y=const(1.0))])
    result = build(proj)
    a = by_name(result, "A")
    assert param(a, "basic", "scale") is None          # height 100 %, centred: Basic Motion is identity
    assert param(a, "deformation", "aspect").findtext("value") == "50"
    tall = by_name(build(project(files=[V], clips=[clip("A", "F1", end=3.0, scale_x=const(1.0),
                                                        scale_y=const(0.5))])), "A")
    assert param(tall, "basic", "scale").findtext("value") == "50"
    assert param(tall, "deformation", "aspect").findtext("value") == "-100"
    assert any("unevenly" in w for w in result.warnings)


# --- transitions and lanes ---------------------------------------------------------------------------

def crossfade(audio_hint=False, mask="fade"):
    f1 = video_file("F1", "/media/a.mp4", duration=20.0)
    f2 = video_file("F2", "/media/b.mp4", duration=20.0)
    t = fade("X1", L1, 4.0, 1.0, mask=mask)
    if audio_hint:
        t["fade_audio_hint"] = True
    return project(files=[f1, f2], clips=[clip("A", "F1", position=0.0, end=5.0),
                                         clip("B", "F2", position=4.0, end=6.0)], transitions=[t])


def test_crossfade_becomes_a_centred_dissolve_with_minus_one_edges():
    result = build(crossfade())
    (track,) = tracks(result.root, "video")
    a, tr, b = items(track)
    assert ints(a, "start", "end", "in", "out") == (0, -1, 0, 150)      # A's media runs to the dissolve's end
    assert (tr.tag, ints(tr, "start", "end"), tr.findtext("alignment")) == ("transitionitem", (120, 150), "center")
    assert tr.findtext("effect/name") == "Cross Dissolve" and tr.findtext("effect/effectcategory") == "Dissolve"
    assert int(tr.findtext("cutPointTicks")) == 15 * fcp.TICKS_PER_SECOND // 30
    assert ints(b, "start", "end", "in", "out") == (-1, 300, 0, 180)    # B's media starts at the dissolve
    assert fcp.validate_xmeml(result.root) == []


def test_wipes_become_dissolves_with_a_warning():
    result = build(crossfade(mask="wipe_left_to_right"))
    assert tracks(result.root, "video")[0].find("transitionitem") is not None
    assert any("wipe" in w for w in result.warnings)


def test_audio_cross_fade_follows_the_transitions_fade_audio_hint():
    hinted = build(crossfade(audio_hint=True))
    a_tracks = tracks(hinted.root, "audio")
    assert len(a_tracks) == 2                                         # one stereo pair, A and B on it
    tr = a_tracks[0].find("transitionitem")
    assert tr.findtext("effect/name") == "Cross Fade (+3dB)"
    assert tr.findtext("effect/effectid") == "KGAudioTransCrossFade3dB" and tr.findtext("effect/mediatype") == "audio"
    plain = build(crossfade(audio_hint=False))
    lanes = tracks(plain.root, "audio")
    assert len(lanes) == 4 and not any(t.find("transitionitem") is not None for t in lanes)
    assert [c.findtext("name") for c in lanes[0].findall("clipitem")] == ["A"]   # both play at full volume
    assert [c.findtext("name") for c in lanes[2].findall("clipitem")] == ["B"]
    assert ints(lanes[0].find("clipitem"), "start", "end") == (0, 150)


def test_fade_in_and_out_become_black_aligned_dissolves():
    f = video_file("F1", "/media/a.mp4", duration=20.0, has_audio=False)
    proj = project(files=[f], clips=[clip("C", "F1", position=1.0, end=3.0)],
                   transitions=[fade("I", L1, 1.0, 0.5), fade("O", L1, 3.5, 0.5, reverse=True)])
    result = build(proj)
    first, c, last = items(tracks(result.root, "video")[0])
    assert (ints(first, "start", "end"), first.findtext("alignment")) == ((30, 45), "start-black")
    assert ints(c, "start", "end", "in", "out") == (-1, -1, 0, 90)
    assert (ints(last, "start", "end"), last.findtext("alignment")) == ((105, 120), "end-black")
    assert fcp.validate_xmeml(result.root) == []


def test_a_fade_covering_a_whole_clip_keeps_its_direction():
    # review C3-1 #4: a 1 s fade-out over a 1 s clip used to become a fade-in ("fades the wrong way")
    f = video_file("F1", "/media/a.mp4", duration=20.0, has_audio=False)
    for reverse, alignment in ((True, "end-black"), (False, "start-black")):
        proj = project(files=[f], clips=[clip("A", "F1", position=2.0, end=1.0)],
                       transitions=[fade("T", L1, 2.0, 1.0, reverse=reverse)])
        result = build(proj)
        track_items = items(tracks(result.root, "video")[0])
        (tr,) = [el for el in track_items if el.tag == "transitionitem"]
        assert (ints(tr, "start", "end"), tr.findtext("alignment")) == ((60, 90), alignment)
        assert not any("wrong way" in w for w in result.warnings)
        assert fcp.validate_xmeml(result.root) == []


def test_overlapping_clips_without_a_transition_stack_on_extra_lanes():
    proj = crossfade()
    proj["effects"] = []
    proj["layers"][0]["label"] = "Main"
    result = build(proj)
    v = tracks(result.root, "video")
    assert [t.get("MZ.TrackName") for t in v] == ["Main", "Main (2)"]
    assert [c.findtext("name") for c in v[1].findall("clipitem")] == ["B"]
    assert ints(v[0].find("clipitem"), "start", "end") == (0, 150)


def test_a_fade_in_the_middle_of_a_clip_becomes_opacity_keys():
    f = video_file("F1", "/media/a.mp4", duration=20.0, has_audio=False)
    proj = project(files=[f], clips=[clip("C", "F1", end=6.0)], transitions=[fade("M", L1, 2.0, 1.0)])
    result = build(proj)
    c = by_name(result, "C")
    assert tracks(result.root, "video")[0].find("transitionitem") is None
    op = keys(param(c, "opacity", "opacity"))
    assert op[0] == (59, 100.0) and (60, 0.0) in op and op[-1][1] == 100.0


# --- speed -------------------------------------------------------------------------------------------------

def test_constant_speed_counts_in_out_in_retimed_frames():
    proj = project(files=[V], clips=[clip("S", "F1", end=2.5, time=kf([(1, 1), (76, 150)]))])
    result = build(proj)
    s = by_name(result, "S")
    assert ints(s, "start", "end", "in", "out", "duration") == (0, 75, 0, 75, 300)
    assert param(s, "timeremap", "speed").findtext("value") == "200"
    assert param(s, "timeremap", "reverse").findtext("value") == "FALSE"
    graph = param(s, "timeremap", "graphdict")
    assert [(int(k.findtext("when")), int(k.findtext("value"))) for k in graph.findall("keyframe")] == [
        (0, 0), (75, 150), (300, 600)]
    audio = by_name(result, "S", "audio")
    remap = [e for e in audio.iter("effect") if e.findtext("effectid") == "timeremap"][0]
    assert remap.find("mediatype") is None and param(audio, "timeremap", "graphdict") is None
    assert param(audio, "timeremap", "speed").findtext("value") == "200"


def test_reverse_maps_media_time_from_the_end():
    proj = project(files=[V], clips=[clip("R", "F1", start=1.0, end=3.0, time=kf([(31, 90), (91, 31)]))])
    r = by_name(build(proj), "R")
    assert ints(r, "in", "out", "duration") == (510, 570, 600)
    assert param(r, "timeremap", "reverse").findtext("value") == "TRUE"
    graph = param(r, "timeremap", "graphdict")
    pts = [(int(k.findtext("when")), int(k.findtext("value"))) for k in graph.findall("keyframe")]
    assert pts == [(0, 600), (510, 90), (570, 30), (600, 0)]


def test_a_freeze_frame_becomes_variable_time_remapping_without_audio():
    proj = project(files=[V], clips=[clip("Z", "F1", end=2.0, time=kf([(1, 30), (61, 30)]))])
    result = build(proj)
    z = by_name(result, "Z")
    assert param(z, "timeremap", "variablespeed").findtext("value") == "1"
    graph = [(int(k.findtext("when")), int(k.findtext("value")))
             for k in param(z, "timeremap", "graphdict").findall("keyframe")]
    assert ints(z, "in", "out") == (29, 89) and (29, 29) in graph and (89, 29) in graph
    assert not clipitems(result, "audio")
    assert any("audio was left out" in w for w in result.warnings)


# --- markers, tracks, audio --------------------------------------------------------------------------------

def test_markers_keep_names_and_premiere_colours():
    proj = project(files=[V], clips=[clip("A", "F1", end=10.0)], markers=[
        {"id": "M1", "position": 3.0, "name": "Beat", "vector": "red"},
        {"id": "M2", "position": 5.0, "name": "Green one", "vector": "green"},
        {"id": "M3", "position": 6.0, "name": "", "icon": "pink.png"}])
    seq = build(proj).root.find("sequence")
    markers = seq.findall("marker")
    assert [(m.findtext("name"), m.findtext("in"), m.findtext("out"), m.findtext("pproColor")) for m in markers] == [
        ("Beat", "90", "-1", "4281740498"), ("Green one", "150", "-1", None), ("", "180", "-1", "4289825711")]


def test_tracks_carry_names_locks_and_exploded_stereo_with_links():
    proj = project(files=[V, audio_file("F5", "/media/vo.wav", channels=1)],
                   clips=[clip("A", "F1", end=5.0), clip("VO", "F5", layer=L2, end=4.0)],
                   layers=[{"id": "L1", "number": L1, "label": "Main", "lock": True, "y": 0},
                           {"id": "L2", "number": L2, "label": "Voice", "lock": False, "y": 0}])
    result = build(proj)
    (video,) = tracks(result.root, "video")
    assert video.get("MZ.TrackName") == "Main" and video.findtext("locked") == "TRUE"
    left, right, mono = tracks(result.root, "audio")
    assert (left.get("currentExplodedTrackIndex"), left.get("totalExplodedTrackCount"),
            left.get("premiereTrackType"), left.findtext("outputchannelindex")) == ("0", "2", "Stereo", "1")
    assert right.get("currentExplodedTrackIndex") == "1" and right.findtext("outputchannelindex") == "2"
    assert (mono.get("totalExplodedTrackCount"), mono.get("premiereTrackType"), mono.get("MZ.TrackName")) == (
        "1", "Mono", "Voice")
    assert mono.find("outputchannelindex") is None
    a_l, a_r = left.find("clipitem"), right.find("clipitem")
    assert (a_l.findtext("sourcetrack/trackindex"), a_r.findtext("sourcetrack/trackindex")) == ("1", "2")
    assert a_l.get("premiereChannelType") == "stereo"
    v = video.find("clipitem")
    links = [(lk.findtext("linkclipref"), lk.findtext("mediatype"), lk.findtext("trackindex"),
              lk.findtext("clipindex")) for lk in v.findall("link")]
    assert links == [(v.get("id"), "video", "1", "1"), (a_l.get("id"), "audio", "1", "1"),
                     (a_r.get("id"), "audio", "2", "1")]
    assert [lk.findtext("linkclipref") for lk in a_r.findall("link")] == [x[0] for x in links]
    assert mono.find("clipitem").find("link") is None                  # audio-only: nothing to link
    assert v.find("file").findtext("pathurl") and a_l.find("file").find("pathurl") is None   # defined once


def _audio_layout(result):
    return [(t.get("premiereTrackType"), t.get("currentExplodedTrackIndex"),
             [(c.findtext("name"), c.get("premiereChannelType"), c.findtext("sourcetrack/trackindex"))
              for c in t.findall("clipitem")]) for t in tracks(result.root, "audio")]


def test_mono_media_on_a_track_with_stereo_media_gets_its_own_mono_track():
    # review C3-1 #2: a mono voice-over in a lane with stereo music was written onto the stereo pair
    # (sourcetrack 2 of a one-channel file), so Premiere played it on one side only
    vo = audio_file("F1", "/media/vo mono.wav", duration=20.0, channels=1)
    music = audio_file("F2", "/media/music.wav", duration=20.0, channels=2)
    proj = project(files=[vo, music], clips=[clip("VO", "F1", end=5.0), clip("Music", "F2", position=6.0, end=5.0)])
    result = build(proj)
    assert _audio_layout(result) == [
        ("Stereo", "0", [("Music", "stereo", "1")]), ("Stereo", "1", [("Music", "stereo", "2")]),
        ("Mono", "0", [("VO", "mono", "1")])]
    assert fcp.validate_xmeml(result.root) == []


def test_a_clip_playing_one_channel_is_a_mono_item_of_that_channel():
    # review C3-1 #3: Separate Audio > each channel (channel_filter) exported every channel clip as full stereo
    stereo = video_file("F1", "/media/interview.mov", duration=20.0)
    only = {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": 2}]}
    video_off = {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": 2}]}
    proj = project(files=[stereo], clips=[
        clip("Picture", "F1", end=5.0, has_audio=video_off),
        clip("Lav (channel 1)", "F1", layer=L2, end=5.0, has_video=video_off, channel_filter=only),
        clip("Camera (channel 2)", "F1", layer=L3, end=5.0, has_video=video_off,
             channel_filter={"Points": [{"co": {"X": 1.0, "Y": 1.0}, "interpolation": 2}]}),
        clip("All", "F1", layer=L3, position=6.0, end=2.0, has_video=video_off,
             channel_filter={"Points": [{"co": {"X": 1.0, "Y": -1.0}, "interpolation": 2}]})])
    result = build(proj)
    assert _audio_layout(result) == [
        ("Mono", "0", [("Lav (channel 1)", "mono", "1")]),
        ("Stereo", "0", [("All", "stereo", "1")]), ("Stereo", "1", [("All", "stereo", "2")]),
        ("Mono", "0", [("Camera (channel 2)", "mono", "2")])]
    assert fcp.validate_xmeml(result.root) == []


def test_a_channel_filter_that_changes_during_the_clip_is_reported():
    stereo = audio_file("F1", "/media/stereo.wav", duration=20.0)
    switching = {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": 2},
                            {"co": {"X": 60.0, "Y": 1.0}, "interpolation": 2}]}
    result = build(project(files=[stereo], clips=[clip("Switch", "F1", end=5.0, channel_filter=switching)]))
    assert _audio_layout(result) == [("Mono", "0", [("Switch", "mono", "1")])]
    assert any("switches audio channels" in w for w in result.warnings)


def test_clip_audio_and_video_switches_choose_the_items():
    off = {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": 2}]}
    proj = project(files=[V], clips=[clip("Mute", "F1", end=3.0, has_audio=off),
                                     clip("Sound", "F1", layer=L2, end=3.0, has_video=off)])
    result = build(proj)
    assert [c.findtext("name") for c in clipitems(result, "video")] == ["Mute"]
    assert {c.findtext("name") for c in clipitems(result, "audio")} == {"Sound"}


def test_audio_levels_are_linear_gain_capped_at_plus_12_db():
    proj = project(files=[V], clips=[clip("Half", "F1", end=3.0, volume=const(0.5)),
                                     clip("Loud", "F1", layer=L2, end=3.0, volume=const(6.0))])
    result = build(proj)
    assert param(by_name(result, "Half", "audio"), "audiolevels", "level").findtext("value") == "0.5"
    assert param(by_name(result, "Loud", "audio"), "audiolevels", "level").findtext("value") == "3.98109"
    assert any("+12 dB" in w for w in result.warnings)


def test_unmapped_effects_shear_and_parents_are_reported():
    blur = {"id": "E1", "class_name": "Blur", "name": "Blur", "type": "Blur", "horizontal_radius": const(3.0)}
    proj = project(files=[V], clips=[clip("A", "F1", end=3.0, effects=[blur], shear_x=const(0.2),
                                          parentObjectId="P1")])
    warnings = " | ".join(build(proj).warnings)
    assert "'Blur' has no Premiere equivalent" in warnings and "shear" in warnings and "parent clip" in warnings


def test_crop_maps_to_fcp_crop_in_percent():
    crop = {"id": "E2", "class_name": "Crop", "name": "Crop", "type": "Crop", "left": const(0.1), "right": const(0.0),
            "top": const(0.25), "bottom": const(0.0), "x": const(0.0), "y": const(0.0), "resize": False}
    a = by_name(build(project(files=[V], clips=[clip("A", "F1", end=3.0, effects=[crop])])), "A")
    assert param(a, "crop", "left").findtext("value") == "10" and param(a, "crop", "top").findtext("value") == "25"


# --- files: titles, collect, staging -----------------------------------------------------------------------

def title_file(fid="T1", path="/titles/Lower third.svg"):
    return {"id": fid, "path": path, "name": os.path.basename(path), "media_type": "image", "has_video": True,
            "has_audio": False, "width": 1920, "height": 1080, "duration": 3600.0, "has_single_image": True}


def test_titles_are_rendered_to_png_stills_next_to_the_xml(tmp_path):
    proj = project(files=[V, title_file()], clips=[clip("A", "F1", end=6.0),
                                                   clip("Lower", "T1", layer=L2, position=1.0, end=4.0)])
    stills = FakeStills()
    out = fcp.export_timeline(snapshot(proj), str(tmp_path / "Edit.xml"), render_stills=stills)
    png = tmp_path / "Edit_media" / "titles" / "Lower third-T1.png"
    assert out.stills == [str(png)] and png.is_file() and out.media_dir == str(tmp_path / "Edit_media")
    assert [(j.width, j.height, j.kind) for j in stills.jobs] == [(1920, 1080, "svg")]
    root = parse(out.path)
    lower = [c for c in root.iter("clipitem") if c.findtext("name") == "Lower"][0]
    assert lower.findtext("file/pathurl") == fcp.pathurl(str(png))
    assert lower.findtext("alphatype") == "straight" and ints(lower, "in", "out") == (0, 120)
    assert int(lower.findtext("file/duration")) >= 180
    assert fcp.validate_xmeml(root) == []


def test_stills_written_as_png_drop_their_old_extension_from_the_clip_name(tmp_path):
    # Zenvi names a title clip after its file ("Lower third.svg"); Premiere links to the PNG, so the clip
    # must not say .svg. Stills Premiere reads as they are keep their names.
    files = [V, title_file(), image_file("W1", "/stills/photo.webp"), image_file("P1", "/stills/logo.png")]
    proj = project(files=files, clips=[clip("A", "F1", end=6.0),
                                       clip("T", "T1", layer=L2, end=2.0, title="Lower third.svg"),
                                       clip("W", "W1", layer=L3, end=2.0, title="photo.webp"),
                                       clip("P", "P1", layer=L2, position=3.0, end=2.0, title="logo.png")])
    out = fcp.export_timeline(snapshot(proj), str(tmp_path / "Edit.xml"), render_stills=FakeStills())
    names = {c.findtext("name"): c.findtext("file/name") for c in parse(out.path).iter("clipitem")
             if c.findtext("file/name")}
    assert names["Lower third"] == "Lower third-T1.png" and names["photo"] == "photo-W1.png"
    assert names["logo.png"] == "logo.png" and "Lower third.svg" not in names


def test_titles_shown_bigger_than_their_svg_are_rendered_at_that_size(tmp_path):
    # review C3-1: a title scaled up in Zenvi (or a 1080p SVG in a 4K project) became a 1x PNG that
    # Premiere enlarged; it is now rendered at the size it is shown and Basic Motion scales it less
    from classes.handoff.transform import clip_geometry
    big = clip("Big", "T1", layer=L2, end=3.0, scale_x=kf([(1, 1.0), (90, 2.0)]), scale_y=kf([(1, 1.0), (90, 2.0)]))
    proj = project(files=[V, title_file()], clips=[clip("A", "F1", end=3.0), big])
    stills = FakeStills()
    out = fcp.export_timeline(snapshot(proj), str(tmp_path / "Edit.xml"), render_stills=stills)
    assert [(j.width, j.height) for j in stills.jobs] == [(3840, 2160)]
    item = [c for c in parse(out.path).iter("clipitem") if c.findtext("name") == "Big"][0]
    assert (item.findtext("file/media/video/samplecharacteristics/width"),
            item.findtext("file/media/video/samplecharacteristics/height")) == ("3840", "2160")
    scale = [v for _w, v in keys(param(item, "basic", "scale"))]
    assert scale[0] == pytest.approx(50.0) and scale[-1] == pytest.approx(100.0)
    # the same picture: 50 % of the 3840 px PNG is the 1920 px Zenvi showed at scale 1
    snap = snapshot(proj)
    g = clip_geometry([c for c in snap.clips if c.title == "Big"][0], 0.0, 1920, 1080)
    assert g.width == pytest.approx(3840 * scale[0] / 100.0)
    small = project(files=[V, title_file()], clips=[clip("A", "F1", end=3.0), clip("T", "T1", layer=L2, end=2.0)])
    stills = FakeStills()
    fcp.export_timeline(snapshot(small), str(tmp_path / "Small.xml"), render_stills=stills)
    assert [(j.width, j.height) for j in stills.jobs] == [(1920, 1080)]      # shown at 1x: rendered at 1x


def test_videos_with_an_alpha_channel_keep_it(tmp_path, monkeypatch):
    # review C3-1: ProRes 4444 / Animation / VP9 overlays were written alphatype none (Premiere drops alpha)
    folder = tmp_path / "media"
    folder.mkdir()
    from premiere_fakes import media_on_disk
    overlay, plate = media_on_disk(folder, dict(video_file("F1", "/m/lower third.mov", duration=5.0), vcodec="prores"),
                                   dict(video_file("F2", "/m/plate.mp4", duration=5.0), vcodec="h264"))
    asked = []

    def probe(path):
        asked.append(os.path.basename(path))
        return True

    monkeypatch.setattr(fcp, "has_alpha_channel", probe)
    proj = project(files=[overlay, plate], clips=[clip("Plate", "F2", end=3.0), clip("Overlay", "F1", layer=L2,
                                                                                  end=3.0)])
    out = fcp.export_timeline(snapshot(proj), str(tmp_path / "Edit.xml"), render_stills=FakeStills())
    alphas = {c.findtext("name"): c.findtext("alphatype") for c in parse(out.path).iter("clipitem")
              if c.findtext("alphatype")}
    assert alphas == {"Plate": "none", "Overlay": "straight"} and asked == ["lower third.mov"]

    def broken(path):
        raise fcp.ExportError("ffprobe was not found")

    monkeypatch.setattr(fcp, "has_alpha_channel", broken)
    out = fcp.export_timeline(snapshot(proj), str(tmp_path / "Edit2.xml"), render_stills=FakeStills())
    assert any("could not check 'lower third.mov' for transparency" in w for w in out.warnings)


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None or __import__("shutil").which("ffprobe") is None,
                    reason="needs ffmpeg and ffprobe")
def test_has_alpha_channel_reads_real_files(tmp_path):
    import subprocess
    alpha, opaque = tmp_path / "alpha.mov", tmp_path / "opaque.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red@0.5:s=64x64:d=0.2,format=rgba",
                    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(alpha)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=0.2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(opaque)], check=True)
    assert fcp.has_alpha_channel(str(alpha)) is True
    assert fcp.has_alpha_channel(str(opaque)) is False


def test_collect_media_copies_files_and_points_the_xml_at_them(tmp_path):
    media = tmp_path / "src"
    media.mkdir()
    src = media / "clip one.mp4"
    src.write_bytes(b"movie")
    f = video_file("F1", str(src), duration=10.0)
    out = fcp.export_timeline(snapshot(project(files=[f], clips=[clip("A", "F1", end=3.0)])),
                              str(tmp_path / "out" / "Edit.xml"), collect_media=True, render_stills=FakeStills())
    copy = tmp_path / "out" / "Edit_media" / "clip one.mp4"
    assert out.copied == [str(copy)] and copy.read_bytes() == b"movie"
    assert parse(out.path).find(".//file/pathurl").text == fcp.pathurl(str(copy))


def test_export_is_staged_and_a_failure_leaves_the_old_file(tmp_path):
    target = tmp_path / "Edit.xml"
    target.write_text("old")
    proj = project(files=[V, title_file()], clips=[clip("A", "F1", end=3.0), clip("T", "T1", layer=L2, end=2.0)])

    def broken(stills):
        raise fcp.ExportError("the title is not a valid SVG")

    with pytest.raises(fcp.ExportError):
        fcp.export_timeline(snapshot(proj), str(target), render_stills=broken)
    assert target.read_text() == "old" and not list(tmp_path.glob("*.partial"))
    out = fcp.export_timeline(snapshot(proj), str(target), render_stills=FakeStills())
    assert out.replaced and target.read_text().startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>')
    assert not list(tmp_path.rglob("*.partial"))


def test_empty_timeline_is_refused():
    with pytest.raises(fcp.ExportError, match="no clips"):
        fcp.export_timeline(snapshot(project()), "/tmp/never.xml")


# --- validation --------------------------------------------------------------------------------------------

def test_validate_xmeml_catches_broken_structure():
    result = build(crossfade())
    root = result.root
    assert fcp.validate_xmeml(root) == []
    track = tracks(root, "video")[0]
    a, tr, b = items(track)
    b.set("id", a.get("id"))                       # duplicate id
    a.find("out").text = "149"                     # out - in no longer matches the track
    track.remove(tr)                               # -1 edges without their transition
    problems = " | ".join(fcp.validate_xmeml(root))
    assert "used twice" in problems and "needs a transition" in problems
    ref = ET.fromstring('<xmeml version="4"><sequence><name>s</name><duration>10</duration><rate><timebase>30'
                        '</timebase><ntsc>FALSE</ntsc></rate><media><video><format><samplecharacteristics><width>8'
                        '</width><height>8</height></samplecharacteristics></format><track><clipitem id="c1">'
                        '<name>c</name><start>0</start><end>10</end><in>0</in><out>10</out><file id="file-9"/>'
                        '<link><linkclipref>c7</linkclipref></link></clipitem></track></video></media></sequence>'
                        '</xmeml>')
    problems = " | ".join(fcp.validate_xmeml(ref))
    assert "file-9 is referenced but never defined" in problems and "unknown clipitem 'c7'" in problems


# --- golden ---------------------------------------------------------------------------------------------------

def golden_project():
    files = [video_file("F1", "/media/Interview A.mp4", duration=30.0),
             video_file("F2", "/media/drone 4k.mov", width=3840, height=2160, duration=20.0),
             image_file("F3", "/media/logo.png", width=800, height=400),
             audio_file("F4", "/media/music bed.wav", duration=60.0),
             title_file("T1", "/titles/Launch day.svg")]
    t1 = fade("X1", L1, 6.0, 1.0)
    t1["fade_audio_hint"] = True
    clips = [
        clip("Interview", "F1", position=0.0, start=2.0, end=9.0, alpha=kf([(61, 0.0), (76, 1.0)])),
        clip("Drone", "F2", position=6.0, start=0.0, end=6.0, scale_x=kf([(1, 1.0, BEZIER), (181, 1.08, BEZIER)]),
             scale_y=kf([(1, 1.0, BEZIER), (181, 1.08, BEZIER)])),
        clip("Slowmo", "F1", position=12.0, start=10.0, end=12.0, time=kf([(301, 301), (361, 330)])),
        clip("Logo", "F3", layer=L2, position=1.0, end=4.0, location_x=const(0.3), location_y=const(-0.3),
             rotation=kf([(1, 0.0), (91, 15.0)])),
        clip("Title", "T1", layer=L3, position=2.0, end=3.0),
        clip("Music", "F4", layer=4000000, position=0.0, start=5.0, end=19.0,
             volume=kf([(151, 0.0), (166, 0.6), (556, 0.6), (571, 0.0)])),
    ]
    markers = [{"id": "M1", "position": 6.0, "name": "Drone in", "vector": "orange"}]
    layers = [{"id": "L1", "number": L1, "label": "Picture", "lock": False, "y": 0},
              {"id": "L2", "number": L2, "label": "Graphics", "lock": False, "y": 0},
              {"id": "L3", "number": L3, "label": "Titles", "lock": True, "y": 0},
              {"id": "L4", "number": 4000000, "label": "Music", "lock": False, "y": 0}]
    return project(files=files, clips=clips, transitions=[t1], markers=markers, layers=layers)


def test_golden_export():
    result = build(golden_project(), "/exports/Launch.xml", sequence_name="Launch")
    text = fcp._xml_text(result.root)
    assert fcp.validate_xmeml(result.root) == []
    if os.environ.get("ZENVI_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8"), "export changed; review and rerun with ZENVI_UPDATE_GOLDEN=1"
    # the audio cross fade sits on both channels of the exploded stereo pair
    assert result.counts == {"clips": 6, "video_items": 5, "audio_items": 8, "transitions": 3, "markers": 1,
                             "video_tracks": 3, "audio_tracks": 4, "titles": 1}
