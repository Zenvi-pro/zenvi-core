"""Dubbing on this computer: the private Python and the models lip sync, face swap and local speech need.

Nothing ships with the editor. The first use downloads a pinned micromamba that makes Python 3.12 under
``~/.openshot_qt/dubbing/env`` (conda-forge), pip installs exact versions into it with the ONNX Runtime pack
this computer's GPU wants, and the models are fetched by sha256. Their work runs in that Python, as
``dubbing_worker.py``, in a separate process with an allowlisted environment: the editor loads no GPU or ML code.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import deque
from typing import Callable, Iterable, List, Optional, Tuple

from classes import hyperframes_local as hf
from classes import info
from classes import manim_local as manim
from classes.logger import log

PYTHON_VERSION = "3.12"
INSTALL_TIMEOUT = 3600
PICKS_SETTING = "dubbing-backend-picks"
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dubbing_worker.py")
_download_lock = threading.RLock()        # not the speech lock: a 3 GB model must not hold up captions

BASE_PIP = ["numpy==2.2.6", "opencv-python-headless==4.11.0.86", "onnx==1.18.0", "onnxslim==0.1.99", "psutil==7.0.0"]
PACKS = {                                 # one ONNX Runtime per env; the first requirement names its distribution
    "cpu": ["onnxruntime==1.24.1"],
    "openvino": ["onnxruntime-openvino==1.24.1", "openvino==2025.4.*"],
    "webgpu": ["onnxruntime-webgpu==1.27.0"],
}
ORT_DISTS = ("onnxruntime", "onnxruntime-openvino", "onnxruntime-webgpu")
MING_PIP = ["transformers==4.46.3", "tokenizers==0.20.3", "accelerate==1.3.0", "huggingface_hub==0.30.2",
            "soundfile==0.12.1", "x_transformers==2.9.2", "diffusers==0.33.0", "einops==0.8.1", "inflect==7.4.0",
            "loguru==0.7.3", "packaging==24.2"]
FACE_MODELS = [                           # (url, name in models/, sha256, bytes)
    ("https://huggingface.co/facefusion/models-3.3.0/resolve/main/edtalk_256.onnx", "edtalk_256.onnx",
     "c4dd933458814f280b9b4b94a6d6bd73a103ce8db9a531542831327c0a14d43e", 270681365),
    ("https://huggingface.co/facefusion/models-3.3.0/resolve/main/hyperswap_1a_256.onnx", "hyperswap_1a_256.onnx",
     "c0e98a8a03a238f461ed3d2570e426b49f46745ee400854a60dceeb70c246add", 402742682),
    ("https://huggingface.co/facefusion/models-3.0.0/resolve/main/yoloface_8n.onnx", "yoloface_8n.onnx",
     "821cdbb1e65fbbabdde7dd0933f754797a343e56fd962729c61ffcefcd135929", 12659761),
    ("https://huggingface.co/facefusion/models-3.0.0/resolve/main/fan_68_5.onnx", "fan_68_5.onnx",
     "ea05f7d9d014ae1d9cdc6e9c72643d8f00d198b5db3fdd384148ddff499a613e", 944321),
    ("https://huggingface.co/facefusion/models-3.0.0/resolve/main/arcface_w600k_r50.onnx", "arcface_w600k_r50.onnx",
     "f1f79dc3b0b79a69f94799af1fffebff09fbd78fd96a275fd8f0cbbea23270d1", 174388474),
    ("https://huggingface.co/facefusion/models-3.0.0/resolve/main/wav2lip_gan_96.onnx", "wav2lip_gan_96.onnx",
     "8e804de2ae264fdfad607339846dabcc728d84191e2fc3433b156c40c83f906c", 145175489),
]
MING_BASE = "https://huggingface.co/inclusionAI/Ming-3.0-tts-0.5B/resolve/7247f4c94a5b2e987404ed28a5cfec41b1a4ca61/"
MING_WEIGHTS = [                         # (name in models/ming/, sha256, bytes)
    ("model.safetensors", "45c4d78d8ad6b1d942e3da86ba035fece02de569f7b2ca7e9d121dfaf18d674c", 2959055504),
    ("campplus.onnx", "a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73", 28303423),
    ("config.json", "2fb0767361b3cd3f41dc9392709fc23fe5b40a39f19d70a38d04acabe991822c", 4754),
    ("configuration.json", "6720b8448f6ad5b4631689a60857bdeb238053a37f6cc6a5936eb50c2a3b9296", 51),
    ("added_tokens.json", "34394b1e16d7da2db16618bd43a0f970902ade0075480bcd812fe62d559ec319", 1785),
    ("special_tokens_map.json", "2c2ec51b9da5db654ac20de8310350d0c9c1a65918ae0181b80dacd2919b4fc3", 2726),
    ("tokenizer.json", "87d54ca3b44ea2f822ba39a0a45cb1b61644bd6606d9d3536dea75367bffb06d", 11431389),
    ("tokenizer_config.json", "12d6162c815edef24f8606e65b60d43fe0d92b0a4e150a0db8fa01c523aaf36a", 16260),
    ("vocab.json", "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910", 2776833),
    ("merges.txt", "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5", 1671853),
]
MING_CODE = ("https://github.com/inclusionAI/Ming-3.0-tts/archive/9332fb9443325d4726ce02cefd4ec1c211df4079.zip",
             "ming-code.zip", "e32e8c7134d8ce8dd3ce3f7199a74b9f0455d2fc29e54ec567fa39756b7a7f8f", 5334221)
MING_ONNX = ("ming_backbone.onnx", "ming_flow.onnx", "ming_decoder.onnx")   # written by the worker's export-ming


class DubbingRuntimeError(Exception):
    """The runtime could not be set up, or a worker step failed; the message says why."""


def home() -> str:
    return os.path.join(info.USER_PATH, "dubbing")


def _env_python() -> str:
    env = os.path.join(home(), "env")
    return os.path.join(env, "python.exe") if sys.platform == "win32" else os.path.join(env, "bin", "python")


def _environment() -> dict:
    # Allowlisted like manim_local: the editor's tokens, keys and PYTHONPATH never reach the worker or pip.
    env = {k: v for k, v in os.environ.items() if k.upper() in manim._ENV_KEEP}
    env.update(PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8", MAMBA_ROOT_PREFIX=os.path.join(home(), "mamba"))
    root = os.path.join(home(), "env")
    first = ([root, os.path.join(root, "Library", "bin"), os.path.join(root, "Scripts")] if sys.platform == "win32"
             else [os.path.join(root, "bin")])
    from classes.ffmpeg_cli import find_ffmpeg
    ffmpeg = find_ffmpeg()
    if ffmpeg:                                # the worker runs ffmpeg by name: the editor's own copy
        first.append(os.path.dirname(ffmpeg))
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[path_key] = os.pathsep.join([d for d in first if os.path.isdir(d)] + [env.get(path_key, "")])
    return env


def choose_pack(platform: str, machine: str, gpu_names: List[str]) -> str:
    """OpenVINO for an Intel GPU, WebGPU for any other, CPU otherwise. The GPU packs are published for x64 only."""
    system, arch = manim._key(platform, machine)
    if system not in ("win32", "linux") or arch != "x64" or not gpu_names:
        return "cpu"
    return "openvino" if any("intel" in name.lower() for name in gpu_names) else "webgpu"


def gpu_names() -> List[str]:
    """Names of this computer's display adapters: CIM on Windows, lspci on Linux, none on macOS."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                                 stdin=subprocess.DEVNULL, creationflags=hf._NO_WINDOW).stdout
            return [line.strip() for line in out.splitlines() if line.strip()]
        if sys.platform.startswith("linux"):
            out = subprocess.run(["lspci"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=60, stdin=subprocess.DEVNULL).stdout
            return [line for line in out.splitlines() if re.search(r"VGA|3D|Display", line)]
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not list the graphics adapters, so the CPU pack is used: %s", exc)
    return []


def _saved_pack() -> Optional[str]:
    try:
        with open(os.path.join(home(), "pack.txt"), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _saved_picks() -> Optional[dict]:
    from classes.app import get_app
    try:
        return json.loads(get_app().get_settings().get(PICKS_SETTING) or "{}") or None
    except ValueError:
        return None


def save_picks(picks: dict) -> None:
    from classes.app import get_app
    get_app().get_settings().set(PICKS_SETTING, json.dumps(picks))


def pinned_files(features: Iterable[str]) -> List[Tuple[str, str, str, int]]:
    """(path, url, sha256, bytes) of every pinned download the features need, in download order."""
    files = []
    if "face" in features:
        files += [(os.path.join(home(), "models", name), url, sha, size) for url, name, sha, size in FACE_MODELS]
    if "ming" in features:
        files += [(os.path.join(home(), "models", "ming", name), MING_BASE + name, sha, size)
                  for name, sha, size in MING_WEIGHTS]
        url, name, sha, size = MING_CODE
        files.append((os.path.join(home(), name), url, sha, size))
    return files


def _present(files: List[Tuple[str, str, str, int]]) -> bool:
    return all(os.path.isfile(path) and os.path.getsize(path) == size for path, _url, _sha, size in files)


def status() -> dict:
    """What is in place: the private Python and its pack, the face models, Ming, and the picks cached in settings."""
    env = os.path.isfile(_env_python())
    models = os.path.join(home(), "models")
    return {
        "env": env,
        "pack": _saved_pack() if env else None,
        "face_models": _present(pinned_files({"face"})) and os.path.isfile(os.path.join(models, "edtalk_slim.onnx")),
        "ming": os.path.isdir(os.path.join(home(), "ming-code")) and _present(pinned_files({"ming"}))
                and all(os.path.isfile(os.path.join(models, "ming_onnx", name)) for name in MING_ONNX),
        "probe": _saved_picks(),
    }


def install_plan(st: dict, features: Iterable[str], pack: str) -> List[dict]:
    """The steps still missing for the features asked for, in order (a probe follows any change)."""
    features = set(features)
    steps: List[dict] = []
    if not st.get("env"):
        steps.append({"id": "env", "label": "Download Python %s with micromamba into Zenvi's folder (no installer)"
                                            % PYTHON_VERSION})
    if st.get("pack") != pack or ("ming" in features and not st.get("ming")):
        steps.append({"id": "pip", "label": "Install the Python packages for the %s backend" % pack})
    if "face" in features and not st.get("face_models"):
        steps.append({"id": "face", "label": "Download the face models (about 1 GB)"})
    if "ming" in features and not st.get("ming"):
        steps.append({"id": "ming", "label": "Download the Ming speech model (about 3 GB)"})
    if steps or (features and not st.get("probe")):
        steps.append({"id": "probe", "label": "Measure which backend runs each model fastest"})
    return steps


def _fetch(files: List[Tuple[str, str, str, int]], what: str, say: Callable[[str], None]) -> None:
    for path, url, sha256, _size in files:
        hf._download_pinned(os.path.dirname(path), [(url, os.path.basename(path), sha256)], what, progress=say,
                            lock=_download_lock)


def _unzip_ming(target: str) -> None:
    """Unzip the pinned Ming code (its archive has one top folder) into *target*, replacing it in one step."""
    staging = os.path.join(home(), "ming-code.partial")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(os.path.join(home(), MING_CODE[1])) as bundle:
        bundle.extractall(staging)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(os.path.join(staging, os.listdir(staging)[0]), target)
    shutil.rmtree(staging, ignore_errors=True)


def _pip(*args: str) -> None:
    code, out = manim._run([_env_python(), "-m", "pip", *args, "--disable-pip-version-check", "--no-input"],
                           home(), INSTALL_TIMEOUT, _environment())
    if code != 0:
        raise hf.HyperframesError("could not install the Python packages (exit code %d): %s" % (code, out[-1500:]))


def _install_env() -> None:
    url, name, digest = manim._micromamba_download()
    bin_dir, env = os.path.join(home(), "bin"), os.path.join(home(), "env")
    hf._download_pinned(bin_dir, [(url, name, digest)], "micromamba installer", lock=_download_lock)
    shutil.rmtree(env, ignore_errors=True)                  # a half-made env from an earlier attempt
    code, out = manim._run([os.path.join(bin_dir, name), "create", "-y", "-q", "-p", env, "-c", "conda-forge",
                            "--override-channels", "python=" + PYTHON_VERSION],
                           home(), INSTALL_TIMEOUT, _environment())
    if code != 0 or not os.path.isfile(_env_python()):
        shutil.rmtree(env, ignore_errors=True)
        raise hf.HyperframesError("could not create the dubbing Python (exit code %d): %s" % (code, out[-1500:]))
    shutil.rmtree(os.path.join(home(), "mamba", "pkgs"), ignore_errors=True)   # the download cache: hundreds of MB


def _install_packages(pack: str, ming: bool) -> None:
    marker = os.path.join(home(), "pack.txt")
    if os.path.lexists(marker):
        os.remove(marker)                                   # written again only once every package is in
    wanted = PACKS[pack][0].split("==")[0]
    _pip("uninstall", "-y", *[dist for dist in ORT_DISTS if dist != wanted])
    if ming:                                                # CPU torch first, so no later package pulls CUDA torch
        _pip("install", "torch==2.6.0", "torchaudio==2.6.0", "--index-url", "https://download.pytorch.org/whl/cpu")
    _pip("install", *BASE_PIP, *PACKS[pack], *(MING_PIP if ming else []))
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(pack)


def _work(command: str, say: Callable[[str], None], cancel: Optional[threading.Event], **args) -> dict:
    return run_worker(command, args, progress=lambda fraction, message: say(message), cancel=cancel)


def _install_face(say: Callable[[str], None], cancel: Optional[threading.Event]) -> None:
    _fetch(pinned_files({"face"}), "face models", say)
    _work("prepare", say, cancel)                           # slims edtalk into models/edtalk_slim.onnx


def _install_ming(say: Callable[[str], None], cancel: Optional[threading.Event]) -> None:
    _fetch(pinned_files({"ming"}), "Ming", say)
    if not os.path.isdir(os.path.join(home(), "ming-code")):
        _unzip_ming(os.path.join(home(), "ming-code"))
    _work("export-ming", say, cancel)                       # three slimmed ONNX parts into models/ming_onnx/


def _probe(say: Callable[[str], None], cancel: Optional[threading.Event]) -> None:
    st = status()
    names = (["edtalk", "wav2lip", "hyperswap", "yoloface", "arcface", "fan68"] if st["face_models"] else []) + \
            (["ming_backbone", "ming_flow", "ming_decoder"] if st["ming"] else [])
    save_picks(_work("probe", say, cancel, models=names)["picks"])


def install(features: Iterable[str], progress: Optional[Callable[[str], None]] = None,
            cancel: Optional[threading.Event] = None) -> dict:
    """Run every missing step of :func:`install_plan`; returns the new status."""
    features = set(features)
    if not features <= {"face", "ming"}:
        raise DubbingRuntimeError("unknown dubbing feature: %s" % ", ".join(sorted(features - {"face", "ming"})))
    say = progress or (lambda message: None)
    pack = choose_pack(sys.platform, _platform.machine(), gpu_names())
    try:
        os.makedirs(home(), exist_ok=True)
        with _download_lock:
            for step in install_plan(status(), features, pack):
                # ponytail: cancel is seen between steps and worker calls only; a download or pip run finishes first.
                # Give _fetch verified_download.fetch_verified(should_cancel=...) if a multi-GB wait matters.
                if cancel is not None and cancel.is_set():
                    raise DubbingRuntimeError("cancelled")
                say(step["label"])
                log.info("Dubbing setup: %s", step["label"])
                if step["id"] == "env":
                    _install_env()
                elif step["id"] == "pip":
                    _install_packages(pack, "ming" in features)
                elif step["id"] == "face":
                    _install_face(say, cancel)
                elif step["id"] == "ming":
                    _install_ming(say, cancel)
                elif step["id"] == "probe":
                    _probe(say, cancel)
    except (hf.HyperframesError, OSError, zipfile.BadZipFile) as exc:
        raise DubbingRuntimeError(str(exc)) from exc
    return status()


def _json_line(line: str) -> Optional[dict]:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                       stdin=subprocess.DEVNULL, creationflags=hf._NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)            # the worker leads its own group (start_new_session)
        except ProcessLookupError:
            pass


def run_worker(command: str, args: dict, progress: Optional[Callable[[float, str], None]] = None,
               cancel: Optional[threading.Event] = None, timeout: float = 7200) -> dict:
    """Run one ``dubbing_worker.py`` command in the private Python and return its result.

    The worker prints JSON lines on stdout: progress, result or error. Cancel or timeout kills its whole tree.
    """
    python = _env_python()
    if not os.path.isfile(python):
        raise DubbingRuntimeError("the dubbing runtime is not installed; install it first")
    fd, args_file = tempfile.mkstemp(prefix="dubbing-%s-" % command, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(args, home=home()), fh)
        try:
            proc = subprocess.Popen([python, WORKER, "--command", command, "--args", args_file], cwd=home(),
                                    env=_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    creationflags=hf._NO_WINDOW, start_new_session=sys.platform != "win32")
        except OSError as exc:
            raise DubbingRuntimeError("could not start the dubbing worker: %s" % exc)
        done, stopped = threading.Event(), []

        def watch() -> None:
            deadline = time.monotonic() + timeout
            while not done.wait(0.2):
                if cancel is not None and cancel.is_set():
                    stopped.append("cancelled")
                elif time.monotonic() > deadline:
                    stopped.append("timeout")
                if stopped:
                    _kill_tree(proc)
                    return

        threading.Thread(target=watch, daemon=True).start()
        result, error, tail = None, None, deque(maxlen=40)
        try:
            for line in proc.stdout:
                message = _json_line(line.strip())
                if message is None:
                    tail.append(line.rstrip())
                elif "error" in message:
                    error = str(message["error"])
                elif "result" in message:
                    result = message["result"]
                elif progress and "progress" in message:
                    progress(float(message["progress"]), str(message.get("message", "")))
            code = proc.wait()
        finally:
            done.set()
            if proc.poll() is None:
                _kill_tree(proc)
                proc.wait()
    finally:
        os.remove(args_file)
    if stopped:
        raise DubbingRuntimeError("cancelled" if stopped[0] == "cancelled"
                                  else "the %s step did not finish within %d s" % (command, timeout))
    if error is not None:
        raise DubbingRuntimeError(error)
    if result is None:
        raise DubbingRuntimeError("the %s step ended without a result (exit code %d): %s"
                                  % (command, code, "\n".join(tail)[-800:]))
    return result
