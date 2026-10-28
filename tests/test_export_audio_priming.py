"""Desktop exports start their audio on time (E2E round 3, B2).

libopenshot's FFmpegWriter leaves an MP4 whose audio edit list is
[one-frame empty edit, media from 0]: the AAC priming plays and everything is
2048 samples (42.7 ms) late. trim_audio_priming rewrites that edit list to
start at the encoder delay without moving a byte of media.
"""

from __future__ import annotations

import array
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import export_audio_priming as priming  # noqa: E402


def box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(body), kind) + body


def full(kind: bytes, version: int, body: bytes) -> bytes:
    return box(kind, struct.pack(">B3x", version) + body)


def elst(entries, version=0):
    fmt = ">QqI" if version == 1 else ">IiI"
    return full(b"elst", version, struct.pack(">I", len(entries)) + b"".join(struct.pack(fmt, *e) for e in entries))


def trak(handler, track_id, duration, media_ts, media_dur, edits, entry=b"mp4a", rate=48000, elst_version=0):
    tkhd = full(b"tkhd", 0, struct.pack(">IIIII", 0, 0, track_id, 0, duration) + b"\0" * 60)
    mdhd = full(b"mdhd", 0, struct.pack(">IIII", 0, 0, media_ts, media_dur) + b"\0" * 4)
    hdlr = full(b"hdlr", 0, b"\0" * 4 + handler + b"\0" * 12 + b"Handler\0")
    sample_entry = box(entry, b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 8 + struct.pack(">HHHHI", 2, 16, 0, 0, rate << 16))
    stsd = full(b"stsd", 0, struct.pack(">I", 1) + sample_entry)
    stbl = box(b"stbl", stsd + full(b"stco", 0, struct.pack(">II", 1, 0)))
    mdia = box(b"mdia", mdhd + hdlr + box(b"minf", box(b"smhd", b"\0" * 8) + stbl))
    return box(b"trak", tkhd + box(b"edts", elst(edits, elst_version)) + mdia)


def movie(audio_edits, moov_first=True, audio_entry=b"mp4a", elst_version=0, extra=b""):
    """Shaped like a libopenshot 10.3 s 1080p30 + AAC 48 kHz export (desktop-hype.mp4)."""
    video = trak(b"vide", 1, 494400, 15360, 158208, [(494400, 1024, 0x10000)], entry=b"avc1")
    audio = trak(b"soun", 2, 494592, 48000, 493568, audio_edits, entry=audio_entry, elst_version=elst_version)
    mvhd = full(b"mvhd", 0, struct.pack(">IIII", 0, 0, 48000, 494592) + b"\0" * 80)
    moov = box(b"moov", mvhd + video + audio + extra)
    ftyp = box(b"ftyp", b"isom\0\0\2\0isomiso2avc1mp41")
    mdat = box(b"mdat", bytes(range(256)) * 4)
    return ftyp + (moov + mdat if moov_first else mdat + moov)


LIBOPENSHOT_EDITS = [(1024, -1, 0x10000), (493568, 0, 0x10000)]


def _boxes(buf, start, end):
    return {b.type: b for b in priming._children(buf, start, end)}


def _audio(buf):
    moov_start = buf.index(b"moov") - 4
    moov = priming._children(buf, moov_start, len(buf))[0]
    traks = [b for b in priming._children(buf, moov.body, moov.end) if b.type == b"trak"]
    return moov, traks


def _write(tmp_path, data, name="out.mp4"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_libopenshot_edit_list_starts_audio_at_the_encoder_delay(tmp_path):
    before = movie(LIBOPENSHOT_EDITS)
    path = _write(tmp_path, before)

    receipt = priming.trim_audio_priming(path, "aac")

    assert receipt["status"] == "trimmed" and receipt["tracks"] == 1 and receipt["encoder_delay_samples"] == 1024
    after = bytearray(Path(path).read_bytes())
    assert len(after) == len(before)
    moov, (video, audio) = _audio(after)
    kids = priming._children(after, audio.body, audio.end)
    assert [k.type for k in kids] == [b"tkhd", b"edts", b"free", b"mdia"]
    edts = kids[1]
    version, entries = priming._parse_elst(after, priming._child(after, edts, b"elst"))
    # One edit: skip the 1024 priming samples, play the other 492544.
    assert entries == [(493568 - 1024, 1024, 0x10000)]
    assert priming._read_duration(after, kids[0], *priming._TKHD_DUR) == 492544
    mvhd = priming._child(after, moov, b"mvhd")
    assert priming._read_duration(after, mvhd, *priming._MVHD_DUR) == 494400  # the video is now longest
    # Nothing outside the audio edit list and the two durations moved.
    assert after[video.start:video.end] == before[video.start:video.end]
    assert after[audio.end:] == before[audio.end:]
    assert after[kids[3].start:kids[3].end] == before[kids[3].start:kids[3].end]
    assert moov.size == priming._children(bytearray(before), moov.start, len(before))[0].size


def test_trimming_twice_changes_nothing(tmp_path):
    path = _write(tmp_path, movie(LIBOPENSHOT_EDITS))
    priming.trim_audio_priming(path, "aac")
    once = Path(path).read_bytes()

    receipt = priming.trim_audio_priming(path, "aac")

    assert receipt["status"] == "skipped" and "already trimmed" in receipt["reason"]
    assert Path(path).read_bytes() == once


def test_moov_after_mdat_and_single_untrimmed_edit(tmp_path):
    path = _write(tmp_path, movie([(493568, 0, 0x10000)], moov_first=False))

    assert priming.trim_audio_priming(path, "aac")["status"] == "trimmed"

    buf = bytearray(Path(path).read_bytes())
    _, (_, audio) = _audio(buf)
    edts = priming._child(buf, audio, b"edts")
    assert priming._parse_elst(buf, priming._child(buf, edts, b"elst"))[1] == [(492544, 1024, 0x10000)]
    assert priming._child(buf, audio, b"free") is None  # same size, no padding needed


def test_version_1_edit_list(tmp_path):
    path = _write(tmp_path, movie([(1024, -1, 0x10000), (493568, 0, 0x10000)], elst_version=1))

    assert priming.trim_audio_priming(path, "aac")["status"] == "trimmed"

    buf = bytearray(Path(path).read_bytes())
    _, (_, audio) = _audio(buf)
    edts = priming._child(buf, audio, b"edts")
    assert priming._parse_elst(buf, priming._child(buf, edts, b"elst")) == (1, [(492544, 1024, 0x10000)])
    assert priming._child(buf, audio, b"free").size == 20


@pytest.mark.parametrize("data, codec, reason", [
    (movie(LIBOPENSHOT_EDITS), "libmp3lame", "unknown"),
    (movie(LIBOPENSHOT_EDITS), "", "unknown"),
    (b"RIFF\x24\0\0\0WAVEfmt " + b"\0" * 64, "aac", "not an MP4/MOV"),
    (movie(LIBOPENSHOT_EDITS, audio_entry=b"Opus"), "aac", "not AAC"),
    (movie(LIBOPENSHOT_EDITS, extra=box(b"mvex", b"")), "aac", "fragmented"),
    (movie([(493568, 2112, 0x10000)]), "aac", "already trimmed"),
    (movie([(493568, 0, 0x10000), (10, 0, 0x10000)]), "aac", "not the shape"),
])
def test_files_it_does_not_understand_are_left_alone(tmp_path, data, codec, reason):
    path = _write(tmp_path, data)

    receipt = priming.trim_audio_priming(path, codec)

    assert receipt["status"] == "skipped" and reason in receipt["reason"]
    assert Path(path).read_bytes() == data


def test_a_truncated_file_fails_without_writing(tmp_path):
    whole = movie(LIBOPENSHOT_EDITS, moov_first=False)
    data = whole[:-40]
    path = _write(tmp_path, data)

    receipt = priming.trim_audio_priming(path, "aac")

    assert receipt["status"] in ("skipped", "failed")
    assert Path(path).read_bytes() == data
    assert priming.trim_audio_priming(str(tmp_path / "missing.mp4"), "aac")["status"] == "failed"


def test_logged_wrapper_never_raises(tmp_path):
    assert priming.trim_export_audio_priming(str(tmp_path / "missing.mp4"), "aac")["status"] == "failed"


# --- end to end with real AAC, when ffmpeg is installed -----------------------

FFMPEG = shutil.which("ffmpeg")
CLICK = 0.5


def _ffmpeg(*args):
    subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True, capture_output=True)


def _first_click_seconds(path):
    """Presentation time of the first click, the way a player hears it."""
    # first_pts=0 keeps a leading empty edit as silence; a raw decode drops it.
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-map", "0:a:0", "-af",
                          "aresample=async=1:first_pts=0", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    samples = array.array("f")
    samples.frombytes(raw)
    for i, v in enumerate(samples):
        if abs(v) > 0.1:
            return i / 48000.0
    raise AssertionError("no click")


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
def test_clicks_land_on_time_after_trimming(tmp_path):
    # An AAC track muxed the way libopenshot muxes it: packets stamped one
    # frame late (pts 1024 for the first), so the mov muxer writes an empty
    # edit and keeps the priming.
    tone = str(tmp_path / "tone.mp4")
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x64:r=30:d=1.5",
            "-f", "lavfi", "-i", "aevalsrc=if(between(t\\,%s\\,%s)\\,0.8*sin(2*PI*1000*t)\\,0):s=48000:d=1.5"
            % (CLICK, CLICK + 0.004),
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac", "-af", "asetpts=PTS+2048", tone)
    late = _first_click_seconds(tone)
    if abs(late - CLICK - 2048 / 48000) > 0.002:
        pytest.skip("this ffmpeg does not reproduce libopenshot's layout (click at %.4f s)" % late)
    video_before = subprocess.run([FFMPEG, "-v", "error", "-i", tone, "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
                                  check=True, capture_output=True).stdout

    assert priming.trim_audio_priming(tone, "aac")["status"] == "trimmed"

    assert abs(_first_click_seconds(tone) - CLICK) < 0.0005
    video_after = subprocess.run([FFMPEG, "-v", "error", "-i", tone, "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
                                 check=True, capture_output=True).stdout
    assert video_after == video_before
