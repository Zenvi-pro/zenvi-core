"""A stand-in for the zenvi CLI's engine-bridge commands (tests only).

Run as ``python fake_zenvi.py bridge`` (JSON-RPC lines on stdio),
``python fake_zenvi.py bridge --tools`` or ``python fake_zenvi.py tool-run ...``.
``run`` answers like the real CLI: add_motion_template_tool inserts a track, a
drawing file and its clip (template ``seek``: only moves the saved playhead,
untracked; ``future``: an action kind no desktop knows); update_motion_block_tool
and update_fx_tool re-render a linked file (a new path on its record);
review_frames_tool answers a PNG frame; anything else is refused. Every request is appended
to ``$FAKE_ZENVI_LOG`` (one JSON object per line) for the test to inspect.
``$FAKE_ZENVI_PROTOCOL`` overrides the protocol hello reports.
"""

import json
import os
import sys

TOOLS = [
    {"name": "add_motion_template_tool", "title": "Add motion template", "description": "Place a motion template.",
     "inputSchema": {"type": "object", "properties": {"template": {"type": "string"}}}, "available": True},
    {"name": "list_project_files_tool", "title": "List files", "description": "Desktop has this one.",
     "inputSchema": {"type": "object"}, "available": True},
    {"name": "export_video_tool", "title": "Export", "description": "Needs an exporter.",
     "inputSchema": {"type": "object"}, "available": False, "missing": ["exporter"]},
    {"name": "move_clip_tool", "title": "Move clip", "description": "The engine's simple move.",
     "inputSchema": {"type": "object"}, "available": True},
    {"name": "update_motion_block_tool", "title": "Update a motion block", "description": "Re-render a block.",
     "inputSchema": {"type": "object", "properties": {"file_id": {"type": "string"}}}, "available": True},
    {"name": "update_fx_tool", "title": "Update FX", "description": "Re-render FX.",
     "inputSchema": {"type": "object", "properties": {"clip_id": {"type": "string"}}}, "available": True},
    {"name": "review_frames_tool", "title": "Review frames", "description": "Frames as images.",
     "inputSchema": {"type": "object"}, "available": True,
     "annotations": {"title": "Review frames", "readOnlyHint": True}},
]
# A 1x1 PNG.
PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


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
    base = {"protocol": 2, "engine_version": "fake", "files": [], "timings": {"parse_ms": 0, "run_ms": 0, "translate_ms": 0}}
    args = params.get("args") or {}
    if tool in ("update_motion_block_tool", "update_fx_tool"):
        project = params.get("project") or {}
        file_id = args.get("file_id")
        if tool == "update_fx_tool":
            clip = next((c for c in project.get("clips") or [] if c.get("id") == args.get("clip_id")), None)
            file_id = clip and clip.get("file_id")
        record = next((f for f in project.get("files") or [] if f.get("id") == file_id), None)
        if record is None:
            r = receipt("refused", tool, f"Error: unknown file {file_id}")
            return {**base, "ok": False, "text": json.dumps(r), "receipt": r, "actions": []}
        if (record.get("zenvi_link") or {}).get("render", {}).get("fingerprint") == "sha256:same":
            r = receipt("unchanged", tool, "Already shows these params; nothing changed.")
            return {**base, "ok": True, "text": json.dumps(r), "receipt": r, "actions": []}
        rendered = dict(record, path=record["path"] + ".v2.mov")
        r = receipt("applied", tool, "Re-rendered", file_id=file_id)
        return {**base, "ok": True, "text": json.dumps(r), "receipt": r, "label": "Re-render",
                "actions": [{"type": "update", "key": ["files", {"id": file_id}], "value": rendered}]}
    if tool == "review_frames_tool":
        r = receipt("applied", tool, "2 frames", frames=2)
        r["undoSteps"] = 0
        return {**base, "ok": True, "text": json.dumps(r), "receipt": r, "actions": [],
                "images": [{"mimeType": "image/png", "data": PNG}, {"mimeType": "image/png", "data": PNG}]}
    if tool != "add_motion_template_tool" or args.get("template") == "nope":
        r = receipt("refused", tool, "Error: the fake refuses this")
        return {**base, "ok": False, "text": json.dumps(r), "receipt": r, "actions": []}
    template = (params.get("args") or {}).get("template")
    if template in ("seek", "future"):
        r = receipt("applied", tool, "Moved the playhead")
        actions = [{"type": "update_untracked", "key": ["playhead_position"], "value": 2.5}]
        if template == "future":
            actions = [{"type": "insert", "key": ["layers"], "value": {"id": "FAKELAYR02", "number": 3000000}},
                       {"type": "update_sideways", "key": ["playhead_position"], "value": 2.5}]
        return {**base, "ok": True, "text": json.dumps(r), "receipt": r, "actions": actions}
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
            result = {"protocol": int(os.environ.get("FAKE_ZENVI_PROTOCOL", "2")), "engine_version": "fake"}
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
        print(json.dumps({"protocol": int(os.environ.get("FAKE_ZENVI_PROTOCOL", "2")), "engine_version": "fake", "tools": TOOLS}))
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
