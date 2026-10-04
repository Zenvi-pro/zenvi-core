"""Which shots are worth using: measured sharpness, shake, exposure and audio trouble; takes and scenes."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageFilter

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

import media_fixtures as mf  # noqa: E402
from classes.media_index import library, quality as Q, structure  # noqa: E402
from classes.media_index.probe import probe_media  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402


# ============================ sharpness ============================
def grey(seed, blur=0.0, noise=0.0):
    im = mf.scene(seed, (1920, 1080)).convert("L").resize((320, 180), Image.LANCZOS)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(im).astype(np.float32)
    if noise:
        a = np.clip(a + np.random.default_rng(0).normal(0, noise, a.shape), 0, 255)
    return a


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_sharpness_falls_steadily_as_a_scene_is_blurred(seed):
    values = [Q.frame_sharpness(grey(seed, blur)) for blur in (0, 0.7, 1.5, 3, 6)]
    assert all(a > b for a, b in zip(values, values[1:])), values
    assert values[-1] / values[0] < 0.45, "a heavy blur is clearly softer than the sharp scene"


def test_mild_sensor_noise_does_not_hide_a_real_blur():
    sharp = Q.frame_sharpness(grey(1))
    noisy_blur = Q.frame_sharpness(grey(1, blur=3, noise=6))
    assert noisy_blur < 0.75 * sharp, (noisy_blur, sharp)


def test_flat_and_tiny_frames_have_no_sharpness():
    assert Q.frame_sharpness(np.full((90, 160), 128, np.uint8)) == 0.0
    assert Q.frame_sharpness(np.zeros((4, 4))) == 0.0
    assert Q.frame_sharpness(np.zeros((90, 160, 3))) == 0.0


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    mf.need_ffmpeg()
    d = tmp_path_factory.mktemp("quality")
    base = mf.scene(3)
    rng = np.random.default_rng(7)
    cache = {}

    def jitter(t):
        k = int(round(t * 30))
        cache.setdefault(k, (rng.uniform(-10, 10), rng.uniform(-10, 10)))
        return base.width / 2 + cache[k][0], base.height / 2 + cache[k][1], 1.0

    spec = {"sharp": (base, mf.hold(base)), "blurred": (base.filter(ImageFilter.GaussianBlur(8)), None), "shaky": (base, jitter),
            "pan": (base, lambda t: (base.width / 2 + 40 * t, base.height / 2, 1.0))}
    out = {}
    for name, (img, pose) in spec.items():
        path = str(d / f"{name}.mp4")
        mf.render(path, img, pose or mf.hold(img), 4.0)
        out[name] = structure.analyze_structure(path, probe_media(path))["shots"][0]
    return out


def test_real_clips_measure_sharper_when_sharp(clips):
    assert clips["sharp"]["sharpness"] > clips["blurred"]["sharpness"] * 1.5
    assert clips["blurred"]["sharpness"] < Q.ABS_BLURRY + 0.05


def test_shake_is_jitter_not_deliberate_camera_moves(clips):
    assert Q.shake_score(clips["shaky"]["motion"]) == 1.0
    assert Q.shake_score(clips["pan"]["motion"]) < 0.25, "a smooth pan is not shake"
    assert Q.shake_score(clips["sharp"]["motion"]) < 0.05


def test_too_few_measured_frame_pairs_means_unknown_shake():
    assert Q.shake_score({"valid_pairs": 2, "jitter_px": 5.0}) is None
    assert Q.shake_score(None) is None and Q.shake_score({}) is None


def test_the_shot_quality_block_flags_what_is_wrong(clips):
    good = Q.shot_quality({**clips["sharp"], "look": None}, median_sharpness=clips["sharp"]["sharpness"], audio=None, watch=None)
    bad = Q.shot_quality({**clips["blurred"], "look": None}, median_sharpness=clips["sharp"]["sharpness"], audio=None, watch=None)
    shaky = Q.shot_quality({**clips["shaky"], "look": None}, median_sharpness=clips["sharp"]["sharpness"], audio=None, watch=None)
    assert good["flags"] == [] and good["score"] == 1.0
    assert set(bad["flags"]) & {"soft", "blurry"} and bad["score"] < good["score"]      # on the soft/blurry boundary: the penalty grows with the margin
    assert "shaky" in shaky["flags"] and shaky["score"] <= 0.65
    assert good["score"] > bad["score"] and good["score"] > shaky["score"]


# ============================ exposure and audio trouble ============================
def prof(luma=0.5, hi=0.0, lo=0.0, contrast=0.5):
    return {"present": True, "avg_luma": luma, "clipped_highlights": hi, "clipped_shadows": lo, "contrast_span": contrast}


@pytest.mark.parametrize("profile,problems", [
    (prof(), []), (prof(luma=0.1), ["dark"]), (prof(luma=0.9), ["bright"]), (prof(hi=0.08), ["clipped_highlights"]),
    (prof(luma=0.2, lo=0.4), ["crushed_shadows"]), (prof(contrast=0.1), ["flat"]), (prof(luma=0.3, lo=0.4), ["crushed_shadows"]),
    (prof(luma=0.5, lo=0.4), []),      # lots of black pixels in a bright shot is not "crushed"
])
def test_exposure_problems_come_from_the_measured_profile(profile, problems):
    assert Q.exposure_of(profile)["problems"] == problems


def test_no_profile_means_no_exposure_verdict():
    assert Q.exposure_of(None) is None and Q.exposure_of({"present": False}) is None


def windows():
    return [{"start": 0.0, "end": 5.0, "rms_db": -20.0, "clipped_ratio": 0.0, "rumble_ratio": 0.1},
            {"start": 2.5, "end": 7.5, "rms_db": -18.0, "clipped_ratio": 0.05, "rumble_ratio": 0.7},
            {"start": 10.0, "end": 15.0, "rms_db": -30.0, "clipped_ratio": 0.0, "rumble_ratio": 0.1}]


def test_audio_trouble_reads_the_windows_that_overlap_the_shot():
    t = Q.audio_trouble(windows(), 5.5, 7.0)          # inside the one rumbly, clipped window
    assert t["problems"] == ["clipping", "rumble"] and t["clipped_ratio"] == 0.05
    half = Q.audio_trouble(windows(), 3.0, 6.0)       # rumble is averaged over the windows it overlaps (0.1 and 0.7)
    assert half["problems"] == ["clipping"] and half["rumble_ratio"] == 0.4
    clean = Q.audio_trouble(windows(), 10.0, 14.0)
    assert clean["problems"] == [] and clean["rms_db"] == -30.0
    assert Q.audio_trouble(windows(), 100.0, 105.0) is None and Q.audio_trouble([], 0.0, 5.0) is None


def test_exposure_and_audio_problems_lower_the_measured_score_and_show_as_flags():
    base = {"sharpness": 0.4, "motion": {"valid_pairs": 10, "jitter_px": 0.0}, "black": False}
    ok = Q.shot_quality({**base, "look": prof()}, median_sharpness=0.4, audio=None, watch=None)
    dark = Q.shot_quality({**base, "look": prof(luma=0.08)}, median_sharpness=0.4, audio=None, watch=None)
    noisy = Q.shot_quality({**base, "look": prof()}, median_sharpness=0.4, audio={"problems": ["clipping"]}, watch=None)
    assert ok["score"] == 1.0 and dark["score"] == 0.75 and "dark" in dark["flags"]
    assert noisy["score"] == 0.9 and "audio_clipping" in noisy["flags"]


def test_a_black_shot_scores_zero():
    q = Q.shot_quality({"sharpness": None, "motion": {}, "black": True, "look": None}, median_sharpness=0.4, audio=None, watch=None)
    assert q["score"] == 0.0 and "black" in q["flags"] and q["highlight"] == 0.0


# ============================ measured vs inferred ============================
def test_the_models_opinion_sits_beside_the_measurement_and_never_replaces_it():
    shot = {"sharpness": 0.1, "motion": {}, "black": False, "look": prof()}      # very soft
    watch = {"interest": 0.9, "highlight_reason": "great moment", "usable": True, "people_count": 2}
    q = Q.shot_quality(shot, median_sharpness=0.4, audio=None, watch=watch)
    assert q["kind"] == "measured" and "blurry" in q["flags"], "the measured defect stays"
    assert q["inferred"] == {"interest": 0.9, "highlight_reason": "great moment", "usable": True, "people_count": 2}
    assert q["highlight"] == pytest.approx(0.6 * 0.9 + 0.4 * q["score"], abs=1e-3)


def test_a_shot_the_model_calls_unusable_cannot_be_a_highlight():
    shot = {"sharpness": 0.5, "motion": {}, "black": False, "look": prof()}
    q = Q.shot_quality(shot, median_sharpness=0.5, audio=None, watch={"interest": 0.95, "usable": False, "usable_reason": "lens covered"})
    assert q["highlight"] <= 0.1 and q["inferred"]["usable_reason"] == "lens covered"


def test_without_the_model_the_highlight_is_the_measured_score():
    q = Q.shot_quality({"sharpness": 0.5, "motion": {}, "black": False, "look": prof()}, median_sharpness=0.5, audio=None, watch=None)
    assert q["highlight"] == q["score"] and "inferred" not in q


# ============================ takes and scenes ============================
DIMS = 8


def unit(i, j=None, w=0.0):
    v = np.zeros(DIMS, np.float32)
    v[i] = 1.0 - w
    if j is not None:
        v[j] = w
    return v / np.linalg.norm(v)


def fake_file(sha, file_id, shots, vecs, highlights=None):
    fi = SimpleNamespace(sha=sha, file_id=file_id, shots=[], image_rows=[], image_matrix=None)
    rows, mats = [], []
    for i, (start, end) in enumerate(shots):
        fi.shots.append({"id": i, "start": start, "end": end, "black": False, "watch": {"description": f"shot {i}", "mood": "calm"},
                         "quality": {"highlight": (highlights or {}).get(i, 0.5)}, "speech": None})
        rows.append({"shot": i, "t": (start + end) / 2})
        mats.append(vecs[i])
    fi.image_rows, fi.image_matrix = rows, np.stack(mats)
    return fi


def test_near_identical_shots_across_files_are_one_group_led_by_the_best_take():
    a = fake_file("a" * 64, "A", [(0, 5), (5, 10)], [unit(0), unit(1)], {0: 0.4, 1: 0.9})
    b = fake_file("b" * 64, "B", [(0, 6)], [unit(0, 5, 0.1)], {0: 0.8})            # looks like A's first shot
    c = fake_file("c" * 64, "C", [(0, 4)], [unit(1, 6, 0.1)], {0: 0.6})            # looks like A's second shot
    groups = Q.take_groups([a, b, c])
    by_best = {(g["best"]["file_id"], g["best"]["shot_id"]): g for g in groups}
    assert set(by_best) == {("B", 0), ("A", 1)}, "the better take leads each group"
    assert {(m["file_id"], m["shot_id"]) for m in by_best[("B", 0)]["members"]} == {("A", 0), ("B", 0)}
    assert len(groups) == 2


def test_unique_shots_form_no_group_and_black_shots_never_do():
    a = fake_file("a" * 64, "A", [(0, 5), (5, 10), (10, 15)], [unit(0), unit(1), unit(2)])
    assert Q.take_groups([a]) == []
    a.shots[0]["black"] = a.shots[1]["black"] = True
    b = fake_file("b" * 64, "B", [(0, 5), (5, 10)], [unit(0), unit(1)])
    assert Q.take_groups([a, b]) == []


def test_the_same_footage_imported_twice_is_not_its_own_take():
    a = fake_file("a" * 64, "A", [(0, 5)], [unit(0)])
    twin = fake_file("a" * 64, "A-COPY", [(0, 5)], [unit(0)])
    assert Q.take_groups([a, twin]) == []


def test_neighbouring_shots_that_look_alike_form_a_scene_card():
    fi = fake_file("a" * 64, "A", [(0, 4), (4, 8), (8, 12), (12, 16)], [unit(0), unit(0, 1, 0.1), unit(3), unit(3, 4, 0.1)],
                   {0: 0.3, 1: 0.9, 2: 0.2, 3: 0.4})
    fi.shots[1]["watch"]["objects"] = [{"label": "dog"}]
    cards = Q.scenes_of(fi)
    assert [(c["start"], c["end"], c["shot_ids"]) for c in cards] == [(0, 8, [0, 1]), (8, 16, [2, 3])]
    assert cards[0]["best_shot"] == 1 and cards[0]["objects"] == ["dog"] and cards[0]["mood"] == "calm" and cards[0]["summary"] == "shot 0"


def test_without_picture_vectors_every_shot_is_its_own_scene():
    fi = SimpleNamespace(shots=[{"id": 0, "start": 0, "end": 4, "watch": None, "quality": {}}, {"id": 1, "start": 4, "end": 8, "watch": None, "quality": {}}],
                         image_rows=[], image_matrix=None)
    assert [c["shot_ids"] for c in Q.scenes_of(fi)] == [[0], [1]]


# ============================ loaded with the file ============================
def test_a_loaded_file_carries_quality_and_scenes_for_every_shot(tmp_path):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "s"))
    sha = "7" * 64
    shelf.set_source(sha, duration=10.0, media_type="video")
    shelf.write_json(sha, "structure.json", {"shots": [
        {"id": 0, "start": 0.0, "end": 5.0, "sharpness": 0.42, "motion": {"valid_pairs": 20, "jitter_px": 0.0, "class": "static"}},
        {"id": 1, "start": 5.0, "end": 10.0, "sharpness": 0.12, "motion": {"valid_pairs": 20, "jitter_px": 2.0, "class": "handheld"}}]})
    shelf.set_layer(sha, "structure", version=1, status="ready")        # saved by an older version: still read
    shelf.write_json(sha, "audio.json", {"windows": [{"start": 5.0, "end": 10.0, "rms_db": -20, "clipped_ratio": 0.2, "rumble_ratio": 0.0}],
                                         "noise_floor_db": -60.0})
    shelf.set_layer(sha, "audio", version=1, status="ready")
    fi = library.load_file_index(shelf, sha, file_id="F")
    q0, q1 = fi.shots[0]["quality"], fi.shots[1]["quality"]
    assert q0["flags"] == [] and q0["score"] == 1.0
    assert {"blurry", "shaky", "audio_clipping"} <= set(q1["flags"]) and q1["score"] < 0.4
    assert len(fi.scenes) == 2 and fi.scenes[0]["shot_ids"] == [0]
