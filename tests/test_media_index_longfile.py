"""Long recordings: described in chunks, resumable, and only after a yes with the cost shown."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes import credits_client as cc  # noqa: E402
from classes.indexing_status import SKIPPED, derive_indexing_status  # noqa: E402
from classes.media_index import cloud, flags, longfile  # noqa: E402
from classes.media_index import schema as S  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

SHA = "d" * 64


def shots(n, length=60.0):
    return [{"id": i, "start": i * length, "end": (i + 1) * length, "fps": 1.0} for i in range(n)]


# ============================ planning ============================
def test_chunks_are_whole_shots_in_order_and_never_longer_than_the_limit():
    plan = longfile.plan_chunks(shots(10, 60.0), chunk_seconds=240.0)
    assert [c["shot_ids"] for c in plan] == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]
    assert [(c["start"], c["end"]) for c in plan] == [(0.0, 240.0), (240.0, 480.0), (480.0, 600.0)]
    assert [c["index"] for c in plan] == [0, 1, 2]
    assert all(c["end"] - c["start"] <= 240.0 for c in plan)


def test_a_shot_is_never_cut_across_a_chunk_boundary():
    odd = [{"id": 0, "start": 0.0, "end": 100.0}, {"id": 1, "start": 100.0, "end": 300.0}, {"id": 2, "start": 300.0, "end": 310.0}]
    plan = longfile.plan_chunks(odd, chunk_seconds=150.0)
    assert [c["shot_ids"] for c in plan] == [[0], [1], [2]], "the second shot alone is longer than a chunk, so it gets its own"
    assert sorted(i for c in plan for i in c["shot_ids"]) == [0, 1, 2]


def test_planning_ignores_empty_shots_and_unsorted_input_and_nothing_gives_no_chunks():
    mixed = [{"id": 1, "start": 5.0, "end": 9.0}, {"id": 0, "start": 0.0, "end": 5.0}, {"id": 2, "start": 9.0, "end": 9.0}]
    assert [c["shot_ids"] for c in longfile.plan_chunks(mixed)] == [[0, 1]]
    assert longfile.plan_chunks([]) == []


@pytest.mark.parametrize("seconds,minutes,chunks,credits", [(1801, 31, 2, 31 * 18), (3600, 60, 3, 1080), (7200, 120, 6, 2160), (0, 1, 1, 18)])
def test_the_estimate_is_a_stated_ceiling(seconds, minutes, chunks, credits):
    assert longfile.estimate(seconds) == {"minutes": minutes, "chunks": chunks, "max_credits": credits}


def test_what_the_user_is_told_names_the_cost_the_passes_and_what_to_do():
    text = longfile.approval_text("podcast.mp4", 5400)
    assert "podcast.mp4 is 90 min long" in text and "5 cloud passes" in text and "at most about 1620 credits" in text
    assert "usually lower" in text and "already analysed on this computer" in text and "index_long_file_tool" in text


def test_only_longer_than_thirty_minutes_counts_as_long():
    assert not longfile.is_long(1800) and longfile.is_long(1800.1) and not longfile.is_long(0)


# ============================ the chunked cloud pass ============================
class ChunkClient:
    """A backend that answers for exactly the shots it was asked about, as the real one does."""

    def __init__(self, fail_on_chunk=None):
        self.uploads, self.requests, self.fail_on, self.n = [], [], fail_on_chunk, 0
        self.pending = None

    def _new_http_session(self):
        return "s"

    def v2_upload_session(self, file_id, filename, total_size, mime_type="video/mp4", session=None):
        self.uploads.append(total_size)
        return {"upload_url": "https://u/x"}

    def v2_understand(self, file_name, file_uri, request_shots, transcript, **kw):
        self.n += 1
        self.requests.append({"shots": request_shots, "transcript": transcript})
        if self.fail_on == self.n:
            return {"error": "quota exceeded"}
        self.pending = request_shots
        return {"job_id": f"j{self.n}"}

    def v2_job(self, job_id, session=None):
        done = [{"id": s["id"], "start": s["start"], "end": s["end"], "description": f"shot {s['id']}"} for s in self.pending]
        return {"status": "done", "result": {"shots": done, "usage": {"prompt_tokens": 100, "output_tokens": 10, "model": "m", "batches": 1}, "missing": 0}}


def upload_ok(path, url, mime_type="video/mp4"):
    return {"name": "files/x", "uri": "https://g/x"}, ""


@pytest.fixture
def small(monkeypatch, tmp_path):
    monkeypatch.setattr(longfile, "LONG_SECONDS", 100.0)
    monkeypatch.setattr(longfile, "CHUNK_SECONDS", 240.0)
    windows = []

    def fake_proxy(path, probe, out, start=None, end=None):
        windows.append((start, end))
        Path(out).write_bytes(b"x" * 10)
        return True, ""

    monkeypatch.setattr(cloud, "make_proxy", fake_proxy)
    return SimpleNamespaceLike(windows=windows, shelf=Shelf(str(tmp_path / "shelf")))


class SimpleNamespaceLike:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def structure(n):
    return {"shots": [{"id": s["id"], "start": s["start"], "end": s["end"], "motion": {}} for s in shots(n)]}


SPEECH = {"sentences": [{"start": 10.0, "end": 12.0, "text": "early", "speaker": "A"}, {"start": 250.0, "end": 252.0, "text": "later", "speaker": "A"}]}
PROBE = {"duration": 600.0, "video": {"height": 1080}}


def test_a_long_file_is_described_in_chunks_each_with_its_own_proxy_and_shifted_times(small):
    client = ChunkClient()
    out = cloud.run_watch(client, "/m/long.mp4", PROBE, "F1", structure(10), SPEECH, uploader=upload_ok, resume=(small.shelf, SHA))
    assert small.windows == [(0.0, 240.0), (240.0, 480.0), (480.0, 600.0)]
    first, second, third = client.requests
    assert [s["start"] for s in first["shots"]] == [0.0, 60.0, 120.0, 180.0]
    assert [s["start"] for s in second["shots"]] == [0.0, 60.0, 120.0, 180.0], "times are relative to each chunk's own proxy"
    assert [s["end"] for s in second["shots"]] == [60.0, 120.0, 180.0, 240.0] and [s["end"] for s in third["shots"]] == [60.0, 120.0]
    assert [t["text"] for t in first["transcript"]] == ["early"] and [t["start"] for t in second["transcript"]] == [10.0] and third["transcript"] == []
    assert second["transcript"][0]["text"] == "later"
    assert [s["id"] for s in out["shots"]] == list(range(10)), "every shot, in order"
    assert [(s["start"], s["end"]) for s in out["shots"]][4] == (240.0, 300.0), "results are back on the file's own clock"
    assert out["usage"] == {"prompt_tokens": 300, "output_tokens": 30, "batches": 3, "model": "m", "chunks": 3} and out["missing"] == 0
    assert small.shelf.read_json(SHA, "watch_partial.json") == {}, "nothing half-done is left behind"


def test_a_short_file_is_still_one_upload(small, monkeypatch):
    monkeypatch.setattr(longfile, "LONG_SECONDS", 1000.0)
    client = ChunkClient()
    out = cloud.run_watch(client, "/m/a.mp4", {"duration": 600.0}, "F1", structure(10), None, uploader=upload_ok)
    assert len(client.requests) == 1 and len(out["shots"]) == 10 and small.windows == [(None, None)]


def test_an_interrupted_long_file_carries_on_without_repeating_finished_chunks(small):
    client = ChunkClient(fail_on_chunk=2)
    out = cloud.run_watch(client, "/m/long.mp4", PROBE, "F1", structure(10), SPEECH, uploader=upload_ok, resume=(small.shelf, SHA))
    assert "chunk 2 of 3" in out["error"] and "quota exceeded" in out["error"]
    assert list(small.shelf.read_json(SHA, "watch_partial.json")["done"]) == ["0"], "chunk 1 was kept"
    again = ChunkClient()
    done = cloud.run_watch(again, "/m/long.mp4", PROBE, "F1", structure(10), SPEECH, uploader=upload_ok, resume=(small.shelf, SHA))
    assert len(again.requests) == 2 and len(again.uploads) == 2, "only chunks 2 and 3 were uploaded and paid for"
    assert [s["id"] for s in done["shots"]] == list(range(10)) and done["usage"]["prompt_tokens"] == 300


def test_saved_chunks_are_not_reused_for_different_footage_or_another_layer_version(small, monkeypatch):
    first = ChunkClient(fail_on_chunk=2)
    cloud.run_watch(first, "/m/long.mp4", PROBE, "F1", structure(10), SPEECH, uploader=upload_ok, resume=(small.shelf, SHA))
    other = ChunkClient()
    cloud.run_watch(other, "/m/long.mp4", PROBE, "F1", structure(12), SPEECH, uploader=upload_ok, resume=(small.shelf, SHA))
    assert len(other.requests) == 3, "a different cut of the footage starts again"
    small.shelf.write_json(SHA, "watch_partial.json", {"plan": [[0, 0.0, 240.0, 4]], "version": 999, "done": {"0": {"shots": [], "usage": {}}}})
    third = ChunkClient()
    cloud.run_watch(third, "/m/long.mp4", PROBE, "F1", structure(10), SPEECH, uploader=upload_ok, resume=(small.shelf, SHA))
    assert len(third.requests) == 3, "another layer version starts again"


def test_a_refusal_inside_a_chunk_reaches_the_caller_with_its_flag(small):
    class OutOfCredits(ChunkClient):
        def v2_understand(self, *a, **k):
            return {"credits": True, "error": "Out of credits"}

    out = cloud.run_watch(OutOfCredits(), "/m/long.mp4", PROBE, "F1", structure(10), SPEECH, uploader=upload_ok)
    assert out["credits"] is True and out["error"].startswith("chunk 1 of 3")


def test_cancelling_between_chunks_stops_the_pass(small):
    state = {"n": 0}

    def cancelled():
        state["n"] += 1
        return state["n"] > 4

    client = ChunkClient()
    with pytest.raises(cloud.Cancelled):
        cloud.run_watch(client, "/m/long.mp4", PROBE, "F1", structure(10), SPEECH, uploader=upload_ok, should_cancel=cancelled)
    assert len(client.requests) < 3


# ============================ the job: a long file waits for a yes ============================
@pytest.fixture
def harness(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).parent))
    from test_indexing_job_reuse import Harness
    monkeypatch.setattr(flags, "v2_enabled", lambda: True)
    from classes.media_index import facts
    monkeypatch.setattr(facts, "compute_facts", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(cc, "check_operation", lambda *a, **k: (True, 1000, None))
    return Harness(monkeypatch, tmp_path)


def test_a_long_file_waits_for_approval_and_shows_the_estimate(monkeypatch, harness):
    calls = []
    monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: calls.append(1) or {"layers": {}})
    meta, _ = harness.run(duration=90 * 60.0)
    assert calls == [] and meta["skip_code"] == "long_file" and "at most about 1620 credits" in meta["skip_reason"]
    status = derive_indexing_status(meta)
    assert status.state == SKIPPED and status.label == "Approval needed" and "1620" in status.tooltip
    harness.client.start_direct_indexing_job.assert_not_called()


def test_an_approved_long_file_goes_through_the_cloud_pass(monkeypatch, harness):
    seen = []

    def fake(client, path, probe, sha, shelf, **kw):
        seen.append(sha)
        shelf.write_json(sha, "structure.json", {"shots": [{"id": 0, "start": 0.0, "end": 6.0}]})
        shelf.write_json(sha, "watch.json", {"shots": [{"id": 0, "start": 0.0, "end": 6.0, "description": "A long talk."}]})
        for layer in ("watch", "vectors"):
            shelf.set_layer(sha, layer, version=1, status="ready")
        return {"layers": {"watch": "ready", "vectors": "ready"}}

    monkeypatch.setattr(cloud, "compute_cloud", fake)
    meta, _ = harness.run(duration=90 * 60.0, extra={"index_long_ok": True})
    assert seen == [harness.sha] and meta["index"]["status"] == "ready" and meta["chapters"][0]["summary"] == "A long talk."
    assert "skip_code" not in meta and harness.charges == []


def test_a_long_file_the_cloud_cannot_take_keeps_the_original_limit_message(monkeypatch, harness):
    monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: {"unsupported": True, "error": "no v2", "layers": {}})
    meta, _ = harness.run(duration=90 * 60.0, extra={"index_long_ok": True})
    assert "30-minute limit" in meta["skip_reason"] and "skip_code" not in meta
    harness.client.start_direct_indexing_job.assert_not_called()


def test_without_the_preference_a_long_file_is_skipped_exactly_as_before(monkeypatch, harness):
    monkeypatch.setattr(flags, "v2_enabled", lambda: False)
    meta, _ = harness.run(duration=90 * 60.0, extra={"index_long_ok": True})
    assert "30-minute limit" in meta["skip_reason"] and "skip_code" not in meta


def test_a_clip_just_under_the_limit_is_not_treated_as_long(monkeypatch, harness):
    monkeypatch.setattr(cloud, "compute_cloud", lambda *a, **k: {"layers": {"watch": "skipped"}})
    meta, _ = harness.run(duration=29 * 60.0)
    assert meta.get("skip_code") != "long_file"


# ============================ index_long_file_tool ============================
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from classes.editor_tools import REGISTRY, media_index_tools as T  # noqa: E402


def tool(**kw):
    out = REGISTRY["index_long_file_tool"].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


class Saved(SimpleNamespace):
    def save(self):
        self.saves += 1


@pytest.fixture
def library(monkeypatch, tmp_path):
    shelf = Shelf(str(tmp_path / "shelf"))
    queued = []

    def f(fid, name, minutes, kind="video", sha=None, ai=None, ok=False):
        data = {"name": name, "path": f"/m/{name}", "media_type": kind, "duration": minutes * 60.0,
                "fingerprint": {"sha256": sha} if sha else None, "ai_metadata": ai or {}}
        if ok:
            data["index_long_ok"] = True
        return Saved(id=fid, data=data, saves=0)

    files = [f("L1", "podcast.mp4", 90, sha="1" * 64, ai={"skip_code": "long_file"}), f("S1", "short.mp4", 5, sha="2" * 64),
             f("L2", "lecture.mp4", 40, sha="3" * 64), f("P1", "pic.png", 100, kind="image"), f("L3", "fresh.mp4", 50)]
    monkeypatch.setattr(flags, "v2_enabled", lambda: True)
    monkeypatch.setattr(T, "default_shelf", lambda: shelf)
    monkeypatch.setattr(T, "resolve_files", lambda ids=None, query="": [x for x in files if x.id in (ids or [])])
    monkeypatch.setattr(T, "on_main", lambda fn, *a, **k: fn(*a))
    model = SimpleNamespace(_enqueue_index=lambda fid: queued.append(fid))
    monkeypatch.setattr(T, "get_app", lambda: SimpleNamespace(window=SimpleNamespace(files_model=model)))
    return SimpleNamespace(files={x.id: x for x in files}, shelf=shelf, queued=queued)


def test_without_approval_it_only_reports_the_passes_and_the_cost_ceiling(library):
    head, r = tool(file_ids=["L1", "L2"])
    rows = {x["file_id"]: x for x in r["files"]}
    assert rows["L1"]["state"] == "needs_approval" and rows["L1"]["waiting"] is True
    assert (rows["L1"]["minutes"], rows["L1"]["chunks"], rows["L1"]["max_credits"]) == (90, 5, 1620)
    assert (rows["L2"]["minutes"], rows["L2"]["chunks"], rows["L2"]["max_credits"]) == (40, 2, 720) and rows["L2"]["waiting"] is False
    assert "podcast.mp4 (90 min, 5 passes, up to 1620 credits)" in head and r["changed"] is False
    assert library.queued == [] and "index_long_ok" not in library.files["L1"].data and library.files["L1"].saves == 0


def test_approval_marks_the_file_saves_it_and_queues_it_once(library):
    head, r = tool(file_ids=["L1"], approve=True)
    assert head == "Queued 1 long file(s) to be described." and r["changed"] is True and r["files"][0]["state"] == "will_describe"
    assert library.files["L1"].data["index_long_ok"] is True and library.files["L1"].saves == 1 and library.queued == ["L1"]


def test_only_files_that_really_need_a_yes_are_queued(library):
    library.shelf.set_layer("3" * 64, S.LAYER_WATCH, version=1, status="ready")
    _, r = tool(file_ids=["S1", "L2", "P1", "L3"], approve=True)
    states = {x["file_id"]: x["state"] for x in r["files"]}
    assert states == {"S1": "not_long", "L2": "described", "P1": "not_applicable", "L3": "not_ready"}
    assert library.queued == [] and r["changed"] is False


def test_asking_again_after_approval_does_not_queue_a_second_time(library):
    tool(file_ids=["L1"], approve=True)
    _, r = tool(file_ids=["L1"])
    assert r["files"][0]["state"] == "approved" and library.queued == ["L1"]


def test_it_refuses_when_the_new_index_is_off(library, monkeypatch):
    monkeypatch.setattr(flags, "v2_enabled", lambda: False)
    out = REGISTRY["index_long_file_tool"].func(file_ids=["L1"])
    assert out.startswith("Error") and "media-index-v2" in out


def test_nothing_waiting_says_so(library):
    head, _ = tool(file_ids=["S1"])
    assert head == "No long file is waiting for approval."
