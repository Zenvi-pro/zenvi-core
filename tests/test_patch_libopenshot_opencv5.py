"""installer/patch-libopenshot-opencv5.py can be run again on a tree it already patched.

A manual libopenshot build that fails after patching is retried with the same
documented commands, patch step included. The second run used to stop at
"find_package(OpenCV 4) not found".
"""

import subprocess
import sys
from pathlib import Path

PATCHER = Path(__file__).resolve().parents[1] / "installer" / "patch-libopenshot-opencv5.py"

CMAKE = """find_package(OpenCV 4)
target_link_libraries(openshot PUBLIC
      opencv_core
      opencv_tracking
      )
"""
HEADER = """#ifdef USE_OPENCV
    #include <opencv2/core.hpp>
#endif
"""


def _tree(tmp_path):
    src = tmp_path / "libopenshot" / "src"
    src.mkdir(parents=True)
    (src / "CMakeLists.txt").write_text(CMAKE, encoding="utf-8")
    (src / "Frame.h").write_text(HEADER, encoding="utf-8")
    return src


def _patch(src):
    return subprocess.run(
        [sys.executable, str(PATCHER), str(src.parent)], capture_output=True, text=True)


def test_a_second_run_changes_nothing_and_succeeds(tmp_path):
    src = _tree(tmp_path)
    first = _patch(src)
    assert first.returncode == 0, first.stderr
    cmake = (src / "CMakeLists.txt").read_text(encoding="utf-8")
    header = (src / "Frame.h").read_text(encoding="utf-8")
    assert "find_package(OpenCV)" in cmake and "opencv_geometry" in cmake
    assert "opencv2/geometry.hpp" in header

    second = _patch(src)
    assert second.returncode == 0, second.stderr
    assert (src / "CMakeLists.txt").read_text(encoding="utf-8") == cmake
    assert (src / "Frame.h").read_text(encoding="utf-8") == header


def test_a_tree_that_is_not_libopenshot_is_still_refused(tmp_path):
    src = tmp_path / "other" / "src"
    src.mkdir(parents=True)
    (src / "CMakeLists.txt").write_text("project(other)\n", encoding="utf-8")
    assert _patch(src).returncode != 0
