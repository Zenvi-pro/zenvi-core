#!/usr/bin/env python3
"""Package a zenvi CLI build as a Zenvi engine tools release the desktop installs.

Usage (from the repo root):

    python scripts/package_engine_cli.py ../zenvi-web/packages/cli/dist --out /tmp/engine-release
    python scripts/package_engine_cli.py DIST --out DIR --node /path/to/node

DIST is zenvi-web's ``node packages/cli/scripts/build.mjs --no-plugin`` output
(zenvi.mjs and what it loads). The CLI is started once
(``zenvi bridge --tools``) for its bridge protocol and engine version; then
DIR gets ``zenvi-cli-<version>.tar.gz`` and ``engine-cli.json``, the release
manifest classes/engine_runtime.py reads (an existing manifest keeps its other
releases). Point Preferences > AI > Zenvi Engine Tools Source (or
``ZENVI_ENGINE_SOURCE``) at the manifest -- its path, or its URL once DIR is
served -- and AI Tools > Zenvi Engine Tools... installs it.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile

MANIFEST = "engine-cli.json"


def describe(node: str, dist: str) -> dict:
    """The build's {"protocol", "engine_version"}, from the CLI itself."""
    done = subprocess.run([node, os.path.join(dist, "zenvi.mjs"), "bridge", "--tools"], capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise SystemExit(f"the CLI did not list its tools (exit {done.returncode}): {done.stderr.strip()[-400:]}")
    out = json.loads(done.stdout)
    return {"protocol": out["protocol"], "engine_version": str(out.get("engine_version") or "")}


def package(dist: str, out_dir: str, node: str, version: str = "") -> dict:
    """Write the archive and the manifest into *out_dir*; returns the release entry."""
    if not os.path.isfile(os.path.join(dist, "zenvi.mjs")):
        raise SystemExit(f"{dist} holds no zenvi.mjs (zenvi-web: node packages/cli/scripts/build.mjs --no-plugin)")
    about = describe(node, dist)
    version = version or about["engine_version"] or "0.0.0"
    os.makedirs(out_dir, exist_ok=True)
    name = f"zenvi-cli-{version}.tar.gz"
    archive = os.path.join(out_dir, name)
    with tarfile.open(archive + ".partial", "w:gz") as bundle:
        bundle.add(dist, arcname=f"zenvi-cli-{version}",
                   filter=lambda member: None if member.name.endswith(".map") else member)
    os.replace(archive + ".partial", archive)
    digest = hashlib.sha256()
    with open(archive, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    release = {"version": version, "protocol": about["protocol"], "url": name, "sha256": digest.hexdigest(),
               "size": os.path.getsize(archive)}
    manifest_path = os.path.join(out_dir, MANIFEST)
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        manifest = {}
    releases = [r for r in manifest.get("releases", []) if r.get("version") != version]
    manifest["releases"] = releases + [release]
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    return release


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dist", help="the zenvi CLI build folder (holds zenvi.mjs)")
    ap.add_argument("--out", required=True, help="folder for the archive and engine-cli.json")
    ap.add_argument("--node", default=shutil.which("node") or "node", help="Node.js 22+ to start the CLI with")
    ap.add_argument("--version", default="", help="release version (default: the CLI's engine version)")
    args = ap.parse_args()
    release = package(os.path.abspath(args.dist), os.path.abspath(args.out), args.node, args.version)
    print(json.dumps(release, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
