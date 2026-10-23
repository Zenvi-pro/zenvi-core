"""The face maths: letterbox, YuNet decoding, alignment and embedding, with stand-in models."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes.media_index import faces as F  # noqa: E402


def test_letterbox_keeps_proportion_pads_black_and_gives_bgr():
    rgb = np.zeros((720, 1280, 3), np.uint8)
    rgb[:, :, 0] = 200                                     # red in RGB
    blob, scale = F.letterbox(rgb)
    assert blob.shape == (1, 3, 640, 640) and scale == 0.5
    assert blob[0, 2, 100, 100] == 200 and blob[0, 0, 100, 100] == 0, "BGR: red is the last channel"
    assert blob[0, :, 400:, :].max() == 0, "the bottom (720 * 0.5 = 360 rows used) is padding"


def tensors(r, c, stride, cls=0.9, obj=0.9, bbox=(0.1, -0.2, np.log(4.0), np.log(5.0)), size=640):
    out = []
    for st in F.STRIDES:
        n = (size // st) ** 2
        out.append(np.zeros((1, n, 1), np.float32))
    for st in F.STRIDES:
        out.append(np.zeros((1, (size // st) ** 2, 1), np.float32))
    for st in F.STRIDES:
        out.append(np.zeros((1, (size // st) ** 2, 4), np.float32))
    for st in F.STRIDES:
        out.append(np.zeros((1, (size // st) ** 2, 10), np.float32))
    i = F.STRIDES.index(stride)
    idx = r * (size // stride) + c
    out[i][0, idx, 0], out[3 + i][0, idx, 0] = cls, obj
    out[6 + i][0, idx] = bbox
    out[9 + i][0, idx] = [0.5, 0.5, 1.5, 0.5, 1.0, 1.0, 0.5, 1.5, 1.5, 1.5]
    return out


def test_yunet_outputs_decode_to_a_box_and_five_landmarks():
    found = F.decode_yunet(tensors(10, 20, 8))
    assert len(found) == 1
    x, y, w, h = found[0]["box"]
    assert (x + w / 2, y + h / 2) == pytest.approx((160.8, 78.4), abs=1e-3) and (w, h) == pytest.approx((32.0, 40.0), abs=1e-3)
    assert found[0]["score"] == pytest.approx(0.9, abs=1e-4)
    assert found[0]["kps"].shape == (5, 2) and found[0]["kps"][0] == pytest.approx([(0.5 + 20) * 8, (0.5 + 10) * 8], abs=1e-3)


def test_weak_detections_are_dropped_and_overlapping_ones_merged():
    assert F.decode_yunet(tensors(10, 20, 8, cls=0.3, obj=0.3)) == []
    a = {"box": [10, 10, 40, 40], "score": 0.9}
    b = {"box": [12, 12, 40, 40], "score": 0.8}
    c = {"box": [200, 200, 40, 40], "score": 0.7}
    assert [f["score"] for f in F.nms([b, c, a])] == [0.9, 0.7]


class FakeDetector:
    def __init__(self, outputs):
        self.outputs = outputs

    def get_inputs(self):
        return [SimpleNamespace(name="input")]

    def run(self, _names, feed):
        assert feed["input"].shape == (1, 3, 640, 640)
        return self.outputs


def test_detect_maps_boxes_back_to_the_picture_and_clips_them_to_it():
    rgb = np.zeros((360, 640, 3), np.uint8)                  # scale 1.0
    faces = F.detect(FakeDetector(tensors(10, 20, 8)), rgb)
    assert len(faces) == 1 and faces[0]["box"][0] == pytest.approx(144.8, abs=0.01) and faces[0]["score"] == 0.9
    half = F.detect(FakeDetector(tensors(10, 20, 8)), np.zeros((720, 1280, 3), np.uint8))   # scale 0.5: everything doubles
    assert half[0]["box"][0] == pytest.approx(289.6, abs=0.02) and half[0]["box"][2] == pytest.approx(64.0, abs=0.01)
    edge = F.detect(FakeDetector(tensors(0, 0, 8)), np.zeros((360, 640, 3), np.uint8))
    assert edge[0]["box"][0] == 0.0, "clipped at the picture's edge"


def test_the_similarity_transform_recovers_a_known_rotation_scale_and_shift():
    theta, s, shift = np.deg2rad(12.0), 1.7, np.array([30.0, -8.0])
    rot = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    src = (F.TEMPLATE @ rot.T) * s + shift                   # the face as it sits in a picture
    m = F.similarity_transform(src, F.TEMPLATE)
    mapped = src @ m[:, :2].T + m[:, 2]
    assert np.abs(mapped - F.TEMPLATE).max() < 1e-6


def test_alignment_puts_the_face_on_the_template_and_blanks_what_is_outside_the_picture():
    h, w = 300, 400
    ys, xs = np.mgrid[0:h, 0:w]
    img = np.stack([xs % 256, ys % 256, np.full_like(xs, 77)], -1).astype(np.uint8)
    s, shift = 2.0, np.array([100.0, 50.0])
    kps = F.TEMPLATE * s + shift
    out = F.align(img, kps)
    assert out.shape == (112, 112, 3)
    y, x = 60, 50                                             # an aligned pixel maps to (x * 2 + 100, y * 2 + 50) in the picture
    assert out[y, x, 0] == pytest.approx((x * s + shift[0]) % 256, abs=1.5) and out[y, x, 1] == pytest.approx(y * s + shift[1], abs=1.5)
    far = F.align(img, F.TEMPLATE * 1.0 + np.array([-300.0, -300.0]))
    assert far.max() == 0, "a face entirely outside the picture is black, not wrapped or stretched"


class FakeRecogniser:
    def get_inputs(self):
        return [SimpleNamespace(name="data")]

    def run(self, _n, feed):
        blob = feed["data"]
        assert blob.shape == (1, 3, 112, 112) and blob.dtype == np.float32
        v = np.zeros((1, 128), np.float32)
        v[0, :3] = blob[0, :, 56, 56]                         # the centre pixel's RGB becomes the "face"
        return [v * 3.0]


def test_an_embedding_has_length_one_and_the_same_face_gives_the_same_numbers():
    red = np.zeros((112, 112, 3), np.uint8)
    red[:, :, 0] = 200
    e1, e2 = F.embed(FakeRecogniser(), red), F.embed(FakeRecogniser(), red.copy())
    assert e1.shape == (128,) and float(np.linalg.norm(e1)) == pytest.approx(1.0, abs=1e-6) and np.array_equal(e1, e2)
    blue = np.zeros((112, 112, 3), np.uint8)
    blue[:, :, 2] = 200
    assert F.cosine(e1, F.embed(FakeRecogniser(), blue)) == pytest.approx(0.0, abs=1e-6)
    assert F.cosine(e1, e1) == pytest.approx(1.0)
    assert float(np.linalg.norm(F.embed(FakeRecogniser(), np.zeros((112, 112, 3), np.uint8)))) == 0.0, "an all-zero output is not divided by zero"


def test_a_detection_only_a_few_pixels_wide_is_dropped():
    tiny = tensors(10, 20, 8, bbox=(0.1, -0.2, np.log(0.3), np.log(0.3)))
    assert F.decode_yunet(tiny) and F.detect(FakeDetector(tiny), np.zeros((360, 640, 3), np.uint8)) == []
