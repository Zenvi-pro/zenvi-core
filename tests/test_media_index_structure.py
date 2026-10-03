"""Shots and camera motion, measured from the file.

The pure functions are tested on exact synthetic input; the decode pass is tested on synthetic
video with known cuts, dissolves and camera moves (see media_fixtures). The same detector was
also run on real footage while calibrating it: five hard cuts joined at known times were all
found to the frame, dissolves were found within 0.4 s, nothing fired inside stock, 4K and
AI-generated single shots, and an 85-shot anime opening came out as 85 distinct shots.
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
from classes.media_index import schema as S  # noqa: E402
from classes.media_index import structure as st  # noqa: E402
from classes.media_index.probe import analysis_size, parse_probe, probe_media  # noqa: E402

FPS = S.ANALYSIS_FPS


def texture_gray(seed=1, size=(320, 180)):
    base = mf.scene(seed, (size[0] * 2, size[1] * 2))
    return np.asarray(base.convert("L").resize(size, Image.BICUBIC), np.uint8)


# ================================ probe ================================
def test_the_analysis_frame_keeps_the_display_aspect_with_even_sides():
    assert analysis_size(1920, 1080, 320) == (320, 180)
    assert analysis_size(1080, 1920, 320) == (180, 320)
    assert analysis_size(1000, 1000, 320) == (320, 320)
    assert analysis_size(0, 1080, 320) is None
    w, h = analysis_size(1921, 1081, 320)
    assert w % 2 == 0 and h % 2 == 0


def test_probe_swaps_the_size_of_a_rotated_video_and_flags_hdr():
    data = {"format": {"duration": "12.5", "format_name": "mov"}, "streams": [
        {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "avg_frame_rate": "30000/1001",
         "pix_fmt": "yuv420p10le", "color_transfer": "arib-std-b67", "color_primaries": "bt2020",
         "side_data_list": [{"rotation": -90}]},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}]}
    got = parse_probe(data)
    assert got["ok"] and got["duration"] == 12.5 and got["has_audio"] and got["has_video"]
    v = got["video"]
    assert (v["width"], v["height"]) == (2160, 3840) and v["orientation"] == "portrait"
    assert v["hdr"] is True and v["bit_depth"] == 10 and v["fps"] == pytest.approx(29.97, abs=0.01)
    assert got["audio"]["sample_rate"] == 48000


def test_probe_ignores_cover_art_and_finds_audio_only_files():
    cover = {"codec_type": "video", "codec_name": "mjpeg", "width": 500, "height": 500, "disposition": {"attached_pic": 1}}
    audio = {"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100", "channels": 2}
    got = parse_probe({"format": {"duration": "200"}, "streams": [cover, audio]})
    assert got["has_video"] is False and got["has_audio"] is True and got["duration"] == 200.0
    assert parse_probe({"streams": []})["ok"] is False


def test_probe_of_a_missing_file_reports_instead_of_raising(tmp_path):
    mf.need_ffmpeg()
    got = probe_media(str(tmp_path / "nope.mp4"))
    assert got["ok"] is False and got["error"]


# ================================ grey, features, cuts ================================
def test_grey_uses_rec601_weights_rounded():
    rgb = np.zeros((1, 3, 3), np.uint8)
    rgb[0, 0] = (255, 0, 0)
    rgb[0, 1] = (0, 255, 0)
    rgb[0, 2] = (0, 0, 255)
    assert st.rgb_to_gray(rgb).tolist() == [[76, 150, 29]]


def test_features_are_zero_for_identical_frames_and_large_for_different_ones():
    g1, g2 = texture_gray(1), texture_gray(2)
    h1, h2 = st.gray_hist32(g1), st.gray_hist32(g2)
    assert st.cut_features(g1, g1, h1, h1) == (0.0, 0.0)
    a, b = st.cut_features(g1, g2, h1, h2)
    assert a > 0.05 and b > 0.0


def test_a_clear_jump_is_a_cut_and_ordinary_motion_is_not():
    d = st.CutDetector()
    assert d.is_cut(0.20, 0.60) is True
    assert d.is_cut(0.02, 0.03) is False
    assert d.is_cut(0.30, 0.02) is True, "a huge pixel change is a cut on its own"
    assert d.is_cut(0.04, 0.40) is True, "so is a huge histogram jump"


def test_busy_footage_raises_the_bar_but_a_real_cut_still_clears_it():
    d = st.CutDetector()
    for _ in range(10):
        assert d.is_cut(0.07, 0.10) is False  # fast action, no cuts
    assert d.is_cut(0.12, 0.20) is False, "a flash of extra motion in busy footage is not a cut"
    assert d.is_cut(0.35, 0.60) is True


def test_a_cut_does_not_teach_the_detector_that_the_footage_is_busy():
    d = st.CutDetector()
    for _ in range(5):
        d.is_cut(0.01, 0.01)
    for _ in range(5):
        assert d.is_cut(0.30, 0.70) is True  # five cuts in a row stay cuts


# ================================ dissolves ================================
def window_of(frames_fn, k=st.DISSOLVE_K):
    return [st.small_gray(frames_fn(i)) for i in range(2 * k + 1)]


def test_a_cross_fade_is_recognised_as_a_blend():
    a, b = texture_gray(1), texture_gray(7)
    win = window_of(lambda i: np.rint(a * (1 - i / 12.0) + b * (i / 12.0)).astype(np.uint8))
    ratio, end_diff = st.blend_ratio(win)
    assert ratio is not None and ratio <= st.DISSOLVE_MAX_RATIO and end_diff >= st.DISSOLVE_MIN_END_DIFF


def test_a_pan_is_not_mistaken_for_a_blend():
    base = np.asarray(mf.scene(3, (1280, 360)).convert("L"), np.uint8)
    win = window_of(lambda i: base[:180, i * 40: i * 40 + 320])  # 40 px per frame, a very fast pan
    ratio, _ = st.blend_ratio(win)
    assert ratio is None or ratio > st.DISSOLVE_MAX_RATIO


def test_a_change_in_part_of_the_frame_is_not_a_dissolve():
    """Half the frame changes, clearly enough to pass the end-difference gate, and the fade is a
    perfect blend there: only the area rule keeps it from being called a scene dissolve (an
    overlay fading off a pillarboxed person, a panel filling in a UI)."""
    a = texture_gray(1)
    b = a.copy()
    b[:, :160] = 255 - texture_gray(9)[:, :160]
    win = window_of(lambda i: np.rint(a * (1 - i / 12.0) + b * (i / 12.0)).astype(np.uint8))
    d = (win[-1] - win[0]).ravel()
    assert np.sqrt((d @ d) / d.size) / 255.0 >= st.DISSOLVE_MIN_END_DIFF, "the test must reach the area rule"
    assert st.changed_area(win[0], win[-1]) < st.DISSOLVE_MIN_AREA
    assert st.blend_ratio(win)[0] is None, "a local change must not read as a scene dissolve"


def test_two_similar_pictures_are_not_a_dissolve():
    a = texture_gray(1)
    win = window_of(lambda i: np.clip(a.astype(np.int16) + (i // 6), 0, 255).astype(np.uint8))
    assert st.blend_ratio(win)[0] is None


def test_changed_area_counts_the_cells_that_differ():
    a = np.zeros((180, 320), np.float32)
    b = a.copy()
    b[:90, :] = 200.0
    assert st.changed_area(a, b) == pytest.approx(0.5)
    assert st.changed_area(a, a) == 0.0


# ================================ motion ================================
def test_phase_correlation_finds_a_known_shift_and_its_sign():
    base = np.asarray(mf.scene(4, (400, 300)).convert("L"), np.float32)
    a = base[50:178, 50:178]
    b = base[50:178, 53:181]          # the window moved right 3 px, so content moved LEFT 3 px
    dx, dy, peak = st.phase_shift(a, b)
    assert dx == pytest.approx(-3.0, abs=0.15) and dy == pytest.approx(0.0, abs=0.15) and peak > 0.3
    c = base[54:182, 50:178]          # moved down 4 px: content moved UP
    dx, dy, _ = st.phase_shift(a, c)
    assert dy == pytest.approx(-4.0, abs=0.15) and dx == pytest.approx(0.0, abs=0.15)


def test_phase_correlation_resolves_half_pixels():
    big = mf.scene(4, (800, 600))
    a = np.asarray(big.transform((128, 128), Image.AFFINE, (1, 0, 100.0, 0, 1, 100.0), Image.BICUBIC).convert("L"), np.float32)
    b = np.asarray(big.transform((128, 128), Image.AFFINE, (1, 0, 101.5, 0, 1, 100.0), Image.BICUBIC).convert("L"), np.float32)
    assert st.phase_shift(a, b)[0] == pytest.approx(-1.5, abs=0.2)


def test_zoom_shows_up_as_outward_drift_not_as_a_pan():
    base = mf.scene(5, (1280, 720))
    def view(scale):
        w, h = 640 / scale, 360 / scale
        return np.asarray(base.transform((320, 180), Image.AFFINE, (w / 320, 0, 640 - w / 2, 0, h / 180, 360 - h / 2),
                                         Image.BICUBIC).convert("L"), np.uint8)
    m = st.motion_between(view(1.0), view(1.04))
    assert m is not None and m["zoom"] > 0.3 and abs(m["dx"]) < 0.3 and abs(m["dy"]) < 0.3
    m = st.motion_between(view(1.04), view(1.0))
    assert m["zoom"] < -0.3


def _pairs(n, **kw):
    base = {"diff": 0.05, "dx": 0.0, "dy": 0.0, "zoom": 0.0, "peak": 0.8, "crop": 128.0}
    base.update(kw)
    return [dict(base) for _ in range(n)]


def test_a_still_shot_is_static():
    m = st.classify_motion(_pairs(20, diff=0.002), FPS)
    assert m["class"] == "static" and m["confidence"] >= 0.6


@pytest.mark.parametrize("dx,dy,direction", [(-4.0, 0.0, "right"), (4.0, 0.0, "left"), (0.0, 4.0, "up"), (0.0, -4.0, "down")])
def test_consistent_drift_is_a_pan_or_tilt_with_the_camera_direction(dx, dy, direction):
    # content moves opposite to the camera: content left = camera right
    m = st.classify_motion(_pairs(20, dx=dx, dy=dy), FPS)
    assert m["class"] in ("pan", "tilt") and m["direction"] == direction


def test_outward_drift_is_a_zoom():
    assert st.classify_motion(_pairs(20, zoom=2.0), FPS)["class"] == "zoom_in"
    assert st.classify_motion(_pairs(20, zoom=-2.0), FPS)["class"] == "zoom_out"


def test_random_small_shifts_are_handheld_not_a_pan():
    rng = np.random.default_rng(1)
    pairs = [{"diff": 0.04, "dx": float(rng.normal(0, 1.2)), "dy": float(rng.normal(0, 1.2)), "zoom": 0.0, "peak": 0.8, "crop": 128.0}
             for _ in range(30)]
    m = st.classify_motion(pairs, FPS)
    assert m["class"] == "handheld"


def test_a_scene_that_moves_while_the_camera_does_not_is_busy():
    pairs = _pairs(20, diff=0.08, peak=0.15)
    pairs = [dict(p, dx=0.0) for p in pairs]
    assert st.classify_motion(pairs, FPS)["class"] in ("busy", "static")


def test_too_little_evidence_is_unknown_not_a_guess():
    assert st.classify_motion([], FPS)["class"] == "unknown"
    m = st.classify_motion(_pairs(2, diff=0.05, peak=0.9), FPS)
    assert m["class"] != "pan"


def test_motion_summary_reports_the_numbers_it_used():
    m = st.classify_motion(_pairs(20, dx=-4.0), FPS)
    assert m["pan_speed"] == pytest.approx(-4.0 * FPS / 128.0, abs=0.01)
    assert m["valid_pairs"] == 20 and m["diff_median"] == 0.05


# ================================ shots ================================
def test_boundaries_near_the_edges_or_each_other_are_merged_with_hard_winning():
    raw = [{"t": 0.2, "kind": "hard"}, {"t": 5.0, "kind": "dissolve", "score": 0.9}, {"t": 5.1, "kind": "hard", "score": 3.0},
           {"t": 9.9, "kind": "hard"}, {"t": 12.0, "kind": "fade"}]
    kept = st.merge_boundaries(raw, 10.0)
    assert [(b["t"], b["kind"]) for b in kept] == [(5.1, "hard")], kept


def test_shots_tile_the_file_without_gaps():
    shots = st.build_shots([{"t": 3.0, "kind": "hard", "score": 2.0}, {"t": 5.0, "kind": "dissolve", "score": 0.8}], 9.0)
    assert [(s["start"], s["end"]) for s in shots] == [(0.0, 3.0), (3.0, 5.0), (5.0, 9.0)]
    assert [s["id"] for s in shots] == [0, 1, 2]
    assert shots[0]["opens_with"] is None and shots[1]["opens_with"]["kind"] == "hard"
    assert sum(s["duration"] for s in shots) == pytest.approx(9.0)


def test_a_long_take_is_split_evenly_so_no_record_exceeds_the_cap():
    shots = st.build_shots([], 20.0)
    assert len(shots) == 3 and all(s["duration"] <= S.SHOT_MAX_SECONDS for s in shots)
    assert shots[0]["split"] is False and shots[1]["split"] is True and shots[1]["opens_with"] == {"kind": "split"}
    assert shots[-1]["end"] == 20.0 and shots[0]["start"] == 0.0


def test_a_take_of_exactly_the_cap_is_not_split():
    assert len(st.build_shots([], S.SHOT_MAX_SECONDS)) == 1


# ================================ the decode pass (real ffmpeg) ================================
@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    mf.need_ffmpeg()
    d = tmp_path_factory.mktemp("structure")
    scenes = [mf.scene(s) for s in (11, 12, 13, 14)]
    parts = []
    for i, base in enumerate(scenes):
        parts.append(mf.render(str(d / f"s{i}.mp4"), base, mf.hold(base), 4.0))
    out = {"parts": parts, "dir": d, "scenes": scenes}
    out["hard"] = mf.join_hard(str(d / "hard.mp4"), parts)
    out["dissolve"] = mf.join_dissolve(str(d / "dissolve.mp4"), parts, 4.0)
    return out


def analyse(path):
    return st.analyze_structure(path, probe_media(path))


def test_hard_cuts_are_found_at_the_right_frame_and_nothing_else(clips):
    res = analyse(clips["hard"])
    assert [(b["t"], b["kind"]) for b in res["boundaries"]] == [(4.0, "hard"), (8.0, "hard"), (12.0, "hard")]
    assert [round(s["start"], 1) for s in res["shots"]] == [0.0, 4.0, 8.0, 12.0]


def test_dissolves_are_found_close_to_their_centres(clips):
    res = analyse(clips["dissolve"])
    found = sorted(b["t"] for b in res["boundaries"])
    truth = mf.dissolve_centres(3, 4.0)
    assert len(found) == 3, res["boundaries"]
    for got, want in zip(found, truth):
        assert abs(got - want) <= 0.5, (found, truth)


@pytest.mark.parametrize("name,pose,want,direction", [
    ("static", lambda t: (960.0, 540.0, 1.0), "static", None),
    ("pan_right", lambda t: (700.0 + 115.0 * t, 540.0, 1.0), "pan", "right"),
    ("pan_left", lambda t: (1200.0 - 115.0 * t, 540.0, 1.0), "pan", "left"),
    ("tilt_down", lambda t: (960.0, 450.0 + 96.0 * t, 1.0), "tilt", "down"),
    ("tilt_up", lambda t: (960.0, 650.0 - 96.0 * t, 1.0), "tilt", "up"),
    ("zoom_in", lambda t: (960.0, 540.0, 1.0 + 0.10 * t), "zoom_in", None),
    ("zoom_out", lambda t: (960.0, 540.0, 1.4 - 0.10 * t), "zoom_out", None),
])
def test_camera_motion_is_classified_with_its_direction(clips, name, pose, want, direction):
    path = mf.render(str(clips["dir"] / f"m_{name}.mp4"), clips["scenes"][0], pose, 4.0)
    res = analyse(path)
    assert len(res["shots"]) == 1, res["boundaries"]
    m = res["shots"][0]["motion"]
    assert m["class"] == want and m["direction"] == direction, m


def test_handheld_shake_is_recognised_at_two_strengths_and_noise_alone_is_static(clips):
    base = clips["scenes"][1]
    for std in (1.0, 4.0):
        rng = np.random.default_rng(11)
        walk = np.zeros((120, 2))
        for i in range(1, 120):
            walk[i] = 0.5 * walk[i - 1] + rng.normal(0, std * 0.87, 2)
        walk *= 1.5
        path = mf.render(str(clips["dir"] / f"hh{std}.mp4"), base,
                         lambda t, w=walk: (960.0 + w[min(int(t * 30), 119), 0], 540.0 + w[min(int(t * 30), 119), 1], 1.0), 4.0)
        assert analyse(path)["shots"][0]["motion"]["class"] == "handheld", std
    quiet = mf.render(str(clips["dir"] / "quiet.mp4"), base, mf.hold(base), 4.0, noise=3.0)
    assert analyse(quiet)["shots"][0]["motion"]["class"] == "static"


def test_a_two_frame_flash_inside_a_shot_is_not_a_cut(clips):
    mf.need_ffmpeg()
    src = clips["parts"][0]
    out = str(clips["dir"] / "flash.mp4")
    # frames 55-56 (about 1.83-1.87 s) are white
    import subprocess
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-i", src, "-vf",
                    "drawbox=x=0:y=0:w=iw:h=ih:color=white@1:t=fill:enable='between(n,55,56)'",
                    "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", out], check=True)
    res = analyse(out)
    assert res["boundaries"] == [], res["boundaries"]


def test_a_fade_through_black_is_a_boundary_but_black_at_the_ends_is_not(clips):
    import subprocess
    out = str(clips["dir"] / "fade.mp4")
    # scene 0 (0-4 s) fades out to black, 0.6 s of black, scene 1 fades in; plus black at the very start
    fade_clip = str(clips["dir"] / "fade_core.mp4")
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-i", clips["parts"][0], "-i", clips["parts"][1], "-filter_complex",
                    "[0]trim=0:3,fade=t=out:st=2.4:d=0.6[a];color=c=black:s=640x360:r=30:d=0.7[b];[1]trim=0:3,setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.6[c];"
                    "color=c=black:s=640x360:r=30:d=0.8[z];[z][a][b][c]concat=n=4:v=1:a=0,format=yuv420p",
                    "-c:v", "libx264", "-crf", "14", out], check=True)
    res = analyse(out)
    kinds = [b["kind"] for b in res["boundaries"]]
    assert kinds.count("fade") == 1, res["boundaries"]
    fade_t = next(b["t"] for b in res["boundaries"] if b["kind"] == "fade")
    assert 3.4 <= fade_t <= 4.4, fade_t   # the black gap is at about 3.0-3.7 s after the 0.8 s lead-in
    assert not any(b["kind"] == "fade" and b["t"] < 1.0 for b in res["boundaries"]), "a fade-in from black at the start is not a boundary"
    assert res["shots"][0]["black"] is True and res["shots"][0]["end"] <= 1.0, "the lead-in is one shot, flagged black"
    assert sum(1 for s in res["shots"] if s["black"]) >= 2, "the black gap in the middle is flagged too"
    assert any(not s["black"] for s in res["shots"])


def test_a_long_take_comes_back_as_several_shots_of_at_most_the_cap(clips):
    base = clips["scenes"][2]
    path = mf.render(str(clips["dir"] / "long.mp4"), base, mf.hold(base), 20.0, size=(320, 180))
    res = analyse(path)
    assert res["boundaries"] == []
    assert len(res["shots"]) == 3 and all(s["duration"] <= S.SHOT_MAX_SECONDS for s in res["shots"])
    assert res["shots"][-1]["end"] == pytest.approx(20.0, abs=0.1)


def test_a_portrait_video_is_analysed_at_its_display_shape(clips):
    base = clips["scenes"][3]
    path = mf.render(str(clips["dir"] / "portrait.mp4"), base, mf.hold(base), 2.0, size=(360, 640))
    res = analyse(path)
    assert (res["analysis"]["width"], res["analysis"]["height"]) == (180, 320)


def test_audio_only_input_has_no_structure(clips, tmp_path):
    import subprocess
    wav = str(tmp_path / "tone.wav")
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", wav], check=True)
    assert st.analyze_structure(wav, probe_media(wav)) is None


def test_progress_runs_forward_and_finishes(clips):
    seen = []
    p = clips["hard"]
    st.analyze_structure(p, probe_media(p), on_progress=seen.append)
    assert seen and seen[-1] == 1.0 and all(a <= b for a, b in zip(seen, seen[1:]))


def test_cancelling_stops_the_decode_and_raises(clips):
    p = clips["hard"]
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 5

    with pytest.raises(st.Cancelled):
        st.analyze_structure(p, probe_media(p), should_cancel=cancel)
    assert calls["n"] <= 8, "it must stop promptly, not finish the file"


def test_frames_visited_matches_the_duration(clips):
    res = analyse(clips["hard"])
    assert res["frames"] == pytest.approx(160, abs=3)   # four 4 s scenes at 10 fps
