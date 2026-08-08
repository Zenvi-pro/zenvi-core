"""Build tiny VP9 WebM samples for alpha probe / OpenShot import tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CONTRACT_PATH = _HERE / "alpha_probe_contract.json"

_FFMPEG_CANDIDATE_DIRS = [
    os.environ.get("FFMPEG_BIN_DIR"),
    r"C:\msys64\mingw64\bin",
    r"C:\msys64\usr\bin",
    "/usr/bin",
    "/usr/local/bin",
]


def load_alpha_probe_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _which_in_dirs(name: str):
    found = shutil.which(name)
    if found:
        return found
    for d in _FFMPEG_CANDIDATE_DIRS:
        if not d:
            continue
        for candidate in (Path(d) / name, Path(d) / f"{name}.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


def ffmpeg_bin():
    return _which_in_dirs("ffmpeg")


def ffprobe_bin():
    return _which_in_dirs("ffprobe")


def ensure_ffmpeg_on_path() -> None:
    """Prepend MSYS ffmpeg dir so tool_handlers subprocess calls resolve."""
    ff = ffmpeg_bin()
    if not ff:
        return
    bindir = str(Path(ff).parent)
    path = os.environ.get("PATH", "")
    if bindir.lower() not in path.lower():
        os.environ["PATH"] = bindir + os.pathsep + path


def require_ffmpeg_libvpx():
    """Return (ffmpeg, ffprobe) or raise RuntimeError if unavailable."""
    ensure_ffmpeg_on_path()
    ff = ffmpeg_bin()
    fp = ffprobe_bin()
    if not ff or not fp:
        raise RuntimeError("ffmpeg/ffprobe not on PATH")
    p = subprocess.run(
        [ff, "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    text = (p.stdout or "") + (p.stderr or "")
    if "libvpx-vp9" not in text:
        raise RuntimeError("ffmpeg missing libvpx-vp9 encoder")
    return ff, fp


def build_vp9_alpha_overlay(out_path: Path, *, duration: float = 0.2) -> Path:
    """Fully transparent VP9 WebM (probes yuv420p + ALPHA_MODE=1)."""
    ff, _ = require_ffmpeg_libvpx()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ff, "-y",
        "-f", "lavfi",
        "-i", f"color=c=black@0.0:s=320x180:d={duration},format=yuva420p",
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-metadata:s:v:0", "alpha_mode=1",
        "-auto-alt-ref", "0",
        "-an",
        str(out_path),
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if p.returncode != 0 or not out_path.is_file():
        raise RuntimeError(f"build_vp9_alpha_overlay failed: {p.stderr[-500:]}")
    return out_path


def build_vp9_opaque_plate(out_path: Path, *, duration: float = 0.2) -> Path:
    """Opaque VP9 WebM without ALPHA_MODE (solid plate)."""
    ff, _ = require_ffmpeg_libvpx()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ff, "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s=320x180:d={duration},format=yuv420p",
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuv420p",
        "-auto-alt-ref", "0",
        "-an",
        str(out_path),
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if p.returncode != 0 or not out_path.is_file():
        raise RuntimeError(f"build_vp9_opaque_plate failed: {p.stderr[-500:]}")
    return out_path
