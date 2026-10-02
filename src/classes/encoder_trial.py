"""Child side of the hardware-encoder trial (see export_acceleration/hw_encode.py).

A hardware encoder FFmpeg lists can still reject the frames libopenshot hands
it, and some reject them with abort(): h264_videotoolbox asserts
``frame->format == AV_PIX_FMT_VIDEOTOOLBOX`` on libopenshot 1.0's frames, which
kills the whole process. So before export uses one, the app starts a copy of
itself with ``TRIAL_FLAG <codec>`` (src/launch.py hands over before the update
installer, crash handlers or Qt run) and this module encodes a few blank
frames the way export does. Exit code 0 means the encoder works.

Imports stay to the standard library and openshot: this runs in a fresh
process, and ``classes.info`` would pull in all of Qt.
"""

import os
import shutil
import sys
import tempfile

TRIAL_FLAG = "--zenvi-trial-encode"

_WIDTH, _HEIGHT = 640, 360
_FRAMES = 5


def main(argv):
    """Encode _FRAMES blank frames with the codec in argv[0]. Returns an exit code."""
    if not argv:
        return 2
    codec = str(argv[0])
    out_dir = tempfile.mkdtemp(prefix="zenvi-encoder-trial-")
    # Matroska takes every codec the presets use (H.264, HEVC, VP9, AV1).
    out_path = os.path.join(out_dir, "trial.mkv")
    try:
        import openshot

        fps = openshot.Fraction(30, 1)
        timeline = openshot.Timeline(
            _WIDTH, _HEIGHT, fps, 48000, 2, openshot.LAYOUT_STEREO)
        timeline.Open()
        writer = openshot.FFmpegWriter(out_path)
        writer.SetVideoOptions(
            True, codec, fps, _WIDTH, _HEIGHT, openshot.Fraction(1, 1),
            False, False, 2000000)
        writer.PrepareStreams()
        writer.Open()
        for number in range(1, _FRAMES + 1):
            writer.WriteFrame(timeline.GetFrame(number))
        writer.Close()
        timeline.Close()
        return 0 if os.path.getsize(out_path) > 0 else 1
    except Exception as exc:
        if sys.stderr is not None:
            sys.stderr.write("trial encode with %s failed: %s\n" % (codec, exc))
        return 1
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
