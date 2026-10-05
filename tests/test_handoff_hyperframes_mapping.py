"""HyperFrames primitives as Zenvi clips: CSS placement checked against libopenshot's geometry, GSAP tweens
as keyframes checked against the eases frame by frame, audio gain."""

import os

import pytest

from classes.handoff import transform as tf
from classes.handoff.hyperframes import gsap
from classes.handoff.hyperframes import mapping
from classes.handoff.hyperframes import parser as hfp
from classes.handoff.keyframes import CONSTANT, Curve
from test_handoff_hyperframes_parser import root_div, write_project

FPS = 30.0
VIDEO = {"width": 1280, "height": 720, "duration": 20.0}
LOGO = {"width": 400, "height": 200, "duration": 0.0}


def load(tmp_path, body, style="", script="", files=None, comp_extra='data-width="1920" data-height="1080"'):
    files = dict(files or {})
    files.setdefault("a.mp4", b"v")
    files.setdefault("logo.png", b"i")
    root = write_project(tmp_path / "p", root_div(body, extra=comp_extra), files=files, style=style,
                         script=("const tl = gsap.timeline({paused:true});" + script +
                                 'window.__timelines["main"] = tl;'))
    return hfp.load_project(root)


def plan_of(project, ident, media, canvas=(1920, 1080)):
    clip = project.root.clip(ident)
    return mapping.native_plan(clip, project.root, media, fps=FPS, canvas=canvas)


def curve(plan, key):
    default = tf.TRANSFORM_DEFAULTS.get(key, 0.0)
    kf = plan.props.get(key)
    if kf is None:
        return Curve.constant(default, fps=FPS, position=plan.position, start=plan.start)
    return Curve.from_json(kf, fps=FPS, position=plan.position, start=plan.start, default=default)


def drawn_rect(plan, media, t, canvas=(1920, 1080)):
    """Where libopenshot 1.0 draws the clip at timeline second t: (x, y, w, h, rotation, alpha)."""
    values = {k: curve(plan, k).value_at(t) for k in ("scale_x", "scale_y", "location_x", "location_y", "rotation",
                                                     "alpha")}
    g = tf.geometry(media["width"], media["height"], canvas[0], canvas[1], scale_mode=plan.props.get("scale", 1),
                    gravity=plan.props.get("gravity", 4), **values)
    return g.x, g.y, g.width, g.height, g.rotation, g.opacity


# --- placement ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("fit, mode", [("cover", tf.SCALE_CROP), ("contain", tf.SCALE_FIT), ("fill", tf.SCALE_STRETCH),
                                       (None, tf.SCALE_FIT)])
def test_full_frame_media_is_a_plain_scale_mode(tmp_path, fit, mode):
    style = ".clip { position: absolute; inset: 0; width: 100%%; height: 100%%; %s }" % (
        "object-fit: %s;" % fit if fit else "")
    p = load(tmp_path, '<video id="v" class="clip" src="a.mp4" data-start="0" data-duration="4" muted></video>',
             style=style)
    plan = plan_of(p, "v", VIDEO)
    assert plan.props["scale"] == mode and plan.props["gravity"] == tf.GRAVITY_CENTER
    assert not any(k in plan.props for k in ("scale_x", "location_x", "alpha"))
    assert not plan.problems


@pytest.mark.parametrize("style, rect", [
    ("left: 100px; top: 50px; width: 400px; height: 200px", (100, 50, 400, 200)),
    ("right: 10%; bottom: 0; width: 25%; height: auto", (1920 - 192 - 480, 1080 - 240, 480, 240)),
    ("left: 50vw; top: 10vh; height: 100px", (960, 108, 200, 100)),
    ("inset: 0; width: 960px; height: 540px; object-fit: contain", (0, 30, 960, 480)),
])
def test_boxed_images_land_on_their_css_rectangle(tmp_path, style, rect):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="0" data-duration="2" style="position: absolute; %s"/>'
             % style)
    plan = plan_of(p, "i", LOGO)
    x, y, w, h, _r, _a = drawn_rect(plan, LOGO, 0.0)
    assert (x, y, w, h) == pytest.approx(rect, abs=0.01)
    assert plan.props["scale"] == tf.SCALE_FIT and not plan.problems


def test_positioned_wrappers_offset_their_children(tmp_path):
    p = load(tmp_path, '<div style="position: absolute; left: 200px; top: 100px; width: 800px; height: 600px">'
                       '<img id="i" src="logo.png" data-start="0" data-duration="2" '
                       'style="position: absolute; left: 10%; top: 0; width: 50%"/></div>')
    plan = plan_of(p, "i", LOGO)
    assert drawn_rect(plan, LOGO, 0.0)[:4] == pytest.approx((280, 100, 400, 200), abs=0.01)


def test_static_css_transform_and_opacity_are_starting_values(tmp_path):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="0" data-duration="2" style="position:absolute; '
                       'left: 100px; top: 100px; width: 400px; height: 200px; opacity: 0.5; '
                       'transform: translate(20px, -10px) rotate(15deg) scale(1.5)"/>')
    plan = plan_of(p, "i", LOGO)
    x, y, w, h, r, a = drawn_rect(plan, LOGO, 0.0)
    # scaled 1.5x about the box centre (300, 200) moved by (20, -10)
    assert (w, h) == pytest.approx((600, 300), abs=0.01)
    assert (x + w / 2, y + h / 2) == pytest.approx((320, 190), abs=0.01)
    assert (r, a) == pytest.approx((15.0, 0.5))


@pytest.mark.parametrize("style, problem", [
    ("filter: blur(4px)", "filter: blur(4px)"),
    ("animation: spin 2s linear infinite", "animation"),
    ("clip-path: circle(50%)", "clip-path"),
    ("border-radius: 24px", "border-radius"),
    ("mix-blend-mode: screen", "mix-blend-mode"),
    ("left: calc(10px + 5%)", "calc"),
    ("transform: matrix(1, 0, 0, 1, 0, 0)", "matrix"),
    ("object-fit: cover; object-position: 10px 20px; width: 100px; height: 100px", "object-position"),
])
def test_layouts_zenvi_cannot_rebuild_are_problems(tmp_path, style, problem):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="0" data-duration="2" style="position:absolute; %s"/>'
             % style)
    plan = plan_of(p, "i", LOGO)
    assert any(problem in pr for pr in plan.problems), plan.problems


def test_cover_inside_a_smaller_box_warns(tmp_path):
    p = load(tmp_path, '<video id="v" src="a.mp4" data-start="0" data-duration="2" muted style="position:absolute; '
                       'left:0; top:0; width: 500px; height: 500px; object-fit: cover"></video>')
    plan = plan_of(p, "v", VIDEO)
    assert any("cropped by its 500x500 box" in w for w in plan.warnings)


def test_another_project_shape_warns_and_keeps_relative_place(tmp_path):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="0" data-duration="2" style="position:absolute; '
                       'left: 960px; top: 540px; width: 400px; height: 200px"/>')
    plan = plan_of(p, "i", LOGO, canvas=(1280, 720))
    x, y, w, h, _r, _a = drawn_rect(plan, LOGO, 0.0, canvas=(1280, 720))
    assert (x, y, w, h) == pytest.approx((640, 360, 266.667, 133.333), abs=0.01)
    plan = plan_of(p, "i", LOGO, canvas=(1080, 1920))
    assert any("stretched to the new shape" in w for w in plan.warnings)


# --- animation -------------------------------------------------------------------------------------------

def _frames(plan):
    n = round(plan.duration * FPS)
    return [plan.position + k / FPS for k in range(n)]


def progress(t, start, duration):
    """GSAP's tween progress at timeline second t (within 1e-8 s of an end it snaps to it)."""
    local = t - start
    if local > duration - 1e-8:
        return 1.0
    return 0.0 if local < 1e-8 else local / duration


def test_power2_fromto_is_one_exact_bezier_segment(tmp_path):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="3" data-duration="2" style="position:absolute; '
                       'left:100px; top:50px; width:400px; height:200px"/>',
             script='tl.fromTo("#i", {opacity: 0, x: -40}, {opacity: 1, x: 0, duration: 0.6, ease: "power2.out"}, 3);')
    plan = plan_of(p, "i", LOGO)
    assert not plan.problems
    pts = plan.props["alpha"]["Points"]
    assert [(pt["co"]["X"], pt["co"]["Y"]) for pt in pts] == [(1.0, 0.0), (19.0, 1.0)]
    assert pts[0]["handle_right"] == pytest.approx({"X": 1 / 3, "Y": 1.0}) and pts[1]["interpolation"] == 0
    ease = gsap.ease_spec("power2.out").fn
    for t in _frames(plan):
        p_ = progress(t, 3.0, 0.6)
        x, _y, _w, _h, _r, a = drawn_rect(plan, LOGO, t)
        assert a == pytest.approx(ease(p_), abs=2e-3), t
        assert x == pytest.approx(100 - 40 + 40 * ease(p_), abs=0.1), t


def test_other_eases_are_sampled_per_frame_exactly(tmp_path):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="0" data-duration="2"/>',
             script='tl.from("#i", {opacity: 0, rotation: 90, duration: 0.5, ease: "expo.out"}, 0.2);'
                    'tl.to("#i", {scale: 2, duration: 0.4, ease: "back.out(1.7)"}, 1);')
    plan = plan_of(p, "i", LOGO)
    expo, back = gsap.ease_spec("expo.out").fn, gsap.ease_spec("back.out(1.7)").fn
    base = curve(plan, "scale_x").value_at(0.0)
    for t in _frames(plan):
        pa = progress(t, 0.2, 0.5)
        pb = progress(t, 1.0, 0.4)
        assert curve(plan, "alpha").value_at(t) == pytest.approx(expo(pa), abs=1e-9), t
        assert curve(plan, "rotation").value_at(t) == pytest.approx(90 - 90 * expo(pa), abs=1e-6), t
        assert curve(plan, "scale_x").value_at(t) == pytest.approx(base * (1 + back(pb)), abs=1e-9), t
    # immediateRender: before the from tween starts the element already shows its from values
    assert curve(plan, "alpha").value_at(0.0) == 0.0 and curve(plan, "rotation").value_at(0.0) == 90.0


def test_set_holds_and_relative_values(tmp_path):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="0" data-duration="3"/>',
             script='tl.set("#i", {opacity: 0.5}, 1); tl.to("#i", {x: "+=96", duration: 1, ease: "none"}, 1);'
                    'tl.to("#i", {x: "-=48", duration: 0.5, ease: "power1.in"}, 2);')
    plan = plan_of(p, "i", LOGO)
    alpha = plan.props["alpha"]["Points"]
    assert alpha[-1]["interpolation"] == CONSTANT and alpha[-1]["co"]["X"] == 31.0
    a = curve(plan, "alpha")
    assert (a.value_at(0.9), a.value_at(1.0), a.value_at(2.5)) == (1.0, 0.5, 0.5)
    lx = curve(plan, "location_x")
    base = lx.value_at(0.0)
    assert lx.value_at(2.0) - base == pytest.approx(96 / 1920)
    assert lx.value_at(2.5) - base == pytest.approx(48 / 1920)


@pytest.mark.parametrize("script, problem", [
    ('tl.to("#i", {x: 10, duration: 1}, 0); tl.to("#i", {x: 20, duration: 1}, 0.5);', "overlapping tweens on x"),
    ('tl.to("#i", {width: 10}, 0);', "animates width"),
    ('tl.to("#i", {x: "10%"}, 0);', "unit '%'"),
    ('tl.to("#i", {x: 10, stagger: 0.1}, 0);', "uses stagger"),
    ('tl.to("#i", {x: 10, ease: "wiggle.out"}, 0);', "not a standard GSAP ease"),
])
def test_animation_zenvi_cannot_rebuild_is_a_problem(tmp_path, script, problem):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="0" data-duration="2"/>', script=script)
    plan = plan_of(p, "i", LOGO)
    assert any(problem in pr for pr in plan.problems), plan.problems


def test_tween_starting_before_the_clip_and_off_grid(tmp_path):
    p = load(tmp_path, '<img id="i" src="logo.png" data-start="1" data-duration="2"/>',
             script='tl.to("#i", {opacity: 0, duration: 1.01, ease: "power2.out"}, 0.5);')
    plan = plan_of(p, "i", LOGO)
    ease = gsap.ease_spec("power2.out").fn
    for t in _frames(plan):
        expected = 1 - ease(progress(t, 0.5, 1.01))
        assert curve(plan, "alpha").value_at(t) == pytest.approx(expected, abs=1e-9), t
    assert all(pt["co"]["X"] >= 1 for pt in plan.props["alpha"]["Points"])


# --- audio and timing ------------------------------------------------------------------------------------

def test_volume_fades_and_automation(tmp_path):
    lane = '{"version":1,"lanes":[{"target":"volume","points":[{"t":0,"v":1},{"t":2,"v":0.5}]}]}'
    p = load(tmp_path, '<audio id="a" src="a.mp4" data-start="0" data-duration="4" data-volume="0.5" '
                       'data-fade-in="0.5" data-fade-out="1"></audio>'
                       '<audio id="b" src="a.mp4" data-start="0" data-duration="4" data-automation=\'%s\' '
                       'data-fade-out="1"></audio>'
                       '<video id="m" src="a.mp4" data-start="0" data-duration="4" muted></video>'
                       '<audio id="c" src="a.mp4" data-start="0" data-duration="4" data-volume="0.8"></audio>' % lane)
    a = plan_of(p, "a", VIDEO)
    vol = Curve.from_json(a.props["volume"], fps=FPS, position=0, start=0)
    assert [round(vol.value_at(t), 6) for t in (0, 0.25, 0.5, 3.0, 3.5, 4.0)] == [0, 0.25, 0.5, 0.5, 0.25, 0]
    b = plan_of(p, "b", VIDEO)
    vol = Curve.from_json(b.props["volume"], fps=FPS, position=0, start=0)
    clip_b = p.root.clip("b")
    for k in range(0, 121, 7):
        t = k / FPS
        assert vol.value_at(t) == pytest.approx(mapping.gain_at(t, 4.0, 1.0, 0.0, 1.0, [(0, 1), (2, 0.5)]),
                                                abs=1e-9), t
    assert clip_b.automation["lanes"][0]["target"] == "volume"
    m = plan_of(p, "m", VIDEO)
    assert m.props["has_audio"]["Points"][0]["co"]["Y"] == 0.0 and "volume" not in m.props
    c = plan_of(p, "c", VIDEO)
    assert c.props["volume"]["Points"][0]["co"]["Y"] == 0.8


def test_media_start_playback_rate_and_running_out(tmp_path):
    p = load(tmp_path, '<video id="v" src="a.mp4" data-start="2" data-duration="5" data-media-start="1.5" muted>'
                       '</video><video id="r" src="a.mp4" data-start="0" data-duration="4" data-playback-rate="2" '
                       'data-media-start="1" muted></video>'
                       '<video id="o" src="a.mp4" data-start="0" data-duration="30" muted></video>')
    v = plan_of(p, "v", VIDEO)
    assert (v.position, v.start, v.end) == (2.0, 1.5, 6.5)
    r = plan_of(p, "r", VIDEO)
    time = Curve.from_json(r.props["time"], fps=FPS, position=0, start=0)
    assert (r.start, r.end) == (0.0, 4.0)
    assert time.value_at(0.0) == 31.0 and time.value_at(2.0) == pytest.approx(31 + 60 * 2)
    o = plan_of(p, "o", VIDEO)
    assert o.end == pytest.approx(20.0) and "Zenvi ends the clip with the media" in o.warnings[0]


def test_loops_animated_images_and_pitch_are_reported(tmp_path):
    """Review C5-1 #11 and plausible items: what a native clip cannot do the way HyperFrames does it."""
    p = load(tmp_path, '<video id="loop" src="a.mp4" data-start="0" data-duration="30" loop muted></video>'
                       '<video id="short" src="a.mp4" data-start="0" data-duration="5" loop muted></video>'
                       '<img id="gif" src="logo.png" data-start="0" data-duration="2"/>'
                       '<audio id="fast" src="a.mp4" data-start="0" data-duration="4" data-playback-rate="2"></audio>'
                       '<video id="quiet" src="a.mp4" data-start="0" data-duration="4" data-playback-rate="2" muted>'
                       '</video>')
    assert any("loops its media" in x for x in plan_of(p, "loop", VIDEO).problems)
    assert not plan_of(p, "short", VIDEO).problems          # a loop that never wraps changes nothing
    assert any("animated image (12 frames)" in x for x in plan_of(p, "gif", dict(LOGO, frames=12)).problems)
    assert not plan_of(p, "gif", dict(LOGO, frames=1)).problems
    # verify-C5-1 #3: ffprobe reports no nb_frames for an APNG -- its codec says it is animated
    assert any("an animated PNG" in x for x in plan_of(p, "gif", dict(LOGO, frames=0, codec="apng")).problems)
    assert not plan_of(p, "gif", dict(LOGO, frames=0, codec="png")).problems
    assert any("keeps the pitch" in w for w in plan_of(p, "fast", dict(VIDEO, has_audio=True)).warnings)
    assert not any("pitch" in w for w in plan_of(p, "quiet", dict(VIDEO, has_audio=True)).warnings)


def test_svg_images_get_their_size_from_the_file(tmp_path):
    from classes.handoff.hyperframes import importer
    svg = tmp_path / "logo.svg"
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 320"><rect width="10" height="10"/></svg>')
    media = importer.ffprobe_media(str(svg))
    assert (media["width"], media["height"], media["has_video"]) == (640, 320, True)
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200px" height="100"></svg>')
    assert importer.svg_size(str(svg)) == (200, 100)
    svg.write_text("not xml")
    assert importer.svg_size(str(svg)) == (0, 0)
