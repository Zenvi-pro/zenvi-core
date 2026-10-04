"""
 @file
 @brief The two small ONNX models behind people identity: where they live, how they are fetched, and how they are loaded.

 Faces are found with YuNet (MIT, 0.2 MB) and recognised with SFace (Apache 2.0, 37 MB), both from the OpenCV model zoo. They run
 locally through onnxruntime, which is the optional speech extra (``requirements-speech.txt``). Nothing is fetched unless the
 user turned the people preference on and asked for it, every file is checked against a pinned SHA-256, and a download is
 staged in a temporary file and only then moved into place.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from typing import Any, Callable, Dict, Optional

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS: Dict[str, Dict[str, Any]] = {
    "detector": {"file": "face_detection_yunet_2023mar.onnx", "url": f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
                 "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", "bytes": 232589, "license": "MIT"},
    "recognizer": {"file": "face_recognition_sface_2021dec.onnx", "url": f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
                   "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79", "bytes": 38696353, "license": "Apache-2.0"},
}


class PeopleUnavailable(RuntimeError):
    """People identity cannot run, and why (a missing package or model)."""


def models_dir() -> str:
    try:
        from classes import info
        base = getattr(info, "USER_PATH", None) or os.path.join(os.path.expanduser("~"), ".openshot_qt")
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".openshot_qt")
    return os.path.join(base, "models", "people")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def model_path(name: str) -> Optional[str]:
    """The installed model's path when it is there and intact, else None."""
    spec = MODELS[name]
    path = os.path.join(models_dir(), spec["file"])
    try:
        if os.path.getsize(path) != spec["bytes"] or _sha256(path) != spec["sha256"]:
            return None
    except OSError:
        return None
    return path


def runtime_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def status() -> Dict[str, Any]:
    """What is installed: the runtime and each model."""
    return {"runtime": runtime_available(), "models": {n: bool(model_path(n)) for n in MODELS},
            "download_bytes": sum(s["bytes"] for n, s in MODELS.items() if not model_path(n)),
            "licenses": {n: s["license"] for n, s in MODELS.items()}}


def download(progress: Optional[Callable[[float], None]] = None, should_cancel: Optional[Callable[[], bool]] = None,
             fetch: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Fetch every model that is missing. Each is checked against its pinned hash and moved into place only when whole.

    Blocks on the network: call it from a worker, never the GUI thread. ``fetch`` (url -> readable) exists for tests.
    Returns ``{"ok", "installed", "error"}``; a failed or cancelled download leaves nothing half-written.
    """
    import urllib.request
    opener = fetch or (lambda url: urllib.request.urlopen(url, timeout=60))
    os.makedirs(models_dir(), exist_ok=True)
    todo = [n for n in MODELS if not model_path(n)]
    total = sum(MODELS[n]["bytes"] for n in todo) or 1
    done = 0
    installed = []
    for name in todo:
        spec = MODELS[name]
        fd, tmp = tempfile.mkstemp(prefix=name + "_", suffix=".part", dir=models_dir())
        os.close(fd)
        try:
            with opener(spec["url"]) as resp, open(tmp, "wb") as out:
                while True:
                    if should_cancel and should_cancel():
                        return {"ok": False, "installed": installed, "error": "cancelled"}
                    block = resp.read(1 << 18)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    if progress:
                        progress(min(1.0, done / total))
            if os.path.getsize(tmp) != spec["bytes"] or _sha256(tmp) != spec["sha256"]:
                return {"ok": False, "installed": installed, "error": f"the {name} model did not match its checksum; nothing was installed"}
            os.replace(tmp, os.path.join(models_dir(), spec["file"]))
            tmp = ""
            installed.append(name)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, which shows it
            return {"ok": False, "installed": installed, "error": f"could not download the {name} model: {exc}"[:200]}
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return {"ok": True, "installed": installed, "error": None}


def remove_models() -> int:
    """Delete the downloaded models. Returns how many files were removed."""
    count = 0
    for spec in MODELS.values():
        try:
            os.remove(os.path.join(models_dir(), spec["file"]))
            count += 1
        except OSError:
            pass
    return count


_sessions: Dict[str, Any] = {}
_lock = threading.Lock()


def session(name: str) -> Any:
    """The loaded onnxruntime session for a model (cached). Raises ``PeopleUnavailable`` saying what is missing."""
    with _lock:
        if name in _sessions:
            return _sessions[name]
        if not runtime_available():
            raise PeopleUnavailable("people identity needs the onnxruntime package (pip install -r requirements-speech.txt)")
        path = model_path(name)
        if not path:
            raise PeopleUnavailable(f"the {name} model is not installed: run the people setup to download it ({MODELS[name]['bytes'] // 1000} KB)")
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.intra_op_num_threads = 2
        _sessions[name] = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        return _sessions[name]


def clear_sessions() -> None:
    with _lock:
        _sessions.clear()
