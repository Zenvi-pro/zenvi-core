#!/usr/bin/env python3
"""Install or check Zenvi's engine tools runtime (Node.js + the zenvi CLI) without the GUI.

Usage (from the repo root; no Qt needed):

    python scripts/install_engine_runtime.py --status
    python scripts/install_engine_runtime.py --source /tmp/engine-release/engine-cli.json
    python scripts/install_engine_runtime.py --source ../zenvi-web/packages/cli/dist

The same install AI Tools > Zenvi Engine Tools... runs (classes/engine_runtime.py),
into ``~/.openshot_qt/engine`` (``--user-path`` for another settings folder).
``--source`` defaults to ``ZENVI_ENGINE_SOURCE``. Ctrl+C cancels and leaves what
was installed before. Exit 0 when the runtime is ready, 1 when not, 2 on a
refused install.
"""

import argparse
import json
import os
import sys
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=None, help="release manifest (URL or .json file) or a zenvi CLI build folder")
    ap.add_argument("--status", action="store_true", help="only report what is installed")
    ap.add_argument("--user-path", default="", help="settings folder instead of ~/.openshot_qt")
    args = ap.parse_args()

    from classes import engine_runtime, info
    if args.user_path:
        info.USER_PATH = os.path.abspath(args.user_path)
    if args.status:
        status = engine_runtime.status()
        print(json.dumps(status, indent=2))
        return 0 if status["ready"] else 1

    cancel = threading.Event()
    last = {"text": ""}

    def progress(text, done, total):
        line = f"{text}: {done / 1e6:.1f} of {total / 1e6:.1f} MB" if total else text
        if line != last["text"]:
            last["text"] = line
            print("\r" + line.ljust(70), end="" if total else "\n", flush=True)

    try:
        status = engine_runtime.install(args.source, progress=progress, cancel=cancel)
    except KeyboardInterrupt:
        cancel.set()
        print("\nCancelled")
        return 2
    except engine_runtime.RuntimeInstallError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    print()
    print(json.dumps(status, indent=2))
    return 0 if status["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
