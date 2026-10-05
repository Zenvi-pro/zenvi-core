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
    assert [k["t"] for k in remap] == [6, 10] and remap[0]["v"] == pytest.approx(4.0) and remap[1]["v"] == 0
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
def test_inside_zenvi_link_the_script_shows_no_alert(tmp_path):
    out, script, media = _materialize("basic", tmp_path)
    result, dump = _run(script, media, "--zenvi-link")
    assert result["status"] == "ok" and dump["alerts"] == [] and dump["info"]


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
