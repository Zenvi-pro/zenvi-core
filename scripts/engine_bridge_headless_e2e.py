#!/usr/bin/env python3
"""Drive a headless Zenvi through engine-bridge tools over MCP and record what happened.

Starts ``launch.py --headless`` with the engine bridge on, builds a scratch
project from generated media (ffmpeg ``testsrc2`` / ``smptebars`` + ``sine``),
then, as an MCP client would: applies a motion transition, adds a sound effect
and a Zenvi Motion block (all three run in the zenvi CLI), inspects frames,
undoes each edit one step at a time, redoes them, saves, quits, reopens the
project in a second headless session and checks the edits and their undo
history came back. Writes ``transcript.md`` / ``transcript.jsonl`` and the
inspected frames (JPEG, as inspect_timeline_tool renders them) to ``--out``.

Usage (macOS, from the repo root, with run.sh's environment)::

    ZENVI_DEPS=~/zenvi-deps-1.0 PYTHONPATH=~/zenvi-deps-1.0/python \\
    ZENVI_ENGINE_CLI=/path/to/zenvi-web/packages/cli/dist/zenvi.mjs \\
    .venv/bin/python scripts/engine_bridge_headless_e2e.py --out /tmp/bridge-e2e

Needs a signed-in Zenvi (headless refuses otherwise), Node 22+, ffmpeg, and
headless Chrome for the motion block (``node zenvi.mjs motion setup``). A
desktop window may be running: headless serves MCP on a free port when 7434 is
taken and advertises it in ``~/.openshot_qt/headless_mcp.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DISCOVERY = os.path.join(os.path.expanduser("~"), ".openshot_qt", "headless_mcp.json")


class Transcript:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        self.lines: list = []
        self.records: list = []
        self.frames = 0
        self.failures: list = []

    def note(self, text: str) -> None:
        print(text, flush=True)
        self.lines.append(text)

    def check(self, ok: bool, what: str) -> None:
        self.note(f"- {'PASS' if ok else 'FAIL'}: {what}")
        if not ok:
            self.failures.append(what)

    def call(self, tool: str, args: dict, receipt: dict, images: list, seconds: float) -> None:
        names = []
        for data, mime in images:
            self.frames += 1
            ext = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
            name = f"frame_{self.frames:02d}_{tool.replace('_tool', '')}.{ext}"
            with open(os.path.join(self.out_dir, name), "wb") as fh:
                fh.write(data)
            names.append(name)
        self.records.append({"tool": tool, "args": args, "receipt": receipt, "images": names,
                             "seconds": round(seconds, 2)})
        summary = str(receipt.get("summary", ""))[:300]
        self.note(f"\n**{tool}** `{json.dumps(args)}` -> {receipt.get('status')} "
                  f"(undoSteps {receipt.get('undoSteps')}, {seconds:.1f}s): {summary}")
        for name in names:
            self.note(f"  ![{name}]({name})")

    def write(self) -> None:
        with open(os.path.join(self.out_dir, "transcript.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(self.lines) + "\n")
        with open(os.path.join(self.out_dir, "transcript.jsonl"), "w", encoding="utf-8") as fh:
            for record in self.records:
                fh.write(json.dumps(record) + "\n")


def make_media(work: str) -> list:
    """Two 3 s shots with a tone each, made here (no media from anywhere else)."""
    out = []
    for name, source, tone in (("shot_a", "testsrc2", 440), ("shot_b", "smptebars", 660)):
        path = os.path.join(work, f"{name}.mp4")
        if not os.path.isfile(path):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{source}=size=1280x720:rate=30",
                            "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000", "-t", "3",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", path], check=True)
        out.append(path)
    return out


def start_headless(python: str, project: str, log_path: str) -> subprocess.Popen:
    env = dict(os.environ, ZENVI_ENGINE_BRIDGE=os.environ.get("ZENVI_ENGINE_BRIDGE", "1"),
               QT_QPA_PLATFORM="offscreen", QTWEBENGINE_DISABLE_SANDBOX="1")
    argv = [python, os.path.join(ROOT, "src", "launch.py"), "--headless"]
    if project:
        argv += ["--project", project]
    log = open(log_path, "ab")
    return subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def wait_ready(proc: subprocess.Popen, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"headless Zenvi exited with {proc.returncode}")
        try:
            with open(DISCOVERY, encoding="utf-8") as fh:
                info = json.load(fh)
            if int(info.get("pid") or 0) == proc.pid:
                return info
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("headless Zenvi did not advertise its MCP server in time")


class Session:
    def __init__(self, info: dict, transcript: Transcript):
        self.info = info
        self.t = transcript
        self.client = None

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        with open(self.info["token_file"], encoding="utf-8") as fh:
            token = fh.read().strip()
        self._stack = AsyncExitStack()
        read, write, _ = await self._stack.enter_async_context(streamablehttp_client(
            self.info["url"], headers={"Authorization": f"Bearer {token}"}, timeout=600, sse_read_timeout=900))
        self.client = await self._stack.enter_async_context(ClientSession(read, write))
        await self.client.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def tools(self) -> list:
        return [t.name for t in (await self.client.list_tools()).tools]

    async def call(self, tool: str, record: bool = True, **args) -> dict:
        start = time.time()
        result = await self.client.call_tool(tool, args)
        receipt, images = {}, []
        for block in result.content:
            if block.type == "text" and not receipt:
                try:
                    receipt = json.loads(block.text)
                except ValueError:
                    receipt = {"status": "text", "summary": block.text}
            elif block.type == "image":
                images.append((base64.b64decode(block.data), block.mimeType))
        if record:
            self.t.call(tool, args, receipt, images, time.time() - start)
        return receipt

    async def state(self) -> dict:
        receipt = await self.call("get_timeline_state_tool", record=False)
        data = receipt.get("data")
        if isinstance(data, dict):
            return data
        try:
            return json.loads(receipt.get("summary") or "{}")
        except ValueError:
            return {}


def clips_of(state: dict) -> list:
    clips = state.get("clips")
    if clips is None:
        clips = [c for track in state.get("tracks") or [] for c in track.get("clips") or []]
    return clips or []


async def first_session(info: dict, t: Transcript, media: list, project: str, with_3d: bool) -> None:
    async with Session(info, t) as s:
        names = await s.tools()
        bridged = [n for n in ("apply_motion_transition_tool", "add_sfx_tool", "add_motion_block_tool",
                               "speed_ramp_tool", "add_3d_scene_tool", "get_linked_clip_tool",
                               "rerender_linked_clip_tool") if n in names]
        t.note(f"\nMCP lists {len(names)} tools; engine-bridge tools among them: {', '.join(bridged)}")
        t.check("move_clip_tool" not in names, "the engine's move_clip_tool twin is not listed (move_clips_tool is)")
        imported = await s.call("import_files_tool", paths=media)
        ids = re.findall(r"file_id=(\S+)", str(imported.get("summary") or ""))
        t.check(len(ids) == 2, f"two generated shots imported ({ids})")
        a = await s.call("add_clip_to_timeline_tool", file_id=ids[0], position_seconds=0, track="1")
        b = await s.call("add_clip_to_timeline_tool", file_id=ids[1], position_seconds=3, track="1")
        await s.call("save_project_tool", file_path=project)
        state = await s.state()
        t.note(f"  timeline state: {json.dumps(state)[:600]}")
        clips = clips_of(state)
        t.check(len(clips) == 2, f"two clips back to back ({[c.get('id') for c in clips]})")
        first = min(clips, key=lambda c: float(c.get("position") or 0))
        second = max(clips, key=lambda c: float(c.get("position") or 0))
        del a, b

        steps = []
        r = await s.call("apply_motion_transition_tool", preset="whip", clip1_id=first["id"])
        t.check(r.get("status") == "applied" and r.get("undoSteps") == 1, "motion transition: one undo step")
        steps.append(r)
        # The default project is 30 fps: the whip spans the cut at frame 90.
        await s.call("inspect_timeline_tool", startFrame=82, endFrame=98, maxFrames=6)
        r = await s.call("add_sfx_tool", recipe="whoosh_short", clip_id=second["id"], edge="start")
        t.check(r.get("status") == "applied" and r.get("undoSteps") == 1, "sound effect: one undo step")
        steps.append(r)
        r = await s.call("add_motion_block_tool", block="counter", position_seconds=0.5, duration_seconds=2,
                         params={"values": ["0", "100"], "suffix": "%", "label": "Engine bridge"})
        t.check(r.get("status") == "applied" and r.get("undoSteps") == 1, "motion block: one undo step")
        steps.append(r)
        await s.call("inspect_timeline_tool", startFrame=15, endFrame=75, maxFrames=6)
        block_file = str((r.get("data") or {}).get("file_id") or "")
        if block_file:
            info_r = await s.call("get_linked_clip_tool", file_id=block_file)
            t.check((info_r.get("data") or {}).get("kind") == "zenvi-motion", "get_linked_clip_tool reads the block's link")
            again = await s.call("rerender_linked_clip_tool", file_id=block_file)
            t.check(again.get("status") == "unchanged" and again.get("undoSteps") == 0,
                    "re-rendering an up-to-date block is unchanged, no undo step")
        edits = ["motion transition", "sound effect", "motion block"]
        if with_3d:
            r = await s.call("add_3d_scene_tool", scene="glass_hero", quality="draft", position_seconds=3.5,
                             duration_seconds=float(os.environ.get("E2E_SCENE_SECONDS", "2")))
            t.check(r.get("status") == "applied" and r.get("undoSteps") == 1, "3D scene (Blender, draft): one undo step")
            if r.get("undoSteps") == 1:
                edits.append("3D scene")
            scene_file = str((r.get("data") or {}).get("file_id") or "")
            await s.call("inspect_timeline_tool", startFrame=110, endFrame=160, maxFrames=4)
            if scene_file:
                info_r = await s.call("get_linked_clip_tool", file_id=scene_file)
                t.check((info_r.get("data") or {}).get("kind") == "blender", "get_linked_clip_tool reads the scene's link")
                again = await s.call("rerender_linked_clip_tool", file_id=scene_file)
                t.check(not str(again.get("summary", "")).startswith("Error"),
                        f"rerender_linked_clip_tool re-renders the scene through rerender_3d_scene_tool ({again.get('status')})")
                if again.get("undoSteps") == 1:
                    edits.append("scene re-render")
        undone = edits[::-1]
        before = len(clips_of(await s.state()))
        pending, steps = list(undone), 0
        while pending and steps < len(undone) + 6:
            r = await s.call("undo_tool", steps=1)
            steps += 1
            now = len(clips_of(await s.state()))
            if "(update on files)" in str(r.get("summary", "")):
                # The desktop's own bin-file update (indexing) landed in the history meanwhile.
                t.note(f"  that undo step was the desktop's own bin-file update, not an engine edit: {now} clips")
                continue
            t.note(f"  after undoing the {pending.pop(0)}: {now} clips")
        t.check(len(clips_of(await s.state())) == 2,
                f"one undo per engine edit ({len(undone)}) brings back the two-clip timeline (was {before})")
        await s.call("redo_tool", steps=steps)
        t.check(len(clips_of(await s.state())) == before, f"{steps} redos bring the edits back")
        await s.call("save_project_tool")
        await s.call("shutdown_headless_tool", save=False)


async def second_session(info: dict, t: Transcript, expected: int) -> None:
    async with Session(info, t) as s:
        state = await s.state()
        t.check(len(clips_of(state)) == expected, f"reopened project has the {expected} clips")
        await s.call("inspect_timeline_tool", overview=True)
        await s.call("inspect_timeline_tool", startFrame=82, endFrame=98, maxFrames=4)
        movie = os.path.join(t.out_dir, "bridge-e2e.mp4")
        r = await s.call("export_video_tool", preset="MP4 (h.264)", quality="med", output_path=movie, overwrite=True)
        t.check(os.path.isfile(movie), f"exported the reopened timeline to {os.path.basename(movie)}")
        r = await s.call("undo_tool", steps=1)
        t.check(not str(r.get("summary", "")).startswith("Error"), "the saved undo history undoes the last edit")
        await s.call("shutdown_headless_tool", save=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="folder for transcript.md, transcript.jsonl and frames")
    ap.add_argument("--python", default=os.path.join(ROOT, ".venv", "bin", "python"))
    ap.add_argument("--with-3d", action="store_true", help="also add a Zenvi 3D scene (needs Blender 5) and re-render it")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    work = os.path.join(out, "work")
    os.makedirs(work, exist_ok=True)
    t = Transcript(out)
    project = os.path.join(work, "bridge-e2e.zvn")
    for stale in (project,):
        if os.path.exists(stale):
            os.remove(stale)
    t.note("# Engine bridge, headless end to end\n")
    t.note(f"CLI: {os.environ.get('ZENVI_ENGINE_CLI') or '(discovered)'}; project: {project}")
    media = make_media(work)
    log_path = os.path.join(out, "headless.log")
    proc = start_headless(args.python, "", log_path)
    try:
        info = wait_ready(proc)
        t.note(f"headless session 1: pid {proc.pid}, MCP {info['url']}")
        asyncio.run(first_session(info, t, media, project, args.with_3d))
        proc.wait(60)
        # Two shots + the sound effect + the motion block (+ the 3D scene); the transition is effects on the shots.
        expected = 2 + 2 + (1 if args.with_3d else 0)
        proc = start_headless(args.python, project, log_path)
        info = wait_ready(proc)
        t.note(f"\nheadless session 2 (reopen): pid {proc.pid}, MCP {info['url']}")
        asyncio.run(second_session(info, t, expected))
        proc.wait(60)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(30)
        t.note(f"\n{len(t.failures)} check(s) failed" + (": " + "; ".join(t.failures) if t.failures else ""))
        t.write()
    return 1 if t.failures else 0


if __name__ == "__main__":
    sys.exit(main())
