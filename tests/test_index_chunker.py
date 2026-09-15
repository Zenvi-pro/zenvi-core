"""Desktop chunker + Gemini direct upload unit tests."""

import os
import shutil
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


def test_cleanup_chunk_dir(tmp_path):
    from classes.index_chunker import cleanup_chunk_dir
    d = tmp_path / "chunks"
    d.mkdir()
    (d / "a.mp4").write_bytes(b"x")
    cleanup_chunk_dir(str(d))
    assert not d.exists()


@patch("classes.index_chunker._ffmpeg_run", return_value=(True, ""))
@patch("classes.index_chunker.os.path.isfile", return_value=True)
@patch("classes.index_chunker.os.path.getsize", return_value=123)
def test_extract_chunks_calls_ffmpeg(mock_size, mock_isfile, mock_ff, tmp_path, monkeypatch):
    from classes.index_chunker import extract_chunks

    monkeypatch.setenv("ZENVI_INDEX_MIN_HEIGHT", "540")

    # Make ffmpeg "create" the output file
    def _run(args):
        out = args[-1]
        open(out, "wb").write(b"data")
        return True, ""

    mock_ff.side_effect = _run
    plan = [{"chunk_index": 0, "start": 0.0, "end": 5.0, "role": "av"}]
    infos, work, err = extract_chunks("input.mp4", plan, out_dir=str(tmp_path / "w"))
    assert not err
    assert len(infos) == 1
    assert infos[0]["size"] == 123
    assert work
    ff_args = mock_ff.call_args[0][0]
    assert "-vf" in ff_args
    vf = ff_args[ff_args.index("-vf") + 1]
    assert "max(ih,540)" in vf
    assert "720" in vf


def test_video_scale_filter_only_caps_when_min_height_is_off():
    from classes.index_chunker import video_scale_filter
    assert video_scale_filter(720) == "scale=-2:'min(720,ih)'"
    assert video_scale_filter(480, 0) == "scale=-2:'min(480,ih)'"


def test_video_scale_filter_upscales_low_res_up_to_the_cap():
    from classes.index_chunker import video_scale_filter
    assert video_scale_filter(720, 540) == "scale=-2:'min(max(ih,540),720)':flags=lanczos"
    # A minimum above the backend's cap never pushes past the cap.
    assert video_scale_filter(480, 720) == "scale=-2:'min(max(ih,480),480)':flags=lanczos"


@pytest.mark.parametrize("env, expected", [
    (None, 720), ("540", 540), ("0", 0), ("-5", 0), ("junk", 720),
])
def test_index_min_height_reads_env(monkeypatch, env, expected):
    from classes.index_chunker import index_min_height
    if env is None:
        monkeypatch.delenv("ZENVI_INDEX_MIN_HEIGHT", raising=False)
    else:
        monkeypatch.setenv("ZENVI_INDEX_MIN_HEIGHT", env)
    assert index_min_height() == expected


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_low_res_chunk_is_upscaled_for_indexing(tmp_path, monkeypatch):
    """A 640x320 source (the #167 interview) must reach Gemini at 720p."""
    from classes.index_chunker import extract_chunk

    src = tmp_path / "low.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=size=640x320:rate=25:duration=2", "-pix_fmt", "yuv420p", str(src),
    ], check=True)
    monkeypatch.setenv("ZENVI_INDEX_MIN_HEIGHT", "720")
    path, err = extract_chunk(str(src), start=0.0, end=2.0, out_dir=str(tmp_path / "out"))
    assert not err, err
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path,
    ], capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "1440x720"


@patch("classes.gemini_direct_upload.requests.post")
@patch("classes.gemini_direct_upload.os.path.isfile", return_value=True)
@patch("classes.gemini_direct_upload.os.path.getsize", return_value=4)
def test_gemini_direct_upload_parses_file(mock_size, mock_isfile, mock_post, tmp_path):
    from classes.gemini_direct_upload import upload_file_to_gemini_resumable

    f = tmp_path / "c.mp4"
    f.write_bytes(b"abcd")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"file":{"name":"files/abc","uri":"https://x/files/abc"}}'
    mock_resp.json.return_value = {"file": {"name": "files/abc", "uri": "https://x/files/abc"}}
    mock_resp.text = mock_resp.content.decode()
    mock_post.return_value = mock_resp

    info, err = upload_file_to_gemini_resumable(str(f), "https://upload.example/u")
    assert not err
    assert info["name"] == "files/abc"
    assert "upload, finalize" in mock_post.call_args.kwargs["headers"]["X-Goog-Upload-Command"]


def test_twelvelabs_is_indexed_accepts_index_block():
    from classes.twelvelabs_match import twelvelabs_is_indexed, get_index_block

    ai = {
        "index": {"status": "ready", "index_id": "zenvi-p", "video_id": "f1"},
        "twelvelabs": {},
    }
    assert twelvelabs_is_indexed(get_index_block(ai))
    assert twelvelabs_is_indexed(ai)
