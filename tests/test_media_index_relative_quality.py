"""Shake and black are judged against the file's own shots, not against fixed numbers tuned on one clip."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index import quality as Q, structure as st  # noqa: E402


def shot(i, jitter, valid=20, typical=None):
    motion = {"valid_pairs": valid, "jitter_px": jitter}
    if typical is not None:
        motion["typical_jitter_px"] = typical
    return {"id": i, "start": float(i), "end": float(i + 1), "black": False, "motion": motion,
            "sharpness": 0.5, "look": None, "watch": None}


def shaky(s):
    return "shaky" in Q.shot_quality(s, median_sharpness=0.5, audio=None, watch=None)["flags"]


def test_in_an_all_handheld_file_only_the_much_shakier_shot_is_shaky():
    shots = [shot(i, j, typical=1.1) for i, j in enumerate([1.0, 1.1, 1.2, 1.05, 3.0, 1.15])]
    assert [shaky(s) for s in shots] == [False, False, False, False, True, False]


def test_a_steady_file_flags_only_its_shaky_shot_and_tiny_jitter_never_is():
    shots = [shot(i, j, typical=0.07) for i, j in enumerate([0.05, 0.08, 0.04, 1.0, 0.06])]
    assert [shaky(s) for s in shots] == [False, False, False, True, False]
    tripod = [shot(i, j, typical=0.08) for i, j in enumerate([0.05, 0.08, 0.12, 0.2])]
    assert not any(shaky(s) for s in tripod), "0.2 px is 2.5x the typical 0.08 px but far under the absolute floor"
    assert shaky(shot(0, 1.5, typical=0.0)) and not shaky(shot(0, 0.3, typical=0.0)), "a one-shot file has no siblings: the floor decides"


def test_typical_jitter_is_the_median_of_the_other_measured_shots():
    shots = [shot(i, j) for i, j in enumerate([1.0, 1.2, 3.0, 0.9])]
    shots.append(shot(4, 0.1, valid=2))   # too few frame pairs to measure: it is not a reference for the others
    st.typical_jitter(shots)
    assert [s["motion"]["typical_jitter_px"] for s in shots] == [1.2, 1.0, 1.0, 1.2, 1.1]
    one = [shot(0, 1.5)]
    st.typical_jitter(one)
    assert one[0]["motion"]["typical_jitter_px"] == 0.0


def test_a_night_file_is_not_all_black():
    darks = [0.03, 0.035, 0.04, 0.045, 0.032, 0.038]     # every shot is under the 0.05 ceiling, none is a black frame
    assert sum(st.black_flags(darks)) <= 1, st.black_flags(darks)


def test_a_real_black_frame_in_a_bright_file_is_black():
    assert st.black_flags([0.002, 0.4, 0.35, 0.38, 0.33]) == [True, False, False, False, False]
