import os
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from classes.ffmpeg_cli import find_ffmpeg, resolve_ffmpeg_args, run_ffmpeg


def test_resolve_ffmpeg_args_rewrites_bare_name(tmp_path):
    fake = tmp_path / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    fake.write_text("", encoding="utf-8")
    find_ffmpeg.cache_clear()
    with patch("classes.ffmpeg_cli.find_ffmpeg", return_value=str(fake)):
        out = resolve_ffmpeg_args(["ffmpeg", "-y", "-i", "in.mp4"])
    assert out[0] == str(fake)
    assert out[1:] == ["-y", "-i", "in.mp4"]


def test_resolve_ffprobe_args_rewrites_bare_name(tmp_path):
    fake = tmp_path / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    fake.write_text("", encoding="utf-8")
    with patch("classes.ffmpeg_cli.find_ffmpeg", return_value=str(fake)):
        out = resolve_ffmpeg_args(["ffprobe", "-v", "error"])
    assert out[0] == str(fake)


def test_run_ffmpeg_hides_windows_console():
    with patch("classes.ffmpeg_cli.find_ffmpeg", return_value="ffmpeg"), patch(
        "classes.ffmpeg_cli.subprocess.run"
    ) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(["ffmpeg"], 0)
        run_ffmpeg(["ffmpeg", "-version"], check=False)
    kwargs = mock_run.call_args.kwargs
    if sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    else:
        assert "creationflags" not in kwargs
