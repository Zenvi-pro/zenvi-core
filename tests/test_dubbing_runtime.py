"""Dubbing runtime: the pack picked for this computer, pinned downloads, the install plan, and the worker protocol."""

import os
import sys
import threading
import time
import zipfile

import pytest

from classes import dubbing_runtime as dr

READY = {"env": True, "pack": "cpu", "face_models": True, "ming": True, "probe": {"edtalk": {"backend": "cpu"}}}

FAKE_WORKER = '''
import json, sys, time
args = json.load(open(sys.argv[sys.argv.index("--args") + 1], encoding="utf-8"))
command = sys.argv[sys.argv.index("--command") + 1]
if command == "work":
    print(json.dumps({"progress": 0.5, "message": "half way"}), flush=True)
    print("a library warning, not JSON", flush=True)
    print(json.dumps({"result": {"home": args["home"], "n": args["n"]}}), flush=True)
elif command == "fail":
    print(json.dumps({"error": "bad clip"}), flush=True)
elif command == "crash":
    print("Traceback (most recent call last):", flush=True)
    sys.exit(3)
else:
    print(json.dumps({"progress": 0.1, "message": "started"}), flush=True)
    time.sleep(60)
'''


@pytest.fixture
def worker(tmp_path, monkeypatch):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    (tmp_path / "dubbing").mkdir()
    monkeypatch.setattr(dr, "WORKER", str(script))
    monkeypatch.setattr(dr, "_env_python", lambda: sys.executable)
    monkeypatch.setattr(dr, "home", lambda: str(tmp_path / "dubbing"))
    return tmp_path


@pytest.mark.parametrize("platform, machine, gpus, pack", [
    ("win32", "AMD64", ["Intel(R) Iris(R) Xe Graphics"], "openvino"),
    ("linux", "x86_64", ["00:02.0 VGA compatible controller: Intel Corporation Alder Lake-P"], "openvino"),
    ("win32", "AMD64", ["NVIDIA GeForce RTX 4060"], "webgpu"),
    ("win32", "AMD64", ["AMD Radeon RX 7600"], "webgpu"),
    ("win32", "AMD64", ["Intel(R) UHD Graphics", "NVIDIA GeForce RTX 4060"], "openvino"),
    ("darwin", "arm64", ["Apple M3"], "cpu"),
    ("win32", "AMD64", [], "cpu"),
    ("linux", "aarch64", ["00:02.0 VGA compatible controller: Intel Corporation"], "cpu"),
])
def test_choose_pack_follows_the_gpu(platform, machine, gpus, pack):
    assert dr.choose_pack(platform, machine, gpus) == pack


def test_pinned_files_carry_sha256_and_size_and_ming_only_when_asked():
    face = dr.pinned_files({"face"})
    both = dr.pinned_files({"face", "ming"})
    assert face and len(both) > len(face)
    for path, url, sha256, size in both:
        assert url.startswith("https://") and len(sha256) == 64 and size > 0, path
    assert "model.safetensors" not in {os.path.basename(path) for path, *_ in face}
    assert "model.safetensors" in {os.path.basename(path) for path, *_ in both}


def steps(st, features, pack="cpu"):
    return [step["id"] for step in dr.install_plan(st, features, pack)]


def test_install_plan_lists_only_missing_steps():
    assert steps(READY, {"face", "ming"}) == []
    assert steps({**READY, "ming": False}, {"face"}) == []                 # Ming not asked for
    assert steps(READY, {"face", "ming"}, pack="webgpu") == ["pip", "probe"]
    assert steps({**READY, "probe": None}, {"face"}) == ["probe"]
    fresh = {"env": False, "pack": None, "face_models": False, "ming": False, "probe": None}
    assert steps(fresh, {"face", "ming"}, pack="openvino") == ["env", "pip", "face", "ming", "probe"]


def test_packages_install_torch_cpu_first_and_drop_the_other_onnxruntime_packs(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(dr, "home", lambda: str(tmp_path))
    monkeypatch.setattr(dr, "_env_python", lambda: "python")
    monkeypatch.setattr(dr.manim, "_run", lambda argv, cwd, timeout, env: calls.append(argv) or (0, ""))
    dr._install_packages("openvino", ming=True)
    uninstall, torch, rest = calls
    assert "onnxruntime" in uninstall and "onnxruntime-webgpu" in uninstall
    assert "onnxruntime-openvino" not in uninstall
    assert "torch==2.6.0" in torch and "https://download.pytorch.org/whl/cpu" in torch
    assert "onnxruntime-openvino==1.24.1" in rest and "transformers==4.46.3" in rest and "torch==2.6.0" not in rest
    assert (tmp_path / "pack.txt").read_text(encoding="utf-8") == "openvino"


def test_ming_code_unzips_without_its_archive_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(dr, "home", lambda: str(tmp_path))
    with zipfile.ZipFile(tmp_path / dr.MING_CODE[1], "w") as bundle:
        bundle.writestr("Ming-3.0-tts-9332fb/README.md", "docs")
        bundle.writestr("Ming-3.0-tts-9332fb/ming/tts.py", "code")
    dr._unzip_ming(str(tmp_path / "ming-code"))
    assert (tmp_path / "ming-code" / "ming" / "tts.py").read_text(encoding="utf-8") == "code"
    assert not (tmp_path / "Ming-3.0-tts-9332fb").exists()


def test_run_worker_reports_progress_and_returns_the_result(worker):
    seen = []
    result = dr.run_worker("work", {"n": 2}, progress=lambda fraction, message: seen.append((fraction, message)))
    assert seen == [(0.5, "half way")]
    assert result == {"home": str(worker / "dubbing"), "n": 2}


def test_run_worker_raises_the_worker_error(worker):
    with pytest.raises(dr.DubbingRuntimeError, match="bad clip"):
        dr.run_worker("fail", {"n": 1})


def test_run_worker_without_a_result_says_why(worker):
    with pytest.raises(dr.DubbingRuntimeError, match="Traceback"):
        dr.run_worker("crash", {"n": 1})


def test_run_worker_cancel_kills_the_worker(worker):
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(dr.DubbingRuntimeError, match="cancelled"):
        dr.run_worker("hang", {"n": 1}, cancel=cancel)
    assert time.monotonic() - started < 30                                  # the worker sleeps 60 s


def test_run_worker_needs_the_runtime_installed(worker, monkeypatch):
    monkeypatch.setattr(dr, "_env_python", lambda: str(worker / "nowhere" / "python"))
    with pytest.raises(dr.DubbingRuntimeError, match="not installed"):
        dr.run_worker("work", {"n": 1})


def test_the_worker_finds_the_editors_ffmpeg_on_its_path(monkeypatch, tmp_path):
    from classes import ffmpeg_cli
    tools = tmp_path / "ffmpeg-bin"
    tools.mkdir()
    monkeypatch.setattr(ffmpeg_cli, "find_ffmpeg", lambda name="ffmpeg": str(tools / "ffmpeg"))
    path = next(v for k, v in dr._environment().items() if k.upper() == "PATH")
    assert str(tools) in path.split(os.pathsep)
