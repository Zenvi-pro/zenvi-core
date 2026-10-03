"""HyperFrames on this computer: the CLI, its skills and the projects the assistant authors.

Motion graphics, product demos and captions are HTML compositions rendered by
the open-source ``hyperframes`` CLI (Node + headless Chrome + ffmpeg). Zenvi
keeps a private install under ``~/.openshot_qt/hyperframes``:

    node/             a portable Node.js, unless a recent one is already installed
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

MAX_OUTPUT_CHARS = 12000
DEFAULT_TIMEOUT = 900
INSTALL_TIMEOUT = 1800

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
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
    return os.path.join(home(), "projects", name)


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


def _remembered() -> dict:
    try:
        with open(_install_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(**values) -> None:
    """Save found/installed paths so the next run does not search again."""
    data = _remembered()
    data.update({k: v for k, v in values.items() if v})
    os.makedirs(home(), exist_ok=True)
    with open(_install_file(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


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


def install_plan(st: dict, platform: Optional[str] = None, captions: bool = False) -> List[dict]:
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


def install(captions: bool = False, progress: Optional[Callable[[str], None]] = None) -> dict:
    """Run every missing step of :func:`install_plan`; returns the new status."""
    os.makedirs(home(), exist_ok=True)
    package = os.path.join(home(), "package.json")
    if not os.path.isfile(package):
        with open(package, "w", encoding="utf-8") as fh:
            json.dump({"name": "zenvi-hyperframes", "private": True}, fh)
    for step in install_plan(status(), captions=captions):
        if progress:
            progress(step["label"])
        log.info("HyperFrames setup: %s", step["label"])
        if step["id"] == "node":
            try:
                install_node()
            except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
                raise HyperframesError("could not download Node.js: %s" % exc)
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
    return st


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _check_value(project: str, program: str, sub: str, value: str, inputs: Iterable[str]) -> None:
    if _SCHEME_RE.match(value) and not _DRIVE_RE.match(value):
        if program == "hyperframes" and sub == "capture" and value.lower().startswith(("http://", "https://")):
            return
        raise HyperframesError("argument %r is a URL or protocol; only 'hyperframes capture <https url>' "
                               "may reach the network" % value)
    if _is_absolute(value) or ".." in value.replace("\\", "/").split("/"):
        allowed = {os.path.normcase(os.path.realpath(p)) for p in inputs}
        full = os.path.realpath(value if _is_absolute(value) else os.path.join(project_dir(project), value))
        if os.path.normcase(full) in allowed or _inside(full, project_dir(project)) or _inside(full, skills_dir()):
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
        value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        if value and not value.startswith("-"):
            _check_value(project, program, sub, value, inputs)
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
