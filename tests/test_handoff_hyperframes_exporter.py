"""Zenvi -> HyperFrames export: structure, geometry and keyframes replayed frame by frame against
libopenshot's own transform, audio attributes, the embedded timeline, folder safety."""

import copy
import json
import math
import os
import re

import pytest

from classes.handoff import transform as tf
from classes.handoff.hyperframes import exporter
from classes.handoff.hyperframes import gsap
from classes.handoff.hyperframes import parser as hfp
from classes.handoff.timeline_view import TimelineSnapshot

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "editor_tools")
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))


def kf(*points):
    """kf((x, y), (x, y, interp), (x, y, interp, handle_right, handle_left))."""
    out = []
    for p in points:
        pt = {"co": {"X": float(p[0]), "Y": float(p[1])}, "interpolation": p[2] if len(p) > 2 else 0,
              "handle_type": 0, "handle_left": {"X": 0.5, "Y": 1.0}, "handle_right": {"X": 0.5, "Y": 0.0}}
        if len(p) > 3 and p[3]:
            pt["handle_right"] = {"X": p[3][0], "Y": p[3][1]}
        if len(p) > 4 and p[4]:
            pt["handle_left"] = {"X": p[4][0], "Y": p[4][1]}
        out.append(pt)
    return {"Points": out}


def media_dir(tmp_path):
    folder = tmp_path / "media"
    folder.mkdir(exist_ok=True)
    for name in ("clip.mp4", "logo.png", "music.wav", "title.svg"):
        (folder / name).write_bytes(b"x" * 64)
    return str(folder)


def project(tmp_path, clips=None, transitions=None, fps=(30, 1)):
    files = json.load(open(os.path.join(FIX, "files.json")))
    clip_t = json.load(open(os.path.join(FIX, "clip.json")))
    proj = json.load(open(os.path.join(SRC, "settings", "_default.project")))
    proj.update(fps={"num": fps[0], "den": fps[1]}, width=1920, height=1080)
    m = media_dir(tmp_path)
    v = dict(files["video"], id="FV", path=os.path.join(m, "clip.mp4"), duration=10.0, width=1280, height=720,
             has_audio=True, name="clip.mp4")
    i = dict(files["image"], id="FI", path=os.path.join(m, "logo.png"), width=400, height=200, name="logo.png")
    a = dict(files["audio"], id="FA", path=os.path.join(m, "music.wav"), duration=8.0, name="music.wav")
    t = dict(files["image"], id="FT", path=os.path.join(m, "title.svg"), width=1920, height=1080, name="title.svg")
    proj["files"] = [v, i, a, t]
    proj["layers"] = [{"id": "L%d" % n, "label": "", "number": n * 1000000, "y": 0, "lock": False}
                      for n in range(1, 5)]

    def clip(cid, f, layer, pos, start, end, **over):
        c = copy.deepcopy(clip_t)
        c.update(id=cid, file_id=f["id"], reader=copy.deepcopy(f), layer=layer, position=pos, start=start, end=end,
                 duration=f["duration"], title=f["name"], effects=[])
        c.update(over)
        return c

    proj["clips"] = clips(clip, v, i, a, t) if clips else [clip("CV", v, 1000000, 0.0, 0.5, 3.5)]
    proj["effects"] = transitions or []
    proj["markers"] = [{"id": "M1", "position": 1.0, "name": "Hit", "icon": "blue.png"}]
    return proj


def export(tmp_path, proj, **kw):
    snap = TimelineSnapshot.from_project(proj, str(tmp_path / "demo.zvn"))
    out = str(tmp_path / "out")
    res = exporter.export_project(snap, exporter.raw_project(proj), out, **kw)
    return res, snap, open(res.index, encoding="utf-8").read()


# --- replaying the exported timeline ---------------------------------------------------------------------

_ZENVI_EASE = re.compile(r"zenviEase \( ([-\d.e]+) , ([-\d.e]+) , ([-\d.e]+) , ([-\d.e]+) , ([-\d.e]+) \)")


def _zenvi_ease(x1, y1, x2, y2, frames):
    tol = 0.01 / max(frames, 1e-9)

    def ease(p):
        if p <= 0:
            return 0.0
        if p >= 1:
            return 1.0
        t, step = 0.5, 0.25
        for _ in range(100):
            u = 1 - t
            x = 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3
            if abs(p - x) < tol:
                break
            t = t - step if x > p else t + step
            step /= 2
        u = 1 - t
        return 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3
    return ease


def _progress(t, start, duration):
    local = t - start
    if duration <= 0 or local > duration - 1e-8:
        return 1.0
    return 0.0 if local < 1e-8 else local / duration


class Replay:
    """Evaluates what the exported GSAP timeline sets on an element at a timeline second."""

    def __init__(self, html):
        doc = hfp.parse_html(html)
        script = [e for e in doc.iter() if e.tag == "script" and not e.attrs.get("src")
                  and e.attrs.get("type") != "application/json"][-1]
        info = gsap.scan_script(script.text)
        self.initial = {}
        for call in info.global_tweens:
            assert call.method == "set"
            self.initial[call.args[0]] = {k: v for k, v in call.args[1].items() if not isinstance(v, str)}
        self.tweens = [c for c in info.calls if c.owner == "tl"]

    def value(self, selector, prop, t):
        value = self.initial.get(selector, {}).get(prop)
        for call in self.tweens:
            if not call.args or call.args[0] != selector:
                continue
            if call.method == "set":
                if prop in call.args[1] and t >= call.args[2] - 1e-9:
                    value = call.args[1][prop]
            elif call.method == "fromTo":
                frm, to, start = call.args[1], call.args[2], call.args[3]
                if prop not in to or t < start - 1e-9:
                    continue
                ease = to["ease"]
                if isinstance(ease, gsap.Ref):
                    m = _ZENVI_EASE.search(ease.text)
                    fn = _zenvi_ease(*[float(g) for g in m.groups()])
                else:
                    assert ease == "none"
                    fn = (lambda p: p)
                p = _progress(t, start, to["duration"])
                value = frm[prop] + (to[prop] - frm[prop]) * fn(p)
            elif call.method == "to":
                vars_, start = call.args[1], call.args[2]
                keys = vars_.get("keyframes") or {}
                if prop not in keys or t < start - 1e-9:
                    continue
                values = keys[prop]
                k = (t - start) / vars_["duration"] * (len(values) - 1)
                lo = max(0, min(len(values) - 1, int(math.floor(k + 1e-9))))
                hi = min(len(values) - 1, lo + 1)
                frac = max(0.0, min(1.0, k - lo))
                value = values[lo] + (values[hi] - values[lo]) * frac
        return value


def _css_rules(html):
    out = {}
    for sel, body in re.findall(r"#(c-[\w-]+) \{ ([^}]*) \}", html):
        out[sel] = dict(part.strip().rstrip(";").split(": ", 1) for part in body.split(";") if ": " in part)
    return out


def css_corners(html, replay, eid, t, src_w, src_h):
    """The source image's corners on the canvas as the browser draws them (CSS box + GSAP transform)."""
    rule = _css_rules(html)[eid]
    left, top = float(rule["left"][:-2]), float(rule["top"][:-2])
    w, h = float(rule["width"][:-2]), float(rule["height"][:-2])
    ox, oy = [float(v[:-1]) / 100 for v in rule["transform-origin"].split()]
    sel = "#" + eid
    static = {}
    text = rule.get("transform", "")
    m = re.search(r"translate\(([-\d.e]+)px, ([-\d.e]+)px\)", text)
    if m:
        static["x"], static["y"] = float(m.group(1)), float(m.group(2))
    m = re.search(r"rotate\(([-\d.e]+)deg\)", text)
    if m:
        static["rotation"] = float(m.group(1))
    m = re.search(r"skew\(([-\d.e]+)deg, ([-\d.e]+)deg\)", text)
    if m:
        static["skewX"], static["skewY"] = float(m.group(1)), float(m.group(2))
    m = re.search(r"scale\(([-\d.e]+), ([-\d.e]+)\)", text)
    if m:
        static["scaleX"], static["scaleY"] = float(m.group(1)), float(m.group(2))

    def ch(prop, default):
        v = replay.value(sel, prop, t)
        return static.get(prop, default) if v is None else v
    x, y = ch("x", 0.0), ch("y", 0.0)
    r = math.radians(ch("rotation", 0.0))
    kx, ky = math.tan(math.radians(ch("skewX", 0.0))), math.tan(math.radians(ch("skewY", 0.0)))
    sx, sy = ch("scaleX", 1.0), ch("scaleY", 1.0)
    pts = []
    for px, py in ((0, 0), (src_w, 0), (0, src_h), (src_w, src_h)):
        bx, by = px * w / src_w - ox * w, py * h / src_h - oy * h      # box px about the origin
        bx, by = bx * sx, by * sy                                         # scale
        bx, by = bx + kx * by, ky * bx + by                               # skew (CSS skew(ax, ay))
        bx, by = bx * math.cos(r) - by * math.sin(r), bx * math.sin(r) + by * math.cos(r)
        pts.append((left + ox * w + x + bx, top + oy * h + y + by))
    return pts, ch("opacity", float(rule.get("opacity", 1)))


def zenvi_corners(snap, clip_id, t):
    clip = snap.clip(clip_id)
    g = tf.clip_geometry(clip, t, snap.width, snap.height)
    sw, sh = clip.file.width, clip.file.height
    return [g.map_point(px, py) for px, py in ((0, 0), (sw, 0), (0, sh), (sw, sh))], g.opacity


def assert_frames_match(snap, html, clip_id, tol=0.02):
    replay = Replay(html)
    clip = snap.clip(clip_id)
    n = round(clip.duration * 30)
    for k in range(n):
        t = clip.timeline_in + k / 30.0
        want, alpha = zenvi_corners(snap, clip_id, t)
        got, opacity = css_corners(html, replay, "c-" + clip_id, t, clip.file.width, clip.file.height)
        for (gx, gy), (wx, wy) in zip(got, want):
            assert gx == pytest.approx(wx, abs=tol) and gy == pytest.approx(wy, abs=tol), (clip_id, t, got, want)
        assert opacity == pytest.approx(alpha, abs=1e-6), (clip_id, t)


# --- tests ---------------------------------------------------------------------------------------------------

def test_project_layout_and_meta_files_like_hyperframes_init(tmp_path):
    res, _snap, html = export(tmp_path, project(tmp_path))
    assert sorted(res.files) == ["README.md", "assets/clip.mp4", "hyperframes.json", "index.html", "meta.json",
                                 "package.json"]
    pkg = json.load(open(os.path.join(res.output_dir, "package.json")))
    assert pkg["scripts"]["render"] == "npx --yes hyperframes@0.8.126 render" and pkg["type"] == "module"
    hfj = json.load(open(os.path.join(res.output_dir, "hyperframes.json")))
    assert hfj["paths"] == {"blocks": "compositions", "components": "compositions/components", "assets": "assets"}
    assert json.load(open(os.path.join(res.output_dir, "meta.json")))["name"] == "demo"
    assert exporter.GSAP_CDN in html and "gsap.min.js" not in "".join(res.files)  # GSAP is never shipped
    assert 'data-composition-id="zenvi-timeline" data-root="true" data-start="0" data-duration="3"' in html
    assert 'data-fps="30"' in html and 'window.__timelines["zenvi-timeline"] = tl;' in html


def test_exported_html_reads_back_with_the_same_timing(tmp_path):
    def clips(clip, v, i, a, t):
        return [clip("CV", v, 1000000, 0.0, 0.5, 3.5),
                clip("CI", i, 2000000, 1.0, 0.0, 2.0),
                clip("CA", a, 1000000, 3.5, 2.0, 4.0, has_video=kf((1, 0.0, 2))),
                clip("CT", t, 3000000, 0.5, 0.0, 2.5)]
    res, _snap, html = export(tmp_path, project(tmp_path, clips))
    p = hfp.load_project(res.output_dir)
    got = {c.zenvi["clip-id"]: (c.kind, c.start, c.duration, c.media_start, c.track_index) for c in p.root.clips}
    assert got == {"CV": ("video", 0.0, 3.0, 0.5, 0), "CA": ("audio", 3.5, 2.0, 2.0, 0),
                   "CI": ("img", 1.0, 2.0, 0.0, 1), "CT": ("img", 0.5, 2.5, 0.0, 2)}
    assert p.root.duration == 5.5 and p.zenvi_timeline["source_project"] == "demo"
    # paint order follows the tracks (z-index) and the DOM order is bottom track first
    rules = _css_rules(html)
    assert (rules["c-CV"]["z-index"], rules["c-CI"]["z-index"], rules["c-CT"]["z-index"]) == ("1", "2", "3")
    assert html.index('id="c-CV"') < html.index('id="c-CI"') < html.index('id="c-CT"')


@pytest.mark.parametrize("over", [
    {},                                                                            # static, full frame
    {"scale_x": kf((1, 0.5)), "scale_y": kf((1, 0.25)), "location_x": kf((1, 0.2)), "rotation": kf((1, 30))},
    {"gravity": tf.GRAVITY_TOP_LEFT, "scale_x": kf((1, 0.5)), "scale_y": kf((1, 0.5)), "origin_x": kf((1, 0.2))},
    {"scale": tf.SCALE_CROP, "location_y": kf((1, -0.1))},
    {"scale": tf.SCALE_NONE, "gravity": tf.GRAVITY_BOTTOM_RIGHT, "shear_x": kf((1, 0.3))},
    {"scale": tf.SCALE_NONE, "scale_x": kf((1, 0.5)), "scale_y": kf((1, 0.5))},   # decoded small, drawn at 0.5
])
@pytest.mark.parametrize("media", ["image", "video"])
def test_static_geometry_matches_libopenshot(tmp_path, over, media):
    def clips(clip, v, i, a, t):
        return [clip("CI", i if media == "image" else v, 1000000, 0.0, 0.0, 1.0, **over)]
    _res, snap, html = export(tmp_path, project(tmp_path, clips))
    assert "keyframes:" not in html and "tl.fromTo" not in html  # plain CSS
    assert_frames_match(snap, html, "CI")


def test_keyframes_replay_frame_by_frame(tmp_path):
    def clips(clip, v, i, a, t):
        return [
            # one curve per channel, centre gravity: exact eased segments (bezier, linear, hold)
            clip("CA", i, 1000000, 0.0, 0.0, 2.0,
                 location_x=kf((1, -0.4), (19, -0.3, 0, (0.16, 1.0), (0.3, 1.0)), (40, 0.1, 1)),
                 alpha=kf((1, 0.0), (16, 1.0, 1), (31, 0.4, 2)),
                 rotation=kf((1, 0.0), (31, 10.0, 0)), scale_x=kf((1, 0.25), (61, 0.5, 0)),
                 scale_y=kf((1, 0.25))),
            # off-centre gravity with scale AND location animated: sampled per frame
            clip("CB", i, 2000000, 0.5, 0.0, 1.5, gravity=tf.GRAVITY_TOP_LEFT,
                 scale_x=kf((1, 0.3), (30, 0.6, 0)), scale_y=kf((1, 0.3), (30, 0.6, 0)),
                 location_x=kf((1, 0.0), (45, 0.2, 1))),
            # a trimmed clip whose keyframes start before its in point
            clip("CC", v, 3000000, 1.0, 1.0, 2.0, location_y=kf((1, 0.0), (61, 0.5, 0)),
                 shear_x=kf((31, 0.0), (61, 0.4, 1))),
        ]
    _res, snap, html = export(tmp_path, project(tmp_path, clips))
    assert "zenviEase(" in html and 'ease: "none"' in html and "keyframes:" in html
    for cid in ("CA", "CB", "CC"):
        assert_frames_match(snap, html, cid)


def _fade(position, seconds=1.0, fps=30, **extra):
    mask = os.path.join(SRC, "transitions", "common", "fade.svg")
    frames = int(round(seconds * fps)) + 1
    t = {"id": "T%g" % position, "layer": 1000000, "type": "Mask", "position": position, "start": 0,
         "end": seconds, "brightness": kf((1, 1.0, 1), (frames, -1.0, 1)), "contrast": kf((1, 3.0, 1)),
         "reader": {"path": mask}}
    t.update(extra)
    return t


# what libopenshot 1.0 draws, measured with its own Timeline + Mask (red clip A 0-3 s under blue clip B 2-5 s,
# fade 2-3 s): B's alpha per 1-based frame -- 61: 0, 62: 0, 65: 58, 70: 158, 75: 255; A stays at 255
LIBOPENSHOT_FADE = {61: 0.0, 62: 0.0, 65: 58 / 255, 70: 158 / 255, 75: 1.0, 80: 1.0}


def test_crossfade_fades_only_the_top_clip_inside_the_transition(tmp_path):
    """Review C5-1 #5: the transition faded every overlapping clip and hid them before it started."""
    def clips(clip, v, i, a, t):
        return [clip("CA", i, 1000000, 0.0, 0.0, 3.0), clip("CB", t, 1000000, 2.0, 0.0, 3.0)]
    _res, _snap, html = export(tmp_path, project(tmp_path, clips, [_fade(2.0)]))
    r = Replay(html)
    # the clip underneath is untouched: no opacity animation at all, fully opaque
    assert r.value("#c-CA", "opacity", 2.5) is None and _css_rules(html)["c-CA"]["opacity"] == "1"
    for frame, alpha in LIBOPENSHOT_FADE.items():
        assert r.value("#c-CB", "opacity", (frame - 1) / 30.0) == pytest.approx(alpha, abs=1.5 / 255), frame
    assert r.value("#c-CB", "opacity", 4.5) == pytest.approx(1.0)


def test_a_fade_in_the_middle_of_a_clip_leaves_the_clip_alone_before_it(tmp_path):
    def clips(clip, v, i, a, t):
        return [clip("CI", i, 1000000, 0.0, 0.0, 3.0)]
    _res, _snap, html = export(tmp_path, project(tmp_path, clips, [_fade(1.0)]))
    r = Replay(html)
    assert r.value("#c-CI", "opacity", 0.5) == pytest.approx(1.0)              # not hidden until it starts
    assert r.value("#c-CI", "opacity", 1.0) == pytest.approx(0.0, abs=1e-5)    # the mask, as libopenshot does
    assert r.value("#c-CI", "opacity", 1.0 + 9 / 30) == pytest.approx(158 / 255, abs=1.5 / 255)
    assert r.value("#c-CI", "opacity", 2.5) == pytest.approx(1.0)


def test_audio_fading_transitions_export_their_equal_power_fades(tmp_path):
    def clips(clip, v, i, a, t):
        return [clip("CA", a, 1000000, 0.0, 0.0, 3.0), clip("CB", a, 1000000, 2.0, 0.0, 3.0)]
    _res, _snap, _html = export(tmp_path, project(tmp_path, clips, [_fade(2.0, fade_audio_hint=True)]))
    p = hfp.load_project(str(tmp_path / "out"))
    c = {x.zenvi["clip-id"]: x for x in p.root.clips}
    out_pts = {round(pt["t"], 4): pt["v"] for pt in c["CA"].automation["lanes"][0]["points"]}
    in_pts = {round(pt["t"], 4): pt["v"] for pt in c["CB"].automation["lanes"][0]["points"]}
    # libopenshot: frame F in the transition (61..90), t = (F - 61) / 29; out = cos, in = sin (equal power)
    for frame in (61, 70, 80, 90):
        p_ = (frame - 61) / 29.0
        assert out_pts[round((frame - 1) / 30.0, 4)] == pytest.approx(math.cos(p_ * math.pi / 2), abs=1e-6)
        assert in_pts[round((frame - 1 - 60) / 30.0, 4)] == pytest.approx(math.sin(p_ * math.pi / 2), abs=1e-6)
    assert out_pts[0.0] == 1.0 and in_pts[round(40 / 30.0, 4)] == 1.0


def test_audio_attributes(tmp_path):
    def clips(clip, v, i, a, t):
        return [clip("CV", v, 1000000, 0.0, 0.0, 3.0, volume=kf((1, 1.0), (91, 0.0, 1))),
                clip("CM", v, 2000000, 3.0, 0.0, 1.0, has_audio=kf((1, 0.0, 2))),
                clip("CA", a, 3000000, 0.0, 0.0, 2.0, volume=kf((1, 0.5))),
                clip("CB", a, 4000000, 2.0, 0.0, 1.0, volume=kf((1, 0.0), (31, 1.0, 0)))]
    _res, _snap, html = export(tmp_path, project(tmp_path, clips))
    p = hfp.load_project(str(tmp_path / "out"))
    c = {x.zenvi["clip-id"]: x for x in p.root.clips}
    assert c["CV"].has_audio and c["CV"].automation["lanes"][0]["points"] == [{"t": 0.0, "v": 1.0},
                                                                               {"t": 3.0, "v": 0.0}]
    assert c["CM"].muted and c["CA"].volume == 0.5
    lane = c["CB"].automation["lanes"][0]["points"]
    assert len(lane) == 30 and lane[15]["v"] == pytest.approx(0.5, abs=0.05)  # a bezier fade: sampled


def test_speed_and_unsupported_features_warn(tmp_path):
    def clips(clip, v, i, a, t):
        fast = clip("CF", v, 1000000, 0.0, 0.0, 2.0, time=kf((1, 1.0, 1), (61, 121.0, 1)))
        rev = clip("CR", v, 2000000, 2.0, 0.0, 1.0, time=kf((1, 31.0, 1), (31, 1.0, 1)))
        fx = clip("CX", i, 3000000, 0.0, 0.0, 1.0, corner_radius=kf((1, 20)),
                  effects=[{"id": "E1", "class_name": "Blur", "name": "Blur"}])
        return [fast, rev, fx]
    res, _snap, html = export(tmp_path, project(tmp_path, clips))
    assert 'data-playback-rate="2' in html
    w = " ".join(res.warnings)
    assert "plays constant reversed" in w and "Blur effect is not exported" in w and "rounded corners" in w
    # review C5-1 #11: the video has sound -- HyperFrames keeps its pitch at 2x, Zenvi does not
    assert re.search(r"'clip.mp4' plays at 2[.\d]*x: Zenvi's speed change shifts its pitch", w)
    readme = open(os.path.join(res.output_dir, "README.md")).read()
    assert "Blur effect" in readme and "gsap.com/standard-license" in readme


def test_embedded_timeline_is_safe_html_and_maps_media(tmp_path):
    proj = project(tmp_path)
    proj["clips"][0]["title"] = "</script><b>& x"
    res, _snap, html = export(tmp_path, proj)
    assert "</script><b>" not in html.split('id="zenvi-timeline">', 1)[1].split("</script>", 1)[0]
    data = hfp.load_project(res.output_dir).zenvi_timeline
    zp = data["zenvi"]
    assert zp["project"]["clips"][0]["title"] == "</script><b>& x"
    assert zp["assets"] == {"FV": "assets/clip.mp4"} and zp["originals"]["FV"].endswith("clip.mp4")
    assert zp["project"]["files"][0]["path"] == "assets/clip.mp4"
    assert zp["elements"]["CV"] == {"kind": "video", "element": "c-CV", "src": "assets/clip.mp4", "start": 0.0,
                                    "duration": 3.0, "media_start": 0.5, "track": 0}
    assert "history" not in zp["project"]


def test_copy_media_false_links_the_originals(tmp_path):
    res, _snap, _html = export(tmp_path, project(tmp_path), copy_media=False)
    link = os.path.join(res.output_dir, "assets", "clip.mp4")
    assert os.path.islink(link) and os.path.realpath(link) == os.path.realpath(str(tmp_path / "media" / "clip.mp4"))


def test_output_folder_rules_and_re_export(tmp_path):
    with pytest.raises(exporter.ExportError, match="output_dir"):
        exporter.check_output_dir("")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "mine.txt").write_text("x")
    with pytest.raises(exporter.ExportError, match="not empty"):
        exporter.check_output_dir(str(busy))
    res, _s, _h = export(tmp_path, project(tmp_path))
    manifest = exporter.read_manifest(res.output_dir)
    assert sorted(manifest["files"]) == ["README.md", "assets/clip.mp4", "hyperframes.json", "index.html",
                                         "meta.json", "package.json"]
    (tmp_path / "out" / "notes.md").write_text("keep me")
    (tmp_path / "out" / "assets" / "logo.png").write_bytes(b"the user's own logo")   # not Zenvi's file
    # export again over the earlier export: Zenvi's files are replaced, the user's kept, stale assets removed
    def clips(clip, v, i, a, t):
        return [clip("CI", i, 1000000, 0.0, 0.0, 1.0)]
    snap = TimelineSnapshot.from_project(project(tmp_path, clips), str(tmp_path / "demo.zvn"))
    again = exporter.export_project(snap, exporter.raw_project(project(tmp_path, clips)), res.output_dir)
    names = sorted(os.listdir(os.path.join(res.output_dir, "assets")))
    assert names == ["logo-2.png", "logo.png"] and (tmp_path / "out" / "notes.md").read_text() == "keep me"
    assert (tmp_path / "out" / "assets" / "logo.png").read_bytes() == b"the user's own logo"
    assert again.clips == 1 and "assets/logo-2.png" in exporter.read_manifest(res.output_dir)["files"]
    assert not [n for n in os.listdir(str(tmp_path)) if n.startswith(".zenvi-export-")]  # staging + backup gone


def _earlier_export(tmp_path):
    res, _s, _h = export(tmp_path, project(tmp_path))
    return res.output_dir


def test_re_export_never_deletes_outside_the_folder(tmp_path):
    """Review C5-1 #6: a crafted record ("assets/../../../victim.txt") made the re-export delete that file."""
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    out = _earlier_export(tmp_path)
    path = os.path.join(out, exporter.MANIFEST)
    data = json.load(open(path))
    data["files"]["assets/../../victim.txt"] = {"size": 8, "mtime_ns": 0}
    json.dump(data, open(path, "w"))
    with pytest.raises(exporter.ExportError, match="not a file inside the folder"):
        export(tmp_path, project(tmp_path))
    data["files"].pop("assets/../../victim.txt")
    data["files"][str(victim)] = {"size": 8}                     # absolute
    json.dump(data, open(path, "w"))
    with pytest.raises(exporter.ExportError, match="not a file inside the folder"):
        export(tmp_path, project(tmp_path))
    assert victim.read_text() == "precious"
    # an export from before the manifest: its embedded JSON is not trusted for deleting anything
    os.unlink(path)
    with pytest.raises(exporter.ExportError, match="kept no record"):
        export(tmp_path, project(tmp_path))
    assert victim.read_text() == "precious"


def test_re_export_refuses_a_linked_assets_folder(tmp_path):
    """Review C5-1 #6: a symlinked assets/ was written into and pruned (another folder's files lost)."""
    out = _earlier_export(tmp_path)
    library = tmp_path / "library"
    os.rename(os.path.join(out, "assets"), str(library))
    (library / "unrelated.png").write_bytes(b"keep")
    os.symlink(str(library), os.path.join(out, "assets"))
    before = sorted(os.listdir(library))
    with pytest.raises(exporter.ExportError, match="is a link to another folder"):
        export(tmp_path, project(tmp_path))
    assert sorted(os.listdir(library)) == before


def test_re_export_stops_on_edits_made_in_hyperframes(tmp_path):
    """Review C5-1 #7: index.html / package.json / assets edited since the export were silently replaced."""
    out = _earlier_export(tmp_path)
    index = os.path.join(out, "index.html")
    html = open(index, encoding="utf-8").read()
    # what HyperFrames Studio does when it opens the project: stamps data-hf-id -- not an edit
    open(index, "w", encoding="utf-8").write(html.replace('<video id="c-CV"', '<video data-hf-id="hf-1" id="c-CV"'))
    assert exporter.plan_output(out).changed == []
    open(index, "w", encoding="utf-8").write(html.replace('data-start="0"', 'data-start="0.5"', 1))
    pkg = os.path.join(out, "package.json")
    open(pkg, "a").write("\n")
    with pytest.raises(exporter.ExportChanged) as err:
        export(tmp_path, project(tmp_path))
    assert err.value.changed == ["index.html", "package.json"]
    assert "data-start=\"0.5\"" in open(index, encoding="utf-8").read()         # nothing was replaced
    res, _s, _h = export(tmp_path, project(tmp_path), overwrite_changes=True)
    assert exporter.plan_output(out).changed == [] and res.clips == 1


def test_a_failed_re_export_puts_the_earlier_export_back(tmp_path, monkeypatch):
    """Review C5-1 #7: files were replaced one by one, so a failure left a mixed project."""
    out = _earlier_export(tmp_path)
    before = {n: open(os.path.join(out, n), "rb").read() for n in ("index.html", "package.json", "README.md")}
    real_replace, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 9:  # partway through moving the new files in
            raise OSError("disk full")
        return real_replace(src, dst)
    monkeypatch.setattr(exporter.os, "replace", flaky)
    with pytest.raises(OSError, match="disk full"):
        export(tmp_path, project(tmp_path))
    monkeypatch.setattr(exporter.os, "replace", real_replace)
    assert {n: open(os.path.join(out, n), "rb").read() for n in before} == before
    assert exporter.plan_output(out).changed == [] and os.path.isfile(os.path.join(out, "assets", "clip.mp4"))
    assert not [n for n in os.listdir(str(tmp_path)) if n.startswith(".zenvi-export-")]


def test_nothing_to_export(tmp_path):
    proj = project(tmp_path)
    proj["clips"] = []
    snap = TimelineSnapshot.from_project(proj)
    with pytest.raises(exporter.ExportError, match="no clips"):
        exporter.export_project(snap, exporter.raw_project(proj), str(tmp_path / "out"))
    assert not os.path.exists(str(tmp_path / "out"))


GOLDEN = os.path.join(HERE, "fixtures", "hyperframes", "golden", "index.html")


def test_golden_index_html(tmp_path):
    """The exported index.html for a fixed project; regenerate with UPDATE_GOLDEN=1 after a deliberate change."""
    def clips(clip, v, i, a, t):
        return [clip("CV", v, 1000000, 0.0, 0.5, 3.5, volume=kf((16, 1.0), (106, 0.0, 1))),
                clip("CI", i, 2000000, 1.0, 0.0, 2.0, scale_x=kf((1, 0.25)), scale_y=kf((1, 0.25)),
                     location_x=kf((1, -0.4), (19, -0.3, 0, (0.16, 1.0), (0.3, 1.0))),
                     alpha=kf((1, 0.0), (16, 1.0, 1)), rotation=kf((1, 0.0), (31, 10.0, 0))),
                clip("CA", a, 3000000, 3.5, 0.0, 1.5, has_video=kf((1, 0.0, 2)), volume=kf((1, 0.5))),
                clip("CT", t, 4000000, 0.5, 0.0, 2.5)]
    _res, _snap, html = export(tmp_path, project(tmp_path, clips))
    html = html.replace(str(tmp_path), "<TMP>").replace('"Zenvi %s"' % exporter._zenvi_version(), '"Zenvi <v>"')
    html = re.sub(r'"generator":"Zenvi [^"]*"', '"generator":"Zenvi <v>"', html)
    if os.environ.get("UPDATE_GOLDEN"):
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with open(GOLDEN, "w", encoding="utf-8") as fh:
            fh.write(html)
    with open(GOLDEN, encoding="utf-8") as fh:
        assert html == fh.read()


def test_re_export_after_a_case_only_rename_of_the_media(tmp_path):
    """verify-C5-1 #4: on a case-insensitive disk the renamed asset's new name is the old file."""
    proj = project(tmp_path)
    res, _s, _h = export(tmp_path, proj)
    media = str(tmp_path / "media")
    os.rename(os.path.join(media, "clip.mp4"), os.path.join(media, "tmp.mp4"))
    os.rename(os.path.join(media, "tmp.mp4"), os.path.join(media, "Clip.mp4"))
    for f in proj["files"]:
        if f["id"] == "FV":
            f["path"] = os.path.join(media, "Clip.mp4")
    for c in proj["clips"]:
        if c["file_id"] == "FV":
            c["reader"]["path"] = os.path.join(media, "Clip.mp4")
    snap = TimelineSnapshot.from_project(proj, str(tmp_path / "demo.zvn"))
    again = exporter.export_project(snap, exporter.raw_project(proj), res.output_dir)
    names = sorted(os.listdir(os.path.join(res.output_dir, "assets")))
    assert names == ["Clip.mp4"] and again.clips == 1
    assert sorted(exporter.read_manifest(res.output_dir)["files"]) == ["README.md", "assets/Clip.mp4",
                                                                     "hyperframes.json", "index.html", "meta.json",
                                                                     "package.json"]
