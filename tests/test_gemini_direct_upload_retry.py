"""Retrying the chunk byte upload to Gemini (zenvi-core#192, #156).

A long file uploads ~45 chunks straight to Gemini; one 503 or dropped
connection used to fail the whole index. Before each retry the upload is
queried, because Gemini answers a re-sent finished upload with a 400
("Upload has already been terminated") - checked against the live API.
"""

import os
import random
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from classes import gemini_direct_upload as gdu  # noqa: E402

_FILE = b'{"file": {"name": "files/abc", "uri": "https://g/files/abc", "mimeType": "video/mp4"}}'


def _resp(status, headers=None, body=b""):
    r = MagicMock()
    r.status_code = status
    r.headers = requests.structures.CaseInsensitiveDict(headers or {})
    r.content = body
    r.text = body.decode()
    r.json.side_effect = lambda: __import__("json").loads(body.decode())
    return r


def _query(status, received=0, body=b""):
    return _resp(200, {"X-Goog-Upload-Status": status, "X-Goog-Upload-Size-Received": str(received)}, body)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 0.0, "sleeps": []}

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(gdu, "_clock", lambda: state["now"])
    monkeypatch.setattr(gdu, "_sleep", sleep)
    monkeypatch.setattr(gdu, "_rng", random.Random(3))
    return state


@pytest.fixture
def chunk(tmp_path):
    f = tmp_path / "c0.mp4"
    f.write_bytes(b"0123456789")
    return str(f)


def _commands(post):
    return [c.kwargs["headers"]["X-Goog-Upload-Command"] for c in post.call_args_list]


def test_a_503_is_retried_after_checking_the_upload(clock, chunk):
    with patch.object(gdu.requests, "post", side_effect=[
        _resp(503, body=b"unavailable"), _query("active", 0), _resp(200, body=_FILE),
    ]) as post:
        info, err = gdu.upload_file_to_gemini_resumable(chunk, "https://upload/u")
    assert err == ""
    assert info["name"] == "files/abc"
    assert _commands(post) == ["upload, finalize", "query", "upload, finalize"]
    assert len(clock["sleeps"]) == 1


def test_a_timeout_whose_upload_landed_is_not_sent_again(clock, chunk):
    """The finalize reached Gemini but the answer was lost: the query has the file."""
    with patch.object(gdu.requests, "post", side_effect=[
        requests.ReadTimeout("read timed out"), _query("final", 10, _FILE),
    ]) as post:
        info, err = gdu.upload_file_to_gemini_resumable(chunk, "https://upload/u")
    assert err == ""
    assert info["uri"] == "https://g/files/abc"
    assert _commands(post) == ["upload, finalize", "query"]


def test_a_partial_upload_resumes_at_the_received_offset(clock, chunk):
    sent = []

    def post(url, data=None, headers=None, timeout=None):
        if headers["X-Goog-Upload-Command"] == "query":
            return _query("active", 4)
        sent.append((headers["X-Goog-Upload-Offset"], headers["Content-Length"], data.read()))
        if len(sent) == 1:
            raise requests.ConnectionError("Connection reset by peer")
        return _resp(200, body=_FILE)

    with patch.object(gdu.requests, "post", side_effect=post):
        info, err = gdu.upload_file_to_gemini_resumable(chunk, "https://upload/u")
    assert err == "" and info["name"] == "files/abc"
    assert sent[1] == ("4", "6", b"456789")


def test_a_400_fails_without_retrying(clock, chunk):
    with patch.object(gdu.requests, "post", return_value=_resp(400, body=b"bad request")) as post:
        info, err = gdu.upload_file_to_gemini_resumable(chunk, "https://upload/u")
    assert info == {}
    assert err == "Gemini upload failed (400): bad request"
    assert post.call_count == 1
    assert clock["sleeps"] == []


def test_a_429_waits_for_retry_after(clock, chunk):
    with patch.object(gdu.requests, "post", side_effect=[
        _resp(429, {"Retry-After": "9"}, b"slow down"), _query("active", 0), _resp(200, body=_FILE),
    ]):
        _, err = gdu.upload_file_to_gemini_resumable(chunk, "https://upload/u")
    assert err == ""
    assert 9.0 <= clock["sleeps"][0] <= 10.0


def test_retries_are_bounded(clock, chunk):
    def post(url, data=None, headers=None, timeout=None):
        if headers["X-Goog-Upload-Command"] == "query":
            return _query("active", 0)
        return _resp(503, body=b"unavailable")

    with patch.object(gdu.requests, "post", side_effect=post) as mock_post:
        info, err = gdu.upload_file_to_gemini_resumable(chunk, "https://upload/u")
    assert info == {}
    assert err.startswith("Gemini upload failed (503): unavailable (gave up after 5 attempts")
    assert _commands(mock_post).count("upload, finalize") == gdu._ATTEMPTS
    assert clock["now"] <= gdu._BUDGET_SEC


def test_a_retry_after_past_the_budget_gives_up_now(clock, chunk):
    with patch.object(gdu.requests, "post", return_value=_resp(429, {"Retry-After": "3600"}, b"quota")) as post:
        _, err = gdu.upload_file_to_gemini_resumable(chunk, "https://upload/u")
    assert "gave up after 1 attempts" in err
    assert post.call_count == 1
    assert clock["sleeps"] == []


def test_an_expired_upload_session_is_not_retried_blindly(clock, chunk):
    with patch.object(gdu.requests, "post", side_effect=[
        _resp(503, body=b"unavailable"), _resp(404, body=b"no such upload"),
    ]) as post:
        info, err = gdu.upload_file_to_gemini_resumable(chunk, "https://upload/u")
    assert info == {}
    assert "no longer usable" in err
    assert _commands(post) == ["upload, finalize", "query"]
