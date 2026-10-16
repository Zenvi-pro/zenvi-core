#!/usr/bin/env python3
"""Measure how smoothly a saved project would play: render its frames one after another the way the player asks for them and time each one.

    save the project (File > Save, or save_project_tool), then:
    python scripts/bench_playback.py PROJECT.zvn [--seconds 30] [--layers 6000000,7000000] [--proxies]

Needs libopenshot's python module (the same one the app runs on: set ZENVI_DEPS to its folder, e.g. ~/zenvi-deps-1.0).
At 30 fps a frame has 33 ms; a frame that takes longer is one the player has to drop or stall on.

--proxies  play the optimized preview copies (what the preview uses once "optimize for smooth playback" has run) instead of the originals
--layers   only these layers (the numbers are in the project file); use it to leave out old attempts stacked under a new one

Prints mean / median / 95th / 99th percentile / worst frame time, the share of frames over 33 ms, and the source files behind the slowest frames.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--layers", default="")
    ap.add_argument("--proxies", action="store_true")
    ap.add_argument("--start", type=float, default=0.0, help="start this many seconds into the timeline")
    args = ap.parse_args()

    deps = os.environ.get("ZENVI_DEPS")
    if deps:
        sys.path.insert(0, os.path.join(os.path.expanduser(deps), "python"))
    try:
        import openshot
    except ImportError:
        sys.exit("libopenshot's python module was not found: set ZENVI_DEPS to the folder that holds it (it has a python/ subfolder)")

    with open(args.project, encoding="utf-8") as fh:
        project = json.load(fh)
    clips = project["clips"]
    if args.layers:
        keep = {int(x) for x in args.layers.split(",")}
        clips = [c for c in clips if c["layer"] in keep]
    optimized = os.path.expanduser("~/.openshot_qt/optimized/")
    if args.proxies:
        by_id = {f["id"]: f for f in project.get("files", [])}
        swapped = 0
        for c in clips:
            proxy = (by_id.get(c.get("file_id")) or {}).get("proxy_reader")
            if proxy:
                proxy = dict(proxy)
                proxy["path"] = proxy["path"].replace("@assets/optimized/", optimized)
                c["reader"] = proxy
                swapped += 1
        pictures = [c for c in clips if (c.get("reader") or {}).get("has_video", True)]
        print(f"{swapped} of {len(clips)} clips use an optimized copy")
        if swapped < 0.9 * len(pictures):
            sys.exit("most clips have no optimized copy yet, so a comparison would mislead: run manage_optimized_previews_tool (action=optimize, "
                     "all_videos=true, wait_seconds=600) in the app, save the project, and try again")
    effects = project.get("effects", [])
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for e in effects:
        reader = e.get("reader") or {}
        if str(reader.get("path", "")).startswith("@transitions/"):
            reader["path"] = os.path.join(here, "src", "transitions", reader["path"][len("@transitions/"):])
    if args.layers:
        effects = [e for e in effects if e.get("layer") in keep]

    fps = project["fps"]
    timeline = openshot.Timeline(project["width"], project["height"], openshot.Fraction(fps["num"], fps["den"]), project["sample_rate"],
                                 project["channels"], project["channel_layout"])
    timeline.SetJson(json.dumps({"clips": clips, "effects": effects, "duration": project.get("duration", 0), "fps": fps, "width": project["width"],
                                 "height": project["height"], "sample_rate": project["sample_rate"], "channels": project["channels"],
                                 "channel_layout": project["channel_layout"], "layers": project.get("layers", [])}))
    timeline.Open()
    rate = fps["num"] / fps["den"]
    first, count = int(args.start * rate) + 1, int(args.seconds * rate)
    print(f"{len(clips)} clips, project {project['width']}x{project['height']} at {rate:g} fps, frames {first}..{first + count - 1}")

    def clip_at(frame: int):
        t = (frame - 1) / rate
        for c in clips:
            if c["position"] <= t < c["position"] + (c["end"] - c["start"]) and (c.get("reader") or {}).get("has_video", True):
                return c

    times, slow = [], collections.defaultdict(list)
    for frame in range(first, first + count):
        began = time.time()
        timeline.GetFrame(frame)                              # decoding, effects and compositing all happen in this call
        ms = (time.time() - began) * 1000.0
        times.append(ms)
        if ms > 100:
            c = clip_at(frame)
            if c:
                slow[os.path.basename((c.get("reader") or {}).get("path", "?"))].append(round(ms))
    times.sort()
    n = len(times)
    print("mean %.1f  median %.1f  p95 %.1f  p99 %.1f  worst %.1f ms;  over 33 ms: %.0f%% of frames" % (
        statistics.mean(times), times[n // 2], times[int(n * 0.95)], times[int(n * 0.99)], times[-1], 100.0 * sum(1 for t in times if t > 33.4) / n))
    for name, values in sorted(slow.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"  slow frames from {name}: {len(values)} (worst {max(values)} ms)")


if __name__ == "__main__":
    main()
