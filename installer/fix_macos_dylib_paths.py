#!/usr/bin/env python3
"""Rewrite absolute Mach-O deps inside a frozen macOS build to @loader_path.

Mirrors the arm64 "Fix Mach-O rpath references" step in release.yml so local
DMG builds (scripts/build-local-mac.sh) produce a runnable .app without the
host .venv / Homebrew / zenvi-deps absolute paths that cause dyld to load a
second Qt and segfault on import openshot.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys

NON_EXEC_EXT = {
    ".py",
    ".svg",
    ".png",
    ".blend",
    ".a",
    ".pak",
    ".qm",
    ".pyc",
    ".txt",
    ".jpg",
    ".zip",
    ".dat",
    ".conf",
    ".xml",
    ".h",
    ".ui",
    ".json",
    ".exe",
    ".woff",
    ".html",
    ".css",
    ".js",
    ".pyx",
    ".pxd",
    ".md",
    ".rst",
    ".cfg",
    ".ini",
    ".ts",
    ".pot",
    ".icns",
    ".plist",
}
MACH_MAGIC = {
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


def is_system(path: str) -> bool:
    return (
        path.startswith("/usr/lib")
        or path.startswith("/System/")
        or path.startswith("/Library/Apple")
        or path.startswith("@executable_path")
        or path.startswith("@loader_path")
        or path.startswith("@rpath")
    )


def get_deps(binary: str) -> list[str]:
    try:
        out = subprocess.check_output(
            ["otool", "-L", binary], stderr=subprocess.DEVNULL
        ).decode()
    except Exception:
        return []
    seen: set[str] = set()
    deps: list[str] = []
    for line in out.split("\n")[1:]:
        line = line.strip()
        if " (architecture " in line or not line:
            continue
        path = line.split("(")[0].strip()
        if path and path not in seen:
            seen.add(path)
            deps.append(path)
    return deps


def get_rpaths(binary: str) -> list[str]:
    try:
        out = subprocess.check_output(
            ["otool", "-l", binary], stderr=subprocess.DEVNULL
        ).decode()
    except Exception:
        return []
    rpaths: list[str] = []
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "LC_RPATH" in line:
            for j in range(i + 1, min(i + 6, len(lines))):
                m = re.search(r"path\s+(\S+)\s+\(offset", lines[j])
                if m:
                    rpaths.append(m.group(1))
                    break
    return rpaths


def find_in_bundle(name: str, frozen_dir: str) -> str | None:
    for root, _dirs, files in os.walk(frozen_dir):
        if name in files:
            return os.path.join(root, name)
    return None


def is_macho(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in MACH_MAGIC
    except Exception:
        return False


def delete_host_rpaths(binary: str) -> int:
    removed = 0
    for rpath in get_rpaths(binary):
        if rpath.startswith("@") or rpath.startswith("/usr/lib"):
            continue
        # Host absolute rpaths (.venv, zenvi-deps, Cellar) break portable apps.
        os.chmod(binary, os.stat(binary).st_mode | 0o200)
        ret = subprocess.call(
            ["install_name_tool", "-delete_rpath", rpath, binary],
            stderr=subprocess.DEVNULL,
        )
        if ret == 0:
            print(f"RPATH- {os.path.basename(binary)}: removed {rpath}")
            removed += 1
    return removed


def fix_frozen_dir(frozen_dir: str) -> int:
    if not os.path.isdir(frozen_dir):
        raise SystemExit(f"Not a directory: {frozen_dir}")

    fixed = 0
    rpaths_removed = 0
    for root, _dirs, files in os.walk(frozen_dir):
        for basename in files:
            fp = os.path.join(root, basename)
            if os.path.splitext(fp)[-1].lower() in NON_EXEC_EXT or basename.startswith("."):
                continue
            if not is_macho(fp):
                continue

            rpaths_removed += delete_host_rpaths(fp)

            for dep in get_deps(fp):
                # Absolute host paths always need rewrite. @rpath framework-style
                # Qt refs also need rewrite onto cx_Freeze's flat lib/QtCore layout.
                framework_style = dep.startswith("@rpath/") and ".framework/" in dep
                if is_system(dep) and not framework_style:
                    continue
                if framework_style:
                    dep_name = dep.rsplit("/", 1)[-1]
                else:
                    dep_name = os.path.basename(dep)
                dep_in_bundle = find_in_bundle(dep_name, frozen_dir)
                if dep_in_bundle:
                    rel = os.path.relpath(dep_in_bundle, root)
                    new_ref = "@loader_path/" + rel
                    if dep != new_ref:
                        os.chmod(fp, os.stat(fp).st_mode | 0o200)
                        ret = subprocess.call(
                            ["install_name_tool", "-change", dep, new_ref, fp],
                            stderr=subprocess.DEVNULL,
                        )
                        if ret == 0:
                            print(
                                f"OK  {os.path.relpath(fp, frozen_dir)}: "
                                f"{dep_name} -> {new_ref}"
                            )
                            fixed += 1
                else:
                    print(
                        f"MISS {os.path.relpath(fp, frozen_dir)}: "
                        f"{dep_name} not found (from {dep})"
                    )
    print(f"Pass 1: fixed {fixed} absolute path references; removed {rpaths_removed} host rpaths")

    # Pass 2: versioned libopenshot sonames for _openshot.so
    lib_dir = os.path.join(frozen_dir, "lib")
    if not os.path.isdir(lib_dir):
        for candidate in (
            os.path.join(frozen_dir, "Resources", "lib"),
            os.path.join(frozen_dir, "MacOS", "lib"),
        ):
            if os.path.isdir(candidate):
                lib_dir = os.path.realpath(candidate)
                break
    os.makedirs(lib_dir, exist_ok=True)
    openshot_so = find_in_bundle("_openshot.so", frozen_dir)
    if not openshot_so:
        for root, _dirs, files in os.walk(frozen_dir):
            for name in files:
                if name.startswith("_openshot.cpython") and name.endswith(".so"):
                    openshot_so = os.path.join(root, name)
                    break
            if openshot_so:
                break

    soname_re = re.compile(r"^(libopenshot(?:-audio)?)\.(\d+)\.dylib$")
    soname_fixups: list[tuple[str, str]] = []
    for dep in get_deps(openshot_so) if openshot_so else []:
        m = soname_re.match(os.path.basename(dep))
        if m and (dep.startswith("@rpath/") or dep.startswith("@loader_path/") or dep.startswith("/")):
            soname_fixups.append((m.group(0), m.group(1)))
    if not soname_fixups:
        soname_fixups = [
            ("libopenshot-audio.10.dylib", "libopenshot-audio"),
            ("libopenshot.28.dylib", "libopenshot"),
        ]

    for soname, prefix in soname_fixups:
        src = None
        versioned = sorted(
            glob.glob(
                os.path.join(frozen_dir, "**", prefix + ".*.*.*.dylib"),
                recursive=True,
            )
        )
        if versioned:
            src = versioned[0]
        else:
            src = find_in_bundle(prefix + ".dylib", frozen_dir) or find_in_bundle(
                soname, frozen_dir
            )
        if not src:
            print(f"WARNING: source for {soname} not found in bundle")
            continue
        soname_path = os.path.join(lib_dir, soname)
        if not os.path.exists(soname_path):
            shutil.copy2(src, soname_path)
            subprocess.call(
                ["install_name_tool", "-id", f"@loader_path/{soname}", soname_path],
                stderr=subprocess.DEVNULL,
            )
            print(f"Created lib/{soname} (copy of {os.path.basename(src)})")
        if openshot_so:
            openshot_dir = os.path.dirname(openshot_so)
            rel = os.path.relpath(soname_path, openshot_dir)
            os.chmod(openshot_so, os.stat(openshot_so).st_mode | 0o200)
            for old in (
                f"@rpath/{soname}",
                f"@loader_path/{soname}",
                os.path.join(os.path.expanduser("~/zenvi-deps/lib"), soname),
            ):
                subprocess.call(
                    ["install_name_tool", "-change", old, f"@loader_path/{rel}", openshot_so],
                    stderr=subprocess.DEVNULL,
                )
            # Also rewrite whatever absolute path otool currently shows.
            for dep in get_deps(openshot_so):
                if os.path.basename(dep) == soname and not dep.startswith("@loader_path/"):
                    subprocess.call(
                        [
                            "install_name_tool",
                            "-change",
                            dep,
                            f"@loader_path/{rel}",
                            openshot_so,
                        ],
                        stderr=subprocess.DEVNULL,
                    )
                    print(
                        f"Fixed {soname} in {os.path.basename(openshot_so)} "
                        f"-> @loader_path/{rel}"
                    )

    if openshot_so:
        delete_host_rpaths(openshot_so)
        print(f"_openshot deps after fix:")
        for dep in get_deps(openshot_so):
            print(f"  {dep}")

    return fixed


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <frozen-dir-or-Contents/MacOS>", file=sys.stderr)
        raise SystemExit(2)
    fix_frozen_dir(os.path.abspath(sys.argv[1]))


if __name__ == "__main__":
    main()
