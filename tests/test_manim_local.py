"""Manim on this computer: a pinned private install, existing installs reused, renders run in the project."""

import os
import sys

import pytest

from classes import hyperframes_local as hf
from classes import manim_local as ml


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "home", lambda: str(tmp_path / "manim"))
    monkeypatch.setattr(hf, "home", lambda: str(tmp_path / "hyperframes"))
    monkeypatch.setattr(ml.shutil, "which", lambda name: None)          # nothing installed on this computer
    os.makedirs(hf.project_dir("proof"))
    with open(os.path.join(hf.project_dir("proof"), "scene.py"), "w") as fh:
        fh.write("from manim import *\nclass Pythagoras(Scene):\n    def construct(self): pass\n")
    return tmp_path


def _fake_private_install(root):
    exe = ml._env_program("manim")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    open(exe, "w").close()
    tex = os.path.join(ml.home(), "TinyTeX", "bin", "x")
    os.makedirs(tex)
    for name in ("latex", "dvisvgm"):
        open(os.path.join(tex, name + (".exe" if sys.platform == "win32" else "")), "w").close()
    return exe, tex


def test_the_plan_installs_manim_and_latex_only_when_equations_are_asked_for(home):
    st = ml.status()
    assert st["ready"] is False and [s["id"] for s in ml.install_plan(st)] == ["manim"]
    assert [s["id"] for s in ml.install_plan(st, latex=True)] == ["manim", "latex"]
    _fake_private_install(home)
    st = ml.status()
    assert st["ready"] and st["latex"] and ml.install_plan(st, latex=True) == []


def test_downloads_are_pinned_per_computer(home):
    for platform, machine in (("win32", "AMD64"), ("darwin", "arm64"), ("darwin", "x86_64"), ("linux", "x86_64"),
                              ("linux", "aarch64")):
        url, name, digest = ml._micromamba_download(platform, machine)
        assert ml.MICROMAMBA_VERSION in url and len(digest) == 64
        url, name, digest = ml._tinytex_download(platform, machine)
        assert ml.TINYTEX_VERSION in url and len(digest) == 64
    with pytest.raises(hf.HyperframesError):
        ml._micromamba_download("sunos5", "sparc")


def test_existing_manim_and_latex_are_reused(home, monkeypatch, tmp_path):
    found = {"manim": str(tmp_path / "bin" / "manim"), "latex": str(tmp_path / "tex" / "latex"),
             "dvisvgm": str(tmp_path / "tex" / "dvisvgm")}
    monkeypatch.setattr(ml.shutil, "which", lambda name: found.get(name))
    st = ml.status()
    assert st["manim"] == found["manim"] and st["latex"] and ml.install_plan(st, latex=True) == []
    assert ml.latex_dir() == str(tmp_path / "tex")
    del found["dvisvgm"]                                   # latex alone cannot turn equations into shapes
    assert ml.latex_dir() is None and not ml.status()["latex"]


def test_render_runs_the_scene_in_the_project_and_returns_the_video(home, monkeypatch):
    exe, tex = _fake_private_install(home)
    seen = {}

    def fake_run(argv, cwd, timeout, env):
        seen.update(argv=argv, cwd=cwd, env=env)
        out = os.path.join(cwd, "manim-media", "videos", "scene", "1080p30", argv[argv.index("-o") + 1] + ".mov")
        os.makedirs(os.path.dirname(out))
        open(out, "wb").write(b"\x1a\x45\xdf\xa3")
        return 0, "File ready at " + out

    monkeypatch.setattr(ml, "_run", fake_run)
    monkeypatch.setenv("PYTHONPATH", "/somewhere/libopenshot")
    path = ml.render("proof", "scene.py", "Pythagoras", transparent=True)

    argv = seen["argv"]
    assert argv[0] == exe and seen["cwd"] == hf.project_dir("proof")
    scene = argv[argv.index("render") + 1]                 # a private copy of the checked scene (see below)
    assert os.path.basename(scene) == "scene.py" and os.path.dirname(scene) != hf.project_dir("proof")
    assert "Pythagoras" in argv and "-t" in argv and argv[argv.index("--format") + 1] == "mov"
    assert "PYTHONPATH" not in seen["env"]                 # the editor's own Python path never leaks in
    assert seen["env"]["PATH"].split(os.pathsep)[0] == tex
    assert path.endswith(".mov") and os.path.isfile(path) and path.startswith(hf.project_dir("proof"))


def test_render_refuses_what_is_not_a_scene_in_the_project(home, monkeypatch):
    _fake_private_install(home)
    monkeypatch.setattr(ml, "_run", lambda *a, **k: pytest.fail("nothing may run"))
    for scene_file, scene in (("../other/scene.py", "Pythagoras"), ("scene.txt", "Pythagoras"),
                              ("skills/x/scene.py", "Pythagoras"), ("scene.py", "Bad; rm -rf"),
                              ("scene.py", "--help")):
        with pytest.raises(hf.HyperframesError):
            ml.render("proof", scene_file, scene)


@pytest.mark.parametrize("body", [
    "import os\nos.system('calc')",
    "import subprocess",
    "from pathlib import Path",
    "open('C:/secrets.txt').read()",
    "exec('print(1)')",
    "__import__('os').system('x')",
    "x = (1).__class__.__mro__",
    "import manim\nmanim.os.system('x')",
    "getattr(config, 'x')",
    "import numpy as np\nnp.load('payload.npy', allow_pickle=True)",   # unpickling runs code
    "from numpy import load",
    "import numpy as np\nnp.save('C:/Users/me/overwritten.npy', [1])",
    "from numpy.ctypeslib import ctypes",                 # native code, one import line
    "import numpy.f2py as f",
    "from numpy.lib.format import open_memmap",
    "from numpy import ctypeslib",
    "import manim.utils.file_ops",
    "import numpy as np\nnp.fromregex('C:/secrets.txt', '.', [('', 'i')])",
    "from numpy import recfromtxt",
    "import numpy as np\nnp.recfromcsv('C:/secrets.txt')",
    "import numpy as np\nnp.array([1]).dump('C:/Users/me/out.pkl')",
])
def test_a_scene_that_reaches_beyond_drawing_is_refused_before_it_runs(home, monkeypatch, body):
    _fake_private_install(home)
    monkeypatch.setattr(ml, "_run", lambda *a, **k: pytest.fail("a refused scene must not run"))
    with open(os.path.join(hf.project_dir("proof"), "bad.py"), "w") as fh:
        fh.write("from manim import *\n%s\nclass S(Scene):\n    def construct(self): pass\n" % body)
    with pytest.raises(hf.HyperframesError) as err:
        ml.render("proof", "bad.py", "S")
    assert "scene" in str(err.value).lower()


def test_a_custom_mobject_may_call_its_parents_init():
    ml.check_scene("from manim import *\nclass Badge(VGroup):\n    def __init__(self, **kw):\n"
                   "        super().__init__(**kw)\n")
    with pytest.raises(hf.HyperframesError):
        ml.check_scene("from manim import *\nx = Circle().__init__")


def test_the_checked_text_is_what_runs_away_from_the_projects_other_files(home, monkeypatch):
    # Manim puts the scene's folder first on sys.path: a project colour.py would shadow the real module.
    _fake_private_install(home)
    root = hf.project_dir("proof")
    open(os.path.join(root, "colour.py"), "w").write("import os\n")
    ran = {}

    def fake_run(argv, cwd, timeout, env):
        scene = argv[argv.index("render") + 1]
        ran.update(folder=os.path.dirname(scene), text=open(scene).read(), cwd=cwd)
        out = os.path.join(cwd, "manim-media", "videos", argv[argv.index("-o") + 1] + ".mov")
        os.makedirs(os.path.dirname(out))
        open(out, "wb").write(b"0")
        return 0, ""

    monkeypatch.setattr(ml, "_run", fake_run)
    ml.render("proof", "scene.py", "Pythagoras")
    assert ran["folder"] != root and not os.path.exists(os.path.join(ran["folder"], "colour.py"))
    assert "class Pythagoras" in ran["text"] and ran["cwd"] == root       # output still lands in the project
    assert not os.path.exists(ran["folder"])                              # the private copy is cleaned up


def test_the_scene_does_not_see_the_editors_secrets(home, monkeypatch):
    _fake_private_install(home)
    monkeypatch.setenv("ZENVI_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    seen = {}

    def fake_run(argv, cwd, timeout, env):
        seen.update(env)
        out = os.path.join(cwd, "manim-media", "videos", argv[argv.index("-o") + 1] + ".mov")
        os.makedirs(os.path.dirname(out))
        open(out, "wb").write(b"0")
        return 0, ""

    monkeypatch.setattr(ml, "_run", fake_run)
    ml.render("proof", "scene.py", "Pythagoras")
    assert "ZENVI_AUTH_TOKEN" not in seen and "OPENAI_API_KEY" not in seen and "PATH" in seen


def test_an_equation_without_latex_says_how_to_get_it(home, monkeypatch):
    exe = ml._env_program("manim")
    os.makedirs(os.path.dirname(exe))
    open(exe, "w").close()
    monkeypatch.setattr(ml, "_run", lambda *a, **k: (1, "FileNotFoundError: latex failed but did not produce "
                                                         "a log file. Check your LaTeX installation."))
    with pytest.raises(hf.HyperframesError) as err:
        ml.render("proof", "scene.py", "Pythagoras")
    assert "latex=true" in str(err.value)


def test_render_before_setup_says_how_to_set_up(home):
    with pytest.raises(hf.HyperframesError) as err:
        ml.render("proof", "scene.py", "Pythagoras")
    assert "manim_setup_tool" in str(err.value)
