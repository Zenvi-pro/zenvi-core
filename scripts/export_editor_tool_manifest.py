#!/usr/bin/env python3
"""Write the editor-tool manifest that zenvi-backend builds its tool stubs from.

The backend declares every frontend-delegated tool a second time (the schema
the model sees). For tools registered in ``classes.editor_tools`` that second
declaration is generated from this manifest instead of written by hand, so a
renamed argument or a new enum value cannot drift between the two repos.

Usage (from the repo root; runs headless, no Qt or libopenshot needed):

    python scripts/export_editor_tool_manifest.py                  # print to stdout
    python scripts/export_editor_tool_manifest.py --out ../zenvi-backend/core/tools/openshot/editor_tools_manifest.json
    python scripts/export_editor_tool_manifest.py --check PATH      # exit 1 if PATH is stale
"""

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "src"))

MANIFEST_VERSION = 1


def build_manifest() -> dict:
    import conftest  # noqa: F401  -- installs the headless Qt/openshot stubs
    from classes.editor_tools import REGISTRY, workstream_of

    tools = []
    for name in sorted(REGISTRY):
        spec = REGISTRY[name]
        tools.append({
            "name": name,
            "label": spec.label,
            "workstream": workstream_of(spec.domain),
            "read_only": spec.read_only,
            "description": spec.description,
            "input_schema": spec.schema,
            "covers": list(spec.covers),
        })
    from classes.edit_playbook import playbook_manifest

    return {"manifest_version": MANIFEST_VERSION, "source": "zenvi-core src/classes/editor_tools",
            "playbook": playbook_manifest(), "tools": tools}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the manifest here")
    ap.add_argument("--check", help="compare against this manifest; exit 1 when it differs")
    args = ap.parse_args()
    text = json.dumps(build_manifest(), indent=1, sort_keys=True) + "\n"
    if args.check:
        try:
            with open(args.check, encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            current = ""
        if current != text:
            print(f"{args.check} is stale; regenerate with --out", file=sys.stderr)
            return 1
        return 0
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
