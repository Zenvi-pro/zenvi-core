#!/usr/bin/env python3
"""Refuse to ship a macOS app that cannot start on the CPU it is labelled for.

Every Mac updater installs the DMG named for its architecture, so a bad one
breaks every Mac on that architecture at once. v1.2.1's "x86_64" DMG was built
on an Apple Silicon runner: its binaries were arm64-only and libopenshot was
left out, so it could not open on any Intel Mac.

Checks a .app bundle (or a cx_Freeze output folder):
  * every Mach-O file (thin or universal) contains the requested architecture;
  * libopenshot and its Python binding (_openshot*.so) are bundled.

Pure Python (reads Mach-O headers itself), so it runs anywhere.

Usage: verify_mac_bundle.py --arch x86_64|arm64 Zenvi.app
"""

import argparse
import os
import struct
import sys

CPU_TYPES = {
    "x86_64": 0x01000007,  # CPU_TYPE_X86_64
    "arm64": 0x0100000C,   # CPU_TYPE_ARM64
}
_ARCH_NAMES = {v: k for k, v in CPU_TYPES.items()}
_ARCH_NAMES.update({7: "i386", 12: "arm"})

_THIN_LE = (0xFEEDFACE, 0xFEEDFACF)          # MH_MAGIC, MH_MAGIC_64 read little-endian
_THIN_BE = (0xCEFAEDFE, 0xCFFAEDFE)          # the same, byte-swapped
_FAT = (0xCAFEBABE, 0xCAFEBABF)              # FAT_MAGIC, FAT_MAGIC_64 (big-endian)


def macho_archs(path):
    """The CPU types in a Mach-O file, or None when it isn't one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
            if len(head) < 8:
                return None
            magic_be = struct.unpack(">I", head[:4])[0]
            magic_le = struct.unpack("<I", head[:4])[0]
            if magic_be in _FAT:
                count = struct.unpack(">I", head[4:8])[0]
                # Java class files share 0xCAFEBABE; they have a large "count".
                if count == 0 or count > 30:
                    return None
                entry = 32 if magic_be == 0xCAFEBABF else 20
                archs = []
                for _ in range(count):
                    data = fh.read(entry)
                    if len(data) < entry:
                        return None
                    archs.append(struct.unpack(">i", data[:4])[0] & 0xFFFFFFFF)
                return archs
            if magic_le in _THIN_LE:
                return [struct.unpack("<i", head[4:8])[0] & 0xFFFFFFFF]
            if magic_le in _THIN_BE:
                return [struct.unpack(">i", head[4:8])[0] & 0xFFFFFFFF]
    except OSError:
        return None
    return None


def check(root, arch):
    """(problems, stats) for the bundle at *root*."""
    want = CPU_TYPES[arch]
    problems = []
    machos = 0
    wrong = []
    has_binding = False
    has_lib = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            if name.startswith("_openshot") and name.endswith(".so"):
                has_binding = True
            if name.startswith("libopenshot.") and ".dylib" in name:
                has_lib = True
            archs = macho_archs(path)
            if archs is None:
                continue
            machos += 1
            if want not in archs:
                wrong.append((os.path.relpath(path, root),
                              "/".join(_ARCH_NAMES.get(a, hex(a)) for a in archs)))
    if machos == 0:
        problems.append("no Mach-O files found: is this a macOS app?")
    if wrong:
        sample = ", ".join(f"{p} ({a})" for p, a in wrong[:8])
        problems.append(f"{len(wrong)} of {machos} Mach-O files lack {arch}: {sample}")
    if not has_lib:
        problems.append("libopenshot*.dylib is not bundled")
    if not has_binding:
        problems.append("the openshot Python binding (_openshot*.so) is not bundled")
    return problems, {"machos": machos, "wrong_arch": len(wrong)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--arch", required=True, choices=sorted(CPU_TYPES))
    parser.add_argument("bundle")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.bundle):
        print(f"::error::{args.bundle} is not a folder")
        return 2
    problems, stats = check(args.bundle, args.arch)
    if problems:
        for problem in problems:
            print(f"::error::{args.arch} app: {problem}")
        return 1
    print(f"{args.bundle}: all {stats['machos']} Mach-O files contain {args.arch}; "
          "libopenshot and its binding are bundled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
