"""Watch-window frame extraction: sampling plan + ffmpeg wrapper."""

import base64
import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from classes import watch_frames as wf  # noqa: E402


# --------------------------------------------------------------------------
# plan_frame_times
# --------------------------------------------------------------------------

def test_samples_are_strictly_inside_the_window():
    times = wf.plan_frame_times(10.0, 20.0)
    assert times
    assert all(10.0 < t < 20.0 for t in times)


def test_the_first_sample_is_never_the_window_edge():
    """The backend prompt warns against picking the first frame."""
    times = wf.plan_frame_times(10.0, 20.0)
    assert times[0] > 10.0


def test_samples_are_ordered_and_evenly_spaced():
    times = wf.plan_frame_times(0.0, 12.0, count=6)
    assert times == sorted(times)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gaps[0] == pytest.approx(g) for g in gaps)


def test_default_is_six_frames():
    assert len(wf.plan_frame_times(0.0, 60.0)) == 6


def test_the_backend_cap_is_honoured():
    assert len(wf.plan_frame_times(0.0, 60.0, count=99)) == wf.WATCH_FRAME_MAX


def test_at_least_two_frames_are_requested():
    assert len(wf.plan_frame_times(0.0, 60.0, count=1)) == 2
    assert len(wf.plan_frame_times(0.0, 60.0, count=0)) == 2


def test_a_short_window_gets_fewer_frames():
    """Six stills from 1.5s of video would be near-duplicates."""
    assert len(wf.plan_frame_times(0.0, 1.5, count=6)) == 3


def test_a_reversed_window_is_normalised():
    assert wf.plan_frame_times(20.0, 10.0) == wf.plan_frame_times(10.0, 20.0)


def test_a_zero_length_window_yields_the_single_instant():
    assert wf.plan_frame_times(5.0, 5.0) == [5.0]


@pytest.mark.parametrize("bad", [("a", 1.0), (1.0, "b"), (None, 2.0)])
def test_unparseable_bounds_yield_nothing(bad):
    assert wf.plan_frame_times(*bad) == []


# --------------------------------------------------------------------------
# extract_watch_frames
# --------------------------------------------------------------------------

def _fake_ffmpeg(payload=b"JPEGDATA", fail_indices=()):
    """Stand in for _ffmpeg_run, writing a real file so encoding is exercised."""
    calls = []

    def _run(args):
        calls.append(args)
        out_path = args[-1]
        if (len(calls) - 1) in fail_indices:
            return False, "ffmpeg: boom"
        with open(out_path, "wb") as fh:
            fh.write(payload)
        return True, ""

    return _run, calls


def test_a_missing_file_is_reported_not_raised(tmp_path):
    frames, err = wf.extract_watch_frames(str(tmp_path / "nope.mp4"), 0.0, 5.0)
    assert frames == []
    assert "not found" in err.lower()


def test_frames_carry_their_timestamp_and_base64(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    run, _calls = _fake_ffmpeg()
    with patch.object(wf, "_ffmpeg_run", run):
        frames, err = wf.extract_watch_frames(str(src), 10.0, 20.0)
    assert err == ""
    assert len(frames) == 6
    assert all(10.0 < f["timestamp"] < 20.0 for f in frames)
    assert base64.b64decode(frames[0]["image_base64"]) == b"JPEGDATA"


def test_the_ffmpeg_argv_uses_accurate_seek_and_scales(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    run, calls = _fake_ffmpeg()
    with patch.object(wf, "_ffmpeg_run", run):
        wf.extract_watch_frames(str(src), 10.0, 20.0, count=2, width=320)
    assert len(calls) == 2
    argv = calls[0]
    # -ss must precede -i, otherwise the seek is slow and the label is wrong.
    assert argv.index("-ss") < argv.index("-i")
    assert "scale=320:-2" in argv
    assert argv[argv.index("-frames:v") + 1] == "1"


def test_a_partial_extraction_still_returns_usable_frames(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    run, _calls = _fake_ffmpeg(fail_indices={0, 2})
    with patch.object(wf, "_ffmpeg_run", run):
        frames, err = wf.extract_watch_frames(str(src), 0.0, 10.0)
    assert err == ""
    assert len(frames) == 4


def test_a_total_failure_reports_the_ffmpeg_error(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    run, _calls = _fake_ffmpeg(fail_indices=set(range(12)))
    with patch.object(wf, "_ffmpeg_run", run):
        frames, err = wf.extract_watch_frames(str(src), 0.0, 10.0)
    assert frames == []
    assert "boom" in err


def test_an_empty_output_file_is_not_sent_as_a_frame(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    run, _calls = _fake_ffmpeg(payload=b"")
    with patch.object(wf, "_ffmpeg_run", run):
        frames, err = wf.extract_watch_frames(str(src), 0.0, 10.0)
    assert frames == []
    assert err


def test_the_temp_dir_is_always_removed(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    seen = []

    def _run(args):
        seen.append(os.path.dirname(args[-1]))
        with open(args[-1], "wb") as fh:
            fh.write(b"J")
        return True, ""

    with patch.object(wf, "_ffmpeg_run", _run):
        wf.extract_watch_frames(str(src), 0.0, 10.0)
    assert seen and not os.path.exists(seen[0])


def test_the_temp_dir_is_removed_even_when_ffmpeg_raises(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    seen = []

    def _run(args):
        seen.append(os.path.dirname(args[-1]))
        raise RuntimeError("ffmpeg exploded")

    with patch.object(wf, "_ffmpeg_run", _run):
        with pytest.raises(RuntimeError):
            wf.extract_watch_frames(str(src), 0.0, 10.0)
    assert seen and not os.path.exists(seen[0])


def test_never_more_than_the_backend_cap_is_uploaded(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    run, _calls = _fake_ffmpeg()
    with patch.object(wf, "_ffmpeg_run", run):
        frames, _err = wf.extract_watch_frames(str(src), 0.0, 600.0, count=99)
    assert len(frames) <= wf.WATCH_FRAME_MAX
