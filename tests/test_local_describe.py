"""Shot descriptions on this computer: pinned on-demand install, one llama.cpp call per clip, no backend."""

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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from classes import credits_client as cc  # noqa: E402
from classes import hyperframes_local as hf  # noqa: E402
from classes import local_describe as ld  # noqa: E402
from classes.media_index import cloud  # noqa: E402
from classes.media_index import embedder as E  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from test_indexing_job_reuse import Harness, _stub_vectors  # noqa: E402

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
    assert not [f for _r, _d, files in os.walk(ld.home()) for f in files
                if f.endswith((".zip", ".tar.gz"))]                     # the archive is not kept, in any folder
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


# ---- shot descriptions on this computer (the cloud pass's layer, no backend) -----------------
SHA = "e" * 64
SHORT = [{"id": 0, "start": 0.0, "end": 4.0}, {"id": 1, "start": 4.0, "end": 8.0}, {"id": 2, "start": 8.0, "end": 9.0}]
LONG = [{"id": 0, "start": 0.0, "end": 30.0}, {"id": 1, "start": 30.0, "end": 60.0}, {"id": 2, "start": 60.0, "end": 90.0}]
PROBE = {"video": {"height": 720}, "duration": 90.0}


@pytest.fixture
def clips(monkeypatch):
    """Clips are cut without ffmpeg: each one remembers the span it came from and the height asked for."""
    cut = {}

    def proxy(path, probe, out, start=None, end=None, max_height=None):
        cut[out] = {"start": float(start or 0.0), "end": None if end is None else float(end), "height": max_height}
        Path(out).write_bytes(b"clip")
        return True, ""

    monkeypatch.setattr(cloud, "make_proxy", proxy)
    return cut


def _model(monkeypatch, clips, shots, fail_on=(), events=None):
    """Stand in for LFM3-Omni: answers for the shots inside the clip it is shown, and fails when one of them is in fail_on."""
    calls = []

    def describe(clip, media_type, prompt, schema, duration=0.0):
        span = clips[clip]
        inside = [s for s in shots if span["start"] <= s["start"] and s["end"] <= span["end"]]
        calls.append({"prompt": prompt, "schema": schema, "duration": duration, "media_type": media_type})
        if events is not None:
            events.append("describe")
        if any(s["id"] in fail_on for s in inside):
            raise hf.HyperframesError("the local model gave no description")
        return {"shots": [{"id": s["id"], "description": "A dog on shot %d." % s["id"], "objects": ["dog"],
                           "sound_events": [{"label": "bark"}], "interest": 0.4, "usable": True} for s in inside]}

    monkeypatch.setattr(ld, "describe", describe)
    monkeypatch.setattr(ld, "ready", lambda: True)
    return SimpleNamespace(calls=calls)


def _shelf(tmp_path, shots):
    shelf = Shelf(str(tmp_path / "shelf"))
    shelf.write_json(SHA, "structure.json", {"shots": [{**s, "motion": {}} for s in shots]})
    return shelf


def test_local_shots_share_one_clip_at_360p_and_need_no_backend(tmp_path, monkeypatch, clips):
    model = _model(monkeypatch, clips, SHORT)
    client, uploader = MagicMock(), MagicMock()
    out = cloud.run_watch(client, "/m/clip.mp4", PROBE, "F1", {"shots": SHORT}, None, uploader=uploader)

    assert not uploader.called and not client.v2_upload_session.called and not client.v2_understand.called
    assert list(clips.values()) == [{"start": 0.0, "end": 9.0, "height": 360}]      # three short shots, one model run
    call = model.calls[0]
    assert call["duration"] == 9.0 and call["media_type"] == "video"
    assert set(call["schema"]["properties"]["shots"]["items"]["properties"]) >= {"description", "interest", "usable", "hook_potential"}
    first = out["shots"][0]
    assert (first["id"], first["start"], first["end"]) == (0, 0.0, 4.0) and first["interest"] == 0.4 and first["usable"] is True
    assert cloud.shot_document(first) == "A dog on shot 0. Seen: dog. Sounds: bark."


def test_a_shot_whose_clip_fails_is_missing_and_the_rest_are_kept(tmp_path, monkeypatch, clips):
    _model(monkeypatch, clips, LONG, fail_on={1})         # each shot is longer than a clip, so each one is described alone
    shelf = _shelf(tmp_path, LONG)
    out = cloud.compute_cloud(MagicMock(), "/m/clip.mp4", PROBE, SHA, shelf, file_id="F1", uploader=MagicMock())
    assert "error" not in out and out["layers"] == {"watch": "ready"}
    assert [s["id"] for s in shelf.read_json(SHA, "watch.json")["shots"]] == [0, 2]      # shot 1 is asked for again next time


def test_shots_separated_by_a_gap_are_cut_as_separate_clips(tmp_path, monkeypatch, clips):
    gapped = [{"id": 0, "start": 0.0, "end": 4.0}, {"id": 1, "start": 10.0, "end": 14.0}]     # the footage between is not theirs
    _model(monkeypatch, clips, gapped)
    cloud.run_watch(MagicMock(), "/m/clip.mp4", PROBE, "F1", {"shots": gapped}, None, uploader=MagicMock())
    assert sorted((c["start"], c["end"]) for c in clips.values()) == [(0.0, 4.0), (10.0, 14.0)]


def test_cancelling_stops_between_clips(tmp_path, monkeypatch, clips):
    model = _model(monkeypatch, clips, LONG)
    with pytest.raises(cloud.Cancelled):
        cloud.run_watch(MagicMock(), "/m/clip.mp4", PROBE, "F1", {"shots": LONG}, None, uploader=MagicMock(),
                        should_cancel=lambda: bool(model.calls))
    assert len(model.calls) == 1


def test_the_model_is_installed_before_the_first_clip_and_a_failed_install_is_an_error(tmp_path, monkeypatch, clips):
    events = []
    model = _model(monkeypatch, clips, SHORT, events=events)
    monkeypatch.setattr(ld, "install", lambda progress=None: events.append("install"))
    out = cloud.run_watch(MagicMock(), "/m/clip.mp4", PROBE, "F1", {"shots": SHORT}, None, uploader=MagicMock())
    assert events == ["install", "describe"] and "error" not in out

    def offline(progress=None):
        raise OSError("no network")
    monkeypatch.setattr(ld, "install", offline)
    out = cloud.run_watch(MagicMock(), "/m/clip.mp4", PROBE, "F1", {"shots": SHORT}, None, uploader=MagicMock())
    assert "no network" in out["error"] and len(model.calls) == 1


def test_the_gemini_choice_still_describes_in_the_cloud(monkeypatch, clips):
    monkeypatch.setattr(ld, "enabled", lambda: False)
    model = _model(monkeypatch, clips, SHORT)
    client = MagicMock()
    client.v2_upload_session.return_value = {"upload_url": "https://u/x"}
    client.v2_understand.return_value = {"job_id": "j1"}
    client.v2_job.return_value = {"status": "done", "result": {
        "shots": [{"id": 0, "start": 0.0, "end": 4.0, "description": "A dog."}], "usage": {"model": "m"}}}
    out = cloud.run_watch(client, "/m/clip.mp4", PROBE, "F1", {"shots": SHORT}, None,
                          uploader=lambda path, url, mime_type="video/mp4": ({"name": "files/a", "uri": "u"}, ""))
    assert client.v2_understand.called and model.calls == [] and out["shots"][0]["description"] == "A dog."


def test_describing_on_import_runs_on_this_computer_without_sign_in_or_credits(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    _stub_vectors(monkeypatch, h)
    monkeypatch.setattr(E, "describe_on_import", lambda: True)
    monkeypatch.setattr(ld, "ready", lambda: False)
    described = []
    monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: described.append(a) or {"layers": {}})
    charge = MagicMock(return_value=(True, 1000, None))
    monkeypatch.setattr(cc, "check_operation", charge)
    _meta, progress = h.run()
    assert len(described) == 1 and ("installing", 0) in progress
    charge.assert_not_called()


def test_gemini_describing_on_import_still_needs_sign_in(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, signed_in=False)
    _stub_vectors(monkeypatch, h)
    monkeypatch.setattr(E, "describe_on_import", lambda: True)
    monkeypatch.setattr(ld, "enabled", lambda: False)
    described = []
    monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: described.append(a) or {"layers": {}})
    h.run()
    assert described == []


def test_the_describe_tool_on_this_computer_needs_no_sign_in_or_credits(monkeypatch):
    from classes.editor_tools import media_index_tools as T
    from classes.media_index.job import IndexingJob
    f = SimpleNamespace(id="F1", data={"path": "/m/clip.mp4", "media_type": "video", "duration": 9.0})
    fi = SimpleNamespace(sha=SHA, shots=SHORT, vector_model=None)
    described = []
    charge = MagicMock(return_value=(True, 1000, None))
    monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: False))
    monkeypatch.setattr(cc, "check_operation", charge)
    monkeypatch.setattr("classes.api_client.get_backend_client", lambda: "client")
    monkeypatch.setattr("classes.media_index.probe.probe_media", lambda p: {"duration": 9.0})
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p)
    monkeypatch.setattr(E, "get_embedder", lambda model: "embedder")
    monkeypatch.setattr(T, "default_shelf", lambda: "shelf")
    monkeypatch.setattr(cloud, "describe_shots", lambda *a, **k: described.append(a) or {"described": [0], "shots": [], "usage": {}})

    out, why = T.describe_file_shots(f, fi, [0])
    assert why is None and out["described"] == [0] and not charge.called
    monkeypatch.setattr(ld, "enabled", lambda: False)
    out, why = T.describe_file_shots(f, fi, [1])
    assert out is None and "sign in" in why and len(described) == 1


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


def test_speech_is_heard_by_the_captions_recogniser_installed_on_demand(home, monkeypatch, tmp_path):
    installed = []
    monkeypatch.setattr(hf, "speech_supported", lambda *a: True)
    monkeypatch.setattr(hf, "speech_ready", lambda fresh=False: bool(installed))
    monkeypatch.setattr(hf, "install_speech", lambda: installed.append(1))
    monkeypatch.setattr(hf, "transcribe", lambda wav: [{"text": "Hi", "start": 0.2, "end": 0.4}])
    monkeypatch.setattr(ld, "run_ffmpeg", lambda argv, **k: SimpleNamespace(returncode=0), raising=False)
    assert ld.speech_words(str(tmp_path / "clip.mp4")) == [{"text": "Hi", "start": 0.2, "end": 0.4}]
    assert installed == [1]                                     # Whistle (18 MB) fetched on first use


def test_without_whistle_whisper_hears_it_and_a_failure_means_no_transcript(home, monkeypatch, tmp_path):
    from classes.speech.cache import TranscriptRecord, Word

    monkeypatch.setattr(hf, "speech_supported", lambda *a: False)       # Intel Macs
    record = TranscriptRecord(path="c", size=1, mtimeNs=0, modelId="m", language="en", words=[Word("Yo", 1.0, 1.2)])
    with patch("classes.speech.asr.transcribe_file", return_value=record):
        assert ld.speech_words(str(tmp_path / "clip.mp4")) == [{"text": "Yo", "start": 1.0, "end": 1.2}]
    with patch("classes.speech.asr.transcribe_file", side_effect=RuntimeError("no engine")):
        assert ld.speech_words(str(tmp_path / "clip.mp4")) == []


def test_a_one_shot_clip_answered_without_the_shots_list_is_still_described():
    from classes.media_index import cloud

    bare = {"description": "A dog runs on the beach.", "mood": "playful"}
    assert cloud._local_answers(bare, [{"id": 4}]) == {4: bare}
    assert cloud._local_answers(bare, [{"id": 4}, {"id": 5}]) == {}       # which shot it meant is unknown
    assert cloud._local_answers({"shots": [{"id": 5, "description": "x"}]}, [{"id": 4}, {"id": 5}]) == {5: {"id": 5, "description": "x"}}
