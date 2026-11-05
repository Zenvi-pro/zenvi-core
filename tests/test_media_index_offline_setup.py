"""First-run setup of the local search model without a network: the file waits for the network, it does not fail.

Regression for #375 (local indexer #367). With a recent Node already on the computer, the setup's ``npm ci`` failed
offline after minutes of retries as a plain RuntimeError: the file was marked failed, npm's log was the reason, and it
was never tried again when the network came back. The model download inside the Node worker failed the same way.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import info  # noqa: E402
from classes.api_client import ZenviBackendClient  # noqa: E402
from classes.indexing_status import FAILED, PENDING, WAITING_FOR_NETWORK, derive_indexing_status  # noqa: E402
from classes.media_fingerprint import fingerprint  # noqa: E402
from classes.media_index import Shelf  # noqa: E402
from classes.media_index import embed_runtime as R  # noqa: E402
from classes.media_index import embedder as E  # noqa: E402
from classes.media_index.job import IndexingJob  # noqa: E402

NPM_OFFLINE_LOG = textwrap.dedent("""\
    (node:4242) MaxListenersExceededWarning: Possible EventEmitter memory leak detected. 11 abort listeners added to [AbortSignal].
    npm error code E502
    npm error 502 Bad Gateway - GET https://registry.npmjs.org/@huggingface%2ftransformers/-/transformers-4.3.1.tgz
    npm error A complete log of this run can be found in: /Users/someone/.npm/_logs/2026-11-04T10_00_00_000Z-debug-0.log
""")
NPM_DISK_FULL_LOG = textwrap.dedent("""\
    npm error code ENOSPC
    npm error syscall write
    npm error errno -28
    npm error nospc ENOSPC: no space left on device, write
    npm error nospc There appears to be insufficient space on your system to finish.
    npm error A complete log of this run can be found in: /Users/someone/.npm/_logs/2026-11-04T10_00_00_000Z-debug-0.log
""")


class Net:
    """urllib as the setup sees it: the answers to successive requests, True for a reply and False for no network."""

    def __init__(self, monkeypatch, *answers):
        self.answers, self.urls = list(answers), []
        monkeypatch.setattr(R.urllib.request, "urlopen", self.urlopen)

    def urlopen(self, request, timeout=None):
        self.urls.append(request.full_url)
        up = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if not up:
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        return MagicMock()


@pytest.fixture
def system_node(monkeypatch, tmp_path):
    """A recent Node on PATH (Homebrew), nothing installed yet in a fresh profile; npm's runs are recorded."""
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "profile"))
    monkeypatch.setattr(R, "find_node", lambda: "/opt/homebrew/bin/node")
    monkeypatch.setattr(R, "_npm", lambda node: ["/opt/homebrew/bin/npm"])
    monkeypatch.setattr(E, "_shared", {})
    runs = {"argv": [], "ci": (0, "")}

    def run(argv, **kw):
        runs["argv"].append(list(argv))
        if argv[1:4] == ["config", "get", "registry"]:
            return subprocess.CompletedProcess(argv, 0, "https://registry.npmjs.org/\n", "")
        if argv[1] == "ci":
            code, err = runs["ci"]
            return subprocess.CompletedProcess(argv, code, "", err)
        raise AssertionError(f"unexpected command {argv}")

    monkeypatch.setattr(R.subprocess, "run", run)
    return runs


def _ran_npm_ci(runs):
    return any(argv[1] == "ci" for argv in runs["argv"])


# -- npm ci, with a system Node -------------------------------------------------------------------------
def test_offline_with_a_system_node_waits_for_the_network_and_does_not_run_npm(system_node, monkeypatch):
    system_node["ci"] = (1, NPM_OFFLINE_LOG)
    net = Net(monkeypatch, False)
    with pytest.raises(E.EmbedError) as caught:
        E.LocalEmbedder().start()
    assert caught.value.flags.get("offline") is True
    assert "npm error" not in str(caught.value) and "EventEmitter" not in str(caught.value)
    assert not _ran_npm_ci(system_node), "offline, npm must not spend minutes retrying"
    assert net.urls == ["https://registry.npmjs.org/"]


def test_the_network_going_away_while_npm_works_is_also_a_wait(system_node, monkeypatch):
    system_node["ci"] = (1, NPM_OFFLINE_LOG)
    Net(monkeypatch, True, False)          # up when npm starts, gone when it fails
    with pytest.raises(E.EmbedError) as caught:
        E.LocalEmbedder().start()
    assert caught.value.flags.get("offline") is True and _ran_npm_ci(system_node)


def test_a_real_npm_failure_is_a_failure_with_npms_one_line_reason(system_node, monkeypatch):
    system_node["ci"] = (1, NPM_DISK_FULL_LOG)
    Net(monkeypatch, True)
    with pytest.raises(E.EmbedError) as caught:
        E.LocalEmbedder().start()
    message = str(caught.value)
    assert not caught.value.flags.get("offline")
    assert "ENOSPC: no space left on device" in message
    assert "complete log" not in message and "npm error" not in message and len(message) < 260


def test_npm_is_asked_for_its_registry_so_a_mirror_is_what_gets_checked(system_node, monkeypatch):
    def run(argv, **kw):
        system_node["argv"].append(list(argv))
        if argv[1:4] == ["config", "get", "registry"]:
            return subprocess.CompletedProcess(argv, 0, "https://npm.example.internal/\n", "")
        return subprocess.CompletedProcess(argv, 1, "", NPM_OFFLINE_LOG)

    monkeypatch.setattr(R.subprocess, "run", run)
    net = Net(monkeypatch, False)
    with pytest.raises(R.OFFLINE):
        R.ensure()
    assert net.urls == ["https://npm.example.internal/"]


# -- the model download in the Node worker ----------------------------------------------------------------
STUB_WORKER = 'import json, sys\n{}\nsys.exit(1)\n'


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """The real setup path (no command given), with ensure() done and a stub worker that fails to load the model."""
    monkeypatch.setattr(E, "_shared", {})

    def use(line):
        script = tmp_path / "worker.py"
        script.write_text(STUB_WORKER.format(f"print({line!r}, flush=True)" if line else ""))
        monkeypatch.setattr(R, "worker_command", lambda: ([sys.executable, str(script)], {}))
    return use


@pytest.mark.parametrize("said", [json.dumps({"ready": False, "error": "fetch failed"}), ""],
                         ids=["says-why", "says-nothing"])
def test_the_model_download_failing_offline_is_a_wait(worker, monkeypatch, said):
    worker(said)
    net = Net(monkeypatch, False)
    with pytest.raises(E.EmbedError) as caught:
        E.LocalEmbedder().start()
    assert caught.value.flags.get("offline") is True
    assert net.urls == [R.MODEL_HOST]


def test_a_worker_that_fails_with_the_network_up_is_a_failure_with_its_reason(worker, monkeypatch):
    worker(json.dumps({"ready": False, "error": "Protobuf parsing failed"}))
    Net(monkeypatch, True)
    with pytest.raises(E.EmbedError) as caught:
        E.LocalEmbedder().start()
    assert not caught.value.flags.get("offline") and "Protobuf parsing failed" in str(caught.value)


def test_a_custom_worker_command_never_checks_the_network(monkeypatch, tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(STUB_WORKER.format(""))
    net = Net(monkeypatch, False)
    with pytest.raises(E.EmbedError) as caught:
        E.LocalEmbedder(command=[sys.executable, str(script)]).start()
    assert not caught.value.flags.get("offline") and net.urls == []


def test_the_worker_says_why_it_could_not_load_the_model(tmp_path):
    """Run the real embed_worker.mjs against a Transformers.js whose download fails: it must say so on stdout, not die silently."""
    node = shutil.which("node")
    if not node:
        pytest.skip("no Node.js on PATH")
    here = SRC / "classes" / "media_index" / "worker"
    for name in ("embed_worker.mjs", "embed_reference.json"):
        shutil.copyfile(here / name, tmp_path / name)
    pkg = tmp_path / "node_modules" / "@huggingface" / "transformers"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": "@huggingface/transformers", "version": "4.3.1", "type": "module",
                                                  "main": "index.js"}))
    (pkg / "index.js").write_text(textwrap.dedent("""\
        export const env = {};
        export class RawImage {}
        export const AutoModel = { from_pretrained: async () => { throw new Error("not reached"); } };
        export const AutoProcessor = { from_pretrained: async () => { throw new TypeError("fetch failed"); } };
    """))
    proc = subprocess.run([node, str(tmp_path / "embed_worker.mjs")], capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, env={**os.environ, "ZENVI_MODEL_DIR": str(tmp_path / "models")})
    assert proc.returncode == 1
    assert json.loads(proc.stdout.splitlines()[0]) == {"ready": False, "error": "fetch failed"}


# -- check_network ---------------------------------------------------------------------------------------
def _http_error(code):
    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, "", {}, None)
    return urlopen


def test_a_gateway_that_cannot_get_through_is_no_network_but_any_other_answer_is(monkeypatch):
    monkeypatch.setattr(R.urllib.request, "urlopen", _http_error(502))
    with pytest.raises(R.OFFLINE):
        R.check_network("https://registry.npmjs.org/")
    monkeypatch.setattr(R.urllib.request, "urlopen", _http_error(405))
    assert R.check_network("https://registry.npmjs.org/") is None


# -- what the user sees --------------------------------------------------------------------------------
class Job:
    """IndexingJob on a fresh profile, with the search vectors starting the real local embedder as compute_vectors does."""

    def __init__(self, monkeypatch, tmp_path):
        from classes.media_index import facts, vectors
        self.shelf = Shelf(str(tmp_path / "shelf"))
        monkeypatch.setattr("classes.media_index.store.default_shelf", lambda: self.shelf)
        monkeypatch.setattr("classes.media_index.default_shelf", lambda: self.shelf)
        monkeypatch.setattr(IndexingJob, "_signed_in", staticmethod(lambda: False))
        monkeypatch.setattr(facts, "compute_facts", lambda *a, **k: {"ok": True})
        monkeypatch.setattr("classes.media_index.probe.probe_media", lambda p: {"has_video": True, "duration": 6.0})
        monkeypatch.setattr(E, "_setting", lambda: "")
        monkeypatch.setattr(E, "describe_on_import", lambda: False)
        monkeypatch.setattr(vectors, "compute_vectors", lambda path, probe, sha, shelf, embedder, **kw: embedder.start())
        self.client = MagicMock()
        self.client._empty_ai_metadata.side_effect = ZenviBackendClient._empty_ai_metadata
        self.media = tmp_path / "clip.mp4"
        self.media.write_bytes(os.urandom(4096))

    def run(self):
        done, phases = [], []
        IndexingJob({"id": "f1", "path": str(self.media), "media_type": "video", "duration": 6.0,
                     "fingerprint": fingerprint(str(self.media))}, "p1",
                    client_factory=lambda: self.client, emit_completed=lambda fd, md, err: done.append((md, err)),
                    emit_progress=lambda fid, phase, pct: phases.append(phase)).run()
        assert len(done) == 1
        return done[0][0], done[0][1], phases


def test_offline_first_import_with_a_system_node_shows_waiting_and_stays_queued(system_node, monkeypatch, tmp_path):
    system_node["ci"] = (1, NPM_OFFLINE_LOG)
    Net(monkeypatch, False)
    meta, error, phases = Job(monkeypatch, tmp_path).run()
    assert error is None and not meta.get("error")
    assert meta.get("retry_index") is True, "the file model retries it in 15 s"
    assert "downloading" in phases
    status = derive_indexing_status(meta)
    assert status.state == PENDING and status.label == WAITING_FOR_NETWORK and "npm" not in status.tooltip


def test_a_real_setup_failure_is_shown_short_and_is_not_retried(system_node, monkeypatch, tmp_path):
    system_node["ci"] = (1, NPM_DISK_FULL_LOG)
    Net(monkeypatch, True)
    meta, error, _ = Job(monkeypatch, tmp_path).run()
    assert error is None and not meta.get("retry_index")
    assert meta["index"]["status"] == "failed"
    status = derive_indexing_status(meta)
    assert status.state == FAILED and "ENOSPC" in status.tooltip and "complete log" not in status.tooltip
