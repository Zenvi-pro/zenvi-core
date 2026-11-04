"""Voiceprints: the features, grouping speech by speaker, the voice registry, linking voices to faces, and the diarizer hook."""

from __future__ import annotations

import json
import re
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes import info  # noqa: E402
from classes.media_index import people as P, voice_diarize, voiceprint as V  # noqa: E402
from eval import corpus  # noqa: E402

SR = V.SAMPLE_RATE
SHA1, SHA2 = "1" * 64, "2" * 64


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"), raising=False)


def unit(i, dims=256):
    v = np.zeros(dims, np.float32)
    v[i] = 1.0
    return v


def words_at(*spans, step=0.3):
    out = []
    for a, b in spans:
        out += [SimpleNamespace(startSec=float(t), endSec=float(t) + 0.25, text="w") for t in np.arange(a, b - 0.3, step)]
    return out


def write_wav(path, samples):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.asarray(samples) * 32767).astype("<i2").tobytes())


# ============================ features ============================
def test_the_mel_filters_are_triangles_ordered_from_low_to_high_pitch():
    banks = V.mel_banks()
    assert banks.shape == (V.MELS, V.FFT // 2 + 1) and banks.min() >= 0.0 and banks.max() <= 1.0 + 1e-9
    peaks = banks.argmax(axis=1)
    assert (np.diff(peaks) >= 0).all() and peaks[0] <= 2 and peaks[-1] >= 240
    assert (banks[:, -1] == 0).all(), "the Nyquist bin is left out, as Kaldi does"
    assert banks[:, :2].sum() < banks.sum() * 0.02, "nothing below 20 Hz"


def test_features_have_one_row_per_hop_a_zero_mean_per_band_and_follow_the_pitch():
    n = 3 * SR
    t = np.arange(n) / SR
    audio = np.where(t < 1.5, np.sin(2 * np.pi * 500 * t), np.sin(2 * np.pi * 4000 * t)).astype(np.float32) * 0.3
    f = V.fbank(audio)
    assert f.shape == (1 + (n - V.FRAME) // V.SHIFT, V.MELS) and np.abs(f.mean(axis=0)).max() < 1e-4 and np.isfinite(f).all()
    first, second = f[:100].mean(axis=0), f[-100:].mean(axis=0)
    assert int(first.argmax()) < 30 < int(second.argmax()), "the low tone lights low bands, then the high tone lights high bands"
    assert V.fbank(np.zeros(100, np.float32)).shape == (0, V.MELS)


class FakeModel:
    def __init__(self, vec):
        self.vec, self.seen = vec, []

    def get_inputs(self):
        return [SimpleNamespace(name="feats")]

    def run(self, _n, feed):
        self.seen.append(feed["feats"].shape)
        return [self.vec[None] * 2.0]


def test_a_voiceprint_needs_a_second_of_speech_and_has_length_one():
    model = FakeModel(unit(3))
    assert V.embed(model, np.zeros(int(0.5 * SR), np.float32)) is None and model.seen == []
    assert V.embed(model, np.random.default_rng(1).normal(0, 0.1, int(0.8 * SR)).astype(np.float32)) is None and model.seen == [], "under a second is not used even when it has enough frames"
    v = V.embed(model, (np.random.default_rng(0).normal(0, 0.1, 2 * SR)).astype(np.float32))
    assert v.shape == (256,) and float(np.linalg.norm(v)) == pytest.approx(1.0) and model.seen[0][0] == 1 and model.seen[0][2] == 80
    assert V.embed(FakeModel(np.zeros(256, np.float32)), np.random.default_rng(0).normal(0, 0.1, 2 * SR).astype(np.float32)) is None


def test_only_16k_mono_16bit_audio_is_read(tmp_path):
    ok = tmp_path / "ok.wav"
    bad = tmp_path / "bad.wav"
    for path, rate in ((ok, SR), (bad, 44100)):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.ones(1000) * 3277).astype("<i2").tobytes())
    assert V.read_wav16k(str(ok)).shape == (1000,) and V.read_wav16k(str(ok))[0] == pytest.approx(0.1, abs=1e-3)
    with pytest.raises(RuntimeError, match="16 kHz mono"):
        V.read_wav16k(str(bad))
    with pytest.raises(RuntimeError, match="could not read"):
        V.read_wav16k(str(tmp_path / "nope.wav"))


# ============================ who spoke when ============================
def test_speech_is_split_at_pauses_and_at_the_longest_stretch():
    spans = ((0, 4), (5.5, 9), (10.2, 24))
    segs = V.segments_from_words(words_at(*spans))
    assert [round(s["start"], 1) for s in segs][:3] == [0.0, 5.5, 10.2], "a pause over 0.8 s ends a stretch"
    assert len(segs) >= 5 and all(s["end"] - s["start"] <= V.SEGMENT_MAX_SECONDS + 0.3 for s in segs), "and a long run is cut at the longest stretch"
    assert sum(len(s["words"]) for s in segs) == len(words_at(*spans)) and V.segments_from_words([]) == []
    assert len(V.segments_from_words(words_at((0, 2.5), (2.9, 5.5)))) == 1, "a short breath does not end a stretch"


def test_voices_are_grouped_by_likeness_numbered_in_order_heard_and_capped():
    a, b = unit(0), unit(1)
    near_a = (a + 0.1 * unit(5)) / np.linalg.norm(a + 0.1 * unit(5))
    assert V.cluster([a, b, near_a, b]) == [0, 1, 0, 1] and V.cluster([]) == [] and V.cluster([a]) == [0]
    many = [unit(i) for i in range(6)]
    assert sorted(set(V.cluster(many, max_clusters=3))) == [0, 1, 2], "never more speakers than allowed: the closest are merged"
    assert V.cluster([a, near_a], threshold=0.999) == [0, 1] and V.cluster([a, near_a], threshold=0.5) == [0, 0]


@pytest.fixture
def by_level(monkeypatch):
    """Stand-in voiceprints: the speaker is the loudness of the stretch (0.1 -> voice 0, 0.3 -> voice 1, 0.6 -> voice 2)."""
    def fake(_session, samples):
        if len(samples) < SR:
            return None
        rms = float(np.sqrt(np.mean(samples ** 2)))
        return unit({1: 0, 3: 1, 6: 2}[int(round(rms * 10 * 1.4142))])
    monkeypatch.setattr(V, "embed", fake)


def tone(seconds, level):
    t = np.arange(int(seconds * SR)) / SR
    return (np.sin(2 * np.pi * 300 * t) * level).astype(np.float32)


def test_each_word_gets_the_speaker_of_its_stretch_and_speakers_are_ordered_by_first_heard(by_level):
    parts = [(0.1, 6), (0.3, 6), (0.1, 5)]
    gap = np.zeros(SR, np.float32)
    audio = np.concatenate([np.concatenate([tone(s, lvl), gap]) for lvl, s in parts])
    spans, at = [], 0.0
    for _lvl, s in parts:
        spans.append((at, at + s))
        at += s + 1.0
    words = words_at(*spans)
    got = V.speakers_of(object(), audio, words)
    first = [lab for w, lab in zip(words, got["labels"]) if w.startSec < 6]
    second = [lab for w, lab in zip(words, got["labels"]) if 7 <= w.startSec < 12]
    third = [lab for w, lab in zip(words, got["labels"]) if w.startSec >= 14]
    assert set(first) == {0} and set(second) == {1} and set(third) == {0} and got["unused"] == 0
    assert [s["index"] for s in got["speakers"]] == [0, 1] and got["speakers"][0]["seconds"] == pytest.approx(11.0, abs=1.5) and got["speakers"][0]["vector"].shape == (256,)


def test_a_word_in_a_stretch_too_short_to_tell_takes_the_nearest_speaker_and_all_short_speech_is_reported(by_level):
    audio = np.concatenate([tone(6, 0.1), np.zeros(SR, np.float32), tone(0.4, 0.3), np.zeros(SR, np.float32), tone(6, 0.3)])
    words = words_at((0, 6), (7, 7.4), (8.4, 14.4))
    got = V.speakers_of(object(), audio, words)
    short = [lab for w, lab in zip(words, got["labels"]) if 7 <= w.startSec < 7.4]
    assert short and got["unused"] == len(short) and set(short) <= {0, 1}
    few = words_at((0, 0.5))
    assert V.speakers_of(object(), tone(0.5, 0.1), few) == {"labels": [0] * len(few), "speakers": [], "unused": len(few)}


def test_a_cancelled_listen_stops_with_nothing_saved(by_level):
    with pytest.raises(InterruptedError):
        V.speakers_of(object(), tone(6, 0.1), words_at((0, 6)), should_cancel=lambda: True)


# ============================ the registry ============================
def vscan(*speakers, segments=()):
    return {"version": P.VERSION, "duration": 60.0, "segments": [{"start": a, "end": b, "speaker": s} for a, b, s in segments],
            "speakers": [{"id": sid, "seconds": 20.0, "embedding": P.enc(v)} for sid, v in speakers]}


def test_a_voice_is_a_match_unsure_or_new_by_the_voice_lines():
    reg = P.load_registry()
    p = P.new_person(reg, voice=unit(0))
    assert P.match_voice(reg, unit(0))["status"] == "match" and P.match_voice(reg, unit(0))["person"] == p["id"]
    mid = V.UNSURE_VOICE + 0.03
    unsure = P.match_voice(reg, np.array([mid, np.sqrt(1 - mid ** 2)] + [0] * 254, np.float32))
    assert unsure["status"] == "unsure" and unsure["person"] is None and unsure["candidate"] == p["id"]
    assert P.match_voice(reg, unit(9))["status"] == "new"
    assert P.match(reg, unit(0, 128))["status"] == "new", "a voice-only person has no face to match"


def test_the_voice_lines_are_the_ones_measured_and_recorded():
    rec = json.loads((Path(__file__).parent / "eval" / "results" / "voice_calibration.json").read_text())
    assert rec["chosen"] == {**rec["chosen"], "IN_FILE": V.IN_FILE, "SAME_VOICE": V.SAME_VOICE, "UNSURE_VOICE": V.UNSURE_VOICE}
    assert rec["by_threshold"][f"{V.SAME_VOICE:.2f}"]["false_merge"] < 0.001 and rec["by_threshold"][f"{V.SAME_VOICE:.2f}"]["same_accepted"] > 0.99
    assert V.UNSURE_VOICE < V.IN_FILE < V.SAME_VOICE


def test_new_voices_become_unnamed_people_and_a_returning_voice_is_the_same_person():
    assert P.assign_new_voices(SHA1, vscan(("S1", unit(0)), ("S2", unit(1)))) == {"matched": 0, "new": 2, "unsure": 0}
    assert P.assign_new_voices(SHA2, vscan(("S1", unit(1)), ("S2", unit(2)))) == {"matched": 1, "new": 1, "unsure": 0}
    reg = P.load_registry()
    assert [(p["id"], len(p["voices"]), p["exemplars"]) for p in reg["people"]] == [("P1", 1, []), ("P2", 1, []), ("P3", 1, [])]
    assert [r["person"] for r in P.resolve_speakers(SHA2, vscan(("S1", unit(1)), ("S2", unit(2))), reg)] == ["P2", "P3"]


def test_a_possible_voice_is_reported_unsure_and_never_merged_and_a_pinned_one_is_left_alone():
    mid = V.UNSURE_VOICE + 0.03
    maybe = np.array([mid, np.sqrt(1 - mid ** 2)] + [0] * 254, np.float32)
    P.assign_new_voices(SHA1, vscan(("S1", unit(0))))
    assert P.assign_new_voices(SHA2, vscan(("S1", maybe))) == {"matched": 0, "new": 0, "unsure": 1} and len(P.load_registry()["people"]) == 1
    P._write_json(P._voice_path(SHA2), vscan(("S1", maybe)))
    P.pin_speaker(SHA2, "S1", "P1")
    assert P.assign_new_voices(SHA2, vscan(("S1", maybe))) == {"matched": 0, "new": 0, "unsure": 0}, "a corrected voice is not matched again"
    assert P.resolve_speakers(SHA2, vscan(("S1", maybe)), P.load_registry())[0]["status"] == "pinned"
    with pytest.raises(KeyError):
        P.pin_speaker(SHA2, "S9", "P1")
    with pytest.raises(KeyError):
        P.pin_speaker(SHA2, "S1", "P99")


def test_merging_a_voice_person_into_a_face_person_joins_voices_and_pins():
    reg = P.load_registry()
    face = P.new_person(reg, unit(0, 128))
    voice = P.new_person(reg, voice=unit(0))
    reg["voice_pins"]["x:S1"] = voice["id"]
    P.save_registry(reg)
    out = P.merge_people(face["id"], voice["id"])
    reg = P.load_registry()
    assert out["kept"] == face["id"] and len(reg["people"]) == 1 and len(reg["people"][0]["voices"]) == 1 and reg["voice_pins"] == {"x:S1": face["id"]}
    assert P.match_voice(reg, unit(0))["person"] == face["id"] and P.match(reg, unit(0, 128))["person"] == face["id"]


def test_who_is_speaking_when_and_in_which_shots():
    segments = [(0.0, 10.0, "S1"), (12.0, 20.0, "S2"), (21.0, 30.0, "S1")]
    scan = vscan(("S1", unit(0)), ("S2", unit(1)), segments=segments)
    P.assign_new_voices(SHA1, scan)
    reg = P.load_registry()
    assert P.voice_at(SHA1, scan, reg, 5.0)["person"] == "P1" and P.voice_at(SHA1, scan, reg, 15.0)["person"] == "P2" and P.voice_at(SHA1, scan, reg, 40.0) is None
    assert P.voice_at(SHA1, scan, reg, 10.3)["person"] == "P1", "a breath after a stretch still counts"
    shots = [{"id": 0, "start": 0.0, "end": 11.0}, {"id": 1, "start": 11.0, "end": 20.5}, {"id": 2, "start": 20.5, "end": 40.0}]
    assert P.speaking_shots(SHA1, scan, reg, ["P1"], shots) == {0: 1.0, 2: 1.0} and P.speaking_shots(SHA1, scan, reg, ["P2"], shots) == {1: 1.0}
    assert P.speaking_seconds(reg, {SHA1: scan}) == {"P1": 19.0, "P2": 8.0}


def faces_scan(*appearances):
    """appearances: (start, end, vector) one face each, in separate shots."""
    shots = [{"id": i, "start": a, "end": b} for i, (a, b, _v) in enumerate(appearances)]
    ds = [{"t": a + 0.5, "box": [0.4, 0.2, 0.1, 0.2], "px": 100, "score": 0.9, "vec": v} for a, b, v in appearances] + \
         [{"t": b - 0.5, "box": [0.4, 0.2, 0.1, 0.2], "px": 100, "score": 0.9, "vec": v} for a, b, v in appearances]
    return P.build_scan(ds, shots, len(ds), appearances[-1][1])


def test_a_voice_is_linked_to_the_face_that_is_alone_on_screen_while_it_speaks():
    face = faces_scan((0.0, 20.0, unit(0, 128)), (20.0, 40.0, unit(1, 128)))
    voice = vscan(("S1", unit(0)), ("S2", unit(1)), segments=[(0.0, 18.0, "S1"), (21.0, 39.0, "S2")])
    P.assign_new_faces(SHA1, face)
    P.assign_new_voices(SHA1, voice)
    reg = P.load_registry()
    links = P.voice_face_links(reg, {SHA1: (face, voice)})
    face_of = {t["person"] for t in P.resolve_tracks(SHA1, face, reg)}
    assert len(links) == 2 and all(k["ratio"] == 1.0 and k["seconds"] == 18.0 and k["confidence"] == pytest.approx(0.6) for k in links)
    assert {k["face"] for k in links} == face_of and len({k["voice"] for k in links}) == 2


def test_no_link_when_there_is_too_little_time_two_faces_are_on_screen_or_they_are_already_one_person():
    face = faces_scan((0.0, 20.0, unit(0, 128)))
    short = vscan(("S1", unit(0)), segments=[(0.0, 4.0, "S1")])
    P.assign_new_faces(SHA1, face)
    P.assign_new_voices(SHA1, short)
    assert P.voice_face_links(P.load_registry(), {SHA1: (face, short)}) == [], "4 s is under the minimum"
    both = faces_scan((0.0, 30.0, unit(0, 128)))
    both["tracks"].append({**both["tracks"][0], "id": "s0t2", "embedding": P.enc(unit(1, 128))})
    long = vscan(("S1", unit(0)), segments=[(0.0, 28.0, "S1")])
    P.delete_all()
    P.assign_new_faces(SHA1, both)
    P.assign_new_voices(SHA1, long)
    assert P.voice_face_links(P.load_registry(), {SHA1: (both, long)}) == [], "with two faces on screen it is not known whose voice it is"
    P.delete_all()
    one = faces_scan((0.0, 30.0, unit(0, 128)))
    P.assign_new_faces(SHA1, one)
    P.assign_new_voices(SHA1, long)
    link = P.voice_face_links(P.load_registry(), {SHA1: (one, long)})[0]
    P.merge_people(link["face"], link["voice"])
    assert P.voice_face_links(P.load_registry(), {SHA1: (one, long)}) == [], "once merged they are one person"


def test_a_voice_that_mostly_speaks_with_no_one_on_screen_is_not_linked():
    face = faces_scan((0.0, 30.0, unit(0, 128)))
    voice = vscan(("S1", unit(0)), segments=[(0.0, 12.0, "S1"), (40.0, 80.0, "S1")])
    P.assign_new_faces(SHA1, face)
    P.assign_new_voices(SHA1, voice)
    assert P.voice_face_links(P.load_registry(), {SHA1: (face, voice)}) == [], "12 s with the face but 40 s speaking over nothing: a narrator, not that face"
    voice2 = vscan(("S1", unit(0)), segments=[(0.0, 12.0, "S1")])
    link = P.voice_face_links(P.load_registry(), {SHA1: (face, voice2)})
    assert len(link) == 1 and link[0]["ratio"] == 1.0
    voice3 = vscan(("S1", unit(0)), segments=[(0.0, 12.0, "S1"), (40.0, 45.0, "S1")])
    assert P.voice_face_links(P.load_registry(), {SHA1: (face, voice3)})[0]["ratio"] == pytest.approx(12 / 17, abs=1e-3), "enough of its speech is on that face"


# ============================ scanning a file ============================
def test_a_file_is_listened_to_saved_and_a_silent_one_is_saved_as_silent(tmp_path, by_level):
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")
    wav = tmp_path / "talk.wav"
    gap = np.zeros(SR, np.float32)
    write_wav(wav, np.concatenate([tone(6, 0.1), gap, tone(6, 0.3), gap]))
    words = words_at((0, 6), (7, 13))
    scan = P.scan_voices(str(wav), SHA1, words, 14.0, session=object())
    assert [s["id"] for s in scan["speakers"]] == ["S1", "S2"] and [g["speaker"] for g in scan["segments"]] == ["S1", "S2"] and P.load_voice_scan(SHA1) == scan
    assert wav.exists(), "the file given is the user's: only the temporary copy is removed"
    silent = P.scan_voices(str(wav), SHA2, [], 14.0)
    assert silent["speakers"] == [] and silent["segments"] == [] and P.load_voice_scan(SHA2) is not None
    with pytest.raises(InterruptedError):
        P.scan_voices(str(wav), "3" * 64, words, 14.0, session=object(), should_cancel=lambda: True)
    assert P.load_voice_scan("3" * 64) is None


def test_the_voice_scan_is_stored_with_the_other_people_data_and_erased_with_it():
    P._write_json(P._voice_path(SHA1), vscan(("S1", unit(0))))
    P._write_json(P._scan_path(SHA2), {"version": P.VERSION, "tracks": []})
    assert P.delete_all() == {"scans": 2, "registry": False, "models": 0} and P.load_voice_scan(SHA1) is None and not P.has_data()


def test_nothing_in_the_voice_code_can_reach_the_network_and_nothing_logs_a_voiceprint(monkeypatch, tmp_path):
    for name in ("voiceprint.py", "voice_diarize.py"):
        src = (SRC / "classes" / "media_index" / name).read_text()
        for banned in ("urllib", "requests", "http.client", "socket", "api_client", "websocket", "aiohttp", "httpx"):
            assert not re.search(rf"^\s*(import|from)\s+{re.escape(banned)}\b", src, re.M), f"{name} imports {banned}"
    messages = []
    monkeypatch.setattr(P.log, "info", lambda msg, *a, **k: messages.append(msg % a if a else msg))
    monkeypatch.setattr(V, "speakers_of", lambda *a, **k: {"labels": [0], "speakers": [{"index": 0, "seconds": 3.0, "vector": unit(4)}], "unused": 0})
    temp = tmp_path / "extracted.wav"
    write_wav(temp, np.zeros(SR, np.float32))
    monkeypatch.setattr("classes.speech.audio_extract.extract_mono_16k_wav", lambda p: (str(temp), ""))
    scan = P.scan_voices("/x.mp4", SHA1, words_at((0, 3)), 3.0, session=object())
    assert not temp.exists(), "the temporary copy of the audio is removed"
    assert messages and all(scan["speakers"][0]["embedding"] not in m and "embedding" not in m.lower() for m in messages) and re.search(r"\d+ voice", messages[0])


# ============================ the diarizer hook ============================
def test_the_baseline_stays_when_people_identity_is_off_or_not_ready_and_voiceprints_are_used_when_it_is(monkeypatch):
    from classes.speech.diarize import WindowClusterDiarize
    monkeypatch.setattr("classes.media_index.flags.people_enabled", lambda: False)
    assert isinstance(voice_diarize.factory(), WindowClusterDiarize)
    monkeypatch.setattr("classes.media_index.flags.people_enabled", lambda: True)
    monkeypatch.setattr("classes.media_index.people_models.runtime_available", lambda: True)
    monkeypatch.setattr("classes.media_index.people_models.model_path", lambda n: None)
    assert isinstance(voice_diarize.factory(), WindowClusterDiarize), "no voice model: baseline"
    monkeypatch.setattr("classes.media_index.people_models.model_path", lambda n: "/m.onnx")
    assert isinstance(voice_diarize.factory(), voice_diarize.EmbeddingDiarize)


def test_the_diarizer_labels_words_by_speaker_and_warns_when_most_speech_was_unplaceable(monkeypatch):
    from classes.speech.cache import Word
    from classes.speech.runtime import CancelToken
    monkeypatch.setattr(V, "read_wav16k", lambda p: np.zeros(SR, np.float32))
    words = [Word("a", 0.0, 0.2), Word("b", 0.3, 0.5), Word("c", 5.0, 5.2)]
    seen = {}

    def fake(session, samples, ws, **kw):
        seen.update(kw)
        return {"labels": [0, 0, 1], "speakers": [], "unused": 0}

    monkeypatch.setattr(V, "speakers_of", fake)
    out, warnings = voice_diarize.EmbeddingDiarize(session=object()).label_words("/w.wav", words, token=CancelToken(), max_speakers=3)
    assert [w.speakerId for w in out] == ["S1", "S1", "S2"] and warnings == [] and seen["max_speakers"] == 3 and out[0].text == "a"
    monkeypatch.setattr(V, "speakers_of", lambda *a, **k: {"labels": [0, 0, 0], "speakers": [], "unused": 3})
    assert voice_diarize.EmbeddingDiarize(session=object()).label_words("/w.wav", words, token=CancelToken())[1]
    assert voice_diarize.EmbeddingDiarize(session=object()).label_words("/w.wav", [], token=CancelToken()) == ([], [])


def test_installing_the_factory_replaces_the_default_and_can_be_undone():
    from classes.speech import diarize
    try:
        voice_diarize.install()
        assert diarize._factory is voice_diarize.factory
    finally:
        diarize.reset_diarize_factory()
    assert diarize._factory is diarize.WindowClusterDiarize
