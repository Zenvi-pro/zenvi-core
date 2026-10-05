"""classes.handoff.remotion.exporter / restore: a Zenvi timeline as a Remotion project, and back (lossless)."""

import copy
import json
import os
import shutil
import subprocess
from fractions import Fraction

import pytest

from classes.handoff.keyframes import BEZIER, CONSTANT, LINEAR, Curve
from classes.handoff.remotion import exporter, restore
from classes.handoff.timeline_view import TimelineSnapshot
from handoff_fakes import linked, tt  # noqa: F401  (fixtures)

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "handoff_remotion", "golden")
GENERATOR = "Zenvi 9.9.9-test"


def _kf(*points):
    """[(x, y, interpolation, (hl_x, hl_y), (hr_x, hr_y))...] -> keyframe JSON."""
    out = []
    for p in points:
        x, y = p[0], p[1]
        interp = p[2] if len(p) > 2 else BEZIER
        hl = p[3] if len(p) > 3 else (0.5, 1.0)
        hr = p[4] if len(p) > 4 else (0.5, 0.0)
        out.append({"co": {"X": float(x), "Y": float(y)}, "interpolation": interp, "handle_type": 0,
                    "handle_left": {"X": hl[0], "Y": hl[1]}, "handle_right": {"X": hr[0], "Y": hr[1]}})
    return {"Points": out}


def build_project(editor, media_dir):
    """video (trimmed, fading in, audio) + image (keyframed slide) + title + music + a fade transition + marker."""
    os.makedirs(media_dir, exist_ok=True)

    def media(name, body=b"media"):
        path = os.path.join(media_dir, name)
        with open(path, "wb") as fh:
            fh.write(body)
        return path

    video = editor.add_file("video", path=media("beach.mp4"), duration=12.0, width=1280, height=720,
                            has_audio=True, name="beach.mp4")
    image = editor.add_file("image", path=media("logo.png"), width=800, height=400, name="logo.png")
    title = editor.add_file("image", path=media("title.svg", b"<svg/>"), width=1920, height=1080, name="Title")
    music = editor.add_file("audio", path=media("music.mp3"), duration=30.0, name="music.mp3")
    clips = {
        "video": editor.add_clip(video, position=0.0, layer=1000000, start=2.0, end=8.0,
                                 alpha=_kf((61, 0.0), (76, 1.0)),
                                 volume=_kf((61, 1.0), (241, 0.0, LINEAR))),
        "image": editor.add_clip(image, position=1.0, layer=2000000, start=0.0, end=4.0,
                                 location_x=_kf((1, -0.2), (31, 0.0, BEZIER, (0.3, 1.0), (0.5, 0.0))),
                                 scale_x=_kf((1, 0.5)), scale_y=_kf((1, 0.5)), gravity=8, scale=1),
        "title": editor.add_clip(title, position=0.5, layer=3000000, start=0.0, end=3.0,
                                 alpha=_kf((1, 1.0), (60, 1.0), (90, 0.0, CONSTANT))),
        "music": editor.add_clip(music, position=0.0, layer=1000000, start=0.0, end=0.0 + 6.0,
                                 volume=_kf((1, 0.8))),
    }
    mask = media("fade.svg", b"<svg/>")
    transition = editor.effect_fixture("Mask")
    transition.update(id="TRANS1", layer=2000000, position=1.0, start=0.0, end=1.0, duration=1.0, title="Fade",
                      brightness=_kf((1, 1.0, LINEAR), (31, -1.0, LINEAR)), contrast=_kf((1, 3.0)),
                      reader={"path": mask, "type": "QtImageReader"})
    editor.store._data["effects"] = [transition]
    editor.store._data["markers"] = [{"id": "M1", "position": 2.0, "name": "Drop", "vector": "red"}]
    editor.mark()
    return clips, {"video": video, "image": image, "title": title, "music": music}


def _export(editor, out, **kw):
    data = exporter.project_copy(editor.store._data)
    snapshot = TimelineSnapshot.from_project(data, None)
    return exporter.export_project(snapshot, data, str(out), generator=GENERATOR, **kw)


def _timeline(out):
    with open(os.path.join(str(out), "src", "zenvi", "timeline.json")) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# The generated project
# ---------------------------------------------------------------------------

def test_export_writes_a_complete_remotion_project(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    out = tmp_path / "trip-remotion"
    receipt = _export(linked, out)
    assert receipt["mode"] == "new" and receipt["clips"] == 4 and receipt["media_files"] == 5
    for rel in [dest for _src, dest in exporter.STATIC_FILES] + list(exporter.GENERATED_FILES):
        assert os.path.isfile(os.path.join(str(out), *rel.split("/"))), rel
    media = sorted(os.listdir(str(out / "public" / "zenvi-media")))
    assert media == ["Title.svg", "beach.mp4", "fade.svg", "logo.png", "music.mp3"]
    assert not [n for n in os.listdir(str(tmp_path)) if ".zenvi-partial-" in n]
    package = json.load(open(str(out / "package.json")))
    assert package["dependencies"]["remotion"] == exporter.REMOTION_VERSION == "4.0.532"
    assert package["dependencies"]["@remotion/cli"] == "4.0.532" and package["dependencies"]["zod"] == "3.22.3"
    assert package["scripts"]["render"] == "remotion render ZenviTimeline out/video.mp4"
    readme = open(str(out / "README.md")).read()
    assert "Import Project > Remotion Project" in readme and "remotion.dev/license" in readme
    assert "{{" not in readme and "1920×1080" in readme
    gitignore = open(str(out / ".gitignore")).read()
    assert "node_modules" in gitignore


def test_root_tsx_matches_the_golden_file(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    _export(linked, tmp_path / "out")
    produced = open(str(tmp_path / "out" / "src" / "Root.tsx")).read()
    golden = open(os.path.join(GOLDEN, "Root.tsx")).read()
    assert produced == golden


def test_timeline_json_carries_timing_keyframes_speed_transitions_and_markers(linked, tmp_path):  # noqa: F811
    clips, files = build_project(linked, str(tmp_path / "media"))
    _export(linked, tmp_path / "out")
    t = _timeline(tmp_path / "out")
    assert t["zenvi_timeline"] == 1 and t["generator"] == GENERATOR and t["source_project"] == "Untitled"
    assert t["composition"] == {"id": "ZenviTimeline", "width": 1920, "height": 1080, "fps": 30,
                                "fpsFraction": {"num": 30, "den": 1}, "durationInFrames": 180}
    by_id = {c["id"]: c for c in t["clips"]}
    video = by_id[clips["video"]]
    assert (video["from"], video["durationInFrames"], video["kind"], video["track"]) == (0, 180, "video", 0)
    assert video["time"] == {"mode": "normal", "trimBefore": 60, "playbackRate": 1}
    assert video["src"] == "zenvi-media/beach.mp4" and video["hasAudio"] and video["hasVideo"]
    assert (video["sourceWidth"], video["sourceHeight"]) == (1280, 720)
    # keyframe X 61 is the first visible frame of a clip trimmed by 2 s at 30 fps -> frame 0
    assert video["keyframes"]["alpha"] == [{"frame": 0, "value": 0, "easing": None},
                                          {"frame": 15, "value": 1, "easing": [0.5, 0, 0.5, 1]}]
    assert video["keyframes"]["volume"][1] == {"frame": 180, "value": 0, "easing": "linear"}
    image = by_id[clips["image"]]
    assert (image["from"], image["durationInFrames"], image["kind"], image["gravity"]) == (30, 120, "image", 8)
    assert image["keyframes"]["location_x"] == [{"frame": 0, "value": -0.2, "easing": None},
                                               {"frame": 30, "value": 0, "easing": [0.5, 0, 0.3, 1]}]
    assert image["keyframes"]["scale_x"] == [{"frame": 0, "value": 0.5, "easing": None}]
    assert "rotation" not in image["keyframes"]  # defaults are left out
    title = by_id[clips["title"]]
    assert title["kind"] == "title" and title["keyframes"]["alpha"][2]["easing"] == "hold"
    music = by_id[clips["music"]]
    assert music["kind"] == "audio" and music["hasVideo"] is False and music["keyframes"]["volume"][0]["value"] == 0.8
    assert [c["track"] for c in t["clips"]] == sorted(c["track"] for c in t["clips"])  # bottom track first
    (fade,) = t["transitions"]
    assert (fade["from"], fade["durationInFrames"], fade["kind"], fade["layer"]) == (30, 30, "fade", 2000000)
    assert fade["mask"] == "zenvi-media/fade.svg"
    assert fade["opacity"][0] == 0.0 and fade["opacity"][-1] == 1.0  # brightness 1 hides, -1 shows (fade in)
    assert fade["opacity"] == sorted(fade["opacity"])
    assert t["markers"] == [{"frame": 60, "time": 2.0, "name": "Drop", "color": "red"}]
    assert t["media"][files["video"]]["src"] == "zenvi-media/beach.mp4"
    assert t["zenvi"]["readable_sha256"] == exporter.readable_hash(t)


def test_the_zenvi_block_is_the_original_project(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    linked.store._data["clips"][0]["ui"] = {"audio_data": [0.1] * 50}
    original = copy.deepcopy(linked.store._data)
    _export(linked, tmp_path / "out")
    saved = _timeline(tmp_path / "out")["zenvi"]["project"]
    expected = {k: v for k, v in original.items() if k != "history"}
    for clip in expected["clips"]:
        clip.pop("ui", None)
    assert saved == json.loads(json.dumps(expected))


def test_exporting_again_updates_the_project_and_keeps_installs_and_added_deps(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    out = tmp_path / "out"
    _export(linked, out)
    (out / "node_modules").mkdir()
    (out / "node_modules" / "keep.txt").write_text("x")
    package = json.load(open(str(out / "package.json")))
    package["dependencies"]["lottie-web"] = "5.12.0"
    package["dependencies"]["remotion"] = "4.0.100"
    json.dump(package, open(str(out / "package.json"), "w"))
    (out / "public" / "zenvi-media" / "stale.mp4").write_text("old")
    receipt = _export(linked, out)
    assert receipt["mode"] == "update"
    assert (out / "node_modules" / "keep.txt").exists() and not (out / "public" / "zenvi-media" / "stale.mp4").exists()
    package = json.load(open(str(out / "package.json")))
    assert package["dependencies"]["lottie-web"] == "5.12.0" and package["dependencies"]["remotion"] == "4.0.532"


def test_export_refuses_foreign_folders_and_empty_timelines(linked, tmp_path):  # noqa: F811
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine")
    with pytest.raises(exporter.ExportError, match="not empty and is not a Zenvi Remotion export"):
        _export(linked, busy)
    with pytest.raises(exporter.ExportError, match="timeline is empty"):
        _export(linked, tmp_path / "new")
    with pytest.raises(exporter.ExportError, match="does not exist"):
        _export(linked, tmp_path / "missing" / "deeper" / "out")
    assert (busy / "notes.txt").read_text() == "mine" and not (tmp_path / "new").exists()


def test_missing_media_is_left_out_with_a_warning(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    os.remove(str(tmp_path / "media" / "logo.png"))
    receipt = _export(linked, tmp_path / "out")
    assert receipt["clips"] == 3 and any("logo.png" in w and "missing" in w for w in receipt["warnings"])


def test_media_can_be_symlinked(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    _export(linked, tmp_path / "out", copy_media=False)
    link = tmp_path / "out" / "public" / "zenvi-media" / "beach.mp4"
    assert link.is_symlink() and os.path.realpath(str(link)) == os.path.realpath(str(tmp_path / "media" / "beach.mp4"))


# ---------------------------------------------------------------------------
# Speed, masks, effects
# ---------------------------------------------------------------------------

def _clip_with_time(time_kf, start=0.0, end=4.0):
    project = {"fps": {"num": 30, "den": 1}, "width": 1920, "height": 1080,
               "layers": [{"number": 1000000}],
               "files": [{"id": "F", "path": "/x.mp4", "media_type": "video", "has_video": True, "duration": 20}],
               "clips": [{"id": "C", "file_id": "F", "layer": 1000000, "position": 0.0, "start": start, "end": end,
                          "time": time_kf}]}
    return TimelineSnapshot.from_project(project).clips[0]


def test_time_spec_reads_what_libopenshot_plays():
    fps = Fraction(30)
    spec, note = exporter.time_spec(_clip_with_time(None, start=1.0), fps, 30, 90)
    assert spec == {"mode": "normal", "trimBefore": 30, "playbackRate": 1} and note is None
    double = _kf((1, 1, LINEAR), (301, 601, LINEAR))
    spec, _ = exporter.time_spec(_clip_with_time(double), fps, 0, 120)
    assert spec["mode"] == "rate" and spec["trimBefore"] == 0 and spec["playbackRate"] == pytest.approx(2.0)
    held = _kf((1, 50, LINEAR), (301, 50, LINEAR))
    spec, note = exporter.time_spec(_clip_with_time(held), fps, 0, 30)
    assert spec == {"mode": "freeze", "trimBefore": 49} and "no sound" in note
    backwards = _kf((1, 120, LINEAR), (121, 1, LINEAR))
    spec, note = exporter.time_spec(_clip_with_time(backwards), fps, 0, 120)
    assert spec["mode"] == "map" and spec["map"][0] == 119 and spec["map"][-1] < spec["map"][0]
    # a 2x curve that ends inside the clip holds its last frame: not "constant 2x"
    short = _kf((1, 1, LINEAR), (31, 61, LINEAR))
    spec, _ = exporter.time_spec(_clip_with_time(short), fps, 0, 90)
    assert spec["mode"] == "map" and spec["map"][-1] == 60


def test_mask_math_matches_libopenshot_for_a_black_fade_mask():
    assert exporter.mask_multiplier(1.0, 3.0, gray=0) == 0.0
    assert exporter.mask_multiplier(-1.0, 3.0, gray=0) == 1.0
    # src/effects/Mask.cpp: a black mask is fully shown from brightness 0 down; the fade is the first half
    assert exporter.mask_multiplier(0.0, 3.0, gray=0) == 1.0
    assert exporter.mask_multiplier(0.5, 3.0, gray=0) == pytest.approx(129 / 255.0)
    assert exporter.mask_multiplier(0.0, 0.0, gray=128) == pytest.approx(127 / 255.0)


def test_simple_effects_become_css_filters_and_the_rest_are_listed(linked, tmp_path):  # noqa: F811
    clips, _files = build_project(linked, str(tmp_path / "media"))
    linked.add_effect(clips["image"], "Brightness", brightness=_kf((1, 0.2)), contrast=_kf((1, 3.0)))
    linked.add_effect(clips["image"], "Saturation", saturation=_kf((1, 0.0)))
    linked.add_effect(clips["image"], "Negate")
    linked.add_effect(clips["image"], "Crop", left=_kf((1, 0.1)), right=_kf((1, 0.0)), top=_kf((1, 0.0)),
                      bottom=_kf((1, 0.25)))
    linked.add_effect(clips["image"], "Pixelate")
    receipt = _export(linked, tmp_path / "out")
    image = [c for c in _timeline(tmp_path / "out")["clips"] if c["id"] == clips["image"]][0]
    fns = [f["fn"] for f in image["filters"]]
    assert fns == ["contrast", "brightness", "saturate", "invert"]
    assert image["filters"][1]["keys"][0]["value"] == pytest.approx(1.2)
    assert image["crop"]["left"][0]["value"] == pytest.approx(0.1) and image["crop"]["bottom"][0]["value"] == 0.25
    assert any("Pixelate" in n for n in receipt["notes"])


def test_curve_keys_match_segment_easing():
    curve = Curve.from_json(_kf((11, 0.0), (41, 1.0, BEZIER, (0.2, 1.0), (0.5, 0.0)), (61, 1.0, CONSTANT)),
                            fps=30, position=0, start=0)
    keys = exporter.curve_keys(curve, 10)
    assert keys == [{"frame": 0, "value": 0, "easing": None}, {"frame": 30, "value": 1, "easing": [0.5, 0, 0.2, 1]},
                    {"frame": 50, "value": 1, "easing": "hold"}]


# ---------------------------------------------------------------------------
# geometry.ts == handoff.transform.geometry (run under Node's type stripping)
# ---------------------------------------------------------------------------

def _node_strips_types():
    node = shutil.which("node")
    if not node:
        return None
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        major, minor = (int(x) for x in version.lstrip("v").split(".")[:2])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if (major, minor) >= (23, 6):
        return [node]
    if (major, minor) >= (22, 6):
        return [node, "--experimental-strip-types", "--no-warnings"]
    return None


POSES = [
    dict(src=(1280, 720), mode=1, gravity=4, pose=dict(scaleX=1, scaleY=1, locationX=0, locationY=0, rotation=0)),
    dict(src=(1280, 720), mode=1, gravity=4, pose=dict(scaleX=0.5, scaleY=0.5, locationX=0.1, locationY=-0.2,
                                                       rotation=15)),
    dict(src=(800, 400), mode=0, gravity=8, pose=dict(scaleX=1.2, scaleY=0.8, locationX=-0.3, locationY=0.4,
                                                      rotation=-90, originX=0.2, originY=0.9)),
    dict(src=(1080, 1920), mode=2, gravity=0, pose=dict(scaleX=1, scaleY=1, locationX=0, locationY=0, rotation=180,
                                                        shearX=0.2, margin=0.05)),
    dict(src=(640, 480), mode=3, gravity=5, pose=dict(scaleX=2, scaleY=2, locationX=0.05, locationY=0.05,
                                                      rotation=33.3)),
]


def test_geometry_ts_matches_the_python_port(tmp_path):
    cmd = _node_strips_types()
    if cmd is None:
        pytest.skip("needs Node.js 22.6+ (type stripping) to run geometry.ts")
    from classes.handoff.transform import geometry
    shutil.copy(os.path.join(exporter.TEMPLATE_DIR, "src", "zenvi", "geometry.ts"), str(tmp_path / "geometry.ts"))
    full = []
    for case in POSES:
        pose = dict(dict(scaleX=1, scaleY=1, locationX=0, locationY=0, rotation=0, originX=0.5, originY=0.5,
                         shearX=0, shearY=0, margin=0), **case["pose"])
        full.append(dict(case, pose=pose))
    (tmp_path / "cases.json").write_text(json.dumps(full))
    (tmp_path / "run.mts").write_text(
        "import {readFileSync} from 'node:fs';\nimport {clipMatrix} from './geometry.ts';\n"
        "const cases = JSON.parse(readFileSync(new URL('./cases.json', import.meta.url), 'utf8'));\n"
        "console.log(JSON.stringify(cases.map((c) => clipMatrix(c.src[0], c.src[1], 1920, 1080, c.mode, c.gravity, "
        "c.pose))));\n")
    out = subprocess.run(cmd + [str(tmp_path / "run.mts")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    ts = json.loads(out.stdout)
    for case, matrix in zip(full, ts):
        p = case["pose"]
        g = geometry(case["src"][0], case["src"][1], 1920, 1080, scale_mode=case["mode"], gravity=case["gravity"],
                     scale_x=p["scaleX"], scale_y=p["scaleY"], location_x=p["locationX"], location_y=p["locationY"],
                     rotation=p["rotation"], origin_x=p["originX"], origin_y=p["originY"], shear_x=p["shearX"],
                     shear_y=p["shearY"], margin=p["margin"])
        assert matrix == pytest.approx(list(g.matrix), abs=1e-9), case


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def test_restored_project_points_at_copies_when_the_originals_are_gone(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    original = copy.deepcopy(linked.store._data)
    out = tmp_path / "out"
    _export(linked, out)
    timeline = restore.load_timeline(restore.timeline_path(str(out)))
    project, warnings = restore.restored_project(timeline, str(out))
    assert project["files"] == original["files"] and warnings == []
    shutil.rmtree(str(tmp_path / "media"))
    project, warnings = restore.restored_project(timeline, str(out))
    paths = {f["name"]: f["path"] for f in project["files"]}
    assert paths["beach.mp4"] == str(out / "public" / "zenvi-media" / "beach.mp4")
    assert project["effects"][0]["reader"]["path"] == str(out / "public" / "zenvi-media" / "fade.svg")
    assert all(c["reader"]["path"].startswith(str(out)) for c in project["clips"] if "reader" in c)


def test_insert_native_into_an_empty_project_restores_the_same_clips_in_one_undo_step(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    original = copy.deepcopy(linked.store._data)
    out = tmp_path / "out"
    _export(linked, out)
    linked.store._data.update(clips=[], files=[], effects=[], markers=[])
    linked.mark()
    timeline = restore.load_timeline(restore.timeline_path(str(out)))
    receipt = restore.insert_native(timeline, str(out), position=0.0)
    assert linked.undo_steps_since_mark() == 1
    assert receipt["tracks"] == 0 and receipt["files"] == 4 and len(receipt["clips"]) == 4
    for key in ("files", "clips", "effects", "markers"):
        assert sorted(linked.get(key), key=lambda d: d["id"]) == sorted(original[key], key=lambda d: d["id"]), key
    linked.undo()
    assert linked.get("clips") == [] and linked.get("files") == []


def test_insert_native_into_a_busy_project_uses_new_tracks_ids_and_the_position(linked, tmp_path):  # noqa: F811
    clips, files = build_project(linked, str(tmp_path / "media"))
    original = copy.deepcopy(linked.store._data)
    out = tmp_path / "out"
    _export(linked, out)
    timeline = restore.load_timeline(restore.timeline_path(str(out)))
    os.remove(str(tmp_path / "media" / "music.mp3"))  # files with the same path are reused; this one comes back
    receipt = restore.insert_native(timeline, str(out), position=10.0)
    assert linked.undo_steps_since_mark() == 1
    top = max(ly["number"] for ly in original["layers"])
    assert receipt["tracks"] == 3 and sorted(receipt["layer_map"].values()) == [top + 1000000 * i for i in (1, 2, 3)]
    new_clips = [c for c in linked.get("clips") if c["id"] not in clips.values()]
    assert len(new_clips) == 4 and all(c["layer"] > top for c in new_clips)
    assert sorted(c["position"] for c in new_clips) == sorted(c["position"] + 10.0 for c in original["clips"])
    assert receipt["reused_files"] == 3 and receipt["files"] == 1
    reused = {c["file_id"] for c in new_clips} & set(files.values())
    assert reused == {files["video"], files["image"], files["title"]}
    assert len(linked.get("effects")) == 2 and len(linked.get("markers")) == 2


def test_edits_to_the_readable_timeline_are_reported_and_fps_changes_rescale(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    out = tmp_path / "out"
    _export(linked, out)
    path = restore.timeline_path(str(out))
    timeline = json.load(open(path))
    timeline["clips"][0]["from"] += 5
    json.dump(timeline, open(path, "w"))
    timeline = restore.load_timeline(path)
    assert restore.timeline_edited(timeline)
    project, warnings = restore.restored_project(timeline, str(out))
    assert any("edited after Zenvi exported it" in w for w in warnings)
    current = {"fps": {"num": 60, "den": 1}, "width": 1920, "height": 1080, "layers": [], "clips": []}
    plan, plan_warnings = restore.plan_native(project, current)
    assert any("rescaled" in w for w in plan_warnings)
    video = [c for c in plan["clips"] if c["start"] == 2.0][0]
    assert [p["co"]["X"] for p in video["alpha"]["Points"]] == [121.0, 151.0]


def test_write_project_file_makes_a_new_project(linked, tmp_path):  # noqa: F811
    build_project(linked, str(tmp_path / "media"))
    linked.store._data["id"] = "ORIGINAL01"
    out = tmp_path / "out"
    _export(linked, out)
    timeline = restore.load_timeline(restore.timeline_path(str(out)))
    path, warnings = restore.write_project_file(timeline, str(out), str(tmp_path / "restored"))
    assert path == str(tmp_path / "restored.zvn") and warnings == []
    data = json.load(open(path))
    assert data["id"] != "ORIGINAL01" and data["history"] == {"undo": [], "redo": []}
    assert len(data["clips"]) == 4 and data["fps"] == {"num": 30, "den": 1}


def test_load_timeline_refuses_what_it_cannot_restore(tmp_path):
    bad = tmp_path / "t.json"
    bad.write_text("{nope")
    with pytest.raises(restore.RestoreError, match="not valid JSON"):
        restore.load_timeline(str(bad))
    bad.write_text(json.dumps({"zenvi_timeline": 7, "zenvi": {"project": {}}}))
    with pytest.raises(restore.RestoreError, match="newer Zenvi"):
        restore.load_timeline(str(bad))
    bad.write_text(json.dumps({"zenvi_timeline": 1}))
    with pytest.raises(restore.RestoreError, match="no 'zenvi.project' block"):
        restore.load_timeline(str(bad))


def test_template_and_helper_ship_as_package_data():
    """setup.py and freeze.py copy every file under src/ (minus .pyc); these must all be there."""
    from classes.handoff.remotion import helper
    names = [src for src, _dest in exporter.STATIC_FILES] + ["src/Root.tsx", "README.md"]
    for rel in names:
        assert os.path.isfile(os.path.join(exporter.TEMPLATE_DIR, *rel.split("/"))), rel
    assert os.path.isfile(helper.HELPER_FILE) and helper.HELPER_FILE.endswith(os.path.join("remotion", "helper.mjs"))
    assert exporter.TEMPLATE_DIR.startswith(os.path.join(os.path.dirname(exporter.__file__)))
