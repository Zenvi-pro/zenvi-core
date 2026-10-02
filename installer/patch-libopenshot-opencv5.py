#!/usr/bin/env python3
"""Let libopenshot 1.0 build against OpenCV 5 (what MSYS2 ships since 2026-09).

libopenshot asks for ``find_package(OpenCV 4)``; with OpenCV 5 installed that is
"not found" and the OpenCV effects (Stabilizer, Tracker, Object Detector) are
silently left out. OpenCV 5 still has every module libopenshot uses; it only
moved the 2D/3D geometry functions (``boundingRect``, ``estimateAffinePartial2D``)
out of imgproc/calib3d into a ``geometry`` module.

Usage: patch-libopenshot-opencv5.py <libopenshot source dir>
A no-op for the build on OpenCV 4: the include is version-guarded and the extra
link target only exists in OpenCV 5.
"""
import glob
import os
import re
import sys

GUARDED_INCLUDE = (
    "#if CV_VERSION_MAJOR >= 5\n"
    "#include <opencv2/geometry.hpp>\n"
    "#endif\n"
)
# Indented inside #ifdef USE_OPENCV in Frame.h / Clip.h, quoted in sort_filter/.
OPENCV_INCLUDE = re.compile(r'^[ \t]*#include [<"]opencv2/[^>"\n]+[>"][ \t]*\n', re.M)


def patch_cmake(src_dir: str) -> None:
    path = os.path.join(src_dir, "src", "CMakeLists.txt")
    text = open(path, encoding="utf-8").read()
    if "$<TARGET_NAME_IF_EXISTS:opencv_geometry>" in text:
        # A retried build runs this again on the same tree.
        print("already patched", path)
        return
    if "find_package(OpenCV 4)" not in text:
        sys.exit("patch-libopenshot-opencv5: find_package(OpenCV 4) not found in %s" % path)
    text = text.replace("find_package(OpenCV 4)", "find_package(OpenCV)")
    link = "      opencv_tracking\n"
    if text.count(link) != 1:
        sys.exit("patch-libopenshot-opencv5: opencv link list not found in %s" % path)
    text = text.replace(link, link + "      $<TARGET_NAME_IF_EXISTS:opencv_geometry>\n")
    open(path, "w", encoding="utf-8", newline="\n").write(text)
    print("patched", path)


def patch_sources(src_dir: str) -> None:
    patched = 0
    # Every file that includes OpenCV: effects reach it through Frame.h / Clip.h.
    sources = glob.glob(os.path.join(src_dir, "src", "**", "*.h*"), recursive=True)
    sources += glob.glob(os.path.join(src_dir, "src", "**", "*.cpp"), recursive=True)
    for path in sorted(sources):
        text = open(path, encoding="utf-8").read()
        if "opencv2/geometry.hpp" in text:
            patched += 1  # by an earlier run
            continue
        match = OPENCV_INCLUDE.search(text)
        if not match:
            continue
        text = text[:match.end()] + GUARDED_INCLUDE + text[match.end():]
        open(path, "w", encoding="utf-8", newline="\n").write(text)
        patched += 1
        print("patched", path)
    if not patched:
        sys.exit("patch-libopenshot-opencv5: no OpenCV source files found under %s/src" % src_dir)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    patch_cmake(sys.argv[1])
    patch_sources(sys.argv[1])
