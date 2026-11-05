"""A DLL antivirus quarantined must be named in the startup error.

Sophos Home flagged lib\\libva.dll in v1.2.3 (an FFmpeg dependency, straight
from MSYS2) and deleted it. libopenshot then failed to load with Windows'
"The specified module could not be found", which never says which module, so
the user saw a generic import error. freeze.py now lists every shipped native
binary; on a failed import the diagnostic compares the install against it.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import openshot_import_diag as diag  # noqa: E402


def _fake_install(root: Path) -> Path:
    for rel in (
        "Zenvi.exe",
        "lib/libva.dll",
        "lib/avutil-60.dll",
        "lib/_openshot.cp314-mingw_x86_64_ucrt_gnu.pyd",
        "lib/openshot.py",
        "lib/library.zip",
        "lib/PyQt5/Qt5/plugins/imageformats/qjpeg.dll",
        "lib/whisper/whisper-cli.exe",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return root


def test_manifest_lists_native_binaries_only(tmp_path):
    install = _fake_install(tmp_path)

    path = diag.write_bundle_manifest(str(install))

    assert Path(path) == install / "lib" / diag.BUNDLE_MANIFEST
    assert Path(path).read_text(encoding="utf-8").splitlines() == [
        "Zenvi.exe",
        "lib/PyQt5/Qt5/plugins/imageformats/qjpeg.dll",
        "lib/_openshot.cp314-mingw_x86_64_ucrt_gnu.pyd",
        "lib/avutil-60.dll",
        "lib/libva.dll",
        "lib/whisper/whisper-cli.exe",
    ]


def test_quarantined_files_are_reported(tmp_path):
    install = _fake_install(tmp_path)
    diag.write_bundle_manifest(str(install))
    assert diag.missing_bundled_files(str(install)) == []

    (install / "lib" / "libva.dll").unlink()
    (install / "lib" / "PyQt5" / "Qt5" / "plugins" / "imageformats" / "qjpeg.dll").unlink()

    assert diag.missing_bundled_files(str(install)) == [
        "lib/PyQt5/Qt5/plugins/imageformats/qjpeg.dll",
        "lib/libva.dll",
    ]


def test_no_manifest_means_nothing_reported(tmp_path):
    install = _fake_install(tmp_path)
    (install / "lib" / "libva.dll").unlink()

    assert diag.missing_bundled_files(str(install)) == []


def test_message_names_files_folder_and_caps_the_list():
    missing = ["lib/libva.dll"] + ["lib/extra-%02d.dll" % i for i in range(12)]

    text = diag.missing_files_message(missing, r"C:\Program Files\Zenvi")

    assert "lib\\libva.dll" in text
    assert "lib\\extra-08.dll" in text
    assert "extra-09" not in text
    assert "...and 3 more" in text
    assert "antivirus" in text.lower()
    assert text.endswith(r"C:\Program Files\Zenvi")


def test_missing_files_notice_reads_this_install(tmp_path, monkeypatch):
    install = _fake_install(tmp_path)
    diag.write_bundle_manifest(str(install))
    monkeypatch.setattr(sys, "executable", str(install / "Zenvi.exe"))
    assert diag.missing_files_notice() == ""

    (install / "lib" / "libva.dll").unlink()

    assert "lib\\libva.dll" in diag.missing_files_notice()


def _run_windows_diagnostic(install: Path, monkeypatch) -> tuple[list, str]:
    boxes = []

    class _User32:
        @staticmethod
        def MessageBoxW(_hwnd, text, title, icon):
            boxes.append((text, title, icon))

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(install / "Zenvi.exe"))
    monkeypatch.setenv("TEMP", str(install))
    monkeypatch.setattr(ctypes, "windll", type("windll", (), {"user32": _User32}), raising=False)
    try:
        raise ImportError("DLL load failed while importing _openshot")
    except ImportError as exc:
        log_path = diag.write_openshot_import_diagnostic(exc, show_message_box=True)
    return boxes, Path(log_path).read_text(encoding="utf-8")


def test_startup_box_names_the_quarantined_dll(tmp_path, monkeypatch):
    install = _fake_install(tmp_path)
    diag.write_bundle_manifest(str(install))
    (install / "lib" / "libva.dll").unlink()

    boxes, log = _run_windows_diagnostic(install, monkeypatch)

    [(text, title, _icon)] = boxes
    assert title == "Zenvi — files missing"
    assert "lib\\libva.dll" in text
    assert "zenvi-openshot-import.log" in text
    assert "Missing from %s (1):\n  lib/libva.dll" % diag.BUNDLE_MANIFEST in log


def test_startup_box_stays_generic_when_nothing_is_missing(tmp_path, monkeypatch):
    install = _fake_install(tmp_path)
    diag.write_bundle_manifest(str(install))

    boxes, log = _run_windows_diagnostic(install, monkeypatch)

    [(text, title, _icon)] = boxes
    assert title == "Zenvi — DLL diagnostic"
    assert "OpenShot native libraries failed to load." in text
    assert "Missing from %s (0):" % diag.BUNDLE_MANIFEST in log
