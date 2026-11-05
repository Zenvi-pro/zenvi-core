"""
Windows-only helpers to diagnose missing DLLs when importing native OpenShot bindings.
Writes %TEMP%\\zenvi-openshot-import.log (append).
"""

import glob
import os
import sys
import traceback
from typing import List, Optional

# Written into lib/ at freeze time: every native binary the build ships, one
# install-relative path per line. Windows never names the dependency a failed
# DLL load was missing, so this list is how a startup error can.
BUNDLE_MANIFEST = "bundled-native-files.txt"
_NATIVE_EXTENSIONS = (".dll", ".pyd", ".exe")


def write_bundle_manifest(install_dir: str) -> str:
    """List every native binary under ``install_dir`` in lib/BUNDLE_MANIFEST; return its path."""
    names = []
    for root, _dirs, files in os.walk(install_dir):
        for name in files:
            if name.lower().endswith(_NATIVE_EXTENSIONS):
                rel = os.path.relpath(os.path.join(root, name), install_dir)
                names.append(rel.replace(os.sep, "/"))
    path = os.path.join(install_dir, "lib", BUNDLE_MANIFEST)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join("%s\n" % n for n in sorted(names)))
    return path


def missing_bundled_files(install_dir: str) -> List[str]:
    """Manifest entries no longer on disk (quarantined by antivirus, usually).

    Empty when the install has no manifest (source checkouts, older builds).
    """
    try:
        with open(os.path.join(install_dir, "lib", BUNDLE_MANIFEST), encoding="utf-8") as fh:
            names = [line.strip() for line in fh if line.strip()]
    except OSError:
        return []
    return [n for n in names if not os.path.isfile(os.path.join(install_dir, *n.split("/")))]


def missing_files_message(missing: List[str], install_dir: str) -> str:
    """Plain-language startup error for files gone from the install folder."""
    shown = missing[:10]
    listing = "\n".join("  %s" % n.replace("/", "\\") for n in shown)
    if len(missing) > len(shown):
        listing += "\n  ...and %d more" % (len(missing) - len(shown))
    return (
        "Zenvi can't start because files it needs are missing from its install folder:\n\n"
        "%s\n\n"
        "Antivirus software sometimes removes these by mistake. In your antivirus, add an "
        "exception for this folder, then restore the files from quarantine or reinstall Zenvi:\n"
        "  %s" % (listing, install_dir)
    )


def missing_files_notice() -> str:
    """missing_files_message() for this install, or "" when nothing is missing."""
    install_dir = os.path.dirname(os.path.abspath(sys.executable))
    missing = missing_bundled_files(install_dir)
    return missing_files_message(missing, install_dir) if missing else ""


def write_openshot_import_diagnostic(exc: BaseException, *, show_message_box: bool = False) -> Optional[str]:
    """
    Append diagnosis for a failed ``import openshot`` / ``_openshot`` load.

    Returns absolute path to the log file, or None if skipped/unwritable.
    """
    if sys.platform != "win32":
        return None
    lines = [
        "",
        "=" * 72,
        "Zenvi: OpenShot native module import failed",
        "=" * 72,
        "Exception: %r" % (exc,),
        traceback.format_exc(),
        "frozen=%r" % getattr(sys, "frozen", False),
        "executable=%s" % sys.executable,
        "base_prefix=%s" % getattr(sys, "base_prefix", ""),
    ]
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    lib_dir = os.path.join(exe_dir, "lib")
    lines.append("install_dir (exe_dir)=%s" % exe_dir)
    lines.append("lib_dir=%s exists=%s" % (lib_dir, os.path.isdir(lib_dir)))
    missing = missing_bundled_files(exe_dir)
    if os.path.isfile(os.path.join(lib_dir, BUNDLE_MANIFEST)):
        lines.append("Missing from %s (%d):" % (BUNDLE_MANIFEST, len(missing)))
        for n in missing:
            lines.append("  %s" % n)
    else:
        lines.append("%s not found: missing-file check skipped" % BUNDLE_MANIFEST)
    if os.path.isdir(lib_dir):
        dlls = sorted(glob.glob(os.path.join(lib_dir, "*.dll")))
        lines.append("DLLs in lib/ (%d):" % len(dlls))
        for p in dlls:
            lines.append("  %s" % os.path.basename(p))
        for hint in (
            "avcodec",
            "avutil",
            "avformat",
            "swscale",
            "swresample",
            "libopenshot",
            "libbabl",
            "liblcms2",
            "libjsoncpp",
        ):
            hits = [
                os.path.basename(x)
                for x in dlls
                if os.path.basename(x).lower().startswith(hint.lower())
            ]
            lines.append("  %s*: %s" % (hint, ", ".join(hits) if hits else "MISSING"))
        lo = os.path.join(lib_dir, "libopenshot.dll")
        pyd = glob.glob(os.path.join(lib_dir, "_openshot*.pyd"))
        lines.append("libopenshot.dll present=%s" % os.path.isfile(lo))
        lines.append("_openshot*.pyd=%s" % [os.path.basename(x) for x in pyd])
        try:
            import ctypes

            add = getattr(os, "add_dll_directory", None)
            if add:
                try:
                    add(lib_dir)
                except OSError as oe:
                    lines.append("add_dll_directory(lib_dir): %s" % oe)
            babl_ext = os.path.join(lib_dir, "babl-ext")
            if os.path.isdir(babl_ext) and add:
                try:
                    add(babl_ext)
                except OSError as oe:
                    lines.append("add_dll_directory(babl-ext): %s" % oe)
            if os.path.isfile(lo):
                try:
                    ctypes.CDLL(lo)
                    lines.append("ctypes.CDLL(libopenshot.dll): loaded OK")
                except OSError as oe:
                    lines.append(
                        "ctypes.CDLL(libopenshot.dll): FAILED — often means a dependency DLL "
                        "of libopenshot.dll is missing from lib/. WinError detail: %s" % oe
                    )
        except Exception as tools_exc:
            lines.append("ctypes probe skipped: %s" % tools_exc)
    lines.append(
        "External tools: open lib\\\\libopenshot.dll in Dependencies "
        "(https://github.com/lucasg/Dependencies/releases) "
        "or run: dumpbin /dependents lib\\\\libopenshot.dll"
    )
    lines.append("=" * 72)
    log_path = os.path.join(os.environ.get("TEMP", "."), "zenvi-openshot-import.log")
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        return None
    abs_log = os.path.abspath(log_path)
    err = sys.stderr
    if err is not None:
        try:
            err.write(
                "\nZenvi: OpenShot DLL diagnostic written to:\n  %s\n" % abs_log
            )
        except OSError:
            pass
    if show_message_box:
        if missing:
            text = "%s\n\nA detailed report was saved to:\n%s" % (
                missing_files_message(missing, exe_dir), abs_log)
            title, icon = "Zenvi — files missing", 0x30  # MB_ICONWARNING
        else:
            text = (
                "OpenShot native libraries failed to load.\n\n"
                "A detailed report was saved to:\n%s\n\n"
                "Send that file when asking for help." % abs_log
            )
            title, icon = "Zenvi — DLL diagnostic", 0x40  # MB_ICONINFORMATION
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, icon)
        except Exception:
            pass
    return abs_log
