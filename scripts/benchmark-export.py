#!/usr/bin/env python3
"""Measure export throughput and optional quality for acceleration baselines.

Records wall time, frames/sec, output size, and (when available) SSIM via ffmpeg.
Pins libopenshot version so 0.5.0 numbers are never compared to 0.7.0.

Usage:
  PYTHONPATH=$HOME/zenvi-deps/python:.venv/lib/... python3 scripts/benchmark-export.py
  python3 scripts/benchmark-export.py --write-baseline tests/benchmarks/baseline.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_ROOT = os.path.join(REPO_ROOT, "src")
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

from classes.ffmpeg_cli import find_ffmpeg, run_ffmpeg  # noqa: E402

FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "media")


def _openshot_version() -> str:
    try:
        import openshot

        return str(getattr(openshot, "OPENSHOT_VERSION_FULL", "unknown"))
    except Exception as exc:
        return f"unavailable:{exc}"


def _probe_frames(path: str) -> int | None:
    ffprobe = find_ffmpeg("ffprobe")
    if not ffprobe:
        return None
    result = run_ffmpeg(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=nb_read_packets",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip().splitlines()[0])
    except Exception:
        return None


def _ssim(reference: str, candidate: str) -> float | None:
    """Return average SSIM from ffmpeg lavfi, or None if unavailable."""
    if not find_ffmpeg("ffmpeg"):
        return None
    result = run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            candidate,
            "-i",
            reference,
            "-lavfi",
            "ssim",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    # ffmpeg writes ssim stats to stderr
    text = (result.stderr or "") + (result.stdout or "")
    for token in text.replace("\n", " ").split():
        if token.startswith("All:"):
            try:
                return float(token.split(":")[1])
            except Exception:
                return None
    return None


def _encode_with_ffmpeg(
    source: str,
    output: str,
    *,
    vcodec: str = "libx264",
    bitrate: str = "8M",
) -> dict:
    started = time.perf_counter()
    result = run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source,
            "-c:v",
            vcodec,
            "-b:v",
            bitrate,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            output,
        ],
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "ffmpeg encode failed")

    frames = _probe_frames(output) or 0
    size = os.path.getsize(output) if os.path.isfile(output) else 0
    fps = (frames / elapsed) if elapsed > 0 and frames else 0.0
    return {
        "mode": "ffmpeg-reencode",
        "vcodec": vcodec,
        "elapsed_sec": round(elapsed, 4),
        "frames": frames,
        "fps": round(fps, 2),
        "output_bytes": size,
        "ssim_vs_source": _ssim(source, output),
    }


def run_benchmarks(fixture_names: list[str] | None = None) -> dict:
    if fixture_names is None:
        fixture_names = [
            "h264_720p30_2s.mp4",
            "h264_1080p30_2s.mp4",
            "h264_4k30_1s.mp4",
        ]

    missing = [
        name
        for name in fixture_names
        if not os.path.isfile(os.path.join(FIXTURE_DIR, name))
    ]
    if missing:
        raise FileNotFoundError(
            "Missing fixtures %s — run scripts/generate_export_fixtures.py first"
            % missing
        )

    cases = []
    with tempfile.TemporaryDirectory(prefix="zenvi-export-bench-") as tmp:
        for name in fixture_names:
            source = os.path.join(FIXTURE_DIR, name)
            out = os.path.join(tmp, f"out_{name}")
            try:
                metrics = _encode_with_ffmpeg(source, out)
                metrics["fixture"] = name
                cases.append(metrics)
                print(
                    f"{name}: {metrics['fps']:.1f} fps, "
                    f"{metrics['elapsed_sec']:.2f}s, "
                    f"ssim={metrics['ssim_vs_source']}"
                )
            except Exception as exc:
                cases.append({"fixture": name, "error": str(exc)})
                print(f"{name}: ERROR {exc}", file=sys.stderr)

    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "libopenshot_version": _openshot_version(),
        "platform": sys.platform,
        "note": (
            "Phase 0 baseline uses ffmpeg re-encode of fixtures as a portable "
            "throughput floor. Full Timeline.GetFrame export benchmarks require "
            "an initialized OpenShot App and are layered on in later phases."
        ),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline",
        metavar="PATH",
        help="Write JSON baseline to this path",
    )
    parser.add_argument(
        "--fixtures",
        nargs="*",
        default=None,
        help="Fixture filenames under tests/fixtures/media/",
    )
    args = parser.parse_args()

    # Ensure fixtures exist
    gen = os.path.join(REPO_ROOT, "scripts", "generate_export_fixtures.py")
    if not os.path.isdir(FIXTURE_DIR) or not os.listdir(FIXTURE_DIR):
        print("Generating fixtures...")
        code = os.system(f'"{sys.executable}" "{gen}"')
        if code != 0:
            return 1

    report = run_benchmarks(args.fixtures)
    text = json.dumps(report, indent=2) + "\n"
    if args.write_baseline:
        os.makedirs(os.path.dirname(os.path.abspath(args.write_baseline)), exist_ok=True)
        with open(args.write_baseline, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"Wrote baseline: {args.write_baseline}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
