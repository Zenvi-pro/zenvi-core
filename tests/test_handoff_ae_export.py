"""Zenvi -> After Effects script generator (classes.exporters.after_effects).

Projects come from ``ae_project.ProjectBuilder`` (libopenshot-shaped clips).
Animated properties are checked by evaluating the exported keys the way
After Effects interpolates them and comparing with the exact libopenshot
placement at every frame the clip shows.
"""

import json
import math

import pytest

from ae_project import CONSTANT, LINEAR, ProjectBuilder, const, kf, point
from classes.exporters import after_effects as AE
from classes.exporters import after_effects_keys as K
from classes.exporters.after_effects_effects import AE_BLUR_SIZE_TO_SIGMA
from classes.exporters.after_effects_js import es3_problems
from classes.exporters.after_effects_titles import parse_title_svg
from classes.handoff import transform as T

FADE = "/zenvi/transitions/common/fade.svg"
WIPE = "/zenvi/transitions/common/wipe_left_to_right.svg"


def build(b, *, media=None, titles=None, masks=None, project_path="/projects/Trip.zvn", **options):
    snap = b.snapshot(project_path)
    if media is None:
        media = {f["id"]: AE.MediaRef(abs=f["path"]) for f in b.data["files"]}
    if masks is None:
        masks = {FADE: AE.MaskAsset("uniform", gray=0),
                 WIPE: AE.MaskAsset("image", image=AE.MediaRef(abs="/export/masks/wipe.png", rel="masks/wipe.png"),
                                    width=1920, height=1080)}
    opts = AE.AeExportOptions(generator="Zenvi test", **options)
    return AE.build_ae_script(snap, media_map=media, title_assets=titles or {}, mask_assets=masks, options=opts), snap


def layer(out, name=None, index=None):
    layers = out.data["layers"]
    if index is not None:
        return layers[index]
    return next(L for L in layers if L["name"] == name)


def track_of(spec):
    """A K.Track from an exported keyframe spec (None for a static value)."""
    if not isinstance(spec, dict) or "t" not in spec:
        return None
    values = [tuple(v) if isinstance(v, list) else (v,) for v in spec["v"]]
    dims = len(values[0])
    n = len(spec["t"]) - 1
    lin = [[(0.0, K.LINEAR_INFLUENCE)] * dims for _ in range(n)]
    outs = [[(s, i / 100.0) for s, i in span] for span in spec["o"]] if "o" in spec else lin
    ins = [[(s, i / 100.0) for s, i in span] for span in spec["n"]] if "n" in spec else lin
    return K.Track(spec["t"], values, spec["i"], outs, ins, spatial=bool(spec.get("sp")))


def value_at(spec, t):
    track = track_of(spec)
    if track is None:
        return tuple(spec) if isinstance(spec, list) else (spec,)
    return K.track_value(track, t)


def exact_pose(snap, clip_id, t, *, src=None):
    """Exact AE anchor/position/scale/rotation/opacity of a clip at t (C1 geometry + exact curves)."""
    clip = snap.clip(clip_id)
    keys = ("alpha", "location_x", "location_y", "scale_x", "scale_y", "rotation", "origin_x", "origin_y", "margin")
    v = {k: K.exact_value(clip.curve(k), t) for k in keys}
    fw, fh = clip.file.width, clip.file.height
    g = T.geometry(fw, fh, snap.width, snap.height, scale_mode=clip.scale_mode, gravity=clip.gravity,
                   scale_x=v["scale_x"], scale_y=v["scale_y"], location_x=v["location_x"],
                   location_y=v["location_y"], rotation=v["rotation"], origin_x=v["origin_x"],
                   origin_y=v["origin_y"], alpha=v["alpha"], margin=v["margin"])
    sw, sh = src or (fw, fh)
    return {"anchor": (v["origin_x"] * sw, v["origin_y"] * sh), "pos": (g.anchor_x, g.anchor_y),
            "scale": (g.scale_x * 100 * fw / sw, g.scale_y * 100 * fh / sh), "rot": (g.rotation,),
            "op": (min(1.0, max(0.0, v["alpha"])) * 100,)}


def position_at(tf, t):
    pos = tf["pos"]
    if isinstance(pos, dict) and pos.get("sep"):
        return value_at(pos["x"], t)[0], value_at(pos["y"], t)[0]
    return value_at(pos, t)


def assert_follows(out, snap, clip_id, name, *, tol=0.02, props=("anchor", "pos", "scale", "rot", "op")):
    clip = snap.clip(clip_id)
    tf = layer(out, name)["tf"]
    for t in K.frame_times(clip.timeline_in, clip.timeline_out, snap.fps_float):
        want = exact_pose(snap, clip_id, t)
        for prop in props:
            got = position_at(tf, t) if prop == "pos" else value_at(tf[prop], t)
            for g, w in zip(got, want[prop]):
                assert g == pytest.approx(w, abs=tol), f"{prop} at {t:.4f}: {got} != {want[prop]}"


# ---------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------

def _basic():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/beach.mp4")
    c = b.add_clip(v, track=1, position=2.0, start=1.0, end=6.0)
    return b, v, c


def test_script_shape_metadata_and_es3():
    b, _, _ = _basic()
    out, _ = build(b)
    lines = out.jsx.split("\n")
    assert lines[0] == "// Zenvi → After Effects export v1"
    assert lines[1] == "#target aftereffects"
    assert "(function () {" in lines and lines[-2] == "}());"
    assert "    return zenviBuild(ZENVI_EXPORT, DATA);" in lines
    assert 'app.beginUndoGroup("Import Zenvi project")' in out.jsx
    assert out.jsx.count("app.beginUndoGroup(") == 1 and out.jsx.count("app.endUndoGroup()") == 1
    assert '"folder": "Zenvi \\u2014 Trip"' in out.jsx
    assert '"version": 1' in out.jsx and '"project": "Trip"' in out.jsx and "var ZENVI_EXPORT = {" in out.jsx
    assert es3_problems(out.jsx) == []
    assert all(ord(ch) < 127 for ch in "\n".join(lines[1:]))
    data = json.loads(out.jsx.split("    var DATA = ", 1)[1].split(";\n", 1)[0])
    assert data == json.loads(json.dumps(out.data))


@pytest.mark.parametrize("code, problem", [
    ("var f = (a) => a;", "arrow"),
    ("let x = 1;", "ES2015"),
    ("const x = 1;", "ES2015"),
    ("var s = `x`;", "template"),
    ("var a = [1, 2,];", "trailing comma"),
    ("var o = {a: 1,\n};", "trailing comma"),
    ("x.default = 1;", "reserved word as a property"),
    ("var o = {class: 1};", "reserved word as an object key"),
    ("var o = {int: 1};", "reserved word as an object key"),
    ("f(...args);", "spread"),
    ("// ok\nvar s = \"café\";", "non-ASCII"),
])
def test_es3_lint_names_each_problem(code, problem):
    found = es3_problems(code)
    assert any(problem in p for p in found), found


def test_es3_lint_ignores_strings_comments_and_directives():
    code = '// Zenvi → header\n#target aftereffects\nvar s = "a => b, let x"; /* const y = `z` */ x["default"] = 1.5;'
    assert es3_problems(code) == []


def test_comp_matches_the_project():
    b = ProjectBuilder(fps=(30000, 1001), width=3840, height=2160)
    v = b.add_file("video", path="/media/a.mp4")
    b.add_clip(v, track=1, position=0.0, start=0.0, end=5.0)
    b.data["pixel_ratio"] = {"num": 1, "den": 1}
    out, _ = build(b)
    comp = out.data["comp"]
    assert (comp["w"], comp["h"], comp["par"], comp["bg"]) == (3840, 2160, 1, [0, 0, 0])
    assert comp["fps"] == pytest.approx(29.97002997)
    assert comp["dur"] >= 5.0 and comp["name"] == "Trip"


def test_an_empty_timeline_is_refused():
    with pytest.raises(AE.AeExportError, match="no clips"):
        build(ProjectBuilder())


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def test_trimmed_clip_starts_its_source_at_the_in_point():
    b, _, _ = _basic()
    out, _ = build(b)
    L = layer(out, index=0)
    assert (L["inp"], L["outp"], L["start"], L["stretch"]) == (2.0, 7.0, 1.0, 100)
    assert "remap" not in L


def _speed_curve(b, cid, start_frame, speed, frames):
    """The time curve Clip > Speed writes (timeline.Time_Triggered): X0 -> X0 + D/speed, Y0 -> Y0 + D."""
    end_x = start_frame + frames / speed
    b.clip(cid)["time"] = kf((start_frame, start_frame, LINEAR), (end_x, start_frame + frames, LINEAR))
    b.clip(cid)["end"] = end_x / 30.0


@pytest.mark.parametrize("speed, stretch", [(2.0, 50.0), (0.5, 200.0), (4.0, 25.0)])
def test_constant_speed_is_a_time_stretch(speed, stretch):
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/long.mp4", duration=60.0)
    c = b.add_clip(v, track=1, position=3.0, start=0.0, end=10.0)
    _speed_curve(b, c, 1, speed, 300)
    out, snap = build(b)
    L = layer(out, index=0)
    assert L["stretch"] == pytest.approx(stretch) and "remap" not in L
    # source time shown at comp time t is (t - startTime) * 100 / stretch
    clip = snap.clip(c)
    tc = AE._time_curve(clip)
    for t in K.frame_times(L["inp"], L["outp"], 30.0):
        want = (K.exact_value(tc, t) - 1.0) / 30.0
        assert (t - L["start"]) * 100.0 / L["stretch"] == pytest.approx(want, abs=1e-6)


def test_speed_with_a_trimmed_start_places_the_right_source_frame():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/long.mp4", duration=60.0)
    c = b.add_clip(v, track=1, position=0.0, start=1.0, end=5.0)
    b.clip(c)["time"] = kf((1, 1, LINEAR), (151, 300, LINEAR))  # the C1 review example
    out, snap = build(b)
    L = layer(out, index=0)
    tc = AE._time_curve(snap.clip(c))
    first = (K.exact_value(tc, 0.0) - 1.0) / 30.0
    assert first == pytest.approx((1 + 30 * 299 / 150 - 1) / 30.0)  # ~2.0 s, not the 1.0 s 'start'
    assert (L["inp"] - L["start"]) * 100.0 / L["stretch"] == pytest.approx(first, abs=1e-6)


def _remap_values(L, frames):
    return [value_at(L["remap"], t)[0] for t in frames]


def test_reverse_is_time_remapped():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/long.mp4", duration=60.0)
    c = b.add_clip(v, track=1, position=0.0, start=0.0, end=10.0)
    b.clip(c)["time"] = kf((1, 301, LINEAR), (301, 1, LINEAR))
    out, snap = build(b)
    L = layer(out, index=0)
    assert L["remap"]["i"] == ["l"] and L["start"] == 0.0 and L["stretch"] == 100
    tc = AE._time_curve(snap.clip(c))
    frames = K.frame_times(0.0, 10.0, 30.0)
    for t, got in zip(frames, _remap_values(L, frames)):
        assert got == pytest.approx((K.exact_value(tc, t) - 1) / 30.0, abs=1e-6)


def test_a_freeze_is_one_time_remap_key():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/long.mp4", duration=60.0)
    c = b.add_clip(v, track=1, position=1.0, start=0.0, end=3.0)
    b.clip(c)["time"] = kf((1, 46, CONSTANT), (91, 46, CONSTANT))
    out, _ = build(b)
    remap = layer(out, index=0)["remap"]
    assert remap["t"] == [1.0] and remap["v"] == [pytest.approx(1.5)] and remap["i"] == []


def test_holding_past_the_curves_last_point_and_ramps_are_keyed_exactly():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/long.mp4", duration=60.0)
    c = b.add_clip(v, track=1, position=0.0, start=0.0, end=8.0)
    # an eased ramp to frame 151 at X=91, then the clip runs on past the curve's last point (a hold)
    b.clip(c)["time"] = {"Points": [point(1, 1), point(91, 151, 0, (0.5, 1.0), (0.2, 0.0))]}
    out, snap = build(b)
    L = layer(out, index=0)
    assert "remap" in L and L["remap"]["i"][0] == "b"
    tc = AE._time_curve(snap.clip(c))
    frames = K.frame_times(0.0, 8.0, 30.0)
    for t, got in zip(frames, _remap_values(L, frames)):
        assert got == pytest.approx((K.exact_value(tc, t) - 1) / 30.0, abs=1e-5)
    assert _remap_values(L, [7.9])[0] == pytest.approx(150 / 30.0)


def test_a_speed_curve_past_the_media_end_holds_the_last_frame_and_warns():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/short.mp4", duration=4.0)
    c = b.add_clip(v, track=1, position=0.0, start=0.0, end=4.0)
    b.clip(c)["time"] = kf((1, 1, LINEAR), (121, 241, LINEAR))
    out, _ = build(b)
    assert any("past the end" in w for w in out.warnings)
    values = _remap_values(layer(out, index=0), K.frame_times(0.0, 4.0, 30.0))
    assert max(values) <= 4.0 - 1 / 30.0 + 1e-6


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scale_mode", [T.SCALE_FIT, T.SCALE_CROP, T.SCALE_STRETCH, T.SCALE_NONE])
@pytest.mark.parametrize("gravity", [T.GRAVITY_CENTER, T.GRAVITY_TOP_LEFT, T.GRAVITY_BOTTOM_RIGHT, T.GRAVITY_RIGHT])
def test_static_placement_follows_libopenshot_geometry(scale_mode, gravity):
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/clip.mp4")  # 1280x720 on 1920x1080
    c = b.add_clip(v, track=1, position=0.0, start=0.0, end=3.0, scale=scale_mode, gravity=gravity,
                   location_x=const(0.1), location_y=const(-0.05), scale_x=const(0.8), scale_y=const(0.6),
                   rotation=const(12.0), alpha=const(0.75), origin_x=const(0.3), origin_y=const(0.7))
    out, snap = build(b)
    tf = layer(out, index=0)["tf"]
    want = exact_pose(snap, c, 0.0)
    assert tuple(tf["anchor"]) == pytest.approx(want["anchor"], abs=1e-4)
    assert tuple(tf["pos"]) == pytest.approx(want["pos"], abs=1e-4)
    assert tuple(tf["scale"]) == pytest.approx(want["scale"], abs=1e-4)
    assert tf["rot"] == pytest.approx(12.0) and tf["op"] == pytest.approx(75.0)


def test_eased_moves_keep_the_curves_eases():
    b = ProjectBuilder()
    img = b.add_file("image", path="/media/photo.jpg")
    c = b.add_clip(img, track=1, position=1.0, start=0.0, end=4.0, location_x=kf((1, -0.5), (31, 0.0)),
                   location_y=kf((1, 0.2), (31, 0.0)), alpha=kf((1, 0.0), (16, 1.0)))
    out, snap = build(b)
    tf = layer(out, index=0)["tf"]
    assert tf["pos"]["sp"] == 1 and tf["pos"]["i"] == ["b"] and len(tf["pos"]["t"]) == 2
    assert tf["pos"]["o"][0][0][1] == pytest.approx(50.0)  # the editor's default handles: 50 % influence
    assert tf["op"]["i"] == ["b"] and tf["op"]["t"] == [1.0, 1.5]
    assert_follows(out, snap, c, "photo.jpg")


def test_x_and_y_with_different_eases_are_separated_dimensions():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/clip.mp4")
    c = b.add_clip(v, track=1, position=0.0, start=0.0, end=3.0,
                   location_x={"Points": [point(1, 0.0), point(31, 0.3, 0, (0.5, 1.0), (0.1, 1.0))]},
                   location_y={"Points": [point(1, 0.0), point(46, -0.2, 0, (0.5, 1.0), (0.9, 0.0))]})
    out, snap = build(b)
    pos = layer(out, index=0)["tf"]["pos"]
    assert pos["sep"] == 1 and pos["x"]["t"] == [0.0, 1.0] and pos["y"]["t"] == [0.0, 1.5]
    assert_follows(out, snap, c, "clip.mp4", props=("pos",))


@pytest.mark.parametrize("overrides", [
    {"gravity": T.GRAVITY_TOP_LEFT},
    {"origin_x": const(0.25), "origin_y": const(0.9)},
    {"scale": T.SCALE_CROP, "gravity": T.GRAVITY_BOTTOM},
])
def test_positions_that_depend_on_scale_too_stay_exact(overrides):
    """Non-centre gravity, origin != 0.5 and crop mode: position is not one curve's function (C1 review)."""
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/clip.mp4")
    kw = dict(location_x=kf((1, -0.2), (31, 0.15)), scale_x=kf((1, 0.5), (46, 1.2)), scale_y=kf((1, 0.5), (46, 1.2)))
    kw.update(overrides)
    c = b.add_clip(v, track=1, position=0.0, start=0.0, end=3.0, **kw)
    out, snap = build(b)
    assert_follows(out, snap, c, "clip.mp4", tol=AE.TOL_PX + 1e-9)


def test_a_clip_trimmed_through_an_animation_is_cut_exactly_at_its_in_point():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/clip.mp4")
    # the animation runs over source frames 1..61; the clip shows from source second 1.0
    c = b.add_clip(v, track=1, position=4.0, start=1.0, end=5.0, rotation=kf((1, 0.0), (61, 90.0)),
                   scale_x=kf((1, 1.0), (21, 1.4), (61, 1.0)), scale_y=kf((1, 1.0), (41, 0.6), (61, 1.0)))
    out, snap = build(b)
    tf = layer(out, index=0)["tf"]
    assert tf["rot"]["t"][0] == pytest.approx(4.0) and tf["rot"]["i"] == ["b"]
    assert tf["scale"]["t"][0] == pytest.approx(4.0)
    assert_follows(out, snap, c, "clip.mp4", tol=1e-4, props=("rot", "scale"))


def test_shear_warns():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/clip.mp4")
    b.add_clip(v, track=1, position=0.0, start=0.0, end=2.0, shear_x=const(0.2))
    out, _ = build(b)
    assert any("shear" in w for w in out.warnings)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def test_volume_becomes_audio_levels_in_decibels():
    b = ProjectBuilder()
    a = b.add_file("audio", path="/media/music.mp3")
    half = b.add_clip(a, track=1, position=0.0, start=0.0, end=4.0, volume=const(0.5))
    full = b.add_clip(a, track=2, position=0.0, start=0.0, end=4.0)
    mute = b.add_clip(a, track=3, position=0.0, start=0.0, end=4.0, volume=const(0.0))
    out, snap = build(b)
    by_track = {L["comment"].split(" on track ")[1][0]: L for L in out.data["layers"]}
    assert by_track["1"]["levels"] == [pytest.approx(-6.0206, abs=1e-4)] * 2
    assert "levels" not in by_track["2"]
    assert by_track["3"]["levels"] == [-96, -96]
    assert all(L["kind"] == "audio" and "tf" not in L for L in out.data["layers"])
    assert snap.clip(half) and snap.clip(full) and snap.clip(mute)


def test_volume_fades_are_keyed_within_a_tenth_of_a_decibel():
    b = ProjectBuilder()
    a = b.add_file("audio", path="/media/music.mp3")
    c = b.add_clip(a, track=1, position=0.0, start=0.0, end=6.0, volume=kf((1, 0.0), (31, 1.0), (151, 1.0), (181, 0.2)))
    out, snap = build(b)
    levels = layer(out, index=0)["levels"]
    vol = snap.clip(c).curve("volume")
    for t in K.frame_times(0.0, 6.0, 30.0):
        v = K.exact_value(vol, t)
        want = max(AE.DB_FLOOR, 20 * math.log10(v)) if v > 0 else AE.DB_FLOOR
        assert value_at(levels, t)[0] == pytest.approx(want, abs=AE.TOL_DB + 1e-9)


def test_audio_and_video_switches():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/talk.mp4", has_audio=True)
    a = b.add_file("audio", path="/media/music.mp3")
    b.add_clip(v, track=1, position=0.0, start=0.0, end=3.0, has_audio=kf((1, 0.0, CONSTANT)))
    b.add_clip(v, track=2, position=0.0, start=0.0, end=3.0, has_video=kf((1, 0.0, CONSTANT)))
    b.add_clip(a, track=3, position=0.0, start=0.0, end=3.0)
    out, _ = build(b)
    first, second, music = out.data["layers"]
    assert first["audio"] is False and "video" not in first
    assert second["video"] is False and "audio" not in second
    assert music["kind"] == "audio"
    quiet, _ = build(b, include_audio=False)
    assert len(quiet.data["layers"]) == 2 and all(L.get("audio") is False for L in quiet.data["layers"])
    assert any("audio-only clip" in w for w in quiet.warnings)


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

def _title_project():
    b = ProjectBuilder()
    t = b.add_title("/project/titles/Standard_1.svg")
    c = b.add_clip(t, track=2, position=1.0, start=0.0, end=4.0)
    return b, t, c


def test_simple_titles_become_an_editable_precomp():
    b, t, c = _title_project()
    with open(AE_TEMPLATE("Standard_1"), encoding="utf-8") as fh:
        layout = parse_title_svg(fh.read())
    out, snap = build(b, titles={t: AE.TitleAsset("native", layout=layout)})
    comp = out.data["titles"][0]
    assert (comp["id"], comp["w"], comp["h"]) == ("T1", 1920, 1080)
    texts = [i for i in comp["items"] if i["type"] == "text"]
    assert [i["text"] for i in texts] == ["The Title", "Sub-Title"]
    assert texts[0]["just"] == "CENTER_JUSTIFY" and texts[0]["family"] == "DejaVu Sans" and texts[0]["bold"]
    L = layer(out, index=0)
    assert L["src"] == "T1" and L["kind"] == "still" and (L["inp"], L["outp"]) == (1.0, 5.0)
    assert out.titles == [{"title": "Standard_1", "mode": "native",
                           "detail": "editable layers: 2 text layers (faint outline on 'The Title' omitted; "
                                     "faint outline on 'Sub-Title' omitted)"}]
    assert comp["dur"] >= out.data["comp"]["dur"]
    assert_follows(out, snap, c, "Standard_1.svg", props=("pos", "scale"))


def test_complex_titles_are_images_sized_for_the_comp():
    b, t, c = _title_project()
    png = AE.TitleAsset("png", reason="filter", image=AE.MediaRef(abs="/x/titles/Gold.png", rel="titles/Gold.png"),
                        width=3840, height=2160)
    out, snap = build(b, titles={t: png})
    entry = out.data["footage"][0]
    assert entry["title"] and entry["kind"] == "still" and entry["rel"] == "titles/Gold.png"
    tf = layer(out, index=0)["tf"]
    assert tf["scale"] == [pytest.approx(50.0), pytest.approx(50.0)] and tf["anchor"] == [1920, 1080]
    assert out.titles[0]["mode"] == "png" and out.titles[0]["detail"] == "filter"
    assert_follows_png = exact_pose(snap, c, 1.0, src=(3840, 2160))
    assert tuple(tf["pos"]) == pytest.approx(assert_follows_png["pos"])


def test_a_title_without_an_asset_is_skipped_with_a_warning():
    b, _, _ = _title_project()
    v = b.add_file("video", path="/media/a.mp4")
    b.add_clip(v, track=1, position=0.0, start=0.0, end=2.0)
    out, _ = build(b)
    assert len(out.data["layers"]) == 1 and any("Title Standard_1" in w for w in out.warnings)


def AE_TEMPLATE(name):
    import os
    return os.path.join(os.path.dirname(__file__), "..", "src", "titles", name + ".svg")


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------

def _fx_project(class_name, file_kind="video", width=None, **props):
    b = ProjectBuilder()
    extra = {} if width is None else {"width": width, "height": int(width * 9 / 16)}
    v = b.add_file(file_kind, path="/media/fx." + ("jpg" if file_kind == "image" else "mp4"), **extra)
    c = b.add_clip(v, track=1, position=0.0, start=0.0, end=2.0)
    b.add_effect(c, class_name, **props)
    return b, c


def _params(fx, opt=0):
    return {p["ids"][-1]: p["val"] for p in fx["opts"][opt]["params"]}


def test_blur_is_a_gaussian_blur_with_the_box_blur_sigma():
    b, _ = _fx_project("Blur", horizontal_radius=const(6.0), vertical_radius=const(6.0), iterations=const(3.0))
    out, _ = build(b)
    fx = layer(out, index=0)["fx"]
    assert len(fx) == 1 and fx[0]["opts"][0]["match"] == "ADBE Gaussian Blur 2"
    p = _params(fx[0])
    assert p["Blurriness"] == pytest.approx(math.sqrt(3 * 6 * 7 / 3) / AE_BLUR_SIZE_TO_SIGMA)
    assert (p["Blur Dimensions"], p["Repeat Edge Pixels"]) == (1, 1)
    ids = [q["ids"][0] for q in fx[0]["opts"][0]["params"]]
    assert ids == ["ADBE Gaussian Blur 2-0001", "ADBE Gaussian Blur 2-0002", "ADBE Gaussian Blur 2-0003"]


def test_blur_radius_follows_the_decode_size_and_splits_directions():
    b, _ = _fx_project("Blur", width=3840, horizontal_radius=const(6.0), vertical_radius=const(2.0),
                       iterations=const(3.0))
    out, _ = build(b)
    fx = layer(out, index=0)["fx"]
    assert [_params(f)["Blur Dimensions"] for f in fx] == [2, 3]
    # 4K video is decoded at 1920 wide for a 1080p timeline: libopenshot's 6 px are 12 source pixels
    assert _params(fx[0])["Blurriness"] == pytest.approx(2 * math.sqrt(3 * 6 * 7 / 3) / AE_BLUR_SIZE_TO_SIGMA)
    assert _params(fx[1])["Blurriness"] == pytest.approx(2 * math.sqrt(3 * 2 * 3 / 3) / AE_BLUR_SIZE_TO_SIGMA)


def test_colour_effects_map_to_their_ae_parameters():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/fx.mp4")
    c = b.add_clip(v, track=1, position=0.0, start=0.0, end=2.0)
    b.add_effect(c, "Brightness", brightness=const(0.2), contrast=const(64.0))
    b.add_effect(c, "Saturation", saturation=const(0.0), saturation_R=const(1.5))
    b.add_effect(c, "Hue", hue=const(0.25))
    b.add_effect(c, "Negate")
    b.add_effect(c, "Sharpen", amount=const(10.0))
    out, _ = build(b)
    fx = layer(out, index=0)["fx"]
    assert [f["opts"][0]["match"] for f in fx] == ["ADBE Brightness & Contrast 2", "ADBE HUE SATURATION",
                                                    "ADBE HUE SATURATION", "ADBE Invert", "ADBE Sharpen"]
    bc = _params(fx[0])
    assert bc["Brightness"] == pytest.approx(51.0) and bc["Contrast"] == pytest.approx(50.0)
    assert bc["Use Legacy"] == 1
    assert _params(fx[1])["Master Saturation"] == pytest.approx(-100.0)
    assert _params(fx[2])["Master Hue"] == pytest.approx(90.0)
    assert _params(fx[3]) == {"Channel": 1, "Blend With Original": 0}
    assert _params(fx[4])["Sharpen Amount"] == pytest.approx(25.0)
    assert any("per colour channel" in w for w in out.warnings)


def test_pixelate_block_counts_come_from_the_decoded_width():
    b, _ = _fx_project("Pixelate", pixelization=const(0.5))
    out, _ = build(b)
    p = _params(layer(out, index=0)["fx"][0])
    # a 1280x720 video on a 1080p timeline decodes at 1280 wide: 1280 * 0.001**0.5 = 40.48 -> 40 blocks
    assert (p["Horizontal Blocks"], p["Vertical Blocks"], p["Sharp Colors"]) == (40, 23, 0)


def test_chroma_key_tries_keylight_then_color_key():
    green = {"red": const(0), "green": const(255), "blue": const(0), "alpha": const(255)}
    b, _ = _fx_project("ChromaKey", color=green, fuzz=const(30.0))
    out, _ = build(b)
    fx = layer(out, index=0)["fx"][0]
    assert [o["match"] for o in fx["opts"]] == ["Keylight 906", "ADBE Color Key"]
    assert _params(fx, 0)["Keylight 906-0002"] == [0.0, 1.0, 0.0, 1.0]
    teal = {"red": const(0), "green": const(200), "blue": const(40), "alpha": const(255)}
    b, _ = _fx_project("ChromaKey", color=teal)
    out, _ = build(b)
    assert _params(layer(out, index=0)["fx"][0], 1)["Key Color"] == pytest.approx([0.0, 200 / 255, 40 / 255, 1.0])
    assert _params(fx, 1)["Color Tolerance"] == pytest.approx(30.0)


def test_crop_is_a_mask_in_source_pixels():
    b, _ = _fx_project("Crop", left=const(0.1), top=const(0.0), right=const(0.25), bottom=const(0.5),
                       x=const(0.1))
    out, _ = build(b)
    L = layer(out, index=0)
    assert "fx" not in L
    shape = L["masks"][0]["shape"]
    assert shape["pts"] == [[128, 0], [960, 0], [960, 360], [128, 360]]
    assert any("offsets" in w for w in out.warnings)


def test_colour_grade_maps_to_lumetri_and_lut_warns():
    b, _ = _fx_project("ColorGrade", exposure=const(0.5), contrast=const(0.2), saturation=const(1.2),
                       lut_path="/luts/teal.cube")
    out, _ = build(b)
    fx = layer(out, index=0)["fx"][0]
    p = _params(fx)
    assert fx["opts"][0]["match"] == "ADBE Lumetri"
    assert (p["Exposure"], p["Contrast"], p["Saturation"]) == (pytest.approx(0.5), pytest.approx(20.0),
                                                               pytest.approx(120.0))
    assert any("/luts/teal.cube" in w for w in out.warnings)


def test_unmapped_effects_warn_and_are_listed_in_the_comment():
    b, _ = _fx_project("Wave")
    out, _ = build(b)
    L = layer(out, index=0)
    assert "fx" not in L and "Zenvi effects not exported: Wave" in L["comment"]
    assert any("Wave" in w for w in out.warnings)


def test_animated_effect_parameters_are_keyed():
    b, c = _fx_project("Hue", hue=kf((1, 0.0), (31, 0.5)))
    out, snap = build(b)
    spec = _params(layer(out, index=0)["fx"][0])["Master Hue"]
    assert spec["i"] == ["b"]  # 360 * hue is affine: the curve's ease is kept
    hue = snap.clip(c).effects[0].params["hue"]
    for t in K.frame_times(0.0, 2.0, 30.0):
        assert value_at(spec, t)[0] == pytest.approx(360 * K.exact_value(hue, t), abs=1e-4)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def _overlap(mask, **extra):
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/a.mp4", has_audio=True)
    w = b.add_file("video", path="/media/b.mp4", has_audio=True)
    first = b.add_clip(v, track=1, position=0.0, start=0.0, end=5.0)
    second = b.add_clip(w, track=1, position=4.0, start=0.0, end=4.0)
    b.add_transition(track=1, position=4.0, duration=1.0, mask=mask, **extra)
    return b, first, second


def test_a_fade_multiplies_the_top_clips_opacity_by_the_mask_result():
    b, first, second = _overlap(FADE)
    out, snap = build(b)
    top, bottom = layer(out, "b.mp4"), layer(out, "a.mp4")
    assert bottom["tf"]["op"] == 100
    tv = snap.transitions[0]
    for k in range(120, 150):
        t = k / 30.0
        want = 100 * AE.mask_factor(K.exact_value(tv.brightness, t), K.exact_value(tv.contrast, t), 0)
        assert value_at(top["tf"]["op"], t)[0] == pytest.approx(want, abs=AE.TOL_OPACITY + 1e-9)
    assert value_at(top["tf"]["op"], 6.0)[0] == pytest.approx(100.0)
    assert out.stats["transitions_fade"] == 1
    assert out.data["layers"].index(top) > out.data["layers"].index(bottom)


def test_mask_factor_matches_the_mask_kernel():
    assert AE.mask_factor(1.0, 3.0, 0) == 0.0
    assert AE.mask_factor(-1.0, 3.0, 255) == 1.0
    assert AE.mask_factor(0.0, 0.0, 128) == pytest.approx(127 / 255)
    assert AE.mask_factor(1.0, 3.0, 0, invert=True) == 1.0


def test_a_wipe_is_a_gradient_wipe_reading_a_hidden_guide_layer():
    b, first, second = _overlap(WIPE)
    out, _ = build(b)
    guide = out.data["guides"][0]
    assert guide["key"] == "G1" and guide["name"].startswith("Wipe image")
    entry = next(f for f in out.data["footage"] if f["id"] == guide["src"])
    assert entry["rel"] == "masks/wipe.png" and entry["kind"] == "still"
    fx = layer(out, "b.mp4")["fx"][0]
    p = _params(fx)
    assert fx["opts"][0]["match"] == "ADBE Gradient Wipe"
    assert p["Gradient Layer"] == {"layer": "G1"} and p["Invert Gradient"] == 1 and p["Gradient Placement"] == 3
    completion = p["Transition Completion"]
    assert value_at(completion, 4.0)[0] == pytest.approx(100.0)
    assert value_at(completion, 4.97)[0] < 5.0 and value_at(completion, 6.0)[0] == 0.0
    assert "fx" not in layer(out, "a.mp4")
    inverted, _ = build(_overlap(WIPE, mask_invert=True)[0])
    assert _params(layer(inverted, "b.mp4")["fx"][0])["Invert Gradient"] == 0


def test_audio_crossfades_follow_libopenshots_equal_power_curve():
    b, first, second = _overlap(FADE, fade_audio_hint=True)
    out, _ = build(b)
    fade_out, fade_in = layer(out, "a.mp4")["levels"], layer(out, "b.mp4")["levels"]
    mid = 4.0 + 14 / 30.0  # frame index 134 of a 30-frame mask starting at 120
    s = (134 - 121) / (150 - 121)
    assert value_at(fade_in, mid)[0] == pytest.approx(20 * math.log10(math.sin(s * math.pi / 2)), abs=AE.TOL_DB + 1e-9)
    assert value_at(fade_out, mid)[0] == pytest.approx(20 * math.log10(math.cos(s * math.pi / 2)), abs=AE.TOL_DB + 1e-9)


def test_a_missing_wipe_image_skips_the_transition_with_a_warning():
    b, _, _ = _overlap("/gone/wipe.png")
    out, _ = build(b)
    assert any("wipe image is missing" in w for w in out.warnings)
    assert "fx" not in layer(out, "b.mp4") and layer(out, "b.mp4")["tf"]["op"] == 100


# ---------------------------------------------------------------------------
# Everything else
# ---------------------------------------------------------------------------

def test_markers_become_comp_markers_with_labels():
    b, _, _ = _basic()
    b.add_marker(1.0, "Intro", "red")
    b.add_marker(1.0, "Beat", "blue")
    b.add_marker(2.5, "", "green")
    out, _ = build(b)
    assert out.data["markers"] == [{"t": 1.0, "c": "Intro / Beat", "lb": 1}, {"t": 2.5, "c": "Marker", "lb": 9}]


def test_missing_media_is_flagged_for_a_placeholder():
    b, v, _ = _basic()
    out, _ = build(b, media={v: AE.MediaRef(abs="/media/beach.mp4", missing=True)})
    entry = out.data["footage"][0]
    assert entry["missing"] and (entry["w"], entry["h"]) == (1280, 720) and entry["dur"] > 0
    assert out.stats["missing_media"] == 1 and any("Missing media" in w for w in out.warnings)


def test_linked_clips_carry_their_link_and_ae_comps_point_back():
    b = ProjectBuilder()
    link = {"version": 1, "kind": "aftereffects", "source": {"project_dir": None, "entry": None,
            "composition": "Promo", "composition_key": 12, "file": None, "line": None, "aep": "/work/promo.aep"},
            "props": {}, "render": {"codec": "h264"}, "state": "fresh", "error": None}
    v = b.add_file("video", path="/project/links/Promo.mov", zenvi_link=link)
    b.add_clip(v, track=1, position=0.0, start=0.0, end=3.0)
    out, _ = build(b)
    entry = out.data["footage"][0]
    assert entry["aecomp"] == {"id": 12, "name": "Promo", "aep": "/work/promo.aep"}
    assert "Zenvi linked clip" in entry["comment"] and "Zenvi linked clip" in layer(out, index=0)["comment"]


def test_layer_order_labels_blend_lock_and_parent():
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/a.mp4")
    bottom = b.add_clip(v, track=1, position=0.0, start=0.0, end=3.0)
    top = b.add_clip(v, track=3, position=0.0, start=0.0, end=3.0, composite=13, parentObjectId="X1")
    b.name_track(3, "Graphics", lock=True)
    out, _ = build(b)
    first, last = out.data["layers"]
    assert first["comment"].startswith(f"Zenvi clip {bottom} on track 1")
    assert last["comment"].startswith(f"Zenvi clip {top} on track 3 (Graphics)")
    assert last["blend"] == "MULTIPLY" and last["lock"] is True and first["label"] != last["label"]
    assert any("parent" in w for w in out.warnings)


@pytest.mark.parametrize("scale_mode, gravity, rotation, origin", [
    (T.SCALE_FIT, T.GRAVITY_CENTER, 30.0, (0.5, 0.5)),
    (T.SCALE_CROP, T.GRAVITY_TOP_LEFT, -75.0, (0.2, 0.8)),
    (T.SCALE_STRETCH, T.GRAVITY_BOTTOM_RIGHT, 181.0, (1.0, 0.0)),
    (T.SCALE_NONE, T.GRAVITY_RIGHT, 5.0, (0.5, 0.1)),
])
def test_after_effects_layer_transform_lands_every_source_pixel_where_libopenshot_draws_it(
        scale_mode, gravity, rotation, origin):
    """AE draws source point p at Position + R(rotation) * S(scale) * (p - Anchor) (no shear)."""
    b = ProjectBuilder()
    v = b.add_file("video", path="/media/clip.mp4")
    b.add_clip(v, track=1, position=0.0, start=0.0, end=2.0, scale=scale_mode, gravity=gravity,
               rotation=const(rotation), origin_x=const(origin[0]), origin_y=const(origin[1]),
               location_x=const(0.05), location_y=const(-0.1), scale_x=const(0.7), scale_y=const(1.3))
    out, snap = build(b)
    tf = layer(out, index=0)["tf"]
    clip = snap.clips[0]
    g = T.clip_geometry(clip, 0.0, snap.width, snap.height)
    theta = math.radians(tf["rot"])
    for px, py in ((0, 0), (1280, 0), (1280, 720), (0, 720), (333, 222)):
        dx = (px - tf["anchor"][0]) * tf["scale"][0] / 100.0
        dy = (py - tf["anchor"][1]) * tf["scale"][1] / 100.0
        ae = (tf["pos"][0] + dx * math.cos(theta) - dy * math.sin(theta),
              tf["pos"][1] + dx * math.sin(theta) + dy * math.cos(theta))
        assert ae == pytest.approx(g.map_point(px, py), abs=1e-3)


def test_colour_grade_curves_and_wheels_warn_only_when_they_change_the_image():
    from classes import color_presets
    b, _ = _fx_project("ColorGrade", exposure=const(0.5), curve_all=color_presets.default_curve_data(),
                       wheels=color_presets.default_wheels_data())
    out, _ = build(b)
    assert not any("curve" in w or "wheels" in w for w in out.warnings)
    lifted = color_presets.curve_data([{"x": 0.0, "y": 0.1}, {"x": 1.0, "y": 1.0}])
    wheels = color_presets.default_wheels_data()
    wheels["shadows"] = color_presets.wheel_entry("#2040ff", 0.2)
    b, _ = _fx_project("ColorGrade", exposure=const(0.5), curve_all=lifted, wheels=wheels)
    out, _ = build(b)
    assert any("curve all" in w for w in out.warnings) and any("wheels" in w for w in out.warnings)
