"""Packaging guards for the media index: numpy ships, and the Mac app carries ffmpeg.

The index searches saved vectors with numpy, and indexing / transcription audio /
thumbnails run the ffmpeg CLI. freeze.py used to exclude numpy, and only Windows
copied ffmpeg.exe, so a frozen Mac app had neither. These read the build files as
text (no cx_Freeze needed) so a later edit cannot quietly undo either.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _excludes_block(freeze_src: str) -> str:
    m = re.search(r'build_exe_options\["excludes"\]\s*=\s*\[(.*?)\]', freeze_src, re.S)
    assert m, "freeze.py no longer sets build_exe_options['excludes']"
    return m.group(1)


def _packages_block(freeze_src: str) -> str:
    m = re.search(r"python_packages\s*=\s*\[(.*?)\n\s*\]", freeze_src, re.S)
    assert m, "freeze.py no longer defines python_packages"
    return m.group(1)


def test_freeze_does_not_exclude_numpy():
    assert '"numpy"' not in _excludes_block(_read("freeze.py"))


def test_freeze_always_includes_the_numpy_package():
    assert '"numpy"' in _packages_block(_read("freeze.py"))


def test_requirements_declare_numpy():
    for rel in ("requirements.txt", "requirements-noqt.txt"):
        assert re.search(r"(?m)^numpy[<>=!~ ]", _read(rel)), f"{rel} must list numpy"


def test_windows_msys_packages_install_numpy():
    # The Windows venv uses system site-packages: without the pacman package pip
    # would try to build numpy from source.
    assert "mingw-w64-ucrt-x86_64-python-numpy" in _read("installer/ci-win-msys-packages.sh")


def test_freeze_copies_the_ffmpeg_cli_into_the_mac_app():
    src = _read("freeze.py")
    mac = src[src.index('if sys.platform == "darwin":\n    _mac_ff_dir'):]
    assert '"ffmpeg", "ffprobe"' in mac
    assert "build-ffmpeg-cli.sh" in mac


def test_release_builds_and_verifies_the_mac_ffmpeg_cli():
    wf = _read(".github/workflows/release.yml")
    assert "bash installer/build-ffmpeg-cli.sh" in wf
    # The build runs before the macOS freeze, and the frozen app is checked afterwards.
    built = wf.index("bash installer/build-ffmpeg-cli.sh")
    frozen = wf.index("python3 freeze.py build --git-branch=production", built)
    assert wf.index("ffmpeg CLI and numpy are in the frozen app", frozen) > frozen


def test_build_script_pins_its_sources_and_stays_self_contained():
    sh = _read("installer/build-ffmpeg-cli.sh")
    assert re.search(r'FFMPEG_TAG="\$\{FFMPEG_TAG:-n\d+\.\d+(\.\d+)?\}"', sh), "ffmpeg tag must be pinned"
    assert re.search(r'X264_REF="\$\{X264_REF:-[0-9a-f]{40}\}"', sh), "x264 must be pinned to a commit"
    # A binary that links Homebrew dylibs would run here and crash on a user's Mac.
    assert "links non-system libraries" in sh


def test_find_ffmpeg_prefers_the_frozen_lib_dir_over_homebrew(monkeypatch, tmp_path):
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from classes import ffmpeg_cli

    exe_dir = tmp_path / "app"
    (exe_dir / "lib").mkdir(parents=True)
    bundled = exe_dir / "lib" / "ffmpeg"
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)

    monkeypatch.delenv("FFMPEG_BIN_DIR", raising=False)
    monkeypatch.delenv("ZENVI_FFMPEG_DIR", raising=False)
    monkeypatch.setattr(ffmpeg_cli.shutil, "which", lambda *_a, **_k: None)  # GUI-launched: no Homebrew on PATH
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "launch"))
    ffmpeg_cli.find_ffmpeg.cache_clear()
    try:
        assert ffmpeg_cli.find_ffmpeg("ffmpeg") == str(bundled)
    finally:
        ffmpeg_cli.find_ffmpeg.cache_clear()
