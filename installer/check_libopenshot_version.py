"""Fail when the libopenshot a build is about to ship is older than the app needs.

The app refuses to start below MINIMUM_LIBOPENSHOT_VERSION (src/classes/info.py),
so a build that bundles an older one is broken for every user. Reads the minimum
from info.py as text (no app imports), then compares it with either:

  --version X.Y.Z   a version the caller already knows (macOS: from the dylib name)
  (no flag)         openshot.OPENSHOT_VERSION_FULL from the importable bindings
"""
import argparse
import pathlib
import re
import sys


def parse(text: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def minimum() -> str:
    info = pathlib.Path(__file__).resolve().parent.parent / "src" / "classes" / "info.py"
    m = re.search(r'^MINIMUM_LIBOPENSHOT_VERSION\s*=\s*"([^"]+)"', info.read_text(), re.M)
    if not m:
        raise SystemExit("::error::MINIMUM_LIBOPENSHOT_VERSION not found in src/classes/info.py")
    return m.group(1)


def check(found: str) -> int:
    need = minimum()
    if parse(found) < parse(need):
        print("::error::libopenshot %s is older than the %s the app requires; "
              "users would see 'Wrong Version of libopenshot Detected'." % (found, need))
        return 1
    print("libopenshot %s satisfies minimum %s" % (found, need))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version")
    args = ap.parse_args()
    if args.version:
        return check(args.version)
    import openshot
    return check(openshot.OPENSHOT_VERSION_FULL)


if __name__ == "__main__":
    sys.exit(main())
