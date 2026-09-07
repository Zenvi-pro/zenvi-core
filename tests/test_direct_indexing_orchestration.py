"""Direct Gemini indexing orchestration + auto-index gate (headless)."""

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


def test_start_direct_indexing_job_orchestration(tmp_path):
    from classes.api_client import ZenviBackendClient

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video-bytes")

    client = ZenviBackendClient.__new__(ZenviBackendClient)
    client.api_url = "http://backend.test/api/v1"
    client._ssl_verify = True

    session = MagicMock()

    plan_resp = MagicMock()
    plan_resp.raise_for_status = MagicMock()
    plan_resp.json.return_value = {
        "chunks": [{"chunk_index": 0, "start": 0.0, "end": 5.0, "role": "av"}],
    }

    sess_resp = MagicMock()
    sess_resp.raise_for_status = MagicMock()
    sess_resp.json.return_value = {
        "job_id": "job-1",
        "upload_url": "https://upload.example/u",
        "presigned_urls": [{"url": "https://upload.example/u"}],
    }

    complete_resp = MagicMock()
    complete_resp.raise_for_status = MagicMock()
    complete_resp.json.return_value = {"success": True, "job_id": "job-1"}

    session.post.side_effect = [plan_resp, sess_resp, complete_resp]

    with patch("classes.index_chunker.extract_chunks") as extract, \
         patch("classes.index_chunker.cleanup_chunk_dir") as cleanup, \
         patch(
             "classes.gemini_direct_upload.upload_file_to_gemini_resumable",
             return_value=({"name": "files/x", "uri": "https://x/files/x"}, ""),
         ), \
         patch.object(
             client,
             "_poll_indexing_job",
             return_value={"success": True, "video_id": "vid-1", "status": "ready"},
         ):
        extract.return_value = (
            [{
                "chunk_index": 0,
                "path": str(media),
                "size": 11,
                "start": 0.0,
                "end": 5.0,
                "mime_type": "video/mp4",
            }],
            str(tmp_path / "work"),
            "",
        )
        result = client.start_direct_indexing_job(
            str(media),
            "zenvi-proj1",
            file_id="f1",
            filename="clip.mp4",
            session=session,
            project_id="proj1",
            duration_sec=5.0,
            media_type="video",
        )

    assert result.get("success") is True
    assert result.get("video_id") == "vid-1"
    assert session.post.call_count == 3
    plan_call = session.post.call_args_list[0]
    plan_payload = plan_call.kwargs.get("json")
    if plan_payload is None and plan_call.args:
        # requests-style: post(url, json=...)
        plan_payload = plan_call.kwargs["json"]
    assert plan_payload["media_type"] == "video"
    cleanup.assert_called_once()


def test_start_direct_indexing_job_uploads_in_parallel(tmp_path):
    import threading
    from classes.api_client import ZenviBackendClient

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video-bytes")

    client = ZenviBackendClient.__new__(ZenviBackendClient)
    client.api_url = "http://backend.test/api/v1"
    client._ssl_verify = True

    plan_resp = MagicMock()
    plan_resp.raise_for_status = MagicMock()
    plan_resp.json.return_value = {
        "chunks": [
            {"chunk_index": 0, "start": 0.0, "end": 5.0, "role": "av"},
            {"chunk_index": 1, "start": 5.0, "end": 10.0, "role": "av"},
        ],
        "index_max_height": 720,
    }
    complete_resp = MagicMock()
    complete_resp.raise_for_status = MagicMock()
    complete_resp.json.return_value = {"success": True, "job_id": "job-p"}

    plan_session = MagicMock()
    plan_session.post.side_effect = [plan_resp, complete_resp]

    barrier = threading.Barrier(2, timeout=5)
    session_posts = []

    def _hs_post(url, **kwargs):
        session_posts.append(url)
        barrier.wait()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "job_id": "job-p",
            "upload_url": "https://upload.example/u",
        }
        return resp

    hs = MagicMock()
    hs.post.side_effect = _hs_post

    with patch("classes.index_chunker.extract_chunks") as extract, \
         patch("classes.index_chunker.cleanup_chunk_dir"), \
         patch(
             "classes.gemini_direct_upload.upload_file_to_gemini_resumable",
             return_value=({"name": "files/x", "uri": "https://x/files/x"}, ""),
         ), \
         patch.object(client, "_new_http_session", return_value=hs), \
         patch.object(
             client,
             "_poll_indexing_job",
             return_value={"success": True, "video_id": "vid-1", "status": "ready"},
         ):
        extract.return_value = (
            [
                {"chunk_index": 0, "path": str(media), "size": 11, "start": 0.0, "end": 5.0, "mime_type": "video/mp4"},
                {"chunk_index": 1, "path": str(media), "size": 11, "start": 5.0, "end": 10.0, "mime_type": "video/mp4"},
            ],
            str(tmp_path / "work"),
            "",
        )
        result = client.start_direct_indexing_job(
            str(media),
            "zenvi-proj1",
            file_id="f1",
            filename="clip.mp4",
            session=plan_session,
            project_id="proj1",
            duration_sec=10.0,
            media_type="video",
        )
    assert result.get("success") is True
    assert len(session_posts) == 2


def test_auto_index_gate_media_types():
    """Import path queues video/image/audio unless skip_indexing (source contract)."""
    src = (_REPO / "src" / "windows" / "models" / "files_model.py").read_text(encoding="utf-8")
    assert 'media_type") in (\n                    "video", "image", "audio"' in src or \
           '"video", "image", "audio"' in src
    assert "skip_indexing" in src
    assert 'media_type") not in ("video", "image", "audio")' in src or \
           'not in ("video", "image", "audio")' in src


def test_poll_indexing_job_interval_is_3s():
    import inspect
    from classes.api_client import ZenviBackendClient

    sig = inspect.signature(ZenviBackendClient._poll_indexing_job)
    assert sig.parameters["poll_interval"].default == 3
    assert sig.parameters["max_wait"].default == 21600
