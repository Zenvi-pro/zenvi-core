"""Colour measured per shot and per file, checked against pictures whose answers are known.

The scope definition is pinned against the real FrameScope in ``test_framescope_parity`` (needs
libopenshot): Rec.601 luma rounded to 8 bits, clipped shadows = luma <= 2, clipped highlights =
luma >= 253, 256-bin histograms. Measured at the time of writing on a structured 640x360 still:
average luma, clipped counts and the R/G/B histograms were identical, and the luma histogram
differed by 248 of 230400 pixels (rounding). Small analysis frames were also compared with a
640 px analysis on real clips: means within 0.001, percentiles within 0.015, clipped fractions
within 0.005.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import media_fixtures as mf  # noqa: E402
from classes import color_agent as ca  # noqa: E402
from classes.media_index import look  # noqa: E402
from classes.media_index.probe import probe_media  # noqa: E402
from classes.media_index.structure import analyze_structure, rgb_to_gray  # noqa: E402


def solid(r, g, b, h=90, w=160):
    a = np.zeros((h, w, 3), np.uint8)
    a[...] = (r, g, b)
    return a


def feed(frames, fps=10.0, stride=1):
    acc = look.LookAccumulator(fps=fps, stride=stride)
    for i, rgb in enumerate(frames):
        acc.add(i, rgb, rgb_to_gray(rgb))
    return acc


# ============================ the per-frame scope ============================
def test_frame_scope_has_the_framescope_shape_and_counts():
    rgb = np.zeros((10, 10, 3), np.uint8)
    rgb[:5] = 255            # half white, half black
    scope = look.frame_scope(rgb_to_gray(rgb), rgb)
    assert len(scope["histogram"]["luma"]) == 256 and sum(scope["histogram"]["luma"]) == 100
    assert scope["summary"]["clipped_shadows"] == 50 and scope["summary"]["clipped_highlights"] == 50
    assert scope["summary"]["avg_luma"] == pytest.approx(0.5)


def test_clipping_thresholds_match_framescope():
    # Probed on the real library: grey 0..2 count as clipped shadows, 253..255 as clipped highlights.
    for level, shadow, highlight in ((2, 1, 0), (3, 0, 0), (252, 0, 0), (253, 0, 1), (255, 0, 1)):
        rgb = solid(level, level, level)
        s = look.frame_scope(rgb_to_gray(rgb), rgb)["summary"]
        assert (s["clipped_shadows"] > 0, s["clipped_highlights"] > 0) == (bool(shadow), bool(highlight)), level


def test_known_colours_have_the_known_measurements():
    acc = feed([solid(200, 100, 50)] * 10)
    r = acc.profile(0, 1.0)
    p = r["profile"]
    assert p["avg_luma"] == pytest.approx(124 / 255, abs=1e-3)          # Rec.601
    assert p["warm_cool"] == pytest.approx((200 - 50) / 255, abs=1e-3)   # red minus blue
    assert p["channel_means"]["red"] == pytest.approx(200 / 255, abs=1e-3)
    assert r["extras"]["saturation"]["mean"] == pytest.approx(0.75, abs=1e-3)
    assert p["clipped_shadows"] == 0.0 and p["clipped_highlights"] == 0.0


def test_a_cool_picture_reads_cool_and_a_grey_one_reads_neutral():
    assert feed([solid(40, 80, 200)] * 5).profile(0, 1)["profile"]["warm_cool"] < -0.5
    neutral = feed([solid(128, 128, 128)] * 5).profile(0, 1)
    assert neutral["profile"]["warm_cool"] == pytest.approx(0.0, abs=1e-3)
    assert neutral["extras"]["saturation"]["mean"] == 0.0


def test_a_white_frame_and_a_black_frame_clip_completely_as_fractions():
    white = feed([solid(255, 255, 255)] * 4).profile(0, 1)["profile"]
    black = feed([solid(0, 0, 0)] * 4).profile(0, 1)["profile"]
    assert white["clipped_highlights"] == 1.0 and white["clipped_shadows"] == 0.0
    assert black["clipped_shadows"] == 1.0 and black["clipped_highlights"] == 0.0


def test_a_grey_ramp_has_evenly_spread_percentiles_and_its_ends_as_black_and_white_points():
    ramp = np.tile(np.linspace(0, 255, 256).astype(np.uint8), (8, 1))
    rgb = np.stack([ramp] * 3, axis=-1)
    r = feed([rgb] * 4).profile(0, 1)["extras"]
    pcts = r["luma_percentiles"]
    for key, want in (("p5", 0.05), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p95", 0.95)):
        assert pcts[key] == pytest.approx(want, abs=0.02), key
    assert r["black_point"] <= 0.02 and r["white_point"] >= 0.98


def test_tone_bands_report_the_colour_of_shadows_mids_and_highlights():
    rgb = np.zeros((90, 150, 3), np.uint8)
    rgb[:, :50] = (30, 10, 60)      # shadows: a purple-ish dark
    rgb[:, 50:100] = (120, 120, 120)  # mids
    rgb[:, 100:] = (250, 230, 190)  # highlights: warm
    t = feed([rgb] * 4).profile(0, 1)["extras"]["tone_bands"]
    assert t["shadows"]["mean_rgb"][2] > t["shadows"]["mean_rgb"][1]               # bluish shadows
    assert t["highlights"]["mean_rgb"][0] > t["highlights"]["mean_rgb"][2]         # warm highlights
    assert t["shadows"]["fraction"] == pytest.approx(1 / 3, abs=0.01)
    assert sum(b["fraction"] for b in t.values()) == pytest.approx(1.0, abs=1e-3)


def test_hue_balance_peaks_where_the_colour_is():
    rgb = solid(0, 0, 255)  # blue = hue 240 = band 8 of 12
    hb = feed([rgb] * 3).profile(0, 1)["extras"]["hue_balance"]
    assert int(np.argmax(hb)) == 8 and hb[8] == pytest.approx(1.0, abs=1e-3) and len(hb) == 12


def test_the_palette_finds_the_dominant_colours_with_their_weights():
    rgb = np.zeros((90, 160, 3), np.uint8)
    rgb[:, :100] = (220, 30, 30)    # 62.5% red
    rgb[:, 100:] = (20, 40, 200)    # 37.5% blue
    pal = feed([rgb] * 6).profile(0, 1)["extras"]["palette"]
    top = pal[0]
    assert top["hex"] == "#dc1e1e" and top["weight"] == pytest.approx(0.625, abs=0.08)
    assert pal[1]["hex"] == "#1428c8"
    assert sum(c["weight"] for c in pal) == pytest.approx(1.0, abs=1e-3)


def test_the_palette_is_deterministic_and_handles_tiny_inputs():
    px = np.random.default_rng(0).random((200, 3)).astype(np.float32)
    assert look.kmeans_palette(px) == look.kmeans_palette(px)
    assert look.kmeans_palette(np.zeros((0, 3), np.float32)) == []
    assert len(look.kmeans_palette(np.array([[0.1, 0.2, 0.3]], np.float32), k=5)) == 1


# ============================ ranges, shots and the file ============================
def test_each_shot_gets_its_own_profile_and_the_file_reports_the_spread():
    dark, bright = solid(20, 20, 30), solid(230, 200, 150)
    acc = feed([dark] * 20 + [bright] * 20)          # 10 fps: 0-2 s dark, 2-4 s bright
    shots = [{"id": 0, "start": 0.0, "end": 2.0}, {"id": 1, "start": 2.0, "end": 4.0}]
    out = acc.finalize(shots, look.pipeline_from_probe({}))
    p0, p1 = out["shots"][0]["profile"], out["shots"][1]["profile"]
    assert p0["avg_luma"] < 0.15 and p1["avg_luma"] > 0.7 and p1["warm_cool"] > 0.2 > p0["warm_cool"] - 0.5
    spread = out["file"]["shot_variance"]
    assert spread["avg_luma"] > 0.2, "two very different shots must show as a wide spread"
    assert out["file"]["profile"]["samples"] == 40
    assert out["sampling"]["frames"] == 40 and out["file"]["kind"] == "measured"


def test_a_shot_shorter_than_the_sampling_gap_uses_its_nearest_sample():
    acc = feed([solid(10, 10, 10)] * 10 + [solid(240, 240, 240)] * 10, stride=5)   # samples at 0, .5, 1.0, 1.5 s
    got = acc.profile(1.02, 1.08)
    assert got is not None and got["samples"] == 1 and got["profile"]["avg_luma"] > 0.9


def test_a_range_with_no_footage_has_no_profile():
    assert look.LookAccumulator().profile(0, 1) is None


def test_only_every_stride_th_frame_is_sampled():
    acc = feed([solid(9, 9, 9)] * 21, stride=5)
    assert [round(s.t, 1) for s in acc.samples] == [0.0, 0.5, 1.0, 1.5, 2.0]


def test_stored_profiles_feed_phase_6_unchanged():
    """The stored profile is a color_agent LookProfile: its own distance and solver accept it."""
    a = feed([solid(200, 150, 100)] * 6).profile(0, 1)["profile"]
    b = feed([solid(100, 150, 200)] * 6).profile(0, 1)["profile"]
    assert ca.look_profile_distance(a, a) == 0.0
    assert ca.look_profile_distance(a, b) > 0.2
    patch = ca.solve_grade_from_profiles(a, b)
    assert patch.get("temperature", 0.0) < 0.0, "warm to cool needs a cooler grade"


# ============================ pipeline awareness ============================
def test_pipeline_is_read_from_the_probe():
    p = look.pipeline_from_probe({"video": {"color_primaries": "bt2020", "color_transfer": "arib-std-b67",
                                            "color_space": "bt2020nc", "color_range": "tv", "bit_depth": 10, "hdr": True}})
    assert p["hdr"] is True and p["bit_depth"] == 10 and p["transfer"] == "arib-std-b67"
    assert look.pipeline_from_probe({})["transfer"] == "unspecified"


def test_hdr_footage_is_flagged_as_not_display_referred():
    out = feed([solid(120, 110, 100)] * 10).finalize(
        [{"id": 0, "start": 0.0, "end": 1.0}], look.pipeline_from_probe({"video": {"hdr": True}}))
    assert out["pipeline"]["display_referred"] is False
    assert any(w.startswith("hdr_not_tonemapped") for w in out["warnings"])


def test_flat_desaturated_footage_is_flagged_as_possibly_log_and_normal_footage_is_not():
    rng = np.random.default_rng(2)
    flat = np.clip(rng.normal(120, 12, (90, 160, 1)), 90, 150).astype(np.uint8).repeat(3, axis=2)
    normal = np.zeros((90, 160, 3), np.uint8)
    normal[:, :80] = (230, 40, 30)
    normal[:, 80:] = (10, 20, 90)
    shots = [{"id": 0, "start": 0.0, "end": 1.0}]
    flat_out = feed([flat] * 10).finalize(shots, look.pipeline_from_probe({}))
    norm_out = feed([normal] * 10).finalize(shots, look.pipeline_from_probe({}))
    assert flat_out["pipeline"]["log_guess"] is True and 0.0 < flat_out["pipeline"]["log_confidence"] <= 1.0
    assert flat_out["pipeline"]["display_referred"] is False
    assert norm_out["pipeline"]["log_guess"] is False and norm_out["pipeline"]["display_referred"] is True
    assert norm_out["warnings"] == []


def test_flat_look_needs_all_of_its_signs():
    assert look.flat_look({})["flat"] is False
    contrasty = {"black_point": 0.02, "white_point": 0.95, "saturation": {"mean": 0.1},
                 "luma_percentiles": {"p5": 0.03, "p95": 0.9}}
    assert look.flat_look(contrasty)["flat"] is False


# ============================ through the decode pass (real ffmpeg) ============================
def test_a_decoded_video_yields_a_profile_for_every_shot(tmp_path):
    mf.need_ffmpeg()
    a, b = mf.scene(21), mf.scene(22)
    parts = [mf.render(str(tmp_path / "a.mp4"), a, mf.hold(a), 3.0), mf.render(str(tmp_path / "b.mp4"), b, mf.hold(b), 3.0)]
    path = mf.join_hard(str(tmp_path / "ab.mp4"), parts)
    probe = probe_media(path)
    acc = look.LookAccumulator()
    structure = analyze_structure(path, probe, look=acc)
    out = acc.finalize(structure["shots"], look.pipeline_from_probe(probe))
    assert len(out["shots"]) == 2 and all(s["samples"] >= 5 for s in out["shots"])
    assert out["shots"][0]["profile"]["present"] and out["shots"][1]["profile"]["present"]
    l0, l1 = out["shots"][0]["profile"]["avg_luma"], out["shots"][1]["profile"]["avg_luma"]
    assert 0.0 < l0 < 1.0 and 0.0 < l1 < 1.0
    assert out["file"]["profile"]["samples"] == out["sampling"]["frames"]
    assert 0.0 <= out["file"]["profile"]["clipped_shadows"] <= 1.0, "clipping must come out as a fraction"
    assert out["pipeline"]["hdr"] is False


# ============================ against the real FrameScope ============================
def test_framescope_parity_on_a_structured_still(tmp_path):
    openshot = pytest.importorskip("openshot")
    if not hasattr(openshot, "FrameScope"):
        pytest.skip("needs the real libopenshot module (ZENVI_REAL_QT=1 and openshot on PYTHONPATH)")
    w, h = 640, 360
    rng = np.random.default_rng(7)
    y, x = np.mgrid[0:h, 0:w]
    img = np.zeros((h, w, 3), np.float64)
    img[..., 0], img[..., 1] = x / w * 255, y / h * 255
    img[..., 2] = 128 + 100 * np.sin(x / 37.0) * np.cos(y / 23.0)
    img[40:90, 40:200] = 0
    img[200:260, 300:520] = 255
    img[100:180, 400:560] = (250, 40, 60)
    img += rng.normal(0, 6, img.shape)
    rgb = np.clip(img, 0, 255).astype(np.uint8)
    png = tmp_path / "still.png"
    Image.fromarray(rgb).save(png)

    timeline = openshot.Timeline(w, h, openshot.Fraction(30, 1), 48000, 2, 3)
    clip = openshot.Clip(str(png))
    clip.Position(0.0)
    clip.End(1.0)
    timeline.AddClip(clip)
    timeline.Open()
    try:
        scope = openshot.FrameScope()
        scope.SetFrame(timeline.GetFrame(3))
        real = {"avg": scope.GetVideoAverageLuma(), "cs": scope.GetVideoClippedShadows(),
                "ch": scope.GetVideoClippedHighlights(), "luma": np.array(list(scope.GetVideoHistogramLuma())),
                "r": np.array(list(scope.GetVideoHistogramRed())), "g": np.array(list(scope.GetVideoHistogramGreen())),
                "b": np.array(list(scope.GetVideoHistogramBlue()))}
    finally:
        timeline.Close()

    mine = look.frame_scope(rgb_to_gray(rgb), rgb)
    assert mine["summary"]["avg_luma"] == pytest.approx(real["avg"], abs=1e-3)
    assert mine["summary"]["clipped_shadows"] == int(real["cs"])
    assert mine["summary"]["clipped_highlights"] == int(real["ch"])
    for name, key in (("red", "r"), ("green", "g"), ("blue", "b")):
        assert np.array_equal(np.array(mine["histogram"][name]), real[key]), name
    l1 = np.abs(np.array(mine["histogram"]["luma"]) - real["luma"]).sum()
    assert l1 / real["luma"].sum() < 0.005, "luma histograms may differ only by rounding"
