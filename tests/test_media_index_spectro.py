"""Spectrogram strips: real ffmpeg on a known tone, caching, and the limits."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

import media_fixtures as mf  # noqa: E402
from classes.media_index import spectro  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

SHA = "d" * 64


@pytest.fixture
def shelf(tmp_path):
    return Shelf(str(tmp_path / "s"))


@pytest.fixture
def tone(tmp_path):
    """3 s of 440 Hz, then 3 s of 3000 Hz."""
    ff = mf.need_ffmpeg()
    path = str(tmp_path / "tones.wav")
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-f", "lavfi", "-i",
                    "sine=frequency=3000:duration=3", "-filter_complex", "[0][1]concat=n=2:v=0:a=1", path], check=True)
    return path


def test_a_strip_is_drawn_saved_on_the_shelf_and_readable(tone, shelf):
    out = spectro.make_spectrogram(tone, SHA, shelf, 0.0, 6.0, 6.0)
    assert out["ok"] and out["cached"] is False and out["path"].endswith("spectro_0_6000.png")
    img = Image.open(out["path"])
    w, h = img.size
    assert w > spectro.SIZE[0] and h > spectro.SIZE[1], "axes and the dBFS legend are drawn around the plot"
    px = np.asarray(img.convert("RGB")).astype(int)
    # The plot (SIZE) sits inside margins of about 143 px left and 63 px top. The 440 Hz line is in its left
    # half and the 3 kHz line in its right half; frequency is logarithmic, low at the bottom.
    x0, y0 = 143, 63
    plot = px[y0 + 6:y0 + spectro.SIZE[1] - 6, x0 + 6:x0 + spectro.SIZE[0] - 6]      # inside the frame line
    energy = plot.sum(axis=2)
    row_left = int(np.argmax(energy[:, 100:500].mean(axis=1)))        # the strongest horizontal line in each half
    row_right = int(np.argmax(energy[:, 900:1300].mean(axis=1)))
    assert row_left > row_right + 40, f"the lower tone must sit lower in the picture ({row_left} vs {row_right})"
    # On the log axis (20 Hz to 11 kHz) 440 Hz -> 3 kHz is log(3000/440)/log(11025/20) of the height (~128 px); a
    # linear axis would put them ~98 px apart.
    import math
    expected = math.log(3000 / 440) / math.log(11025 / 20) * spectro.SIZE[1]
    assert abs((row_left - row_right) - expected) < 12, f"expected ~{expected:.0f} px apart, got {row_left - row_right}"
    assert energy[row_left, 100:500].mean() > 2 * energy[:, 100:500].mean(), "and it is a clear line, not noise"


def test_asking_again_is_free(tone, shelf, monkeypatch):
    first = spectro.make_spectrogram(tone, SHA, shelf, 0.0, 3.0, 6.0)
    monkeypatch.setattr(spectro, "run_ffmpeg", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run again")))
    again = spectro.make_spectrogram(tone, SHA, shelf, 0.0, 3.0, 6.0)
    assert again["ok"] and again["cached"] is True and again["path"] == first["path"]


def test_a_range_is_cut_from_the_file(tone, shelf):
    a = spectro.make_spectrogram(tone, SHA, shelf, 0.0, 3.0, 6.0)
    b = spectro.make_spectrogram(tone, SHA, shelf, 3.0, 6.0, 6.0)
    assert a["path"] != b["path"] and open(a["path"], "rb").read() != open(b["path"], "rb").read()


@pytest.mark.parametrize("start,end,fragment", [(0.0, 0.2, "too short"), (0.0, 200.0, "at most 90"), (5.9, 6.0, "too short")])
def test_unreasonable_ranges_are_refused_with_what_to_ask_instead(tone, shelf, start, end, fragment):
    out = spectro.make_spectrogram(tone, SHA, shelf, start, end, 400.0 if end > 100 else 6.0)
    assert out["ok"] is False and fragment in out["error"]
    assert not any(n.endswith(".png") for n in os.listdir(shelf.entry_dir(SHA, create=True)))


def test_a_file_without_audio_is_refused(shelf):
    out = spectro.make_spectrogram("/nowhere.mp4", SHA, shelf, 0.0, 5.0, 5.0, has_audio=False)
    assert out == {"ok": False, "error": "this file has no audio track"}


def test_a_decoder_failure_is_reported_not_raised_and_leaves_nothing(tmp_path, shelf):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio" * 100)
    out = spectro.make_spectrogram(str(bad), SHA, shelf, 0.0, 3.0, 6.0)
    assert out["ok"] is False and "could not draw" in out["error"]
    assert [n for n in os.listdir(shelf.entry_dir(SHA)) if n.endswith(".png")] == []
