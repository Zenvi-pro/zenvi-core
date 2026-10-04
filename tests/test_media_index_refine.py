"""The exact frame of a cut, found at the file's own frame rate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from eval import corpus  # noqa: E402
from classes.media_index import refine as R  # noqa: E402
from classes.media_index.probe import probe_media  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def need_ffmpeg():
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")


def cut(path, near, radius=0.5):
    return R.refine_cut(str(path), probe_media(str(path)), near, radius)


# ============================ real clips ============================
def test_a_hard_cut_is_found_to_the_frame_even_when_the_hint_is_a_tenth_off():
    path, truth = corpus.hard_cuts()
    out = cut(path, 4.1)                                       # the index would say 4.0 or 4.1: truth is frame 100 at 25 fps
    assert out["kind"] == "hard" and out["t"] == pytest.approx(4.0, abs=1e-3) and out["frame"] == 100 and out["fps"] == 25.0
    assert out["hint_error"] == pytest.approx(-0.1, abs=1e-3) and out["measured"] is True and out["widened"] is False


def test_every_cut_of_a_clip_is_found_exactly_from_a_rough_hint():
    path, truth = corpus.hard_cuts()
    for exact in truth["hard"]:
        out = cut(path, exact + 0.12)
        assert out["kind"] == "hard" and out["t"] == pytest.approx(exact, abs=1e-3), exact


def test_ntsc_frame_rates_are_exact_too(tmp_path):
    fps = "30000/1001"
    a, b, out = str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4"), str(tmp_path / "ntsc.mp4")
    for path, src in ((a, "testsrc2"), (b, "smptebars")):
        corpus.run("-f", "lavfi", "-i", f"{src}=size=320x180:rate={fps}", "-frames:v", "90", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path)
    corpus.concat([a, b], out)
    result = cut(out, 3.1)
    assert result["kind"] == "hard" and result["frame"] == 90 and result["t"] == pytest.approx(90 * 1001 / 30000, abs=1e-3) and result["fps"] == pytest.approx(29.97, abs=0.01)


def test_a_dissolve_reports_its_whole_span_and_centre():
    path, truth = corpus.dissolves()
    out = cut(path, truth["soft"][0] + 0.1, radius=0.9)
    assert out["kind"] == "dissolve" and out["start"] < out["t"] < out["end"] and out["end"] - out["start"] >= 0.5
    assert out["t"] == pytest.approx(truth["soft"][0], abs=0.25)


def test_a_fade_through_black_is_one_event_at_the_darkest_frame():
    path, truth = corpus.fades_through_black()
    out = cut(path, truth["soft"][0], radius=0.8)
    assert out["kind"] == "fade" and out["t"] == pytest.approx(truth["soft"][0], abs=0.1) and out["evidence"]["darkest_luma"] < 0.08


def test_a_flash_is_not_a_cut_and_a_quiet_stretch_has_no_cut():
    flash, _ = corpus.flash_inside()
    assert cut(flash, 4.0, radius=0.3)["kind"] == "none", "a two-frame flash comes straight back: not a cut"
    busy, _ = corpus.no_cuts_busy()
    out = cut(busy, 6.0)
    assert out["kind"] == "none" and out["widened"] is True and out["hint_error"] is None, "it looked wider before saying so"


def test_a_window_is_clamped_to_the_clip_and_a_bad_file_is_an_error(tmp_path):
    path, truth = corpus.hard_cuts()
    assert cut(path, 0.05)["window"][0] == 0.0
    with pytest.raises(RuntimeError):
        R.decode_window(str(tmp_path / "missing.mp4"), 0, 1, (160, 90))


# ============================ the maths on synthetic frames ============================
def frames_with_cut(at, n=12, h=36, w=64):
    rng = np.random.default_rng(1)
    a, b = rng.integers(20, 90, (h, w)), rng.integers(150, 230, (h, w))
    return np.stack([(a if i < at else b).astype(np.uint8) for i in range(n)]), [i / 25.0 for i in range(n)]


def test_the_step_between_two_different_pictures_is_the_one_that_scores_over_one():
    frames, times = frames_with_cut(6)
    scores = R.step_scores(frames)
    assert len(scores) == 11 and max(range(11), key=lambda k: scores[k]["score"]) == 5 and scores[5]["score"] > 1.0 and max(s["score"] for k, s in enumerate(scores) if k != 5) < 0.1


def test_the_new_shot_starts_at_the_frame_after_the_biggest_step():
    frames, times = frames_with_cut(6)
    out = R.classify(frames, times)
    assert out["kind"] == "hard" and out["frame_index"] == 6 and out["t"] == times[6]


def test_too_few_frames_is_not_judged():
    frames, times = frames_with_cut(1, n=2)
    assert R.classify(frames, times)["kind"] == "none"


def test_the_longest_run_of_consecutive_steps():
    assert R._longest_run([1, 2, 3, 7, 8]) == [1, 2, 3] and R._longest_run([]) == [] and R._longest_run([4]) == [4]


def test_a_decoder_can_be_swapped_in_and_the_hint_error_is_reported():
    frames, times = frames_with_cut(6)
    probe = {"video": {"width": 64, "height": 36, "fps": 25.0}, "duration": 12.0}
    out = R.refine_cut("x", probe, 0.3, 0.5, decode=lambda *a, **k: (frames, [t + 0.0 for t in times]))
    assert out["kind"] == "hard" and out["t"] == pytest.approx(6 / 25.0) and out["frame"] == 6 and out["hint_error"] == pytest.approx(-0.06, abs=1e-3)


def test_the_radius_is_kept_within_bounds():
    calls = []
    frames, times = frames_with_cut(6)

    def decode(path, lo, hi, size, **k):
        calls.append(hi - lo)
        return frames, times

    R.refine_cut("x", {"video": {"width": 64, "height": 36, "fps": 25.0}, "duration": 100.0}, 50.0, 99.0, decode=decode)
    assert calls[0] <= 2 * R.MAX_RADIUS + 1e-6
    R.refine_cut("x", {"video": {"width": 64, "height": 36, "fps": 25.0}, "duration": 100.0}, 50.0, 0.0, decode=decode)
    assert calls[1] >= 0.2 - 1e-6


# ============================ the tool ============================
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402


def call(**kw):
    out = REGISTRY["refine_cut_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


@pytest.fixture
def library(monkeypatch, tmp_path):
    from classes.editor_tools import media_index_tools_precision as P
    path, truth = corpus.hard_cuts()
    shelf = Shelf(str(tmp_path / "shelf"))
    sha = "d" * 64
    shelf.write_json(sha, "structure.json", {"shots": [{"id": i, "start": 4.0 * i, "end": 4.0 * (i + 1)} for i in range(6)]})
    f = SimpleNamespace(id="V1", data={"name": "cuts.mp4", "path": str(path), "media_type": "video", "fingerprint": {"sha256": sha}})
    song = SimpleNamespace(id="S1", data={"name": "song.wav", "path": "/m/song.wav", "media_type": "audio", "fingerprint": None})
    monkeypatch.setattr(P, "default_shelf", lambda: shelf)
    monkeypatch.setattr(P, "resolve_files", lambda ids=None, query="": [x for x in (f, song) if x.id in (ids or [])])
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    return SimpleNamespace(file=f, shelf=shelf, sha=sha)


def test_the_tool_names_the_exact_frame_and_how_far_off_the_hint_was(library):
    head, r = call(file_ids=["V1"], near_seconds=8.1)
    assert r["cut"]["kind"] == "hard" and r["cut"]["frame"] == 200 and r["cut"]["t"] == pytest.approx(8.0, abs=1e-3) and r["cached"] if "cached" in r else True
    assert head == "The cut is at 8.000 s (frame 200 at 25 fps); the hint was 0.10 s late." and r["changed"] is False and r["cut"]["cached"] is False


def test_asking_again_is_answered_from_the_shelf_without_decoding(library, monkeypatch):
    call(file_ids=["V1"], near_seconds=8.1)
    from classes.media_index import refine
    monkeypatch.setattr(refine, "decode_window", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not decode again")))
    monkeypatch.setattr(refine, "refine_cut", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not refine again")))
    _, r = call(file_ids=["V1"], near_seconds=8.1)
    assert r["cut"]["cached"] is True and r["cut"]["frame"] == 200


def test_a_shot_gets_both_of_its_edges_exact_and_the_file_ends_are_not_searched(library):
    head, r = call(file_ids=["V1"], shot_id=2)
    assert r["edges"]["start"]["frame"] == 200 and r["edges"]["end"]["frame"] == 300 and "start 8.000 s (frame 200)" in head and "end 12.000 s (frame 300)" in head
    _, first = call(file_ids=["V1"], shot_id=0)
    assert first["edges"]["start"]["kind"] == "file_edge" and first["edges"]["end"]["frame"] == 100
    _, last = call(file_ids=["V1"], shot_id=5)
    assert last["edges"]["end"]["kind"] == "file_edge"


def test_no_cut_where_the_hint_points_is_said_plainly(library):
    head, r = call(file_ids=["V1"], near_seconds=2.0)
    assert head.startswith("No cut near 2.00 s") and r["cut"]["kind"] == "none"


@pytest.mark.parametrize("kw,fragment", [(dict(file_ids=["V1"]), "near_seconds"), (dict(file_ids=["V1"], shot_id=99), "no shot 99"), (dict(file_ids=["S1"], near_seconds=1.0), "only video")])
def test_what_it_cannot_do_is_refused(library, kw, fragment):
    assert fragment in REGISTRY["refine_cut_tool"].func(**kw)


def test_a_flash_cut_off_by_the_edge_of_the_window_is_still_not_a_cut():
    flash, _ = corpus.flash_inside()                # the flash is frames 100-101 (4.00-4.04 s); this window starts inside it
    out = cut(flash, 5.0, radius=0.5)
    assert out["kind"] == "none" and out["widened"] is True


def test_a_real_cut_near_the_edge_of_the_window_is_still_found_exactly():
    path, truth = corpus.hard_cuts()
    out = cut(path, 4.45, radius=0.5)               # the cut at 4.0 is 0.05 s from the window's start
    assert out["kind"] == "hard" and out["frame"] == 100
