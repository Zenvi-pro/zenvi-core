"""Trim the encoder priming from the audio of MP4/MOV files libopenshot writes.

libopenshot's FFmpegWriter stamps audio packet N with pts (N + 1) * frame_size
instead of the pts the encoder returns. Two things go wrong in the file:

* the muxer writes an empty edit of one frame (1024 samples for AAC) before
  the audio, and
* the edit list starts the audio at media time 0, so the encoder's priming
  samples (1024 for FFmpeg's native AAC encoder) are played, not trimmed.

Together that puts every sound 2048 samples (42.7 ms at 48 kHz, 1.3 frames at
30 fps) late against the picture and against the engine's render, which
trims the priming the standard way (edit list media time = encoder delay).

``trim_audio_priming`` rewrites the audio track's edit list in the ``moov``
box to that standard form: one edit that starts at the encoder delay. No
packet is re-encoded or moved and the video track is not touched. The new
edit list is shorter than the old one; a ``free`` box takes up the
difference, so every box and chunk offset in the file stays where it was.
"""

import os
import struct

from classes.logger import log

# Encoder delay in samples, by the codec name passed to SetAudioOptions.
# Only encoders whose delay is known are listed; anything else is left alone.
ENCODER_DELAY = {
    "aac": 1024,
}

# Refuse a moov larger than this rather than read it into memory.
_MAX_MOOV_BYTES = 256 * 1024 * 1024
_RATE_ONE = 0x00010000


class _Box:
    __slots__ = ("type", "start", "header", "size")

    def __init__(self, box_type, start, header, size):
        self.type = box_type
        self.start = start
        self.header = header
        self.size = size

    @property
    def body(self):
        return self.start + self.header

    @property
    def end(self):
        return self.start + self.size


def _children(buf, start, end):
    """Boxes in buf[start:end]. Raises ValueError on a malformed size."""
    boxes = []
    pos = start
    while pos + 8 <= end:
        size, box_type = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise ValueError("truncated box header")
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError("bad box size")
        boxes.append(_Box(box_type, pos, header, size))
        pos += size
    return boxes


def _child(buf, box, box_type):
    for c in _children(buf, box.body, box.end):
        if c.type == box_type:
            return c
    return None


def _top_level_moov(f, file_size):
    """(offset, size) of the top-level moov box, or None if it is not an ISO file."""
    pos = 0
    seen_ftyp = False
    while pos + 8 <= file_size:
        f.seek(pos)
        head = f.read(16)
        if len(head) < 8:
            return None
        size, box_type = struct.unpack_from(">I4s", head, 0)
        if size == 1:
            if len(head) < 16:
                return None
            size = struct.unpack_from(">Q", head, 8)[0]
        elif size == 0:
            size = file_size - pos
        if size < 8 or pos + size > file_size:
            return None
        if box_type == b"ftyp":
            seen_ftyp = True
        elif box_type == b"moov":
            return (pos, size) if seen_ftyp or pos == 0 else None
        elif not seen_ftyp and box_type not in (b"wide", b"free", b"skip", b"mdat"):
            return None
        pos += size
    return None


def _full_box_version(buf, box):
    return buf[box.body]


def _read_duration(buf, box, v0_offset, v1_offset):
    """Duration field of tkhd/mvhd (offsets are from the box body)."""
    if _full_box_version(buf, box) == 1:
        return struct.unpack_from(">Q", buf, box.body + v1_offset)[0]
    return struct.unpack_from(">I", buf, box.body + v0_offset)[0]


def _write_duration(buf, box, v0_offset, v1_offset, value):
    if _full_box_version(buf, box) == 1:
        struct.pack_into(">Q", buf, box.body + v1_offset, value)
    else:
        struct.pack_into(">I", buf, box.body + v0_offset, min(value, 0xFFFFFFFF))


# Duration offsets inside the box body: version/flags, creation, modification,
# then track_id + reserved (tkhd) or timescale (mvhd).
_TKHD_DUR = (20, 28)
_MVHD_DUR = (16, 24)


def _mvhd_timescale(buf, mvhd):
    off = 20 if _full_box_version(buf, mvhd) == 1 else 12
    return struct.unpack_from(">I", buf, mvhd.body + off)[0]


def _mdhd_timescale_duration(buf, mdhd):
    if _full_box_version(buf, mdhd) == 1:
        return struct.unpack_from(">IQ", buf, mdhd.body + 20)
    return struct.unpack_from(">II", buf, mdhd.body + 12)


def _parse_elst(buf, elst):
    version = _full_box_version(buf, elst)
    count = struct.unpack_from(">I", buf, elst.body + 4)[0]
    fmt, step = (">QqI", 20) if version == 1 else (">IiI", 12)
    if elst.body + 8 + count * step > elst.end:
        raise ValueError("truncated elst")
    entries = [struct.unpack_from(fmt, buf, elst.body + 8 + i * step) for i in range(count)]
    return version, entries


def _build_edts(version, duration, media_time):
    if version == 1:
        elst_body = struct.pack(">B3xIQqI", 1, 1, duration, media_time, _RATE_ONE)
    else:
        elst_body = struct.pack(">B3xIIiI", 0, 1, duration, media_time, _RATE_ONE)
    elst = struct.pack(">I4s", 8 + len(elst_body), b"elst") + elst_body
    return struct.pack(">I4s", 8 + len(elst), b"edts") + elst


def _sound_sample_entry(buf, stbl):
    """(format, sample_rate) of the first sample entry of an audio stsd."""
    stsd = _child(buf, stbl, b"stsd")
    if stsd is None or stsd.size < 16 + 36:
        return None, 0
    entry_type = bytes(buf[stsd.body + 12:stsd.body + 16])
    # AudioSampleEntry: 8-byte box header + 6 reserved + 2 index + 8 reserved
    # + channels, samplesize, pre_defined, reserved (8) + samplerate 16.16.
    rate = struct.unpack_from(">I", buf, stsd.body + 8 + 32)[0] >> 16
    return entry_type, rate


def _plan_track(buf, trak, movie_timescale, delay):
    """Patch for one audio track: (edts box, new bytes, tkhd, new tkhd duration) or a skip reason."""
    tkhd = _child(buf, trak, b"tkhd")
    mdia = _child(buf, trak, b"mdia")
    if tkhd is None or mdia is None:
        return "track without tkhd/mdia"
    hdlr = _child(buf, mdia, b"hdlr")
    if hdlr is None or bytes(buf[hdlr.body + 8:hdlr.body + 12]) != b"soun":
        return None  # not audio
    mdhd = _child(buf, mdia, b"mdhd")
    minf = _child(buf, mdia, b"minf")
    stbl = _child(buf, minf, b"stbl") if minf is not None else None
    if mdhd is None or stbl is None:
        return "audio track without mdhd/stbl"
    entry_type, sample_rate = _sound_sample_entry(buf, stbl)
    if entry_type != b"mp4a":
        return "audio track is not AAC (%s)" % (entry_type or b"?").decode("latin-1")
    media_timescale, media_duration = _mdhd_timescale_duration(buf, mdhd)
    if media_timescale != sample_rate or media_timescale <= 0:
        return "audio timescale %s does not match the sample rate %s" % (media_timescale, sample_rate)
    edts = _child(buf, trak, b"edts")
    elst = _child(buf, edts, b"elst") if edts is not None else None
    if edts is None or elst is None:
        return "audio track has no edit list"
    if len(_children(buf, edts.body, edts.end)) != 1:
        return "audio edit list box has extra children"
    version, entries = _parse_elst(buf, elst)
    if len(entries) == 2 and entries[0][1] == -1:
        entries = entries[1:]  # libopenshot's one-frame empty edit
    if len(entries) != 1 or entries[0][2] != _RATE_ONE:
        return "audio edit list is not the shape libopenshot writes"
    if entries[0][1] != 0:
        return "already trimmed (audio starts at media time %d)" % entries[0][1]
    if media_duration <= delay:
        return "audio track is shorter than the encoder delay"
    duration = (media_duration - delay) * movie_timescale // media_timescale
    if version == 0 and duration > 0xFFFFFFFF:
        version = 1
    new_edts = _build_edts(version, duration, delay)
    spare = edts.size - len(new_edts)
    if spare == 0:
        patch = new_edts
    elif spare >= 8:
        patch = new_edts + struct.pack(">I4s", spare, b"free") + b"\0" * (spare - 8)
    else:
        return "no room to rewrite the audio edit list"
    return edts, patch, tkhd, duration


def trim_audio_priming(path, audio_codec):
    """Rewrite *path* so its audio starts on time. Returns a receipt dict.

    ``status`` is ``"trimmed"`` (file changed), ``"skipped"`` (nothing to do or
    not a file this applies to; ``reason`` says why) or ``"failed"`` (the file
    could not be read or written; ``reason`` has the error). The file is only
    written in the ``"trimmed"`` case, with one write of the moov box.
    """
    receipt = {"status": "skipped", "path": path, "audio_codec": audio_codec, "tracks": 0}
    delay = ENCODER_DELAY.get(str(audio_codec or ""))
    if delay is None:
        receipt["reason"] = "encoder delay of %r is unknown" % (audio_codec,)
        return receipt
    try:
        with open(path, "r+b") as f:
            f.seek(0, os.SEEK_END)
            located = _top_level_moov(f, f.tell())
            if located is None:
                receipt["reason"] = "not an MP4/MOV file"
                return receipt
            moov_at, moov_size = located
            if moov_size > _MAX_MOOV_BYTES:
                receipt["reason"] = "moov box too large"
                return receipt
            f.seek(moov_at)
            buf = bytearray(f.read(moov_size))
            if len(buf) != moov_size:
                raise ValueError("truncated moov box")
            moov = _children(buf, 0, len(buf))[0]
            boxes = _children(buf, moov.body, moov.end)
            mvhd = next((b for b in boxes if b.type == b"mvhd"), None)
            if mvhd is None:
                receipt["reason"] = "moov without mvhd"
                return receipt
            if any(b.type == b"mvex" for b in boxes):
                receipt["reason"] = "fragmented MP4"
                return receipt
            movie_timescale = _mvhd_timescale(buf, mvhd)
            traks = [b for b in boxes if b.type == b"trak"]
            plans, reasons = [], []
            for trak in traks:
                plan = _plan_track(buf, trak, movie_timescale, delay)
                if isinstance(plan, tuple):
                    plans.append(plan)
                elif plan:
                    reasons.append(plan)
            if not plans:
                receipt["reason"] = "; ".join(reasons) or "no audio track"
                return receipt
            for edts, patch, tkhd, duration in plans:
                buf[edts.start:edts.end] = patch
                _write_duration(buf, tkhd, *_TKHD_DUR, duration)
            movie_duration = max(
                _read_duration(buf, tkhd, *_TKHD_DUR)
                for tkhd in (_child(buf, t, b"tkhd") for t in traks) if tkhd is not None
            )
            _write_duration(buf, mvhd, *_MVHD_DUR, movie_duration)
            f.seek(moov_at)
            f.write(buf)
            f.flush()
            os.fsync(f.fileno())
    except (OSError, ValueError, struct.error, IndexError) as exc:
        receipt["status"] = "failed"
        receipt["reason"] = str(exc)
        return receipt
    receipt["status"] = "trimmed"
    receipt["tracks"] = len(plans)
    receipt["encoder_delay_samples"] = delay
    return receipt


def trim_export_audio_priming(path, audio_codec):
    """Run trim_audio_priming on a finished export and log the outcome.

    A file this cannot fix is still a usable export, so failures are logged,
    not raised. Returns the receipt.
    """
    receipt = trim_audio_priming(path, audio_codec)
    if receipt["status"] == "failed":
        log.warning("Could not trim audio priming in %s: %s", path, receipt.get("reason"))
    else:
        log.info("Audio priming %s for %s (%s)", receipt["status"], path,
                 receipt.get("reason") or "%s sample(s)" % receipt.get("encoder_delay_samples"))
    return receipt
