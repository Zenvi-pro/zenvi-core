"""Frames of a range as one readable picture, stamped with exact times."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index import contact as C  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from eval import corpus  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def need_ffmpeg():
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")


# ============================ the stamp and the picture ============================
def test_a_stamp_puts_white_text_on_a_black_box_and_leaves_the_rest_alone():
    img = np.full((40, 120, 3), 90, np.uint8)
    C.stamp(img, "t=1.5", 4, 4, 2)
    box = img[4:4 + 18, 4:4 + (5 * 6 + 1) * 2]
    assert (box == 255).any() and (box == 0).any() and set(np.unique(box)) == {0, 255}
    assert (img[30:, :] == 90).all() and (img[:, 100:] == 90).all()


def test_a_stamp_is_clipped_to_the_picture_instead_of_failing():
    img = np.full((10, 10, 3), 50, np.uint8)
    C.stamp(img, "t=123.456", 6, 6, 2)
    C.stamp(img, "t=1", -5, -5, 2)
    C.stamp(img, "t=1", 50, 50, 2)
    assert img.shape == (10, 10, 3)


def test_every_character_a_stamp_uses_has_a_shape():
    for ch in "0123456789.:= -tfs":
        glyph = C._GLYPHS[ch]
        assert len(glyph) == 7 and all(len(row) == 5 for row in glyph), ch
    assert any("1" in row for row in C._GLYPHS["8"]) and not any("1" in row for row in C._GLYPHS[" "])


def test_the_png_is_a_valid_picture_with_the_pixels_given(tmp_path):
    from PIL import Image
    rgb = np.random.default_rng(1).integers(0, 255, (24, 40, 3), dtype=np.uint8)
    path = tmp_path / "x.png"
    C.write_png(str(path), rgb)
    back = np.asarray(Image.open(path).convert("RGB"))
    assert back.shape == (24, 40, 3) and (back == rgb).all()


# ============================ how many tiles ============================
@pytest.mark.parametrize("kw,expected", [(dict(rate=1.0), (1.0, 10)), (dict(rate=5.0), (5.0, 48 if False else 50)), (dict(count=12), (1.2, 12)), (dict(), (1.0, 10)),
                                         (dict(every_frame=True, fps=25.0), (None, 250))])
def test_the_sampling_follows_what_was_asked_for_up_to_the_limit(kw, expected):
    if expected[1] > C.MAX_TILES:
        with pytest.raises(ValueError, match="tiles"):
            C.choose_times(0.0, 10.0, **kw)
    else:
        assert C.choose_times(0.0, 10.0, **kw) == expected


def test_every_frame_of_a_short_range_is_allowed_and_says_how_to_narrow_a_long_one():
    assert C.choose_times(2.0, 3.5, every_frame=True, fps=25.0) == (None, 38)
    with pytest.raises(ValueError, match=r"narrow it to 1\.92 s"):
        C.choose_times(0.0, 5.0, every_frame=True, fps=25.0)
    with pytest.raises(ValueError, match="count must be"):
        C.choose_times(0.0, 5.0, count=99)


def test_the_sheet_is_laid_out_in_columns_within_the_width_budget():
    assert C.columns_for(320, 40) == 6 and C.columns_for(96, 40) == 8 and C.columns_for(640, 40) == 3 and C.columns_for(320, 2) == 2


# ============================ real frames ============================
def test_decoding_at_a_rate_gives_the_frames_nearest_those_moments_with_their_real_times():
    path, _ = corpus.hard_cuts()
    frames, times = C.decode_frames(str(path), 1.0, 4.0, (160, 90), rate=2.0)
    assert frames.shape[1:] == (90, 160, 3) and 5 <= len(times) <= 7 and times == sorted(times) and 0.9 <= times[0] <= 1.6 and times[-1] <= 4.0


def test_decoding_every_frame_gives_exactly_the_files_own_frames():
    path, _ = corpus.hard_cuts()
    frames, times = C.decode_frames(str(path), 3.9, 4.1, (160, 90))
    assert len(times) == 5 and times == pytest.approx([3.92, 3.96, 4.0, 4.04, 4.08], abs=1e-3), "25 fps: 40 ms apart, on the file's own frames"
    assert not np.array_equal(frames[1], frames[2]), "the cut is between the 4th and 5th of these (3.96 and 4.00)"
    assert np.abs(frames[0].astype(int) - frames[1].astype(int)).mean() < 20 and np.abs(frames[1].astype(int) - frames[2].astype(int)).mean() > 20


def test_a_bad_file_is_an_error(tmp_path):
    with pytest.raises(RuntimeError):
        C.decode_frames(str(tmp_path / "nope.mp4"), 0, 1, (160, 90))


# ============================ the tool ============================
def call(**kw):
    out = REGISTRY["view_frames_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


@pytest.fixture
def library(monkeypatch, tmp_path):
    from classes.editor_tools import media_index_tools_precision as P
    path, _ = corpus.hard_cuts()
    shelf = Shelf(str(tmp_path / "shelf"))
    video = SimpleNamespace(id="V1", data={"name": "cuts.mp4", "path": str(path), "media_type": "video", "fingerprint": {"sha256": "f" * 64}})
    nofp = SimpleNamespace(id="V2", data={"name": "new.mp4", "path": str(path), "media_type": "video", "fingerprint": None})
    song = SimpleNamespace(id="S1", data={"name": "song.wav", "path": "/m/song.wav", "media_type": "audio", "fingerprint": None})
    monkeypatch.setattr(P, "default_shelf", lambda: shelf)
    monkeypatch.setattr(P, "resolve_files", lambda ids=None, query="": [x for x in (video, nofp, song) if x.id in (ids or [])])
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    return SimpleNamespace(shelf=shelf)


def test_a_wide_look_gives_a_readable_picture_and_the_exact_time_and_frame_of_each_tile(library):
    from PIL import Image
    head, r = call(file_ids=["V1"], start_seconds=2.0, end_seconds=10.0)
    assert r["changed"] is False and len(r["tiles"]) == 8 and r["tile_size"] == [320, 180] and r["fps"] == 25.0 and r["cached"] is False
    assert all(t["frame"] == round(t["t"] * 25) for t in r["tiles"]) and head == "8 frames of cuts.mp4, 2.00 to 10.00 s."
    img = Image.open(r["image_path"])
    assert img.size == (320 * 6, 180 * 2) and "every tile is stamped" in r["hint"]


def test_a_close_look_at_a_cut_shows_every_frame_and_the_stamps_are_on_the_picture(library):
    from PIL import Image
    _, r = call(file_ids=["V1"], start_seconds=3.9, end_seconds=4.2, every_frame=True, tile_width=160)
    assert [t["frame"] for t in r["tiles"]] == [98, 99, 100, 101, 102, 103, 104], "the range is [3.9, 4.2): the frame at 4.20 belongs to the next stretch"
    arr = np.asarray(Image.open(r["image_path"]).convert("RGB"))
    tile = arr[:90, :160]
    assert (tile[70:, :] == 255).any() and (tile[70:, :] == 0).any(), "the time and frame are burned into the bottom of the tile"


def test_asking_again_is_answered_from_the_shelf_and_a_file_without_a_fingerprint_still_works(library, monkeypatch):
    call(file_ids=["V1"], start_seconds=2.0, end_seconds=6.0)
    real = C.decode_frames
    monkeypatch.setattr(C, "decode_frames", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not decode again")))
    _, again = call(file_ids=["V1"], start_seconds=2.0, end_seconds=6.0)
    assert again["cached"] is True and len(again["tiles"]) == 4
    monkeypatch.setattr(C, "decode_frames", real)
    _, loose = call(file_ids=["V2"], start_seconds=2.0, end_seconds=4.0)
    assert Path(loose["image_path"]).is_file() and loose["cached"] is False


def test_the_range_and_the_rate_are_held_to_the_limits_with_advice(library):
    assert "narrow it to" in REGISTRY["view_frames_tool"].func(file_ids=["V1"], start_seconds=0.0, end_seconds=5.0, every_frame=True)
    assert "lower the rate" in REGISTRY["view_frames_tool"].func(file_ids=["V1"], start_seconds=0.0, end_seconds=20.0, per_second=5)
    assert "at least a frame" in REGISTRY["view_frames_tool"].func(file_ids=["V1"], start_seconds=2.0, end_seconds=2.01)
    assert "outside the file" in REGISTRY["view_frames_tool"].func(file_ids=["V1"], start_seconds=500.0, end_seconds=510.0)
    assert "only video" in REGISTRY["view_frames_tool"].func(file_ids=["S1"], start_seconds=0.0, end_seconds=4.0)


def test_the_range_is_clamped_to_the_clip(library):
    _, r = call(file_ids=["V1"], start_seconds=20.0, end_seconds=40.0, per_second=1)
    assert 3 <= len(r["tiles"]) <= 5 and r["tiles"][-1]["t"] <= 24.0
