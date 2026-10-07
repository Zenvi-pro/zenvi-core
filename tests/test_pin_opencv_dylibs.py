"""Frozen mac bundle must carry the libtiff OpenCV links, not a stale copy."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from installer.pin_opencv_dylibs import pin_opencv_dylibs  # noqa: E402


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_stale_bundled_libtiff_is_replaced_and_missing_deps_added(tmp_path):
    build = tmp_path / "homebrew"
    new_tiff = _write(build / "libtiff.6.dylib", "tiff-4.7")
    zstd = _write(build / "libzstd.1.dylib", "zstd")
    frozen = tmp_path / "exe"
    _write(frozen / "libopencv_imgcodecs.414.dylib", "cv")
    stale = _write(frozen / "lib" / "libtiff.6.dylib", "tiff-4.6")
    pillow = _write(frozen / "lib" / "PIL" / ".dylibs" / "libtiff.6.dylib", "pil")

    graph = {"libopencv_imgcodecs.414.dylib": [str(new_tiff)],
             "libtiff.6.dylib": [str(zstd)]}
    changed = pin_opencv_dylibs(str(frozen), lambda p: graph.get(pathlib.Path(p).name, []))

    assert stale.read_text() == "tiff-4.7"
    assert pillow.read_text() == "pil"  # Pillow keeps its private copy
    assert (frozen / "lib" / "libzstd.1.dylib").read_text() == "zstd"
    assert str(stale) in changed


def test_already_correct_bundle_is_untouched(tmp_path):
    new_tiff = _write(tmp_path / "hb" / "libtiff.6.dylib", "tiff-4.7")
    frozen = tmp_path / "exe"
    _write(frozen / "libopencv_core.414.dylib", "cv")
    bundled = _write(frozen / "libtiff.6.dylib", "tiff-4.7")
    bundled.unlink()
    bundled.symlink_to(new_tiff)
    changed = pin_opencv_dylibs(
        str(frozen), lambda p: [str(new_tiff)] if "opencv" in p else [])
    assert changed == []
