"""Auditioning candidate music: fit scoring, the safe preview download, and ranking."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.media_index import audition as A, musicfit as MF  # noqa: E402
from test_media_index_audio import clicks, tone, write_wav  # noqa: E402


# ============================ the fit verdict ============================
def profile(**kw):
    base = {"bpm": 110.0, "seconds": 90.0, "energy_arc": [0.5, 0.5, 0.5], "phrase_points": [8.0, 24.0]}
    base.update(kw)
    return base


def test_nothing_asked_means_everything_fits_with_a_full_score():
    assert MF.music_fit(profile()) == {"fits": True, "score": 1.0, "met": 0, "asked": 0, "notes": []}


def test_the_score_is_the_share_of_requested_criteria_met():
    got = MF.music_fit(profile(bpm=90.0), bpm_min=100, bpm_max=130, seconds=60, energy="medium")
    assert got["asked"] == 3 and got["met"] == 2 and got["score"] == pytest.approx(0.667, abs=0.001) and got["fits"] is False
    assert any("outside 100-130" in n for n in got["notes"]) and any("long enough" in n for n in got["notes"]) and any("within the medium" in n for n in got["notes"])


@pytest.mark.parametrize("bpm,expected", [(100.0, True), (130.0, True), (99.0, False), (131.0, False), (None, False)])
def test_the_tempo_range_is_inclusive_and_a_track_with_no_tempo_fails_a_tempo_wish(bpm, expected):
    assert MF.music_fit(profile(bpm=bpm), bpm_min=100, bpm_max=130)["fits"] is expected


def test_a_one_sided_tempo_wish_works():
    assert MF.music_fit(profile(bpm=150.0), bpm_min=140)["fits"] is True and MF.music_fit(profile(bpm=150.0), bpm_max=140)["fits"] is False


def test_a_short_track_fits_only_when_it_has_phrase_points_to_end_or_loop_on():
    short = profile(seconds=30.0)
    assert MF.music_fit(short, seconds=60)["fits"] is True
    out = MF.music_fit(profile(seconds=30.0, phrase_points=[]), seconds=60)
    assert out["fits"] is False and "no clear phrase points" in out["notes"][0]


@pytest.mark.parametrize("arc,energy,fits", [([0.1, 0.2, 0.15], "low", True), ([0.5, 0.5, 0.5], "medium", True), ([0.8, 0.9, 0.85], "high", True),
                                             ([0.8, 0.9, 0.85], "low", False), ([0.1, 0.1, 0.1], "high", False),
                                             ([0.1, 0.2, 0.8, 0.9], "building", True), ([0.1, 0.9, 0.2], "building", False), ([0.9, 0.5, 0.1], "building", False)])
def test_energy_is_judged_from_the_measured_arc(arc, energy, fits):
    assert MF.music_fit(profile(energy_arc=arc), energy=energy)["fits"] is fits


def test_an_unknown_energy_word_or_a_missing_arc_asks_nothing():
    unknown = MF.music_fit(profile(), energy="spicy")
    assert unknown["asked"] == 0 and unknown["fits"] is True, "a word we do not know is not a failed criterion"
    assert MF.music_fit(profile(energy_arc=[]), energy="high")["asked"] == 0


def test_the_profile_carries_what_an_editor_needs():
    audio = {"tempo": {"bpm": 110.0, "beats": [1.0, 1.5, 2.0]}, "tempo_confidence": 0.8, "loudness": {"integrated_lufs": -14.0, "lra": 5.0},
             "dynamic_range_db": 20.0, "silence_ranges": [[1, 2]] * 20,
             "music": {"arc": [0.1, 0.9], "arc_seconds": 4, "sections": [{"label": "intro"}], "downbeats": list(range(40)), "phrase_points": list(range(40)), "brightness_hz": 900.0}}
    p = MF.profile_from_audio(audio, 61.234)
    assert p["seconds"] == 61.2 and p["bpm"] == 110.0 and p["beats"] == 3 and p["integrated_lufs"] == -14.0 and p["energy_arc"] == [0.1, 0.9]
    assert len(p["downbeats"]) == 16 and len(p["phrase_points"]) == 16 and len(p["silence_ranges"]) == 8
    empty = MF.profile_from_audio({}, 0)
    assert empty["bpm"] is None and empty["energy_arc"] == [] and empty["beats"] == 0


# ============================ preview URLs ============================
@pytest.mark.parametrize("url,ok", [("https://cdn.freesound.org/previews/1/1_1-hq.mp3", True), ("https://freesound.org/data/previews/x.mp3", True),
                                    ("http://cdn.freesound.org/x.mp3", False), ("https://evil.example/x.mp3", False),
                                    ("https://freesound.org.evil.example/x.mp3", False), ("https://notfreesound.org/x.mp3", False),
                                    ("file:///etc/passwd", False), ("https://127.0.0.1/x.mp3", False), ("", False), (None, False), ("not a url", False)])
def test_only_https_freesound_previews_are_fetched(url, ok):
    assert (A.url_problem(url) is None) is ok


# ============================ auditioning ============================
GOOD = "https://cdn.freesound.org/previews/1/"


@pytest.fixture
def sounds(tmp_path):
    files = {
        "groove120": write_wav(tmp_path / "g120.wav", clicks(24, 120)[0]),
        "groove80": write_wav(tmp_path / "g80.wav", clicks(24, 80)[0]),
        "drone": write_wav(tmp_path / "drone.wav", tone(24, 220.0, -20.0)),
    }

    def fetch(url, dest):
        key = url.rsplit("/", 1)[-1].split(".")[0]
        if key == "broken":
            return False, "connection reset"
        shutil.copy(files[key], dest)
        return True, ""
    return fetch


def cand(key, **kw):
    return {"id": key, "name": key, "preview_url": f"{GOOD}{key}.mp3", **kw}


def test_candidates_are_analysed_and_ranked_by_how_well_they_fit(sounds):
    out = A.audition([cand("drone", duration=90), cand("groove80", duration=90), cand("groove120", duration=90)], bpm_min=100, bpm_max=130, seconds=60,
                     fetch=sounds)
    ranked = out["ranked"]
    assert [r["id"] for r in ranked][0] == "groove120" and ranked[0]["fits"] is True and ranked[0]["rank"] == 1
    assert ranked[0]["bpm"] == pytest.approx(120.0, abs=2.0) and ranked[0]["seconds"] == 90.0 and ranked[0]["preview_seconds"] == pytest.approx(24.0, abs=0.5)
    assert all(not r["fits"] for r in ranked[1:]) and out["failed"] == [] and out["analysed"] == 3
    assert any("outside 100-130" in n for r in ranked[1:] for n in r["notes"]) and any("no steady tempo" in n for r in ranked[1:] for n in r["notes"])
    assert [r["rank"] for r in ranked] == [1, 2, 3]


def test_the_full_sound_length_not_the_previews_is_what_counts_for_duration(sounds):
    short = A.audition([cand("groove120", duration=20)], seconds=60, fetch=sounds)["ranked"][0]
    long = A.audition([cand("groove120", duration=120)], seconds=60, fetch=sounds)["ranked"][0]
    assert long["fits"] is True and any("long enough" in n for n in long["notes"])
    assert any("shorter than the 60 s needed" in n for n in short["notes"])


def test_one_bad_candidate_does_not_sink_the_others(sounds):
    out = A.audition([cand("broken"), {"id": "evil", "name": "evil", "preview_url": "https://evil.example/x.mp3"}, cand("groove120"), {"id": "nourl", "name": "n"}],
                     bpm_min=100, bpm_max=130, fetch=sounds)
    assert [r["id"] for r in out["ranked"]] == ["groove120"]
    errors = {f["id"]: f["error"] for f in out["failed"]}
    assert "connection reset" in errors["broken"] and "only freesound.org" in errors["evil"] and "https" in errors["nourl"]


def test_a_preview_that_is_not_audio_is_reported(tmp_path):
    def fetch(url, dest):
        Path(dest).write_bytes(b"not audio at all" * 50)
        return True, ""
    out = A.audition([cand("x")], fetch=fetch)
    assert out["ranked"] == [] and out["failed"][0]["error"] == "the preview has no audio"


def test_the_ranking_is_stable_and_fitting_tracks_come_first_even_with_a_lower_score_tie(sounds):
    out = A.audition([cand("groove80", name="b-second"), cand("groove80", name="a-first")], bpm_min=100, bpm_max=130, fetch=sounds)
    assert [r["name"] for r in out["ranked"]] == ["a-first", "b-second"], "ties are ordered by name"


def test_only_the_first_dozen_candidates_are_analysed(sounds):
    out = A.audition([cand("groove120", name=f"s{i:02d}") for i in range(20)], fetch=sounds)
    assert out["analysed"] == A.MAX_CANDIDATES and len(out["ranked"]) == 12 and out["failed"] == []
    assert [r["name"] for r in out["ranked"]] == [f"s{i:02d}" for i in range(12)], "the first twelve, even when they share an id"


def test_nothing_to_audition_is_an_empty_result(sounds):
    assert A.audition([], fetch=sounds) == {"ranked": [], "failed": [], "analysed": 0}
    assert A.audition(["junk", 3, None], fetch=sounds)["analysed"] == 0


def test_temporary_files_are_cleaned_up(sounds, tmp_path, monkeypatch):
    import tempfile
    seen = []
    real = tempfile.TemporaryDirectory

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self.name)
    monkeypatch.setattr(A.tempfile, "TemporaryDirectory", Spy)
    A.audition([cand("groove120")], fetch=sounds)
    assert seen and not Path(seen[0]).exists()


# ============================ the download itself ============================
class FakeResponse:
    def __init__(self, url, chunks, status=200):
        self.url, self._chunks, self.status = url, chunks, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def iter_content(self, size):
        yield from self._chunks


def patch_get(monkeypatch, response):
    import requests
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(requests, "get", get)
    return seen


def test_a_preview_is_downloaded_to_the_destination(monkeypatch, tmp_path):
    seen = patch_get(monkeypatch, FakeResponse(GOOD + "a.mp3", [b"abc", b"def"]))
    dest = tmp_path / "a.mp3"
    assert A.default_fetch(GOOD + "a.mp3", str(dest)) == (True, "") and dest.read_bytes() == b"abcdef"
    assert seen["stream"] is True and seen["timeout"] == A.TIMEOUT_SECONDS


def test_a_redirect_to_another_host_is_refused_before_anything_is_written(monkeypatch, tmp_path):
    patch_get(monkeypatch, FakeResponse("https://evil.example/stolen.mp3", [b"payload"]))
    dest = tmp_path / "a.mp3"
    ok, why = A.default_fetch(GOOD + "a.mp3", str(dest))
    assert ok is False and "redirected away" in why and not dest.exists()


def test_an_oversized_download_is_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "MAX_BYTES", 10)
    patch_get(monkeypatch, FakeResponse(GOOD + "a.mp3", [b"12345", b"67890", b"x"]))
    ok, why = A.default_fetch(GOOD + "a.mp3", str(tmp_path / "a.mp3"))
    assert ok is False and "larger than" in why


@pytest.mark.parametrize("response,fragment", [(FakeResponse(GOOD + "a.mp3", [], 200), "empty download"), (FakeResponse(GOOD + "a.mp3", [b"x"], 404), "HTTP 404"),
                                               (RuntimeError("timed out"), "timed out")])
def test_empty_failed_and_erroring_downloads_are_reported(monkeypatch, tmp_path, response, fragment):
    patch_get(monkeypatch, response)
    ok, why = A.default_fetch(GOOD + "a.mp3", str(tmp_path / "a.mp3"))
    assert ok is False and fragment in why


def test_candidates_never_share_a_temporary_file_even_with_the_same_id(sounds):
    seen = []

    def spying(url, dest):
        seen.append(dest)
        return sounds(url, dest)
    A.audition([cand("groove120") for _ in range(6)], fetch=spying)
    assert len(seen) == 6 and len(set(seen)) == 6
