"""Engine bridge timings on a 300-clip project (zenvi-web docs/ENGINE_BRIDGE.md).

    ZENVI_ENGINE_CLI=<zenvi-web>/packages/cli/dist/zenvi.mjs \\
        .venv/bin/python tests/benchmarks/bench_engine_bridge.py [--clips 300] [--runs 10]

Headless (the pytest stubs, the editor-tools harness): the desktop's real
ProjectDataStore + UpdateManager, the real CLI. Prints one JSON object with
median milliseconds per phase for each tool and process model.
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "src")]

import conftest  # noqa: E402,F401  (installs the headless Qt / openshot stubs)


def ms(since):
    return round((time.perf_counter() - since) * 1000, 1)


def build_editor(clips, folder):
    from editor_tools_harness import make_editor

    editor = make_editor()
    files = [editor.add_file("video", path=os.path.join(folder, f"shot{i:03}.mp4"), duration=4.0) for i in range(30)]
    for i in range(clips):
        editor.add_clip(files[i % len(files)], position=(i // 3) * 2.0, layer=1000000 * (1 + i % 3), end=2.0)
    editor.store.current_filepath = os.path.join(folder, "bench.zvn")
    return editor


def measure(editor, client, tool, args, runs):
    """Phases of one call as run_tool does them, with the real apply (undone after each run)."""
    from classes.editor_tools import engine_bridge as bridged

    rows = []
    for _ in range(runs):
        t = time.perf_counter()
        project_json, project_file, version = bridged._snapshot()
        snapshot = ms(t)
        t = time.perf_counter()
        answer = client.run(tool, args, project_json, project_file)
        round_trip = ms(t)
        assert answer["ok"], answer["text"][:300]
        t = time.perf_counter()
        editor.mark()
        with_tid = editor.app.updates
        with_tid.transaction_id = "bench"
        try:
            assert bridged._apply(answer, project_file, version) is None
        finally:
            with_tid.transaction_id = None
        apply = ms(t)
        editor.undo()
        rows.append({"snapshot_ms": snapshot, "round_trip_ms": round_trip, "apply_ms": apply,
                     "end_to_end_ms": round(snapshot + round_trip + apply, 1), **answer["timings"],
                     "actions": len(answer["actions"]), "request_kb": round(len(project_json) / 1024)})
    keys = [k for k in rows[0] if k != "actions"]
    out = {k: statistics.median(r[k] for r in rows) for k in keys}
    out["actions"] = rows[0]["actions"]
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips", type=int, default=300)
    parser.add_argument("--runs", type=int, default=10)
    opts = parser.parse_args()
    cli = os.environ["ZENVI_ENGINE_CLI"]
    node = os.environ.get("ZENVI_NODE") or shutil.which("node")

    from classes import engine_bridge

    folder = tempfile.mkdtemp(prefix="zenvi-bridge-bench-")
    try:
        editor = build_editor(opts.clips, folder)
        first = editor.clips()[0]["id"]
        tools = {
            "move_clip_tool": {"clip_id": first, "position_seconds": 1.5},
            "add_motion_template_tool": {"template": "grid_background", "variant": "paper", "duration_seconds": 2},
        }
        startups = []
        for _ in range(5):
            t = time.perf_counter()
            subprocess.run([node, cli, "--version"], capture_output=True, check=True)
            startups.append(ms(t))
        report = {"clips": opts.clips, "node_start_ms": statistics.median(startups), "node": subprocess.run(
            [node, "--version"], capture_output=True, text=True).stdout.strip()}
        server = engine_bridge.EngineBridgeClient(node, cli, "server", timeout=120)
        t = time.perf_counter()
        server._start()
        report["server_cold_start_ms"] = ms(t)
        oneshot = engine_bridge.EngineBridgeClient(node, cli, "oneshot", timeout=120)
        for tool, args in tools.items():
            report[tool] = {"server": measure(editor, server, tool, args, opts.runs),
                            "oneshot": measure(editor, oneshot, tool, args, max(3, opts.runs // 3))}
        server.close()
        print(json.dumps(report, indent=1))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
