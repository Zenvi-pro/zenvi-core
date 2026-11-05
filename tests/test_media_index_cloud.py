"""The cloud layer: shot descriptions on demand, against a fake backend and real ffmpeg."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import media_fixtures as mf  # noqa: E402
from classes import local_describe  # noqa: E402
from classes.ai_metadata_utils import is_ai_metadata_usable  # noqa: E402
from classes.indexing_status import SUCCESS, derive_indexing_status  # noqa: E402
from classes.media_index import cloud, schema as S  # noqa: E402
from classes.media_index.probe import probe_media  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

SHA = "c" * 64


# ============================ planning ============================
@pytest.mark.parametrize("motion,speech,expected", [
    ({"class": "static", "level": 0.0}, None, 0.5),
    ({"class": "static", "level": 0.4}, {"speech_ratio": 0.8}, 0.5),
    ({"class": "static", "level": 0.4}, {"speech_ratio": 0.1}, 1.0),
    ({"class": "pan", "level": 0.3}, None, 1.0),
    ({"class": "busy", "level": 0.2}, None, 2.0),
    ({"class": "handheld", "level": 0.9}, None, 2.0),
    ({}, None, 1.0),
])
def test_calm_shots_are_watched_rarely_and_busy_ones_often(motion, speech, expected):
    assert cloud.plan_shot_fps({"motion": motion}, speech) == expected


STRUCTURE = {"shots": [
    {"id": 0, "start": 0.0, "end": 4.0, "motion": {"class": "static", "level": 0.0}},
    {"id": 1, "start": 4.0, "end": 9.0, "motion": {"class": "busy", "level": 0.7}},
    {"id": 2, "start": 9.0, "end": 9.0, "motion": {}},
]}
SPEECH = {"per_shot": {"0": {"speech_ratio": 0.9}}, "sentences": [
    {"start": 0.5, "end": 2.0, "text": "Hello there.", "speaker": "A"}, {"start": 5.0, "end": 6.0, "text": "  ", "speaker": None}]}


def test_the_request_lists_real_shots_with_their_rates():
    assert cloud.watch_request_shots(STRUCTURE, SPEECH) == [
        {"id": 0, "start": 0.0, "end": 4.0, "fps": 0.5}, {"id": 1, "start": 4.0, "end": 9.0, "fps": 2.0}]


def test_transcript_rows_carry_text_and_speaker():
    rows = cloud.transcript_rows(SPEECH)
    assert rows[0] == {"start": 0.5, "end": 2.0, "text": "Hello there.", "speaker": "A"} and cloud.transcript_rows(None) == []


def test_a_shot_document_gathers_what_is_seen_read_and_heard():
    doc = cloud.shot_document({
        "description": "A man jumps over a fence.", "actions": ["jumps", "lands"], "mood": "tense", "shot_type": "wide",
        "objects": [{"label": "man"}, {"label": "fence"}, {"label": "man"}],
        "on_screen_text": [{"text": "EXIT"}], "sound_events": [{"label": "footsteps"}, {"label": "music"}]})
    assert doc == ("A man jumps over a fence. Actions: jumps; lands. Seen: man, fence. Text on screen: EXIT. "
                   "Sounds: footsteps, music. tense, wide.")
    assert cloud.shot_document({"description": "Just this."}) == "Just this."
    assert cloud.shot_document({}) == ""


# ============================ back to the v1 shape ============================
WATCH = {"shots": [
    {"id": 0, "start": 0.0, "end": 4.0, "description": "A calm pond. Willows sway.", "actions": ["ripples"],
     "objects": [{"label": "pond"}, {"label": "willow tree"}], "sound_events": [{"label": "birdsong"}], "mood": "calm", "shot_type": "wide"},
    {"id": 1, "start": 4.0, "end": 8.0, "description": "A dog runs in.", "actions": ["runs in"], "objects": [{"label": "dog"}, {"label": "Pond"}],
     "sound_events": [], "mood": "playful", "shot_type": "medium"}]}


def test_v2_data_fills_every_field_the_existing_ui_and_tools_read():
    meta = cloud.metadata_view(WATCH, SPEECH, STRUCTURE)
    assert meta["analyzed"] is True and meta["provider"] == "media-index-v2" and is_ai_metadata_usable(meta)
    assert [c["title"] for c in meta["chapters"]] == ["A calm pond", "A dog runs in"]
    assert meta["chapters"][0]["objects"] == ["pond", "willow tree"] and meta["chapters"][0]["sounds"] == "birdsong"
    assert meta["scene_descriptions"][0] == {"description": "A calm pond — A calm pond. Willows sway.", "source_time": 0.0, "time": 0.0}
    assert meta["transcript_cues"][0]["text"] == "Hello there." and "Hello there." in meta["transcript"]
    assert [m["label"] for m in meta["moments"]] == ["ripples", "runs in"]
    assert meta["tags"]["objects"] == ["pond", "willow tree", "dog"] and meta["tags"]["mood"] == ["calm", "playful"]
    assert meta["sounds"] == "birdsong" and meta["short_summary"].startswith("A calm pond.")
    meta["index"] = {"status": "ready", "index_id": "zenvi-p", "video_id": "f"}
    assert derive_indexing_status(meta).state == SUCCESS


def test_nothing_described_is_not_analysed():
    meta = cloud.metadata_view({"shots": []}, None, {"shots": []})
    assert meta["analyzed"] is False and meta["chapters"] == []


# ============================ ffmpeg ============================
@pytest.fixture(scope="module")
def video(tmp_path_factory):
    mf.need_ffmpeg()
    d = tmp_path_factory.mktemp("cloud")
    base = mf.scene(41)
    src = mf.render(str(d / "v.mp4"), base, mf.hold(base), 6.0, size=(1280, 720))
    import subprocess
    av = str(d / "av.mp4")
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-i", src, "-f", "lavfi", "-i", "sine=frequency=300:duration=6", "-c:v", "copy", "-c:a", "aac", "-shortest", av], check=True)
    return av


def test_the_proxy_is_small_keeps_the_audio_and_is_480_high(video, tmp_path):
    out = str(tmp_path / "p.mp4")
    ok, err = cloud.make_proxy(video, probe_media(video), out)
    assert ok, err
    p = probe_media(out)
    assert p["video"]["height"] == 480 and p["has_audio"] and p["duration"] == pytest.approx(6.0, abs=0.3)
    assert Path(out).stat().st_size < Path(video).stat().st_size


def test_a_proxy_can_be_cut_at_another_height(video, tmp_path):
    out = str(tmp_path / "p.mp4")
    ok, err = cloud.make_proxy(video, probe_media(video), out, max_height=360)
    assert ok, err
    assert probe_media(out)["video"]["height"] == 360


def test_a_proxy_of_a_missing_file_fails_cleanly(tmp_path):
    mf.need_ffmpeg()
    ok, err = cloud.make_proxy(str(tmp_path / "nope.mp4"), {"video": {"height": 720}}, str(tmp_path / "p.mp4"))
    assert ok is False and err


# ============================ a fake backend ============================
class FakeClient:
    def __init__(self, watch=WATCH, job_states=None):
        self.calls, self.watch = [], watch
        self.job_states = list(job_states or [{"status": "done", "result": None}])

    def _new_http_session(self):
        return "session"

    def v2_upload_session(self, file_id, filename, total_size, mime_type="video/mp4", session=None):
        self.calls.append(("upload_session", file_id, total_size))
        return {"upload_url": "https://upload.example/x", "mime_type": mime_type}

    def v2_understand(self, file_name, file_uri, shots, transcript, **kw):
        self.calls.append(("understand", file_name, file_uri, shots, transcript))
        return {"job_id": "job1", "status": "started"}

    def v2_job(self, job_id, session=None):
        self.calls.append(("job", job_id))
        state = self.job_states.pop(0) if len(self.job_states) > 1 else self.job_states[0]
        if state.get("status") == "done" and state.get("result") is None:
            state = {"status": "done", "result": {**self.watch, "usage": {"prompt_tokens": 1000, "output_tokens": 200, "model": "m"}, "missing": 0}}
        return state


def uploader(path, url, mime_type="video/mp4"):
    uploader.sent.append((path, url, mime_type, Path(path).stat().st_size))
    return {"name": "files/abc", "uri": "https://gemini/files/abc"}, ""


uploader.sent = []


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    uploader.sent.clear()
    monkeypatch.setattr(cloud, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(local_describe, "enabled", lambda: False)     # these tests are the Gemini pass; local ones are in test_local_describe.py


# ============================ the watch run ============================
def test_a_watch_run_uploads_one_proxy_and_asks_for_every_shot_with_its_rate(video):
    client = FakeClient()
    probe = probe_media(video)
    structure = {"shots": [{"id": 0, "start": 0.0, "end": 3.0, "motion": {"class": "static", "level": 0.0}},
                           {"id": 1, "start": 3.0, "end": 6.0, "motion": {"class": "busy", "level": 0.9}}]}
    out = cloud.run_watch(client, video, probe, "F1", structure, SPEECH, uploader=uploader)
    assert out["shots"][0]["description"].startswith("A calm pond") and out["usage"]["prompt_tokens"] == 1000
    assert len(uploader.sent) == 1 and uploader.sent[0][1] == "https://upload.example/x" and uploader.sent[0][2] == "video/mp4"
    understand = next(c for c in client.calls if c[0] == "understand")
    assert understand[1:3] == ("files/abc", "https://gemini/files/abc")
    assert [(s["id"], s["fps"]) for s in understand[3]] == [(0, 0.5), (1, 2.0)] and understand[4][0]["text"] == "Hello there."


def test_the_watch_run_keeps_polling_until_the_job_is_done(video):
    client = FakeClient(job_states=[{"status": "running"}, {"status": "running"}, {"status": "done", "result": None}])
    out = cloud.run_watch(client, video, probe_media(video), "F1", STRUCTURE, SPEECH, uploader=uploader)
    assert out["shots"] and sum(1 for c in client.calls if c[0] == "job") == 3


def test_a_failed_job_is_reported(video):
    client = FakeClient(job_states=[{"status": "failed", "result": {"error": "quota exceeded"}}])
    assert cloud.run_watch(client, video, probe_media(video), "F1", STRUCTURE, None, uploader=uploader) == {"error": "quota exceeded"}


def test_a_lost_job_is_reported(video):
    client = FakeClient(job_states=[{"status": "not_found"}])
    assert "lost" in cloud.run_watch(client, video, probe_media(video), "F1", STRUCTURE, None, uploader=uploader)["error"]


def test_an_old_backend_without_v2_is_flagged_so_the_caller_can_fall_back(video):
    client = FakeClient()
    client.v2_upload_session = lambda *a, **k: {"unsupported": True, "error": "this backend has no media index v2"}
    out = cloud.run_watch(client, video, probe_media(video), "F1", STRUCTURE, None, uploader=uploader)
    assert out["unsupported"] is True and uploader.sent == []


def test_a_failed_upload_is_reported_and_no_job_is_started(video):
    client = FakeClient()
    out = cloud.run_watch(client, video, probe_media(video), "F1", STRUCTURE, None, uploader=lambda *a, **k: ({}, "connection reset"))
    assert "connection reset" in out["error"] and not any(c[0] == "understand" for c in client.calls)


def test_an_unreachable_backend_gives_up_after_the_grace_period(video, monkeypatch):
    monkeypatch.setattr(cloud, "UNREACHABLE_SECONDS", 0.0)
    client = FakeClient()
    client.v2_job = lambda *a, **k: {"error": "connection refused"}
    assert "unreachable" in cloud.run_watch(client, video, probe_media(video), "F1", STRUCTURE, None, uploader=uploader)["error"]


def test_cancelling_during_the_wait_stops_the_run(video):
    client = FakeClient(job_states=[{"status": "running"}])
    with pytest.raises(cloud.Cancelled):
        cloud.run_watch(client, video, probe_media(video), "F1", STRUCTURE, None, uploader=uploader, should_cancel=lambda: len(client.calls) > 2)


def test_no_shots_means_nothing_to_describe(video):
    assert "no shots" in cloud.run_watch(FakeClient(), video, probe_media(video), "F1", {"shots": []}, None, uploader=uploader)["error"]


# ============================ the description layer ============================
@pytest.fixture
def prepared(video, tmp_path):
    shelf = Shelf(str(tmp_path / "shelf"))
    structure = {"shots": [{"id": 0, "start": 0.0, "end": 3.0, "motion": {"class": "static", "level": 0.0}},
                           {"id": 1, "start": 3.0, "end": 6.0, "motion": {"class": "pan", "level": 0.3}}]}
    shelf.write_json(SHA, "structure.json", structure)
    shelf.set_layer(SHA, "structure", version=1, status="ready")
    shelf.write_json(SHA, "speech.json", SPEECH)
    shelf.set_layer(SHA, "speech", version=1, status="ready")
    return shelf, probe_media(video)


def test_every_shot_is_described_and_kept(video, prepared):
    shelf, probe = prepared
    out = cloud.compute_cloud(FakeClient(), video, probe, SHA, shelf, file_id="F1", uploader=uploader)
    assert out["layers"] == {"watch": "ready"} and out["usage"]["prompt_tokens"] == 1000
    assert shelf.layer_ready(SHA, "watch", version=S.LAYER_VERSIONS["watch"])
    watch = shelf.read_json(SHA, "watch.json")
    assert watch["kind"] == "inferred" and len(watch["shots"]) == 2 and watch["model"] == "m"


def test_a_second_run_costs_nothing(video, prepared):
    shelf, probe = prepared
    cloud.compute_cloud(FakeClient(), video, probe, SHA, shelf, uploader=uploader)
    second = FakeClient()
    out = cloud.compute_cloud(second, video, probe, SHA, shelf, uploader=uploader)
    assert out["layers"] == {"watch": "cached"} and second.calls == [] and len(uploader.sent) == 1


def test_only_the_shots_not_yet_described_are_sent(video, prepared):
    shelf, probe = prepared
    shelf.write_json(SHA, "watch.json", {"shots": [WATCH["shots"][0]]})
    shelf.set_layer(SHA, "watch", version=1, status="ready")          # what an older release, or one shot asked for, left
    client = FakeClient(watch={"shots": [WATCH["shots"][1]]})
    cloud.compute_cloud(client, video, probe, SHA, shelf, uploader=uploader)
    asked = next(c[3] for c in client.calls if c[0] == "understand")
    assert [s["id"] for s in asked] == [1]
    assert [s["id"] for s in shelf.read_json(SHA, "watch.json")["shots"]] == [0, 1]


def test_a_watch_failure_returns_the_reason_and_saves_nothing(video, prepared):
    shelf, probe = prepared
    client = FakeClient(job_states=[{"status": "failed", "result": {"error": "quota exceeded"}}])
    out = cloud.compute_cloud(client, video, probe, SHA, shelf, uploader=uploader)
    assert out["error"] == "quota exceeded" and not shelf.layer_ready(SHA, "watch")


def test_an_old_backend_is_reported(video, prepared):
    shelf, probe = prepared
    client = FakeClient()
    client.v2_upload_session = lambda *a, **k: {"unsupported": True, "error": "x"}
    assert cloud.compute_cloud(client, video, probe, SHA, shelf, uploader=uploader)["unsupported"] is True


def test_audio_files_and_files_without_shots_have_nothing_to_describe(video, tmp_path):
    shelf = Shelf(str(tmp_path / "shelf"))
    audio = cloud.compute_cloud(FakeClient(), video, {"has_video": False, "has_audio": True}, SHA, shelf, media_type="audio", uploader=uploader)
    no_shots = cloud.compute_cloud(FakeClient(), video, probe_media(video), SHA, shelf, uploader=uploader)
    assert audio["layers"] == no_shots["layers"] == {"watch": "skipped"} and uploader.sent == []


def test_a_watch_that_describes_nothing_is_a_failure(video, prepared):
    shelf, probe = prepared
    out = cloud.compute_cloud(FakeClient(watch={"shots": []}), video, probe, SHA, shelf, file_id="F1", uploader=uploader)
    assert "error" in out and not shelf.layer_ready(SHA, "watch")


def test_the_audio_summary_states_only_what_was_measured():
    assert cloud.audio_summary({}, 40.2) == "Audio, 40 s."
    full = cloud.audio_summary({"tempo": {"bpm": 109.6}, "loudness": {"integrated_lufs": -13.6},
                                "music": {"sections": [{"label": "intro"}, {"label": "peak"}, {"label": "outro"}]}}, 36.0)
    assert full == "Audio, 36 s, 110 BPM, sections: intro, peak, outro, -14 LUFS."
    assert cloud.audio_summary({"music": {"sections": [{"label": "steady"}]}}, 10) == "Audio, 10 s.", "one section is not worth naming"


def test_a_watch_refused_for_credits_passes_the_flag_up(video, prepared):
    shelf, probe = prepared

    class OutOfCredits(FakeClient):
        def v2_upload_session(self, *a, **k):
            return {"credits": True, "error": "Out of credits"}

    out = cloud.compute_cloud(OutOfCredits(), video, probe, SHA, shelf, file_id="F1", uploader=uploader)
    assert out["credits"] is True and "Out of credits" in out["error"]
