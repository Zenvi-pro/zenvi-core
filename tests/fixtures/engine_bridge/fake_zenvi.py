"""A stand-in for the zenvi CLI's engine-bridge commands (tests only).

Run as ``python fake_zenvi.py bridge`` (JSON-RPC lines on stdio),
``python fake_zenvi.py bridge --tools`` or ``python fake_zenvi.py tool-run ...``.
``run`` answers like the real CLI: add_motion_template_tool inserts a track, a
drawing file and its clip; anything else is refused. Every request is appended
to ``$FAKE_ZENVI_LOG`` (one JSON object per line) for the test to inspect.
``$FAKE_ZENVI_PROTOCOL`` overrides the protocol hello reports.
"""

import json
import os
import sys

TOOLS = [
    {"name": "add_motion_template_tool", "title": "Add motion template", "description": "Place a motion template.",
     "inputSchema": {"type": "object", "properties": {"template": {"type": "string"}}}, "available": True},
    {"name": "list_files_tool", "title": "List files", "description": "Desktop has this one.",
     "inputSchema": {"type": "object"}, "available": True},
    {"name": "export_video_tool", "title": "Export", "description": "Needs an exporter.",
     "inputSchema": {"type": "object"}, "available": False, "missing": ["exporter"]},
]


def log(entry):
    path = os.environ.get("FAKE_ZENVI_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def receipt(status, tool, summary, **data):
    return {"contract": 3, "status": status, "tool": tool, "summary": summary, "clips": [], "shifted": [],
            "removedClipIds": [], "createdTracks": [], "markers": [], "removedMarkerIds": [],
            "warnings": [], "notes": [], "undoSteps": 1 if status == "applied" else 0, **({"data": data} if data else {})}


def run(params):
    log({"tool": params.get("tool"), "args": params.get("args"), "project_file": params.get("project_file"),
         "backend_token": params.get("backend_token"), "project_keys": sorted((params.get("project") or {}).keys()),
         "clips": len((params.get("project") or {}).get("clips") or [])})
    tool = params.get("tool")
    base = {"protocol": 1, "engine_version": "fake", "files": [], "timings": {"parse_ms": 0, "run_ms": 0, "translate_ms": 0}}
    if tool != "add_motion_template_tool" or (params.get("args") or {}).get("template") == "nope":
        r = receipt("refused", tool, "Error: the fake refuses this")
        return {**base, "ok": False, "text": json.dumps(r), "receipt": r, "actions": []}
    assets = os.path.splitext(params["project_file"])[0] + "_assets"
    svg = os.path.join(assets, "title", "end_card-title.svg")
    file_record = {"id": "FAKEFILE01", "path": svg, "media_type": "image", "duration": 3600, "has_video": True,
                   "has_audio": False, "width": 1920, "height": 1080}
    clip = {"id": "FAKECLIP01", "file_id": "FAKEFILE01", "layer": 2000000, "position": 1.0, "start": 0.0, "end": 2.0,
            "duration": 3600, "title": "end_card title", "reader": dict(file_record), "effects": [],
            "location_x": {"Points": [{"co": {"X": 1, "Y": 0}, "interpolation": 0}]}}
    wipe = {"id": "FAKETRAN01", "layer": 2000000, "position": 1.0, "start": 0.0, "end": 0.5,
            "reader": {"path": "@transitions/common/fade.svg"}}
    actions = [
        {"type": "insert", "key": ["layers"], "value": {"id": "FAKELAYR01", "number": 2000000, "label": "Motion", "y": 0, "lock": False}},
        {"type": "insert", "key": ["files"], "value": file_record},
        {"type": "insert", "key": ["clips"], "value": clip},
        {"type": "insert", "key": ["effects"], "value": wipe},
    ]
    r = receipt("applied", tool, "Added motion template end_card", layers=[{"timeline_clip_id": "FAKECLIP01"}])
    return {**base, "ok": True, "text": json.dumps(r), "receipt": r, "label": "Add motion template end_card",
            "actions": actions, "files": [svg]}


def serve():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        rid, method = msg.get("id"), msg.get("method")
        if method == "hello":
            result = {"protocol": int(os.environ.get("FAKE_ZENVI_PROTOCOL", "1")), "engine_version": "fake"}
        elif method == "run":
            if (msg.get("params") or {}).get("args", {}).get("template") == "crash":
                sys.exit(3)
            result = run(msg.get("params") or {})
        elif method == "shutdown":
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": None}), flush=True)
            return
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)


def main(argv):
    if argv[:1] == ["bridge"] and "--tools" in argv:
        log({"catalog": True})
        print(json.dumps({"engine_version": "fake", "tools": TOOLS}))
    elif argv[:1] == ["bridge"]:
        serve()
    elif argv[:1] == ["tool-run"]:
        tool = argv[1]
        opts = dict(zip(argv[2::2], argv[3::2]))
        with open(opts["--snapshot"], encoding="utf-8") as fh:
            project = json.load(fh)
        answer = run({"tool": tool, "args": json.loads(opts.get("--args", "{}")), "project": project,
                      "project_file": opts["--project-file"], "backend_token": os.environ.get("ZENVI_BACKEND_TOKEN")})
        print(json.dumps(answer))
        return 0 if answer["ok"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
