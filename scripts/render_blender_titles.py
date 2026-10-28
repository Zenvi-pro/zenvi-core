#!/usr/bin/env python3
"""Render one frame of every animated title template headless, the way the app does.

    .venv/bin/python scripts/render_blender_titles.py [--blender blender] [--out DIR]
        [--gpu] [--frame 30] [--only glass_slider,snow]

Uses classes.blender_titles (default_params + project_params + build_script +
the exact ``blender --factory-startup -b <blend> -y -P <script> -f N`` command),
at 320x180 by default so a pass takes minutes, not an hour. Prints one line per template and
exits 1 if any template fails: Blender exits non-zero, writes no frame, or writes
outside DIR (a path baked into the .blend instead of the injected output_path).
"""

import argparse
import glob
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from classes import blender_titles as bt  # noqa: E402


class _Project:
    def __init__(self, width, height):
        self.data = {"fps": {"num": 25, "den": 1}, "width": width, "height": height}

    def get(self, key):
        return self.data[key]


def _inside(path, folder):
    """Whether *path* is *folder* or under it (by directory, not by string prefix)."""
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return os.path.commonpath([path, folder]) == folder


def log_problems(returncode, log, frames, folder):
    """What went wrong in one Blender run, from its exit code, its output and the frames it left."""
    problems = []
    if returncode != 0:
        problems.append("exit %s" % returncode)
    lines = log.splitlines()
    at = next((i for i, ln in enumerate(lines) if ln.startswith("Traceback (most recent call last)")), None)
    if at is not None:
        # the exception is the first unindented line after the indented frames
        problems.append(next((ln for ln in lines[at + 1:] if ln and not ln.startswith(" ")), "Traceback")[:160])
    if not frames:
        problems.append("no frame written")
    stray = [ln.strip() for ln in lines
             if "Saved: '" in ln and not _inside(ln.split("Saved: '")[1].rsplit("'", 1)[0], folder)]
    if stray:
        problems.append("wrote outside the output folder: " + stray[0][:120])
    return problems


def render_one(xml_path, blender, out_root, frame, gpu, timeout, size):
    details = bt.animation_details(xml_path)
    name = os.path.splitext(os.path.basename(xml_path))[0]
    # Absolute: Blender runs in the folder, so a relative -P script path would point inside it again.
    folder = os.path.abspath(os.path.join(out_root, name))
    os.makedirs(folder, exist_ok=True)
    # A frame left by an earlier run must not pass for this run's.
    for stale in glob.glob(os.path.join(folder, name + "*.png")):
        os.remove(stale)
    blend, source, target = bt.script_paths(details["service"], folder)
    params = bt.default_params(details)
    params.update(bt.project_params(_Project(*size), os.path.join(folder, name + "####"), is_preview=False))
    params["start_frame"] = params["end_frame"] = frame
    with open(target, "w") as f:
        f.write(bt.build_script(source, params, gpu_enabled=gpu))
    cmd = [blender, "--factory-startup", "-b", blend, "-y", "-P", target, "-f", str(frame)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, cwd=folder)
    log = proc.stdout.decode("utf-8", errors="ignore")
    with open(os.path.join(folder, "blender.log"), "w") as f:
        f.write(log)
    frames = glob.glob(os.path.join(folder, name + "*.png"))
    problems = log_problems(proc.returncode, log, frames, folder)
    return name, frames, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blender", default="blender")
    ap.add_argument("--out", default=None)
    ap.add_argument("--gpu", action="store_true", help="inject gpu_enable.py.in too")
    ap.add_argument("--frame", type=int, default=30)
    ap.add_argument("--only", default="")
    ap.add_argument("--size", default="320x180", help="WIDTHxHEIGHT (default 320x180)")
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()
    out = args.out or tempfile.mkdtemp(prefix="zenvi-blender-titles-")
    only = {n for n in args.only.split(",") if n}
    unknown = only - {os.path.splitext(os.path.basename(p))[0] for p in bt.template_paths()}
    if unknown:
        ap.error("no template named " + ", ".join(sorted(unknown)))
    failed = 0
    for xml_path in bt.template_paths():
        if only and os.path.splitext(os.path.basename(xml_path))[0] not in only:
            continue
        try:
            name, frames, problems = render_one(xml_path, args.blender, out, args.frame, args.gpu, args.timeout,
                                              tuple(int(v) for v in args.size.split("x")))
        except subprocess.TimeoutExpired:
            name, frames, problems = os.path.basename(xml_path), [], ["timeout"]
        print("%-24s %s" % (name, "ok  " + os.path.basename(frames[0]) if not problems else "FAIL " + "; ".join(problems)), flush=True)
        failed += bool(problems)
    print("frames and logs in", out)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
