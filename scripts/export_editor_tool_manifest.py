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
    python scripts/export_editor_tool_manifest.py --engine-catalog catalog.json   # + engine-bridge tools

``--engine-catalog`` takes the engine's catalog as the bridge runs it
(``node zenvi.mjs bridge --tools > catalog.json``) and adds the engine tools
the desktop lists through the engine bridge (classes/editor_tools/engine_bridge.py):
those it does not implement itself, with the engine's schema, workstream
``engine`` and ``surface`` ``engine-bridge`` (``desktop`` for the desktop's
linked-clip tools over engine renders). The desktop only runs them with the
bridge on, so a backend should offer them only to a desktop that lists them.
"""

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "src"))

MANIFEST_VERSION = 1


def engine_entries(catalog_path: str) -> list:
    """Manifest entries of the engine tools the desktop lists through the bridge."""
    from classes import tool_handlers
    from classes.agent_api_proxy import MCP_EXTRA_TOOLS
    from classes.editor_tools import engine_bridge

    with open(catalog_path, encoding="utf-8") as fh:
        catalog = json.load(fh)
    tools = catalog.get("tools") if isinstance(catalog, dict) else catalog
    if not isinstance(tools, list):
        # catalog=None would mean "ask the live bridge", which is off here: a manifest without engine tools.
        raise SystemExit(f"{catalog_path}: not an engine catalog (no tools list)")
    engine_bridge.BRIDGED.clear()
    engine_bridge.register_tools(set(tool_handlers.AGENT_TOOL_HANDLERS) | set(MCP_EXTRA_TOOLS), catalog=tools)
    return engine_bridge.manifest_entries()


def build_manifest(engine_catalog: str = "") -> dict:
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
    if engine_catalog:
        tools.extend(engine_entries(engine_catalog))
    return {"manifest_version": MANIFEST_VERSION, "source": "zenvi-core src/classes/editor_tools",
            "tools": tools}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the manifest here")
    ap.add_argument("--check", help="compare against this manifest; exit 1 when it differs")
    ap.add_argument("--engine-catalog", default="",
                    help="also list the engine-bridge tools of this catalog (zenvi bridge --tools JSON)")
    args = ap.parse_args()
    # The manifest lists the catalog given, never a live bridge's: with ZENVI_ENGINE_BRIDGE set in the
    # shell, importing tool_handlers would read the CLI's catalog on a thread into the same BRIDGED.
    os.environ["ZENVI_ENGINE_BRIDGE"] = "off"
    text = json.dumps(build_manifest(args.engine_catalog), indent=1, sort_keys=True) + "\n"
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
