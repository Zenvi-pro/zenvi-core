"""effects-color editor tools: Look presets, color grades, LUTs, audio presets, chroma key."""

import os

import pytest

from classes import effect_ops, info, look_presets
from classes.editor_tools import effects_color_analysis as analysis
from effects_color_helpers import effect_of, install_effect_fixtures, receipt, y


@pytest.fixture
def fx(editor, monkeypatch):
    return install_effect_fixtures(editor, monkeypatch)


def kf_value(effect, key):
    return effect[key]["Points"][0]["co"]["Y"]


# ---------------------------------------------------------------------------
# apply_look_preset_tool
# ---------------------------------------------------------------------------

def test_look_presets_tag_replace_and_reset(fx):
    c = fx.add_clip(fx.add_file("video"))
    receipt(fx.call("apply_look_preset_tool", timeline_clip_id=c, look="blur_soft_focus"))
    blur = effect_of(fx, c, "Blur")[0]
    assert blur["ui-menu"] == "look" and kf_value(blur, "horizontal_radius") == 3.0
    assert fx.undo_steps_since_mark() == 1
    receipt(fx.call("apply_look_preset_tool", timeline_clip_id=c, look="blur_heavy"))
    blurs = effect_of(fx, c, "Blur")
    assert len(blurs) == 1 and blurs[0]["id"] == blur["id"] and kf_value(blurs[0], "horizontal_radius") == 20.0
    receipt(fx.call("apply_look_preset_tool", timeline_clip_id=c, look="film_grain_super_8"))
    receipt(fx.call("apply_look_preset_tool", timeline_clip_id=c, look="color_warm_up"))
    grade = effect_of(fx, c, "ColorGrade")[0]
    assert kf_value(grade, "temperature") == pytest.approx(0.18)
    assert kf_value(effect_of(fx, c, "FilmGrain")[0], "amount") == pytest.approx(0.62)
    fx.mark()
    receipt(fx.call("apply_look_preset_tool", timeline_clip_id=c, look="reset_look"))
    assert fx.clip(c)["effects"] == [] and fx.undo_steps_since_mark() == 1
    fx.undo()
    assert {e["class_name"] for e in fx.clip(c)["effects"]} == {"Blur", "FilmGrain", "ColorGrade"}


def test_look_preset_keeps_user_effects_and_no_op_removal(fx):
    c = fx.add_clip(fx.add_file("video"))
    user_blur = fx.add_effect(c, "Blur")
    receipt(fx.call("apply_look_preset_tool", timeline_clip_id=c, look="blur_medium"))
    assert len(effect_of(fx, c, "Blur")) == 2
    fx.call("apply_look_preset_tool", timeline_clip_id=c, look="remove_blur")
    assert [e["id"] for e in effect_of(fx, c, "Blur")] == [user_blur]
    fx.mark()
    r = receipt(fx.call("apply_look_preset_tool", timeline_clip_id=c, look="remove_glow"))
    assert r["changed"] is False and fx.undo_steps_since_mark() == 0


def test_look_preset_refuses_audio_clip_and_unknown(fx):
    a = fx.add_clip(fx.add_file("audio"))
    assert "audio-only" in fx.call("apply_look_preset_tool", timeline_clip_id=a, look="glow_neon")
    assert fx.call("apply_look_preset_tool", timeline_clip_id=a, look="sparkle").startswith("Error")
    assert fx.undo_steps_since_mark() == 0


def test_look_presets_module_matches_menu_rules():
    made = []

    def create(cn):
        made.append(cn)
        return {"id": "NEW", "class_name": cn, "sigma": {"Points": [{"co": {"X": 1, "Y": 0}}]},
                "horizontal_radius": {"Points": []}, "vertical_radius": {"Points": []},
                "iterations": {"Points": []}}

    effects = [{"id": "L1", "class_name": "Blur", "ui-menu": "look", "order": 3}]
    out = look_presets.apply_look_effect_preset(effects, "Blur", "medium", create)
    assert out[0]["id"] == "L1" and out[0]["order"] == 3 and out[0]["ui-menu"] == "look"
    assert out[0]["sigma"]["Points"][0]["co"]["Y"] == 4.0
    assert look_presets.apply_look_effect_preset([], "Blur", "none", create) is None
    assert look_presets.reset_look([{"class_name": "Hue"}]) is None


# ---------------------------------------------------------------------------
# color_grade_clip_tool / list_luts_tool
# ---------------------------------------------------------------------------

def test_cinematic_look_uses_lut_and_one_color_grade(fx):
    c = fx.add_clip(fx.add_file("video"))
    fx.add_effect(c, "ColorGrade")
    fx.add_effect(c, "ColorGrade")
    fx.mark()
    r = receipt(fx.call("color_grade_clip_tool", timeline_clip_id=c, look="cinematic"))
    grades = effect_of(fx, c, "ColorGrade")
    assert len(grades) == 1 and fx.undo_steps_since_mark() == 1
    g = grades[0]
    assert g["lut_path"].endswith(os.path.join("cinematic_&_blockbuster", "teal_&_orange_cinema.cube"))
    assert os.path.isfile(g["lut_path"])
    assert kf_value(g, "lut_intensity") == 0.5 and kf_value(g, "saturation") == 0.95
    assert kf_value(g, "contrast") == pytest.approx(0.18)
    assert r["clips"][0]["grade"]["lut"] == "cinematic_&_blockbuster/teal_&_orange_cinema"


def test_explicit_controls_keep_the_rest_of_the_grade(fx):
    c = fx.add_clip(fx.add_file("video"))
    fx.call("color_grade_clip_tool", timeline_clip_id=c, look="black_and_white")
    g0 = effect_of(fx, c, "ColorGrade")[0]
    assert kf_value(g0, "saturation") == 0.0
    fx.mark()
    fx.call("color_grade_clip_tool", timeline_clip_id=c, exposure=0.4, lut="warm correction", lut_intensity=0.5)
    g = effect_of(fx, c, "ColorGrade")[0]
    assert g["id"] == g0["id"] and kf_value(g, "saturation") == 0.0 and kf_value(g, "exposure") == 0.4
    assert g["lut_path"].endswith("warm_correction.cube") and kf_value(g, "lut_intensity") == 0.5
    fx.call("color_grade_clip_tool", timeline_clip_id=c, reset=True)
    g = effect_of(fx, c, "ColorGrade")[0]
    assert kf_value(g, "saturation") == 1.0 and g["lut_path"] == ""
    assert fx.undo_steps_since_mark() == 2
    fx.undo()
    fx.undo()
    assert kf_value(effect_of(fx, c, "ColorGrade")[0], "exposure") == 0.0


def test_grade_wheels_curves_and_refusals(fx):
    c = fx.add_clip(fx.add_file("video"))
    fx.call("color_grade_clip_tool", timeline_clip_id=c,
            wheels={"shadows": {"color": "#2a6cff", "amount": 0.15}},
            curves={"all": [[0, 0], [0.25, 0.2], [0.75, 0.82], [1, 1]]})
    g = effect_of(fx, c, "ColorGrade")[0]
    assert g["wheels"]["shadows"]["color"] == "#2a6cff" and g["wheels"]["shadows"]["amount"] == 0.15
    assert len(g["curve_all"]["nodes"]) == 4
    fx.mark()
    for kwargs, needle in (({"lut": "no such lut"}, "no LUT named"), ({}, "say what to change"),
                           ({"curves": {"purple": [[0, 0], [1, 1]]}}, "unknown curve"),
                           ({"wheels": {"shadows": {"amount": 3}}}, "0-1"), ({"exposure": 5}, "<= 2"),
                           ({"look": "sepia"}, "must be one of")):
        out = fx.call("color_grade_clip_tool", timeline_clip_id=c, **kwargs)
        assert out.startswith("Error") and needle in out, (kwargs, out)
    a = fx.add_clip(fx.add_file("audio"), layer=2000000)
    assert fx.call("color_grade_clip_tool", timeline_clip_id=a, look="warm").startswith("Error")
    assert fx.undo_steps_since_mark() == 0


def test_list_luts_and_resolve():
    luts = [lut for lut in effect_ops.list_luts() if lut["builtin"]]
    assert len(luts) == 50 and len({lut["category"] for lut in luts}) == 6
    teal = effect_ops.resolve_lut("Signature Teal & Orange")
    assert teal == os.path.join(info.COLORS_PATH, "teal_&_orange_vibes", "signature_teal_&_orange.cube")
    assert effect_ops.resolve_lut("none") == ""
    with pytest.raises(ValueError):
        effect_ops.resolve_lut("/tmp/not-a-lut.txt")


def test_list_luts_tool_filters(fx):
    r = receipt(fx.call("list_luts_tool", query="teal"))
    assert r["luts"] and all("teal" in (lut["id"] + lut["category"]).lower() for lut in r["luts"])


# ---------------------------------------------------------------------------
# apply_audio_effect_tool
# ---------------------------------------------------------------------------

def test_audio_presets(fx):
    c = fx.add_clip(fx.add_file("video", has_audio=True))
    r = receipt(fx.call("apply_audio_effect_tool", timeline_clip_id=c, preset="voice_compressor"))
    comp = effect_of(fx, c, "Compressor")[0]
    assert kf_value(comp, "ratio") == 3.0 and kf_value(comp, "threshold") == -18.0 and r["changed"]
    fx.call("apply_audio_effect_tool", timeline_clip_id=c, preset="strong_compressor", properties={"ratio": 10})
    comps = effect_of(fx, c, "Compressor")
    assert len(comps) == 1 and kf_value(comps[0], "ratio") == 10.0
    fx.call("apply_audio_effect_tool", timeline_clip_id=c, preset="telephone")
    eq = effect_of(fx, c, "ParametricEQ")[0]
    assert eq["filter_type"] == 4 and kf_value(eq, "frequency") == 1500
    img = fx.add_clip(fx.add_file("image"), layer=2000000)
    fx.mark()
    out = fx.call("apply_audio_effect_tool", timeline_clip_id=img, preset="echo")
    assert out.startswith("Error") and "no sound" in out and fx.undo_steps_since_mark() == 0


# ---------------------------------------------------------------------------
# chroma_key_clip_tool
# ---------------------------------------------------------------------------

def test_chroma_key_moves_keyed_clip_above_background_in_one_step(fx):
    fg = fx.add_clip(fx.add_file("video"), position=40.0, layer=1000000)
    bg = fx.add_clip(fx.add_file("video"), position=0.0, layer=1000000, end=10.0)
    blocker = fx.add_clip(fx.add_file("video"), position=0.0, layer=2000000, end=30.0)
    fx.mark()
    r = receipt(fx.call("chroma_key_clip_tool", timeline_clip_id=fg, key_color="green", background_clip_id=bg))
    ck = effect_of(fx, fg, "ChromaKey")[0]
    assert y(ck["color"]["green"]) == [177.0] and ck["keymethod"] == 11 and r["fuzz"] == 20.0
    assert fx.clip(fg)["position"] == 0.0 and fx.clip(fg)["layer"] == 3000000
    assert r["track"] == 3 and not r["new_track"] and blocker
    assert fx.undo_steps_since_mark() == 1
    fx.undo()
    assert fx.clip(fg)["layer"] == 1000000 and fx.clip(fg)["position"] == 40.0 and fx.clip(fg)["effects"] == []


def test_chroma_key_creates_a_top_track_when_needed_and_auto_color(fx, monkeypatch):
    for n in (2000000, 3000000, 4000000, 5000000):
        fx.lock_track(n)
    fg = fx.add_clip(fx.add_file("video"), layer=1000000)
    bg = fx.add_clip(fx.add_file("video"), layer=1000000, position=30.0)
    monkeypatch.setattr(analysis, "sample_screen_color", lambda clip, t=None: ("#10c040", "green screen at 1s", 48.0))
    r = receipt(fx.call("chroma_key_clip_tool", timeline_clip_id=fg, background_clip_id=bg, method="hsv_hue",
                        fuzz=35))
    assert r["new_track"] and fx.clip(fg)["layer"] == 6000000 and r["key_color"] == "#10c040"
    assert 6000000 in [t["number"] for t in fx.get("layers")] and fx.clip(fg)["position"] == 30.0
    assert effect_of(fx, fg, "ChromaKey")[0]["keymethod"] == 1 and r["fuzz"] == 35
    r2 = receipt(fx.call("chroma_key_clip_tool", timeline_clip_id=fg))
    assert r2["fuzz"] == 48.0 and y(effect_of(fx, fg, "ChromaKey")[0]["fuzz"]) == [48.0]
    fx.undo()
    fx.undo()
    assert 6000000 not in [t["number"] for t in fx.get("layers")]
    fx.mark()
    assert fx.call("chroma_key_clip_tool", timeline_clip_id=fg, background_clip_id=fg).startswith("Error")
    assert fx.undo_steps_since_mark() == 0


def test_screen_color_detection_from_edge_histograms():
    def hist(value):
        h = [0] * 256
        h[value] = 100
        return h
    green = {"red": hist(20), "green": hist(180), "blue": hist(60)}
    assert analysis.screen_color_from_histograms([green, green]) == ("#14b43c", "green")
    grey = {"red": hist(120), "green": hist(125), "blue": hist(118)}
    assert analysis.screen_color_from_histograms([grey])[1] is None
    even, patchy = {"red": hist(20), "green": hist(180), "blue": hist(60)}, {
        "red": hist(20), "green": [0] * 120 + [50] * 100 + [0] * 36, "blue": hist(60)}
    assert analysis.screen_fuzz_from_histograms([even]) == 20.0
    assert 70 <= analysis.screen_fuzz_from_histograms([patchy]) <= 80


# --- a named key colour is this footage's shade, and the receipt says what the key removed ----------
# Found live: "remove the green screen" keyed the stock #00b140 on a lighter backdrop (studio walls at
# the frame edges), removed almost nothing, and the assistant reported success.

def _hist(value):
    h = [0] * 256
    h[value] = 100
    return h


def _cell(r, g, b):
    return {"red": _hist(r), "green": _hist(g), "blue": _hist(b)}


def test_a_screen_that_misses_the_frame_edges_is_found_in_the_frame_grid():
    walls = [_cell(200, 200, 205)] * 4
    grid = [_cell(46, 182, 58)] * 14 + [_cell(30, 60, 200)] * 4 + [_cell(200, 200, 205)] * 6
    color, where, fuzz = analysis.screen_from_scopes(walls, grid)
    assert color == "#2eb63a" and "green screen in 14 of 24 frame areas" in where and fuzz == 20.0
    assert analysis.screen_from_scopes(walls, grid, kind="blue")[0] == "#1e3cc8"
    assert analysis.screen_from_scopes(walls, [_cell(200, 200, 205)] * 24) is None
    edges = [_cell(20, 180, 60)] * 4
    assert analysis.screen_from_scopes(edges, grid)[1] == "green screen at the frame edges"


def test_named_green_keys_the_sampled_shade_and_reports_what_it_removed(fx, monkeypatch):
    fg = fx.add_clip(fx.add_file("video"))
    asked = {}

    def sample(clip, t=None, kind=None):
        asked["kind"] = kind
        return "#2eb63a", "green screen in 14 of 24 frame areas at 2.00s", 24.0

    monkeypatch.setattr(analysis, "sample_screen_color", sample)
    monkeypatch.setattr(analysis, "keyed_share", lambda data, t=None: 58.4)
    out = fx.call("chroma_key_clip_tool", timeline_clip_id=fg, key_color="green")
    r = receipt(out)
    assert asked["kind"] == "green" and r["key_color"] == "#2eb63a" and r["fuzz"] == 24.0
    assert r["key_color_source"].startswith("sampled") and r["keyed_pct"] == 58.4
    assert "58.4% of the frame is now transparent" in out and "too little" not in out


def test_a_key_that_removes_almost_nothing_says_so(fx, monkeypatch):
    fg = fx.add_clip(fx.add_file("video"))

    def no_screen(clip, t=None, kind=None):
        raise analysis.ToolError("found no green screen in clip X at 2.00s (its edges are #c8c8cd)")

    monkeypatch.setattr(analysis, "sample_screen_color", no_screen)
    monkeypatch.setattr(analysis, "keyed_share", lambda data, t=None: 1.5)
    out = fx.call("chroma_key_clip_tool", timeline_clip_id=fg, key_color="green")
    r = receipt(out)
    assert r["key_color"] == "#00b140" and r["key_color_source"].startswith("stock green")
    assert "too little for a green/blue screen" in out and "key_color='auto'" in out
    assert not out.startswith("Error")  # applied: the user asked for it; the receipt warns
