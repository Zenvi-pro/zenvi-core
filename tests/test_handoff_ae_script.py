"""The exported After Effects scripts themselves: golden files, an ES3 parse, and runs against a mock DOM.

* Golden files: ``tests/fixtures/after_effects/{basic,effects}.jsx`` from the
  committed project JSON (``ZENVI_UPDATE_GOLDENS=1`` rewrites them).
* ES3 parse: ``npx --yes acorn@8 --ecma3`` when Node is installed (skipped
  without Node or without network for the first download).
* Mock DOM: ``fixtures/after_effects/ae_mock.js`` runs a script against a
  model of the After Effects scripting DOM (strict about argument shapes,
  ease array sizes, locked layers and references invalidated by
  addProperty, in a realm without ES5 library methods) when Node is
  installed. After Effects itself is not installed here; the manual test
  plan in the C2 state notes covers a real run.
"""

import json
import os
import shutil
import subprocess

import pytest

import ae_goldens
from ae_project import LINEAR, ProjectBuilder, kf, point
from classes.exporters import after_effects as AE
from classes.exporters.after_effects_js import es3_problems

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "fixtures", "after_effects", "ae_mock.js")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

KIT = {"linear": "linear", "bezier": "bezier", "hold": "hold"}


# ---------------------------------------------------------------------------
# Golden files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["basic", "effects"])
def test_golden_scripts_are_up_to_date(name):
    jsx = ae_goldens.build(name).jsx
    if os.environ.get("ZENVI_UPDATE_GOLDENS") == "1":
        with open(ae_goldens.golden_path(name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(jsx)
    with open(ae_goldens.golden_path(name), encoding="utf-8") as fh:
        golden = fh.read()
    assert jsx == golden, (f"{name}.jsx changed; review the diff and run ZENVI_UPDATE_GOLDENS=1 "
                           f"pytest {os.path.basename(__file__)} (or python tests/ae_goldens.py)")
    assert es3_problems(golden) == []


@needs_node
@pytest.mark.parametrize("name", ["basic", "effects"])
def test_golden_scripts_parse_as_ecmascript_3(name, tmp_path):
    with open(ae_goldens.golden_path(name), encoding="utf-8") as fh:
        source = fh.read()
    # '#target' is an ExtendScript preprocessor line, not JavaScript: comment it out first
    script = tmp_path / (name + ".js")
    script.write_text("\n".join("//" + ln if ln.startswith("#") else ln for ln in source.split("\n")),
                      encoding="utf-8")
    npx = shutil.which("npx")
    if npx is None:
        pytest.skip("npx is not installed")
    try:
        proc = subprocess.run([npx, "--yes", "acorn@8", "--ecma3", "--silent", str(script)], capture_output=True,
                              text=True, timeout=240)
    except subprocess.TimeoutExpired:
        pytest.skip("npx acorn timed out")
    if proc.returncode != 0 and ("ENOTFOUND" in proc.stderr or "EAI_AGAIN" in proc.stderr
                                 or "network" in proc.stderr.lower()):
        pytest.skip("acorn could not be downloaded (offline)")
    assert proc.returncode == 0, proc.stderr[-2000:]


# ---------------------------------------------------------------------------
# Mock DOM runs
# ---------------------------------------------------------------------------

def _materialize(name, tmp_path):
    """Export fixture *name* with real (tiny) media files, a wipe image and a script path in tmp_path."""
    from ae_goldens import _assets, project_path
    from classes.handoff.timeline_view import TimelineSnapshot
    with open(project_path(name), encoding="utf-8") as fh:
        data = json.load(fh)
    snapshot = TimelineSnapshot.from_project(data, str(tmp_path / ("Trip.zvn" if name == "basic" else "Promo.zvn")))
    media, titles, masks = _assets(name, data["files"])
    info = {}
    real_media = {}
    folder = tmp_path / "export"
    for f in data["files"]:
        if f["id"] not in media:
            continue
        ref = media[f["id"]]
        target = folder / (ref.rel or ("abs/" + os.path.basename(f["path"])))
        if not ref.missing:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"media")
        real_media[f["id"]] = AE.MediaRef(abs=str(target), rel=ref.rel, missing=ref.missing)
        info[str(target)] = {"duration": f.get("duration") or 10.0, "width": f.get("width") or 1920,
                             "height": f.get("height") or 1080, "hasAudio": bool(f.get("has_audio")),
                             "hasVideo": f.get("media_type") != "audio",
                             "fps": (f["fps"]["num"] / f["fps"]["den"]) if f.get("fps") else 30}
    fixed_masks = {}
    for path, asset in masks.items():
        if asset.image is not None:
            target = folder / asset.image.rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"png")
            asset = AE.MaskAsset("image", image=AE.MediaRef(abs=str(target), rel=asset.image.rel), width=1920,
                                 height=1080)
        fixed_masks[path] = asset
    fixed_titles = {}
    for fid, asset in titles.items():
        if asset.mode == "png":
            target = folder / asset.image.rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"png")
            info[str(target)] = {"still": True, "width": asset.width, "height": asset.height}
            asset = AE.TitleAsset("png", reason=asset.reason, image=AE.MediaRef(abs=str(target), rel=asset.image.rel),
                                  width=asset.width, height=asset.height)
        fixed_titles[fid] = asset
    for f in folder.rglob("*.png"):
        info.setdefault(str(f), {"still": True})
    out = AE.build_ae_script(snapshot, media_map=real_media, title_assets=fixed_titles, mask_assets=fixed_masks,
                             options=AE.AeExportOptions(generator="Zenvi test"))
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "Project.jsx"
    script.write_text(out.jsx, encoding="utf-8")
    media_json = tmp_path / "media.json"
    media_json.write_text(json.dumps(info))
    return out, script, media_json


def _run(script, media_json=None, *args):
    argv = [NODE, MOCK, str(script)] + (["--media", str(media_json)] if media_json else []) + list(args)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["error"] is None, out["error"]
    return json.loads(out["result"]) if out["result"] else None, out["dump"]


def _comp(dump, name):
    return next(i for i in dump["items"] if i["type"] == "Composition" and i["name"] == name)


def _prop(layer, group, match):
    for p in layer["groups"][group]["children"]:
        if p["match"] == match:
            return p
    raise KeyError(match)


@needs_node
def test_the_basic_export_builds_the_whole_timeline(tmp_path):
    out, script, media = _materialize("basic", tmp_path)
    result, dump = _run(script, media)
    assert result["status"] == "ok" and result["layers"] == 6 and result["footage"] == 6
    assert result["warnings"] == [] and result["placeholders"] == []
    assert dump["undo"]["groups"] == ["Import Zenvi project"] and dump["undo"]["depth"] == 0
    # the summary line names no undo step (Zenvi adds the route's name); the File > Scripts alert does
    assert result["undo"] == "Import Zenvi project" and "Undo" not in result["summary"]
    folders = {i["name"]: i["parent"] for i in dump["items"] if i["type"] == "Folder"}
    assert folders == {"Zenvi — Trip": "Root", "Footage": "Zenvi — Trip", "Titles": "Zenvi — Trip"}
    comp = _comp(dump, "Trip")
    assert (comp["width"], comp["height"], comp["frameRate"], comp["opened"]) == (1920, 1080, 30, True)
    assert [m["v"]["comment"] for m in comp["markers"]] == ["Title in", "Wipe"]
    names = [L["name"] for L in comp["layers"]]
    # top to bottom: music, title, photo, then track 1's later clips above earlier ones, the wipe image last
    assert names == ["music.mp3", "Standard_3.svg", "photo.jpg", "drone.mp4", "city.mp4", "beach.mp4",
                     "Wipe image: wipe_left_to_right"]
    layers = {L["name"]: L for L in comp["layers"]}
    beach, drone, city = layers["beach.mp4"], layers["drone.mp4"], layers["city.mp4"]
    assert (beach["startTime"], beach["inPoint"], beach["outPoint"]) == (-1, 0, 5)
    assert drone["stretch"] == 50 and drone["inPoint"] == 7.5
    guide = layers["Wipe image: wipe_left_to_right"]
    assert guide["guideLayer"] and not guide["enabled"]
    wipe = drone["groups"]["ADBE Effect Parade"]["children"][0]
    assert wipe["match"] == "ADBE Gradient Wipe"
    params = {p["name"]: p for p in wipe["children"]}
    assert params["Gradient Layer"]["value"] == guide["index"]
    assert params["Invert Gradient"]["value"] == 1 and len(params["Transition Completion"]["keys"]) >= 3
    fade = _prop(city, "ADBE Transform Group", "ADBE Opacity")
    assert fade["keys"][0]["v"] == 0 and fade["keys"][-1]["v"] == 100
    assert all(k["in"] == "linear" for k in fade["keys"])
    photo = layers["photo.jpg"]
    pos = _prop(photo, "ADBE Transform Group", "ADBE Position")
    assert [k["v"] for k in pos["keys"]] == [[0, 540, 0], [960, 540, 0]]
    assert pos["keys"][0]["outEase"] == [[0, 50]] and pos["keys"][0]["outTangent"] == [0, 0, 0]
    assert pos["keys"][0]["spatialAutoBezier"] is False
    scale = _prop(photo, "ADBE Transform Group", "ADBE Scale")
    assert len(scale["keys"][0]["outEase"]) == 3  # ThreeD: one ease per dimension
    levels = _prop(layers["music.mp3"], "ADBE Audio Group", "ADBE Audio Levels")
    # the fade reaches 0 at clip frame 376, one past the last frame shown (375): -63 dB there
    assert levels["keys"][0]["v"] == [0, 0] and levels["keys"][-1]["v"][0] < -60
    title = _comp(dump, "Standard_3")
    texts = [L["groups"]["ADBE Text Properties"]["children"][0]["value"] for L in title["layers"]]
    assert [t["text"] for t in reversed(texts)] == ["Line 1", "Line 2", "Line 3"]
    assert all(t["justification"] == 7415 and t["fontSize"] == pytest.approx(90) for t in texts)
    assert layers["Standard_3.svg"]["sourceId"] == title["id"]
    assert len(dump["alerts"]) == 1 and "Built comp" in dump["alerts"][0]
    assert 'Edit > Undo "Import Zenvi project" removes it.' in dump["alerts"][0]


@needs_node
def test_the_effects_export_runs_with_placeholders_remaps_and_fallbacks(tmp_path):
    out, script, media = _materialize("effects", tmp_path)
    result, dump = _run(script, media, "--no-keylight")
    assert result["status"] == "ok" and result["placeholders"] == ["missing.mp4"]
    assert any("Missing media" in w for w in result["warnings"])
    assert any("no Keylight" in w or "Keylight 906" in w for w in result["warnings"]) is False
    comp = _comp(dump, "Promo")
    assert comp["frameRate"] == 25
    layers = {L["name"]: L for L in comp["layers"]}
    reversed_clip = layers["missing.mp4"]
    remap = reversed_clip["timeRemap"]["keys"]
    # source times sit half a frame (1/50 s) into the frame libopenshot shows: AE floors them
    assert [k["t"] for k in remap] == [6, 10]
    assert remap[0]["v"] == pytest.approx(4.02) and remap[1]["v"] == pytest.approx(0.02)
    interview = layers["interview.mov"]
    fx = [e["match"] for e in interview["groups"]["ADBE Effect Parade"]["children"]]
    assert fx == ["ADBE Gaussian Blur 2", "ADBE Brightness & Contrast 2", "ADBE HUE SATURATION"]
    mask = interview["groups"]["ADBE Mask Parade"]["children"][0]
    assert mask["name"] == "Crop" and mask["maskMode"] == 6813
    vertices = mask["children"][0]["value"]["vertices"]
    assert vertices[0] == [pytest.approx(64.0), 0] and vertices[2][1] == pytest.approx(648.0)
    green = layers["greenscreen.mp4"]
    key = green["groups"]["ADBE Effect Parade"]["children"][0]
    assert key["match"] == "ADBE Color Key"  # Keylight is not installed in this run
    key_color = {p["name"]: p.get("value") for p in key["children"]}["Key Color"]
    assert key_color == pytest.approx([0, 200 / 255, 40 / 255, 1], abs=1e-5)
    assert green["blendingMode"] == 5216
    poster = layers["poster.png"]
    assert [e["match"] for e in poster["groups"]["ADBE Effect Parade"]["children"]] == ["ADBE Lumetri", "ADBE Mosaic"]
    assert "Wave" in poster["comment"]
    assert layers["Gold_1.svg"]["locked"] and layers["Intro-1a2b3c4d.mov"]["locked"]
    assert "Zenvi linked clip" in layers["Intro-1a2b3c4d.mov"]["comment"]
    titles = [i for i in dump["items"] if i["parent"] == "Titles"]
    assert [i["name"] for i in titles] == ["Gold_1.png"]
    with_keylight, dump2 = _run(script, media)
    green2 = {L["name"]: L for L in _comp(dump2, "Promo")["layers"]}["greenscreen.mp4"]
    assert green2["groups"]["ADBE Effect Parade"]["children"][0]["match"] == "Keylight 906"


@needs_node
def test_missing_fonts_fall_back_and_warn_once(tmp_path):
    out, script, media = _materialize("basic", tmp_path)
    result, dump = _run(script, media, "--fonts", "Arial:Bold:Arial-BoldMT,Arial:Regular:ArialMT")
    assert [w for w in result["warnings"] if "Font" in w] == [
        "Font DejaVu Sans is not installed in After Effects; titles use Arial-BoldMT instead"]
    texts = [L["groups"]["ADBE Text Properties"]["children"][0]["value"] for L in _comp(dump, "Standard_3")["layers"]]
    assert {t["font"] for t in texts} == {"Arial-BoldMT"}


@needs_node
def test_older_after_effects_without_a_font_list_or_style_reset(tmp_path):
    out, script, media = _materialize("basic", tmp_path)
    result, dump = _run(script, media, "--no-fonts-api", "--no-char-reset")
    assert result["status"] == "ok"
    texts = [L["groups"]["ADBE Text Properties"]["children"][0]["value"] for L in _comp(dump, "Standard_3")["layers"]]
    assert {t["font"] for t in texts} == {"DejaVuSans-Bold"}


@needs_node
def test_the_zenvi_link_panel_does_not_hide_the_alert_of_a_run_from_file_scripts(tmp_path):
    # After Effects shares one global scope: the panel's ZenviLink global is there for every script
    out, script, media = _materialize("basic", tmp_path)
    result, dump = _run(script, media, "--zenvi-link")
    assert result["status"] == "ok" and len(dump["alerts"]) == 1 and dump["info"]


@needs_node
def test_zenvis_runner_runs_an_interactive_export_without_its_alert(tmp_path):
    from classes.handoff.after_effects_export import quiet_runner
    out, script, media = _materialize("basic", tmp_path)
    runner = quiet_runner(str(script))
    result, dump = _run(runner, media)
    assert result["status"] == "ok" and result["layers"] == 6
    assert dump["alerts"] == [] and dump["quietFlagLeft"] is False


@needs_node
def test_an_export_for_zenvi_link_shows_no_alert(tmp_path):
    b = ProjectBuilder()
    v = b.add_file("video", path="/m/beach.mp4")
    b.add_clip(v, track=1, position=0.0, start=0.0, end=2.0)
    out, script, media = _export_project(b, tmp_path, interactive=False)
    result, dump = _run(script, media)
    assert result["status"] == "ok" and dump["alerts"] == []


# ---------------------------------------------------------------------------
# Review round 1 (state/C2-review-1.md): each runs against the mock After Effects
# ---------------------------------------------------------------------------

def _export_project(b, tmp_path, *, titles=None, interactive=True, missing=()):
    """Export a ProjectBuilder project next to real (tiny) media files; (out, script, media.json)."""
    folder = tmp_path / "export"
    media, info = {}, {}
    for f in b.data["files"]:
        name = os.path.basename(f["path"])
        target = folder / "media" / name
        if name not in missing:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"media")
        media[f["id"]] = AE.MediaRef(abs=str(target), rel="media/" + name, missing=name in missing)
        info[str(target)] = {"duration": f.get("duration") or 10.0, "width": f.get("width") or 1920,
                             "height": f.get("height") or 1080, "hasAudio": bool(f.get("has_audio")),
                             "hasVideo": f.get("media_type") != "audio", "fps": 30}
    out = AE.build_ae_script(b.snapshot(str(tmp_path / "Trip.zvn")), media_map=media, title_assets=titles or {},
                             options=AE.AeExportOptions(generator="Zenvi test", interactive=interactive))
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "Trip.jsx"
    script.write_text(out.jsx, encoding="utf-8")
    media_json = tmp_path / "media.json"
    media_json.write_text(json.dumps(info))
    return out, script, media_json


def _layers(dump, comp="Trip"):
    return {L["name"]: L for L in _comp(dump, comp)["layers"]}


@needs_node
def test_a_missing_audio_file_is_a_placeholder_that_shows_no_picture(tmp_path):
    b = ProjectBuilder()
    v = b.add_file("video", path="/m/beach.mp4")
    a = b.add_file("audio", path="/m/music.mp3")
    b.add_clip(v, track=1, position=0.0, start=0.0, end=4.0)
    b.add_clip(a, track=2, position=0.0, start=0.0, end=4.0)
    out, script, media = _export_project(b, tmp_path, missing=("music.mp3",))
    result, dump = _run(script, media)
    assert result["placeholders"] == ["music.mp3"] and any("Missing media" in w for w in result["warnings"])
    layers = _layers(dump)
    assert layers["music.mp3"]["enabled"] is False  # the colour-bar placeholder would cover the comp
    assert layers["beach.mp4"]["enabled"] is True


@needs_node
def test_a_layer_whose_audio_group_cannot_be_found_is_still_built(tmp_path):
    out, script, media = _materialize("basic", tmp_path)
    result, dump = _run(script, media, "--no-audio-group")
    assert result["status"] == "ok" and result["layers"] == 6
    notes = [w for w in result["warnings"] if "audio levels" in w]
    assert "music.mp3 audio levels: property not found" in notes
    assert all(w.endswith(" audio levels: property not found") for w in notes) and len(notes) == len(result["warnings"])
    for layer in _layers(dump).values():
        assert layer["comment"]  # the parts after the audio were set too


@needs_node
def test_file_names_with_percent_signs_are_found(tmp_path):
    # File() reads %XX as an escape: "50%20off" would otherwise be looked for as "50 off"
    b = ProjectBuilder()
    for i, name in enumerate(("clip 50%20off.mp4", "promo 50% off.mp4", "100%.mov")):
        b.add_clip(b.add_file("video", path="/m/" + name), track=1, position=3.0 * i, start=0.0, end=2.0)
    out, script, media = _export_project(b, tmp_path)
    result, dump = _run(script, media)
    assert result["placeholders"] == [] and result["warnings"] == []
    files = {os.path.basename(i["file"]) for i in dump["items"] if i["type"] == "Footage"}
    assert files == {"clip 50%20off.mp4", "promo 50% off.mp4", "100%.mov"}


@needs_node
def test_a_back_easing_move_is_keyed_as_x_and_y_position(tmp_path):
    # a spatial ease refuses negative speeds (the mock throws); separating the dimensions invalidates the
    # transform group's references (the mock does that too), so the runtime fetches it again
    b = ProjectBuilder()
    img = b.add_file("image", path="/m/photo.jpg")
    back = {"Points": [point(1, -0.3, 0, (0.5, 1.0), (0.175, 0.885)), point(31, 0.0, 0, (0.320, 1.275), (0.5, 0.0))]}
    back_y = {"Points": [point(1, -0.2, 0, (0.5, 1.0), (0.175, 0.885)), point(31, 0.1, 0, (0.320, 1.275), (0.5, 0.0))]}
    b.add_clip(img, track=1, position=0.0, start=0.0, end=3.0, location_x=back, location_y=back_y,
               rotation=kf((1, 0.0), (61, 45.0)), alpha=kf((1, 0.0), (16, 1.0)))
    out, script, media = _export_project(b, tmp_path)
    result, dump = _run(script, media)
    assert result["status"] == "ok" and result["warnings"] == []
    photo = _layers(dump)["photo.jpg"]
    pos = _prop(photo, "ADBE Transform Group", "ADBE Position")
    assert pos["separated"] and [len(f.get("keys", [])) for f in pos["followers"][:2]] == [2, 2]
    assert min(pos["followers"][0]["keys"][1]["inEase"][0][0], pos["followers"][0]["keys"][0]["outEase"][0][0]) < 0
    assert len(_prop(photo, "ADBE Transform Group", "ADBE Rotate Z")["keys"]) == 2
    assert len(_prop(photo, "ADBE Transform Group", "ADBE Opacity")["keys"]) == 2


@needs_node
def test_a_key_that_is_bezier_on_one_side_keeps_its_ease(tmp_path):
    # making a side Bezier recomputes its ease in the mock (as After Effects may): type first, then ease
    b = ProjectBuilder()
    v = b.add_file("video", path="/m/beach.mp4")
    b.add_clip(v, track=1, position=0.0, start=0.0, end=3.0,
               alpha=kf((1, 0.0, 0), (16, 1.0, 0), (31, 0.5, LINEAR)))
    out, script, media = _export_project(b, tmp_path)
    spec = out.data["layers"][0]["tf"]["op"]
    assert spec["i"] == ["b", "l"]
    result, dump = _run(script, media)
    key = _prop(_layers(dump)["beach.mp4"], "ADBE Transform Group", "ADBE Opacity")["keys"][1]
    assert (key["in"], key["out"]) == ("bezier", "linear")
    assert key["inEase"] == [[pytest.approx(spec["n"][0][0][0]), pytest.approx(spec["n"][0][0][1])]]


@needs_node
def test_a_wrongly_guessed_effect_parameter_id_never_sets_another_parameter(tmp_path):
    # --wrong-param-ids: Mosaic -0001 / -0003 and Color Key -0001 / -0002 hold each other's parameters
    out, script, media = _materialize("effects", tmp_path)
    mosaic = next(fx for L in out.data["layers"] for fx in L.get("fx", []) if fx["opts"][0]["match"] == "ADBE Mosaic")
    blocks = mosaic["opts"][0]["params"][0]["val"]
    assert not isinstance(blocks, dict)  # a static value in this fixture
    color = [0, 200 / 255, 40 / 255, 1]
    # English After Effects: an unverified id holding another name falls back to the display name
    result, dump = _run(script, media, "--wrong-param-ids", "--no-keylight")
    params = {p["name"]: p for p in _mosaic_params(dump)}
    assert params["Horizontal Blocks"]["match"] == "ADBE Mosaic-0003"
    assert params["Horizontal Blocks"]["value"] == pytest.approx(blocks) and params["Sharp Colors"]["value"] == 0
    assert not [w for w in result["warnings"] if "Mosaic" in w or "Color Key" in w or "Pixelate" in w]
    key = {p["name"]: p.get("value") for p in _effect(dump, "greenscreen.mp4", "ADBE Color Key")["children"]}
    assert key["Key Color"] == pytest.approx(color, abs=1e-5)
    # another language: names cannot be compared, but a parameter of the wrong kind is never set (the
    # colour does not land on Color Tolerance, nor the tolerance on Key Color)
    result, dump = _run(script, media, "--wrong-param-ids", "--no-keylight", "--language", "de_DE")
    key = {p["name"]: p.get("value") for p in _effect(dump, "greenscreen.mp4", "ADBE Color Key")["children"]}
    assert isinstance(key["Color Tolerance"], (int, float)) and key["Key Color"] == pytest.approx(color, abs=1e-5)


def _effect(dump, layer, match, comp="Promo"):
    fx = _comp(dump, comp)
    L = next(x for x in fx["layers"] if x["name"] == layer)
    return next(e for e in L["groups"]["ADBE Effect Parade"]["children"] if e["match"] == match)


def _mosaic_params(dump):
    for L in _comp(dump, "Promo")["layers"]:
        for e in L["groups"]["ADBE Effect Parade"]["children"]:
            if e["match"] == "ADBE Mosaic":
                return e["children"]
    raise KeyError("ADBE Mosaic")


@needs_node
def test_time_remap_keeps_its_keys_when_after_effects_rounds_key_times(tmp_path):
    # the mock rounds every key time to 1/24 s: the old cleanup (drop keys not at our exact times)
    # deleted our own keys; now only the two keys After Effects added are removed
    b = ProjectBuilder()
    v = b.add_file("video", path="/m/long.mp4", duration=60.0)
    c = b.add_clip(v, track=1, position=1.1, start=0.0, end=3.0)
    b.clip(c)["time"] = kf((1, 91, LINEAR), (91, 1, LINEAR))
    out, script, media = _export_project(b, tmp_path)
    want = out.data["layers"][0]["remap"]
    result, dump = _run(script, media, "--key-time-grid", "24")
    keys = _layers(dump)["long.mp4"]["timeRemap"]["keys"]
    assert [k["v"] for k in keys] == [pytest.approx(v, abs=1e-5) for v in want["v"]]
    assert [k["t"] for k in keys] == [pytest.approx(round(t * 24) / 24) for t in want["t"]]


@needs_node
@pytest.mark.parametrize("installed, font, warned", [
    ("Ubuntu:Regular:Ubuntu-Regular,Arial:Regular:ArialMT", "Ubuntu-Regular", False),
    ("Arial:Regular:ArialMT", "ArialMT", True),
])
def test_older_after_effects_finds_fonts_by_their_usual_postscript_names(tmp_path, installed, font, warned):
    from classes.exporters.after_effects_titles import parse_title_svg
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">'
           '<text x="960" y="540" style="font-family:Ubuntu;font-size:80px;fill:#ffffff">Hello</text></svg>')
    title = tmp_path / "Hello.svg"
    title.write_text(svg, encoding="utf-8")
    b = ProjectBuilder()
    t = b.add_title(str(title))
    b.add_clip(t, track=1, position=0.0, start=0.0, end=2.0)
    out, script, media = _export_project(b, tmp_path, titles={t: AE.TitleAsset("native", layout=parse_title_svg(svg))})
    result, dump = _run(script, media, "--no-fonts-api", "--fonts", installed)
    texts = [L["groups"]["ADBE Text Properties"]["children"][0]["value"] for L in _comp(dump, "Hello")["layers"]]
    assert [t["font"] for t in texts] == [font]
    notes = [w for w in result["warnings"] if "Font" in w]
    assert bool(notes) is warned
    if warned:
        assert "usual PostScript names" in notes[0] and "titles use ArialMT instead" in notes[0]


@needs_node
def test_an_after_effects_link_reuses_the_comp_in_the_open_project(tmp_path):
    from ae_project import ProjectBuilder
    b = ProjectBuilder()
    aep = tmp_path / "promo.aep"
    aep.write_bytes(b"aep")
    link = {"version": 1, "kind": "aftereffects", "source": {"composition": "Promo", "composition_key": 1,
                                                             "aep": str(aep)}, "props": {}}
    render = tmp_path / "Promo.mov"
    render.write_bytes(b"mov")
    v = b.add_file("video", path=str(render), zenvi_link=link)
    b.add_clip(v, track=1, position=0.0, start=0.0, end=2.0)
    out = AE.build_ae_script(b.snapshot(), media_map={v: AE.MediaRef(abs=str(render))})
    # the open project already holds the comp: make it before the export runs and point the link at its id
    script = tmp_path / "s.jsx"
    script.write_text(out.jsx.replace("    return zenviBuild(ZENVI_EXPORT, DATA);",
                                      '    var promo = app.project.items.addComp("Promo", 100, 100, 1, 10, 30);\n'
                                      "    DATA.footage[0].aecomp.id = promo.id;\n"
                                      "    return zenviBuild(ZENVI_EXPORT, DATA);"), encoding="utf-8")
    result, dump = _run(script, None, "--project-file", str(aep))
    assert result["footage"] == 1
    promo = _comp(dump, "Promo")
    layer = _comp(dump, "Trip")["layers"][0]
    assert layer["source"] == "Promo" and layer["sourceId"] == promo["id"]
    assert not any(i["type"] == "Footage" for i in dump["items"])


@needs_node
def test_the_mock_rejects_stale_references_and_bad_ease_arrays(tmp_path):
    """The mock is strict where After Effects is (so a passing export run means something)."""
    stale = tmp_path / "stale.jsx"
    stale.write_text('var c = app.project.items.addComp("c", 100, 100, 1, 10, 30); var L = c.layers.addShape();'
                     ' var fx = L.property("ADBE Effect Parade"); var a = fx.addProperty("ADBE Sharpen");'
                     ' fx.addProperty("ADBE Invert"); a.name;', encoding="utf-8")
    proc = subprocess.run([NODE, MOCK, str(stale)], capture_output=True, text=True, timeout=60)
    assert "Object is invalid" in json.loads(proc.stdout)["error"]
    ease = tmp_path / "ease.jsx"
    ease.write_text('var c = app.project.items.addComp("c", 100, 100, 1, 10, 30); var L = c.layers.addShape();'
                    ' var s = L.property("ADBE Transform Group").property("ADBE Scale");'
                    ' s.setValuesAtTimes([0, 1], [[50, 50, 100], [100, 100, 100]]);'
                    ' s.setTemporalEaseAtKey(1, [new KeyframeEase(0, 50)], [new KeyframeEase(0, 50)]);',
                    encoding="utf-8")
    proc = subprocess.run([NODE, MOCK, str(ease)], capture_output=True, text=True, timeout=60)
    assert "must have 3 KeyframeEase" in json.loads(proc.stdout)["error"]
    es5 = tmp_path / "es5.jsx"
    es5.write_text("[1, 2].indexOf(2);", encoding="utf-8")
    proc = subprocess.run([NODE, MOCK, str(es5)], capture_output=True, text=True, timeout=60)
    assert "indexOf" in json.loads(proc.stdout)["error"]
