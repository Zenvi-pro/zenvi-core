"""The HyperFrames link provider: freshness, render wrappers, render flow and codecs, props, opening."""

import json
import os
import re

import pytest

from classes.handoff import linked_media as lm
from classes.handoff.hyperframes import cli as hf_cli
from classes.handoff.hyperframes import parser as hfp
from classes.handoff.hyperframes import provider as hfprov
from classes.handoff.hyperframes import wrappers
from test_handoff_hyperframes_parser import root_div, write_project

INTRO = """<html data-composition-variables='[{"id":"headline","type":"string","default":"Hello"},
{"id":"accent","type":"color","default":"#FF5A36"}]'><body><template id="t">
<div data-composition-id="intro" data-width="1920" data-height="1080"><img class="x" src="assets/logo.png"/>
<script>const tl = gsap.timeline({paused:true}); tl.from(".x", {opacity: 0}, 0); window.__timelines["intro"] = tl;
</script></div></template></body></html>"""


def make(tmp_path):
    return write_project(tmp_path / "hf", root_div(
        '<video id="bg" class="clip" src="assets/a.mp4" data-start="0" data-duration="4" muted></video>'
        '<div id="intro" data-composition-id="intro" data-composition-src="compositions/intro.html" data-start="1" '
        'data-track-index="2" data-variable-values=\'{"headline":"Launch"}\'></div>'
        '<div id="caps" data-composition-id="caps" data-start="0" data-track-index="3"><b>x</b><script>'
        'const c = gsap.timeline({paused:true}); c.to("b", {opacity:0}, 1); window.__timelines["caps"] = c;'
        '</script></div><h1 id="title" class="clip" data-start="0" data-duration="2" '
        'style="background-image: url(assets/logo.png)">Hi</h1>', extra='data-width="1920" data-height="1080" '
        'data-duration="6"'),
        files={"assets/a.mp4": b"v", "assets/logo.png": b"i", "compositions/intro.html": INTRO,
               "styles.css": "h1 { color: red }"},
        style=".clip { position: absolute }", html_attrs="data-composition-variables='[{\"id\":\"brand\","
                                                         "\"default\":\"Zenvi\"}]'",
        script='const tl = gsap.timeline({paused:true}); window.__timelines["main"] = tl;')


def link(root, role="project", props=None, composition=None, **block):
    data = {"role": role, "fps": "30"}
    data.update(block)
    entry = "compositions/intro.html" if role == "composition" and block.get("host") == "intro" else "index.html"
    # as the importer records it: a composition link names the composition its host mounts
    composition = composition or (block.get("host") if role == "composition" else "main")
    return {"kind": "hyperframes", "source": {"project_dir": root, "entry": entry, "composition": composition,
                                              "file": entry, "line": 3},
            "props": dict(props or {}), "hyperframes": data}


@pytest.fixture
def provider():
    return hfprov.HyperFramesProvider()


def _rewrite(path, change):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    new = change(text)
    assert new != text, "the edit changed nothing"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)


# --- freshness ---------------------------------------------------------------------------------------------

def test_fingerprint_ignores_studio_reserialization_and_render_scratch(tmp_path, provider):
    root = make(tmp_path)
    lk = link(root)
    before = provider.fingerprint(lk)
    index = os.path.join(root, "index.html")
    html = open(index).read()
    # what HyperFrames Studio does on first open: stamp ids, re-escape attributes, upper-case the doctype
    stamped = html.replace("<!doctype html>", "<!DOCTYPE html>").replace('<video id="bg"',
                                                                          '<video data-hf-id="hf-1" id="bg"')
    stamped = stamped.replace("""data-variable-values='{"headline":"Launch"}'""",
                              'data-variable-values="{&quot;headline&quot;:&quot;Launch&quot;}"')
    open(index, "w").write(stamped)
    os.makedirs(os.path.join(root, ".hyperframes", "preview"))
    open(os.path.join(root, ".hyperframes", "hf-ids-stamped.json"), "w").write("{}")
    os.makedirs(os.path.join(root, wrappers.WRAPPER_PREFIX + "abcd"))
    open(os.path.join(root, wrappers.WRAPPER_PREFIX + "abcd", "index.html"), "w").write("<div>wrapper</div>")
    open(os.path.join(root, ".DS_Store"), "w").write("finder")
    assert provider.fingerprint(lk) == before


@pytest.mark.parametrize("change", ["html", "composition", "css", "asset", "props", "exclude", "fps"])
def test_fingerprint_follows_real_changes(tmp_path, provider, change):
    root = make(tmp_path)
    lk = link(root, role="layer", exclude=["bg"])
    before = provider.fingerprint(lk)
    if change == "html":
        path = os.path.join(root, "index.html")
        _rewrite(path, lambda text: text.replace(">Hi<", ">Hello<"))
    elif change == "composition":
        path = os.path.join(root, "compositions", "intro.html")
        _rewrite(path, lambda text: text.replace("opacity: 0", "opacity: 0.5"))
    elif change == "css":
        open(os.path.join(root, "styles.css"), "w").write("h1 { color: blue }")
    elif change == "asset":
        open(os.path.join(root, "assets", "logo.png"), "wb").write(b"other")
    elif change == "props":
        lk = dict(lk, props={"brand": "X"})
    elif change == "exclude":
        lk = link(root, role="layer", exclude=["bg", "intro"])
    else:
        lk = link(root, role="layer", exclude=["bg"], fps="25")
    assert provider.fingerprint(lk) != before


def test_fingerprint_is_a_pure_function_of_the_link(tmp_path, provider):
    """render_link fingerprints before and after it fills the render metadata: they must agree."""
    root = make(tmp_path)
    lk = link(root, role="composition", props={"headline": "Launch"}, host="intro")
    filled = json.loads(json.dumps(lk))
    filled["render"] = {"codec": "prores4444", "width": 1920, "height": 1080, "fps": {"num": 30, "den": 1},
                        "duration_frames": 78, "output": "@assets/links/hyperframes/x.mov", "fingerprint": "sha256:1"}
    filled["state"] = "fresh"
    filled["source"]["line"] = 9
    assert provider.fingerprint(filled) == provider.fingerprint(lk)


def test_missing_sources(tmp_path, provider):
    root = make(tmp_path)
    with pytest.raises(lm.SourceMissing):
        provider.fingerprint(link(str(tmp_path / "gone")))
    os.unlink(os.path.join(root, "index.html"))
    with pytest.raises(lm.SourceMissing, match="index.html is gone"):
        provider.fingerprint(link(root))


# --- wrappers -------------------------------------------------------------------------------------------------

def test_composition_wrapper_mounts_one_host_on_transparent(tmp_path):
    root = make(tmp_path)
    p = hfp.load_project(root)
    host = p.root.clip("intro").element
    html = wrappers.composition_wrapper(p.index, host, width=1920, height=1080, variables={"headline": "Ship"},
                                        fps="25")
    w = hfp.parse_html(html)
    comp = next(e for e in w.iter() if e.attrs.get("data-composition-id") == "intro")
    assert comp.attrs["data-composition-src"] == "compositions/intro.html"  # root-relative, never rebased
    assert comp.attrs["data-start"] == "0" and json.loads(comp.attrs["data-variable-values"]) == {"headline": "Ship"}
    root_el = next(e for e in w.iter() if e.attrs.get("data-composition-id") == wrappers.WRAPPER_ROOT_ID)
    assert root_el.attrs["data-fps"] == "25" and root_el.attrs["data-root"] == "true"
    assert "background: transparent !important" in html and "gsap.min.js" in html
    assert '.clip { position: absolute }' in html  # the root's styles come along


def test_document_wrapper_removes_rebases_and_overrides(tmp_path):
    root = make(tmp_path)
    p = hfp.load_project(root)
    bg, intro = p.root.clip("bg").element, p.root.clip("intro").element
    html = wrappers.document_wrapper(p.index, remove=[bg], overrides={id(intro): {"data-start": "0"}})
    w = hfp.parse_html(html)
    assert w.by_id("bg") is None and w.by_id("intro").attrs["data-start"] == "0"
    assert w.by_id("intro").attrs["data-composition-src"] == "compositions/intro.html"
    assert "url(../assets/logo.png)" in w.by_id("title").attrs["style"]
    assert 'src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"' in html  # absolute: untouched
    assert html.startswith("<!doctype html>") and "background: transparent !important" in html
    assert 'window.__timelines["main"] = tl' in html  # scripts kept verbatim


def test_rebase_rules():
    assert wrappers.rebase_url("assets/a.png") == "../assets/a.png"
    for url in ("/assets/a.png", "https://x/a.png", "data:image/png;base64,AA", "#frag", "//cdn/x.js"):
        assert wrappers.rebase_url(url) == url
    assert wrappers.rebase_css("a{background:url('img/x.png')} @import \"f.css\";") == \
        "a{background:url('../img/x.png')} @import \"../f.css\";"
    assert wrappers.rebase_srcset("a.png 1x, /b.png 2x") == "../a.png 1x, /b.png 2x"


def test_wrapper_folder_is_hidden_and_removed(tmp_path):
    with wrappers.wrapper_folder(str(tmp_path)) as folder:
        assert os.path.basename(folder).startswith(".render-zenvi-") and os.path.isdir(folder)
    assert not os.path.exists(folder)


# --- rendering --------------------------------------------------------------------------------------------------

class FakeCli:
    def __init__(self, tmp_path, monkeypatch, *, opaque=False, fail=None):
        self.calls = []
        self.encodes = []
        self.seen_wrappers = []
        self.opaque = opaque
        monkeypatch.setattr(hf_cli, "resolve_cli", lambda project_dir=None, **kw: "cli")
        monkeypatch.setattr(hf_cli, "render", self.render)
        monkeypatch.setattr(hfprov, "probe_render", self.probe)
        monkeypatch.setattr(hfprov, "opaque_everywhere", lambda path, duration: self.opaque)
        monkeypatch.setattr(hfprov, "to_h264", self.to_h264)
        self.fail = fail

    def render(self, project_dir, entry, output, *, fmt, cli=None, variables=None, fps=None, quality=None,
               sdr=False, on_progress=None, should_cancel=None, timeout=None):
        self.calls.append({"entry": entry, "fmt": fmt, "variables": variables, "fps": fps})
        self.sdr = sdr
        path = os.path.join(project_dir, *entry.split("/"))
        if entry != "index.html":
            self.seen_wrappers.append(open(path).read())
        if self.fail:
            raise hf_cli.CliError(self.fail)
        on_progress(0.5, "Streaming frame 1/2")
        with open(output, "wb") as fh:
            fh.write(b"render")
        return output

    def probe(self, path):
        return {"codec_name": "prores" if path.endswith(".mov") else "h264", "width": 1920, "height": 1080,
                "fps": 30, "frames": 78, "duration": 2.6, "has_audio": False}

    def to_h264(self, src, dst, *, has_audio, should_cancel=None):
        self.encodes.append((os.path.basename(src), os.path.basename(dst)))
        with open(dst, "wb") as fh:
            fh.write(b"h264")
        return dst


def _render(provider, lk, tmp_path):
    out = tmp_path / "staging"
    out.mkdir(exist_ok=True)
    progress = []
    result = provider.render(lk, str(out), on_progress=lambda f, m: progress.append((f, m)),
                             should_cancel=lambda: False)
    return result, progress


def test_project_role_renders_index_as_mp4_with_variables(tmp_path, monkeypatch, provider):
    fake = FakeCli(tmp_path, monkeypatch)
    root = make(tmp_path)
    result, progress = _render(provider, link(root, props={"brand": "Ship"}), tmp_path)
    assert fake.calls == [{"entry": "index.html", "fmt": "mp4", "variables": {"brand": "Ship"}, "fps": "30"}]
    assert (result.codec, result.width, result.duration_frames) == ("h264", 1920, 78)
    # no colour conversion and no second encode (SPEC section 5, 2026-10-05); SDR always
    assert result.path.endswith("render.mp4") and fake.encodes == [] and fake.sdr is True
    assert any("HyperFrames:" in m for _f, m in progress) and result.props == {"brand": "Ship"}
    # a prop equal to the HTML's default changes nothing: not sent, and the link stores no override
    result, _p = _render(provider, link(root, props={"brand": "Zenvi"}), tmp_path)
    assert fake.calls[-1]["variables"] is None and result.props == {}


def test_composition_role_renders_a_transparent_wrapper_then_removes_it(tmp_path, monkeypatch, provider):
    fake = FakeCli(tmp_path, monkeypatch)
    root = make(tmp_path)
    result, _p = _render(provider, link(root, role="composition", props={"headline": "Ship"}, host="intro"),
                         tmp_path)
    (call,) = fake.calls
    assert call["fmt"] == "mov" and call["entry"].startswith(".render-zenvi-") and call["variables"] is None
    assert '"headline": "Ship"' in fake.seen_wrappers[0].replace("&quot;", '"')
    assert result.codec == "prores4444" and result.path.endswith(".mov") and result.props == {"headline": "Ship"}
    assert not [n for n in os.listdir(root) if n.startswith(".render-")]


def test_opaque_composition_is_reencoded_to_h264(tmp_path, monkeypatch, provider):
    fake = FakeCli(tmp_path, monkeypatch, opaque=True)
    root = make(tmp_path)
    result, _p = _render(provider, link(root, role="composition", host="intro"), tmp_path)
    assert result.codec == "h264" and result.path.endswith("render.mp4")
    assert fake.encodes == [("render.mov", "render.mp4")]  # same YUV values, no colour conversion
    assert not os.path.exists(os.path.join(os.path.dirname(result.path), "render.mov"))


def _hidden(doc, ident):
    el = doc.by_id(ident)
    return el is not None and wrappers.HIDE_ATTR in el.attrs


def test_inline_composition_and_layer_wrappers(tmp_path, monkeypatch, provider):
    fake = FakeCli(tmp_path, monkeypatch)
    root = make(tmp_path)
    _render(provider, link(root, role="composition", host="caps"), tmp_path)
    _render(provider, link(root, role="layer", exclude=["bg", "intro", "caps"]), tmp_path)
    inline, layer = (hfp.parse_html(w) for w in fake.seen_wrappers)
    assert inline.by_id("caps").attrs["data-start"] == "0" and not _hidden(inline, "caps")
    assert _hidden(inline, "bg") and _hidden(inline, "title") and _hidden(inline, "intro")
    assert inline.by_id("root").attrs.get("data-duration") is not None
    # the layer hides what Zenvi rebuilt natively (scripts still find it by id; the layout stays)
    assert not _hidden(layer, "title") and all(_hidden(layer, i) for i in ("bg", "intro", "caps"))
    assert wrappers.HIDE_CSS in fake.seen_wrappers[1]


def test_inline_composition_inside_a_wrapper_div_keeps_its_ancestors(tmp_path, monkeypatch, provider):
    """Review C5-1 #10: a host inside <div class="stage"> was removed with its wrapper (an empty render)."""
    fake = FakeCli(tmp_path, monkeypatch)
    root = write_project(tmp_path / "p2", root_div(
        '<video id="v1" class="clip" src="assets/a.mp4" data-start="0" data-duration="4" muted></video>'
        '<div class="stage" id="stage" style="background: #123"><p id="side">side</p>'
        '<div id="cap" data-composition-id="captions" data-start="1" data-track-index="2"><h2 id="cap-text">Hello'
        '</h2><script>const ct = gsap.timeline({paused:true}); ct.set({}, {}, 2); window.__timelines["captions"] = ct;'
        '</script></div></div>', extra='data-width="1920" data-height="1080" data-duration="4"'),
        files={"assets/a.mp4": b"v"}, script='const tl = gsap.timeline({paused:true}); window.__timelines["main"] = tl;')
    _render(provider, link(root, role="composition", host="cap", composition="captions"), tmp_path)
    w = hfp.parse_html(fake.seen_wrappers[0])
    assert w.by_id("cap-text") is not None and not _hidden(w, "cap") and not _hidden(w, "stage")
    assert wrappers.CLEAR_ATTR in w.by_id("stage").attrs      # the stage's own background is not part of it
    assert _hidden(w, "v1") and _hidden(w, "side")


def test_renders_resolve_starts_after_media_without_duration(tmp_path, monkeypatch, provider):
    """Review C5-1 #2: the provider read the project without measuring media, so a data-start="<id>" after a
    video without data-duration failed every composition / layer render (the import itself measured it)."""
    from classes.handoff.hyperframes import importer
    fake = FakeCli(tmp_path, monkeypatch)
    monkeypatch.setattr(importer, "ffprobe_media", lambda path: {"width": 1920, "height": 1080, "duration": 4.0,
                                                                  "has_video": True, "has_audio": False})
    root = write_project(tmp_path / "p3", root_div(
        '<video id="v1" class="clip" src="assets/a.mp4" data-start="0" muted></video>'
        '<video id="v2" class="clip" src="assets/a.mp4" data-start="v1" muted></video>'
        '<h1 id="t" class="clip" data-start="v1 - 0.5" data-duration="1">Hi</h1>'),
        files={"assets/a.mp4": b"v"}, script='const tl = gsap.timeline({paused:true}); window.__timelines["main"] = tl;')
    result, _p = _render(provider, link(root, role="layer", exclude=["v1", "v2"]), tmp_path)
    assert result.codec == "prores4444" and fake.calls


def test_props_store_only_zenvi_changes_and_html_edits_come_through(tmp_path, monkeypatch, provider):
    """Review C5-1 #3: stored defaults / mount values hid edits made in HyperFrames."""
    fake = FakeCli(tmp_path, monkeypatch)
    root = make(tmp_path)
    lk = link(root, role="composition", host="intro")      # a fresh import stores no props
    assert provider.editable_props(lk) == {"headline": "Launch", "accent": "#FF5A36"}
    # Edit Props shows those and stores every shown value; the user changed only the accent
    from windows.linked_clip_dialog import merge_edited_props
    shown = provider.editable_props(lk)
    lk["props"] = merge_edited_props(lk["props"], shown, dict(shown, accent="#00FF00"))
    result, _p = _render(provider, lk, tmp_path)
    assert result.props == {"accent": "#00FF00"}           # only the real change is kept
    values = json.loads(re.search(r'data-variable-values="([^"]*)"', fake.seen_wrappers[-1]).group(1)
                        .replace("&quot;", '"'))
    assert values == {"headline": "Launch", "accent": "#00FF00"}
    lk["props"] = result.props
    # the mount's headline and the composition's default accent change in HyperFrames
    index = os.path.join(root, "index.html")
    _rewrite(index, lambda text: text.replace('{"headline":"Launch"}', '{"headline":"Mount B"}'))
    assert provider.editable_props(lk) == {"headline": "Mount B", "accent": "#00FF00"}
    _render(provider, lk, tmp_path)
    values = json.loads(re.search(r'data-variable-values="([^"]*)"', fake.seen_wrappers[-1]).group(1)
                        .replace("&quot;", '"'))
    assert values == {"headline": "Mount B", "accent": "#00FF00"}
    # the root's layer: a changed default in index.html is what renders (no stored copy of it)
    layer = link(root, role="layer", exclude=["bg"])
    _rewrite(index, lambda text: text.replace('"default":"Zenvi"', '"default":"Root edited"'))
    assert provider.editable_props(layer) == {"brand": "Root edited"}
    result, _p = _render(provider, layer, tmp_path)
    assert fake.calls[-1]["variables"] is None and result.props == {}


def test_fingerprint_follows_files_in_symlinked_folders(tmp_path, provider):
    root = make(tmp_path)
    library = tmp_path / "library"
    library.mkdir()
    (library / "card.html").write_text("<div>one</div>")
    (library / "pic.png").write_bytes(b"one")
    os.symlink(str(library), os.path.join(root, "shared"))
    lk = link(root)
    before = provider.fingerprint(lk)
    (library / "card.html").write_text("<div>two</div>")
    middle = provider.fingerprint(lk)
    (library / "pic.png").write_bytes(b"two!")
    assert len({before, middle, provider.fingerprint(lk)}) == 3


def test_missing_host_and_cli_failures_are_link_errors(tmp_path, monkeypatch, provider):
    FakeCli(tmp_path, monkeypatch, fail="the HyperFrames render failed (exit 1): boom")
    root = make(tmp_path)
    with pytest.raises(hfprov.HyperFramesLinkError, match="no longer mounts"):
        _render(provider, link(root, role="composition", host="gone"), tmp_path)
    with pytest.raises(hf_cli.CliError, match="boom"):
        _render(provider, link(root), tmp_path)
    assert not [n for n in os.listdir(root) if n.startswith(".render-")]
    with pytest.raises(hfprov.HyperFramesLinkError, match="unknown HyperFrames link role"):
        _render(provider, link(root, role="mystery"), tmp_path)


def test_layer_reports_vanished_exclusions(tmp_path, monkeypatch, provider):
    FakeCli(tmp_path, monkeypatch)
    root = make(tmp_path)
    result, _p = _render(provider, link(root, role="layer", exclude=["bg", "renamed"]), tmp_path)
    assert any("'renamed'" in w for w in result.warnings)


# --- props and opening ---------------------------------------------------------------------------------------------

def test_editable_props(tmp_path, provider):
    root = make(tmp_path)
    assert provider.editable_props(link(root, props={"extra": 1})) == {"brand": "Zenvi", "extra": 1}
    comp = provider.editable_props(link(root, role="composition", props={"accent": "#000000"}, host="intro"))
    assert comp == {"headline": "Launch", "accent": "#000000"}
    assert provider.editable_props(link(str(tmp_path / "gone"), props={"a": 1})) == {"a": 1}


def test_open_source_and_studio(tmp_path, monkeypatch, provider):
    from classes.handoff import open_source
    root = make(tmp_path)
    opened, studios = [], []
    monkeypatch.setattr(open_source, "open_in_editor", lambda f, line=None, folder=None, setting=None:
                        opened.append((f, line, folder)) or {"editor": "x"})
    monkeypatch.setattr(hf_cli, "open_studio", lambda d: studios.append(d) or "http://127.0.0.1:1/#project/hf")
    provider.open_source(link(root, role="composition", host="intro"))
    assert opened == [(os.path.join(root, "compositions", "intro.html"), 3, root)]
    provider.open_studio(link(root))
    assert studios == [root]


def test_registered_with_linked_media():
    import classes.handoff.hyperframes  # noqa: F401  (registers on import)
    assert lm.provider_for("hyperframes").label == "HyperFrames" and lm.supports_studio("hyperframes")


def test_layer_finds_an_id_less_element_again_after_index_html_moved_it(tmp_path, monkeypatch, provider):
    """Plausible: ``@i/j`` paths of elements without an id shift when index.html is edited."""
    fake = FakeCli(tmp_path, monkeypatch)
    root = write_project(tmp_path / "p4", root_div(
        '<h1 id="t" class="clip" data-start="0" data-duration="2">Hi</h1>'
        '<img class="clip pic" src="assets/logo.png" data-start="0" data-duration="2"/>'),
        files={"assets/logo.png": b"i"}, script='const tl = gsap.timeline({paused:true}); window.__timelines["main"] = tl;')
    p = hfp.load_project(root)
    img = next(e for e in p.root.element.children if e.tag == "img")
    ref, sig = hfprov.element_ref(img, p.root.element), hfprov.element_signature(img)
    assert ref == "@1"
    lk = link(root, role="layer", exclude=[ref], signatures={ref: sig})
    # a new element is added at the top of the root in HyperFrames: the image is now @2
    _rewrite(os.path.join(root, "index.html"), lambda text: text.replace(
        '<h1 id="t"', '<p class="clip" data-start="0" data-duration="1">new</p><h1 id="t"'))
    result, _p = _render(provider, lk, tmp_path)
    w = hfp.parse_html(fake.seen_wrappers[-1])
    hidden = [e for e in w.iter() if wrappers.HIDE_ATTR in e.attrs]
    assert [e.tag for e in hidden] == ["img"] and not result.warnings


COMP = """<html><body><template><div data-composition-id="%s" data-width="1920" data-height="1080">
<b class="x">%s</b><script>const tl = gsap.timeline({paused:true}); tl.set({}, {}, 2);
window.__timelines["%s"] = tl;</script></div></template></body></html>"""


def test_an_id_less_host_is_found_by_what_it_mounts(tmp_path, monkeypatch, provider):
    """verify-C5-1 #6: an element added above id-less hosts moved clip "b"'s @i/j path onto composition "a"."""
    fake = FakeCli(tmp_path, monkeypatch)
    root = write_project(tmp_path / "p5", root_div(
        '<div data-composition-id="a" data-composition-src="compositions/a.html" data-start="0" '
        'data-duration="2"></div>'
        '<div data-composition-id="b" data-composition-src="compositions/b.html" data-start="2" '
        'data-duration="2"></div>', extra='data-width="1920" data-height="1080" data-duration="4"'),
        files={"compositions/a.html": COMP % ("a", "A", "a"), "compositions/b.html": COMP % ("b", "B", "b")},
        script='const tl = gsap.timeline({paused:true}); window.__timelines["main"] = tl;')
    p = hfp.load_project(root)
    refs = [hfprov.element_ref(c.element, p.root.element) for c in p.root.clips if c.kind == "composition"]
    lk = link(root, role="composition", host=refs[1], composition="b")
    lk["source"]["entry"] = lk["source"]["file"] = "compositions/b.html"
    _rewrite(os.path.join(root, "index.html"), lambda text: text.replace(
        '<div data-composition-id="a"', '<p class="clip" data-start="0" data-duration="1">new</p>'
                                        '<div data-composition-id="a"', 1))
    p2 = hfp.load_project(root)
    assert hfprov.element_ref(hfprov._host_clip(p2, lk).element, p2.root.element) != refs[1]
    assert hfprov._host_clip(p2, lk).composition_id == "b"
    _render(provider, lk, tmp_path)
    assert 'data-composition-src="compositions/b.html"' in fake.seen_wrappers[-1]
    assert 'compositions/a.html' not in fake.seen_wrappers[-1]
    # two id-less mounts of the same file, and the recorded path now on another element: refused, never a guess
    _rewrite(os.path.join(root, "index.html"), lambda text: text.replace(
        'data-composition-id="a" data-composition-src="compositions/a.html"',
        'data-composition-id="b" data-composition-src="compositions/b.html"').replace(
        '<p class="clip"', '<p class="clip" data-start="0" data-duration="1">two</p><p class="clip"', 1))
    with pytest.raises(hfprov.HyperFramesLinkError, match="no longer mounts"):
        _render(provider, lk, tmp_path)
