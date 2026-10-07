"""Lazy cloud audio understanding: windows, the small audio upload, kept music descriptions, the mix audit."""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

import media_fixtures as mf  # noqa: E402
from classes.media_index import audio_cloud as AC, cloud  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

SHA = "5" * 64


# ============================ windows ============================
def covers(windows, duration):
    assert windows[0]["start"] == 0.0 and windows[-1]["end"] == pytest.approx(duration, abs=0.01)
    for a, b in zip(windows, windows[1:]):
        assert b["start"] == pytest.approx(a["end"], abs=0.01), "windows are contiguous"
    for w in windows:
        assert AC.MIN_WINDOW - 0.01 <= w["end"] - w["start"] <= AC.MAX_WINDOW + 0.01, w
    assert len(windows) <= AC.MAX_WINDOWS


def test_a_track_without_sections_is_cut_into_even_windows_of_about_thirty_seconds():
    w = AC.plan_windows(95.0)
    covers(w, 95.0)
    assert len(w) == 4 and all(abs((x["end"] - x["start"]) - 23.75) < 0.01 for x in w)
    assert AC.plan_windows(10.0) == [{"start": 0.0, "end": 10.0}]


def test_too_short_audio_has_no_windows():
    assert AC.plan_windows(2.0) == [] and AC.plan_windows(0.0) == []


def test_windows_follow_the_musics_own_sections():
    sections = [{"end": 12.0}, {"end": 24.0}, {"end": 36.0}]
    assert AC.plan_windows(36.0, sections) == [{"start": 0.0, "end": 12.0}, {"start": 12.0, "end": 24.0}, {"start": 24.0, "end": 36.0}]


def test_a_tiny_section_merges_into_its_neighbour_and_a_long_one_is_split():
    w = AC.plan_windows(40.0, [{"end": 2.0}, {"end": 20.0}, {"end": 40.0}])
    covers(w, 40.0)
    assert w[0] == {"start": 0.0, "end": 20.0}
    long = AC.plan_windows(300.0, [{"end": 100.0}, {"end": 300.0}])
    covers(long, 300.0)
    assert max(x["end"] - x["start"] for x in long) <= 120.0 and len(long) == 3


def test_a_trailing_sliver_is_absorbed_not_left_as_a_window_that_is_too_short():
    w = AC.plan_windows(61.5, [{"end": 60.0}])
    covers(w, 61.5)


def test_an_hour_of_audio_is_widened_not_cut_off():
    w = AC.plan_windows(3600.0)
    covers(w, 3600.0)
    assert len(w) == 60 and w[-1]["end"] == 3600.0
    assert AC.plan_windows(7200.0)[-1]["end"] == 3600.0, "only the first hour is described"


def test_windows_always_cover_the_track_whatever_its_length():
    rng = random.Random(1)
    for _ in range(300):
        d = rng.uniform(3.0, 3600.0)
        cuts = sorted(rng.uniform(0, d) for _ in range(rng.randint(0, 6)))
        sections = [{"end": c} for c in cuts] + [{"end": d}] if rng.random() < 0.6 else None
        covers(AC.plan_windows(d, sections), min(d, 3600.0))


# ============================ the audio proxy ============================
@pytest.fixture(scope="module")
def song(tmp_path_factory):
    ff = mf.need_ffmpeg()
    path = str(tmp_path_factory.mktemp("ac") / "song.mp4")
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=20:size=160x90:rate=10", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=20", "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    "-metadata", "location=+37.7749-122.4194/", path], check=True)
    return path


def duration_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


def test_the_audio_proxy_is_small_mono_aac_and_carries_no_tags(song, tmp_path):
    out = str(tmp_path / "a.aac")
    ok, err = AC.make_audio_proxy(song, out)
    assert ok, err
    raw = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", out], capture_output=True, text=True).stdout
    assert '"channels": 1' in raw and "aac" in raw and "37.7749" not in raw
    assert Path(out).stat().st_size < 20 * 0.36 * 1024 * 1024 / 60 * 3        # a few tens of KB for 20 s


def test_a_range_of_the_audio_can_be_cut(song, tmp_path):
    whole, part = str(tmp_path / "w.aac"), str(tmp_path / "p.aac")
    AC.make_audio_proxy(song, whole)
    ok, _ = AC.make_audio_proxy(song, part, 5.0, 10.0)
    assert ok and duration_of(part) == pytest.approx(5.0, abs=0.3) and duration_of(whole) == pytest.approx(20.0, abs=0.3)


def test_a_file_that_is_not_audio_fails_cleanly(tmp_path):
    bad = tmp_path / "x.mp3"
    bad.write_bytes(b"nope" * 100)
    ok, err = AC.make_audio_proxy(str(bad), str(tmp_path / "o.aac"))
    assert ok is False and err


# ============================ talking to the backend ============================
class FakeClient:
    def __init__(self, windows_reply=None, job_result=None, listen_reply=None, upload_error=None):
        self.calls = []
        self.windows_reply = windows_reply
        self.job_result = job_result
        self.upload_error = upload_error

    def _new_http_session(self):
        return "session"

    def v2_upload_session(self, file_id, filename, total_size, mime_type="video/mp4", session=None):
        self.calls.append(("upload_session", file_id, mime_type))
        return self.upload_error or {"upload_url": "https://upload.example/a", "mime_type": mime_type}

    def v2_describe_audio(self, file_name, file_uri, duration_seconds, windows, mime_type="audio/aac", session=None):
        self.calls.append(("describe", file_name, file_uri, duration_seconds, windows, mime_type))
        return {"job_id": "j1", "status": "started"}

    def v2_listen(self, file_name, file_uri, duration_seconds, context=None, mime_type="audio/aac", session=None):
        self.calls.append(("listen", file_name, duration_seconds, context, mime_type))
        return {"job_id": "j2", "status": "started"}

    def v2_job(self, job_id, session=None):
        self.calls.append(("job", job_id))
        return {"status": "done", "result": self.job_result}


def uploader(path, url, mime_type="audio/aac"):
    uploader.sent.append((url, mime_type, Path(path).stat().st_size))
    return {"name": "files/aud", "uri": "https://gemini/files/aud"}, ""


uploader.sent = []


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    uploader.sent.clear()
    monkeypatch.setattr(cloud, "POLL_SECONDS", 0.0)


DESCRIBED = {"windows": [{"start": 0.0, "end": 10.0, "genre": "ambient", "energy": 0.2}], "usage": {"prompt_tokens": 700, "output_tokens": 90, "model": "m"}}


def probe_of(duration=10.0, has_audio=True):
    return {"ok": True, "has_audio": has_audio, "duration": duration}


def test_music_is_described_once_and_kept(song, tmp_path):
    shelf = Shelf(str(tmp_path / "s"))
    client = FakeClient(job_result=DESCRIBED)
    out = AC.describe_music(client, song, probe_of(10.0), SHA, shelf, file_id="F1", uploader=uploader)
    assert out["cached"] is False and out["windows"][0]["genre"] == "ambient" and out["usage"]["prompt_tokens"] == 700
    describe = next(c for c in client.calls if c[0] == "describe")
    assert describe[1:3] == ("files/aud", "https://gemini/files/aud") and describe[3] == 10.0 and describe[5] == "audio/aac"
    assert describe[4] == [{"start": 0.0, "end": 10.0}] and uploader.sent[0][1] == "audio/aac"
    assert shelf.layer_ready(SHA, "music_desc") and shelf.read_json(SHA, "music_desc.json")["kind"] == "inferred"
    again = AC.describe_music(FakeClient(job_result=DESCRIBED), song, probe_of(10.0), SHA, shelf, uploader=uploader)
    assert again["cached"] is True and again["windows"][0]["genre"] == "ambient" and len(uploader.sent) == 1


def test_force_describes_again(song, tmp_path):
    shelf = Shelf(str(tmp_path / "s"))
    AC.describe_music(FakeClient(job_result=DESCRIBED), song, probe_of(), SHA, shelf, uploader=uploader)
    again = AC.describe_music(FakeClient(job_result=DESCRIBED), song, probe_of(), SHA, shelf, uploader=uploader, force=True)
    assert again["cached"] is False and len(uploader.sent) == 2


def test_the_windows_follow_known_sections(song, tmp_path):
    shelf = Shelf(str(tmp_path / "s"))
    client = FakeClient(job_result=DESCRIBED)
    AC.describe_music(client, song, probe_of(20.0), SHA, shelf, uploader=uploader, sections=[{"end": 8.0}, {"end": 20.0}])
    assert next(c for c in client.calls if c[0] == "describe")[4] == [{"start": 0.0, "end": 8.0}, {"start": 8.0, "end": 20.0}]


@pytest.mark.parametrize("probe,fragment", [(probe_of(has_audio=False), "no audio"), (probe_of(1.0), "too short")])
def test_files_that_cannot_be_described_say_so_without_calling_the_backend(song, tmp_path, probe, fragment):
    client = FakeClient(job_result=DESCRIBED)
    out = AC.describe_music(client, song, probe, SHA, Shelf(str(tmp_path / "s")), uploader=uploader)
    assert fragment in out["error"] and client.calls == [] and uploader.sent == []


def test_backend_problems_are_reported_and_nothing_is_saved(song, tmp_path):
    shelf = Shelf(str(tmp_path / "s"))
    for client, key in ((FakeClient(upload_error={"unsupported": True, "error": "no v2"}), "unsupported"),
                        (FakeClient(upload_error={"auth": True, "error": "sign in"}), "auth"),
                        (FakeClient(job_result={"error": "quota"}), "error")):
        out = AC.describe_music(client, song, probe_of(), SHA, shelf, uploader=uploader)
        assert out.get(key) and "windows" not in out
    assert not shelf.layer_ready(SHA, "music_desc")


def test_a_failed_upload_is_reported(song, tmp_path):
    def bad(path, url, mime_type="audio/aac"):
        return None, "connection reset"
    out = AC.describe_music(FakeClient(job_result=DESCRIBED), song, probe_of(), SHA, Shelf(str(tmp_path / "s")), uploader=bad)
    assert "upload failed" in out["error"] and "connection reset" in out["error"]


def test_cancelling_stops_before_anything_is_uploaded(song, tmp_path):
    with pytest.raises(cloud.Cancelled):
        AC.describe_music(FakeClient(job_result=DESCRIBED), song, probe_of(), SHA, Shelf(str(tmp_path / "s")), uploader=uploader,
                          should_cancel=lambda: True)
    assert uploader.sent == []


def test_a_mix_is_audited_with_its_context_and_never_cached(song, tmp_path):
    audit = {"overall": {"polish": 0.6}, "findings": [{"start": 4.0, "end": 6.0, "kind": "dialogue_buried", "severity": "medium", "note": "buried"}],
             "summary": "Voice is buried.", "usage": {"prompt_tokens": 900}}
    client = FakeClient(job_result=audit)
    ctx = {"form": "vlog", "vibe": "warm", "speech_ranges": [[1.0, 5.0]]}
    out = AC.listen(client, song, 20.0, ctx, uploader=uploader)
    assert out["findings"][0]["kind"] == "dialogue_buried" and out["summary"] == "Voice is buried."
    call = next(c for c in client.calls if c[0] == "listen")
    assert call[2] == 20.0 and call[3] == ctx and call[4] == "audio/aac"
    AC.listen(FakeClient(job_result=audit), song, 20.0, ctx, uploader=uploader)
    assert len(uploader.sent) == 2, "every audit is a fresh upload: the mix has changed"


@pytest.mark.parametrize("duration", [0, -3, 7200])
def test_a_bad_mix_length_is_refused_before_uploading(song, duration):
    out = AC.listen(FakeClient(), song, duration, {}, uploader=uploader)
    assert "between 0 and 3600" in out["error"] and uploader.sent == []
