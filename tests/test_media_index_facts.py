"""One call runs every missing local layer for a file, keeps it on the shelf, and never repeats work."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import media_fixtures as mf  # noqa: E402
from classes.media_fingerprint import fingerprint  # noqa: E402
from classes.media_index import audio as audio_mod  # noqa: E402
from classes.media_index import facts, schema as S  # noqa: E402
from classes.media_index import structure as structure_mod  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from classes.speech.cache import TranscriptRecord, Word, file_identity  # noqa: E402


def fake_transcribe(path, **kw):
    fake_transcribe.calls.append(path)
    a, size, mt = file_identity(path)
    words = [Word("Hello", 0.5, 0.9, 0.9), Word("there.", 0.95, 1.4, 0.9), Word("Second", 4.5, 4.9, 0.8), Word("scene.", 4.95, 5.4, 0.8)]
    return TranscriptRecord(path=a, size=size, mtimeNs=mt, modelId="whisper.cpp-base-q5_1", language="en", words=words)


fake_transcribe.calls = []


@pytest.fixture(autouse=True)
def _reset_calls():
    fake_transcribe.calls.clear()


def with_audio(src, out, seconds, hz=440):
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-i", src, "-f", "lavfi", "-i", f"sine=frequency={hz}:duration={seconds}",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", out], check=True)
    return out


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    mf.need_ffmpeg()
    d = tmp_path_factory.mktemp("facts")
    a, b = mf.scene(31), mf.scene(32)
    parts = [mf.render(str(d / "a.mp4"), a, mf.hold(a), 4.0), mf.render(str(d / "b.mp4"), b, mf.hold(b), 4.0)]
    silent = mf.join_hard(str(d / "silent.mp4"), parts)
    return {"dir": d, "silent": silent, "av": with_audio(silent, str(d / "av.mp4"), 8)}


@pytest.fixture
def shelf(tmp_path):
    return Shelf(str(tmp_path / "shelf"))


def run(path, shelf, **kw):
    kw.setdefault("transcribe", fake_transcribe)
    kw.setdefault("transcript_cache", None)
    return facts.compute_facts(path, shelf=shelf, **kw)


# ============================ a full run ============================
def test_every_layer_is_computed_and_kept_on_the_shelf(media, shelf):
    out = run(media["av"], shelf)
    assert out["ok"] and out["errors"] == {}
    assert out["layers"] == {"structure": "ready", "look": "ready", "audio": "ready", "speech": "ready"}
    sha = out["sha"]
    for layer in ("structure", "look", "audio", "speech"):
        assert shelf.layer_ready(sha, layer, version=S.LAYER_VERSIONS[layer]), layer
        assert shelf.read_json(sha, f"{layer}.json"), layer

    structure = shelf.read_json(sha, "structure.json")
    assert [round(s["start"], 1) for s in structure["shots"]] == [0.0, 4.0] and structure["kind"] == "measured"
    look = shelf.read_json(sha, "look.json")
    assert [s["id"] for s in look["shots"]] == [0, 1] and look["file"]["profile"]["present"]
    audio = shelf.read_json(sha, "audio.json")
    assert audio["loudness"]["integrated_lufs"] is not None and audio["windows"]
    speech = shelf.read_json(sha, "speech.json")
    assert speech["kind"] == "inferred" and speech["stats"]["word_count"] == 4
    assert speech["per_shot"]["0"]["words"] == 2 and speech["per_shot"]["1"]["words"] == 2, "words are apportioned to the shots they fall in"

    manifest = shelf.manifest(sha)
    assert manifest["source"]["duration"] == pytest.approx(8.0, abs=0.2) and manifest["source"]["width"] == 640
    assert manifest["source"]["has_audio"] is True and manifest["source"]["colour"]["hdr"] is False


def test_a_second_run_does_no_work_at_all(media, shelf, monkeypatch):
    first = run(media["av"], shelf)
    called = []
    monkeypatch.setattr(structure_mod, "analyze_structure", lambda *a, **k: called.append("structure"))
    monkeypatch.setattr(audio_mod, "analyze_audio", lambda *a, **k: called.append("audio"))
    again = run(media["av"], shelf, fingerprint=fingerprint(media["av"]))
    assert again["layers"] == {"structure": "cached", "look": "cached", "audio": "cached", "speech": "cached"}
    assert called == [] and len(fake_transcribe.calls) == 1 and again["sha"] == first["sha"]


def test_layers_saved_by_an_older_version_are_refreshed_and_gain_the_new_fields(media, shelf):
    first = run(media["av"], shelf)
    sha = first["sha"]
    # what an older release left behind: structure and audio without sharpness, quality or music facts
    saved = shelf.read_json(sha, "structure.json")
    for shot in saved["shots"]:
        shot.pop("sharpness", None)
    shelf.write_json(sha, "structure.json", saved)
    audio_saved = shelf.read_json(sha, "audio.json")
    audio_saved.pop("music", None)
    shelf.write_json(sha, "audio.json", audio_saved)
    for layer in ("structure", "audio"):
        shelf.set_layer(sha, layer, version=1, status="ready")
    # still readable as it is ...
    assert shelf.layer_ready(sha, "structure") and not shelf.layer_ready(sha, "structure", version=2)
    # ... and the next run refreshes just those (speech is untouched)
    again = run(media["av"], shelf)
    assert again["layers"]["structure"] == "ready" and again["layers"]["audio"] == "ready" and again["layers"]["speech"] == "cached"
    assert all(s.get("sharpness") is not None for s in shelf.read_json(sha, "structure.json")["shots"])
    assert "music" in shelf.read_json(sha, "audio.json") and shelf.layer_ready(sha, "structure", version=2)


def test_capture_time_and_place_are_kept_with_the_source(media, shelf, tmp_path):
    tagged = str(tmp_path / "tagged.mp4")
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-i", media["av"], "-c", "copy", "-metadata", "location=+37.7749-122.4194+012.000/",
                    "-metadata", "creation_time=2024-05-01T14:03:09Z", tagged], check=True)
    out = run(tagged, shelf)
    source = shelf.manifest(out["sha"])["source"]
    assert source["gps"] == {"lat": 37.7749, "lon": -122.4194, "alt": 12.0} and source["captured_at"] == "2024-05-01T14:03:09+00:00"
    untagged = shelf.manifest(run(media["silent"], shelf)["sha"])["source"]
    assert "gps" not in untagged and "captured_at" not in untagged, "nothing is invented for a file that carries no tags"


def test_the_same_content_at_another_path_is_already_done(media, shelf, tmp_path):
    run(media["av"], shelf)
    copy = tmp_path / "renamed.mov"
    shutil.copy(media["av"], copy)
    again = run(str(copy), shelf)
    assert set(again["layers"].values()) == {"cached"}


def test_only_the_layer_that_is_out_of_date_runs_again(media, shelf, monkeypatch):
    run(media["av"], shelf)
    monkeypatch.setitem(S.LAYER_VERSIONS, "audio", S.LAYER_VERSIONS["audio"] + 1)
    structure_calls = []
    monkeypatch.setattr(structure_mod, "analyze_structure", lambda *a, **k: structure_calls.append(1))
    again = run(media["av"], shelf)
    assert again["layers"]["audio"] == "ready" and again["layers"]["structure"] == "cached" and again["layers"]["speech"] == "cached"
    assert structure_calls == []


# ============================ what a file may lack ============================
def test_video_without_audio_skips_audio_and_speech(media, shelf):
    out = run(media["silent"], shelf)
    assert out["layers"] == {"structure": "ready", "look": "ready", "audio": "skipped", "speech": "skipped"}
    assert fake_transcribe.calls == []
    # recorded in the manifest, so nothing waits on a layer this file can never have
    for layer in ("audio", "speech"):
        assert shelf.layer(out["sha"], layer)["status"] == "not_applicable" and not shelf.layer_ready(out["sha"], layer)
    assert shelf.layer(out["sha"], "structure")["status"] == "ready"


def test_audio_only_files_skip_the_picture_layers(tmp_path, shelf):
    mf.need_ffmpeg()
    wav = str(tmp_path / "talk.wav")
    subprocess.run([mf.need_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=3", wav], check=True)
    out = run(wav, shelf, media_type="audio")
    assert out["layers"] == {"structure": "skipped", "look": "skipped", "audio": "ready", "speech": "ready"}
    assert shelf.layer(out["sha"], "structure")["status"] == "not_applicable" and shelf.layer(out["sha"], "look")["status"] == "not_applicable"
    assert shelf.layer_ready(out["sha"], "audio")


def test_a_still_image_gets_a_look_and_no_audio(tmp_path, shelf):
    mf.need_ffmpeg()
    path = str(tmp_path / "pic.png")
    Image.fromarray((np.indices((120, 200, 3)).sum(axis=0) % 256).astype(np.uint8)).save(path)
    out = run(path, shelf, media_type="image")
    assert out["layers"]["look"] == "ready" and out["layers"]["audio"] == "skipped" and out["layers"]["speech"] == "skipped"
    look = shelf.read_json(out["sha"], "look.json")
    assert look["file"]["profile"]["present"] and look["sampling"]["frames"] == 1


def test_an_unreadable_file_reports_instead_of_raising(tmp_path, shelf):
    bad = tmp_path / "garbage.mp4"
    bad.write_bytes(b"this is not a video" * 50)
    out = run(str(bad), shelf)
    assert out["ok"] is False and out["error"]


def test_a_missing_file_reports_instead_of_raising(tmp_path, shelf):
    out = run(str(tmp_path / "nope.mp4"), shelf)
    assert out["ok"] is False


def test_the_fingerprint_is_computed_when_not_given(media, shelf):
    out = run(media["silent"], shelf, fingerprint=None)
    assert out["sha"] == fingerprint(media["silent"])["sha256"]


# ============================ failure and control ============================
def test_one_failing_layer_does_not_stop_the_others_and_is_recorded(media, shelf, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(audio_mod, "analyze_audio", boom)
    out = run(media["av"], shelf)
    assert out["layers"]["audio"] == "failed" and "decoder exploded" in out["errors"]["audio"]
    assert out["layers"]["structure"] == "ready" and out["layers"]["speech"] == "ready" and out["ok"] is True
    row = shelf.layer(out["sha"], "audio")
    assert row["status"] == "failed" and "decoder exploded" in row["error"]
    assert not shelf.layer_ready(out["sha"], "audio"), "a failed layer is retried next time"


def test_a_missing_speech_engine_leaves_the_rest_intact(media, shelf):
    def no_engine(path, **kw):
        raise RuntimeError("faster-whisper is not installed")

    out = run(media["av"], shelf, transcribe=no_engine)
    assert out["layers"]["speech"] == "unavailable" and "not installed" in out["errors"]["speech"]
    assert out["layers"]["audio"] == "ready" and out["ok"] is True
    assert not shelf.layer_ready(out["sha"], "speech")


def test_speech_can_be_turned_off(media, shelf):
    out = run(media["av"], shelf, run_speech=False)
    assert out["layers"]["speech"] == "skipped" and fake_transcribe.calls == []


def test_cancelling_keeps_what_finished(media, shelf):
    state = {"structure_done": False}

    def cancel():
        return state["structure_done"]

    real = structure_mod.analyze_structure

    def wrapped(*a, **k):
        res = real(*a, **k)
        state["structure_done"] = True
        return res

    structure_mod.analyze_structure, saved = wrapped, structure_mod.analyze_structure
    try:
        with pytest.raises(facts.Cancelled):
            run(media["av"], shelf, should_cancel=cancel)
    finally:
        structure_mod.analyze_structure = saved
    sha = fingerprint(media["av"])["sha256"]
    assert shelf.layer_ready(sha, "structure") and shelf.layer_ready(sha, "look")
    assert not shelf.layer_ready(sha, "audio") and not shelf.layer_ready(sha, "speech")
    # and the next run picks up where it stopped
    again = run(media["av"], shelf)
    assert again["layers"]["structure"] == "cached" and again["layers"]["audio"] == "ready"


def test_a_cancel_during_transcription_stops_it(media, shelf):
    def slow(path, token=None, **kw):
        for _ in range(200):
            if token is not None:
                token.raise_if_cancelled()
            import time
            time.sleep(0.02)
        raise AssertionError("was never cancelled")

    state = {"n": 0}

    def cancel():
        state["n"] += 1
        return state["n"] > 12   # after the picture and audio layers have passed their checks

    with pytest.raises(facts.Cancelled):
        run(media["av"], shelf, transcribe=slow, should_cancel=cancel)


def test_progress_runs_forward_to_the_end(media, shelf):
    seen = []
    run(media["av"], shelf, on_progress=seen.append)
    assert seen and seen[-1] == 1.0 and all(a <= b + 1e-9 for a, b in zip(seen, seen[1:]))
