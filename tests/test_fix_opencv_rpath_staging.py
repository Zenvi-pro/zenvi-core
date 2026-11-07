"""Staging OpenCV dylibs must work on a warm cache, over last run's read-only copies."""

import os
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer.fix_opencv_rpath import stage_opencv_dylibs  # noqa: E402


def test_restaging_over_read_only_copies_from_a_cache(tmp_path):
    lib = tmp_path / "opencv" / "lib"
    lib.mkdir(parents=True)
    source = lib / "libopencv_core.4.14.0.dylib"
    source.write_bytes(b"new")
    source.chmod(0o444)  # as Homebrew installs it

    stage = tmp_path / "stage"
    stage.mkdir()
    stale = stage / "libopencv_core.4.14.0.dylib"
    stale.write_bytes(b"old")
    stale.chmod(0o444)  # the previous run's copy, restored from the cache

    stage_opencv_dylibs(str(tmp_path / "opencv"), str(stage))

    assert stale.read_bytes() == b"new"
    assert os.stat(stale).st_mode & stat.S_IWUSR, "install_name_tool rewrites it next"


def test_a_staged_symlink_is_replaced_without_touching_what_it_points_at(tmp_path):
    lib = tmp_path / "opencv" / "lib"
    lib.mkdir(parents=True)
    source = lib / "libopencv_core.4.14.0.dylib"
    source.write_bytes(b"new")
    elsewhere = tmp_path / "keg" / "libopencv_core.4.14.0.dylib"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"keg")
    elsewhere.chmod(0o444)
    stage = tmp_path / "stage"
    stage.mkdir()
    link = stage / "libopencv_core.4.14.0.dylib"
    try:
        link.symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("this account cannot create symlinks")
    before = stat.S_IMODE(os.stat(elsewhere).st_mode)

    stage_opencv_dylibs(str(tmp_path / "opencv"), str(stage))

    assert not link.is_symlink() and link.read_bytes() == b"new"
    assert stat.S_IMODE(os.stat(elsewhere).st_mode) == before and elsewhere.read_bytes() == b"keg"
