"""Shot descriptions on this computer: pinned on-demand install, one llama.cpp call per clip, no backend."""

import ast
import io
import json
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from classes import credits_client as cc  # noqa: E402
from classes import hyperframes_local as hf  # noqa: E402
from classes import local_describe as ld  # noqa: E402
from classes.handoff import node_runtime  # noqa: E402
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


class _Llama:
    """Stand in for llama.cpp at subprocess.Popen: *answer(argv, **kwargs)* gives (exit code, stdout), or raises."""

    def __init__(self, monkeypatch, answer):
        self.answer, self.started, self.running, self.most = answer, [], 0, 0
        self.killed = threading.Event()
        self._lock = threading.Lock()
        monkeypatch.setattr(subprocess, "Popen", self._popen)
        monkeypatch.setattr(node_runtime, "kill_process_tree", lambda proc: proc.kill())

    def _popen(self, argv, **kwargs):
        llama = self

        class Proc:
            args, pid, returncode = argv, 0, None

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def communicate(self, input=None, timeout=None):
                if self.returncode is not None:
                    return "", ""
                with llama._lock:
                    llama.running += 1
                    llama.most = max(llama.most, llama.running)
                try:
                    self.returncode, out = llama.answer(argv, timeout=timeout, **kwargs)
                finally:
                    with llama._lock:
                        llama.running -= 1
                kwargs["stdout"].write(out.replace("\n", "\r\n").encode())     # as it comes on Windows

            def kill(self):
                self.returncode = 1
                llama.killed.set()

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        self.started.append(argv)
        return Proc()


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
        return 0, "> " + seen["prompt"] + "\n\n" + json.dumps(answer) + "\n"

    _Llama(monkeypatch, run)
    clip = tmp_path / "chunk.mp4"
    clip.write_bytes(b"x")
    schema = {"type": "object", "properties": {"short_summary": {"type": "string"}}}
    assert ld.describe(str(clip), "video", "You analyze a video segment.", schema, duration=20.0) == answer
    argv = seen["argv"]
    assert argv[:2] == [ld.llama_program(), "cli"]
    assert "-st" in argv and "--json-schema-file" not in argv   # its grammar breaks this model's chat template
    # RC v1.3.0: with a controlling terminal (run.sh) llama.cpp wrote the answer there, not to our pipe.
    assert "--simple-io" in argv
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
    _Llama(monkeypatch, lambda argv, **k: (1, ""))
    with pytest.raises(hf.HyperframesError):
        ld.describe(str(clip), "video", "p", {})
    _Llama(monkeypatch, lambda argv, **k: (0, "sorry"))
    with pytest.raises(hf.HyperframesError):
        ld.describe(str(clip), "video", "p", {})


def test_the_model_is_not_asked_to_transcribe(home, monkeypatch, tmp_path):
    _fake_downloads(monkeypatch)
    ld.install()
    seen = {}
    _Llama(monkeypatch, lambda argv, **k: seen.update(prompt=argv[argv.index("-p") + 1]) or (0, '{"visual": "x"}'))
    clip = tmp_path / "chunk.mp4"
    clip.write_bytes(b"x")
    schema = {"type": "object", "required": ["visual", "transcript_cues"],
              "properties": {"visual": {"type": "string"}, "transcript": {"type": "string"},
                             "transcript_en": {"type": "string"}, "transcript_cues": {"type": "array"}}}
    ld.describe(str(clip), "video", "p", schema)
    asked = json.loads(seen["prompt"].rsplit("\n", 1)[-1])
    assert asked == {"type": "object", "required": ["visual"], "properties": {"visual": {"type": "string"}}}


# ---- one model at a time (about 5 GB each: three at once filled a 16 GB computer) ---------------
@pytest.fixture
def engine(home, monkeypatch, tmp_path):
    _fake_downloads(monkeypatch)
    ld.install()
    clip = tmp_path / "chunk.mp4"
    clip.write_bytes(b"x")
    return str(clip)


def _at_once(*work):
    """Run every callable in its own thread; returns their results, an exception standing for its own."""
    out = [None] * len(work)

    def run(i):
        try:
            out[i] = work[i]()
        except Exception as exc:
            out[i] = exc

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(work))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not any(t.is_alive() for t in threads), "a describe never got its turn"
    return out


def test_concurrent_describes_never_run_two_models_at_once(engine, monkeypatch):
    llama = _Llama(monkeypatch, lambda argv, **k: time.sleep(0.3) or (0, '{"visual": "x"}'))
    found = _at_once(*[lambda: ld.describe(engine, "video", "p", {})] * 3)
    assert found == [{"visual": "x"}] * 3
    assert len(llama.started) == 3 and llama.most == 1


def _exits(argv, **k):
    return 1, ""


def _times_out(argv, timeout=None, **k):
    raise subprocess.TimeoutExpired(argv, timeout)


def _cannot_start(argv, **k):
    raise OSError("no such program")


@pytest.mark.parametrize("failure", [_exits, _times_out, _cannot_start])
def test_a_failed_describe_gives_the_model_to_the_next_one(engine, monkeypatch, failure):
    llama = _Llama(monkeypatch, failure)
    if failure is _cannot_start:
        monkeypatch.setattr(subprocess, "Popen", failure)
    failed = _at_once(lambda: ld.describe(engine, "video", "p", {}))[0]
    assert isinstance(failed, hf.HyperframesError) and not isinstance(failed, ld.Busy)
    assert llama.killed.is_set() == (failure is _times_out)          # a run that is too long is stopped, not left behind
    _Llama(monkeypatch, lambda argv, **k: (0, '{"visual": "x"}'))
    assert _at_once(lambda: ld.describe(engine, "video", "p", {})) == [{"visual": "x"}]


def test_a_run_that_is_too_long_takes_what_it_started_down_with_it(monkeypatch, tmp_path):
    """Real processes: an ffmpeg that hangs holds llama.cpp's output open, and the next describe would never get its turn."""
    monkeypatch.setattr(ld, "DESCRIBE_TIMEOUT", 3)
    child = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"
    begun = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        ld._run([sys.executable, "-c", child], str(tmp_path))
    assert time.monotonic() - begun < 30


def test_a_run_ends_when_the_model_does_even_if_something_it_started_lives_on(tmp_path):
    child = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)']); "
             "print('caf\\u00e9'.encode()); sys.stderr.write('log')")
    begun = time.monotonic()
    code, out, err = ld._run([sys.executable, "-c", child], str(tmp_path))
    assert time.monotonic() - begun < 20 and (code, err) == (0, "log") and "caf" in out


def test_a_waiting_describe_can_give_up_and_never_starts_the_model(engine, monkeypatch):
    started, finish = threading.Event(), threading.Event()
    llama = _Llama(monkeypatch, lambda argv, **k: started.set() or finish.wait(15) and (0, '{"visual": "x"}'))

    def second():
        started.wait(10)
        try:
            ld.describe(engine, "video", "p", {}, give_up=lambda: True)
        finally:
            finish.set()

    first, gave_up = _at_once(lambda: ld.describe(engine, "video", "p", {}), second)
    assert first == {"visual": "x"} and isinstance(gave_up, ld.Busy) and len(llama.started) == 1


def test_quitting_stops_the_running_model_and_the_ones_waiting(engine, monkeypatch):
    monkeypatch.setattr(ld, "_stopping", False)
    started = threading.Event()

    def answer(argv, **k):
        started.set()
        return (1, "") if llama.killed.wait(15) else (0, '{"visual": "x"}')

    llama = _Llama(monkeypatch, answer)

    def quit_editor():
        started.wait(10)
        ld.stop()

    first, second, _ = _at_once(lambda: ld.describe(engine, "video", "p", {}),
                                lambda: started.wait(10) and ld.describe(engine, "video", "p", {}), quit_editor)
    assert isinstance(first, hf.HyperframesError) and isinstance(second, ld.Busy)
    assert len(llama.started) == 1 and llama.killed.is_set()
    with pytest.raises(ld.Busy):                     # nothing new starts while the editor is closing
        ld.describe(engine, "video", "p", {})
    assert len(llama.started) == 1


def test_the_editor_quitting_stops_the_model(monkeypatch):
    """The real FilesModel method, compiled without importing Qt."""
    tree = ast.parse((_REPO / "src" / "windows" / "models" / "files_model.py").read_text(encoding="utf-8"))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_stop_active_indexers")
    ns = {}
    exec(compile(ast.Module([method], []), "files_model.py", "exec"), ns)
    events = []
    monkeypatch.setattr(ld, "stop", lambda: events.append("stop"))
    worker = SimpleNamespace(interrupt=lambda: events.append("interrupt"), quit=lambda: None,
                             wait=lambda ms: events.append("wait"))
    ns["_stop_active_indexers"](SimpleNamespace(_indexing_queue=[1], _active_indexers=[worker]))
    assert events == ["stop", "interrupt", "wait"]   # stopped before the wait, or a worker cannot finish in time


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

    def describe(clip, media_type, prompt, schema, duration=0.0, give_up=None):
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


def _busy_after(monkeypatch, model, n):
    """The model is in use by someone else from the n-th clip on: a describe waits until it gives up."""
    mine = ld.describe

    def describe(*args, give_up=None, **kwargs):
        if len(model.calls) < n:
            return mine(*args, **kwargs)
        if not (give_up and give_up()):
            pytest.fail("it would wait for ever")        # not an Exception, so a failing clip cannot swallow it
        raise ld.Busy("the local model is busy")

    monkeypatch.setattr(ld, "describe", describe)


def test_a_tool_call_stops_queueing_for_the_model_and_keeps_what_it_has(tmp_path, monkeypatch, clips):
    monkeypatch.setattr("classes.media_index.vectors.add_descriptions", lambda *a, **k: None)
    shelf = _shelf(tmp_path, LONG)
    model = _model(monkeypatch, clips, LONG)
    _busy_after(monkeypatch, model, 1)
    out = cloud.describe_shots(MagicMock(), "/m/clip.mp4", PROBE, SHA, shelf, [0, 1, 2], None, give_up=lambda: True)
    assert out["described"] == [0] and len(clips) == 2           # it did not go on to cut a third clip
    out = cloud.describe_shots(MagicMock(), "/m/clip.mp4", PROBE, SHA, shelf, [1], None, give_up=lambda: True)
    assert "busy" in out["error"]


def test_giving_up_before_a_later_run_still_returns_and_indexes_the_earlier_one(tmp_path, monkeypatch, clips):
    indexed = []
    monkeypatch.setattr("classes.media_index.vectors.add_descriptions", lambda sh, sha, emb, shots: indexed.extend(shots))
    far = [{"id": 0, "start": 0.0, "end": 4.0}, {"id": 1, "start": 80.0, "end": 84.0}]      # too far apart for one run
    model = _model(monkeypatch, clips, far)
    _busy_after(monkeypatch, model, 1)
    out = cloud.describe_shots(MagicMock(), "/m/clip.mp4", PROBE, SHA, _shelf(tmp_path, far), [0, 1], None, give_up=lambda: True)
    assert out["described"] == [0] and [s["id"] for s in indexed] == [0]


def test_giving_up_part_way_through_a_run_does_not_start_the_next_one(tmp_path, monkeypatch, clips):
    monkeypatch.setattr("classes.media_index.vectors.add_descriptions", lambda *a, **k: None)
    far = LONG + [{"id": 3, "start": 180.0, "end": 184.0}]
    model = _model(monkeypatch, clips, far)
    _busy_after(monkeypatch, model, 1)
    out = cloud.describe_shots(MagicMock(), "/m/clip.mp4", PROBE, SHA, _shelf(tmp_path, far), [0, 1, 2, 3], None, give_up=lambda: True)
    assert out["described"] == [0] and len(clips) == 2


def test_a_tool_call_does_not_hang_behind_the_same_file_being_described(tmp_path, monkeypatch, clips):
    from classes.media_index.job import single_flight
    model = _model(monkeypatch, clips, SHORT)
    shelf = _shelf(tmp_path, SHORT)
    with single_flight(SHA):
        out = _at_once(lambda: cloud.describe_shots(MagicMock(), "/m/clip.mp4", PROBE, SHA, shelf, [0], None,
                                                    give_up=lambda: True))[0]
    assert out["error"] and not model.calls


def test_cancelling_a_job_that_waits_for_the_model_stops_it(tmp_path, monkeypatch, clips):
    model = _model(monkeypatch, clips, LONG)
    _busy_after(monkeypatch, model, 1)
    with pytest.raises(cloud.Cancelled):             # cancelled once the second clip is cut and waiting
        cloud.run_watch(MagicMock(), "/m/clip.mp4", PROBE, "F1", {"shots": LONG}, None, uploader=MagicMock(),
                        should_cancel=lambda: len(clips) > 1)
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
    monkeypatch.setattr(cloud, "describe_shots", lambda *a, **k: described.append(k) or {"described": [0], "shots": [], "usage": {}})

    out, why = T.describe_file_shots(f, fi, [0])
    assert why is None and out["described"] == [0] and not charge.called
    give_up, now = described[0]["give_up"], time.monotonic()       # a tool call queues for the model only so long
    assert not give_up()
    monkeypatch.setattr(time, "monotonic", lambda: now + T.DESCRIBE_QUEUE_SECONDS + 1)
    assert give_up()
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
    asked = _fake_downloads(monkeypatch)
    threads = [threading.Thread(target=ld.install) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ld.ready() and sum(1 for a in asked if a[1] == ld._llama_download()[1]) == 1
    assert not os.path.exists(os.path.join(ld.home(), "llama.partial"))


def test_a_one_shot_clip_answered_without_the_shots_list_is_still_described():
    from classes.media_index import cloud

    bare = {"description": "A dog runs on the beach.", "mood": "playful"}
    assert cloud._local_answers(bare, [{"id": 4}]) == {4: bare}
    assert cloud._local_answers(bare, [{"id": 4}, {"id": 5}]) == {}       # which shot it meant is unknown
    assert cloud._local_answers({"shots": [{"id": 5, "description": "x"}]}, [{"id": 4}, {"id": 5}]) == {5: {"id": 5, "description": "x"}}


def test_sound_times_in_a_shared_clip_are_moved_onto_each_shot():
    from classes.media_index import cloud

    # Shots 0-4 s and 4-8 s share one clip; the model times sounds from the clip's start.
    raw = {"description": "A dog barks.", "sound_events": [{"label": "bark", "start": 5.0, "end": 5.5}]}
    shot = cloud._local_shot(raw, {"id": 1, "start": 4.0, "end": 8.0}, clip_start=0.0)
    assert shot["sound_events"] == [{"label": "bark", "start": 1.0, "end": 1.5}]
    assert "from the start of the segment" in cloud.LOCAL_PROMPT

