"""Synthetic video with known cuts and known camera motion, for the media index tests.

No footage is checked in. Scenes are smooth coloured noise at several scales: textured enough
that phase correlation and frame differencing behave as they do on real footage, and fully
reproducible (seeded). Every clip is encoded by ffmpeg, so the tests exercise the real decode path.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable, List, Optional, Tuple

import numpy as np
import pytest
from PIL import Image

W, H, FPS = 640, 360, 30


def ffmpeg_path() -> Optional[str]:
    from classes.ffmpeg_cli import find_ffmpeg

    return find_ffmpeg("ffmpeg") or shutil.which("ffmpeg")


def need_ffmpeg() -> str:
    path = ffmpeg_path()
    if not path:
        pytest.skip("needs an ffmpeg binary")
    return path


def scene(seed: int, size: Tuple[int, int] = (1920, 1080)) -> Image.Image:
    """A large textured, coloured picture; different seeds look nothing alike."""
    w, h = size
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    for channel in range(3):
        acc = np.zeros((h, w), np.float32)
        for scale, weight in ((64, 1.0), (24, 0.6), (8, 0.35), (3, 0.15)):
            small = rng.normal(0, 1, (h // scale + 2, w // scale + 2)).astype(np.float32)
            up = np.asarray(Image.fromarray(small).resize((w, h), Image.BICUBIC), np.float32)
            acc += weight * up
        lo, hi = np.percentile(acc, (2, 98))     # stretch like a well-exposed picture
        acc = np.clip((acc - lo) / (hi - lo + 1e-6), 0.0, 1.0)
        img[..., channel] = acc * rng.uniform(0.6, 1.0) * 255
    # a different dominant colour per scene so histograms differ too
    tint = np.array([(seed * 53) % 100, (seed * 97) % 100, (seed * 29) % 100], np.float32) - 40
    return Image.fromarray(np.clip(img + tint, 0, 255).astype(np.uint8))


Pose = Callable[[float], Tuple[float, float, float]]  # t -> (centre x, centre y, scale) in base pixels


def hold(base: Image.Image) -> Pose:
    return lambda t: (base.width / 2.0, base.height / 2.0, 1.0)


def render(path: str, base: Image.Image, pose: Pose, seconds: float, *, noise: float = 1.5,
           size: Tuple[int, int] = (W, H), fps: int = FPS, extra: Optional[List[str]] = None) -> str:
    """Encode a clip of *base* viewed through a moving window (a 960x540 window = 1x)."""
    w, h = size
    cmd = [need_ffmpeg(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p"]
    cmd += (extra or []) + [path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    rng = np.random.default_rng(5)
    win_w0, win_h0 = base.width / 2.0, base.height / 2.0
    for i in range(int(round(seconds * fps))):
        cx, cy, sc = pose(i / fps)
        win_w, win_h = win_w0 / sc, win_h0 / sc
        x0, y0 = cx - win_w / 2.0, cy - win_h / 2.0
        im = base.transform((w, h), Image.AFFINE, (win_w / w, 0, x0, 0, win_h / h, y0), resample=Image.BICUBIC)
        frame = np.asarray(im, np.float32) + rng.normal(0, noise, (h, w, 3))
        proc.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
    proc.stdin.close()
    assert proc.wait() == 0, "ffmpeg failed to encode the test clip"
    return path


def join_hard(path: str, parts: List[str]) -> str:
    """Concatenate clips (same codec and size) with hard cuts."""
    listing = path + ".txt"
    with open(listing, "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run([need_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing,
                    "-c", "copy", path], check=True)
    return path


def join_dissolve(path: str, parts: List[str], seconds_each: float, fade: float = 1.0) -> str:
    """Cross-fade the clips; the n-th dissolve is centred at n*(seconds_each - fade) + fade/2... see offsets."""
    cmd = [need_ffmpeg(), "-y", "-v", "error"]
    for p in parts:
        cmd += ["-i", p]
    graph, last = [], "[0]"
    for n in range(1, len(parts)):
        offset = n * (seconds_each - fade)
        out = f"[x{n}]"
        graph.append(f"{last}[{n}]xfade=transition=fade:duration={fade}:offset={offset}{out}")
        last = out
    cmd += ["-filter_complex", ";".join(graph), "-map", last, "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", path]
    subprocess.run(cmd, check=True)
    return path


def dissolve_centres(count: int, seconds_each: float, fade: float = 1.0) -> List[float]:
    return [n * (seconds_each - fade) + fade / 2.0 for n in range(1, count + 1)]
