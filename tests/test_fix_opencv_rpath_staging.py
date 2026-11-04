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
