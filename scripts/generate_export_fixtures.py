#!/usr/bin/env python3
"""Synthesize short media clips for export benchmarks and acceleration tests.

Outputs are written under tests/fixtures/media/ and are gitignored.
Uses the project's ffmpeg_cli helper so frozen and Homebrew paths resolve.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_ROOT = os.path.join(REPO_ROOT, "src")
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

from classes.ffmpeg_cli import find_ffmpeg, run_ffmpeg  # noqa: E402

OUT_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "media")


def _run(args: list[str]) -> bool:
    result = run_ffmpeg(args, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout or "ffmpeg failed\n")
        return False
    return True


def generate_clip(
    path: str,
    *,
    width: int,
    height: int,
    fps: int,
    duration: float,
    vcodec: str,
    bitrate: str,
) -> bool:
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        print(f"  exists: {os.path.basename(path)}")
        return True

    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration}",
        "-c:v",
        vcodec,
        "-b:v",
        bitrate,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        path,
    ]
    ok = _run(args)
    if ok:
        print(f"  wrote:  {os.path.basename(path)}")
    else:
        print(f"  FAILED: {os.path.basename(path)} ({vcodec})")
    return ok


def main() -> int:
    if not find_ffmpeg("ffmpeg"):
        print("ffmpeg not found; install ffmpeg or set FFMPEG_BIN_DIR", file=sys.stderr)
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Generating fixtures in {OUT_DIR}")

    ok = True
    ok &= generate_clip(
        os.path.join(OUT_DIR, "h264_720p30_2s.mp4"),
        width=1280,
        height=720,
        fps=30,
        duration=2.0,
        vcodec="libx264",
        bitrate="4M",
    )
    ok &= generate_clip(
        os.path.join(OUT_DIR, "h264_1080p30_2s.mp4"),
        width=1920,
        height=1080,
        fps=30,
        duration=2.0,
        vcodec="libx264",
        bitrate="8M",
    )
    ok &= generate_clip(
        os.path.join(OUT_DIR, "h264_4k30_1s.mp4"),
        width=3840,
        height=2160,
        fps=30,
        duration=1.0,
        vcodec="libx264",
        bitrate="20M",
    )
    # HEVC is optional — skip cleanly if the encoder is unavailable.
    hevc_path = os.path.join(OUT_DIR, "hevc_1080p30_2s.mp4")
    if not generate_clip(
        hevc_path,
        width=1920,
        height=1080,
        fps=30,
        duration=2.0,
        vcodec="libx265",
        bitrate="6M",
    ):
        print("  note: HEVC fixture skipped (libx265 unavailable)")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
