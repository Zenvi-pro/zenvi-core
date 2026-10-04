"""
 @file
 @brief Frames of a range as one readable picture, each tile stamped with its exact time and frame number.

 For zooming in: a wide range at about a frame a second to find a moment, then a second or two at every frame to choose the
 exact cut. Tiles are big enough to read small things (320 px wide by default), and every tile says the file time and frame
 it shows, so what is seen can be cut exactly. Pure numpy plus ffmpeg: no fonts, no Qt, so it is safe off the GUI thread.
"""

from __future__ import annotations

import re
import struct
import subprocess
import zlib
from typing import List, Optional, Sequence, Tuple

import numpy as np

from classes.ffmpeg_cli import run_ffmpeg

MAX_TILES = 48
DEFAULT_TILE_WIDTH = 320
MIN_TILE_WIDTH, MAX_TILE_WIDTH = 96, 640
MAX_SHEET_WIDTH = 1920
_PTS = re.compile(r"pts_time:\s*(-?[\d.]+)")

# A 5x7 bitmap for the characters a stamp needs ("t=12.345 f=301" and the like).
_GLYPHS = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"], "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"], "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"], "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"], "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"], "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"], ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "=": ["00000", "00000", "11111", "00000", "11111", "00000", "00000"], " ": ["00000"] * 7, "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "t": ["00100", "00100", "11111", "00100", "00100", "00100", "00011"], "f": ["00110", "01000", "01000", "11110", "01000", "01000", "01000"],
    "s": ["00000", "01111", "10000", "01110", "00001", "11110", "00000"],
}


def stamp(img: np.ndarray, text: str, x: int, y: int, scale: int = 2) -> None:
    """Draw *text* in white on a black box with its top-left at (x, y), onto an RGB array, in place. Clipped to the picture."""
    glyphs = [_GLYPHS.get(ch, _GLYPHS[" "]) for ch in text]
    width = (len(glyphs) * 6 + 1) * scale
    height = 9 * scale
    h, w = img.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w, x + width), min(h, y + height)
    if x1 <= x0 or y1 <= y0:
        return
    img[y0:y1, x0:x1] = 0
    for n, glyph in enumerate(glyphs):
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit == "1":
                    px, py = x + (n * 6 + col + 1) * scale, y + (row + 1) * scale
                    ex, ey = min(w, px + scale), min(h, py + scale)
                    if px < w and py < h and ex > px and ey > py and px >= 0 and py >= 0:
                        img[py:ey, px:ex] = 255


def write_png(path: str, rgb: np.ndarray) -> None:
    """Write an (h, w, 3) uint8 array as a PNG."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[row].tobytes() for row in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def decode_frames(path: str, start: float, end: float, size: Tuple[int, int], *, rate: Optional[float] = None, start_time: float = 0.0
                  ) -> Tuple[np.ndarray, List[float]]:
    """RGB frames of [start, end) at *size*, with their times (seconds from the file's start).

    ``rate`` samples that many a second; None gives every frame exactly as stored (passthrough). Raises RuntimeError on a bad read.
    """
    w, h = size
    length = max(0.05, float(end) - float(start))
    sample = f"fps={rate:.6f}," if rate else ""
    cmd = ["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{max(0.0, start + start_time):.3f}", "-copyts", "-t", f"{length:.3f}", "-i", path, "-an",
           "-vf", f"{sample}scale={w}:{h}:flags=area,format=rgb24,showinfo", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = run_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=180)
    raw = proc.stdout or b""
    times = [float(m.group(1)) - start_time for m in _PTS.finditer((proc.stderr or b"").decode("utf-8", "replace"))]
    if proc.returncode != 0 or not raw or not times:
        raise RuntimeError((proc.stderr or b"could not decode the picture").decode("utf-8", "replace")[-200:])
    count = min(len(times), len(raw) // (w * h * 3))
    frames = np.frombuffer(raw[: count * w * h * 3], dtype=np.uint8).reshape(count, h, w, 3)
    # ffmpeg may read past the end with timestamps preserved: keep only the frames whose own time is inside the range asked for
    keep = [i for i in range(count) if start - 1e-3 <= times[i] < end - 1e-3]
    if not keep:
        raise RuntimeError("no frames in that range")
    return frames[keep], [times[i] for i in keep]


def choose_times(start: float, end: float, count: Optional[int] = None, rate: Optional[float] = None, every_frame: bool = False, fps: float = 0.0
                 ) -> Tuple[Optional[float], int]:
    """(sampling rate or None for every frame, how many tiles that gives) for a range, held to ``MAX_TILES``.

    Raises ValueError saying what to change when the request would give too many tiles.
    """
    span = max(0.0, float(end) - float(start))
    if every_frame:
        frames = int(round(span * fps)) if fps > 0 else 0
        if frames > MAX_TILES:
            raise ValueError(f"{frames} frames in that range is more than {MAX_TILES} tiles: narrow it to {MAX_TILES / fps:.2f} s or less, or ask for a rate")
        return None, max(1, frames)
    if count:
        count = int(count)
        if not 1 <= count <= MAX_TILES:
            raise ValueError(f"count must be 1 to {MAX_TILES}")
        return (count / span if span > 0 else 1.0), count
    rate = float(rate or 1.0)
    n = max(1, int(round(span * rate)))
    if n > MAX_TILES:
        raise ValueError(f"{rate:g} a second over {span:.1f} s is {n} tiles (the limit is {MAX_TILES}): lower the rate or narrow the range")
    return rate, n


def sheet(frames: np.ndarray, times: Sequence[float], fps: float, columns: int) -> np.ndarray:
    """Tiles in a grid, each stamped ``t=<seconds> f=<frame>`` at its bottom-left."""
    n, h, w, _ = frames.shape
    columns = max(1, min(columns, n))
    rows = (n + columns - 1) // columns
    out = np.zeros((rows * h, columns * w, 3), dtype=np.uint8)
    scale = 2 if w >= 192 else 1
    for i in range(n):
        r, c = divmod(i, columns)
        tile = frames[i].copy()
        label = f"t={times[i]:.3f}" + (f" f={int(round(times[i] * fps))}" if fps > 0 else "")
        stamp(tile, label, 2, h - 9 * scale - 2, scale)
        out[r * h:(r + 1) * h, c * w:(c + 1) * w] = tile
    return out


def columns_for(tile_width: int, count: int) -> int:
    return max(1, min(count, MAX_SHEET_WIDTH // max(1, tile_width), 8))
