"""
Windows-only helpers to diagnose missing DLLs when importing native OpenShot bindings.
Writes %TEMP%\\zenvi-openshot-import.log (append).
"""

import glob
import os
import sys
import traceback
from typing import Optional


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
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "OpenShot native libraries failed to load.\n\n"
                "A detailed report was saved to:\n%s\n\n"
                "Send that file when asking for help." % abs_log,
                "Zenvi — DLL diagnostic",
                0x40,
            )
        except Exception:
            pass
    return abs_log
