"""Manim on this computer: math and science animations rendered here, like HyperFrames.

Nothing needs an installer or admin rights. Manim and its own Python (with cairo and
pango) come from conda-forge through a pinned ``micromamba`` into
``~/.openshot_qt/manim``; LaTeX, needed only for equations, is a pinned TinyTeX in the
same folder. A ``manim``, or a ``latex`` + ``dvisvgm``, already on this computer is
used instead. Scenes live in a HyperFrames project folder, so the same file tools
write them and the same import brings the render into Project Files.

A scene is Python the assistant wrote, and it may have read untrusted pages on the
way, so before it runs it is checked to only draw: imports limited to Manim and a few
maths modules, no file, code or OS access, no dunder attributes. It runs with an
allowlisted environment, so the editor's tokens and keys never reach it.

ponytail: that check stops a prompt-injected payload, not a determined escape (it is
not a sandbox); render server-side in a locked-down container if that ever matters
more than not installing anything. The conda and TeX Live packages are pinned by
version through their own signed indexes, not by hash like the installers.
"""

from __future__ import annotations

import glob
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Callable, List, Optional, Tuple

from classes import hyperframes_local as hf
from classes import info
from classes.logger import log

MANIM_VERSION = "0.22.0"
PYTHON_VERSION = "3.12"
MICROMAMBA_VERSION = "2.9.0-0"
MICROMAMBA_URL = "https://github.com/mamba-org/micromamba-releases/releases/download/%s/%s"
MICROMAMBA = {          # (platform, architecture) -> (asset, sha256), from the release's own .sha256 files
    ("win32", "x64"): ("micromamba-win-64", "a6d804394b2418991c4e29562853eaace2f2ce9d9da661a98e74e02e8dbb44b0"),
    ("darwin", "arm64"): ("micromamba-osx-arm64", "ec2a072f028e1a7cf20f3e2e74d5a8127cf5a5f27636375b5359811565f4e5be"),
    ("darwin", "x64"): ("micromamba-osx-64", "1e71054bb3ac9a076e21f7ec48acfef536f9b3f1408f371a942784bf5ef83d8a"),
    ("linux", "x64"): ("micromamba-linux-64", "366cd9cd8be14df1ab8ed50352a82111082a36686b2d389fdb79a92c3fafb3e3"),
    ("linux", "arm64"): ("micromamba-linux-aarch64", "9f93b974adcb4d166996af969b6cd371287d1a3e52733704727884d9b74cb7a7"),
}
TINYTEX_VERSION = "v2026.10"
TINYTEX_URL = "https://github.com/rstudio/tinytex-releases/releases/download/%s/%s"
TINYTEX = {             # sha256 from the GitHub release's asset digests
    ("win32", "x64"): ("TinyTeX-1-windows-v2026.10.exe",
                       "36c329c644683d981a668419d13c8804696e2816ae4d287ad755788fee231a31"),
    ("darwin", "arm64"): ("TinyTeX-1-darwin-v2026.10.tar.xz",
                          "e40c294efae81026d8cc8d12c09d2dd456d268efe7fe092410478bc1973fd40f"),
    ("darwin", "x64"): ("TinyTeX-1-darwin-v2026.10.tar.xz",
                        "e40c294efae81026d8cc8d12c09d2dd456d268efe7fe092410478bc1973fd40f"),
    ("linux", "x64"): ("TinyTeX-1-linux-x86_64-v2026.10.tar.xz",
                       "4d519d6236ee6798e3ec0d8e2093d1fda01aeb267aa22c2a5586d76bcfce6566"),
    ("linux", "arm64"): ("TinyTeX-1-linux-arm64-v2026.10.tar.xz",
                         "1bc5465c7a7c6f13e13c1b54a52147577eb3f389033af0444bc097698eb6653f"),
}
# dvisvgm turns LaTeX into shapes; the rest is what Manim's default TeX template loads.
TEX_PACKAGES = ("dvisvgm", "standalone", "preview", "doublestroke", "setspace", "tipa", "relsize", "jknapltx",
                "rsfs", "calligra", "wasysym", "ragged2e", "physics", "microtype", "babel-english")
QUALITY = {"draft": "l", "standard": "h"}           # 480p15 / 1080p, then --fps keeps the editor's rate
INSTALL_TIMEOUT = 3600
RENDER_TIMEOUT = 1800
_SCENE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
SCENE_IMPORTS = {"manim", "numpy", "math", "random", "itertools", "functools", "colour", "typing", "__future__"}
_SCENE_BANNED_NAMES = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "globals",
                       "locals", "vars", "getattr", "setattr", "delattr", "help", "exit", "quit", "memoryview"}
_SCENE_BANNED_ATTRS = {"os", "sys", "subprocess", "shutil", "socket", "pathlib", "builtins", "importlib",
                       "ctypes", "system", "popen", "spawnv", "urllib", "request", "requests", "http"}
# numpy reads and writes files, and unpickling with np.load runs code: banned as names and as attributes.
_NUMPY_IO = {"load", "save", "savez", "savez_compressed", "savetxt", "loadtxt", "genfromtxt", "fromfile",
             "tofile", "memmap", "DataSource", "lib", "ctypeslib", "f2py", "testing",
             "fromregex", "recfromtxt", "recfromcsv", "dump"}
_SCENE_BANNED_NAMES |= _NUMPY_IO
_SCENE_BANNED_ATTRS |= _NUMPY_IO
# What a scene process sees of the editor's environment: enough to find the system, never its tokens or keys.
_ENV_KEEP = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
             "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "OS",
             "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "DISPLAY", "XDG_RUNTIME_DIR",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE"}
_NO_LATEX_RE = re.compile(r"latex|dvisvgm", re.I)


def home() -> str:
    return os.path.join(info.USER_PATH, "manim")


def _env_program(name: str) -> str:
    env = os.path.join(home(), "env")
    return (os.path.join(env, "Scripts", name + ".exe") if sys.platform == "win32"
            else os.path.join(env, "bin", name))


def _private_tex_bin() -> Optional[str]:
    for latex in glob.glob(os.path.join(home(), "TinyTeX", "bin", "*", "latex*")):
        folder = os.path.dirname(latex)
        if glob.glob(os.path.join(folder, "dvisvgm*")):
            return folder
    return None


def manim_program() -> str:
    """The private Manim, else one already installed on this computer, else ''."""
    private = _env_program("manim")
    return private if os.path.isfile(private) else (shutil.which("manim") or "")


def latex_dir() -> Optional[str]:
    """Folder holding latex and dvisvgm (both needed for equations), private first."""
    private = _private_tex_bin()
    if private:
        return private
    latex, dvisvgm = shutil.which("latex"), shutil.which("dvisvgm")
    return os.path.dirname(latex) if latex and dvisvgm else None


def status() -> dict:
    manim = manim_program()
    return {"manim": manim, "latex": bool(latex_dir()), "ready": bool(manim)}


def install_plan(st: dict, latex: bool = False) -> List[dict]:
    steps: List[dict] = []
    if not st.get("manim"):
        steps.append({"id": "manim", "label": "Download Manim %s with its own Python into Zenvi's folder "
                                              "(about 1.3 GB, no installer)" % MANIM_VERSION})
    if latex and not st.get("latex"):
        steps.append({"id": "latex", "label": "Download TinyTeX so Manim can typeset equations "
                                              "(about 320 MB, no installer)"})
    return steps


def _key(platform: Optional[str], machine: Optional[str]) -> Tuple[str, str]:
    platform, machine = platform or sys.platform, (machine or _platform.machine()).lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "amd64": "x64", "x86_64": "x64", "x64": "x64"}.get(machine, machine)
    return ("linux" if platform.startswith("linux") else platform), arch


def _micromamba_download(platform: Optional[str] = None, machine: Optional[str] = None) -> tuple:
    found = MICROMAMBA.get(_key(platform, machine))
    if not found:
        raise hf.HyperframesError("Manim has no installer for this computer (%s %s)" % _key(platform, machine))
    name = found[0] + (".exe" if found[0].startswith("micromamba-win") else "")
    return MICROMAMBA_URL % (MICROMAMBA_VERSION, found[0]), name, found[1]


def _tinytex_download(platform: Optional[str] = None, machine: Optional[str] = None) -> tuple:
    found = TINYTEX.get(_key(platform, machine))
    if not found:
        raise hf.HyperframesError("there is no TinyTeX for this computer (%s %s)" % _key(platform, machine))
    return TINYTEX_URL % (TINYTEX_VERSION, found[0]), found[0], found[1]


def _activation_dirs() -> List[str]:
    """What activating the private env puts first on PATH, so its own cairo/pango DLLs load, not another's."""
    env = os.path.join(home(), "env")
    if sys.platform != "win32":
        return [os.path.join(env, "bin")]
    return [env] + [os.path.join(env, *p) for p in (("Library", "mingw-w64", "bin"), ("Library", "usr", "bin"),
                                                    ("Library", "bin"), ("Scripts",))]


def _environment(path_first: Optional[str] = None) -> dict:
    # Allowlisted: not the editor's tokens and keys, PYTHONPATH (libopenshot, its venv) or conda settings.
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    env.update(PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8", MAMBA_ROOT_PREFIX=os.path.join(home(), "mamba"))
    first = ([path_first] if path_first else []) + [d for d in _activation_dirs() if os.path.isdir(d)]
    path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
    env[path_key] = os.pathsep.join(first + [env.get(path_key, "")])
    return env


def _run(argv: List[str], cwd: str, timeout: float, env: dict) -> Tuple[int, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, creationflags=hf._NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise hf.HyperframesError("%s did not finish within %d s" % (os.path.basename(argv[0]), timeout))
    except OSError as exc:
        raise hf.HyperframesError("could not start %s: %s" % (argv[0], exc))
    return proc.returncode, hf._clean((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))


def _install_manim() -> None:
    url, name, digest = _micromamba_download()
    folder = os.path.join(home(), "bin")
    hf._download_pinned(folder, [(url, name, digest)], "Manim installer (micromamba)")
    env = os.path.join(home(), "env")
    shutil.rmtree(env, ignore_errors=True)                 # a half-made env from an earlier attempt
    code, out = _run([os.path.join(folder, name), "create", "-y", "-q", "-p", env, "-c", "conda-forge",
                      "--override-channels", "python=" + PYTHON_VERSION, "manim=" + MANIM_VERSION],
                     home(), INSTALL_TIMEOUT, _environment())
    if code != 0 or not os.path.isfile(_env_program("manim")):
        shutil.rmtree(env, ignore_errors=True)
        raise hf.HyperframesError("could not install Manim (exit code %d): %s" % (code, out[-1500:]))
    shutil.rmtree(os.path.join(home(), "mamba", "pkgs"), ignore_errors=True)    # the download cache: 300+ MB


def _install_latex() -> None:
    url, name, digest = _tinytex_download()
    downloads = os.path.join(home(), "downloads")
    hf._download_pinned(downloads, [(url, name, digest)], "TinyTeX")
    archive = os.path.join(downloads, name)
    shutil.rmtree(os.path.join(home(), "TinyTeX"), ignore_errors=True)
    if name.endswith(".exe"):                               # a 7-Zip self-extractor: unpacks quietly, no admin
        code, out = _run([archive, "-y", "-o" + home()], home(), INSTALL_TIMEOUT, _environment())
        if code != 0:
            raise hf.HyperframesError("could not unpack TinyTeX (exit code %d): %s" % (code, out[-800:]))
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(home(), filter="data")
        hidden = os.path.join(home(), ".TinyTeX")
        if os.path.isdir(hidden):
            os.replace(hidden, os.path.join(home(), "TinyTeX"))
    tlmgr = (glob.glob(os.path.join(home(), "TinyTeX", "bin", "*", "tlmgr.bat"))
             or glob.glob(os.path.join(home(), "TinyTeX", "bin", "*", "tlmgr")))
    if not tlmgr:
        raise hf.HyperframesError("TinyTeX unpacked without its package manager (tlmgr)")
    run_tlmgr = (["cmd", "/c", tlmgr[0]] if tlmgr[0].endswith(".bat") else [tlmgr[0]])
    # The bundled tlmgr refuses to install once TeX Live has moved on, until it updates itself.
    for args in (["update", "--self"], ["install", *TEX_PACKAGES]):
        code, out = _run(run_tlmgr + args, home(), INSTALL_TIMEOUT, _environment(os.path.dirname(tlmgr[0])))
        if code != 0:
            raise hf.HyperframesError("could not install the LaTeX packages Manim needs (exit code %d): %s"
                                      % (code, out[-1500:]))
    if not _private_tex_bin():
        raise hf.HyperframesError("TinyTeX installed without dvisvgm, so equations still cannot render")


def install(latex: bool = False, progress: Optional[Callable[[str], None]] = None) -> dict:
    """Run every missing step of :func:`install_plan`; returns the new status."""
    os.makedirs(home(), exist_ok=True)
    for step in install_plan(status(), latex=latex):
        if progress:
            progress(step["label"])
        log.info("Manim setup: %s", step["label"])
        _install_manim() if step["id"] == "manim" else _install_latex()
    return status()


def check_scene(source: str) -> None:
    """Refuse a scene that does more than draw (see the module docstring); raises HyperframesError."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise hf.HyperframesError("the scene is not valid Python: %s (line %s)" % (exc.msg, exc.lineno))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # The modules themselves, never a submodule: numpy.ctypeslib, numpy.f2py and numpy.lib reach native
            # code and files, and an attribute check never sees what an import line names.
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            bad = [n for n in names if n not in SCENE_IMPORTS or (isinstance(node, ast.ImportFrom) and node.level)]
            if bad:
                raise hf.HyperframesError("the scene may only import %s (not their submodules); not %s"
                                          % (", ".join(sorted(SCENE_IMPORTS - {"__future__"})), ", ".join(bad)))
            taken = [a.name for a in node.names if a.name in _SCENE_BANNED_NAMES | _SCENE_BANNED_ATTRS]
            if isinstance(node, ast.ImportFrom) and taken:
                raise hf.HyperframesError("the scene may only draw; %s is not available in a scene" % taken[0])
        elif isinstance(node, ast.Name) and node.id in _SCENE_BANNED_NAMES:
            raise hf.HyperframesError("the scene may only draw; %s() is not available in a scene" % node.id)
        elif isinstance(node, ast.Attribute) and (
                (node.attr.startswith("__") and not _is_super_init(node)) or node.attr in _SCENE_BANNED_ATTRS):
            raise hf.HyperframesError("the scene may only draw; .%s is not available in a scene" % node.attr)


def _is_super_init(node) -> bool:
    """super().__init__ -- how every custom Mobject or Scene subclass starts; any other dunder is refused."""
    import ast

    return (node.attr == "__init__" and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "super")


def render(project: str, scene_file: str, scene: str, transparent: bool = True, quality: str = "standard",
           fps: int = 30, timeout: float = RENDER_TIMEOUT) -> str:
    """Render one Scene class from a .py file in the project; returns the video's path."""
    root = hf.project_dir(project)
    path = hf.resolve_path(project, scene_file)
    if not hf._inside(path, root) or not path.endswith(".py") or not os.path.isfile(path):
        raise hf.HyperframesError("scene_file must be an existing .py file in the project: %r" % scene_file)
    if not _SCENE_RE.match(str(scene or "")):
        raise hf.HyperframesError("scene must be the name of a Scene class in that file, e.g. 'PythagorasProof'")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    check_scene(source)
    manim = manim_program()
    if not manim:
        raise hf.HyperframesError("Manim is not set up on this computer yet; call manim_setup_tool with install=true")
    # Run exactly the checked text, from a folder of its own: Manim puts the scene's folder first on
    # sys.path, where a project file (colour.py, numpy.py) would shadow an allowed module.
    run_dir = tempfile.mkdtemp(prefix="zenvi-manim-")
    try:
        copy = os.path.join(run_dir, os.path.basename(path))
        with open(copy, "w", encoding="utf-8") as fh:
            fh.write(source)
        return _render_checked(root, copy, scene, transparent, quality, fps, timeout, manim)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _render_checked(root, path, scene, transparent, quality, fps, timeout, manim) -> str:
    out_name = "%s-%d" % (scene, time.time_ns() // 1000000)
    argv = [manim, "render", path, scene, "-q", QUALITY.get(quality, "h"), "--fps", str(int(fps)),
            # conda-forge's PyAV has no VP9 encoder; a transparent .mov (qtrle, lossless) carries the alpha
            # and the import re-encodes it like any overlay.
            "--format", "mov" if transparent else "mp4", "-o", out_name, "--media_dir", "manim-media",
            "--disable_caching", "--progress_bar", "none"]
    if transparent:
        argv.append("-t")
    tex = latex_dir()
    code, out = _run(argv, root, timeout, _environment(tex))
    if code != 0:
        hint = ("; this scene typesets text or equations with LaTeX, which is not installed: call "
                "manim_setup_tool with install=true latex=true, or use Text() instead of Tex()/MathTex()"
                if not tex and _NO_LATEX_RE.search(out) else "")
        raise hf.HyperframesError("the Manim render failed (exit code %d)%s:\n%s" % (code, hint, out[-2000:]))
    found = glob.glob(os.path.join(root, "manim-media", "videos", "**", out_name + ".*"), recursive=True)
    if not found:
        raise hf.HyperframesError("Manim finished but wrote no video:\n%s" % out[-800:])
    return found[0]
