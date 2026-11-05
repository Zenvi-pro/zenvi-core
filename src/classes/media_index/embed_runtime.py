"""
 @file
 @brief What the local search model needs on this computer, set up on first use.

 Everything lives under ``~/.openshot_qt`` and nothing is installed system-wide:

     embedder/node/          a portable Node.js, unless a recent one is already here
     embedder/node_modules/  Transformers.js, pinned (it brings onnxruntime with WebGPU)
     embedder/*.mjs, *.json  the worker, copied from the app on every start
     models/embeddinggemma-2/ the model, downloaded by the worker the first time it starts (q4, about 470 MB)

 Deleting those folders removes it; the next index sets it up again.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile
from typing import Dict, List, Optional, Tuple

from classes import info
from classes.logger import log

TRANSFORMERS = "@huggingface/transformers@4.3.1"
NODE_VERSION = "22.23.4"
NODE_MAJOR = int(NODE_VERSION.split(".", 1)[0])
# Checksums from nodejs.org/dist/v22.23.4/SHASUMS256.txt, pinned here so the download is not "whatever latest is today".
NODE_SHA256 = {
    "darwin-arm64.tar.gz": "c9e1dbc748b33ed2d14f42445191ecaf14c56259c7a9a26d0042c49ec2b66428",
    "darwin-x64.tar.gz": "efcc2b0db6f94bf53eddb038fe2b370277a042bd29e4f19b8f8e23598795b819",
    "linux-arm64.tar.gz": "f7a1e486dbc8b286847815b477d4fb5e9fb963ed4489c3e1588853d685f589f9",
    "linux-x64.tar.gz": "b8492284c76a54baf58d61668dd0e41ee0fc98adabf9503d30f05d08f55b4406",
    "win-arm64.zip": "a8b8cd79cbbb3062435f449591d98dd63730bbbf30ce2d7cc3a6376c236cb111",
    "win-x64.zip": "cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20",
}
WORKER_FILES = ("embed_worker.mjs", "embed_reference.json")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_setup_lock = threading.Lock()


def home() -> str:
    return os.path.join(info.USER_PATH, "embedder")


def model_dir() -> str:
    return os.path.join(info.USER_PATH, "models", "embeddinggemma-2")


def _node_in(folder: str) -> str:
    return os.path.join(folder, "node.exe") if sys.platform == "win32" else os.path.join(folder, "bin", "node")


def _recent(node: str) -> bool:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW).stdout
        return int(out.strip().lstrip("v").split(".")[0]) >= NODE_MAJOR
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def find_node() -> Optional[str]:
    """Our own Node, the one HyperFrames installed, or a recent system one."""
    for candidate in (_node_in(os.path.join(home(), "node")), _node_in(os.path.join(info.USER_PATH, "hyperframes", "node")),
                      shutil.which("node")):
        if candidate and os.path.isfile(candidate) and _recent(candidate):
            return candidate
    return None


def _fetch(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Zenvi"}), timeout=120)


def install_node() -> str:
    """Download the pinned portable Node.js for this computer and check it against the checksum in this file."""
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    kind = f"win-{arch}.zip" if sys.platform == "win32" else f"{'darwin' if sys.platform == 'darwin' else 'linux'}-{arch}.tar.gz"
    expect = NODE_SHA256.get(kind)
    if not expect:
        raise RuntimeError(f"no pinned Node.js {NODE_VERSION} checksum for {kind}")
    filename = f"node-v{NODE_VERSION}-{kind}"
    os.makedirs(home(), exist_ok=True)
    archive, staging = os.path.join(home(), filename), os.path.join(home(), "node.partial")
    try:
        sha = hashlib.sha256()
        with _fetch(f"https://nodejs.org/dist/v{NODE_VERSION}/{filename}") as response, open(archive, "wb") as out:
            for block in iter(lambda: response.read(1 << 20), b""):
                sha.update(block)
                out.write(block)
        if sha.hexdigest() != expect:
            raise RuntimeError("the Node.js download did not match its pinned checksum")
        shutil.rmtree(staging, ignore_errors=True)
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(staging)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(staging, filter="data")
        target = os.path.join(home(), "node")
        shutil.rmtree(target, ignore_errors=True)
        os.replace(os.path.join(staging, os.listdir(staging)[0]), target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.exists(archive):
            os.remove(archive)
    return _node_in(os.path.join(home(), "node"))


def _npm(node: str) -> List[str]:
    base = os.path.dirname(node)
    for root in (os.path.join(base, "node_modules", "npm", "bin"), os.path.join(base, "..", "lib", "node_modules", "npm", "bin")):
        script = os.path.join(root, "npm-cli.js")
        if os.path.isfile(script):
            return [node, os.path.normpath(script)]
    found = shutil.which("npm")
    if not found:
        raise RuntimeError(f"npm was not found next to Node.js at {node}")
    return [found]


def needs_download() -> bool:
    """True when the first index still has to fetch Node, the runtime, or the model (about 1.2 GB)."""
    if find_node() is None or _installed_version() != TRANSFORMERS.rsplit("@", 1)[1]:
        return True
    try:
        return not any(os.scandir(model_dir()))
    except OSError:
        return True


def _installed_version() -> str:
    try:
        with open(os.path.join(home(), "node_modules", "@huggingface", "transformers", "package.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "")
    except (OSError, ValueError):
        return ""


def ensure() -> str:
    """Node and Transformers.js in place, the worker copied in; returns the Node executable."""
    with _setup_lock:
        node = find_node() or install_node()
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker")
        if _installed_version() != TRANSFORMERS.rsplit("@", 1)[1]:
            log.info("Installing the search model runtime (%s)", TRANSFORMERS)
            os.makedirs(home(), exist_ok=True)
            for name in ("package.json", "package-lock.json"):
                shutil.copyfile(os.path.join(here, name), os.path.join(home(), name))
            # npm ci installs the pinned lockfile. onnxruntime-node's postinstall fetches its native binary, so scripts stay on.
            proc = subprocess.run([*_npm(node), "ci", "--no-audit", "--no-fund"], cwd=home(),
                                  capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                  env={**os.environ, "PATH": os.path.dirname(node) + os.pathsep + os.environ.get("PATH", "")})
            if proc.returncode != 0:
                raise RuntimeError("could not install the search model runtime: " + (proc.stderr or proc.stdout)[-400:])
        for name in WORKER_FILES:
            shutil.copyfile(os.path.join(here, name), os.path.join(home(), name))
        return node


def worker_command() -> Tuple[List[str], Dict[str, str]]:
    node = ensure()
    os.makedirs(model_dir(), exist_ok=True)
    return [node, os.path.join(home(), "embed_worker.mjs")], {"ZENVI_MODEL_DIR": model_dir()}
