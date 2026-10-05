"""classes.handoff.timeline_view: the read-only project snapshot exporters use."""

import copy
import json
import os
from fractions import Fraction

import pytest

from classes.handoff.timeline_view import TimelineSnapshot, speed_from_time

_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "editor_tools")


def _load(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


def _kf(*pts, interp=1):
    return {"Points": [{"co": {"X": x, "Y": y}, "interpolation": interp} for x, y in pts]}


def _project(tmp_path):
    files = _load("files.json")
    video = dict(files["video"], id="FV", path=str(tmp_path / "a.mp4"), name="Beach")
    audio = dict(files["audio"], id="FA", path="music/song.mp3")
    title = dict(files["image"], id="FT", path=str(tmp_path / "Title.svg"), media_type="image")
    linked = dict(files["video"], id="FL", path=str(tmp_path / "links" / "Intro-3f9a1c2b.mov"),
                  zenvi_link={"version": 1, "kind": "remotion", "source": {"composition": "Intro"},
                              "props": {"title": "Hi"}, "render": {}, "state": "fresh", "custom": {"x": 1}})
    base = _load("clip.json")

    def clip(cid, fid, layer, position, start, end, **over):
        c = copy.deepcopy(base)
        c.update(id=cid, file_id=fid, layer=layer, position=position, start=start, end=end, effects=[])
        c.update(over)
        return c

    effects_fixture = _load("effects.json")
    blur_cls = next(iter(effects_fixture))
    effect = dict(effects_fixture[blur_cls], id="E1")
    return {
        "fps": {"num": 30, "den": 1}, "width": 1920, "height": 1080,
        "pixel_ratio": {"num": 1, "den": 1}, "display_ratio": {"num": 16, "den": 9},
        "sample_rate": 44100, "channels": 2, "channel_layout": 3, "duration": 300, "profile": "HD 1080p 30 fps",
        "layers": [{"id": "L2", "number": 2000000, "label": "Titles", "lock": True},
                   {"id": "L1", "number": 1000000, "label": "", "lock": False}],
        "files": [video, audio, title, linked],
        "clips": [
            clip("C2", "FV", 1000000, 5.0, 1.0, 4.0, time=_kf((31, 61), (121, 240))),  # 2x over its window
            clip("C1", "FV", 1000000, 0.0, 2.0, 5.0, effects=[effect], alpha=_kf((61, 0.0), (76, 1.0))),
            clip("C3", "FT", 2000000, 1.0, 0.0, 3.0),
            clip("C4", "FA", 1000000, 8.0, 0.0, 2.0),
            clip("C5", "FL", 2000000, 6.0, 0.0, 5.0),
        ],
        "effects": [{"id": "T1", "layer": 1000000, "position": 4.5, "start": 0, "end": 1.0, "type": "Mask",
                     "title": "Wipe", "reader": {"path": "@transitions/common/circle.svg"},
                     "brightness": _kf((1, -1.0), (31, 1.0)), "contrast": _kf((1, 3.0))}],
        "markers": [{"id": "M2", "position": 9.0, "vector": "red", "name": "End"},
                    {"id": "M1", "position": 2.0, "icon": "green.png"}],
    }


def test_project_settings_and_tracks_bottom_to_top(tmp_path):
    snap = TimelineSnapshot.from_project(_project(tmp_path), str(tmp_path / "trip.zvn"))
    assert snap.fps == Fraction(30) and (snap.width, snap.height) == (1920, 1080)
    assert snap.sample_rate == 44100 and snap.pixel_aspect == 1 and snap.display_ratio == Fraction(16, 9)
    assert snap.name == "trip" and snap.timeline_length == 300
    assert [t.number for t in snap.tracks] == [1000000, 2000000]
    assert snap.tracks[1].label == "Titles" and snap.tracks[1].locked and snap.tracks[0].name == "Track 1"
    assert [c.id for c in snap.tracks[0].clips] == ["C1", "C2", "C4"]
    assert {c.track_index for c in snap.tracks[1].clips} == {1}
    assert snap.duration == pytest.approx(11.0)  # C5 ends at 6 + 5


def test_clip_timing_curves_and_effects(tmp_path):
    snap = TimelineSnapshot.from_project(_project(tmp_path), str(tmp_path / "trip.zvn"))
    c1 = snap.clip("C1")
    assert (c1.timeline_in, c1.timeline_out, c1.duration) == (0.0, 3.0, 3.0)
    assert c1.frame_range() == (61, 150)
    alpha = c1.curves["alpha"]
    assert alpha.points[0].time == pytest.approx(0.0) and alpha.points[1].time == pytest.approx(0.5)
    assert alpha.value_at(0.25) == pytest.approx(0.5)
    assert c1.curve("rotation").value_at(1.0) == 0.0
    assert len(c1.effects) == 1 and c1.effects[0].id == "E1" and c1.effects[0].class_name
    assert any(isinstance(v, type(alpha)) for v in c1.effects[0].params.values())
    assert c1.speed.kind == "normal"
    c2 = snap.clip("C2")
    assert c2.speed.kind == "constant" and c2.speed.factor == pytest.approx(2.0) and not c2.speed.reversed
    assert c2.time is not None and (c2.source_in, c2.source_out) == (pytest.approx(2.0), pytest.approx(238 / 30))
    assert c1.time is None and (c1.source_in, c1.source_out) == (2.0, 5.0)


def test_files_resolve_paths_and_flags(tmp_path):
    snap = TimelineSnapshot.from_project(_project(tmp_path), str(tmp_path / "trip.zvn"))
    assert snap.file("FA").path == os.path.normpath(str(tmp_path / "music" / "song.mp3"))
    assert snap.file("FT").is_title and not snap.file("FV").is_title
    assert snap.file("FV").name == "Beach" and snap.file("FV").fps == Fraction(30)
    linked = snap.file("FL")
    assert linked.is_linked and linked.zenvi_link["custom"] == {"x": 1}
    assert snap.linked_files() == (linked,) and snap.clip("C5").is_linked
    assert [f.id for f in snap.used_files()] == ["FV", "FT", "FL", "FA"]
    with pytest.raises(TypeError):
        linked.zenvi_link["kind"] = "x"  # read-only


def test_transitions_and_markers(tmp_path):
    snap = TimelineSnapshot.from_project(_project(tmp_path), str(tmp_path / "trip.zvn"))
    (tr,) = snap.transitions
    assert (tr.position, tr.duration, tr.end, tr.track_index) == (4.5, 1.0, 5.5, 0)
    assert tr.reversed and tr.brightness.value_at(5.5) == 1.0 and tr.contrast.first_value == 3.0
    assert tr.mask_path.endswith(os.path.join("transitions", "common", "circle.svg"))
    assert [(m.id, m.time, m.color) for m in snap.markers] == [("M1", 2.0, "green"), ("M2", 9.0, "red")]


def test_snapshot_is_detached_from_the_project(tmp_path):
    project = _project(tmp_path)
    snap = TimelineSnapshot.from_project(project, str(tmp_path / "trip.zvn"))
    project["clips"][0]["position"] = 99.0
    project["files"][3]["zenvi_link"]["kind"] = "hyperframes"
    assert snap.clip("C2").position == 5.0 and snap.file("FL").zenvi_link["kind"] == "remotion"


def test_stray_layers_and_missing_files_do_not_drop_clips(tmp_path):
    project = _project(tmp_path)
    reader = dict(project["files"][0], path=str(tmp_path / "gone.mp4"))
    project["clips"].append(dict(project["clips"][0], id="CX", layer=3000000, file_id="GONE", reader=reader))
    project["clips"].append(dict(project["clips"][0], id="CY", layer=3000000, file_id="GONE2", position=20.0))
    snap = TimelineSnapshot.from_project(project, str(tmp_path / "trip.zvn"))
    assert snap.tracks[-1].number == 3000000
    assert snap.clip("CX").file.path == str(tmp_path / "gone.mp4")  # from the clip's own reader
    assert snap.clip("CY").file is None


@pytest.mark.parametrize("points,interp,expected", [
    (None, 1, ("normal", 1.0, False)),
    ([(1, 1)], 1, ("normal", 1.0, False)),
    ([(1, 1), (151, 300)], 1, ("constant", 2.0, False)),
    ([(1, 300), (300, 1)], 1, ("normal", 1.0, True)),
    ([(1, 300), (150, 1)], 1, ("constant", 2.0, True)),
    ([(1, 40), (90, 40)], 1, ("freeze", 0.0, False)),
    ([(1, 1), (31, 31), (61, 31), (91, 61)], 1, ("variable", None, False)),
    ([(1, 1), (151, 300)], 0, ("variable", None, False)),
])
def test_speed_from_time_curves(points, interp, expected):
    kf = None if points is None else _kf(*points, interp=interp)
    info = speed_from_time(kf)
    kind, factor, rev = expected
    if kind == "normal" and rev:
        kind = "constant"
    assert info.kind == kind and info.reversed == rev
    if factor is None:
        assert info.factor is None
    else:
        assert info.factor == pytest.approx(factor, rel=0.01)
    assert speed_from_time(kf, repeat_active=True).kind == "variable"


def _retimed_snapshot(tmp_path, start, end, time_points, interp=1):
    project = _project(tmp_path)
    project["clips"] = [dict(project["clips"][1], id="R", start=start, end=end, time=_kf(*time_points, interp=interp),
                             alpha=_kf((1, 1.0)))]
    return TimelineSnapshot.from_project(project, str(tmp_path / "trip.zvn")).clip("R")


def test_time_remapped_clip_reports_the_source_it_shows(tmp_path):
    # review case: start 1.0, end 5.0, time (1,1)->(151,300): libopenshot shows source frames 61..298
    clip = _retimed_snapshot(tmp_path, 1.0, 5.0, [(1, 1), (151, 300)])
    assert clip.frame_range() == (31, 150)
    assert (clip.source_in, clip.source_out) == (pytest.approx(2.0), pytest.approx(298 / 30))
    assert clip.speed.kind == "constant" and clip.speed.factor == pytest.approx(2.0)
    assert clip.source_frame_at(clip.position) == 61 and clip.source_time_at(clip.position) == pytest.approx(2.0)
    assert clip.source_frame_at(clip.position + 1.0) == 121  # one timeline second plays two source seconds


def test_a_clip_extended_past_its_time_curve_holds_and_is_variable(tmp_path):
    clip = _retimed_snapshot(tmp_path, 1.0, 6.0, [(1, 1), (151, 300)])  # frames 152..180 hold source 300
    assert clip.speed.kind == "variable"
    assert clip.source_out == pytest.approx(10.0)
    assert clip.source_frame_at(clip.position + 5.0 - 1 / 30) == 300


def test_reversed_and_held_windows(tmp_path):
    # Speed > Reverse builds X over [start_x, end_x + 1): (1,150)->(151,1) for 150 frames
    rev = _retimed_snapshot(tmp_path, 0.0, 5.0, [(1, 150), (151, 1)])
    assert rev.speed.kind == "constant" and rev.speed.reversed and rev.speed.factor == pytest.approx(1.0)
    assert (rev.source_in, rev.source_out) == (pytest.approx(1 / 30), pytest.approx(5.0))  # frames 150 down to 2
    held = _retimed_snapshot(tmp_path, 0.0, 2.0, [(1, 40), (90, 40)])
    assert held.speed.kind == "freeze" and (held.source_in, held.source_out) == (pytest.approx(39 / 30),
                                                                                pytest.approx(40 / 30))
    eased = _retimed_snapshot(tmp_path, 0.0, 5.0, [(1, 1), (151, 300)], interp=0)
    assert eased.speed.kind == "variable" and eased.time is not None


def test_snapshot_shares_waveform_samples_instead_of_copying_them(tmp_path):
    project = _project(tmp_path)
    samples = [0.5] * 20000
    project["clips"][0]["ui"] = {"audio_data": samples, "other": {"x": 1}}
    snap = TimelineSnapshot.from_project(project, str(tmp_path / "trip.zvn"))
    data = snap.clip(project["clips"][0]["id"]).data
    assert data["ui"]["audio_data"] is samples           # shared (replaced wholesale, never edited in place)
    assert data["ui"]["other"] is not project["clips"][0]["ui"]["other"]  # everything else is copied
