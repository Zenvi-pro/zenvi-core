"""import_hyperframes_project_tool / export_to_hyperframes_tool through execute_tool, like the chat calls them.

Renders are faked (the provider's render writes a small file; ``probe_media`` reads its length back),
media are measured by a fake ffprobe, and the HyperFrames CLI counts as not installed, so timing
comes from Zenvi's own parser -- the real CLI is exercised by the end-to-end run.
"""

import json
import os
import re

import pytest

from classes.handoff import linked_media as lm
from classes.handoff.hyperframes import cli as hf_cli
from classes.handoff.hyperframes import importer
from classes.handoff.hyperframes import provider as hfprov
from classes.handoff.linked_media import RenderResult
from handoff_fakes import linked, tt  # noqa: F401
from test_handoff_hyperframes_parser import root_div, write_project

MEDIA = {".mp4": {"width": 1280, "height": 720, "duration": 20.0, "has_video": True, "has_audio": True},
         ".png": {"width": 400, "height": 200, "duration": 0.0, "has_video": True, "has_audio": False},
         ".wav": {"width": 0, "height": 0, "duration": 5.0, "has_video": False, "has_audio": True}}
INTRO = """<html data-composition-variables='[{"id":"headline","type":"string","default":"Hello"},
{"id":"accent","type":"color","default":"#FF5A36"}]'><body>
<template><div data-composition-id="intro" data-width="1920" data-height="1080"><b class="x">Hi</b>
<script>const tl = gsap.timeline({paused:true}); tl.from(".x", {opacity: 0, duration: 0.6}, 0);
tl.to(".x", {opacity: 0, duration: 0.4}, 2.2); window.__timelines["intro"] = tl;</script></div></template>
</body></html>"""


@pytest.fixture
def hf(linked, monkeypatch):  # noqa: F811
    def no_cli(*a, **kw):
        raise hf_cli.CliError("HyperFrames needs Node.js 22 or newer (test)")
    monkeypatch.setattr(hf_cli, "resolve_cli", no_cli)
    monkeypatch.setattr(importer, "ffprobe_media",
                        lambda path: dict(MEDIA.get(os.path.splitext(str(path))[1].lower(), MEDIA[".mp4"])))
    renders = []
    fake_probe = lm.probe_media

    def render(self, link, out_dir, *, on_progress, should_cancel):
        role = (link.get("hyperframes") or {}).get("role")
        seconds = {"project": 6.0, "composition": 2.6, "layer": 6.0}[role]
        ext = ".mp4" if role == "project" else ".mov"
        path = os.path.join(out_dir, "render" + ext)
        with open(path, "w") as fh:
            fh.write("render %s %s" % (role, seconds))
        renders.append(json.loads(json.dumps(link)))
        on_progress(1.0, "done")
        return RenderResult(path=path, codec="h264" if role == "project" else "prores4444", width=1920,
                            height=1080, fps=30, duration_frames=int(seconds * 30))

    def probe(path):
        try:
            text = open(path).read(64)
        except (OSError, UnicodeDecodeError):
            text = ""
        if text.startswith("render "):
            linked.probe.durations[path] = float(text.split()[2])
        return fake_probe(path)

    monkeypatch.setattr(hfprov.HyperFramesProvider, "render", render)
    monkeypatch.setattr(lm, "probe_media", probe)
    hfprov.register()
    linked.renders = renders
    return linked


def project(tmp_path, *, script="", extra_root="", name="hf"):
    return write_project(tmp_path / name, root_div(
        '<video id="bg" class="clip" src="assets/a.mp4" data-start="0" data-duration="4" data-track-index="0" muted>'
        '</video><img id="logo" class="clip" src="assets/logo.png" data-start="bg - 1" data-duration="2" '
        'data-track-index="1" style="left: 100px; top: 50px; width: 400px; height: 200px"/>'
        '<audio id="music" src="assets/m.wav" data-start="0" data-duration="5" data-volume="0.5" '
        'data-fade-out="1" data-track-index="2"></audio>'
        '<div id="intro" data-composition-id="intro" data-composition-src="compositions/intro.html" data-start="1" '
        'data-track-index="3" data-variable-values=\'{"headline":"Launch"}\'></div>'
        '<h1 id="title" class="clip" data-start="4" data-duration="2" data-track-index="4">Hello</h1>' + extra_root,
        extra='data-width="1920" data-height="1080" data-duration="6"'),
        files={"assets/a.mp4": b"v", "assets/logo.png": b"i", "assets/m.wav": b"a",
               "compositions/intro.html": INTRO},
        style=".clip { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }",
        script=('const tl = gsap.timeline({paused:true});'
                'tl.fromTo("#logo", {opacity: 0, x: -40}, {opacity: 1, x: 0, duration: 0.6, ease: "power2.out"}, 3);'
                + script + 'window.__timelines["main"] = tl;'))


def _failed(r):
    return r["status"] in ("error", "refused") and r["summary"].startswith("Error") and r["undoSteps"] == 0


def _clips_by_kind(r):
    out = {}
    for c in r["data"]["clips"]:
        out.setdefault(c["kind"], []).append(c)
    return out


# --- import ------------------------------------------------------------------------------------------------

def test_native_import_is_one_undo_step(hf, tmp_path):
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(tmp_path), position=2.0)
    assert r["status"] == "applied" and r["undoSteps"] == 1, r["summary"]
    d = r["data"]
    assert d["mode"] == "native" and (d["native"], d["linked"], d["restored"]) == (3, 2, 0)
    kinds = _clips_by_kind(r)
    native = {c["element"]: c for c in kinds["native"]}
    assert (native["bg"]["position"], native["bg"]["end"]) == (2.0, 6.0)
    assert (native["logo"]["position"], native["logo"]["end"]) == (5.0, 7.0)
    linked_clips = {c["role"]: c for c in kinds["linked"]}
    assert linked_clips["composition"]["position"] == 3.0 and linked_clips["composition"]["end"] == 5.6
    assert linked_clips["layer"]["position"] == 2.0
    # tracks: one per data-track-index (bottom up), the graphics layer on top
    layers = {c["element"] if c["kind"] == "native" else c["role"]: c["layer"] for c in d["clips"]}
    assert layers["bg"] < layers["logo"] < layers["music"] < layers["composition"] < layers["layer"]
    # the logo's GSAP slide is keyframes on its native clip; its link-free file is a plain import
    logo = hf.clip(native["logo"]["timeline_clip_id"])
    assert [p["co"]["X"] for p in logo["alpha"]["Points"]] == [1.0, 19.0]
    comp_file = hf.file(linked_clips["composition"]["file_id"])
    link = comp_file["zenvi_link"]
    assert link["kind"] == "hyperframes" and link["hyperframes"]["role"] == "composition"
    assert link["hyperframes"]["host"] == "intro"
    assert link["props"] == {"headline": "Launch", "accent": "#FF5A36"}  # every variable, for Edit Props
    assert link["source"]["entry"] == "compositions/intro.html" and link["render"]["codec"] == "prores4444"
    layer_link = hf.file(linked_clips["layer"]["file_id"])["zenvi_link"]
    assert sorted(layer_link["hyperframes"]["exclude"]) == ["bg", "intro", "logo", "music"]
    assert lm.check_link(comp_file).state == "fresh"  # a fresh import reads fresh
    hf.undo()
    assert not hf.clips() and not [f for f in hf.get("files") if f.get("zenvi_link")]
    hf.redo()
    assert len(hf.clips()) == 5


def test_flatten_and_auto_decisions(hf, tmp_path):
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(tmp_path), mode="flatten")
    assert r["data"]["mode"] == "flatten" and r["data"]["linked"] == 1 and r["undoSteps"] == 1
    (clip,) = r["data"]["clips"]
    link = hf.file(clip["file_id"])["zenvi_link"]
    assert link["hyperframes"]["role"] == "project" and link["render"]["codec"] == "h264"
    assert hf.renders[-1]["source"]["entry"] == "index.html"
    # an animation Zenvi cannot rebuild exactly: auto flattens and says why
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(
        tmp_path, name="hf2", script='tl.to("#bg", {x: 10, stagger: 0.1}, 0);'))
    assert r["data"]["mode"] == "flatten" and "stagger" in r["data"]["reason"]
    assert "bg" in r["data"]["problems"]
    # ...and native mode imports it anyway, without that animation, listing the problem
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(
        tmp_path, name="hf3", script='tl.to("#bg", {x: 10, stagger: 0.1}, 0);'), mode="native")
    assert r["data"]["mode"] == "native" and r["data"]["native"] == 3 and "bg" in r["data"]["problems"]


def test_auto_flattens_a_project_without_media(hf, tmp_path):
    root = write_project(tmp_path / "init", root_div('<h1 id="t" class="clip" data-start="0" data-duration="3">'
                                                     'Title</h1>', extra='data-duration="3"'))
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=root)
    assert r["data"]["mode"] == "flatten" and "no local media" in r["data"]["reason"]


def test_refusals_leave_history_untouched(hf, tmp_path):
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=str(tmp_path / "nope"))
    assert _failed(r) and "not a folder" in r["summary"]
    (tmp_path / "empty").mkdir()
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=str(tmp_path / "empty"))
    assert _failed(r) and "no index.html" in r["summary"]
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(tmp_path), mode="weird")
    assert r["status"] == "refused" and "must be one of" in r["summary"]
    cyclic = write_project(tmp_path / "cyc", root_div(
        '<div id="a" class="clip" data-start="b" data-duration="1"></div>'
        '<div id="b" class="clip" data-start="a" data-duration="1"></div>'))
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=cyclic)
    assert _failed(r) and "cycle" in r["summary"]
    assert hf.undo_steps_since_mark() == 0 and not hf.clips()


def test_a_failed_render_adds_nothing_and_leaves_no_files(hf, tmp_path, monkeypatch):
    calls = []

    def boom(self, link, out_dir, *, on_progress, should_cancel):
        calls.append(link)
        if len(calls) == 2:
            raise lm.LinkError("the HyperFrames render failed (exit 1): Chrome cannot start")
        path = os.path.join(out_dir, "render.mov")
        open(path, "w").write("render composition 2.6")
        return RenderResult(path=path, codec="prores4444", width=1920, height=1080, fps=30, duration_frames=78)
    monkeypatch.setattr(hfprov.HyperFramesProvider, "render", boom)
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(tmp_path))
    assert _failed(r) and "Chrome cannot start" in r["summary"]
    assert hf.undo_steps_since_mark() == 0 and not hf.clips()
    links = os.path.join(hf.user_path, "links", "hyperframes")
    assert not [n for n in os.listdir(links) if not n.startswith(".")]  # the first render was removed too


def test_track_argument(hf, tmp_path):
    hf.add_clip(hf.add_file("video"), position=0.0, layer=2000000)
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(tmp_path), track="3",
                        position=30.0)
    assert r["status"] == "applied", r["summary"]
    layers = sorted({c["layer"] for c in r["data"]["clips"]})
    assert layers[0] == 3000000 and r["data"]["created_tracks"]  # tracks 3, 4, 5 then new ones on top
    hf.lock_track(1000000)
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(tmp_path, name="b"), track="1")
    assert _failed(r) and "locked" in r["summary"]
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(tmp_path, name="c"), track="2",
                        position=0.0)
    assert _failed(r) and "already has" in r["summary"]


# --- export and the round trip -------------------------------------------------------------------------------

def _zenvi_timeline(hf):
    v = hf.add_file("video", path=os.path.join(hf.user_path, "clip.mp4"), duration=10.0)
    open(os.path.join(hf.user_path, "clip.mp4"), "wb").write(b"v")
    i = hf.add_file("image", path=os.path.join(hf.user_path, "logo.png"))
    open(os.path.join(hf.user_path, "logo.png"), "wb").write(b"i")
    a = hf.add_clip(v, position=0.0, layer=1000000, start=0.5, end=3.5)
    b = hf.add_clip(i, position=1.0, layer=2000000, end=2.0, alpha={"Points": [
        {"co": {"X": 1.0, "Y": 0.0}, "interpolation": 0, "handle_left": {"X": 0.5, "Y": 1.0},
         "handle_right": {"X": 0.5, "Y": 0.0}, "handle_type": 0},
        {"co": {"X": 16.0, "Y": 1.0}, "interpolation": 1}]})
    return a, b


def test_export_then_import_restores_the_same_clips(hf, tmp_path, monkeypatch):
    monkeypatch.setattr(hf_cli, "lint", lambda d, **kw: {"ok": True, "errorCount": 0, "warningCount": 0,
                                                         "findings": []})
    a, b = _zenvi_timeline(hf)
    before = {c["id"]: json.loads(json.dumps(c)) for c in hf.clips()}
    out = str(tmp_path / "export")
    r = hf.call_receipt("export_to_hyperframes_tool", output_dir=out)
    assert r["status"] in ("applied", "unchanged") and r["undoSteps"] == 0, r["summary"]
    assert r["data"]["clips"] == 2 and r["data"]["lint"]["ok"] is True
    assert re.search(r'data-zenvi-clip-id="%s"' % a, open(os.path.join(out, "index.html")).read())
    # take the clips out (not undoable setup), then bring the export back
    hf.store._data["clips"] = []
    hf.mark()
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=out, position=0.0)
    assert r["status"] == "applied" and r["undoSteps"] == 1, r["summary"]
    assert r["data"]["mode"] == "restore" and r["data"]["restored"] == 2 and r["data"]["linked"] == 0
    after = {c["id"]: c for c in hf.clips()}
    assert set(after) == set(before)
    for cid, clip in before.items():
        assert after[cid] == clip, cid
    hf.undo()
    assert not hf.clips()


def test_export_refusals(hf, tmp_path):
    r = hf.call_receipt("export_to_hyperframes_tool", output_dir=str(tmp_path / "x"))
    assert _failed(r) and "no clips" in r["summary"]
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("x")
    _zenvi_timeline(hf)
    r = hf.call_receipt("export_to_hyperframes_tool", output_dir=str(busy))
    assert _failed(r) and "not empty" in r["summary"]
    assert (busy / "keep.txt").read_text() == "x" and hf.undo_steps_since_mark() == 0


def test_tools_are_registered_with_their_coverage():
    from classes.editor_tools import REGISTRY
    imp, exp = REGISTRY["import_hyperframes_project_tool"], REGISTRY["export_to_hyperframes_tool"]
    assert imp.covers == ("handoff.hyperframes_import",) and imp.background_safe and not imp.read_only
    assert exp.covers == ("handoff.hyperframes_export",) and exp.read_only
    assert set(imp.schema["properties"]) == {"project_dir", "mode", "position", "track"}
    assert set(exp.schema["properties"]) == {"output_dir", "copy_media"}


def test_restore_with_clips_added_in_hyperframes(hf, tmp_path, monkeypatch):
    monkeypatch.setattr(hf_cli, "lint", lambda d, **kw: {"ok": True})
    a, _b = _zenvi_timeline(hf)
    for layer in hf.store._data["layers"]:
        if layer["number"] == 2000000:
            layer["label"] = "Logos"
    out = str(tmp_path / "export")
    assert hf.call_receipt("export_to_hyperframes_tool", output_dir=out)["status"] in ("applied", "unchanged")
    index = os.path.join(out, "index.html")
    html = open(index, encoding="utf-8").read()
    html = html.replace("\n    </div>\n    <script type=\"application/json\"",
                        '\n      <img id="added" class="clip" src="assets/logo.png" data-start="5" data-duration="1" '
                        'data-track-index="0" />\n      <h1 id="hello" class="clip" data-start="0" data-duration="1">'
                        'Hi</h1>\n    </div>\n    <script type="application/json"', 1)
    open(index, "w", encoding="utf-8").write(html)
    hf.store._data["clips"] = []
    for layer in hf.store._data["layers"]:
        layer["label"] = ""
    hf.mark()
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=out, position=0.0)
    assert r["status"] == "applied" and r["undoSteps"] == 1, r["summary"]
    d = r["data"]
    assert (d["mode"], d["restored"], d["native"], d["linked"]) == ("restore", 2, 1, 1)
    by_kind = _clips_by_kind(r)
    added = by_kind["native"][0]
    restored_a = next(c for c in by_kind["restored"] if c["timeline_clip_id"] == a)
    assert added["layer"] == restored_a["layer"] == 1000000  # shares the exported track 1: it is free at 5 s
    assert by_kind["linked"][0]["role"] == "layer" and by_kind["linked"][0]["layer"] > 2000000
    labels = {ly["number"]: ly["label"] for ly in hf.store._data["layers"]}
    assert labels[2000000] == "Logos"  # the exported track's name came back
    hf.undo()
    assert not hf.clips() and {ly["number"]: ly["label"] for ly in hf.store._data["layers"]}[2000000] == ""


def test_graphics_under_and_over_the_media_get_their_own_layers(hf, tmp_path):
    root = write_project(tmp_path / "split", root_div(
        '<div id="backdrop" style="position:absolute; inset:0; background: linear-gradient(#123, #456)"></div>'
        '<video id="v" class="clip" src="assets/a.mp4" data-start="0" data-duration="3" muted></video>'
        '<div id="inline" data-composition-id="inline" data-start="1" data-track-index="5"><i>x</i><script>'
        'const c = gsap.timeline({paused:true}); c.to("i", {opacity: 0}, 1); window.__timelines["inline"] = c;'
        '</script></div>'
        '<h1 id="t" class="clip" data-start="0" data-duration="3">Over</h1>', extra='data-duration="3"'),
        files={"assets/a.mp4": b"v"}, style="html, body { background: #0a0a0a; }",
        script='const tl = gsap.timeline({paused:true}); window.__timelines["main"] = tl;')
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=root, mode="native", position=0.0)
    assert r["status"] == "applied", r["summary"]
    linked_clips = _clips_by_kind(r)["linked"]
    layers = sorted((c for c in linked_clips if c["role"] == "layer"), key=lambda c: c["layer"])
    native = _clips_by_kind(r)["native"][0]
    assert len(layers) == 2 and layers[0]["layer"] < native["layer"] < layers[1]["layer"]
    under = hf.file(layers[0]["file_id"])["zenvi_link"]["hyperframes"]["exclude"]
    over = hf.file(layers[1]["file_id"])["zenvi_link"]["hyperframes"]["exclude"]
    assert "t" in under and "backdrop" in over and "v" in under and "v" in over
    inline = next(c for c in linked_clips if c["role"] == "composition")
    link = hf.file(inline["file_id"])["zenvi_link"]
    assert link["hyperframes"]["host"] == "inline" and link["source"]["entry"] == "index.html"
    assert any("background (#0a0a0a) is not imported" in w for w in r["data"]["warnings"])


def test_linked_compositions_take_new_variables_and_rerender(hf, tmp_path):
    r = hf.call_receipt("import_hyperframes_project_tool", project_dir=project(tmp_path), position=0.0)
    comp = next(c for c in r["data"]["clips"] if c.get("role") == "composition")
    hf.mark()
    up = hf.call_receipt("update_linked_clip_tool", file_id=comp["file_id"], props={"headline": "Ship it"})
    assert up["status"] == "applied" and up["undoSteps"] == 1, up["summary"]
    assert hf.renders[-1]["props"] == {"headline": "Ship it", "accent": "#FF5A36"}
    info = hf.call_receipt("get_linked_clip_tool", file_id=comp["file_id"])
    assert info["data"]["editable_props"] == {"headline": "Ship it", "accent": "#FF5A36"}
    assert info["data"]["state"] == "fresh" and info["data"]["can_open_studio"]
