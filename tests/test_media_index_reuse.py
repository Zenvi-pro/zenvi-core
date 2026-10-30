"""Saved layers are reused only for the same footage: the same kind of media and the same length."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index.reuse import durations_match, same_footage  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

FP = "c" * 64


@pytest.mark.parametrize("saved,current,kind,expected", [
    (30, 30, "video", True), (30, 30.4, "video", True), (3600, 3630, "video", True),
    (3600, 3700, "video", False), (30, 0, "video", False), (0, 30, "video", False),
    ("x", 30, "video", False), (30, None, "audio", False), (0, 0, "image", True),
])
def test_duration_rule(saved, current, kind, expected):
    assert durations_match(saved, current, kind) is expected


# -- two files that share a fingerprint must not share layers ---------------------------
def test_layers_made_from_other_footage_are_not_the_same_footage(tmp_path):
    s = Shelf(str(tmp_path / "idx"))
    s.set_source(FP, media_type="video", duration=30.0)
    assert same_footage(s, FP, media_type="video", duration=30.2)
    assert not same_footage(s, FP, media_type="video", duration=75.0)
    assert not same_footage(s, FP, media_type="audio", duration=30.0)


def test_nothing_saved_yet_never_holds_back_a_first_analysis(tmp_path):
    assert same_footage(Shelf(str(tmp_path / "idx")), FP, media_type="video", duration=30.0)
