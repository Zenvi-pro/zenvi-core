"""Reading HyperFrames projects: compositions, timing, tracks, styles, scripts, Zenvi markers.

``tests/fixtures/hyperframes/init`` is what ``npx hyperframes@0.8.126 init`` scaffolds and
``quickstart`` the ``docs/quickstart-template.html`` of ``@hyperframes/core`` 0.8.126 --
HyperFrames' own Apache-2.0 templates, copied unchanged (they load GSAP from its CDN;
GSAP is not in this repository).
"""

import json
import os

import pytest

from classes.handoff.hyperframes import parser as hfp

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "hyperframes")

HEAD = """<!doctype html><html><head><meta charset="UTF-8" />
<script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>{style}</head><body>"""


def write_project(root, index_body, files=None, style="", script="", html_attrs=""):
    os.makedirs(root, exist_ok=True)
    head = HEAD.format(style="<style>%s</style>" % style if style else "")
    if html_attrs:
        head = head.replace("<html>", "<html %s>" % html_attrs)
    text = head + index_body + ("<script>%s</script>" % script if script else "") + "</body></html>"
    with open(os.path.join(root, "index.html"), "w") as fh:
        fh.write(text)
    for rel, content in (files or {}).items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb" if isinstance(content, bytes) else "w") as fh:
            fh.write(content)
    return str(root)


def root_div(inner, comp="main", extra='data-width="1920" data-height="1080"'):
    return '<div id="root" data-composition-id="%s" data-start="0" %s>%s</div>' % (comp, extra, inner)


# --- the real templates ------------------------------------------------------------------------------

def test_init_template():
    p = hfp.load_project(os.path.join(FIXTURES, "init"))
    assert (p.root.id, p.root.width, p.root.height, p.root.duration) == ("main", 1920, 1080, 10.0)
    (title,) = p.root.clips
    assert (title.id, title.kind, title.tag, title.start, title.duration) == ("title", "graphics", "h1", 0.0, 10.0)
    assert p.root.timeline.variable == "tl" and not p.root.timeline.tweens  # the example tween is a comment
    assert not p.warnings and p.zenvi_timeline is None


def test_quickstart_template_meta_composition():
    p = hfp.load_project(os.path.join(FIXTURES, "quickstart"))
    assert p.root.id == "my-video" and p.root.element.tag == "meta"
    clips = {c.id: c for c in p.root.clips}
    assert set(clips) == {"el-video", "el-image", "el-audio", "el-title"}
    v, i, a, t = clips["el-video"], clips["el-image"], clips["el-audio"], clips["el-title"]
    assert (v.kind, v.start, v.duration, v.has_audio, v.track_index) == ("video", 0.0, 10.0, True, 0)
    assert (i.kind, i.start, i.duration) == ("img", 10.0, 5.0)
    assert (a.kind, a.volume, a.track_index) == ("audio", 0.8, 1)
    assert (t.kind, t.start, t.duration) == ("graphics", 1.0, 4.0)
    assert [(tw.method, tw.start) for tw in v.tweens] == [("to", 0.0), ("to", 9.5)]
    assert "missing" in v.problems[0]  # the template's media are placeholders
    assert p.root.duration == 15.0


# --- compositions ------------------------------------------------------------------------------------

def test_nested_compositions_from_files_inline_and_variables(tmp_path):
    root = write_project(tmp_path / "p", root_div(
        '<div id="intro" data-composition-id="intro" data-composition-src="compositions/intro.html" data-start="1" '
        'data-track-index="3" data-variable-values=\'{"headline": "Launch"}\'></div>'
        '<div id="caps" data-composition-id="caps" data-start="intro + 0.5" data-track-index="4">'
        '<div class="w">hi</div><script>const c = gsap.timeline({paused:true}); c.to(".w", {opacity:0}, 1.5);'
        'window.__timelines["caps"] = c;</script></div>'),
        files={"compositions/intro.html": """<html data-composition-variables='[{"id":"headline","type":"string","default":"Hello"}]'>
<body><template id="t"><div data-composition-id="intro" data-width="1920" data-height="1080">
<span class="x">x</span><script>const tl = gsap.timeline({paused:true}); tl.from(".x", {opacity:0, duration:0.6}, 0);
tl.to(".x", {opacity:0, duration:0.4}, 2.2); window.__timelines["intro"] = tl;</script></div></template></body></html>"""},
        script='const tl = gsap.timeline({paused: true}); window.__timelines["main"] = tl;')
    p = hfp.load_project(root)
    intro, caps = p.root.clips
    assert (intro.kind, intro.composition_src, intro.variable_values) == (
        "composition", "compositions/intro.html", {"headline": "Launch"})
    assert (intro.start, intro.duration, intro.duration_source) == (1.0, 2.6, "timeline")
    comp = p.composition_for(intro)
    assert comp.file == "compositions/intro.html" and comp.variable_defaults() == {"headline": "Hello"}
    assert not comp.inline and comp.timeline.variable == "tl"
    assert caps.start == pytest.approx(4.1) and caps.duration == pytest.approx(2.0)  # after intro + 0.5; 1.5 + 0.5 s
    inline = p.composition_for(caps)
    assert inline.inline and inline.timeline.variable == "c"
    assert p.root.timeline.variable == "tl" and not p.root.timeline.tweens  # caps' script is its own


def test_missing_and_cyclic_composition_files(tmp_path):
    root = write_project(tmp_path / "p", root_div(
        '<div id="a" data-composition-id="a" data-composition-src="compositions/a.html" data-start="0"></div>'
        '<div id="gone" data-composition-id="g" data-composition-src="compositions/nope.html" data-start="0"></div>'),
        files={"compositions/a.html": '<template><div data-composition-id="a"><div id="b" data-composition-id="b" '
                                      'data-composition-src="compositions/a.html" data-start="0"></div></div></template>'})
    with pytest.raises(hfp.HyperFramesError, match="include each other"):
        hfp.load_project(root)
    with open(os.path.join(root, "compositions", "a.html"), "w") as fh:
        fh.write('<template><div data-composition-id="a"></div></template>')
    p = hfp.load_project(root)
    gone = p.root.clips[1]
    assert "missing" in gone.problems[0] and any("nope.html" in w for w in p.warnings)


def test_not_a_project(tmp_path):
    with pytest.raises(hfp.HyperFramesError, match="no index.html"):
        hfp.load_project(str(tmp_path))
    write_project(tmp_path / "p", "<div>no composition</div>")
    with pytest.raises(hfp.HyperFramesError, match="no element with data-composition-id"):
        hfp.load_project(str(tmp_path / "p"))
    with pytest.raises(hfp.HyperFramesError, match="not a folder"):
        hfp.load_project(str(tmp_path / "nope"))


# --- timing ------------------------------------------------------------------------------------------

def test_relative_starts_offsets_and_default_durations(tmp_path):
    media = {"assets/a.mp4": b"v", "assets/b.png": b"i", "assets/m.wav": b"a"}
    root = write_project(tmp_path / "p", root_div(
        '<video id="intro" src="assets/a.mp4" data-start="0" data-duration="10" data-track-index="0" muted></video>'
        '<video id="main" src="assets/a.mp4" data-start="intro" data-media-start="2" data-track-index="0"></video>'
        '<img id="gap" src="assets/b.png" data-start="intro + 2" data-track-index="1" />'
        '<img id="lap" src="assets/b.png" data-start="intro - 0.5" data-duration="4" data-layer="2" />'
        '<audio id="m" src="assets/m.wav" data-start="0" data-end="12" data-volume="0.6" data-fade-in="0.25" '
        'data-fade-out="1.5" data-track-index="5"></audio>'), files=media)
    probed = []

    def probe(path):
        probed.append(os.path.basename(path))
        return 20.0

    p = hfp.load_project(root, probe=probe)
    c = {x.id: x for x in p.root.clips}
    assert (c["main"].start, c["main"].duration, c["main"].duration_source) == (10.0, 18.0, "media")
    assert (c["gap"].start, c["gap"].duration, c["gap"].duration_source) == (12.0, 3.0, "default")
    assert (c["lap"].start, c["lap"].track_index) == (9.5, 2)
    m = c["m"]
    assert (m.duration, m.volume, m.fade_in, m.fade_out, m.track_index) == (12.0, 0.6, 0.25, 1.5, 5)
    assert c["intro"].muted and c["main"].media_start == 2.0
    assert probed == ["a.mp4"]  # only the clip without a duration is measured


@pytest.mark.parametrize("starts, message", [
    ({"a": "b", "b": "a"}, "cycle"),
    ({"a": "a"}, "cycle"),
    ({"a": "ghost"}, "not a clip"),
    ({"a": "0", "b": "a + x"}, "not a time or a clip id"),
])
def test_bad_references_are_errors(tmp_path, starts, message):
    body = "".join('<div id="%s" class="clip" data-start="%s" data-duration="1"></div>' % kv for kv in starts.items())
    root = write_project(tmp_path / "p", root_div(body))
    with pytest.raises(hfp.HyperFramesError, match=message):
        hfp.load_project(root)


def test_reference_to_a_clip_of_unknown_length_is_an_error(tmp_path):
    root = write_project(tmp_path / "p", root_div(
        '<video id="v" src="assets/missing.mp4" data-start="0"></video><img id="i" src="x.png" data-start="v"/>'))
    with pytest.raises(hfp.HyperFramesError, match="duration is unknown"):
        hfp.load_project(root)


def test_cli_timeline_wins_for_root_clips(tmp_path):
    root = write_project(tmp_path / "p", root_div(
        '<video id="v" src="assets/a.mp4" data-start="0" data-track-index="0"></video>'
        '<img id="i" src="assets/b.png" data-start="v" data-duration="2" data-track-index="1"/>'),
        files={"assets/a.mp4": b"v", "assets/b.png": b"i"})
    cli = {"timeline": {"duration": 9, "tracks": [
        {"kind": "video", "rows": [{"id": "v", "elementId": "v", "file": "index.html", "absStart": 0, "duration": 7,
                                    "durationSource": "media", "trackIndex": 0, "nested": False, "volume": 0.5}]},
        {"kind": "graphics", "rows": [{"id": "i", "elementId": "i", "file": "index.html", "absStart": 7,
                                       "duration": 2, "durationSource": "authored", "trackIndex": 3,
                                       "nested": False},
                                      {"id": "x", "file": "compositions/x.html", "absStart": 1, "nested": True}]},
    ]}}
    p = hfp.load_project(root, probe=lambda _p: 5.0, cli_timeline=cli)
    v, i = p.root.clips
    assert p.cli_timeline_used
    assert (v.duration, v.duration_source, v.volume) == (7.0, "cli", 0.5)
    # i follows v, whose length is its media's: Zenvi resolves that start (with the CLI's 7 s length)
    assert (i.start, i.track_index) == (7.0, 3)


def _row(ident, start, duration, track, source="authored", element=True):
    row = {"id": ident, "file": "index.html", "absStart": start, "duration": duration, "durationSource": source,
           "trackIndex": track, "nested": False}
    row["elementId"] = ident if element else None
    return row


def test_cli_starts_after_media_without_duration_are_not_trusted(tmp_path):
    """`hyperframes timeline --json` resolves data-start="<id>" with the START of a clip that has no
    data-duration (0.8.126; review C5-1 #1): those starts stay Zenvi's (media length), others follow the CLI."""
    root = write_project(tmp_path / "p", root_div(
        '<video id="v1" src="assets/a.mp4" data-start="0" data-track-index="0" muted></video>'
        '<video id="v2" src="assets/a.mp4" data-start="v1" data-media-start="1" data-playback-rate="2" '
        'data-track-index="0" muted></video>'
        '<img id="pic" src="assets/b.png" data-start="v1 - 0.5" data-duration="1" data-track-index="1"/>'
        '<img id="after" src="assets/b.png" data-start="pic + 0.25" data-duration="1" data-track-index="2"/>'
        '<img id="cap" src="assets/b.png" data-start="card" data-duration="1" data-track-index="3"/>'
        '<img id="card" src="assets/b.png" data-start="0.5" data-duration="2" data-track-index="4"/>'
        '<img id="img" src="assets/b.png" data-start="1" data-duration="1" data-track-index="5"/>'),
        files={"assets/a.mp4": b"v", "assets/b.png": b"i"})
    cli = {"timeline": {"duration": 5, "tracks": [{"rows": [
        _row("v1", 0, 4, 0, "media"), _row("v2", 0, 1.5, 0, "media"),          # v2 wrongly at v1's start
        _row("pic", 0, 1, 1), _row("after", 1.25, 1, 2),                       # pic "v1 - 0.5" -> 0 (wrong)
        _row("cap", 2.5, 1, 3), _row("card", 0.5, 2, 4),                       # "card": authored -> trusted
        _row("img", 9, 1, 6, element=False),                                   # a row for an element w/o id
    ]}]}}
    p = hfp.load_project(root, probe=lambda _p: 4.0, cli_timeline=cli)
    clips = {c.id: c for c in p.root.clips}
    assert clips["v1"].duration == 4.0
    assert (clips["v2"].start, clips["v2"].duration) == (4.0, 1.5)     # after v1's 4 s of media
    assert clips["pic"].start == 3.5 and clips["after"].start == 4.75  # chains follow the corrected start
    assert clips["cap"].start == 2.5                                   # reference to an authored duration
    assert (clips["img"].start, clips["img"].track_index) == (1.0, 5)  # not the id-less row's values
    assert hfp.cli_start_trusted(clips["cap"], clips) and not hfp.cli_start_trusted(clips["after"], clips)


def test_playback_start_wins_over_media_start_and_loop_is_read(tmp_path):
    root = write_project(tmp_path / "p", root_div(
        '<video id="a" src="assets/a.mp4" data-start="0" data-playback-start="2" data-media-start="1" loop muted>'
        '</video><audio id="b" src="assets/a.mp4" data-start="0" data-media-start="1.5"></audio>'),
        files={"assets/a.mp4": b"v"})
    p = hfp.load_project(root, probe=lambda _p: 10.0)
    a, b = p.root.clips
    assert (a.media_start, a.duration, a.loop) == (2.0, 8.0, True)
    assert (b.media_start, b.duration, b.loop) == (1.5, 8.5, False)


# --- styles, attributes, scripts ---------------------------------------------------------------------

def test_css_cascade_and_selectors(tmp_path):
    root = write_project(tmp_path / "p", root_div(
        '<div class="wrap"><img id="logo" class="clip big" src="x.png" data-start="0" data-duration="1" '
        'style="top: 50px" data-zenvi-clip-id="ABC" data-zenvi-note="n"/></div>'),
        style=""".clip { position: absolute; left: 0; top: 0; width: 100%; }
               @media (min-width: 10px) { .wrap > img.big { width: 400px; } }
               #logo { left: 100px; } img { height: 200px !important; }
               @keyframes spin { from { opacity: 0 } }
               a:hover { color: red }""")
    p = hfp.load_project(root)
    logo = p.root.clips[0]
    assert logo.style["left"] == "100px" and logo.style["top"] == "50px" and logo.style["width"] == "400px"
    assert logo.style["height"] == "200px" and logo.style["position"] == "absolute"
    assert logo.zenvi == {"clip-id": "ABC", "note": "n"}
    el = logo.element
    assert hfp.selector_matches("div.wrap > img#logo", el) and hfp.selector_matches("[data-zenvi-clip-id='ABC']", el)
    assert hfp.selector_matches("#root img", el) and not hfp.selector_matches("#root > img", el)
    assert hfp.selector_matches("img:first-child", el) is None  # unsupported syntax: cannot tell


def test_tweens_attach_by_selector_and_ancestors_are_problems(tmp_path):
    root = write_project(tmp_path / "p", root_div(
        '<video id="a" class="clip" src="a.mp4" data-start="0" data-duration="2"></video>'
        '<div id="pip"><video id="b" src="a.mp4" data-start="0" data-duration="2"></video></div>'
        '<h1 id="t" class="clip" data-start="0" data-duration="2">Hi</h1>'),
        files={"a.mp4": b"v"},
        script="""const tl = gsap.timeline({paused:true});
          tl.to("#a", {opacity: 0}, 1); tl.to(".clip", {x: 10}, 0); tl.to("#pip", {scale: 0.5}, 0);
          gsap.to("#b", {rotation: 5}); window.__timelines["main"] = tl;""")
    p = hfp.load_project(root)
    a, b, t = p.root.clips
    assert [tw.target for tw in a.tweens] == ["#a", ".clip"]
    assert [tw.target for tw in t.tweens] == [".clip"]
    assert any("wrapper <div#pip> is animated" in pr for pr in b.problems)
    assert any("global gsap.to()" in pr for pr in b.problems)


def test_embedded_zenvi_timeline_and_damaged_json(tmp_path):
    good = {"zenvi_timeline": 1, "zenvi": {"project": {"clips": []}}}
    body = root_div("") + '<script type="application/json" id="zenvi-timeline">%s</script>' % json.dumps(good)
    p = hfp.load_project(write_project(tmp_path / "a", body))
    assert p.zenvi_timeline == good
    p = hfp.load_project(write_project(tmp_path / "b", root_div("") +
                                       '<script type="application/json" id="zenvi-timeline">{oops</script>'))
    assert p.zenvi_timeline is None and "damaged" in p.warnings[0]


def test_studio_reserialized_html_reads_the_same(tmp_path):
    """HyperFrames Studio stamps data-hf-id and re-escapes attributes; the reading must not change."""
    plain = root_div('<div id="i" data-composition-id="i" data-composition-src="c.html" data-start="1" '
                     'data-variable-values=\'{"t":"A"}\'></div>')
    stamped = plain.replace('<div id="i"', '<div data-hf-id="hf-1" id="i"').replace(
        "'{\"t\":\"A\"}'", '"{&quot;t&quot;:&quot;A&quot;}"')
    a = hfp.load_project(write_project(tmp_path / "a", plain, files={"c.html": '<div data-composition-id="i"></div>'}))
    b = hfp.load_project(write_project(tmp_path / "b", stamped, files={"c.html": '<div data-composition-id="i"></div>'}))
    assert a.root.clips[0].variable_values == b.root.clips[0].variable_values == {"t": "A"}


def test_resolve_src(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "assets"))
    open(os.path.join(root, "assets", "a b.png"), "w").close()
    assert hfp.resolve_src("assets/a%20b.png?v=2", root) == (os.path.join(root, "assets", "a b.png"), False)
    assert hfp.resolve_src("/assets/a b.png", root)[0] == os.path.join(root, "assets", "a b.png")
    assert hfp.resolve_src("../assets/a b.png", root, "compositions/x.html")[0] == os.path.join(root, "assets",
                                                                                                "a b.png")
    assert hfp.resolve_src("https://x.test/a.mp4", root) == (None, True)
    assert hfp.resolve_src("missing.mp4", root) == (None, False)


def test_remaining_visuals(tmp_path):
    root = write_project(tmp_path / "p", root_div(
        '<div id="bg" style="background: #123"></div><div id="w"><video id="v" src="a.mp4" data-start="0" '
        'data-duration="1"></video></div><h1 id="t" class="clip" data-start="0" data-duration="1">Hi</h1>'),
        files={"a.mp4": b"v"})
    p = hfp.load_project(root)
    v = p.root.clip("v")
    left = hfp.remaining_visuals(p.root, {id(v.element)})
    assert left == ["<div#bg>", "<h1#t>"]
    assert hfp.remaining_visuals_in(p.root, p.root.element.children[1], {id(v.element)}) == []


def test_parse_fps():
    assert hfp.parse_fps("30") == 30 and hfp.parse_fps("30000/1001").denominator == 1001
    assert hfp.parse_fps("") is None and hfp.parse_fps("abc") is None and hfp.parse_fps("0") is None


@pytest.mark.parametrize("text, value", [
    ("4", 4.0), (" 4.5 ", 4.5), ("", 0.0), (".5", 0.5), ("5.", 5.0), ("1e1", 10.0), ("0x10", 16.0),
    ("-2", -2.0), ("4s", None), ("1_000", None), ("Infinity", None), ("nan", None), (None, None),
])
def test_numbers_are_read_like_javascript_number(text, value):
    assert hfp.js_number(text) == value


def test_durations_hyperframes_ignores_are_not_authored(tmp_path):
    """verify-C5-1 #5: data-duration="4s" (or 0) is no duration for HyperFrames (Number(v) > 0 only): the runtime
    plays such a video for its media's length, and the CLI's start after it must not be trusted."""
    root = write_project(tmp_path / "p", root_div(
        '<video id="v1" src="assets/a.mp4" data-start="0" data-duration="4s" muted></video>'
        '<img id="pic" src="assets/b.png" data-start="v1" data-duration="1"/>'
        '<video id="v2" src="assets/a.mp4" data-start="0" data-duration="0" data-track-index="1" muted></video>'
        '<img id="after2" src="assets/b.png" data-start="v2" data-duration="1" data-track-index="1"/>'
        '<img id="ends" src="assets/b.png" data-start="0" data-end="2" data-track-index="2"/>'
        '<img id="next" src="assets/b.png" data-start="ends" data-duration="1" data-track-index="2"/>'),
        files={"assets/a.mp4": b"v", "assets/b.png": b"i"})
    cli = {"timeline": {"tracks": [{"rows": [
        _row("v1", 0, 10, 0, "media"), _row("pic", 0, 1, 0), _row("v2", 0, 10, 1, "media"),
        _row("after2", 0, 1, 1), _row("ends", 0, 2, 2), _row("next", 2.5, 1, 2)]}]}}
    p = hfp.load_project(root, probe=lambda _p: 10.0, cli_timeline=cli)
    c = {x.id: x for x in p.root.clips}
    assert (c["v1"].duration, c["pic"].start) == (10.0, 10.0) and not hfp.cli_start_trusted(c["pic"], c)
    assert (c["v2"].duration, c["after2"].start) == (10.0, 10.0)
    assert hfp.cli_start_trusted(c["next"], c) and c["next"].start == 2.5     # data-end after its start counts


def test_a_composition_id_names_its_host_in_data_start(tmp_path):
    """HyperFrames accepts data-start="<data-composition-id>"; such projects no longer fail to import."""
    root = write_project(tmp_path / "p", root_div(
        '<div data-composition-id="intro" data-start="0" data-duration="2"><b>x</b></div>'
        '<img id="pic" src="assets/b.png" data-start="intro + 0.5" data-duration="1"/>'),
        files={"assets/b.png": b"i"})
    p = hfp.load_project(root)
    assert p.root.clip("pic").start == 2.5
