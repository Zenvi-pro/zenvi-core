"""classes.handoff.alpha: the never-WebM alpha rule, checked with real ffmpeg when it is installed."""

import shutil
import subprocess

import pytest

from classes.handoff import alpha

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


@needs_ffmpeg
def test_transparency_is_detected_per_pixel(tmp_path):
    half = str(tmp_path / "half.png")
    opaque_rgba = str(tmp_path / "opaque_rgba.png")
    opaque_rgb = str(tmp_path / "opaque.png")
    _ffmpeg("-f", "lavfi", "-i", "color=c=red@0.5:s=64x36,format=rgba", "-frames:v", "1", half)
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x36,format=rgba", "-frames:v", "1", opaque_rgba)
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x36", "-frames:v", "1", opaque_rgb)
    assert alpha.has_transparency(half) is True
    assert alpha.has_transparency(opaque_rgba) is False
    assert alpha.has_transparency(opaque_rgb) is False
    assert alpha.choose_codec(half) == "prores4444" and alpha.choose_codec(opaque_rgb) == "h264"


@needs_ffmpeg
def test_reencode_to_prores4444_keeps_alpha(tmp_path):
    src = str(tmp_path / "in.mov")
    _ffmpeg("-f", "lavfi", "-i", "color=c=red@0.5:s=64x36:d=0.5,format=rgba", "-c:v", "qtrle", "-pix_fmt", "argb", src)
    out = alpha.reencode_to_prores4444(src, str(tmp_path / "out"), keep_audio=False)
    assert out.endswith("out.mov")
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,pix_fmt", "-of", "csv=p=0", out], capture_output=True, text=True)
    assert "prores" in probe.stdout and "yuva444p" in probe.stdout
    assert alpha.has_transparency(out) is True


def test_codec_args_and_missing_ffmpeg(monkeypatch, tmp_path):
    assert "yuva444p10le" in alpha.codec_args("prores4444") and "argb" in alpha.codec_args("qtrle")
    assert "-crf" in alpha.codec_args("h264") and "bt709" in alpha.codec_args("h264")
    with pytest.raises(ValueError):
        alpha.codec_args("vp9")
    monkeypatch.setattr(alpha.ffmpeg_cli, "find_ffmpeg", lambda name="ffmpeg": None)
    with pytest.raises(alpha.AlphaError, match="ffmpeg was not found"):
        alpha.has_transparency(str(tmp_path / "x.png"))


def test_a_cancelled_reencode_is_a_cancellation_not_an_error(monkeypatch, tmp_path):
    import subprocess as sp
    from classes.handoff import jobs
    monkeypatch.setattr(alpha.ffmpeg_cli, "find_ffmpeg", lambda name="ffmpeg": "/usr/bin/true")
    monkeypatch.setattr(alpha.ffmpeg_cli, "run_ffmpeg_with_progress",
                        lambda cmd, on_progress=None, should_cancel=None: sp.CompletedProcess(cmd, 1, "", "cancelled"))
    with pytest.raises(jobs.JobCancelled):
        alpha.reencode_to_prores4444(str(tmp_path / "in.webm"), str(tmp_path / "out.mov"), should_cancel=lambda: True)
    with pytest.raises(alpha.AlphaError):
        alpha.reencode_to_prores4444(str(tmp_path / "in.webm"), str(tmp_path / "out.mov"), should_cancel=lambda: False)
