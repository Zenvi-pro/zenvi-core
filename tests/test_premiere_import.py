"""Premiere / FCP7 XML -> Zenvi (classes.importers.final_cut_pro), headless.

Fixtures in tests/fixtures/premiere/: OpenTimelineIO's Premiere export
(Apache-2.0, attribution in its header) and Premiere-style files written to
the conventions of real Premiere exports. Media paths are remapped to small
temporary files and probed by premiere_fakes.FakeMediaProbe, so no
libopenshot is needed; commits run on the real project store + undo stack.
"""

import os
import pathlib

import pytest

from classes.importers import final_cut_pro as imp
from handoff_fakes import linked, tt  # noqa: F401  (fixtures)
from premiere_fakes import (BEZIER, L1, L2, L3, FakeMediaProbe, FakeStills, audio_file, clip, const, fade,
                            image_file, kf, media_on_disk, project, snapshot, video_file)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "premiere"
F = 1.0 / 25


def info(fps=(25, 1), width=1920, height=1080, playhead=0.0, files=None):
    from fractions import Fraction
    return imp.ProjectInfo(fps=Fraction(*fps), width=width, height=height, playhead=playhead, file_ids=files or {})


def plan(name, tmp_path, media, *, project_info=None, placement="new_tracks"):
    """plan_import of a fixture with its media remapped to temporary files ({xml path: file dict})."""
    folder = tmp_path / "media"
    folder.mkdir(exist_ok=True)
    remap, probe = {}, FakeMediaProbe()
    for xml_path, fd in media.items():
        (disk,) = media_on_disk(folder, dict(fd, path=xml_path))
        remap[xml_path] = disk["path"]
        probe.add(disk)
    return imp.plan_import(str(FIXTURES / name), placement=placement, info=project_info or info(), remap=remap,
                           probe=probe)


def by_title(p, title):
    found = [c for c in p.clips if c.title == title]
    assert found, f"no planned clip {title!r}: {[c.title for c in p.clips]}"
    return found[0]


def pts(curve):
    return [(p["co"]["X"], round(p["co"]["Y"], 6)) for p in curve["Points"]]


def off(curve):
    return curve == {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": 2}]}


PROMO = {
    "/media/promo/interview.mp4": video_file("", "/media/promo/interview.mp4", duration=60.0, fps=(25, 1)),
    "/media/promo/broll drone.mov": video_file("", "/media/promo/broll drone.mov", width=3840, height=2160,
                                               duration=30.0, fps=(25, 1)),
    "/media/promo/logo.png": image_file("", "/media/promo/logo.png", width=800, height=400),
    "/media/promo/music.wav": audio_file("", "/media/promo/music.wav", duration=120.0),
    "/media/promo/hidden.mp4": video_file("", "/media/promo/hidden.mp4", duration=20.0, fps=(25, 1)),
}


# --- parsing ---------------------------------------------------------------------------------------------

def test_parse_reads_premiere_structure():
    seq = imp.parse_xml(str(FIXTURES / "premiere_promo.xml"))
    assert (seq.name, seq.rate.timebase, seq.rate.ntsc, seq.width, seq.height) == ("Promo", 25, False, 1920, 1080)
    assert [t.name for t in seq.video] == ["Picture", "Graphics", "Spare"]
    assert [t.primary for t in seq.audio] == [True, False, True, False]
    interview = seq.video[0].items[0]
    assert (interview.start, interview.end, interview.in_, interview.out) == (0, -1, 125, 387)
    assert interview.next is seq.video[0].items[1] and interview.next.cut == 250
    assert interview.file.path == "/media/promo/interview.mp4" and interview.file.width == 1920
    assert seq.video[0].items[2].file.path == "/media/promo/broll drone.mov"     # %20 decoded, id reference
    assert seq.video[0].items[2].file is not interview.file
    assert [m.name for m in seq.markers] == ["Hook", "Section", "Outro"] and seq.markers[0].color == 4281740498


@pytest.mark.parametrize("text, message", [
    ("not xml <", "not valid XML"),
    ("<fcpxml/>", "not a Final Cut Pro 7"),
    ("<xmeml version='4'><sequence><name>x</name></sequence></xmeml>", "no clips"),
])
def test_parse_refuses_what_it_cannot_import(tmp_path, text, message):
    path = tmp_path / "x.xml"
    path.write_text(text)
    with pytest.raises(imp.XmlImportError, match=message):
        imp.parse_xml(str(path))


def test_entity_tricks_are_refused(tmp_path):
    path = tmp_path / "bomb.xml"
    path.write_text('<?xml version="1.0"?><!DOCTYPE xmeml [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                    "<xmeml version='4'><sequence><name>&b;</name></sequence></xmeml>")
    with pytest.raises(imp.XmlImportError, match="safely|not valid"):
        imp.parse_xml(str(path))


def test_pathurl_forms():
    p = imp.path_from_pathurl
    assert p("file://localhost/Users/me/My%20Clip.mov") == "/Users/me/My Clip.mov"
    assert p("file:///Users/me/a.mov") == "/Users/me/a.mov"
    assert p("file://localhost/D%3a/media/x.mov") == "D:/media/x.mov"
    assert p("file:///C:/media/x.mov") == "C:/media/x.mov"
    assert p("file://Gladiator/hd/x.aep") == "//Gladiator/hd/x.aep"
    assert p("/abs/raw path.mov") == "/abs/raw path.mov"                    # auto-editor style raw paths
    assert p("media/clip.mov", "/xml/dir") == os.path.normpath("/xml/dir/media/clip.mov")


# --- the Premiere-style promo ------------------------------------------------------------------------------

def test_promo_clips_keep_timing_trims_and_merge_linked_audio(tmp_path):
    p = plan("premiere_promo.xml", tmp_path, PROMO)
    interview = by_title(p, "Interview")
    assert (interview.position, interview.start, interview.end) == pytest.approx((0.0, 5.0, 5.0 + 262 * F))
    drone = by_title(p, "Drone")
    assert (drone.position, drone.start, drone.end) == pytest.approx((238 * F, 2.0, 2.0 + 162 * F))
    assert interview.track == drone.track == "V1"                           # overlapping on one track, like Zenvi
    assert "has_audio" not in interview.props and "has_video" not in interview.props   # A/V merged
    assert pts(interview.props["volume"]) == [(1.0, 0.5)]                   # Audio Levels 0.5 = -6 dB, constant
    music = by_title(p, "Music bed")
    assert off(music.props["has_video"]) if "has_video" in music.props else True
    assert (music.position, music.start, music.end) == pytest.approx((0.0, 10.0, 26.0))
    assert pts(music.props["volume"]) == [(251.0, 0.0), (276.0, 1.0), (626.0, 1.0), (651.0, 0.0)]
    assert [t.label for t in p.tracks] == ["Music", "Picture", "Graphics", "Spare"]
    assert {c.title for c in p.clips} == {"Interview", "Drone", "Logo", "Logo off", "Hidden", "Music bed"}


def test_promo_dissolve_with_audio_cross_fade_becomes_a_fade_with_audio_hint(tmp_path):
    p = plan("premiere_promo.xml", tmp_path, PROMO)
    (tr,) = p.transitions
    assert (tr.track, tr.position, tr.duration, tr.reverse, tr.audio) == ("V1", pytest.approx(238 * F),
                                                                         pytest.approx(24 * F), False, True)
    assert "volume" not in by_title(p, "Drone").props                       # libopenshot fades the audio itself


def test_promo_motion_lands_where_premiere_drew_it(tmp_path):
    p = plan("premiere_promo.xml", tmp_path, PROMO)
    interview = by_title(p, "Interview")
    assert pts(interview.props["scale_x"]) == [(126.0, 1.0), (251.0, 1.2)]
    assert pts(interview.props["location_x"]) == [(126.0, 0.0), (251.0, 0.1)]       # 0.1 x 1920 source px
    assert pts(interview.props["location_y"]) == [(126.0, 0.0), (251.0, -0.05)]
    assert pts(interview.props["alpha"]) == [(126.0, 0.0), (138.0, 1.0)]
    assert "scale_x" not in by_title(p, "Drone").props                      # 50 % of 4K = FIT in 1080p
    logo = by_title(p, "Logo")
    assert (logo.start, logo.end) == pytest.approx((0.0, 6.0))             # stills start at 0
    assert pts(logo.props["scale_x"]) == [(1.0, round(400 / 1920, 6))]      # 50 % of 800 px; FIT is 1920
    assert pts(logo.props["location_x"]) == [(1.0, 0.25)]                   # anchor at 960 + 0.6 x 800 = 1440
    assert pts(logo.props["location_y"]) == [(1.0, round(-280 / 1080, 6))]  # 540 - 0.7 x 400 = 260
    assert pts(logo.props["rotation"]) == [(1.0, 10.0)]


def test_promo_disabled_items_come_in_hidden_and_extras_are_reported(tmp_path):
    p = plan("premiere_promo.xml", tmp_path, PROMO)
    assert off(by_title(p, "Logo off").props["has_video"])
    hidden = by_title(p, "Hidden")
    assert off(hidden.props["has_video"]) and off(hidden.props["has_audio"])   # disabled track, no linked audio
    warnings = " | ".join(p.warnings)
    assert "'Title Card'" in warnings and "Gaussian Blur" in warnings
    assert "turned off in Premiere" in warnings and "marker durations" in warnings
    assert [(m.position, m.name, m.color) for m in p.markers] == [
        (pytest.approx(1.0), "Hook: start strong", "red"), (pytest.approx(4.0), "Section", "green"),
        (pytest.approx(15.0), "Outro", "green")]
    locked = [t for t in p.tracks if t.locked]
    assert [t.label for t in locked] == ["Graphics"]


def test_at_playhead_shifts_everything(tmp_path):
    p = plan("premiere_promo.xml", tmp_path, PROMO, project_info=info(playhead=10.0), placement="at_playhead")
    assert by_title(p, "Interview").position == pytest.approx(10.0)
    assert p.transitions[0].position == pytest.approx(10.0 + 238 * F)
    assert p.markers[0].position == pytest.approx(11.0)


def test_missing_media_is_skipped_and_listed(tmp_path):
    media = dict(PROMO)
    media.pop("/media/promo/music.wav")
    p = plan("premiere_promo.xml", tmp_path, media)
    assert p.missing == ["/media/promo/music.wav"] and "Music bed" not in {c.title for c in p.clips}


def test_a_video_only_dissolve_keeps_the_audio_cut_as_a_gate(tmp_path):
    """Premiere: picture dissolves, sound hard-cuts at the edit -> merged clips with silent tails."""
    text = (FIXTURES / "premiere_promo.xml").read_text(encoding="utf-8")
    start = text.index("<transitionitem>", text.index("currentExplodedTrackIndex=\"0\""))
    end = text.index("</transitionitem>", start) + len("</transitionitem>")
    text = text[:start] + text[end:]
    # the audio items now meet at the cut (250): Interview 0..250, Drone 250..400 (in 62)
    text = text.replace("<end>-1</end>\n\t\t\t\t\t\t<in>125</in>\n\t\t\t\t\t\t<out>387</out>\n\t\t\t\t\t\t<pproTicksIn>"
                        "1270080000000</pproTicksIn>\n\t\t\t\t\t\t<pproTicksOut>3932167680000</pproTicksOut>\n\t\t\t\t\t\t"
                        '<file id="file-1"/>',
                        "<end>250</end>\n<in>125</in>\n<out>375</out>\n<file id=\"file-1\"/>", 1)
    text = text.replace('<start>-1</start>\n\t\t\t\t\t\t<end>400</end>\n\t\t\t\t\t\t<in>50</in>\n\t\t\t\t\t\t<out>212</out>'
                        '\n\t\t\t\t\t\t<file id="file-2"/>\n\t\t\t\t\t\t<sourcetrack>\n\t\t\t\t\t\t\t<mediatype>audio'
                        '</mediatype>\n\t\t\t\t\t\t\t<trackindex>1</trackindex>',
                        "<start>250</start>\n<end>400</end>\n<in>62</in>\n<out>212</out>\n<file id=\"file-2\"/>\n"
                        "<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex>", 1)
    (tmp_path / "cut.xml").write_text(text, encoding="utf-8")
    folder = tmp_path / "media"
    folder.mkdir()
    remap, probe = {}, FakeMediaProbe()
    for xml_path, fd in PROMO.items():
        (disk,) = media_on_disk(folder, dict(fd, path=xml_path))
        remap[xml_path] = disk["path"]
        probe.add(disk)
    p = imp.plan_import(str(tmp_path / "cut.xml"), info=info(), remap=remap, probe=probe)
    interview, drone = by_title(p, "Interview"), by_title(p, "Drone")
    assert "has_audio" not in interview.props and "has_audio" not in drone.props
    assert pts(interview.props["volume"])[-2:] == [(375.0, 0.5), (376.0, 0.0)]       # silent after the cut
    assert pts(drone.props["volume"]) == [(62.0, 0.0), (63.0, 1.0)]                   # sound starts at the cut
    assert p.transitions[0].audio is False


# --- speed -------------------------------------------------------------------------------------------------

SPEED = {"/media/speed/clip.mp4": video_file("", "/media/speed/clip.mp4", duration=20.0)}


def test_constant_speed_reverse_and_ramps_become_time_curves(tmp_path):
    p = plan("premiere_speed.xml", tmp_path, SPEED, project_info=info(fps=(30, 1)))
    fast = by_title(p, "Fast")
    assert (fast.position, fast.start, fast.end) == pytest.approx((0.0, 1.0, 3.5))   # media in from pproTicksIn
    assert pts(fast.props["time"]) == [(31.0, 31.0), (106.0, 180.0)]
    back = by_title(p, "Back")
    assert (back.start, back.end) == pytest.approx((1.0, 3.0))
    assert pts(back.props["time"]) == [(31.0, 90.0), (91.0, 31.0)]               # Zenvi's own Reverse shape
    ramp = by_title(p, "Ramp")
    assert (ramp.position, ramp.start) == pytest.approx((200 / 30.0, 10.0))
    assert pts(ramp.props["time"]) == [(301.0, 301.0), (331.0, 316.0), (346.0, 316.0), (361.0, 331.0)]
    for c in (fast, back, ramp):
        assert off(c.props["has_audio"])                                       # no linked audio in Premiere
    assert any("variable speed" in w for w in p.warnings)


def test_speed_curves_play_what_premiere_played(tmp_path):
    from classes.handoff.timeline_view import TimelineSnapshot
    p = plan("premiere_speed.xml", tmp_path, SPEED, project_info=info(fps=(30, 1)))
    proj = project(files=[video_file("F1", "/m/clip.mp4", duration=20.0)],
                   clips=[clip(c.title, "F1", position=c.position, start=c.start, end=c.end, **c.props)
                          for c in p.clips])
    snap = TimelineSnapshot.from_project(proj)
    fast, back, ramp = snap.clip("Fast"), snap.clip("Back"), snap.clip("Ramp")
    assert fast.speed.kind == "constant" and fast.speed.factor == pytest.approx(2.0, abs=0.02)
    assert back.speed.reversed and back.speed.factor == pytest.approx(1.0, abs=0.02)
    assert fast.source_frame_at(0.0) == 31 and back.source_frame_at(back.position) == 90
    assert ramp.speed.kind == "variable"
    assert ramp.source_frame_at(ramp.position + 1.25) == 316                     # inside the 15-frame freeze


# --- nesting, the OTIO sample, legacy files ------------------------------------------------------------------

def test_nested_sequence_is_flattened_to_the_part_used(tmp_path):
    media = {"/media/speed/clip.mp4": video_file("", "/media/speed/clip.mp4", duration=20.0),
             "/media/speed/other.mp4": video_file("", "/media/speed/other.mp4", duration=10.0)}
    p = plan("premiere_nested.xml", tmp_path, media, project_info=info(fps=(30, 1)))
    got = {c.title: (round(c.position, 4), round(c.start, 4), round(c.end, 4), c.track) for c in p.clips}
    assert got["Before"] == (0.0, 0.0, 2.0, "V1")
    assert got["N1"][:3] == (2.0, round(50 / 30, 4), 5.0)                       # cut to the nest's in point
    assert got["N2"][:3] == (round(160 / 30, 4), 0.0, round(100 / 30, 4))      # cut to its out point
    assert got["N3"][:3] == (round(110 / 30, 4), 0.0, round(100 / 30, 4))
    labels = {t.key: t.label for t in p.tracks}
    assert labels[got["N1"][3]] == "Nest V1" and labels[got["N3"][3]] == "Nest V2"
    keys = [t.key for t in p.tracks]
    assert keys.index("V1") < keys.index(got["N1"][3]) < keys.index(got["N3"][3])   # stacked above their track
    assert any("flattened" in w for w in p.warnings)


OTIO_MEDIA = {f"D:/media/{n}": video_file("", f"D:/media/{n}", width=1280, height=720, duration=d)
              for n, d in (("sc01_sh010_anim.mov", 100 / 30), ("sc01_sh020_anim.mov", 175 / 30),
                           ("sc01_sh030_anim.mov", 400 / 30), ("sc01_master_layerA_sh030_temp.mov", 400 / 30))}
OTIO_MEDIA.update({"D:/media/sc01_placeholder.wav": audio_file("", "D:/media/sc01_placeholder.wav", duration=170 / 30),
                   "D:/media/track_08.wav": audio_file("", "D:/media/track_08.wav", duration=198 / 30)})


def test_otio_premiere_example_imports(tmp_path):
    p = plan("otio_premiere_example.xml", tmp_path, OTIO_MEDIA, project_info=info(fps=(30, 1), width=1280, height=720))
    c1 = [c for c in p.clips if c.title == "sc01_sh010_anim.mov" and c.position == pytest.approx(536 / 30)][0]
    assert (c1.start, c1.end - c1.start) == pytest.approx((0.0, 100 / 30))        # a 15 fps clipitem rate
    faded = [c for c in p.clips if c.title == "sc01_sh030_anim.mov" and c.track == "V2"][0]
    assert faded.position == pytest.approx(322 / 30) and faded.end - faded.start == pytest.approx(235 / 30)
    reverse_fades = [t for t in p.transitions if t.reverse]
    assert [(round(t.position * 30), round(t.duration * 30)) for t in reverse_fades] == [(538, 19)]
    dissolve = [t for t in p.transitions if not t.reverse][0]
    assert (round(dissolve.position * 30), round(dissolve.duration * 30)) == (1152, 25)
    nest = [c for c in p.clips if ">" in c.track]
    assert sorted(round(c.position * 30) for c in nest) == [636, 736]           # sequence-2 flattened at 636
    assert {round(m.position * 30) for m in p.markers} == {113, 492, 298}
    warnings = " | ".join(p.warnings)
    assert "test_title" in warnings and "clip markers" in warnings
    audio_only = [c for c in p.clips if c.title in ("sc01_placeholder.wav", "track_08.wav")]
    assert len(audio_only) == 2                                                 # one per exploded stereo pair


def test_legacy_openshot_exports_still_round_trip(tmp_path):
    p = plan("legacy_openshot.xml", tmp_path, SPEED, project_info=info(fps=(30, 1)))
    (c,) = p.clips
    assert pts(c.props["location_x"]) == [(1.0, 0.25)] and "location_y" not in c.props
    assert pts(c.props["alpha"]) == [(1.0, 0.5)]


# --- committing: one undo step --------------------------------------------------------------------------------

@pytest.fixture
def importer(linked):
    linked.window.timeline._get_transition_reader_json = lambda path: {
        "path": path, "has_single_image": True, "type": "QtImageReader"}
    return linked


def test_commit_is_one_undo_step_and_undo_removes_everything(importer, tmp_path):
    p = plan("premiere_promo.xml", tmp_path, PROMO)
    before = {k: len(importer.get(k) or []) for k in ("files", "clips", "effects", "markers", "layers")}
    importer.mark()
    summary = imp.commit_import(p)
    assert importer.undo_steps_since_mark() == 1
    assert len(summary["clip_ids"]) == 6 and len(summary["transition_ids"]) == 1 and len(summary["marker_ids"]) == 3
    assert len(summary["file_ids"]) == 5 and len(summary["track_numbers"]) == 4
    layers = {int(t["number"]): t for t in importer.get("layers")}
    assert [layers[n]["label"] for n in summary["track_numbers"]] == ["Music", "Picture", "Graphics", "Spare"]
    assert [layers[n]["lock"] for n in summary["track_numbers"]] == [False, False, True, False]
    transition = [t for t in importer.get("effects") if t["id"] == summary["transition_ids"][0]][0]
    assert transition["fade_audio_hint"] is True and transition["type"] == "Mask"
    assert [p_["co"]["Y"] for p_ in transition["brightness"]["Points"]] == list(imp.DISSOLVE_BRIGHTNESS)
    interview = [c for c in importer.clips() if c.get("title") == "Interview"][0]
    assert interview["layer"] == summary["track_numbers"][1] and interview["start"] == pytest.approx(5.0)
    music = [c for c in importer.clips() if c.get("title") == "Music bed"][0]
    assert music["has_video"]["Points"][0]["co"]["Y"] == 0.0 and music["scale"] == 3   # audio-only overrides
    importer.undo()
    assert {k: len(importer.get(k) or []) for k in before} == before


def test_existing_project_files_are_reused(importer, tmp_path):
    imp.commit_import(plan("premiere_speed.xml", tmp_path, SPEED, project_info=info(fps=(30, 1))))
    files_before = list(importer.get("files"))
    again = plan("premiere_speed.xml", tmp_path, SPEED, project_info=imp.read_project_info())
    assert again.media == {}                                           # the media is already in Project Files
    summary = imp.commit_import(again)
    assert summary["file_ids"] == [] and importer.get("files") == files_before
    clips = [c for c in importer.clips() if c["id"] in summary["clip_ids"]]
    assert len(clips) == 3 and {c["file_id"] for c in clips} == {files_before[-1]["id"]}


def test_legacy_import_xml_entry_returns_the_summary_in_one_step(importer, tmp_path, monkeypatch):
    folder = tmp_path / "media"
    folder.mkdir()
    (disk,) = media_on_disk(folder, video_file("", "/media/speed/clip.mp4", duration=20.0))
    xml = tmp_path / "speed.xml"
    xml.write_text((FIXTURES / "premiere_speed.xml").read_text().replace("/media/speed/clip.mp4",
                                                                       disk["path"].replace(" ", "%20")))
    probe = FakeMediaProbe({disk["path"]: disk})
    from classes.handoff import linked_media
    monkeypatch.setattr(linked_media, "probe_media", probe)
    importer.mark()
    summary = imp.import_xml(str(xml), prompt=False)
    assert importer.undo_steps_since_mark() == 1
    assert len(summary["clip_ids"]) == 3 and summary["missing"] == [] and summary["track_numbers"]


# --- Zenvi -> XML -> Zenvi ----------------------------------------------------------------------------------------

def _roundtrip_project(folder):
    files = media_on_disk(folder, video_file("F1", "/m/interview.mp4", duration=30.0),
                          video_file("F2", "/m/drone.mov", width=3840, height=2160, duration=20.0),
                          image_file("F3", "/m/logo.png", width=800, height=400),
                          audio_file("F4", "/m/music.wav", duration=60.0))
    t1 = fade("X1", L1, 6.0, 1.0)
    t1["fade_audio_hint"] = True
    clips = [
        clip("Interview", "F1", position=0.0, start=2.0, end=9.0, alpha=kf([(61, 0.0), (76, 1.0)]),
             volume=const(0.8)),
        clip("Drone", "F2", position=6.0, end=6.0, scale_x=kf([(1, 1.0, BEZIER), (181, 1.1, BEZIER)]),
             scale_y=kf([(1, 1.0, BEZIER), (181, 1.1, BEZIER)]), location_y=kf([(1, 0.0), (181, -0.05)])),
        clip("Slow", "F1", position=12.0, start=10.0, end=12.0, time=kf([(301, 301), (361, 330)])),
        clip("Back", "F1", position=15.0, start=1.0, end=3.0, time=kf([(31, 90), (91, 31)])),
        clip("Logo", "F3", layer=L2, position=1.0, end=4.0, location_x=const(0.3), location_y=const(-0.3),
             rotation=kf([(1, 0.0), (91, 15.0)]), origin_x=const(0.25)),
        clip("Music", "F4", layer=L3, position=0.0, start=5.0, end=19.0,
             volume=kf([(151, 0.0), (166, 0.6), (556, 0.6), (571, 0.0)])),
    ]
    markers = [{"id": "M1", "position": 6.0, "name": "Drone in", "vector": "orange"}]
    return files, project(files=files, clips=clips, transitions=[t1], markers=markers)


def test_zenvi_to_xml_to_zenvi_round_trip(importer, tmp_path):
    from classes.exporters import final_cut_pro as fcp
    from classes.handoff.timeline_view import TimelineSnapshot
    from classes.handoff.transform import clip_geometry
    folder = tmp_path / "media"
    folder.mkdir()
    files, proj = _roundtrip_project(folder)
    before = snapshot(proj)
    out = fcp.export_timeline(before, str(tmp_path / "Edit.xml"), render_stills=FakeStills())
    p = imp.plan_import(out.path, info=info(fps=(30, 1)), probe=FakeMediaProbe({f["path"]: f for f in files}))
    imp.commit_import(p)
    after = TimelineSnapshot.from_project(importer.store._data)
    assert sorted(c.title for c in after.clips) == sorted(c.title for c in before.clips)
    for a in before.clips:
        b = [c for c in after.clips if c.title == a.title][0]
        assert (b.timeline_in, b.timeline_out) == pytest.approx((a.timeline_in, a.timeline_out), abs=1e-6), a.title
        assert b.speed.kind == a.speed.kind and b.speed.reversed == a.speed.reversed, a.title
        first, last = a.frame_range()
        for k in range(0, last - first + 1):
            t = a.timeline_in + k / 30.0
            assert b.source_frame_at(t) == a.source_frame_at(t), (a.title, k)
            assert b.curve("alpha").value_at(t) == pytest.approx(a.curve("alpha").value_at(t), abs=0.011), (a.title, k)
            assert b.curve("volume").value_at(t) == pytest.approx(a.curve("volume").value_at(t), abs=0.003), (a.title, k)
            if a.file.has_video:
                ga, gb = clip_geometry(a, t, 1920, 1080), clip_geometry(b, t, 1920, 1080)
                for key in ("center_x", "center_y", "width", "height"):
                    assert getattr(gb, key) == pytest.approx(getattr(ga, key), abs=0.5), (a.title, k, key)
                assert gb.rotation == pytest.approx(ga.rotation, abs=0.06), (a.title, k)
    (ta,), (tb,) = before.transitions, after.transitions
    assert (tb.position, tb.duration, tb.data.get("fade_audio_hint")) == (ta.position, ta.duration, True)
    assert [(m.time, m.name, m.color) for m in after.markers] == [(m.time, m.name, m.color) for m in before.markers]


GOLDEN_MEDIA = {
    "/media/Interview A.mp4": video_file("", "/media/Interview A.mp4", duration=30.0),
    "/media/drone 4k.mov": video_file("", "/media/drone 4k.mov", width=3840, height=2160, duration=20.0),
    "/media/logo.png": image_file("", "/media/logo.png", width=800, height=400),
    "/media/music bed.wav": audio_file("", "/media/music bed.wav", duration=60.0),
    "/exports/Launch_media/titles/Launch day-T1.png": image_file("", "/exports/Launch_media/titles/Launch day-T1.png",
                                                                width=1920, height=1080),
}


@pytest.mark.parametrize("name", ["export_golden.xml", "otio_rewrite_of_golden.xml"])
def test_our_export_and_otios_rewrite_of_it_import_alike(tmp_path, name):
    """OpenTimelineIO's fcp_xml adapter reads the golden export; what it writes back imports the same way."""
    p = plan(name, tmp_path, GOLDEN_MEDIA, project_info=info(fps=(30, 1)))
    got = {c.title: (round(c.position, 3), round(c.start, 3), round(c.end, 3), c.track) for c in p.clips}
    assert got == {"Interview": (0.0, 2.0, 9.0, "V1"), "Drone": (6.0, 0.0, 6.0, "V1"),
                   "Slowmo": (12.0, 10.0, 12.0, "V1"), "Logo": (1.0, 0.0, 4.0, "V2"),
                   "Title": (2.0, 0.0, 3.0, "V3"), "Music": (0.0, 5.0, 19.0, got["Music"][3])}
    assert pts(by_title(p, "Slowmo").props["time"]) == [(301.0, 301.0), (361.0, 330.0)]
    assert [(t.position, t.duration) for t in p.transitions] == [(6.0, 1.0)]
    assert [(m.position, m.name, m.color) for m in p.markers] == [(6.0, "Drone in", "orange")]
    assert p.missing == []


# --- review C3-1: real Premiere files, channels, failed commits, old Zenvi exports -------------------------

def _xml(tmp_path, body, name="Edit.xml", tb=25):
    """A one-sequence xmeml file around *body* (the sequence's <media> content)."""
    text = (f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4"><sequence id="s1">'
            f'<name>Edit</name><duration>500</duration><rate><timebase>{tb}</timebase><ntsc>FALSE</ntsc></rate>'
            f'<media><video><format><samplecharacteristics><width>1920</width><height>1080</height>'
            f'</samplecharacteristics></format>{body.get("video", "")}</video><audio>{body.get("audio", "")}</audio>'
            f'</media></sequence></xmeml>')
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _file(fid, path, *, duration=3000, tb=25, channels=2, video=False):
    v = "<video><samplecharacteristics><width>1920</width><height>1080</height></samplecharacteristics></video>" \
        if video else ""
    a = f"<audio><channelcount>{channels}</channelcount></audio>" if channels else ""
    return (f"<file id='{fid}'><name>{os.path.basename(path)}</name><pathurl>file://localhost{path}</pathurl>"
            f"<rate><timebase>{tb}</timebase><ntsc>FALSE</ntsc></rate><duration>{duration}</duration>"
            f"<media>{v}{a}</media></file>")


def _item(cid, name, start, end, inn, out, file_xml, *, track=1, links=(), extra="", attrs="", tb=25):
    lk = "".join(f"<link><linkclipref>{ref}</linkclipref><mediatype>{mt}</mediatype></link>" for ref, mt in links)
    return (f"<clipitem id='{cid}'{attrs}><name>{name}</name><enabled>TRUE</enabled><duration>3000</duration>"
            f"<rate><timebase>{tb}</timebase><ntsc>FALSE</ntsc></rate><start>{start}</start><end>{end}</end>"
            f"<in>{inn}</in><out>{out}</out>{file_xml}<sourcetrack><mediatype>audio</mediatype>"
            f"<trackindex>{track}</trackindex></sourcetrack>{extra}{lk}</clipitem>")


def _plan_xml(path, media, **kw):
    probe = FakeMediaProbe()
    for fd in media:
        probe.add(fd)
    kw.setdefault("info", info())
    return imp.plan_import(path, probe=probe, **kw)


def test_premiere_virtual_speed_keys_with_junk_values_do_not_scrub_back(tmp_path):
    # review C3-1 #1, modelled on a real Premiere export (jacksongs/autorough): its virtual "speedkfout"
    # key carried -1815255671, and the clip's last frames played media frame 1
    (m,) = media_on_disk(tmp_path, video_file("", "/m/match.mp4", duration=40.0, fps=(25, 1), has_audio=False))
    keys = [(0, 0, "<speedvirtualkf>TRUE</speedvirtualkf><speedkfstart>TRUE</speedkfstart>"),
            (400, 400, "<speedvirtualkf>TRUE</speedvirtualkf><speedkfin>TRUE</speedkfin>"),
            (420, 420, "<pproRampStart>TRUE</pproRampStart>"), (440, 430, "<pproRampEnd>TRUE</pproRampEnd>"),
            (480, 470, ""), (500, -1815255671, "<speedvirtualkf>TRUE</speedvirtualkf><speedkfout>TRUE</speedkfout>"),
            (600, 580, ""), (1010, 1000, "<speedvirtualkf>TRUE</speedvirtualkf><speedkfend>TRUE</speedkfend>")]
    graph = "".join(f"<keyframe><when>{w}</when><value>{v}</value>{x}</keyframe>" for w, v, x in keys)
    remap = ("<filter><effect><name>Time Remap</name><effectid>timeremap</effectid>"
             "<parameter><parameterid>variablespeed</parameterid><value>1</value></parameter>"
             "<parameter><parameterid>speed</parameterid><value>99.42</value></parameter>"
             "<parameter><parameterid>reverse</parameterid><value>FALSE</value></parameter>"
             "<parameter><parameterid>graphdict</parameterid><valuemin>0</valuemin><valuemax>1000</valuemax>"
             f"<value>0</value>{graph}</parameter></effect></filter>")
    item = _item("c1", "Match", 0, 100, 400, 500, _file("f1", m["path"], duration=1000, channels=0, video=True),
                 extra=remap)
    p = _plan_xml(_xml(tmp_path, {"video": f"<track>{item}</track>"}), [m])
    (c,) = p.clips
    ys = [y for _x, y in pts(c.props["time"])]
    assert min(ys) >= 401 and ys == sorted(ys)                       # never back to frame 1
    assert ys[-1] == pytest.approx(470 + 20 * 110 / 120 + 1, abs=1)  # the real curve at the out point


def test_linked_audio_channels_of_one_file_are_one_clip(tmp_path):
    # review C3-1 #5: FCP7 / Resolve stereo pairs (two mono tracks, linked) became two full-file clips (+6 dB)
    (m,) = media_on_disk(tmp_path, audio_file("", "/m/music.wav", duration=120.0))
    f = _file("f1", m["path"])
    a1 = _item("a1", "Music", 0, 250, 0, 250, f, track=1, links=[("a1", "audio"), ("a2", "audio")])
    a2 = _item("a2", "Music", 0, 250, 0, 250, "<file id='f1'/>", track=2, links=[("a1", "audio"), ("a2", "audio")])
    p = _plan_xml(_xml(tmp_path, {"audio": f"<track>{a1}</track><track>{a2}</track>"}), [m])
    (c,) = p.clips
    assert (c.title, c.position, c.end) == ("Music", 0.0, 10.0) and "channel_filter" not in c.props
    unlinked = _item("a3", "Music", 0, 250, 0, 250, "<file id='f1'/>", track=2)
    p = _plan_xml(_xml(tmp_path, {"audio": f"<track>{a1}</track><track>{unlinked}</track>"}, name="u.xml"), [m])
    assert len(p.clips) == 2                                          # not linked: two sounds on purpose


def test_one_channel_of_a_stereo_file_plays_alone(tmp_path):
    # the import side of review C3-1 #3: a lone mono item of channel 2 must not play both channels
    (m,) = media_on_disk(tmp_path, audio_file("", "/m/interview.wav", duration=120.0))
    (mono,) = media_on_disk(tmp_path, audio_file("", "/m/vo.wav", duration=60.0, channels=1))
    lav = _item("a1", "Camera mic", 0, 100, 0, 100, _file("f1", m["path"]), track=2,
                attrs=' premiereChannelType="mono"')
    vo = _item("a2", "VO", 0, 100, 0, 100, _file("f2", mono["path"], channels=1), track=1)
    p = _plan_xml(_xml(tmp_path, {"audio": f"<track>{lav}</track><track>{vo}</track>"}), [m, mono])
    by = {c.title: c for c in p.clips}
    assert by["Camera mic"].props["channel_filter"] == {"Points": [{"co": {"X": 1.0, "Y": 1.0}, "interpolation": 2}]}
    assert "channel_filter" not in by["VO"].props
    assert any("one channel of a stereo file" in w for w in p.warnings)
    exploded = ('<track currentExplodedTrackIndex="0" totalExplodedTrackCount="2">' + lav.replace("a1", "e1") +
                '</track><track currentExplodedTrackIndex="1" totalExplodedTrackCount="2">' +
                lav.replace("a1", "e2").replace("<trackindex>2</trackindex>", "<trackindex>1</trackindex>")
                + "</track>")
    p = _plan_xml(_xml(tmp_path, {"audio": exploded}, name="pair.xml"), [m])
    assert [("channel_filter" in c.props) for c in p.clips] == [False]   # a stereo pair is the whole file
    said_mono = _item("a3", "Mix", 0, 100, 0, 100, _file("f3", m["path"], channels=1), track=1)
    adaptive = _item("a4", "Adaptive", 0, 100, 0, 100, _file("f4", m["path"]), track=2,
                     attrs=' premiereChannelType="adaptive"')
    p = _plan_xml(_xml(tmp_path, {"audio": f"<track>{said_mono}</track><track>{adaptive}</track>"},
                       name="other.xml"), [m])
    assert [("channel_filter" in c.props) for c in p.clips] == [False, False]   # the XML says they play all


def test_zenvi_channel_clips_round_trip(importer, tmp_path):
    from classes.exporters import final_cut_pro as fcp
    folder = tmp_path / "media"
    folder.mkdir()
    (f1,) = media_on_disk(folder, audio_file("F1", "/m/interview.wav", duration=30.0))
    only = [{"Points": [{"co": {"X": 1.0, "Y": float(k)}, "interpolation": 2}]} for k in (0, 1)]
    proj = project(files=[f1], clips=[clip("Lav", "F1", end=5.0, channel_filter=only[0]),
                                      clip("Camera", "F1", layer=L2, end=5.0, channel_filter=only[1])])
    out = fcp.export_timeline(snapshot(proj), str(tmp_path / "Ch.xml"), render_stills=FakeStills())
    p = imp.plan_import(out.path, info=info(fps=(30, 1)), probe=FakeMediaProbe({f1["path"]: f1}))
    got = {c.title: c.props.get("channel_filter", {}).get("Points", [{}])[0].get("co", {}).get("Y") for c in p.clips}
    assert got == {"Lav": 0.0, "Camera": 1.0}


def test_a_commit_that_cannot_finish_leaves_the_project_and_history_alone(importer, tmp_path, monkeypatch):
    # review C3-1 #6: a failure after files, tracks and clips were saved left half an import on the undo stack
    p = plan("premiere_promo.xml", tmp_path, PROMO)
    before = {k: len(importer.get(k) or []) for k in ("files", "clips", "effects", "markers", "layers")}
    redo_before = list(importer.app.updates.redoHistory)
    importer.mark()
    from classes import transition_ops
    real_find = transition_ops.find_transition
    monkeypatch.setattr(transition_ops, "find_transition", lambda name: (None, []))   # refused before any change
    with pytest.raises(imp.XmlImportError, match="fade transition is missing"):
        imp.commit_import(p)
    assert importer.undo_steps_since_mark() == 0
    monkeypatch.setattr(transition_ops, "find_transition", real_find)
    real = imp._dissolve_transition

    def breaks(*args, **kwargs):                                       # fails half-way through the commit
        raise RuntimeError("disk full")

    monkeypatch.setattr(imp, "_dissolve_transition", breaks)
    with pytest.raises(imp.XmlImportError, match="nothing was imported"):
        imp.commit_import(p)
    assert importer.undo_steps_since_mark() == 0
    assert {k: len(importer.get(k) or []) for k in before} == before
    assert list(importer.app.updates.redoHistory) == redo_before       # Redo cannot bring the half import back
    monkeypatch.setattr(imp, "_dissolve_transition", real)
    summary = imp.commit_import(p)                                    # and the import still works afterwards
    assert len(summary["clip_ids"]) == 6 and importer.undo_steps_since_mark() == 1


def test_crops_that_cannot_be_built_are_reported_not_hidden(importer, tmp_path, monkeypatch):
    from classes.editor_tools import titles_text_common
    (m,) = media_on_disk(tmp_path, video_file("", "/m/a.mp4", duration=20.0, fps=(25, 1)))
    crop = ("<filter><effect><name>Crop</name><effectid>crop</effectid>"
            "<parameter><parameterid>left</parameterid><value>10</value></parameter></effect></filter>")
    body = _item("c1", "Cropped", 0, 50, 0, 50, _file("f1", m["path"], video=True), extra=crop)
    p = _plan_xml(_xml(tmp_path, {"video": f"<track>{body}</track>"}), [m])
    assert [bool(c.crop) for c in p.clips] == [True]

    def no_crop(name):
        raise RuntimeError("this libopenshot build has no Crop effect")

    monkeypatch.setattr(titles_text_common, "new_effect_json", no_crop)
    summary = imp.commit_import(p)
    assert len(summary["clip_ids"]) == 1
    assert any("crops were left out" in w for w in p.warnings)
    assert not any(c.get("effects") for c in importer.clips() if c["id"] in summary["clip_ids"])


def test_import_project_file_tool_plans_off_the_gui_thread(importer, tmp_path, monkeypatch):
    # review C3-1 #7: import_project_file_tool(format="fcpxml") parsed and probed on the GUI thread
    from classes.editor_tools import titles_text_common
    from classes.handoff import linked_media
    folder = tmp_path / "media"
    folder.mkdir()
    (disk,) = media_on_disk(folder, video_file("", "/media/speed/clip.mp4", duration=20.0))
    xml = tmp_path / "Speeds.xml"
    xml.write_text((FIXTURES / "premiere_speed.xml").read_text().replace("/media/speed/clip.mp4", disk["path"]))
    monkeypatch.setattr(linked_media, "probe_media", FakeMediaProbe({disk["path"]: disk}))
    on_gui = []
    real = titles_text_common.on_main

    def record(func, *args, **kwargs):
        on_gui.append(getattr(func, "__name__", str(func)))
        return real(func, *args, **kwargs)

    monkeypatch.setattr(titles_text_common, "on_main", record)
    planned = []
    real_plan = imp.plan_import
    monkeypatch.setattr(imp, "plan_import", lambda *a, **k: planned.append(1) or real_plan(*a, **k))
    importer.mark()
    r = importer.call_receipt("import_project_file_tool", file_path=str(xml), format="fcpxml")
    assert r["status"] == "applied" and len(r["data"]["timeline_clip_ids"]) == 3, r["summary"]
    assert on_gui == ["read_project_info", "commit_import"] and planned == [1]
    assert importer.undo_steps_since_mark() == 1


def test_old_zenvi_exports_keep_holds_eases_and_keys_under_the_effect(tmp_path):
    # the removed legacy tests: per-keyframe <interpolation>, keyframes directly under <effect>,
    # pixel centres; old exports wrote libopenshot's 1-based clip frame as <when>
    (v,) = media_on_disk(tmp_path, video_file("", "/m/video.mp4", duration=10.0, fps=(24, 1)))
    (a,) = media_on_disk(tmp_path, audio_file("", "/m/audio.wav", duration=10.0))
    video = _item("v1", "Video Clip", 24, 72, 12, 60, _file("f1", v["path"], video=True, tb=24), tb=24, extra=(
        "<filter><effect><effectid>opacity</effectid>"
        "<keyframe><when>13</when><value>100</value><interpolation><name>linear</name></interpolation></keyframe>"
        "<keyframe><when>37</when><value>50</value><interpolation><name>bezier</name></interpolation></keyframe>"
        "</effect><effect><effectid>basic</effectid>"
        "<parameter><parameterid>center</parameterid><keyframe><when>13</when><value><horiz>1056</horiz>"
        "<vert>486</vert></value><interpolation><name>linear</name></interpolation></keyframe></parameter>"
        "<parameter><parameterid>scale</parameterid><keyframe><when>13</when><value>125</value>"
        "<interpolation><name>linear</name></interpolation></keyframe></parameter>"
        "<parameter><parameterid>rotation</parameterid>"
        "<keyframe><when>13</when><value>0</value><interpolation><name>linear</name></interpolation></keyframe>"
        "<keyframe><when>25</when><value>15</value><interpolation><name>hold</name></interpolation></keyframe>"
        "</parameter></effect></filter>"))
    audio = _item("a1", "Audio Only", 24, 48, 0, 24, _file("f2", a["path"], tb=24), tb=24, extra=(
        "<filter><effect><effectid>audiolevels</effectid>"
        "<keyframe><when>1</when><value>1.0</value><interpolation><name>linear</name></interpolation></keyframe>"
        "<keyframe><when>13</when><value>0.25</value><interpolation><name>hold</name></interpolation></keyframe>"
        "</effect></filter>"))
    path = _xml(tmp_path, {"video": f"<track>{video}</track>",
                           "audio": f"<track><locked>TRUE</locked>{audio}</track>"}, tb=24)
    p = _plan_xml(path, [v, a], info=info(fps=(24, 1)))
    by = {c.title: c for c in p.clips}
    vc, ac = by["Video Clip"], by["Audio Only"]
    assert (vc.position, vc.start, vc.end) == pytest.approx((1.0, 0.5, 2.5))
    alpha = vc.props["alpha"]["Points"]
    assert [(q["co"]["X"], q["co"]["Y"], q["interpolation"]) for q in alpha] == [(13.0, 1.0, imp.LINEAR),
                                                                                (37.0, 0.5, imp.BEZIER)]
    rot = vc.props["rotation"]["Points"]
    assert [(q["co"]["X"], q["co"]["Y"], q["interpolation"]) for q in rot] == [(13.0, 0.0, imp.LINEAR),
                                                                              (25.0, 15.0, imp.CONSTANT)]
    assert pts(vc.props["location_x"]) == [(1.0, 0.05)] and pts(vc.props["location_y"]) == [(1.0, -0.05)]
    assert pts(vc.props["scale_x"]) == [(1.0, 1.25)]
    vol = ac.props["volume"]["Points"]
    assert [(q["co"]["X"], q["co"]["Y"], q["interpolation"]) for q in vol] == [(1.0, 1.0, imp.LINEAR),
                                                                              (13.0, 0.25, imp.CONSTANT)]
    assert {t.key: t.locked for t in p.tracks}["A1"] is True


def test_assets_pathurls_resolve_through_the_project(tmp_path, monkeypatch):
    from classes import path_utils
    monkeypatch.setattr(path_utils, "absolute_media_path", lambda raw: "/projects/Trip_assets/" + raw[len("@assets/"):])
    assert imp.path_from_pathurl("@assets/renders/intro.mov") == "/projects/Trip_assets/renders/intro.mov"
    assert imp.path_from_pathurl("file:///tmp/My%20Clip.mov") == os.path.normpath("/tmp/My Clip.mov")
    assert imp.path_from_pathurl("media/clip.mov", str(tmp_path)) == os.path.join(str(tmp_path), "media", "clip.mov")


def test_a_multi_sequence_xml_imports_one_and_names_the_others(tmp_path):
    (m,) = media_on_disk(tmp_path, video_file("", "/m/a.mp4", duration=20.0, fps=(25, 1)))
    f = _file("f1", m["path"], video=True)

    def seq(sid, name, body):
        return (f"<sequence id='{sid}'><name>{name}</name><duration>100</duration><rate><timebase>25</timebase>"
                f"<ntsc>FALSE</ntsc></rate><media><video><track>{body}</track></video><audio/></media></sequence>")

    nest = seq("s3", "Nest", _item("n1", "Inside", 0, 50, 0, 50, "<file id='f1'/>"))
    uses_nest = ("<clipitem id='u1'><name>Nest</name><start>50</start><end>100</end><in>0</in><out>50</out>"
                 f"{nest}</clipitem>")
    text = ('<?xml version="1.0"?>\n<!DOCTYPE xmeml>\n<xmeml version="4"><project><name>P</name><children>'
            + seq("s1", "Main", _item("c1", "First", 0, 50, 0, 50, f) + uses_nest)
            + seq("s2", "Alt cut", _item("c2", "Second", 0, 75, 10, 85, "<file id='f1'/>"))
            + "</children></project></xmeml>")
    path = tmp_path / "Project.xml"
    path.write_text(text, encoding="utf-8")
    p = _plan_xml(str(path), [m])
    assert p.sequence_name == "Main" and {c.title for c in p.clips} == {"First", "Inside"}
    assert any("'Alt cut'" in w and "only 'Main'" in w for w in p.warnings)
    assert not any("'Nest'" in w and "holds" in w for w in p.warnings)   # a nest is not a separate sequence
    alt = _plan_xml(str(path), [m], sequence="alt CUT")
    assert alt.sequence_name == "Alt cut" and [c.title for c in alt.clips] == ["Second"]
    with pytest.raises(imp.XmlImportError, match="no sequence named 'Missing'.*'Main', 'Alt cut'"):
        _plan_xml(str(path), [m], sequence="Missing")


def test_image_sequences_are_reported(tmp_path):
    (still,) = media_on_disk(tmp_path, image_file("", "/m/shot_000101.tif", width=1920, height=1080))
    (logo,) = media_on_disk(tmp_path, image_file("", "/m/logo 2024.png", width=800, height=400))
    seq_file = _file("f1", still["path"], duration=300, channels=0, video=True)
    logo_file = _file("f2", logo["path"], duration=1080000, channels=0, video=True)
    body = (_item("c1", "Plate", 0, 100, 0, 100, seq_file) +
            _item("c2", "Logo", 100, 150, 90000, 90050, logo_file))
    p = _plan_xml(_xml(tmp_path, {"video": f"<track>{body}</track>"}), [still, logo])
    (w,) = [w for w in p.warnings if "image sequence" in w]
    assert w.startswith("1 image sequence(s)") and "'shot_000101.tif'" in w and "logo" not in w


def test_a_title_rendered_bigger_comes_back_the_same_size(tmp_path):
    from classes.exporters import final_cut_pro as fcp
    from classes.handoff.timeline_view import TimelineSnapshot
    from classes.handoff.transform import clip_geometry
    title = {"id": "T1", "path": str(tmp_path / "Launch.svg"), "name": "Launch.svg", "media_type": "image",
             "has_video": True, "has_audio": False, "width": 1920, "height": 1080, "duration": 3600.0,
             "has_single_image": True}
    big = clip("Launch", "T1", end=3.0, scale_x=kf([(1, 1.0), (90, 2.0)]), scale_y=kf([(1, 1.0), (90, 2.0)]),
               location_x=const(0.1))
    proj = project(files=[title], clips=[big])
    before = snapshot(proj)
    out = fcp.export_timeline(before, str(tmp_path / "T.xml"), render_stills=FakeStills())
    (png,) = out.stills
    probe = FakeMediaProbe({png: image_file("", png, width=3840, height=2160)})
    p = imp.plan_import(out.path, info=info(fps=(30, 1)), probe=probe)
    (c,) = p.clips
    files = [image_file("P1", png, width=3840, height=2160)]
    back = TimelineSnapshot.from_project(project(files=files, clips=[dict(c.props, id="B", file_id="P1", layer=L1,
                                                                          position=c.position, start=c.start,
                                                                          end=c.end, duration=3600.0,
                                                                          title=c.title)]))
    a, b = before.clips[0], back.clips[0]
    for t in (0.0, 1.0, 2.9):
        ga, gb = clip_geometry(a, t, 1920, 1080), clip_geometry(b, t, 1920, 1080)
        for key in ("center_x", "center_y", "width", "height"):
            assert getattr(gb, key) == pytest.approx(getattr(ga, key), abs=0.5), (t, key)
