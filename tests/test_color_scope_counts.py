"""FrameScope reports clipped shadows/highlights as pixel COUNTS; the colour maths needs fractions.

Measured on libopenshot 1.0.0 (640x360 = 230400 px): clipped_shadows=3836, clipped_highlights=0
for one frame, 2675 and 34 for another. Phase 6 fed those straight into the look distance, the
grade solver and the over-grade check, which all read them as 0..1 fractions: two ordinary clips
came out 728 apart ("matched" is < 0.045) and the solver always asked for highlights -0.3 and
shadows -0.3. The unit tests passed because their fixtures already used fractions.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import color_agent as ca  # noqa: E402

PIXELS = 640 * 360


def _hist(total=PIXELS):
    """A flat 256-bin histogram summing to *total* pixels (the shape FrameScope returns)."""
    each, extra = divmod(int(total), 256)
    return [each + (1 if i < extra else 0) for i in range(256)]


def _video(avg_luma, shadows, highlights, rgb=(0.3, 0.3, 0.3), total=PIXELS):
    hist = _hist(total)
    return {
        "present": True,
        "histogram": {"luma": hist, "red": hist, "green": hist, "blue": hist},
        "summary": {"avg_luma": avg_luma, "clipped_shadows": shadows, "clipped_highlights": highlights},
    }


# -- the conversion ---------------------------------------------------------------
def test_a_pixel_count_becomes_a_fraction_of_the_frame():
    got = ca.summarize_scope_video(_video(0.2862, 3836, 0))
    assert got["clipped_shadows"] == pytest.approx(3836 / PIXELS)
    assert got["clipped_highlights"] == 0.0


def test_a_value_that_is_already_a_fraction_is_left_alone():
    got = ca.summarize_scope_video(_video(0.3, 0.0167, 0.0004))
    assert got["clipped_shadows"] == pytest.approx(0.0167)
    assert got["clipped_highlights"] == pytest.approx(0.0004)


def test_a_fully_clipped_frame_is_one_not_more():
    got = ca.summarize_scope_video(_video(1.0, 0, PIXELS))
    assert got["clipped_highlights"] == 1.0


def test_a_single_clipped_pixel_is_a_tiny_fraction_not_everything():
    got = ca.summarize_scope_video(_video(0.5, 1.0, 1))
    assert got["clipped_shadows"] == pytest.approx(1 / PIXELS)
    assert got["clipped_highlights"] == pytest.approx(1 / PIXELS)


@pytest.mark.parametrize("raw,pixels,expected", [
    (None, PIXELS, None), ("x", PIXELS, None), (0, PIXELS, 0.0), (-5, PIXELS, 0.0),
    (500.0, PIXELS, 500.0 / PIXELS), (0.25, PIXELS, 0.25),
    (500.0, 0.0, 1.0),  # no histogram to divide by: still clamped, never an unbounded count
    (0.25, 0.0, 0.25),
])
def test_clipped_fraction_edge_cases(raw, pixels, expected):
    got = ca.clipped_fraction(raw, pixels)
    assert got == expected if expected is None else got == pytest.approx(expected)


def test_a_portrait_clip_in_a_landscape_canvas_reads_its_bars_as_a_big_fraction():
    # 157331 of 230400 pixels are the black bars: 68%, a fraction, not a number in the hundred-thousands.
    got = ca.summarize_scope_video(_video(0.1457, 157331, 544))
    assert got["clipped_shadows"] == pytest.approx(0.683, abs=0.001)


# -- what the rest of the maths does with it ------------------------------------------
@pytest.fixture
def two_clips():
    """The two real frames measured above (numbers copied from libopenshot's output)."""
    a = ca.build_look_profile(ca.scope_from_raw_video(_video(0.2862, 3836, 0)))
    b = ca.build_look_profile(ca.scope_from_raw_video(_video(0.1705, 2675, 34)))
    return a, b


def test_two_ordinary_clips_are_close_not_hundreds_apart(two_clips):
    a, b = two_clips
    assert ca.look_profile_distance(a, a) == 0.0
    assert 0.0 < ca.look_profile_distance(a, b) < 2.0


def test_the_solver_does_not_chase_a_pixel_count_difference(two_clips):
    a, b = two_clips
    patch = ca.solve_grade_from_profiles(a, b)
    assert "exposure" in patch  # the real difference (luma) is still acted on
    assert "highlights" not in patch and "shadows" not in patch, patch


def test_the_over_grade_check_is_not_tripped_by_a_few_pixels(two_clips):
    a, b = two_clips
    after = ca.build_look_profile(ca.scope_from_raw_video(_video(0.1705, 2700, 40)))
    verdict = ca.assess_grade_outcome(b, after, b)
    assert verdict["nuke_risk"] is False, verdict["reasons"]


def test_a_real_highlight_blowout_is_still_caught():
    before = ca.build_look_profile(ca.scope_from_raw_video(_video(0.5, 100, 200)))
    blown = ca.build_look_profile(ca.scope_from_raw_video(_video(0.7, 100, int(PIXELS * 0.20))))
    verdict = ca.assess_grade_outcome(before, blown, before)
    assert verdict["nuke_risk"] is True
    assert "clipped_highlights_spike" in verdict["reasons"]


# -- against the real libopenshot ------------------------------------------------------
def _ffmpeg():
    from classes.ffmpeg_cli import find_ffmpeg

    return find_ffmpeg("ffmpeg") or shutil.which("ffmpeg")


def test_real_framescope_clipped_values_come_out_as_fractions(tmp_path):
    openshot = pytest.importorskip("openshot")
    if not hasattr(openshot, "FrameScope"):
        pytest.skip("needs the real libopenshot module (ZENVI_REAL_QT=1 and openshot on PYTHONPATH)")
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        pytest.skip("needs an ffmpeg binary to make the test clip")
    clip = tmp_path / "bars.mp4"
    # SMPTE colour bars: real pure-black and pure-white areas, so clipping is genuinely present.
    subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", "smptebars=size=640x360:rate=30:duration=1",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    timeline = openshot.Timeline(640, 360, openshot.Fraction(30, 1), 48000, 2, 3)
    c = openshot.Clip(str(clip))
    c.Position(0.0)
    timeline.AddClip(c)
    timeline.Open()
    try:
        scope = openshot.FrameScope()
        scope.SetFrame(timeline.GetFrame(5))
        luma = list(scope.GetVideoHistogramLuma())
        video = {"present": True,
                 "histogram": {"luma": luma, "red": list(scope.GetVideoHistogramRed()),
                               "green": list(scope.GetVideoHistogramGreen()),
                               "blue": list(scope.GetVideoHistogramBlue())},
                 "summary": {"avg_luma": scope.GetVideoAverageLuma(),
                             "clipped_shadows": scope.GetVideoClippedShadows(),
                             "clipped_highlights": scope.GetVideoClippedHighlights()}}
    finally:
        timeline.Close()
    got = ca.summarize_scope_video(video)
    assert 0.0 <= got["clipped_shadows"] <= 1.0
    assert 0.0 <= got["clipped_highlights"] <= 1.0
    assert got["clipped_shadows"] > 0.0 or got["clipped_highlights"] > 0.0, "bars contain clipped areas"
    profile = ca.build_look_profile(ca.scope_from_raw_video(video))
    assert ca.look_profile_distance(profile, profile) == 0.0
