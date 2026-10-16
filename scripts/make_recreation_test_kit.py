#!/usr/bin/env python3
"""Make a small, repeatable test kit for "recreate this video from my clips": a reference video and footage clips whose right answer is known.

Needs only ffmpeg. Nothing is downloaded and no network is used.

    python scripts/make_recreation_test_kit.py OUT_DIR

Writes, into OUT_DIR:
  reference.mp4       16.5 s, 14 shots cut on a 120 bpm click track. Each shot shows one of 8 distinct patterns ("scenes"), and some scenes
                      come back later (that is the only place footage may be shown again).
  footage_1..6.mp4    6-9 s clips. Each holds 3 of the scenes (each tinted a little differently, so none is a pixel copy of the reference).
  README.txt          the exact answer key: which scene every reference shot shows, which footage holds it, and what a good recreation does.

One scene (scene 5) is left out of the footage on purpose, so a correct recreation has to say it could not fill those shots (or use stock),
not repeat something else to cover the gap.
"""

from __future__ import annotations

import os
import subprocess
import sys

W, H, FPS = 1280, 720, 30
SCENES = {
    0: "testsrc2",
    1: "mandelbrot",
    2: "pal75bars",
    3: "rgbtestsrc",
    4: "smptehdbars",
    5: "cellauto",
    6: "sierpinski",
    7: "yuvtestsrc",
}
# (scene, seconds): a click every half second, so cuts that land on the beat are checkable. Scenes 0 and 3 come back.
REFERENCE_SHOTS = [(0, 1.5), (1, 1.0), (2, 1.5), (3, 1.0), (4, 0.5), (5, 1.5), (6, 1.0), (7, 1.0),
                   (0, 1.5), (2, 1.0), (3, 1.5), (4, 1.0), (6, 1.0), (1, 1.5)]
# footage clip -> [(scene, seconds)], scene 5 is absent everywhere
FOOTAGE = {
    1: [(0, 3.0), (1, 2.5), (2, 2.5)],
    2: [(3, 3.0), (4, 2.0), (6, 2.5)],
    3: [(7, 3.0), (0, 2.0), (2, 2.5)],
    4: [(1, 2.5), (3, 2.5), (6, 2.0)],
    5: [(4, 2.5), (7, 2.5), (0, 2.0)],
    6: [(2, 2.5), (6, 2.5), (3, 2.0)],
}
TINTS = ["", "eq=brightness=0.04:saturation=1.1", "eq=brightness=-0.05:saturation=0.9", "hue=h=8"]


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def segment(path: str, scene: int, seconds: float, tint: str) -> None:
    vf = "format=yuv420p" + (f",{tint}" if tint else "")
    source = f"{SCENES[scene]}=size={W}x{H}:rate={FPS}"
    ffmpeg("-f", "lavfi", "-i", source, "-t", f"{seconds:.3f}", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-an", path)


def concat(parts: list, out: str, audio: str = "") -> None:
    listing = out + ".txt"
    with open(listing, "w") as fh:
        fh.write("".join(f"file '{p}'\n" for p in parts))
    cmd = ["-f", "concat", "-safe", "0", "-i", listing]
    if audio:
        cmd += ["-i", audio, "-c:v", "copy", "-c:a", "aac", "-shortest"]
    else:
        cmd += ["-c", "copy"]
    ffmpeg(*cmd, out)
    os.remove(listing)


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, "_parts")
    os.makedirs(tmp, exist_ok=True)

    # the reference: no tint (the reference's own look), and a 120 bpm click under it
    total = sum(d for _s, d in REFERENCE_SHOTS)
    click = os.path.join(tmp, "click.wav")
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc=0.6*sin(2*PI*880*t)*lt(mod(t\\,0.5)\\,0.04)+0.15*sin(2*PI*110*t):s=48000:d={total:.2f}", click)
    parts = []
    for i, (scene, seconds) in enumerate(REFERENCE_SHOTS):
        p = os.path.join(tmp, f"ref_{i:02d}.mp4")
        segment(p, scene, seconds, "")
        parts.append(p)
    concat(parts, os.path.join(out_dir, "reference.mp4"), click)

    # the footage: the same scenes, each tinted differently
    key = []
    for clip, scenes in FOOTAGE.items():
        parts = []
        for j, (scene, seconds) in enumerate(scenes):
            p = os.path.join(tmp, f"foot_{clip}_{j}.mp4")
            segment(p, scene, seconds, TINTS[(clip + j) % len(TINTS)])
            parts.append(p)
        concat(parts, os.path.join(out_dir, f"footage_{clip}.mp4"))
        key.append(f"footage_{clip}.mp4: " + ", ".join(f"scene {s} for {d:g}s" for s, d in scenes))

    start = 0.0
    shots = []
    for i, (scene, seconds) in enumerate(REFERENCE_SHOTS):
        holders = [f"footage_{c}" for c, sc in FOOTAGE.items() if any(s == scene for s, _d in sc)]
        shots.append(f"  shot {i:2d}  {start:5.2f}-{start + seconds:5.2f} s  scene {scene} ({SCENES[scene]})  footage: {', '.join(holders) or 'NONE (stock or skip)'}")
        start += seconds
    readme = f"""Recreation test kit ({total:.1f} s reference, {len(REFERENCE_SHOTS)} shots, 120 bpm click)

Reference shots and where the right footage is:
{os.linesep.join(shots)}

Footage contents:
  {(os.linesep + '  ').join(key)}

Repeats in the reference (the only places footage may be shown again): scene 0 at shots 0 and 8; scene 3 at shots 3 and 10;
scene 2 at shots 2 and 9; scene 4 at shots 4 and 11; scene 6 at shots 6 and 12; scene 1 at shots 1 and 13.
Scene 5 (shot 5, 1.5 s) is in no footage clip.

A good recreation:
  - every cut within one frame of the reference's cut times, and cut on the click (every 0.5 s)
  - each shot uses footage that holds the same scene (open the comparison picture: same pattern on both sides)
  - shot 5 is NOT filled with another scene: it is skipped (reported) or covered by stock you chose
  - a scene is shown again only at the repeat shots listed above; every other shot uses a fresh moment of footage
  - the reference's own sound (the click) on its own track, the footage's sound off
"""
    with open(os.path.join(out_dir, "README.txt"), "w") as fh:
        fh.write(readme)
    for name in os.listdir(tmp):
        os.remove(os.path.join(tmp, name))
    os.rmdir(tmp)
    print(readme)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
