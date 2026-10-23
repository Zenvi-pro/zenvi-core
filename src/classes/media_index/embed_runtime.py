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
NODE_MAJOR = 22
NODE_DIST = "https://nodejs.org/dist/latest-v%d.x/" % NODE_MAJOR
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
    """Download the portable Node.js for this computer, checked against nodejs.org's published checksum."""
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    asset = f"win-{arch}.zip" if sys.platform == "win32" else f"{'darwin' if sys.platform == 'darwin' else 'linux'}-{arch}.tar.gz"
    with _fetch(NODE_DIST + "SHASUMS256.txt") as response:
        sums = response.read().decode("utf-8", "replace")
    row = next((line.split() for line in sums.splitlines() if line.strip().endswith(asset)), None)
    if not row:
        raise RuntimeError(f"nodejs.org publishes no Node.js {NODE_MAJOR} build for {asset}")
    os.makedirs(home(), exist_ok=True)
    archive, staging = os.path.join(home(), row[1]), os.path.join(home(), "node.partial")
    try:
        sha = hashlib.sha256()
        with _fetch(NODE_DIST + row[1]) as response, open(archive, "wb") as out:
            for block in iter(lambda: response.read(1 << 20), b""):
                sha.update(block)
                out.write(block)
        if sha.hexdigest() != row[0]:
            raise RuntimeError("the Node.js download did not match its published checksum")
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
        if _installed_version() != TRANSFORMERS.rsplit("@", 1)[1]:
            log.info("Installing the search model runtime (%s)", TRANSFORMERS)
            os.makedirs(home(), exist_ok=True)
            with open(os.path.join(home(), "package.json"), "w", encoding="utf-8") as fh:
                json.dump({"name": "zenvi-embedder", "private": True, "type": "module"}, fh)
            proc = subprocess.run([*_npm(node), "install", "--save-exact", "--no-audit", "--no-fund", TRANSFORMERS], cwd=home(),
                                  capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                  env={**os.environ, "PATH": os.path.dirname(node) + os.pathsep + os.environ.get("PATH", "")})
            if proc.returncode != 0:
                raise RuntimeError("could not install the search model runtime: " + (proc.stderr or proc.stdout)[-400:])
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker")
        for name in WORKER_FILES:
            shutil.copyfile(os.path.join(here, name), os.path.join(home(), name))
        return node


def worker_command() -> Tuple[List[str], Dict[str, str]]:
    node = ensure()
    os.makedirs(model_dir(), exist_ok=True)
    return [node, os.path.join(home(), "embed_worker.mjs")], {"ZENVI_MODEL_DIR": model_dir()}
