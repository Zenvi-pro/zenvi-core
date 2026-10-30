"""Index chunks described on this computer: pinned on-demand install, one llama.cpp call per chunk."""

import io
import json
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from classes import hyperframes_local as hf  # noqa: E402
from classes import local_describe as ld  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
EXE = "llama.exe" if sys.platform == "win32" else "llama"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ld, "home", lambda: str(tmp_path / "describe"))
    return tmp_path


def _fake_downloads(monkeypatch):
    """Stand in for the pinned downloader: record what was asked for, write a llama.cpp archive."""
    asked = []

    def download(folder, files, what):
        os.makedirs(folder, exist_ok=True)
        for url, name, digest in files:
            asked.append((url, name, digest))
            if name.endswith(".zip"):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as z:
                    z.writestr(EXE, "")
                Path(folder, name).write_bytes(buf.getvalue())
            elif name.endswith(".tar.gz"):
                import tarfile
                with tarfile.open(os.path.join(folder, name), "w:gz") as tar:
                    info = tarfile.TarInfo("llama-%s/%s" % (ld.LLAMA_BUILD, EXE))
                    tar.addfile(info, io.BytesIO(b""))
            else:
                Path(folder, name).write_bytes(b"gguf")

    monkeypatch.setattr(hf, "_download_pinned", download)
    return asked


def test_nothing_ships_and_every_download_is_pinned_per_computer(home):
    assert not ld.ready()
    for platform, machine in (("win32", "AMD64"), ("win32", "ARM64"), ("darwin", "arm64"), ("darwin", "x86_64"),
                              ("linux", "x86_64"), ("linux", "aarch64")):
        url, name, digest = ld._llama_download(platform, machine)
        assert ld.LLAMA_BUILD in url and url.endswith(name) and len(digest) == 64
    with pytest.raises(hf.HyperframesError):
        ld._llama_download("sunos5", "sparc")
    for url, name, digest in ld._model_downloads():
        assert ld.MODEL_REVISION in url and url.endswith(name) and len(digest) == 64


def test_install_downloads_once_unpacks_llama_and_is_then_ready(home, monkeypatch):
    asked = _fake_downloads(monkeypatch)
    ld.install()
    assert ld.ready() and os.path.basename(ld.llama_program()) == EXE
    names = [a[1] for a in asked]
    assert ld._llama_download()[1] in names and [m[1] for m in ld._model_downloads()] == names[-2:]
    assert not [f for f in os.listdir(ld.home()) if f.endswith((".zip", ".tar.gz"))]   # the archive is not kept
    asked.clear()
    ld.install()
    assert asked == []


def test_describe_asks_the_model_with_the_backend_prompt_and_schema(home, monkeypatch, tmp_path):
    _fake_downloads(monkeypatch)
    ld.install()
    seen = {}
    answer = {"short_summary": "A dog on a beach.", "visual": "dog", "sounds": "surf", "chapters": [],
              "transcript_cues": [], "moments": []}

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["schema"] = json.loads(Path(argv[argv.index("--json-schema-file") + 1]).read_text())
        seen["env"] = kwargs.get("env") or {}
        return SimpleNamespace(returncode=0, stdout="loading...\n" + json.dumps(answer) + "\n", stderr="log")

    monkeypatch.setattr(ld.subprocess, "run", run)
    clip = tmp_path / "chunk.mp4"
    clip.write_bytes(b"x")
    schema = {"type": "object", "properties": {"short_summary": {"type": "string"}}}
    assert ld.describe(str(clip), "video", "You analyze a video segment.", schema) == answer
    argv = seen["argv"]
    assert argv[:2] == [ld.llama_program(), "cli"]
    assert argv[argv.index("--video") + 1] == str(clip)
    assert argv[argv.index("-m") + 1].endswith(ld.MODEL[0]) and argv[argv.index("-mm") + 1].endswith(ld.PROJECTOR[0])
    assert "You analyze a video segment." in " ".join(argv) and argv[argv.index("--temp") + 1] == "0"
    assert seen["schema"] == schema
    assert "ZENVI_AUTH_TOKEN" not in seen["env"]                     # the editor's secrets stay out


def test_a_failed_or_unparseable_answer_is_an_error(home, monkeypatch, tmp_path):
    _fake_downloads(monkeypatch)
    ld.install()
    clip = tmp_path / "chunk.mp4"
    clip.write_bytes(b"x")
    monkeypatch.setattr(ld.subprocess, "run", lambda argv, **k: SimpleNamespace(returncode=1, stdout="", stderr="no"))
    with pytest.raises(hf.HyperframesError):
        ld.describe(str(clip), "video", "p", {})
    monkeypatch.setattr(ld.subprocess, "run", lambda argv, **k: SimpleNamespace(returncode=0, stdout="sorry", stderr=""))
    with pytest.raises(hf.HyperframesError):
        ld.describe(str(clip), "video", "p", {})


def test_local_indexing_never_uploads_to_gemini_and_sends_the_descriptions(tmp_path):
    from classes.api_client import ZenviBackendClient

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video-bytes")
    client = ZenviBackendClient.__new__(ZenviBackendClient)
    client.api_url = "http://backend.test/api/v1"
    client._ssl_verify = True
    session = MagicMock()
    plan = MagicMock(json=MagicMock(return_value={
        "chunks": [{"chunk_index": 0, "start": 0.0, "end": 5.0, "role": "av"}],
        "describe_prompt": "You analyze a video segment.", "describe_schema": {"type": "object"}}))
    complete = MagicMock(json=MagicMock(return_value={"success": True, "job_id": "job-1"}))
    session.post.side_effect = [plan, complete]
    chunk = {"chunk_index": 0, "path": str(media), "size": 11, "start": 0.0, "end": 5.0, "mime_type": "video/mp4"}

    with patch("classes.index_chunker.extract_chunks", return_value=([chunk], str(tmp_path / "w"), "")), \
         patch("classes.index_chunker.cleanup_chunk_dir"), \
         patch("classes.gemini_direct_upload.upload_file_to_gemini_resumable") as gemini_upload, \
         patch("classes.local_describe.ready", return_value=False), \
         patch("classes.local_describe.install") as install, \
         patch("classes.local_describe.describe", return_value={"short_summary": "a dog"}) as describe, \
         patch.object(client, "_poll_indexing_job", return_value={"success": True, "video_id": "f1"}):
        result = client.start_direct_indexing_job(
            str(media), "zenvi-p1", file_id="f1", filename="clip.mp4", session=session, project_id="p1",
            duration_sec=5.0, media_type="video", describe_locally=True)

    assert result.get("success") is True
    install.assert_called_once()
    gemini_upload.assert_not_called()
    assert [c.args[0].rsplit("/", 1)[-1] for c in session.post.call_args_list] == ["plan-chunks", "upload-complete"]
    describe.assert_called_once_with(str(media), "video", "You analyze a video segment.", {"type": "object"})
    sent = session.post.call_args_list[1].kwargs["json"]["chunks"]
    assert sent[0]["description"] == {"short_summary": "a dog"} and not sent[0].get("gemini_file_uri")


def test_local_is_the_default_and_gemini_credits_is_the_other_choice():
    rows = json.loads((_REPO / "src" / "settings" / "_default.settings").read_text(encoding="utf-8"))
    row = next(r for r in rows if r.get("setting") == "index-describe")
    assert row["value"] == "local" and {v["value"] for v in row["values"]} == {"local", "gemini"}


def test_the_media_bin_says_what_local_indexing_is_doing():
    from classes.indexing_status import phase_label

    assert "download" in phase_label("installing").lower()
    assert phase_label("describing", 40) == "Describing on this computer… (40%)"
