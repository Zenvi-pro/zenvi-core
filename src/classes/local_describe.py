"""Describe index chunks on this computer with LFM3-Omni 2B through llama.cpp.

Nothing ships with Zenvi. The first local index downloads a pinned llama.cpp build
(Vulkan on Windows and Linux, so a GPU is used when there is one; Metal on macOS) and
the model (about 1.6 GB) into ``~/.openshot_qt/describe``; each file is kept only if it
matches its sha256. The model sees the frames and hears the soundtrack of a chunk and
answers in the backend's own JSON schema, so the backend indexes it like a Gemini answer.

ponytail: one llama.cpp process per chunk reloads the model each time (a few seconds);
keep one ``llama serve`` up for the whole job if that ever dominates indexing time.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from typing import Any, Callable, Dict, List, Optional, Tuple

from classes import hyperframes_local as hf
from classes import info
from classes.logger import log

LLAMA_BUILD = "b11998"
LLAMA_URL = "https://github.com/ggml-org/llama.cpp/releases/download/%s/%s"
LLAMA = {               # (platform, architecture) -> (asset, sha256), from the GitHub release's asset digests
    ("win32", "x64"): ("llama-b11998-bin-win-vulkan-x64.zip",
                       "47a32e3d164f8221658009d7f48794aed36ab9064791a5e04204a39d6eaf839a"),
    ("win32", "arm64"): ("llama-b11998-bin-win-vulkan-arm64.zip",
                         "6b177513efc87378f1d58636a4d05e265cc62812c33f676402328e04c977528a"),
    ("darwin", "arm64"): ("llama-b11998-bin-macos-arm64.tar.gz",
                          "6a2e192152d1a68bbb046dc8466d350cc03376635b187c551c581d672c31b658"),
    ("darwin", "x64"): ("llama-b11998-bin-macos-x64.tar.gz",
                        "a39e5e5be8c2bb439624fdd41302195faa196796af87a1880a7e8a76242aafc8"),
    ("linux", "x64"): ("llama-b11998-bin-ubuntu-vulkan-x64.tar.gz",
                       "8f0b88dc3868cffa0d01d4ed16643117ecda4e036a3c041795d124424fc34c7e"),
    ("linux", "arm64"): ("llama-b11998-bin-ubuntu-vulkan-arm64.tar.gz",
                         "277440899b88e0adf95d2db52579ed813b68226688acde00802854aa99e5a8ae"),
}
MODEL_REVISION = "3d29af2ad166f8299c2d820937e47dc0add20ffc"
MODEL_URL = "https://huggingface.co/LiquidAI/LFM3-Omni-2B-GGUF/resolve/%s/%s"
MODEL = ("LFM3-Omni-2B-Q4_K_M.gguf", "9f8d30e9d4fd7a9349d586e077988229b3ca0a0e4ad730897563d1eccc9893e2")
PROJECTOR = ("mmproj-LFM3-Omni-2B-Q8_0.gguf", "8a25f9d1931244429de404fccacfb7fb933970a36aa5688d2f5069f29c6b27b1")
# The model card's system prompts: video is watched and heard together.
SYSTEM = {"video": "Answer questions about the video using what you see and hear.",
          "audio": "Answer questions about the audio.",
          "image": "Answer questions about the image."}
MEDIA_FLAG = {"video": "--video", "audio": "--audio", "image": "--image"}
DESCRIBE_TIMEOUT = 900
_EXE = "llama.exe" if sys.platform == "win32" else "llama"


def home() -> str:
    return os.path.join(info.USER_PATH, "describe")


def _model_path(name: str) -> str:
    return os.path.join(home(), "models", name)


def llama_program() -> str:
    for root, _dirs, files in os.walk(os.path.join(home(), "llama")):
        if _EXE in files:
            return os.path.join(root, _EXE)
    return ""


def ready() -> bool:
    return bool(llama_program()) and all(os.path.isfile(_model_path(f[0])) for f in (MODEL, PROJECTOR))


def _llama_download(platform: Optional[str] = None, machine: Optional[str] = None) -> Tuple[str, str, str]:
    platform, machine = platform or sys.platform, (machine or _platform.machine()).lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "amd64": "x64", "x86_64": "x64", "x64": "x64"}.get(machine, machine)
    found = LLAMA.get(("linux" if platform.startswith("linux") else platform, arch))
    if not found:
        raise hf.HyperframesError("local indexing has no llama.cpp build for this computer (%s %s)"
                                  % (platform, machine))
    return LLAMA_URL % (LLAMA_BUILD, found[0]), found[0], found[1]


def _model_downloads() -> List[Tuple[str, str, str]]:
    return [(MODEL_URL % (MODEL_REVISION, name), name, digest) for name, digest in (MODEL, PROJECTOR)]


def install(progress: Optional[Callable[[str], None]] = None) -> None:
    """Download and unpack whatever local describing still needs. Safe to call when it is all there."""
    if ready():
        return
    if not llama_program():
        if progress:
            progress("Downloading llama.cpp (%s)" % LLAMA_BUILD)
        url, name, digest = _llama_download()
        downloads = os.path.join(home(), "downloads")
        hf._download_pinned(downloads, [(url, name, digest)], "local describe engine (llama.cpp)")
        archive, target = os.path.join(downloads, name), os.path.join(home(), "llama")
        shutil.rmtree(target, ignore_errors=True)
        try:
            if name.endswith(".zip"):
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(target)
            else:
                with tarfile.open(archive) as bundle:
                    bundle.extractall(target, filter="data")
        finally:
            os.remove(archive)
        if not llama_program():
            raise hf.HyperframesError("the llama.cpp download had no %s in it" % _EXE)
        if sys.platform != "win32":
            os.chmod(llama_program(), 0o755)
    if progress:
        progress("Downloading the LFM3-Omni 2B model (about 1.6 GB)")
    hf._download_pinned(os.path.join(home(), "models"), _model_downloads(), "LFM3-Omni model")


def _environment() -> dict:
    from classes import manim_local
    from classes.ffmpeg_cli import find_ffmpeg

    # Allowlisted, so the editor's tokens and keys never reach it; llama.cpp reads video through ffmpeg.
    env = {k: v for k, v in os.environ.items() if k.upper() in manim_local._ENV_KEEP}
    ffmpeg = find_ffmpeg("ffmpeg")
    if ffmpeg:
        path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
        env[path_key] = os.pathsep.join([os.path.dirname(os.path.abspath(ffmpeg)), env.get(path_key, "")])
    return env


def _json_answer(text: str) -> Dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def describe(path: str, media_type: str, prompt: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    """Describe one chunk; returns the model's JSON in the backend's schema."""
    mt = media_type if media_type in MEDIA_FLAG else "video"
    with tempfile.TemporaryDirectory(prefix="zenvi-describe-") as tmp:
        schema_file = os.path.join(tmp, "schema.json")
        with open(schema_file, "w", encoding="utf-8") as fh:
            json.dump(schema or {}, fh)
        argv = [llama_program(), "cli", "-m", _model_path(MODEL[0]), "-mm", _model_path(PROJECTOR[0]),
                MEDIA_FLAG[mt], path, "-sys", SYSTEM[mt], "-p", prompt, "--temp", "0",
                "--json-schema-file", schema_file]
        try:
            proc = subprocess.run(argv, cwd=tmp, env=_environment(), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=DESCRIBE_TIMEOUT,
                                  stdin=subprocess.DEVNULL, creationflags=hf._NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise hf.HyperframesError("describing %s took longer than %d s" % (os.path.basename(path), DESCRIBE_TIMEOUT))
        except OSError as exc:
            raise hf.HyperframesError("could not start llama.cpp: %s" % exc)
    if proc.returncode != 0:
        raise hf.HyperframesError("llama.cpp could not describe %s (exit code %d): %s"
                                  % (os.path.basename(path), proc.returncode, (proc.stderr or "")[-800:]))
    answer = _json_answer(proc.stdout or "")
    if not answer:
        log.warning("local describe gave no JSON for %s: %s", path, (proc.stdout or "")[-400:])
        raise hf.HyperframesError("the local model gave no description for %s" % os.path.basename(path))
    return answer
