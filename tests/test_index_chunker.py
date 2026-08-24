"""Desktop chunker + Gemini direct upload unit tests."""

import os
import sys
from unittest.mock import MagicMock, patch

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
def test_extract_chunks_calls_ffmpeg(mock_size, mock_isfile, mock_ff, tmp_path):
    from classes.index_chunker import extract_chunks

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
    assert "min(720,ih)" in vf
    assert "720" in vf


def test_video_scale_filter_never_upsizes_constant():
    from classes.index_chunker import video_scale_filter
    assert video_scale_filter(720) == "scale=-2:'min(720,ih)'"
    assert video_scale_filter(480) == "scale=-2:'min(480,ih)'"
    # 480p source stays 480 via min(720,ih); filter does not force 720x720 upscale.
    assert "720,720" not in video_scale_filter(720)


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
