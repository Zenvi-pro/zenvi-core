#!/usr/bin/env python3
"""Generate tiny H.264 fixtures for Phase 4 inspect tests.

Writes under tests/fixtures/media/. Safe to re-run (skips existing files).
Requires ffmpeg on PATH.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tests" / "fixtures" / "media"

FIXTURES = {
    "h264_720p30_2s.mp4": [
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
    ],
}


def ensure_fixture(name: str, *, force: bool = False) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    if path.exists() and path.stat().st_size > 1024 and not force:
        return path
    args = FIXTURES[name]
    cmd = ["ffmpeg", "-y", *args, str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    if path.stat().st_size < 1024:
        raise RuntimeError(f"fixture too small: {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    for name in FIXTURES:
        path = ensure_fixture(name, force=args.force)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
