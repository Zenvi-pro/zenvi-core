"""Gemini direct upload + auto-index gate (headless)."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

_REPO = Path(__file__).resolve().parents[1]


@patch("classes.gemini_direct_upload.requests.post")
@patch("classes.gemini_direct_upload.os.path.isfile", return_value=True)
@patch("classes.gemini_direct_upload.os.path.getsize", return_value=4)
def test_gemini_direct_upload_http_error(mock_size, mock_isfile, mock_post, tmp_path):
    from classes.gemini_direct_upload import upload_file_to_gemini_resumable

    f = tmp_path / "c.mp4"
    f.write_bytes(b"abcd")
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "boom"
    mock_post.return_value = mock_resp

    info, err = upload_file_to_gemini_resumable(str(f), "https://upload.example/u")
    assert err
    assert "500" in err
    assert info == {}


@patch("classes.gemini_direct_upload.requests.post")
def test_gemini_direct_upload_missing_url(mock_post, tmp_path):
    from classes.gemini_direct_upload import upload_file_to_gemini_resumable

    f = tmp_path / "c.mp4"
    f.write_bytes(b"abcd")
    info, err = upload_file_to_gemini_resumable(str(f), "")
    assert "upload_url" in err
    assert info == {}
    mock_post.assert_not_called()


def test_auto_index_gate_media_types():
    """Import path queues video/image/audio unless skip_indexing (source contract)."""
    src = (_REPO / "src" / "windows" / "models" / "files_model.py").read_text(encoding="utf-8")
    assert 'media_type") in (\n                    "video", "image", "audio"' in src or \
           '"video", "image", "audio"' in src
    assert "skip_indexing" in src
    assert 'media_type") not in ("video", "image", "audio")' in src or \
           'not in ("video", "image", "audio")' in src
