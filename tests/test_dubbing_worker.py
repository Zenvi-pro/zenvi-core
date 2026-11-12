"""Dubbing worker helpers that need no model files: geometry, scene cut, mel windows, probe picks, JSON protocol.

Numpy only, except the cases that need cv2, which skip when cv2 is missing (the CI mirror has none).
"""

import io
import json

import numpy as np
import pytest

from classes import dubbing_worker as dw


def test_template_matrix_maps_template_onto_itself():
    pytest.importorskip("cv2")
    template = dw.WARP_TEMPLATES["arcface_112_v2"] * 112
    matrix = dw.template_matrix(template.astype(np.float32), "arcface_112_v2", (112, 112))
    np.testing.assert_allclose(matrix, [[1, 0, 0], [0, 1, 0]], atol=1e-3)


def test_template_matrix_maps_scaled_and_shifted_landmarks():
    cv2 = pytest.importorskip("cv2")
    target = dw.WARP_TEMPLATES["ffhq_512"] * 512
    moved = (target * 2.0 + [3.0, -4.0]).astype(np.float32)  # the same face, bigger and shifted
    matrix = dw.template_matrix(moved, "ffhq_512", (512, 512))
    np.testing.assert_allclose(matrix, [[0.5, 0, -1.5], [0, 0.5, 2]], atol=1e-3)
    np.testing.assert_allclose(cv2.transform(moved.reshape(1, -1, 2), matrix).reshape(-1, 2), target, atol=1e-2)


def test_paste_back_zero_mask_returns_frame_unchanged():
    pytest.importorskip("cv2")
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
    crop = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
    out = dw.paste_back(frame, crop, np.zeros((64, 64), np.float32), np.eye(2, 3))
    assert np.array_equal(out, frame)


def test_paste_back_full_mask_writes_the_crop():
    pytest.importorskip("cv2")
    rng = np.random.default_rng(4)
    frame = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
    crop = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
    out = dw.paste_back(frame, crop, np.ones((64, 64), np.float32), np.eye(2, 3))
    assert np.array_equal(out, crop)


def test_scene_cut_only_between_different_images():
    pytest.importorskip("cv2")
    rng = np.random.default_rng(2)
    first = rng.integers(0, 256, (90, 160, 3), dtype=np.uint8)
    second = rng.integers(0, 256, (90, 160, 3), dtype=np.uint8)
    assert dw.is_scene_cut(dw.frame_histogram(first), dw.frame_histogram(second))
    assert not dw.is_scene_cut(dw.frame_histogram(first), dw.frame_histogram(first.copy()))


def test_mel_windows_are_80_by_16_one_per_video_frame():
    rng = np.random.default_rng(3)
    spectrogram = dw.create_spectrogram(rng.standard_normal(16000))  # 1 s at 16 kHz, hop 200 samples
    assert spectrogram.shape == (80, 81)
    windows = dw.extract_audio_frames(spectrogram, 30)
    assert all(w.shape == (80, 16) for w in windows)
    assert len(windows) == 25  # window j starts at mel step j * 80 / 30; the last ones need audio past 1 s
    assert np.array_equal(windows[10], spectrogram[:, 26:42])


def test_prepare_voice_resamples_48k_to_16k():
    t = np.arange(48000) / 48000
    tone = (np.sin(2 * np.pi * 440 * t) * 20000).astype(np.int16)
    voice = dw.prepare_voice(np.stack([tone, tone], axis=1))
    assert voice.shape == (16000,)
    assert np.argmax(np.abs(np.fft.rfft(voice))) == 440


def test_triang_matches_scipy_windows():
    np.testing.assert_allclose(dw.triang(4), [0.25, 0.75, 0.75, 0.25])
    np.testing.assert_allclose(dw.triang(3), [0.5, 1.0, 0.5])


def test_pick_takes_fastest_candidate_above_threshold():
    rows = [
        {"backend": "webgpu", "ms": 5.0, "cosine": 0.9},
        {"backend": "openvino_gpu", "ms": 9.0, "cosine": 0.99999},
        {"backend": "openvino_gpu_fp32", "ms": 12.0, "cosine": 1.0},
        {"backend": "cpu", "ms": 30.0, "cosine": 1.0},
    ]
    assert dw.pick_backend(rows, 0.999)["backend"] == "openvino_gpu"


def test_pick_falls_back_to_cpu_when_no_candidate_matches():
    rows = [
        {"backend": "openvino_gpu", "ms": 9.0, "cosine": 0.5},
        {"backend": "cuda", "error": "no device"},
        {"backend": "cpu", "ms": 30.0, "cosine": 1.0},
    ]
    assert dw.pick_backend(rows, 0.999)["backend"] == "cpu"


def test_parse_args_reads_the_json_file(tmp_path):
    path = tmp_path / "args.json"
    path.write_text(json.dumps({"home": "C:/dub", "models": ["edtalk"]}), encoding="utf-8")
    command, args = dw.parse_args(["--command", "probe", "--args", str(path)])
    assert command == "probe"
    assert args == {"home": "C:/dub", "models": ["edtalk"]}


def test_parse_args_rejects_unknown_command(tmp_path):
    path = tmp_path / "args.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        dw.parse_args(["--command", "nope", "--args", str(path)])


def test_emit_writes_one_json_line():
    stream = io.StringIO()
    dw.emit({"progress": 0.5, "message": "x"}, stream)
    assert stream.getvalue() == '{"progress": 0.5, "message": "x"}\n'


def test_progress_is_throttled_but_the_final_line_is_sent():
    stream = io.StringIO()
    report = dw.progress_reporter(stream)
    report(0.1, "a")
    report(0.2, "b")  # within 0.5 s of the first line: dropped
    report(1.0, "done")
    assert [json.loads(line)["message"] for line in stream.getvalue().splitlines()] == ["a", "done"]


def test_mings_cuda_only_autocast_runs_on_the_cpu_and_its_speaker_head_takes_float_input():
    seen = []

    class Autocast:
        def __init__(self, device_type, *args, **kwargs):
            seen.append(device_type)

    class FakeTorch:
        autocast = Autocast

    class Head:
        dtype = "bf16"

        def forward(self, x):
            return ("head", x)

    head = Head()
    dw._patch_ming_for_cpu(FakeTorch, head, lambda x, dtype: (x, dtype), lambda module: module.dtype)
    FakeTorch.autocast(device_type="cuda", dtype="bf16")
    FakeTorch.autocast("cpu")
    assert seen == ["cpu", "cpu"]
    assert head.forward("float input") == ("head", ("float input", "bf16"))


def test_speech_shorter_than_the_clip_is_padded_so_ffmpeg_reads_every_frame():
    cmd = dw.encode_cmd("in.wav", "out.mp4", 1280, 720, 30.0, 0.0)
    assert "-shortest" in cmd and "apad" in " ".join(cmd)


def test_a_very_short_line_still_gives_mel_windows():
    pcm = np.zeros((int(48000 * 0.05), 2), np.int16)
    windows = dw.extract_audio_frames(dw.create_spectrogram(dw.prepare_voice(pcm)), 30.0)
    assert len(windows) >= 1 and windows[0].shape == (80, 16)


def test_ffmpeg_runs_without_a_console_window_on_windows(monkeypatch):
    monkeypatch.setattr(dw.sys, "platform", "win32")
    assert dw._quiet().get("creationflags") == 0x08000000
    monkeypatch.setattr(dw.sys, "platform", "linux")
    assert dw._quiet() == {}
