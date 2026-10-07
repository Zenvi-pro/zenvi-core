"""Make the frozen macOS bundle ship the dylibs OpenCV was actually built against.

cx_Freeze keeps ONE copy per dylib basename, whichever consumer it resolves
first (Pillow's ``PIL/.dylibs``, Qt image plugins, Homebrew, ...). When that
copy is older than the one OpenCV links, the app dies at import time:

    Symbol not found: _TIFFOpenOptionsSetWarnAboutUnknownTags
    Referenced from: libopencv_imgcodecs.414.dylib
    Expected in:     Resources/lib/libtiff.6.dylib

Run this after freeze and before ``fix_rpath``: for every non-system dylib an
OpenCV library references by absolute path, overwrite the bundled copies of
that basename with the referenced file and pull in any of its own dependencies
the bundle is missing.

This only decides WHICH file is bundled. References to it are still host paths
(Homebrew is not under /usr/local on arm64, so ``fix_rpath`` leaves them); the
pass that follows freeze rewrites them to ``@loader_path``: the "Fix Mach-O
rpath references" step in release.yml and ``fix_macos_dylib_paths.py`` in
scripts/build-local-mac.sh.
"""

import os
import shutil
import subprocess  # nosec B404 - fixed local macOS tooling.

# libssl/libcrypto are reconciled by their own release.yml step.
SKIP_PREFIXES = ("libssl", "libcrypto")
SYSTEM_PREFIXES = ("/usr/lib/", "/System/", "/Library/Apple/")


def otool_dependencies(path):
    out = subprocess.check_output(  # nosec B603 B607 - fixed tool, no shell.
        ["/usr/bin/otool", "-L", path], text=True, stderr=subprocess.DEVNULL)
    deps = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line:
            deps.append(line.split(" (", 1)[0])
    return deps


def _is_external(dep):
    return (os.path.isabs(dep) and not dep.startswith(SYSTEM_PREFIXES)
            and not os.path.basename(dep).startswith(SKIP_PREFIXES))


def _bundled_copies(frozen_dir, name):
    """Every bundled file called `name`, except Pillow's private copies."""
    found = []
    for root, _dirs, files in os.walk(frozen_dir):
        if name in files and ".dylibs" not in root.split(os.sep):
            found.append(os.path.join(root, name))
    return found


def _install(src, dst):
    if os.path.exists(dst):
        os.chmod(dst, 0o644)
    shutil.copy2(src, dst)
    os.chmod(dst, 0o755)


def pin_opencv_dylibs(frozen_dir, deps_of=otool_dependencies):
    """Return the list of bundled files that were replaced or added."""
    changed = []
    opencv = [os.path.join(root, f)
              for root, _d, files in os.walk(frozen_dir)
              for f in files if "opencv" in f and f.endswith(".dylib")]
    pending = [d for lib in opencv for d in deps_of(lib) if _is_external(d)]
    seen = set()
    while pending:
        src = pending.pop()
        name = os.path.basename(src)
        if name in seen or not os.path.exists(src):
            continue
        seen.add(name)
        copies = _bundled_copies(frozen_dir, name)
        if not copies:
            # A dependency of a pinned lib the bundle never had.
            copies = [os.path.join(frozen_dir, "lib", name)]
            os.makedirs(os.path.dirname(copies[0]), exist_ok=True)
        for dst in copies:
            if os.path.exists(dst) and os.path.samefile(src, dst):
                continue
            _install(src, dst)
            changed.append(dst)
        pending.extend(d for d in deps_of(src) if _is_external(d))
    return changed
