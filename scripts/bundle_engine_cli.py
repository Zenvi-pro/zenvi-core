#!/usr/bin/env python3
"""Put a zenvi CLI build into src/engine so a release build ships Zenvi engine tools.

Usage (from the repo root, before freeze.py):

    python scripts/bundle_engine_cli.py ../zenvi-web/packages/cli/dist --node node

DIST is zenvi-web's ``npm run build -w @zenvi/cli -- --no-plugin`` output.
freeze.py copies all of src/ into the app's lib/, where
classes/engine_bridge.py finds ``<app>/engine/zenvi.mjs``. The CLI is started
once (``zenvi bridge --tools``): a build that does not speak this desktop's
bridge protocol (engine_bridge.PROTOCOL) fails the release and nothing is copied.
"""

import argparse
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from package_engine_cli import describe  # noqa: E402


def main() -> int:
    from classes import engine_bridge

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dist", help="the zenvi CLI build folder (holds zenvi.mjs)")
    ap.add_argument("--node", default=shutil.which("node") or "node", help="Node.js 22+ to start the CLI with")
    ap.add_argument("--dest", default=os.path.join(ROOT, "src", "engine"), help="where to put it (default src/engine)")
    args = ap.parse_args()
    dist, dest = os.path.abspath(args.dist), os.path.abspath(args.dest)
    if not os.path.isfile(os.path.join(dist, engine_bridge.CLI_NAME)):
        raise SystemExit(f"{dist} holds no {engine_bridge.CLI_NAME} (zenvi-web: npm run build -w @zenvi/cli)")
    about = describe(args.node, dist)
    if about["protocol"] != engine_bridge.PROTOCOL:
        raise SystemExit(f"the zenvi CLI in {dist} speaks bridge protocol {about['protocol']}, this desktop "
                         f"{engine_bridge.PROTOCOL} (src/classes/engine_bridge.py PROTOCOL): build a matching zenvi-web")
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(dist, dest, ignore=shutil.ignore_patterns("node_modules", "*.map"))
    print(f"bundled zenvi CLI {about['engine_version']} (bridge protocol {about['protocol']}) into {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
