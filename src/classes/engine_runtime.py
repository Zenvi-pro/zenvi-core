"""The engine bridge's runtime on this computer: Node.js and the zenvi CLI, installed on demand.

Nothing of it ships with the desktop. The first time the user asks for Zenvi
engine tools (AI Tools > Zenvi Engine Tools..., or Preferences > AI) it is
downloaded into ``~/.openshot_qt/engine``:

    node/          the official Node.js NODE_VERSION build -- only when no usable
                   Node is found: the local HyperFrames setup's private copy
                   (``~/.openshot_qt/hyperframes/node``) or one on PATH is used as is
    cli/           the zenvi CLI bundle: zenvi.mjs and what it loads (composite-worker.mjs,
                   resvg.wasm, fonts, engine assets)
    runtime.json   what is installed: the CLI's version and bridge protocol, where it came from

Where the CLI comes from (:func:`source`): ``ZENVI_ENGINE_SOURCE``, else the
``engine-runtime-source`` setting, else DEFAULT_SOURCE. Either

* a release manifest, a URL or a file ending in ``.json``::

      {"releases": [{"version": "0.1.0", "protocol": 2, "url": "zenvi-cli-0.1.0.tar.gz",
                     "sha256": "...", "size": 12345678}]}

  The newest release speaking this desktop's bridge protocol
  (``engine_bridge.PROTOCOL``) is installed; ``url`` may be relative to the
  manifest; the archive (``.tar.gz`` or ``.zip``) is kept only when its
  SHA-256 matches. ``scripts/package_engine_cli.py`` writes both from a
  zenvi-web build.
* a local build: zenvi-web's ``packages/cli/dist`` folder or the ``zenvi.mjs``
  in it, copied as is (development; no checksum, it is the user's own build).

Either way the CLI is started once before it is installed and must answer
this desktop's protocol, so a mismatched build is refused, never half used.
Remote sources must be https (http only from this computer). Everything is
staged next to its destination and swapped in at the end: a failed,
cancelled or refused install leaves the previous CLI untouched (a Node it
downloaded first stays: it is complete and checked on its own).

Nothing here imports Qt and every function blocks (network, disk, Node):
call it off the GUI thread. ``windows/engine_runtime_dialog.py`` is the UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import shutil
import socket
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from classes import engine_bridge, info
from classes.logger import log

# Node.js LTS, pinned with the SHA-256 nodejs.org publishes for each build
# (https://nodejs.org/dist/v24.22.1/SHASUMS256.txt): a download is checked
# against these, never against a list fetched next to it.
NODE_VERSION = "24.22.1"
NODE_DIST = "https://nodejs.org/dist/v%s/" % NODE_VERSION
NODE_SHA256 = {
    "darwin-arm64.tar.gz": "89c682593d76cd44233fa53b1563264e7de9cbb189cad169135c5b4fadfbc0a7",
    "darwin-x64.tar.gz": "d132557e7ee2b85af08b0c8c34338e65c6430f2238d01f727728ef42dc83b035",
    "linux-arm64.tar.gz": "33c37be234ba8bee041961047b19160f4d7d0f164e8e246f78bdd10feff148b3",
    "linux-x64.tar.gz": "eb6728e77c576a35162c122c45ccadf224d89ff00abc8dae46cc2897c6002d7c",
    "win-arm64.zip": "a0c807319fede24c4a0d8f84564c82a619bd313b16b10c007c383c2609e99171",
    "win-x64.zip": "d2046d28c2c3dcd211ac5145c0bf30e2047ef9dfb306693c8c6e2eccd8b95fa5",
}
NODE_DOWNLOAD_MB = 55
# No zenvi CLI release is published yet: until one is, the source is a setting.
DEFAULT_SOURCE = ""
FETCH_TIMEOUT = 60
BLOCK = 1 << 20

Progress = Callable[[str, int, int], None]   # (what is happening, bytes done, bytes total or 0)


class RuntimeInstallError(Exception):
    """The runtime could not be installed; the message says why and what to do."""


class InstallCancelled(RuntimeInstallError):
    """The user cancelled the install."""


def home() -> str:
    return os.path.join(info.USER_PATH, engine_bridge.RUNTIME_DIR)


def _runtime_file() -> str:
    return os.path.join(home(), "runtime.json")


def installed() -> dict:
    """runtime.json ({} when nothing is installed)."""
    return engine_bridge.installed_runtime()


def source(env=None) -> str:
    """Where the CLI comes from: ZENVI_ENGINE_SOURCE, the engine-runtime-source setting, DEFAULT_SOURCE."""
    env = env if env is not None else os.environ
    named = str(env.get("ZENVI_ENGINE_SOURCE", "")).strip()
    if not named and env is os.environ:
        named = engine_bridge._setting("engine-runtime-source")
    return named or DEFAULT_SOURCE


def status(env=None) -> dict:
    """What is in place. ``ready``: bridged tools can run (the same test the bridge makes)."""
    saved = installed()
    out = {"ready": False, "home": home(), "source": source(env), "cli": None, "cli_version": "",
           "protocol": engine_bridge.PROTOCOL, "node": None, "node_version": "", "problem": "",
           "node_needed": False, "managed": False}
    try:
        node, cli = engine_bridge.locate_cli(env)
        out.update(ready=True, cli=cli, node=node)
    except engine_bridge.BridgeUnavailable as exc:
        out["problem"] = str(exc)
    try:
        out["node"] = out["node"] or engine_bridge.find_node(env)
    except engine_bridge.BridgeUnavailable:
        out["node_needed"] = True
    if out["node"]:
        major = engine_bridge.node_major(out["node"])
        out["node_version"] = "v%d" % major if major is not None else ""
    out["managed"] = bool(out["cli"]) and os.path.abspath(out["cli"]) == engine_bridge.managed_cli()
    if out["managed"]:
        out["cli_version"] = str((saved.get("cli") or {}).get("version") or "")
    return out


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

_install_lock = threading.Lock()


def install(src: Optional[str] = None, progress: Optional[Progress] = None,
            cancel: Optional[threading.Event] = None, env=None) -> dict:
    """Install (or update) the runtime from *src* (default :func:`source`); returns the new :func:`status`.

    Downloads Node only when no usable one is found. Raises RuntimeInstallError
    (InstallCancelled when *cancel* is set) with nothing changed.
    """
    src = (src if src is not None else source(env)).strip()
    if not src:
        raise RuntimeInstallError(
            "No download source is set for Zenvi engine tools: set Preferences > AI > Zenvi Engine Tools "
            "Source to a release manifest URL or to a zenvi CLI build (zenvi-web packages/cli/dist).")
    report = progress or (lambda text, done, total: None)
    cancel = cancel or threading.Event()
    if not _install_lock.acquire(blocking=False):
        raise RuntimeInstallError("Zenvi engine tools are already being installed")
    try:
        os.makedirs(home(), exist_ok=True)
        staging = tempfile.mkdtemp(prefix="install-", dir=home())
        try:
            release = _resolve(src, report, cancel)
            node = _ensure_node(staging, report, cancel, env)
            cli_dir = _stage_cli(release, staging, report, cancel)
            report("Checking the zenvi CLI", 0, 0)
            version = _check_cli(node, os.path.join(cli_dir, engine_bridge.CLI_NAME))
            _check(cancel)
            # A running bridge holds the old build (Windows cannot move it then); the next call starts the new one.
            engine_bridge.set_client(None)
            _swap_in(cli_dir, engine_bridge.managed_cli_dir())
            _write_runtime({"cli": {"version": release.get("version") or version, "protocol": engine_bridge.PROTOCOL,
                                    "source": src, "sha256": release.get("sha256", ""),
                                    "installed": time.strftime("%Y-%m-%dT%H:%M:%S")}})
        except RuntimeInstallError:
            raise
        except OSError as exc:
            # A full disk, a folder that cannot be written: what is installed stays as it was.
            raise RuntimeInstallError(f"Could not install Zenvi engine tools into {home()}: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    finally:
        _install_lock.release()
    engine_bridge.forget_node_versions()
    log.info("Zenvi engine tools installed from %s", src)
    return status(env)


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise InstallCancelled("Cancelled: nothing was installed")


def _is_url(text: str) -> bool:
    return urllib.parse.urlparse(text).scheme in ("http", "https", "file")


def _plain_http_elsewhere(url: str) -> bool:
    """http:// to another computer: the manifest names the archive and its checksum, so it must come over TLS."""
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1")


def _resolve(src: str, report: Progress, cancel: threading.Event) -> dict:
    """The release to install: {"version", "url", "sha256", "size"} or, for a local build, {"path"}."""
    if not _is_url(src) and not src.lower().endswith(".json"):
        path = os.path.abspath(os.path.expanduser(src))
        folder = os.path.dirname(path) if os.path.isfile(path) else path
        if not os.path.isfile(os.path.join(folder, engine_bridge.CLI_NAME)):
            raise RuntimeInstallError(f"No zenvi CLI build at {src}: expected a folder holding "
                                      f"{engine_bridge.CLI_NAME} (zenvi-web: npm run build -w @zenvi/cli)")
        return {"path": folder}
    url = src if _is_url(src) else Path(os.path.abspath(os.path.expanduser(src))).as_uri()
    if _plain_http_elsewhere(url):
        raise RuntimeInstallError(f"{src}: Zenvi engine tools are only downloaded over https (or from this computer)")
    report("Reading the Zenvi engine tools release list", 0, 0)
    try:
        manifest = json.loads(_read(url, cancel))
    except ValueError as exc:
        raise RuntimeInstallError(f"{src} is not a Zenvi engine tools release manifest (not JSON)") from exc
    releases = manifest.get("releases") if isinstance(manifest, dict) else None
    if not isinstance(releases, list):
        raise RuntimeInstallError(f"{src} is not a Zenvi engine tools release manifest (no releases)")
    usable = [r for r in releases if isinstance(r, dict) and r.get("protocol") == engine_bridge.PROTOCOL
              and r.get("url") and r.get("sha256")]
    if not usable:
        spoken = sorted({str(r.get("protocol")) for r in releases if isinstance(r, dict)})
        raise RuntimeInstallError(
            f"{src} has no zenvi CLI for this Zenvi (bridge protocol {engine_bridge.PROTOCOL}; it offers "
            f"{', '.join(spoken) or 'none'}): update Zenvi, or point the source at a matching build")
    best = max(usable, key=lambda r: _version_key(str(r.get("version") or "")))
    archive = urllib.parse.urljoin(url, str(best["url"]))
    if _plain_http_elsewhere(archive):
        raise RuntimeInstallError(f"{archive}: Zenvi engine tools are only downloaded over https (or from this computer)")
    return dict(best, url=archive)


def _version_key(version: str) -> tuple:
    parts = []
    for piece in version.split("-")[0].split("."):
        parts.append(int(piece) if piece.isdigit() else -1)
    return tuple(parts)


def _open(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Zenvi"}),
                                  timeout=FETCH_TIMEOUT)


def _offline(url: str, exc: Exception) -> RuntimeInstallError:
    if isinstance(exc, urllib.error.HTTPError):
        return RuntimeInstallError(f"Could not download {url}: the server answered {exc.code} {exc.reason}")
    host = urllib.parse.urlparse(url).netloc or url
    reason = getattr(exc, "reason", exc)
    return RuntimeInstallError(
        f"Could not reach {host} ({reason}). Zenvi engine tools need a one-time download: connect to the "
        "internet and try again, or set Preferences > AI > Zenvi Engine Tools Source to a local build.")


def _read(url: str, cancel: threading.Event) -> bytes:
    _check(cancel)
    try:
        with _open(url) as response:
            return response.read()
    except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
        raise _offline(url, exc) from exc


def _download(url: str, target: str, sha256: str, label: str, report: Progress, cancel: threading.Event,
              size: int = 0) -> None:
    """Stream *url* to *target*, checked against *sha256*; nothing is left at *target* when it fails."""
    digest = hashlib.sha256()
    done = 0
    try:
        with _open(url) as response, open(target, "xb") as out:
            total = int(response.headers.get("Content-Length") or 0) or size
            report(label, 0, total)
            for block in iter(lambda: response.read(BLOCK), b""):
                _check(cancel)
                digest.update(block)
                out.write(block)
                done += len(block)
                report(label, done, total)
    except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
        _remove(target)
        raise _offline(url, exc) from exc
    except BaseException:
        _remove(target)
        raise
    if digest.hexdigest() != sha256.lower():
        _remove(target)
        raise RuntimeInstallError(f"The download of {os.path.basename(target)} did not match its published "
                                  "checksum; nothing was installed. Try again.")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def node_asset(platform: Optional[str] = None, machine: Optional[str] = None) -> str:
    """Suffix of the Node.js archive for this computer, e.g. ``darwin-arm64.tar.gz``."""
    platform = platform or sys.platform
    arch = "arm64" if (machine or _platform.machine()).lower() in ("arm64", "aarch64") else "x64"
    if platform == "win32":
        return "win-%s.zip" % arch
    return "%s-%s.tar.gz" % ("darwin" if platform == "darwin" else "linux", arch)


def _ensure_node(staging: str, report: Progress, cancel: threading.Event, env=None) -> str:
    """A usable Node: the one found (HyperFrames' private copy, PATH...), else the pinned build installed."""
    try:
        return engine_bridge.find_node(env)
    except engine_bridge.BridgeUnavailable:
        pass
    asset = node_asset()
    digest = NODE_SHA256.get(asset)
    if not digest:
        raise RuntimeInstallError(f"Node.js {NODE_VERSION} is not published for this computer ({asset})")
    filename = "node-v%s-%s" % (NODE_VERSION, asset)
    archive = os.path.join(staging, filename)
    _download(NODE_DIST + filename, archive, digest, f"Downloading Node.js {NODE_VERSION}", report, cancel,
              NODE_DOWNLOAD_MB << 20)
    report(f"Unpacking Node.js {NODE_VERSION}", 0, 0)
    unpacked = _unpack(archive, os.path.join(staging, "node"))
    _remove(archive)
    _check(cancel)
    _swap_in(unpacked, engine_bridge.managed_node_dir())
    engine_bridge.forget_node_versions()
    node = engine_bridge.managed_node()
    if not os.path.isfile(node):
        raise RuntimeInstallError(f"The Node.js {NODE_VERSION} archive did not hold {os.path.basename(node)}")
    return node


def _unpack(archive: str, dest: str) -> str:
    """Extract *archive* into *dest*; the folder holding its content (an archive's single top folder)."""
    os.makedirs(dest)
    try:
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(dest)          # zipfile drops absolute and '..' member paths
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(dest, filter="data")
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise RuntimeInstallError(f"Could not unpack {os.path.basename(archive)}: {exc}") from exc
    entries = os.listdir(dest)
    if len(entries) == 1 and os.path.isdir(os.path.join(dest, entries[0])):
        return os.path.join(dest, entries[0])
    return dest


def _stage_cli(release: dict, staging: str, report: Progress, cancel: threading.Event) -> str:
    """The CLI bundle, unpacked or copied under *staging*; the folder holding zenvi.mjs."""
    if "path" in release:
        report("Copying the zenvi CLI build", 0, 0)
        target = os.path.join(staging, "cli")
        shutil.copytree(release["path"], target, ignore=shutil.ignore_patterns("node_modules", "*.map"))
        return target
    url = release["url"]
    name = os.path.basename(urllib.parse.urlparse(url).path) or "zenvi-cli.tar.gz"
    archive = os.path.join(staging, name)
    _download(url, archive, str(release["sha256"]), f"Downloading the zenvi CLI {release.get('version', '')}".strip(),
              report, cancel, int(release.get("size") or 0))
    report("Unpacking the zenvi CLI", 0, 0)
    folder = _unpack(archive, os.path.join(staging, "cli"))
    _remove(archive)
    if not os.path.isfile(os.path.join(folder, engine_bridge.CLI_NAME)):
        raise RuntimeInstallError(f"The zenvi CLI archive {name} holds no {engine_bridge.CLI_NAME}")
    return folder


def _check_cli(node: str, cli: str) -> str:
    """Start the staged CLI once: it must answer this desktop's bridge protocol. Its engine version."""
    client = engine_bridge.EngineBridgeClient(node, cli, "oneshot")
    try:
        client.tools()
    except engine_bridge.BridgeUnavailable as exc:
        raise RuntimeInstallError(f"The zenvi CLI was not installed: {exc}") from exc
    return client.engine_version


def _swap_in(staged: str, target: str) -> None:
    """Move *staged* to *target*, replacing what is there only once the new one is in place."""
    old = ""
    if os.path.lexists(target):
        old = tempfile.mkdtemp(prefix=os.path.basename(target) + ".old-", dir=os.path.dirname(target))
        os.rmdir(old)
        os.replace(target, old)
    try:
        os.replace(staged, target)
    except OSError:
        if old:
            os.replace(old, target)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)


def _write_runtime(values: dict) -> None:
    data = dict(installed(), **values)
    partial = _runtime_file() + ".partial"
    with open(partial, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(partial, _runtime_file())   # a reader sees the old file or the new one, never half
