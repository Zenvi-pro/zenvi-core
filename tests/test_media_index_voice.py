"""The exact edges of a voice, the pauses inside it, and takes of the same line said more than once."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index import voice as V  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

SR = V.SAMPLE_RATE


def tone(seconds, level=0.3, hz=180.0):
    t = np.arange(int(seconds * SR)) / SR
    return (level * np.sin(2 * np.pi * hz * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t) ** 2)).astype(np.float32)


def silence(seconds, noise=0.002, seed=1):
    return (np.random.default_rng(seed).standard_normal(int(seconds * SR)) * noise).astype(np.float32)


def line(*parts):
    return np.concatenate(parts).astype(np.float32)


# ============================ the loudness curve ============================
def test_a_loudness_curve_has_one_value_per_hop_and_follows_the_level():
    db = V.frame_db(line(silence(1.0), tone(1.0), silence(1.0)))
    assert db.size == pytest.approx(300, abs=3) and db[:50].mean() < db[120:180].mean() - 30
    assert V.frame_db(np.zeros(100, np.float32)).size == 0


def test_runs_are_found_at_both_ends():
    assert V._runs(np.array([1, 1, 0, 0, 1, 0, 1, 1], bool)) == [(0, 2), (4, 5), (6, 8)] and V._runs(np.array([], bool)) == []


# ============================ the edges ============================
def test_the_voice_starts_and_stops_where_it_does_to_within_a_few_hundredths_of_a_second():
    out = V.voice_edges(line(silence(1.0), tone(2.0), silence(1.5)))
    assert out["found"] is True and out["method"] == "energy"
    assert out["start"] == pytest.approx(1.0, abs=0.05) and out["end"] == pytest.approx(3.0, abs=0.05) and out["contrast_db"] > 30
    assert out["pauses"] == [] and len(out["spans"]) == 1


def test_times_are_in_the_files_own_seconds_when_the_audio_starts_part_way_in():
    out = V.voice_edges(line(silence(1.0), tone(2.0), silence(1.0)), offset=40.0)
    assert out["start"] == pytest.approx(41.0, abs=0.05) and out["end"] == pytest.approx(43.0, abs=0.05)


def test_a_pause_inside_the_voice_is_reported_and_a_short_dip_is_not():
    out = V.voice_edges(line(silence(0.5), tone(1.0), silence(0.6), tone(1.0), silence(0.05), tone(0.5), silence(0.5)))
    assert out["pauses"] == [[pytest.approx(1.5, abs=0.06), pytest.approx(2.1, abs=0.06)]]
    assert out["start"] == pytest.approx(0.5, abs=0.05) and out["end"] == pytest.approx(3.65, abs=0.06), "the 50 ms dip is inside a word"


def test_noise_before_the_line_is_taken_for_the_voice_unless_the_words_say_where_to_look():
    audio = line(silence(0.3), tone(0.1, level=0.4, hz=3000), silence(0.6), tone(2.0), silence(1.0))
    blind = V.voice_edges(audio)
    assert blind["start"] == pytest.approx(0.3, abs=0.06), "without words, the first loud thing is the start"
    words = [{"startSec": 1.0, "endSec": 1.4, "text": "hello"}, {"startSec": 2.6, "endSec": 3.0, "text": "there."}]
    told = V.voice_edges(audio, words=words)
    assert told["start"] == pytest.approx(1.0, abs=0.06) and told["end"] == pytest.approx(3.0, abs=0.06)
    assert told["recogniser_start"] == 1.0 and told["start_offset"] == pytest.approx(0.0, abs=0.06) and told["end_offset"] == pytest.approx(0.0, abs=0.06)


def test_the_recogniser_being_late_is_visible_in_the_offsets():
    audio = line(silence(1.0), tone(2.0), silence(1.0))
    out = V.voice_edges(audio, words=[{"startSec": 1.2, "endSec": 2.7, "text": "late"}])
    assert out["start_offset"] == pytest.approx(-0.2, abs=0.08) and out["end_offset"] == pytest.approx(0.3, abs=0.08)


@pytest.mark.parametrize("name,audio", [("silence", silence(3.0)), ("steady hiss", silence(3.0, noise=0.05)), ("too short", np.zeros(100, np.float32))])
def test_no_voice_means_found_is_false(name, audio):
    assert V.voice_edges(audio)["found"] is False, name


def test_a_click_is_not_speech():
    assert V.voice_edges(line(silence(1.0), tone(0.02, level=0.5), silence(1.0)))["found"] is False


def test_real_decoded_audio_gives_the_same_answer(tmp_path):
    from eval import corpus
    path, truth = corpus.voice_bursts(spans=((1.0, 3.0), (4.5, 6.0)), seconds=8.0)
    samples = V.read_audio(str(path), 0.5, 7.0)
    out = V.voice_edges(samples, offset=0.5)
    assert out["start"] == pytest.approx(1.0, abs=0.06) and out["end"] == pytest.approx(6.0, abs=0.06)
    assert out["pauses"] and out["pauses"][0][0] == pytest.approx(3.0, abs=0.08) and out["pauses"][0][1] == pytest.approx(4.5, abs=0.08)
    with pytest.raises(RuntimeError):
        V.read_audio(str(tmp_path / "missing.wav"), 0, 1)


# ============================ takes of the same line ============================
def s(start, end, text, speaker=None):
    return {"start": start, "end": end, "text": text, "speaker": speaker}


def w(start, end, text):
    return {"startSec": start, "endSec": end, "text": text}


LINE = "So today we are going to make fresh pasta from scratch."


def test_a_line_said_three_times_is_one_group_in_time_order():
    sents = [s(0, 4, LINE), s(6, 9, "Okay, let me get the flour."), s(12, 16, "So today we're going to make fresh pasta from scratch."), s(30, 34, LINE)]
    groups = V.retake_groups(sents)
    assert len(groups) == 1 and [t["start"] for t in groups[0]["takes"]] == [0, 12, 30] and [t["index"] for t in groups[0]["takes"]] == [1, 2, 3]
    assert groups[0]["kind"] == "repeat" and groups[0]["takes"][0]["pause_after"] == 2.0 and groups[0]["takes"][-1]["pause_after"] is None


def test_a_false_start_is_the_same_line_beginning_again():
    groups = V.retake_groups([s(0, 2, "So today we are going to"), s(3, 7, LINE)])
    assert len(groups) == 1 and groups[0]["kind"] == "false_start" and len(groups[0]["takes"]) == 2


def test_different_lines_speakers_or_distant_times_are_not_retakes():
    assert V.retake_groups([s(0, 4, LINE), s(5, 9, "Next we mix the eggs into a well of flour.")]) == []
    assert V.retake_groups([s(0, 4, LINE, "A"), s(6, 10, LINE, "B")]) == [], "two people saying the same thing"
    assert V.retake_groups([s(0, 4, LINE), s(500, 504, LINE)]) == [], "minutes apart is a callback, not a retake"
    assert V.retake_groups([s(0, 2, "Yes it is."), s(3, 5, "Yes it is.")]) == [], "too short to tell"
    assert V.retake_groups([]) == []


def test_the_threshold_decides_how_alike_is_alike():
    near = [s(0, 4, LINE), s(6, 10, "So today we are going to cook some pasta by hand.")]
    assert V.retake_groups(near, threshold=0.6) != [] and V.retake_groups(near, threshold=0.95) == []


def test_each_take_carries_its_fillers_completeness_and_pace():
    sents = [s(0, 5, "So um today we are going to make fresh pasta from scratch"), s(8, 12, LINE)]
    words = [w(0.0, 0.3, "So"), w(0.4, 0.6, "um"), w(0.7, 1.0, "today"), w(8.0, 8.3, "So"), w(8.4, 8.7, "today")]
    g = V.retake_groups(sents, words)[0]
    first, second = g["takes"]
    assert [f["word"] for f in first["fillers"]] == ["um"] and first["fillers"][0]["start"] == 0.4 and second["fillers"] == []
    assert first["complete"] is False and second["complete"] is True and second["words"] == 2 and second["seconds"] == 4.0


def test_the_likely_best_take_is_the_clean_finished_one_and_says_why():
    sents = [s(0, 4, LINE), s(6, 11, "So um today we are going to make fresh pasta from scratch."), s(14, 18, "So today we are going to make fresh pasta from")]
    words = [w(6.5, 6.8, "um")]
    g = V.retake_groups(sents, words)[0]
    assert g["likely_best"] == 1 and "no filler" in g["why"] and "finishes its sentence" in g["why"]


def test_on_a_tie_the_later_take_is_the_likely_best():
    g = V.retake_groups([s(0, 4, LINE), s(6, 10, LINE)])[0]
    assert g["likely_best"] == 2 and "later of the equally clean takes" in g["why"]


def test_if_nothing_finished_the_sentence_the_least_filler_take_still_gets_named():
    g = V.retake_groups([s(0, 4, "So today we are going to make fresh pasta from"), s(6, 10, "So today we are going to make fresh pasta from")], [w(0.5, 0.8, "uh")])[0]
    assert g["likely_best"] == 2


def test_levels_come_from_the_function_given():
    g = V.retake_groups([s(0, 4, LINE), s(6, 10, LINE)], level_db=lambda a, b: -20.0 if a < 5 else None)[0]
    assert g["takes"][0]["level_db"] == -20.0 and g["takes"][1]["level_db"] is None


def test_groups_are_listed_in_the_order_they_first_appear():
    other = "Now we crack the eggs into the centre of the flour well."
    groups = V.retake_groups([s(0, 4, other), s(5, 9, LINE), s(10, 14, other), s(15, 19, LINE)])
    assert [g["takes"][0]["text"] for g in groups] == [other, LINE]


# ============================ the tools ============================
class F(SimpleNamespace):
    pass


def call(name, **kw):
    out = REGISTRY[name].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {})


@pytest.fixture
def library(monkeypatch, tmp_path):
    from classes.editor_tools import media_index_tools_precision as P
    from eval import corpus
    path, _ = corpus.voice_bursts(spans=((1.0, 3.0), (4.5, 6.0)), seconds=8.0)
    shelf = Shelf(str(tmp_path / "shelf"))
    sha = "a" * 64
    words = [{"startSec": 1.1, "endSec": 1.5, "text": "Hello"}, {"startSec": 2.5, "endSec": 3.1, "text": "everyone."}]
    shelf.write_json(sha, "speech.json", {"words": words, "sentences": [s(1.1, 3.1, "Hello everyone.")]})
    shelf.set_layer(sha, "speech", version=1, status="ready")
    take_sha = "b" * 64
    sents = [s(0, 4, LINE), s(6, 10, "So um today we are going to make fresh pasta from scratch."), s(20, 24, LINE)]
    shelf.write_json(take_sha, "speech.json", {"words": [w(6.5, 6.8, "um")], "sentences": sents})
    shelf.set_layer(take_sha, "speech", version=1, status="ready")
    files = [F(id="V1", data={"name": "voice.wav", "path": str(path), "media_type": "audio", "fingerprint": {"sha256": sha}}),
             F(id="T1", data={"name": "pasta.mp4", "path": "/m/pasta.mp4", "media_type": "video", "fingerprint": {"sha256": take_sha}}),
             F(id="N1", data={"name": "silent.mp4", "path": "/m/silent.mp4", "media_type": "video", "fingerprint": None})]
    monkeypatch.setattr(P, "default_shelf", lambda: shelf)
    monkeypatch.setattr(P, "_all_files", lambda: files)
    monkeypatch.setattr(P, "resolve_files", lambda ids=None, query="": [x for x in files if x.id in (ids or [])])
    monkeypatch.setattr(P, "_index_for", lambda f, sh=None: None)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    return SimpleNamespace(files=files, shelf=shelf, sha=sha, words=words)


def test_the_tool_finds_the_voice_edges_pauses_and_the_transcripts_error(library):
    head, r = call("get_voice_edges_tool", file_ids=["V1"], start_seconds=0.8, end_seconds=6.5)
    v = r["voice"]
    assert v["found"] and v["start"] == pytest.approx(1.0, abs=0.06) and r["cached"] is False and r["changed"] is False
    assert "Voice from 1.00" in head and "pause" in head
    assert v["transcript"] == "Hello everyone." and v["recogniser_start"] == 1.1 and v["start_offset"] == pytest.approx(-0.1, abs=0.06)
    assert "after the end" in r["advice"]


def test_asking_again_is_answered_from_the_shelf(library, monkeypatch):
    call("get_voice_edges_tool", file_ids=["V1"], start_seconds=0.8, end_seconds=6.5)
    from classes.media_index import voice
    monkeypatch.setattr(voice, "read_audio", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not decode again")))
    _, again = call("get_voice_edges_tool", file_ids=["V1"], start_seconds=0.8, end_seconds=6.5)
    assert again["cached"] is True and again["voice"]["found"]


def test_a_stretch_with_no_voice_says_so(library):
    head, r = call("get_voice_edges_tool", file_ids=["V1"], start_seconds=6.5, end_seconds=7.9)
    assert head == "No voice stands out from the noise in that stretch." and r["voice"]["found"] is False


@pytest.mark.parametrize("kw,fragment", [(dict(start_seconds=None, end_seconds=3.0), "start_seconds"), (dict(start_seconds=2.0, end_seconds=2.1), "0.2 s apart"),
                                         (dict(start_seconds=0.0, end_seconds=500.0), "180 seconds")])
def test_bad_ranges_are_refused(library, kw, fragment):
    assert fragment in REGISTRY["get_voice_edges_tool"].func(file_ids=["V1"], **kw)


def test_the_retakes_tool_lists_groups_with_the_file_and_a_likely_best(library):
    head, r = call("find_retakes_tool")
    assert head == "1 line(s) said more than once across 1 file(s) with a transcript" and r["files_with_transcript"] == 1, "a file with one sentence has nothing to repeat"
    g = r["groups"][0]
    assert g["file_id"] == "T1" and g["name"] == "pasta.mp4" and [t["start"] for t in g["takes"]] == [0, 6, 20] and g["likely_best"] == 3
    assert [f["word"] for f in g["takes"][1]["fillers"]] == ["um"] and "heuristic" in r["note"]


def test_no_retakes_is_said_plainly(library):
    head, r = call("find_retakes_tool", file_ids=["V1"])
    assert head == "no retakes found in 0 file(s) with a transcript" and r["groups"] == []
    assert call("find_retakes_tool", min_similarity=1.0)[1]["groups"][0]["takes"][0]["start"] == 0, "an exact repeat still matches at 1.0"


def test_voice_outside_the_range_asked_about_is_not_reported():
    audio = line(silence(0.5), tone(1.0), silence(1.0), tone(1.0), silence(0.5))      # voice at 0.5-1.5 and 2.5-3.5
    only_second = V.voice_edges(audio, within=(2.0, 3.8))
    assert only_second["start"] == pytest.approx(2.5, abs=0.06) and len(only_second["spans"]) == 1
    assert V.voice_edges(audio, within=(1.7, 2.3))["found"] is False
    assert V.voice_edges(audio, within=(0.0, 4.0))["spans"].__len__() == 2


def test_slowly_wobbling_background_noise_is_not_a_voice():
    t = np.arange(int(4 * SR)) / SR
    hiss = np.random.default_rng(2).standard_normal(t.size) * 0.01 * (10 ** (2.5 * np.sin(2 * np.pi * 0.5 * t) / 20.0))      # +-2.5 dB every 2 s
    out = V.voice_edges(hiss.astype(np.float32))
    assert out["found"] is False and out["contrast_db"] < V.MIN_CONTRAST_DB


def test_a_short_dip_inside_a_word_does_not_split_the_voice_in_two():
    out = V.voice_edges(line(silence(0.5), tone(1.0), silence(0.6), tone(1.0), silence(0.05), tone(0.5), silence(0.5)))
    assert len(out["spans"]) == 2, "the 50 ms dip is bridged; the 600 ms pause is not"


def test_a_click_just_before_the_voice_is_not_the_start_of_it():
    out = V.voice_edges(line(silence(0.4), tone(0.025, level=0.5, hz=2500), silence(0.6), tone(2.0), silence(1.0)))
    assert out["start"] == pytest.approx(1.0, abs=0.06) and len(out["spans"]) == 1
