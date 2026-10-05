"""The GSAP timeline reader of the HyperFrames handoff: tokens, literals, placement rules, eases."""

import math

import pytest

from classes.handoff.hyperframes import gsap


def _timeline(src, comp="main"):
    return gsap.build_timeline(gsap.scan_script(src), comp)


# --- tokens and literal values -------------------------------------------------------------------

def test_tokenizer_skips_comments_and_keeps_strings_and_lines():
    toks = gsap.tokenize('// a\nconst a = "x\\"y"; /* b\n c */ let t = `tpl`;\nfoo(.5, 0x10)', first_line=3)
    kinds = [(t.kind, t.value) for t in toks if t.kind in ("str", "num", "template")]
    assert kinds == [("str", 'x"y'), ("template", ("tpl", False)), ("num", 0.5), ("num", 16.0)]
    assert toks[0].line == 4 and toks[-1].line == 6


def test_regex_literals_do_not_swallow_code():
    toks = gsap.tokenize('const r = /a"b/g; tl.to("#x", {x: 1});')
    assert any(t.kind == "regex" for t in toks)
    assert [t.value for t in toks if t.kind == "str"] == ["#x"]


@pytest.mark.parametrize("text, value", [
    ("1.5", 1.5), ("-40", -40.0), ("'#a'", "#a"), ("true", True), ("null", None),
    ("{x: -40, opacity: 0, 'scale': 1.2}", {"x": -40.0, "opacity": 0.0, "scale": 1.2}),
    ("[1, 'a']", [1.0, "a"]), ("0.6 * 2 + (1 - 0.5)", 1.7),
])
def test_literal_values(text, value):
    toks = gsap.tokenize(text)
    got, end = gsap.parse_value(toks, 0)
    assert end == len(toks)
    assert got == pytest.approx(value) if isinstance(value, float) else got == value


@pytest.mark.parametrize("text", ["foo", "i * 0.5", "Math.random()", "() => 1", "{...a}", "`x${y}`", "{[k]: 1}"])
def test_non_literals_are_refs(text):
    got, _end = gsap.parse_value(gsap.tokenize(text), 0)
    assert gsap.has_ref(got)


# --- scripts ---------------------------------------------------------------------------------------

def test_scan_finds_timeline_registration_and_chained_calls():
    info = gsap.scan_script("""
      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true, defaults: { duration: 1, ease: "sine.out" } });
      tl.to("#a", { opacity: 1 }, 0)
        .from("#b", { x: -40 }, "<0.2");
      window.__timelines["main"] = tl;
      tl.seek(0);
    """)
    assert info.timelines["tl"]["defaults"] == {"duration": 1.0, "ease": "sine.out"}
    assert info.registrations == {"main": "tl"}
    assert [c.method for c in info.calls] == ["to", "from", "seek"]


def test_registration_forms():
    for line in ('window.__timelines["x"] = tl;', "window.__timelines.x = tl;", "__timelines['x'] = tl;",
                 'window["__timelines"]["x"] = tl;'):
        info = gsap.scan_script("const tl = gsap.timeline({paused: true});\n" + line)
        assert info.registrations == {"x": "tl"}, line
    info = gsap.scan_script('window.__timelines["y"] = gsap.timeline({paused: true}).to("#a", {x: 5}, 1);')
    assert info.registrations == {"y": "__timelines[y]"} and info.calls[0].method == "to"


def test_global_tweens_are_reported_separately():
    info = gsap.scan_script('gsap.to("#a", {x: 10}); const tl = gsap.timeline(); window.__timelines.m = tl;')
    assert [c.method for c in info.global_tweens] == ["to"] and not info.calls


# --- placement rules ---------------------------------------------------------------------------------

def test_positions_follow_gsap_rules():
    tl = _timeline("""
      const tl = gsap.timeline({ paused: true });
      tl.to("#a", { x: 1, duration: 1 });              // appended: 0..1
      tl.to("#b", { x: 1, duration: 0.5 });            // appended: 1..1.5
      tl.to("#c", { x: 1, duration: 1 }, "-=0.25");    // 1.25..2.25
      tl.to("#d", { x: 1 }, "<");                       // previous start: 1.25
      tl.to("#e", { x: 1 }, ">0.1");                    // previous end + 0.1: 1.75 + 0.1
      tl.addLabel("hit", 3);
      tl.to("#f", { x: 1, delay: 0.5 }, "hit+=1");      // 3 + 1 + 0.5
      tl.set("#g", { x: 0 }, 5);
      window.__timelines["main"] = tl;
    """)
    starts = {t.target: t.start for t in tl.tweens}
    assert starts == pytest.approx({"#a": 0, "#b": 1, "#c": 1.25, "#d": 1.25, "#e": 1.85, "#f": 4.5, "#g": 5})
    assert tl.tweens[-1].duration == 0 and tl.duration == pytest.approx(5.0)
    assert tl.labels == {"hit": 3.0} and tl.exact_duration and not tl.reasons


def test_defaults_duration_and_ease_and_from_semantics():
    tl = _timeline("""
      const tl = gsap.timeline({ paused: true, defaults: { duration: 0.8, ease: "expo.out" } });
      tl.from("#t", { opacity: 0, y: 24 }, 1);
      tl.fromTo("#l", { opacity: 0 }, { opacity: 1, duration: 0.6, ease: "power2.out" }, 3);
      window.__timelines.main = tl;
    """)
    t, f = tl.tweens
    assert (t.method, t.start, t.duration, t.ease) == ("from", 1.0, 0.8, "expo.out")
    assert t.from_vars == {"opacity": 0.0, "y": 24.0} and t.to_vars == {} and t.immediate_render
    assert f.from_vars == {"opacity": 0.0} and f.to_vars == {"opacity": 1.0} and f.ease == "power2.out"
    assert tl.duration == pytest.approx(3.6)


def test_spacer_sets_lengthen_the_timeline():
    tl = _timeline('const tl = gsap.timeline({paused:true}); tl.to("#a",{x:1},0); tl.set({}, {}, 12);'
                   'window.__timelines["main"] = tl;')
    assert tl.duration == 12 and tl.tweens[-1].is_spacer


@pytest.mark.parametrize("call, reason", [
    ('tl.to(el, {x: 1}, 0)', "target is computed"),
    ('tl.to("#a", {x: i * 2}, 0)', "x is computed"),
    ('tl.to("#a", {x: 1, stagger: 0.1}, 0)', "uses stagger"),
    ('tl.to("#a", {x: 1, repeat: 2}, 0)', "uses repeat"),
    ('tl.to("#a", {x: 1}, "missing")', "label 'missing' is not defined"),
    ('tl.to("#a", {x: 1}, "<25%")', "position"),
    ('tl.to("#a", {x: 1, ease: myEase}, 0)', "ease is computed"),
])
def test_what_is_not_understood_says_why(call, reason):
    tl = _timeline("const tl = gsap.timeline({paused:true}); %s; window.__timelines['main'] = tl;" % call)
    assert tl.tweens and not tl.tweens[0].parsed
    assert any(reason in r for r in tl.tweens[0].reasons), tl.tweens[0].reasons


def test_nested_timelines_and_timescale_make_the_duration_inexact():
    tl = _timeline("""const tl = gsap.timeline({paused:true}); const sub = gsap.timeline();
      tl.add(sub, 1); tl.timeScale(2); window.__timelines["main"] = tl;""")
    assert not tl.exact_duration and len(tl.reasons) == 2


def test_unregistered_composition_has_no_timeline():
    tl = _timeline('const a = gsap.timeline(); const b = gsap.timeline(); window.__timelines["x"] = a;', "main")
    assert tl.variable is None and "no timeline is registered" in tl.reasons[0]


def test_prop_values():
    assert gsap.prop_value(40) == gsap.PropValue(40.0)
    assert gsap.prop_value("+=20") == gsap.PropValue(20.0, True)
    assert gsap.prop_value("-=0.5") == gsap.PropValue(-0.5, True)
    assert gsap.prop_value("45deg") == gsap.PropValue(45.0, False, "deg")
    assert gsap.prop_value("50%").unit == "%"
    assert gsap.prop_value("red") is None and gsap.prop_value("*=2") is None and gsap.prop_value(True) is None


# --- eases -------------------------------------------------------------------------------------------

def _bezier_y(x1, y1, x2, y2, p):
    lo, hi = 0.0, 1.0
    for _ in range(80):
        t = (lo + hi) / 2
        x = 3 * (1 - t) ** 2 * t * x1 + 3 * (1 - t) * t * t * x2 + t ** 3
        lo, hi = (t, hi) if x < p else (lo, t)
    t = (lo + hi) / 2
    return 3 * (1 - t) ** 2 * t * y1 + 3 * (1 - t) * t * t * y2 + t ** 3


@pytest.mark.parametrize("name", ["power1.in", "power1.out", "power2.in", "power2.out", "power1.inOut",
                                  "power2.inOut", "quad.out", "cubic.in"])
def test_exact_bezier_pieces_match_the_ease(name):
    spec = gsap.ease_spec(name)
    assert spec is not None and spec.pieces
    for k in range(1, 20):
        p = k / 20
        piece = next(pc for pc in spec.pieces if pc[0] <= p <= pc[1])
        a, b, shape = piece
        local = (p - a) / (b - a)
        ya, yb = spec.fn(a), spec.fn(b)
        y = ya + (yb - ya) * _bezier_y(*shape, local)
        assert y == pytest.approx(spec.fn(p), abs=1e-9), (name, p)


def test_ease_names_and_shapes():
    assert gsap.ease_spec("none").pieces == ((0.0, 1.0, "linear"),)
    assert gsap.ease_spec(None).name == "power1.out"  # GSAP's default
    assert gsap.ease_spec("Power2.easeOut").fn(0.5) == pytest.approx(0.875)
    assert gsap.ease_spec("power2").fn(0.5) == pytest.approx(0.875)  # bare name = .out
    assert gsap.ease_spec("expo.out").fn(1.0) == 1.0 and gsap.ease_spec("expo.in").fn(0.0) == 0.0
    assert gsap.ease_spec("sine.inOut").fn(0.5) == pytest.approx(0.5)
    assert gsap.ease_spec("back.out(1.7)").fn(0.7) > 1.0  # overshoots
    assert gsap.ease_spec("bounce.out").fn(1.0) == pytest.approx(1.0)
    assert gsap.ease_spec("elastic.out(1, 0.3)").fn(1.0) == 1.0
    steps = gsap.ease_spec("steps(4)")  # GSAP: n + 1 levels, the last from 80 %
    assert [steps.fn(p) for p in (0.0, 0.3, 0.79, 0.99, 1.0)] == [0.0, 0.25, 0.75, 1.0, 1.0]
    assert gsap.ease_spec("expo.out").pieces is None
    assert gsap.ease_spec("CustomEase.create('x')") is None and gsap.ease_spec("wiggle.out") is None
    for name in ("circ.inOut", "expo.inOut", "back.inOut", "elastic.inOut", "bounce.inOut"):
        fn = gsap.ease_spec(name).fn
        assert fn(0.0) == pytest.approx(0.0, abs=1e-9) and fn(1.0) == pytest.approx(1.0, abs=1e-9), name
        assert not math.isnan(fn(0.37))


def test_scripts_that_draw_content_are_told_apart_from_pure_animation():
    pure = gsap.scan_script("""window.__timelines = window.__timelines || {};
      function ease(p) { return Math.abs(p); } const tl = gsap.timeline({paused: true});
      gsap.set("#a", {x: 1}); tl.to("#a", {x: 2, ease: "none"}, 0); window.__timelines["m"] = tl;""")
    assert not pure.other_code
    drawing = gsap.scan_script('const v = window.__hyperframes.getVariables();'
                               'document.querySelector(".t").textContent = v.title;')
    assert drawing.other_code
