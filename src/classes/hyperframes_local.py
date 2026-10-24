"""HyperFrames on this computer: the CLI, its skills and the projects the assistant authors.

Motion graphics, product demos and captions are HTML compositions rendered by
the open-source ``hyperframes`` CLI (Node + headless Chrome + ffmpeg). Zenvi
keeps a private install under ``~/.openshot_qt/hyperframes``:

    node/             a portable Node.js, unless a recent one is already installed
    speech/           the Whistle speech recogniser: one small engine and one model file
    matting/          the RVM model that cuts a speaker out of a clip (run by hyperframes_rvm.cjs)
    node_modules/     the pinned CLI (``npm install``)
    .agents/skills/   the HyperFrames skills the assistant follows (read-only)
    .cache/           the Chrome build, fonts and models the CLI downloads
    projects/<name>/  one folder per composition the assistant writes
    install.json      what was found or installed, so later runs skip the search

Setup needs no administrator rights and changes nothing outside that folder:
no installer runs, and PATH is only set for the child processes started here.
Deleting the folder removes everything.

Nothing here imports Qt. The assistant's model runs in the cloud, so what it may
execute on the user's machine is deliberately narrow: the CLI's authoring
subcommands, ffmpeg/ffprobe, and the scripts that ship inside the installed
skills. It never gets a shell, and no argument may point outside the project.

That is a limit on commands, not a browser sandbox: a composition is a web page,
and the headless Chrome that checks and renders it loads whatever the page
references (GSAP and fonts from a CDN are normal), as any HTML renderer does.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile
from typing import Callable, Iterable, List, Optional, Tuple

from classes import info
from classes.logger import log

CLI_VERSION = "0.8.115"
NODE_MAJOR = 22                       # the CLI's package.json: engines.node >= 22
NODE_DIST = "https://nodejs.org/dist/latest-v%d.x/" % NODE_MAJOR
SKILLS_SOURCE = "heygen-com/hyperframes"
# Helpers the embedded-captions skill's preview/measure scripts require.
CAPTION_PACKAGES = ("sharp@0.35.3", "puppeteer@25.8.0", "gsap@3.15.0")

# Speech-to-text for captions: Cactus Whistle (Apache-2.0), a prebuilt engine and a 17 MB
# model. Both are pinned to a revision and a SHA-256, like the Node.js download.
SPEECH_SOURCE = "https://huggingface.co/Cactus-Compute/%s/resolve/%s/%s"
SPEECH_MODEL = ("whistle", "ca5287601bef25af26dcf2e1b2bdc0843a7c19e5", "whistle.cact",
                "b6e02f048568ac5d01a2042556c658061e699acbc0aa2a1439f52f3d461dffeb")
SPEECH_ENGINE_REVISION = "93e88ede867e4311c39151285e74df513e4b835b"
SPEECH_ENGINES = {          # (platform, architecture) -> (path in Cactus-Compute/needle3, sha256)
    ("win32", "x64"): ("windows-x86_64/needle.exe", "47cd411a022d22e393d87ce091848fd446e6d25df2ce31b12a7328337557c85b"),
    ("win32", "arm64"): ("windows-arm64/needle.exe", "5bc31642787b85cd64ac56d75368ec0b23f595ea6cb8bdfd091aa2575971cbd8"),
    ("darwin", "arm64"): ("macos-arm64/needle", "1e7e698b492904a92496e2b168844a3101e671928ffa6372970d897512251c48"),
    ("linux", "x64"): ("linux-x86_64/needle", "3531889c4d677acc47905b21c79741b52fd9af2c54ba177f54f83c0bc12e84c7"),
    ("linux", "arm64"): ("linux-arm64/needle", "ab21ca5272e7699ed4972246d95e75dd09a7b81f5ef8b8a6262e6ff57c600593"),
}
# Cutting a speaker out: Robust Video Matting (GPL-3.0, like Zenvi), 15 MB, pinned like the speech files.
# It runs on the ONNX runtime HyperFrames installs for its own (U^2-Net) cut-out, in the same folder.
RVM_MODEL = ("https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp32.onnx",
             "rvm_mobilenetv3_fp32.onnx", "88d4531297118f595bf2fd60f6f566aec2e559393802d1f436c380f0cbbd2828")
ORT_VERSION = "1.21.1"                # hyperframes@CLI_VERSION's optionalDependencies pin
MATTERS = ("rvm", "u2net")
RVM_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hyperframes_rvm.cjs")
PARAKEET_INSTALL = ("models", "install", "parakeet")   # prebuilt sherpa-onnx runtime + model, by the CLI
SPEECH_THREADS = 8                    # the engine's own default stops at 4; 8 nearly halved the time on a laptop
SPEECH_TIMEOUT = 3600

PROGRAMS = ("hyperframes", "node", "bash", "ffmpeg", "ffprobe")
# Authoring, checking and rendering. Not here: anything that uploads (publish,
# cloud, lambda, cloudrun), signs in (auth), serves (preview, present) or
# changes the install (upgrade, skills, telemetry, feedback).
CLI_SUBCOMMANDS = frozenset({
    "init", "add", "catalog", "capture", "render", "lint", "check", "validate", "beats", "inspect",
    "keyframes", "snapshot", "media-treatment", "grade-compare", "compare", "info", "compositions",
    "docs", "doctor", "transcribe", "remove-background",
})
SCRIPT_PROGRAMS = ("node", "bash")
FILTER_PROGRAMS = ("ffmpeg", "ffprobe")
_FILE_FED_OPTIONS = ("-filter_script", "-filter_complex_script", "-/", "-safe")   # paths we never see

MAX_OUTPUT_CHARS = 12000
DEFAULT_TIMEOUT = 900
INSTALL_TIMEOUT = 1800

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_FILTER_SPLIT_RE = re.compile(r"""[=:,;'"\[\]\s]+""")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0


class HyperframesError(Exception):
    """Something the caller got wrong, or a part of the install that is missing."""


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------

def home() -> str:
    return os.path.join(info.USER_PATH, "hyperframes")


def skills_dir() -> str:
    return os.path.join(home(), ".agents", "skills")


def cli_script() -> str:
    return os.path.join(home(), "node_modules", "hyperframes", "dist", "cli.js")


def project_dir(project: str) -> str:
    name = str(project or "").strip()
    if not _PROJECT_RE.match(name):
        raise HyperframesError(
            "project must be a short lowercase name (a-z, 0-9, '-' or '_'), e.g. 'intro-title'; got %r" % project)
    root = os.path.join(home(), "projects", name)
    expected = os.path.join(os.path.realpath(home()), "projects", name)
    if os.path.normcase(os.path.realpath(root)) != os.path.normcase(expected):      # a link to somewhere else
        raise HyperframesError("project folder %r resolves outside Zenvi's projects folder" % name)
    return root


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:                      # different drives
        return False


def _is_absolute(path: str) -> bool:
    return os.path.isabs(path) or path[:1] in ("/", "\\") or bool(_DRIVE_RE.match(path))


def resolve_path(project: str, path: str, writable: bool = False) -> str:
    """Real path of *path* in the project, or under ``skills/`` (read-only)."""
    rel = str(path or "").strip().replace("\\", "/")
    if not rel or _is_absolute(rel):
        raise HyperframesError("path must be relative to the project folder (or start with skills/): %r" % path)
    if rel == "skills" or rel.startswith("skills/"):
        if writable:
            raise HyperframesError("the installed skills are read-only; write inside the project instead")
        root, rel = skills_dir(), rel[len("skills"):].lstrip("/")
    else:
        root = project_dir(project)
    full = os.path.normpath(os.path.join(root, rel))
    if not _inside(full, root):
        raise HyperframesError("path leaves the project folder: %r" % path)
    return full


def list_files(project: str, pattern: str = "**/*", limit: int = 400) -> List[str]:
    """Project-relative (or ``skills/``-prefixed) paths matching *pattern*."""
    pat = str(pattern or "**/*").strip().replace("\\", "/")
    in_skills = pat == "skills" or pat.startswith("skills/")
    root = skills_dir() if in_skills else project_dir(project)
    rel = pat[len("skills"):].lstrip("/") if in_skills else pat
    if _is_absolute(rel) or ".." in rel.split("/"):
        raise HyperframesError("pattern must stay inside the project folder: %r" % pattern)
    out = []
    for hit in sorted(glob.glob(os.path.join(root, rel or "*"), recursive=True)):
        if "node_modules" in hit.replace("\\", "/").split("/") or not _inside(hit, root):
            continue
        shown = os.path.relpath(hit, root).replace("\\", "/")
        out.append(("skills/" if in_skills else "") + shown + ("/" if os.path.isdir(hit) else ""))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# What is installed
# ---------------------------------------------------------------------------

def _install_file() -> str:
    return os.path.join(home(), "install.json")


_install_file_lock = threading.Lock()


def _remembered() -> dict:
    try:
        with open(_install_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(**values) -> None:
    """Save found/installed paths so the next run does not search again."""
    os.makedirs(home(), exist_ok=True)
    with _install_file_lock:                            # the dock and setup can both write it
        data = _remembered()
        data.update({k: v for k, v in values.items() if v})
        partial = _install_file() + ".partial"
        with open(partial, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(partial, _install_file())            # a reader sees the old file or the new one, never half


def _private_node() -> str:
    if sys.platform == "win32":
        return os.path.join(home(), "node", "node.exe")
    return os.path.join(home(), "node", "bin", "node")


def _usable_system_node() -> Optional[str]:
    """A Node.js already on this computer, if it is new enough for the CLI."""
    found = shutil.which("node")
    if not found:
        return None
    try:
        out = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW).stdout
        major = int(out.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if major < NODE_MAJOR:
        return None
    remember(node=found)
    return found


def _bash_dirs() -> List[str]:
    if sys.platform != "win32":
        return ["/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"]
    # Git for Windows. (System32's bash.exe is the WSL launcher, not a shell for scripts.)
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    return [os.path.join(p, "Git", "bin") for p in roots if p] + [r"C:\msys64\usr\bin"]


def find_program(name: str) -> Optional[str]:
    """Absolute path of node / bash / ffmpeg / ffprobe, or None."""
    if name in ("ffmpeg", "ffprobe"):
        from classes.ffmpeg_cli import find_ffmpeg
        return find_ffmpeg(name)
    saved = _remembered().get(name)
    if saved and os.path.isfile(saved):
        return saved
    if name == "node":
        return _private_node() if os.path.isfile(_private_node()) else _usable_system_node()
    exe = name + ".exe" if sys.platform == "win32" else name
    for directory in _bash_dirs():
        candidate = os.path.join(directory, exe)
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which(name)
    if found and sys.platform == "win32" and "system32" in found.lower():
        return None
    return found


def speech_engine() -> str:
    return os.path.join(home(), "speech", "needle.exe" if sys.platform == "win32" else "needle")


def speech_model() -> str:
    return os.path.join(home(), "speech", SPEECH_MODEL[2])


_verified: dict = {}                  # path -> (size, mtime_ns, sha256) of the last file hashed there
# Held to hash, replace or run the speech files; reentrant because transcribe hashes while holding it.
# (On Windows a file being hashed cannot be replaced, so nothing hashes while an install swaps them.)
_speech_install_lock = threading.RLock()


def _sha256(path: str, fresh: bool = False) -> str:
    """SHA-256 of a regular file ('' for anything else); remembered until the file changes.

    *fresh* reads the file again whatever was remembered: size and time can be made to match.
    """
    try:
        with _speech_install_lock:
            if os.path.islink(path) or not os.path.isfile(path):
                return ""
            stat = os.stat(path)
            seen = _verified.get(path)
            if fresh or not seen or seen[:2] != (stat.st_size, stat.st_mtime_ns):
                sha = hashlib.sha256()
                with open(path, "rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        sha.update(block)
                seen = _verified[path] = (stat.st_size, stat.st_mtime_ns, sha.hexdigest())
            return seen[2]
    except OSError:
        return ""


def speech_ready(fresh: bool = False) -> bool:
    """Both files are there and are the pinned ones: the engine is a program Zenvi runs."""
    try:
        return all(_sha256(os.path.join(home(), "speech", name), fresh) == digest
                   for _url, name, digest in _speech_downloads())
    except HyperframesError:
        return False


def speech_supported(platform: Optional[str] = None, machine: Optional[str] = None) -> bool:
    """Whether Whistle has an engine for this computer. Where it has none (Intel Macs), Parakeet is the recogniser."""
    try:
        _speech_downloads(platform, machine)
        return True
    except HyperframesError:
        return False


RECOGNISERS = ("whistle", "parakeet")


def recogniser() -> str:
    """The recogniser captions use: the user's choice, Whistle by default, Parakeet where Whistle has no engine."""
    if not speech_supported():
        return "parakeet"
    chosen = _remembered().get("recogniser")
    return chosen if chosen in RECOGNISERS else "whistle"


def choose_recogniser(name: str) -> None:
    if name not in RECOGNISERS:
        raise HyperframesError("recogniser must be one of %s; got %r" % (", ".join(RECOGNISERS), name))
    remember(recogniser=name)


def matting() -> str:
    """How a speaker is cut out: ``rvm`` (default) or HyperFrames' own ``u2net``."""
    chosen = _remembered().get("matting")
    return chosen if chosen in MATTERS else "rvm"


def choose_matting(name: str) -> None:
    if name not in MATTERS:
        raise HyperframesError("matting must be one of %s; got %r" % (", ".join(MATTERS), name))
    remember(matting=name)


def rvm_model() -> str:
    return os.path.join(home(), "matting", RVM_MODEL[1])


def _ort_dir() -> str:
    return os.path.join(home(), ".cache", "hyperframes", "optional", "onnxruntime-node@" + ORT_VERSION)


def _ort_module() -> str:
    return os.path.join(_ort_dir(), "node_modules", "onnxruntime-node")


_ort_install_lock = threading.Lock()


def _ort_installed() -> bool:
    """The module itself, as ``require`` and HyperFrames' loader see it (an interrupted npm leaves only the prefix)."""
    return os.path.isfile(os.path.join(_ort_module(), "package.json"))


def rvm_ready(fresh: bool = False) -> bool:
    return _sha256(rvm_model(), fresh) == RVM_MODEL[2] and _ort_installed()


def _u2net_model() -> str:
    """Where ``hyperframes remove-background`` keeps its model (it downloads it on first use)."""
    return os.path.join(home(), ".cache", "hyperframes", "background-removal", "models", "u2net_human_seg.onnx")


def _parakeet_model() -> str:
    """Where ``hyperframes models install parakeet`` puts the model (under the CLI's own cache)."""
    return os.path.join(home(), ".cache", "hyperframes", "parakeet", "parakeet-tdt-0.6b-v3-int8", "encoder.int8.onnx")


def _cli_version() -> Optional[str]:
    if not os.path.isfile(cli_script()):
        return None
    try:
        with open(os.path.join(home(), "node_modules", "hyperframes", "package.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "") or "unknown"
    except (OSError, ValueError):
        return "unknown"


def status() -> dict:
    """What is in place. ``ready`` means a composition can be linted and rendered."""
    saved = _remembered()
    node, cli = find_program("node"), _cli_version()
    browser = saved.get("browser") if saved.get("browser") and os.path.exists(saved["browser"]) else None
    skills = len(glob.glob(os.path.join(skills_dir(), "*", "SKILL.md")))
    captions = all(os.path.isdir(os.path.join(home(), "node_modules", p.split("@")[0])) for p in CAPTION_PACKAGES)
    return {
        "ready": bool(node and cli and browser),
        "home": home(),
        "node": node,
        "cli": cli,
        "browser": browser,
        "skills": skills,
        "bash": find_program("bash"),
        "ffmpeg": find_program("ffmpeg"),
        "caption_helpers": captions,
        "speech": speech_ready(),
        "parakeet": os.path.isfile(_parakeet_model()),
        "recogniser": recogniser(),
        "matting": matting(),
        "rvm": rvm_ready(),
        "u2net": os.path.isfile(_u2net_model()),
    }


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def _node_asset(platform: Optional[str] = None, machine: Optional[str] = None) -> str:
    """Suffix of the portable Node.js archive for this computer, e.g. ``win-x64.zip``."""
    platform = platform or sys.platform
    arch = "arm64" if (machine or _platform.machine()).lower() in ("arm64", "aarch64") else "x64"
    if platform == "win32":
        return "win-%s.zip" % arch
    return "%s-%s.tar.gz" % ("darwin" if platform == "darwin" else "linux", arch)


def install_plan(st: dict, platform: Optional[str] = None, captions: bool = False,
                 parakeet: bool = False) -> List[dict]:
    """The steps still needed, in order. ``argv`` starting with npm/npx/hyperframes
    is resolved against the Node found at run time (step one may download it)."""
    steps: List[dict] = []
    if not st.get("node"):
        steps.append({"id": "node", "label": "Download Node.js %d into Zenvi's folder (%s, no installer)"
                                             % (NODE_MAJOR, _node_asset(platform))})
    if st.get("cli") != CLI_VERSION:
        steps.append({"id": "cli", "label": "Install the HyperFrames CLI %s" % CLI_VERSION,
                      "argv": ["npm", "install", "hyperframes@" + CLI_VERSION, "--no-audit", "--no-fund"]})
    if not st.get("browser"):
        steps.append({"id": "browser", "label": "Download the Chrome build HyperFrames renders with",
                      "argv": ["hyperframes", "browser", "ensure"]})
    if not st.get("skills"):
        steps.append({"id": "skills", "label": "Install the HyperFrames skills",
                      "argv": ["npx", "--yes", "skills", "add", SKILLS_SOURCE, "--agent", "universal", "--copy",
                               "--full-depth", "--yes"]})
    if captions and not st.get("caption_helpers"):
        steps.append({"id": "captions", "label": "Install the caption preview helpers",
                      "argv": ["npm", "install", "--save-exact", "--no-audit", "--no-fund", *CAPTION_PACKAGES]})
    whistle = speech_supported(platform)
    if whistle and not st.get("speech"):
        steps.append({"id": "speech", "label": "Download the speech recogniser captions use (Whistle, 18 MB)"})
    if (parakeet or not whistle or st.get("recogniser") == "parakeet") and not st.get("parakeet"):
        steps.append({"id": "parakeet", "label": "Download the %s (Parakeet, about 900 MB)" % (
            "higher-accuracy speech recogniser" if whistle else "speech recogniser captions use"),
                      "argv": ["hyperframes", *PARAKEET_INSTALL]})
    return steps


def _fetch(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Zenvi"}), timeout=120)


def install_node() -> str:
    """Download the portable Node.js for this computer into ``node/``; returns the executable."""
    asset = _node_asset()
    with _fetch(NODE_DIST + "SHASUMS256.txt") as response:
        sums = response.read().decode("utf-8", "replace")
    row = next((line.split() for line in sums.splitlines() if line.strip().endswith(asset)), None)
    if not row:
        raise HyperframesError("nodejs.org publishes no Node.js %d build for %s" % (NODE_MAJOR, asset))
    digest, filename = row[0], row[1]
    archive, staging = os.path.join(home(), filename), os.path.join(home(), "node.partial")
    try:
        sha = hashlib.sha256()
        with _fetch(NODE_DIST + filename) as response, open(archive, "wb") as out:
            for block in iter(lambda: response.read(1 << 20), b""):
                sha.update(block)
                out.write(block)
        if sha.hexdigest() != digest:
            raise HyperframesError("the Node.js download did not match its published checksum; try again")
        shutil.rmtree(staging, ignore_errors=True)
        if filename.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(staging)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(staging, filter="data")
        target = os.path.join(home(), "node")
        shutil.rmtree(target, ignore_errors=True)
        os.replace(os.path.join(staging, os.listdir(staging)[0]), target)    # the archive's single top folder
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if os.path.exists(archive):
            os.remove(archive)
    remember(node=_private_node())
    return _private_node()


def _speech_downloads(platform: Optional[str] = None, machine: Optional[str] = None) -> List[tuple]:
    """(url, file name, sha256) of the speech engine for this computer and of the model."""
    platform, machine = platform or sys.platform, (machine or _platform.machine()).lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "amd64": "x64", "x86_64": "x64", "x64": "x64"}.get(machine)
    engine = SPEECH_ENGINES.get(("linux" if platform.startswith("linux") else platform, arch))
    if not engine:
        raise HyperframesError("no speech recogniser is published for this computer (%s %s)" % (platform, machine))
    repo, revision, name, digest = SPEECH_MODEL
    return [(SPEECH_SOURCE % ("needle3", SPEECH_ENGINE_REVISION, engine[0]), os.path.basename(engine[0]), engine[1]),
            (SPEECH_SOURCE % (repo, revision, name), name, digest)]


def install_speech() -> None:
    """Download the speech engine and model into ``speech/``. Nothing is kept unless its checksum matches."""
    _download_pinned(os.path.join(home(), "speech"), _speech_downloads(), "speech recogniser")


def _download_pinned(folder: str, files: List[tuple], what: str) -> None:
    os.makedirs(folder, exist_ok=True)
    with _speech_install_lock:                          # setup and a first caption may both get here
        for url, name, digest in files:
            target, partial = os.path.join(folder, name), os.path.join(folder, name + ".partial")
            if _sha256(target) == digest:               # anything else there is replaced, never trusted
                continue
            try:
                if os.path.lexists(partial):
                    os.remove(partial)                  # never write through a link someone left here
                with _fetch(url) as response, open(partial, "xb") as out:
                    shutil.copyfileobj(response, out, 1 << 20)
                if _sha256(partial) != digest:          # what is on disk, not what went past
                    raise HyperframesError("the %s download (%s) did not match its pinned checksum; try again"
                                           % (what, name))
                if sys.platform != "win32":
                    os.chmod(partial, 0o755)
                os.replace(partial, target)             # in one step: a failure leaves what was there
            except OSError as exc:
                raise HyperframesError("could not download the %s (%s): %s" % (what, name, exc))
            finally:
                if os.path.lexists(partial):
                    os.remove(partial)


def install_parakeet() -> None:
    """Have the CLI download its Parakeet recogniser (runtime and model) into Zenvi's folder."""
    node = find_program("node")
    if not node or not os.path.isfile(cli_script()):
        raise HyperframesError("HyperFrames is not set up on this computer yet; call hyperframes_setup_tool")
    code, out = _exec([node, cli_script(), *PARAKEET_INSTALL], home(), node, INSTALL_TIMEOUT)
    if code != 0:
        raise HyperframesError("could not install the Parakeet speech recogniser (exit code %d): %s"
                               % (code, out[-1500:]))


def install_rvm() -> None:
    """Download the RVM model, and the ONNX runtime the way HyperFrames installs it, if either is missing."""
    url, name, digest = RVM_MODEL
    _download_pinned(os.path.dirname(rvm_model()), [(url, name, digest)], "speaker cut-out model")
    with _ort_install_lock:                             # two caption jobs must not run npm into one folder
        if _ort_installed():
            return
        node = find_program("node")
        if not node:
            raise HyperframesError("HyperFrames is not set up on this computer yet; call hyperframes_setup_tool")
        code, out = _exec(_node_tool(node, "npm") + ["install", "onnxruntime-node@" + ORT_VERSION, "--prefix",
                                                      _ort_dir(), "--no-audit", "--no-fund"],
                          home(), node, INSTALL_TIMEOUT, isolated=False)
        if code != 0 or not _ort_installed():
            raise HyperframesError("could not install the ONNX runtime the speaker cut-out runs on (exit code %d): %s"
                                   % (code, out[-1500:]))


def matte(project: str, source: str, output: str, timeout: float = SPEECH_TIMEOUT) -> dict:
    """Cut the subject out of *source* into a transparent WebM *output* (both in the project) with RVM.

    On the GPU where the ONNX runtime has one (DirectML on Windows, CoreML on Apple Silicon,
    CUDA on Linux), else the CPU. Returns ``{frames, provider, ms_per_frame}``.
    """
    if not rvm_ready(fresh=True):                       # hashed again: what Node is about to load
        raise HyperframesError("the speaker cut-out model is not installed")
    root = project_dir(project)
    for path in (source, output):
        _check_value(project, "ffmpeg", "", path, ())
    node, ffmpeg = find_program("node"), find_program("ffmpeg")
    if not node or not ffmpeg:
        raise HyperframesError("HyperFrames is not set up on this computer yet; call hyperframes_setup_tool")
    os.makedirs(os.path.dirname(os.path.join(root, output)) or root, exist_ok=True)
    code, out = _exec([node, RVM_RUNNER, _ort_module(), rvm_model(), ffmpeg, source, output], root, node, timeout)
    if code != 0:
        raise HyperframesError("the speaker cut-out failed (exit code %d): %s" % (code, out[-800:]))
    for line in reversed(out.splitlines()):             # the runtime's own warnings come after it (stderr)
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    raise HyperframesError("the speaker cut-out finished without a report: %s" % out[-400:])


def transcribe(wav: str, timeout: float = SPEECH_TIMEOUT) -> List[dict]:
    """Words of a WAV file as ``[{text, start, end}]`` (seconds), from the private Whistle engine.

    Not an assistant command: Zenvi runs it on audio it extracted itself. Audio of any
    length is accepted (the engine windows it); seven European languages, detected.
    """
    argv = [speech_engine(), "--model", speech_model(), "--audio", wav, "--audio-word-timestamps",
            "--threads", str(max(1, min(SPEECH_THREADS, os.cpu_count() or 4)))]
    try:
        with _speech_install_lock:                      # no install swaps the files between the check and the run
            if not speech_ready(fresh=True):            # hashed again each run: this file is executed
                raise HyperframesError("the speech recogniser is not installed; call hyperframes_setup_tool")
            proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise HyperframesError("the speech recogniser did not finish within %d s" % timeout)
    except OSError as exc:
        raise HyperframesError("could not start the speech recogniser: %s" % exc)
    if proc.returncode != 0:
        raise HyperframesError("the speech recogniser failed (exit code %d): %s"
                               % (proc.returncode, _clean(proc.stderr or proc.stdout)[-800:]))
    try:
        words = json.loads(proc.stdout).get("words") or []
        return [{"text": str(w["word"]), "start": float(w["start"]), "end": float(w["end"])} for w in words]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HyperframesError("the speech recogniser returned something unreadable: %s" % exc)


def _node_tool(node: str, name: str) -> List[str]:
    """argv prefix for npm / npx without going through a .cmd shim (no shell on Windows)."""
    base = os.path.dirname(node)
    for root in (os.path.join(base, "node_modules", "npm", "bin"),
                 os.path.join(base, "..", "lib", "node_modules", "npm", "bin")):
        script = os.path.join(root, "%s-cli.js" % name)
        if os.path.isfile(script):
            return [node, os.path.normpath(script)]
    found = shutil.which(name)
    if not found:
        raise HyperframesError("%s was not found next to Node.js at %s" % (name, node))
    return [found]


def _environment(node: Optional[str], isolated: bool = True) -> dict:
    """The child's environment. *isolated* points its home at Zenvi's folder, so the
    Chrome build, fonts and models the CLI caches under ``~/.cache`` stay inside it;
    npm keeps the real home so the user's proxy and registry settings still apply."""
    env = dict(os.environ)
    if isolated:
        env["HOME"] = env["USERPROFILE"] = home()
    extra = [os.path.join(home(), "node_modules", ".bin")]
    for exe in (node, find_program("ffmpeg")):
        if exe:
            extra.append(os.path.dirname(exe))
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    # ``init`` would otherwise install skills into the user's own Claude Code / Cursor config.
    env["HYPERFRAMES_SKIP_SKILLS"] = "1"
    env["DO_NOT_TRACK"] = "1"
    env["HYPERFRAMES_NO_TELEMETRY"] = "1"
    return env


def _clean(text: str) -> str:
    text = _ANSI_RE.sub("", text or "").strip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = "[... %d earlier characters omitted ...]\n" % (len(text) - MAX_OUTPUT_CHARS) + text[-MAX_OUTPUT_CHARS:]
    return text


def _exec(argv: List[str], cwd: str, node: Optional[str], timeout: float,
          isolated: bool = True) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=_environment(node, isolated), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise HyperframesError("%s did not finish within %d s" % (os.path.basename(argv[0]), timeout))
    except OSError as exc:
        raise HyperframesError("could not start %s: %s" % (argv[0], exc))
    return proc.returncode, _clean((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))


def install(captions: bool = False, progress: Optional[Callable[[str], None]] = None,
            parakeet: bool = False) -> dict:
    """Run every missing step of :func:`install_plan`; returns the new status."""
    os.makedirs(home(), exist_ok=True)
    package = os.path.join(home(), "package.json")
    if not os.path.isfile(package):
        with open(package, "w", encoding="utf-8") as fh:
            json.dump({"name": "zenvi-hyperframes", "private": True}, fh)
    for step in install_plan(status(), captions=captions, parakeet=parakeet):
        if progress:
            progress(step["label"])
        log.info("HyperFrames setup: %s", step["label"])
        if step["id"] == "node":
            try:
                install_node()
            except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
                raise HyperframesError("could not download Node.js: %s" % exc)
            continue
        if step["id"] == "speech":
            install_speech()
            continue
        node, argv = find_program("node"), list(step["argv"])
        own_cli = argv[0] == "hyperframes"
        argv = ([node, cli_script()] if own_cli else _node_tool(node, argv[0])) + argv[1:]
        code, out = _exec(argv, home(), node, INSTALL_TIMEOUT, isolated=own_cli)
        if code != 0:
            raise HyperframesError("%s failed (exit code %d): %s" % (step["label"], code, out[-1500:]))
        if step["id"] == "browser":
            code, path = _exec([node, cli_script(), "browser", "path"], home(), node, 120)
            lines = [line.strip() for line in path.splitlines() if line.strip()]
            remember(browser=lines[-1] if code == 0 and lines else "")
    st = status()
    remember(node=st["node"], bash=st["bash"])
    if parakeet and st["parakeet"]:
        choose_recogniser("parakeet")                   # asked for better accuracy: use it
    return st


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _check_value(project: str, program: str, sub: str, value: str, inputs: Iterable[str]) -> None:
    if _SCHEME_RE.match(value) and not _DRIVE_RE.match(value):
        if program == "hyperframes" and sub == "capture" and value.lower().startswith("https://"):
            return
        raise HyperframesError("argument %r is a URL or protocol; only 'hyperframes capture <https url>' "
                               "may reach the network" % value)
    if _is_absolute(value) or ".." in value.replace("\\", "/").split("/"):
        allowed = {os.path.normcase(os.path.realpath(p)) for p in inputs}
        full = os.path.realpath(value if _is_absolute(value) else os.path.join(project_dir(project), value))
        if os.path.normcase(full) in allowed or _inside(full, project_dir(project)):
            return
        if program in SCRIPT_PROGRAMS and _inside(full, skills_dir()):    # a skill script reading its own files
            return
        raise HyperframesError("argument %r points outside the project folder; use project-relative paths, "
                               "or a media file that is in Project Files" % value)


def check_command(project: str, program: str, args: Iterable[str], inputs: Iterable[str] = ()) -> List[str]:
    """Refuse anything but the allowlisted programs; returns the argv tail to run."""
    project_dir(project)
    args, inputs = [str(a) for a in (args or [])], list(inputs or ())
    if program not in PROGRAMS:
        raise HyperframesError("program must be one of %s; got %r" % (", ".join(PROGRAMS), program))
    sub = args[0] if args else ""
    if program == "hyperframes" and sub not in CLI_SUBCOMMANDS:
        raise HyperframesError("hyperframes %r is not available here; allowed: %s"
                               % (sub, ", ".join(sorted(CLI_SUBCOMMANDS))))
    if program == "hyperframes" and sub == "transcribe":
        # Any other engine falls back to whisper.cpp, which the CLI clones and compiles when it is missing.
        # Exactly one spelling is accepted, so a second or abbreviated flag cannot override it.
        flags = [(a, args[i + 1] if i + 1 < len(args) else "") for i, a in enumerate(args)
                 if a.startswith(("-e", "--e")) or a == "-m" or a.startswith("--m")]
        if not flags or any(a != "--engine=parakeet" and (a, value) != ("--engine", "parakeet") for a, value in flags):
            raise HyperframesError("hyperframes transcribe runs here only with --engine parakeet (install it with "
                                   "hyperframes_setup_tool accurate_speech=true); for captions use add_captions_tool")
    out = list(args)
    if program in SCRIPT_PROGRAMS:
        if not sub or sub.startswith("-"):
            raise HyperframesError("%s runs only scripts that ship in the installed skills, e.g. "
                                   "skills/embedded-captions/scripts/prepare.sh" % program)
        script = resolve_path(project, sub)
        if not _inside(script, skills_dir()) or not os.path.isfile(script):
            raise HyperframesError("%s runs only scripts that ship in the installed skills (skills/<name>/scripts/...); "
                                   "%r is not one" % (program, sub))
        out[0] = script
    for arg in args[1:] if program in SCRIPT_PROGRAMS else args:
        if program in FILTER_PROGRAMS and arg.startswith(_FILE_FED_OPTIONS):
            raise HyperframesError("%s option %r reads its value from a file and is not available here; "
                                   "pass the filter graph inline" % (program, arg))
        value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        if value and not value.startswith("-"):
            _check_value(project, program, sub, value, inputs)
            if program in FILTER_PROGRAMS and not _is_absolute(value):
                for piece in _FILTER_SPLIT_RE.split(value):     # paths inside a filter graph (movie=, subtitles=)
                    _check_value(project, program, sub, piece, inputs)
    return out


def run(project: str, program: str, args: Iterable[str], timeout: float = DEFAULT_TIMEOUT,
        inputs: Iterable[str] = ()) -> Tuple[int, str]:
    """Run an allowlisted command in the project folder. Returns (exit code, output)."""
    tail = check_command(project, program, args, inputs)
    node = find_program("node")
    if program == "hyperframes":
        if not node or not os.path.isfile(cli_script()):
            raise HyperframesError("HyperFrames is not set up on this computer yet; call hyperframes_setup_tool")
        argv = [node, cli_script()] + tail
    else:
        exe = find_program(program)
        if not exe:
            raise HyperframesError("%s was not found on this computer; call hyperframes_setup_tool" % program)
        argv = [exe] + tail
    cwd = project_dir(project)
    os.makedirs(cwd, exist_ok=True)
    return _exec(argv, cwd, node, timeout)
