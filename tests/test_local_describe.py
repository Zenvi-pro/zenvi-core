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
        seen["prompt"] = argv[argv.index("-p") + 1]
        seen["env"] = kwargs.get("env") or {}
        # As the real CLI does: the prompt (schema braces and all) is echoed before the answer.
        return SimpleNamespace(returncode=0, stdout="> " + seen["prompt"] + "\n\n" + json.dumps(answer) + "\n",
                               stderr="log")

    monkeypatch.setattr(ld.subprocess, "run", run)
    clip = tmp_path / "chunk.mp4"
    clip.write_bytes(b"x")
    schema = {"type": "object", "properties": {"short_summary": {"type": "string"}}}
    assert ld.describe(str(clip), "video", "You analyze a video segment.", schema, duration=20.0) == answer
    argv = seen["argv"]
    assert argv[:2] == [ld.llama_program(), "cli"]
    assert "-st" in argv and "--json-schema-file" not in argv   # its grammar breaks this model's chat template
    assert argv[argv.index("--video") + 1] == str(clip)
    assert argv[argv.index("-m") + 1].endswith(ld.MODEL[0]) and argv[argv.index("-mm") + 1].endswith(ld.PROJECTOR[0])
    assert "You analyze a video segment." in " ".join(argv) and argv[argv.index("--temp") + 1] == "0"
    assert "20.0 seconds long" in seen["prompt"]          # without it every time landed in the first 8 s
    assert json.dumps(schema) in seen["prompt"]
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


def _index_locally(tmp_path, transcript):
    """Index a 9 s clip in two chunks on this computer; returns (result, mocks, chunks sent to the backend)."""
    from classes.api_client import ZenviBackendClient

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video-bytes")
    client = ZenviBackendClient.__new__(ZenviBackendClient)
    client.api_url = "http://backend.test/api/v1"
    client._ssl_verify = True
    session = MagicMock()
    plan = MagicMock(json=MagicMock(return_value={
        "chunks": [{"chunk_index": 0, "start": 0.0, "end": 5.0}, {"chunk_index": 1, "start": 4.0, "end": 9.0}],
        "index_max_height": 720,
        "describe_prompt": "You analyze a video segment.", "describe_schema": {"type": "object"}}))
    complete = MagicMock(json=MagicMock(return_value={"success": True, "job_id": "job-1"}))
    session.post.side_effect = [plan, complete]
    chunks = [{"chunk_index": i, "path": str(media), "size": 11, "start": s, "end": e, "mime_type": "video/mp4"}
              for i, (s, e) in enumerate(((0.0, 5.0), (4.0, 9.0)))]

    with patch("classes.index_chunker.extract_chunks", return_value=(chunks, str(tmp_path / "w"), "")) as extract, \
         patch("classes.index_chunker.cleanup_chunk_dir"), \
         patch("classes.gemini_direct_upload.upload_file_to_gemini_resumable") as gemini_upload, \
         patch("classes.speech.asr.transcribe_file", **transcript) as transcribe, \
         patch("classes.local_describe.ready", return_value=False), \
         patch("classes.local_describe.install") as install, \
         patch("classes.local_describe.describe", side_effect=lambda *a, **k: {"short_summary": "a dog"}) as describe, \
         patch.object(client, "_poll_indexing_job", return_value={"success": True, "video_id": "f1"}):
        result = client.start_direct_indexing_job(
            str(media), "zenvi-p1", file_id="f1", filename="clip.mp4", session=session, project_id="p1",
            duration_sec=9.0, media_type="video")      # no flag: the setting decides, and local is the default
    mocks = SimpleNamespace(extract=extract, gemini_upload=gemini_upload, install=install, describe=describe,
                            transcribe=transcribe, posts=[c.args[0].rsplit("/", 1)[-1] for c in session.post.call_args_list])
    return result, mocks, session.post.call_args_list[-1].kwargs["json"]["chunks"]


def test_local_indexing_never_uploads_to_gemini_and_takes_speech_from_the_recogniser(tmp_path):
    from classes.speech.cache import TranscriptRecord, Word

    words = [Word("Hello", 1.0, 1.5), Word("world.", 1.6, 2.0), Word("Bye", 6.0, 6.5)]
    record = TranscriptRecord(path="clip.mp4", size=11, mtimeNs=0, modelId="m", language="en", words=words)
    result, m, sent = _index_locally(tmp_path, {"return_value": record})

    assert result.get("success") is True
    m.install.assert_called_once()
    m.gemini_upload.assert_not_called()
    assert m.posts == ["plan-chunks", "upload-complete"]
    assert m.extract.call_args.kwargs["max_height"] == 360          # a quarter of the image tokens of 720p
    assert m.transcribe.call_count == 1                             # once per file, not per chunk
    assert m.describe.call_args_list[0].args[2:] == ("You analyze a video segment.", {"type": "object"})
    assert [c.kwargs["duration"] for c in m.describe.call_args_list] == [5.0, 5.0]
    assert [c["description"]["short_summary"] for c in sent] == ["a dog", "a dog"]
    assert not any(c.get("gemini_file_uri") for c in sent)
    # each chunk's own words, timed from the chunk's start
    assert sent[0]["description"]["transcript"] == "Hello world."
    assert sent[0]["description"]["transcript_cues"] == [{"start": 1.0, "end": 2.0, "text": "Hello world."}]
    assert sent[1]["description"]["transcript_cues"] == [{"start": 2.0, "end": 2.5, "text": "Bye"}]


def test_a_clip_whose_speech_cannot_be_recognised_is_still_indexed(tmp_path):
    result, _m, sent = _index_locally(tmp_path, {"side_effect": RuntimeError("no speech engine")})
    assert result.get("success") is True
    assert sent[0]["description"]["transcript_cues"] == [] and sent[0]["description"]["transcript"] == ""


def test_the_model_is_not_asked_to_transcribe(home, monkeypatch, tmp_path):
    _fake_downloads(monkeypatch)
    ld.install()
    seen = {}
    monkeypatch.setattr(ld.subprocess, "run", lambda argv, **k: seen.update(prompt=argv[argv.index("-p") + 1])
                        or SimpleNamespace(returncode=0, stdout='{"visual": "x"}', stderr=""))
    clip = tmp_path / "chunk.mp4"
    clip.write_bytes(b"x")
    schema = {"type": "object", "required": ["visual", "transcript_cues"],
              "properties": {"visual": {"type": "string"}, "transcript": {"type": "string"},
                             "transcript_en": {"type": "string"}, "transcript_cues": {"type": "array"}}}
    ld.describe(str(clip), "video", "p", schema)
    asked = json.loads(seen["prompt"].rsplit("\n", 1)[-1])
    assert asked == {"type": "object", "required": ["visual"], "properties": {"visual": {"type": "string"}}}


def test_local_is_the_default_and_gemini_credits_is_the_other_choice():
    rows = json.loads((_REPO / "src" / "settings" / "_default.settings").read_text(encoding="utf-8"))
    row = next(r for r in rows if r.get("setting") == "index-describe")
    assert row["value"] == "local" and {v["value"] for v in row["values"]} == {"local", "gemini"}


def test_the_media_bin_says_what_local_indexing_is_doing():
    from classes.indexing_status import phase_label

    assert "download" in phase_label("installing").lower()
    assert phase_label("describing", 40) == "Describing on this computer… (40%)"


def test_two_first_imports_install_once_and_never_into_a_half_made_folder(home, monkeypatch):
    import threading

    asked = _fake_downloads(monkeypatch)
    threads = [threading.Thread(target=ld.install) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ld.ready() and sum(1 for a in asked if a[1] == ld._llama_download()[1]) == 1
    assert not os.path.exists(os.path.join(ld.home(), "llama.partial"))
