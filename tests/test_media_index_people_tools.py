"""The people tools: status, scanning, naming, search and locate by person, and deletion."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes import info  # noqa: E402
from classes.editor_tools import REGISTRY  # noqa: E402
from classes.media_index import library, people as P, people_models as PM  # noqa: E402
from test_media_index_people import blend, det, scan_of, unit  # noqa: E402
from test_media_index_search import SHA1, SHA2, build, shot, unit as sunit, w  # noqa: E402
from test_media_index_tools import call as tcall, env, file_obj  # noqa: E402,F401
import classes.editor_tools.media_index_tools as T  # noqa: E402
import classes.editor_tools.media_index_tools_people as TP  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"), raising=False)
    monkeypatch.setattr(TP, "people_enabled", lambda: True)
    monkeypatch.setattr("classes.media_index.flags.people_enabled", lambda: True)
    PM.clear_sessions()


def run(tool, **kw):
    out = REGISTRY[tool].func(**kw)
    head, _, body = out.partition("\n")
    return head, (json.loads(body) if body else {}), out


def seed(env, vectors_by_file):
    """Give indexed files a people scan (one face per shot) and register the people."""
    for sha, vecs in vectors_by_file.items():
        shots = [{"id": i, "start": i * 10.0, "end": i * 10.0 + 10.0} for i in range(len(vecs))]
        scan = P.build_scan([det(i * 10.0 + 2.0, v, box=(0.4, 0.2, 0.1, 0.2)) for i, v in enumerate(vecs)], shots, len(vecs), len(vecs) * 10.0)
        P._write_json(P._scan_path(sha), scan)
        P.assign_new_faces(sha, scan)


# ============================ status and gating ============================
def test_the_tools_say_the_preference_is_off_and_status_says_what_is_missing(monkeypatch):
    monkeypatch.setattr(TP, "people_enabled", lambda: False)
    for name, kw in (("list_people_tool", {}), ("scan_people_tool", {}), ("setup_people_tool", {}), ("who_is_this_tool", dict(file_id="F1", seconds=1.0)),
                     ("locate_person_tool", dict(person="x")), ("merge_people_tool", dict(keep="P1", merge="P2")), ("name_person_tool", dict(name="Sam", person_id="P1"))):
        out = REGISTRY[name].func(**kw)
        assert out.startswith("Error") and "recognise people" in out, name
    head, r, _ = run("people_status_tool")
    assert r["enabled"] is False and r["ready"] is False and "Preferences" in head and "never uploaded" in r["privacy"] and r["licenses"]["detector"] == "MIT"


def test_status_reports_missing_runtime_then_missing_models(monkeypatch):
    monkeypatch.setattr(PM, "runtime_available", lambda: False)
    head, r, _ = run("people_status_tool")
    assert r["ready"] is False and "onnxruntime" in head
    monkeypatch.setattr(PM, "runtime_available", lambda: True)
    head, r, _ = run("people_status_tool")
    assert r["ready"] is False and "download the models" in head and "MB" in head
    monkeypatch.setattr(PM, "model_path", lambda n: "/x")
    head, r, _ = run("people_status_tool")
    assert r["ready"] is True and head == "People identity is ready."


def test_setup_downloads_only_with_the_runtime_and_reports_failures(monkeypatch):
    monkeypatch.setattr(PM, "runtime_available", lambda: False)
    assert "onnxruntime" in REGISTRY["setup_people_tool"].func()
    monkeypatch.setattr(PM, "runtime_available", lambda: True)
    monkeypatch.setattr(PM, "download", lambda *a, **k: {"ok": False, "installed": [], "error": "could not download the detector model: offline"})
    assert "offline" in REGISTRY["setup_people_tool"].func()
    monkeypatch.setattr(PM, "download", lambda *a, **k: {"ok": True, "installed": ["detector"], "error": None})
    head, r, _ = run("setup_people_tool")
    assert r["changed"] is True and r["installed"] == ["detector"]


# ============================ scanning ============================
def test_scanning_needs_the_models_scans_unscanned_files_once_and_reports_each(env, monkeypatch):
    assert "setup_people_tool" in REGISTRY["scan_people_tool"].func() or "onnxruntime" in REGISTRY["scan_people_tool"].func()
    monkeypatch.setattr(PM, "status", lambda: {"runtime": True, "models": {"detector": True, "recognizer": True, "voice": True}, "faces_ready": True, "voices_ready": True, "download_bytes": 0, "licenses": {}, "attributions": []})
    calls = []

    def fake_scan(fi):
        calls.append(fi.file_id)
        scan = scan_of(unit(0), unit(1))
        P._write_json(P._scan_path(fi.sha), scan)
        return {"scan": scan, "assigned": P.assign_new_faces(fi.sha, scan)}

    monkeypatch.setattr(TP, "_scan_one", fake_scan)
    head, r, _ = run("scan_people_tool")
    assert calls == ["F1", "F2"] and r["scanned"][0]["tracks"] == 2 and r["scanned"][0]["new"] == 2 and r["changed"] is True
    head2, r2, _ = run("scan_people_tool")
    assert calls == ["F1", "F2"] and r2["scanned"] == [] and r2["changed"] is False, "already scanned files are not scanned again"
    run("scan_people_tool", file_ids=["F1"], rescan=True)
    assert calls == ["F1", "F2", "F1"]


def test_one_file_failing_does_not_stop_the_others_and_a_budget_leaves_the_rest(env, monkeypatch):
    monkeypatch.setattr(PM, "status", lambda: {"runtime": True, "models": {"detector": True, "recognizer": True, "voice": True}, "faces_ready": True, "voices_ready": True, "download_bytes": 0, "licenses": {}, "attributions": []})
    build(env.shelf, "e" * 64, shots=[shot(0, 0, 8)], duration=8.0, watch=[w(0, 0, 8, "A kitchen")])
    env.files.append(file_obj("F9", "kitchen.mp4", "e" * 64))
    library.clear_cache()

    def flaky(fi):
        if fi.file_id == "F1":
            raise RuntimeError("decode failed")
        scan = scan_of(unit(2))
        P._write_json(P._scan_path(fi.sha), scan)
        return {"scan": scan, "assigned": P.assign_new_faces(fi.sha, scan)}

    monkeypatch.setattr(TP, "_scan_one", flaky)
    _, r, _ = run("scan_people_tool")
    by = {x["file_id"]: x for x in r["scanned"]}
    assert "could not scan" in by["F1"]["error"] and by["F9"]["tracks"] == 1
    monkeypatch.setattr(TP, "SCAN_BUDGET_SECONDS", -1.0)
    _, left, _ = run("scan_people_tool", rescan=True)
    assert left["scanned"] == [] and set(left["left"]) == {"lake.mp4", "partial.mp4", "kitchen.mp4"}


# ============================ list, who, name, merge, fix ============================
def test_list_shows_people_by_screen_time_hides_passers_by_and_never_invents_names(env):
    seed(env, {SHA1: [unit(0), unit(1), unit(0)]})
    _, r, out = run("list_people_tool")
    assert [(p["id"], p["name"], p["shots"]) for p in r["people"]] == [("P1", None, 2)] and r["hidden"] == 1 and r["unnamed"] == 1
    _, more, _ = run("list_people_tool", include_minor=True)
    assert len(more["people"]) == 2 and "embedding" not in out and "exemplars" not in out


def test_who_is_this_gives_the_person_the_confidence_and_unsure_with_the_possible_person(env):
    seed(env, {SHA1: [unit(0)]})
    _, r, _ = run("who_is_this_tool", file_id="F1", seconds=2.5)
    f = r["faces"][0]
    assert f["person"] == "P1" and f["status"] == "match" and f["confidence"] == 1.0 and f["box"] == [0.4, 0.2, 0.1, 0.2] and f["name"] is None
    maybe = np.array([P.UNSURE_LOW + 0.05, np.sqrt(1 - (P.UNSURE_LOW + 0.05) ** 2)] + [0] * 126, np.float32)
    shots = [{"id": 0, "start": 0.0, "end": 10.0}, {"id": 1, "start": 10.0, "end": 20.0}]
    P._write_json(P._scan_path(SHA1), P.build_scan([det(2.0, unit(0)), det(12.0, maybe)], shots, 2, 20.0))
    _, u, _ = run("who_is_this_tool", file_id="F1", seconds=12.0)
    assert u["faces"][0]["status"] == "unsure" and u["faces"][0]["person"] is None and u["faces"][0]["possible"] == "P1"
    head, none, _ = run("who_is_this_tool", file_id="F1", seconds=18.5)
    assert none["faces"] == [] and "No face" in head
    assert "no indexed video" in REGISTRY["who_is_this_tool"].func(file_id="NOPE", seconds=1.0)
    assert "no people scan" in REGISTRY["who_is_this_tool"].func(file_id="F2", seconds=1.0) or "no indexed video" in REGISTRY["who_is_this_tool"].func(file_id="F2", seconds=1.0)


def test_naming_by_id_by_pointing_and_a_known_name_adds_to_that_person(env):
    seed(env, {SHA1: [unit(0), unit(1)]})
    _, r, _ = run("name_person_tool", name="  Sam  ", person_id="P1")
    assert r["person"] == {"id": "P1", "name": "Sam", "same_name_as": None}
    _, r2, _ = run("name_person_tool", name="Maya", file_id="F1", seconds=12.0)
    assert r2["person"]["id"] == "P2" and P.load_registry()["people"][1]["name"] == "Maya"
    head, r3, _ = run("name_person_tool", name="sam", file_id="F1", seconds=12.0)
    assert "already known" in head and r3["person"] == "P1" and [t["person"] for t in P.resolve_tracks(SHA1, P.load_scan(SHA1), P.load_registry())] == ["P1", "P1"]
    assert "a name is needed" in REGISTRY["name_person_tool"].func(name=" ", person_id="P1")
    assert "no person" in REGISTRY["name_person_tool"].func(name="X", person_id="P9")
    assert "say who" in REGISTRY["name_person_tool"].func(name="X")
    assert "no face was found" in REGISTRY["name_person_tool"].func(name="X", file_id="F1", seconds=500.0)


def test_pointing_at_a_time_with_two_faces_asks_which_and_an_unsure_face_becomes_a_new_named_person(env):
    shots = [{"id": 0, "start": 0.0, "end": 10.0}]
    P._write_json(P._scan_path(SHA1), P.build_scan([det(2.0, unit(0)), det(2.0, unit(1), box=(0.6, 0.2, 0.1, 0.2))], shots, 1, 10.0))
    P.assign_new_faces(SHA1, P.load_scan(SHA1))
    out = REGISTRY["name_person_tool"].func(name="Sam", file_id="F1", seconds=2.0)
    assert out.startswith("Error") and "track_id" in out and "s0t1" in out and "s0t2" in out
    _, r, _ = run("name_person_tool", name="Sam", file_id="F1", seconds=2.0, track_id="s0t2")
    assert r["person"]["name"] == "Sam"
    P.delete_all()
    maybe = np.array([P.UNSURE_LOW + 0.05, np.sqrt(1 - (P.UNSURE_LOW + 0.05) ** 2)] + [0] * 126, np.float32)
    P.assign_new_faces("c" * 64, P.build_scan([det(2.0, unit(0))], shots, 1, 10.0))      # someone is known already: P1
    P._write_json(P._scan_path(SHA1), P.build_scan([det(2.0, maybe)], shots, 1, 10.0))   # a face that only might be them
    before = P.resolve_tracks(SHA1, P.load_scan(SHA1), P.load_registry())[0]
    assert before["status"] == "unsure" and before["person"] is None
    _, u, _ = run("name_person_tool", name="Lee", file_id="F1", seconds=2.0)
    after = P.resolve_tracks(SHA1, P.load_scan(SHA1), P.load_registry())[0]
    assert u["person"]["name"] == "Lee" and u["person"]["id"] == "P2" and after["status"] == "pinned" and after["person"] == "P2"
    assert P.load_registry()["people"][0]["name"] is None, "the possible match was not merged into P1"


def test_merging_and_fixing_report_what_happened_and_refuse_nonsense(env):
    seed(env, {SHA1: [unit(0), unit(1)]})
    assert "no person" in REGISTRY["merge_people_tool"].func(keep="P1", merge="P9")
    assert "same person already" in REGISTRY["merge_people_tool"].func(keep="P1", merge="P1")
    _, r, _ = run("fix_person_track_tool", file_id="F1", track_id="s1t1", person="P1")
    assert r["person"] == "P1"
    _, n, _ = run("fix_person_track_tool", file_id="F1", track_id="s1t1", person="new")
    assert n["person"] == "P3"
    assert "no such" in REGISTRY["fix_person_track_tool"].func(file_id="F1", track_id="s9t9", person="P1")
    _, m, _ = run("merge_people_tool", keep="P1", merge="P2")
    assert m["kept"] == "P1" and [p["id"] for p in P.load_registry()["people"]] == ["P1", "P3"]


# ============================ search and locate by person ============================
def test_search_by_person_keeps_only_the_shots_they_are_in_and_marks_the_confidence(env):
    seed(env, {SHA1: [unit(0), unit(1)]})              # lake shot 0: person 1, dog shot 1: person 2
    P.name_person("P2", "Maya")
    _, r = tcall("search_footage_tool", person="maya")
    assert [h["shot_id"] for h in r["hits"]] == [1] and r["hits"][0]["person_confidence"] == 1.0 and r["person"]["name"] == "Maya" and r["unsure_shots"] == 0
    _, by_id = tcall("search_footage_tool", person="P1", filters={"exclude_black": False})
    assert [h["shot_id"] for h in by_id["hits"]] == [0]
    _, both = tcall("search_footage_tool")
    assert len(both["hits"]) > 1, "without a person, nothing is narrowed"


def test_search_by_person_says_when_nobody_has_that_name_or_the_feature_is_off(env, monkeypatch):
    seed(env, {SHA1: [unit(0)]})
    out = REGISTRY["search_footage_tool"].func(person="Zed")
    assert out.startswith("Error") and "no person called 'Zed'" in out and "list_people_tool" in out
    P.name_person("P1", "Sam")
    assert "named people: Sam" in REGISTRY["search_footage_tool"].func(person="Zed")
    monkeypatch.setattr("classes.media_index.flags.people_enabled", lambda: False)
    assert "preference" in REGISTRY["search_footage_tool"].func(person="Sam")


def test_search_by_person_counts_possible_matches_it_left_out_and_unscanned_files(env):
    shots = [{"id": 0, "start": 0.0, "end": 10.0}, {"id": 1, "start": 10.0, "end": 20.0}]
    maybe = np.array([P.UNSURE_LOW + 0.05, np.sqrt(1 - (P.UNSURE_LOW + 0.05) ** 2)] + [0] * 126, np.float32)
    P._write_json(P._scan_path(SHA1), P.build_scan([det(2.0, unit(0)), det(12.0, maybe)], shots, 2, 20.0))
    P.assign_new_faces(SHA1, P.load_scan(SHA1))
    P.name_person("P1", "Sam")
    _, r = tcall("search_footage_tool", person="Sam")
    assert [h["shot_id"] for h in r["hits"]] == [0] and r["unsure_shots"] == 1


def test_locate_a_person_gives_face_and_body_boxes_and_a_handoff_for_the_masking_tools(env):
    seed(env, {SHA1: [unit(0), unit(1)]})
    P.name_person("P1", "Sam")
    f = env.files[0]
    f.data.update(width=1920, height=1080, video_length=500)
    env.shelf.set_source(SHA1, technical={"video": {"width": 1920, "height": 1080, "fps": 25.0}})
    library.clear_cache()
    _, r, _ = run("locate_person_tool", person="Sam")
    h = r["hits"][0]
    assert len(r["hits"]) == 1 and h["shot_id"] == 0 and h["face_box"] == [0.4, 0.2, 0.1, 0.2] and h["person"] == "P1" and "handoff" not in h
    x, y, bw, bh = h["body_box"]
    assert bw == pytest.approx(0.26, abs=1e-3) and y == pytest.approx(0.12, abs=1e-3) and bh == pytest.approx(0.88, abs=1e-3) and 0 <= x and x + bw <= 1
    _, m, _ = run("locate_person_tool", person="sam", for_action="mask_object")
    args = m["hits"][0]["handoff"]["args"]
    assert args["action"] == "mask_object" and args["prompt"] == "Sam" and args["file_id"] == "F1" and args["seed_frame"] == 51 and args["boxes"][0]["x2"] <= 1920
    assert "no person" in REGISTRY["locate_person_tool"].func(person="Nobody")
    head, none, _ = run("locate_person_tool", person="P2", file_ids=["F2"])
    assert none["hits"] == [] and "not found" in head


def test_the_body_box_is_a_rough_estimate_kept_inside_the_frame():
    from classes.media_index import handoff
    assert handoff.body_box_from_face([0.0, 0.0, 0.1, 0.1]) == [0.0, 0.0, 0.18, 0.56], "clipped at the frame's top-left corner"
    low = handoff.body_box_from_face([0.45, 0.9, 0.1, 0.1])
    assert low[1] + low[3] <= 1.0 and handoff.body_box_from_face([0.1, 0.1, 0.0, 0.1]) is None and handoff.body_box_from_face(["a"]) is None


# ============================ delete ============================
def test_delete_needs_confirmation_and_removes_everything_even_with_the_preference_off(env, monkeypatch):
    seed(env, {SHA1: [unit(0)]})
    assert "ask the user" in REGISTRY["erase_people_data_tool"].func(confirm=False)
    assert P.has_data()
    monkeypatch.setattr(TP, "people_enabled", lambda: False)
    _, r, _ = run("erase_people_data_tool", confirm=True)
    assert r["removed"] == {"scans": 1, "registry": True, "models": 0} and not P.has_data() and P.load_scan(SHA1) is None and P.load_registry()["people"] == []


def test_no_tool_receipt_carries_the_numbers_of_a_face(env):
    seed(env, {SHA1: [unit(0), unit(1)]})
    P.name_person("P1", "Sam")
    blob = ""
    for name, kw in (("list_people_tool", {}), ("who_is_this_tool", dict(file_id="F1", seconds=2.0)), ("locate_person_tool", dict(person="Sam")),
                     ("people_status_tool", {}), ("search_footage_tool", dict(person="Sam"))):
        blob += REGISTRY[name].func(**kw)
    for secret in (P._read_json(P._registry_path())["people"][0]["exemplars"][0], P.load_scan(SHA1)["tracks"][0]["embedding"]):
        assert secret not in blob
    assert "embedding" not in blob and "exemplar" not in blob


# ============================ voices ============================
def vscan_of(*speakers, segments=()):
    return {"version": P.VERSION, "duration": 20.0, "segments": [{"start": a, "end": b, "speaker": s} for a, b, s in segments],
            "speakers": [{"id": sid, "seconds": 10.0, "embedding": P.enc(v)} for sid, v in speakers]}


def seed_voices(sha, *speakers, segments=()):
    scan = vscan_of(*speakers, segments=segments)
    P._write_json(P._voice_path(sha), scan)
    P.assign_new_voices(sha, scan)
    return scan


def test_scanning_also_listens_to_speech_and_saves_silence_as_silence(env, monkeypatch):
    monkeypatch.setattr(PM, "status", lambda: {"runtime": True, "models": {"detector": True, "recognizer": True, "voice": True}, "faces_ready": True, "voices_ready": True,
                                              "download_bytes": 0, "licenses": {}, "attributions": []})
    monkeypatch.setattr(TP, "_scan_one", lambda fi: {"scan": scan_of(unit(0)), "assigned": {"matched": 0, "new": 1, "unsure": 0}})
    env.shelf.write_json(SHA1, "speech.json", {"words": [{"startSec": 0.0, "endSec": 0.3}, {"startSec": 0.4, "endSec": 0.7}]})
    monkeypatch.setattr(TP, "default_shelf", lambda: env.shelf)
    heard = []
    monkeypatch.setattr(P, "scan_voices", lambda path, sha, words, duration, **k: heard.append((sha, len(words))) or (
        P._write_json(P._voice_path(sha), vscan_of(("S1", unit(0, 256)))) or vscan_of(("S1", unit(0, 256))) if words else vscan_of()))
    _, r, _ = run("scan_people_tool", file_ids=["F1"])
    row = r["scanned"][0]
    assert heard == [(SHA1, 2)] and row["voices"] == 1 and row["new"] == 1 and "error" not in row
    _, r2, _ = run("scan_people_tool", file_ids=["F2"])
    assert r2["scanned"][0]["voices"] == 0 and "no speech" in r2["scanned"][0]["why"]


def test_voices_are_only_scanned_when_the_voice_model_is_installed(env, monkeypatch):
    monkeypatch.setattr(PM, "status", lambda: {"runtime": True, "models": {"detector": True, "recognizer": True, "voice": False}, "faces_ready": True, "voices_ready": False,
                                              "download_bytes": 26_000_000, "licenses": {}, "attributions": []})
    monkeypatch.setattr(TP, "_scan_one", lambda fi: {"scan": scan_of(unit(0)), "assigned": {"matched": 0, "new": 1, "unsure": 0}})
    monkeypatch.setattr(P, "scan_voices", lambda *a, **k: pytest.fail("no voice model: no listening"))
    _, r, _ = run("scan_people_tool", file_ids=["F1"])
    assert "voices" not in r["scanned"][0]
    monkeypatch.setattr(PM, "model_path", lambda n: "/x" if n != "voice" else None)
    head, st, _ = run("people_status_tool")
    assert st["voices_ready"] is False and st["ready"] is True and "voices need the voice model" in head and "MB" in head


def test_who_is_this_also_says_whose_voice_is_heard_then(env):
    seed(env, {SHA1: [unit(0)]})
    seed_voices(SHA1, ("S1", unit(0, 256)), ("S2", unit(1, 256)), segments=[(0.0, 8.0, "S1"), (9.0, 15.0, "S2")])
    P.name_person("P2", "Maya")
    _, r, _ = run("who_is_this_tool", file_id="F1", seconds=12.0)
    assert r["voice"]["person"] == "P3" and r["voice"]["status"] == "match" and r["voice"]["speaking"] == [9.0, 15.0] and r["faces"] == [], "no face at 12 s, but a voice"
    _, a, _ = run("who_is_this_tool", file_id="F1", seconds=3.0)
    assert a["voice"]["speaker"] == "S1" and a["faces"][0]["person"] == "P1"
    head, none, _ = run("who_is_this_tool", file_id="F1", seconds=18.5)
    assert none["voice"] is None and none["faces"] == [] and "No face or voice" in head


def test_naming_by_voice_names_the_speaker_or_adds_to_a_known_person(env):
    seed_voices(SHA1, ("S1", unit(0, 256)), ("S2", unit(1, 256)), segments=[(0.0, 8.0, "S1"), (9.0, 15.0, "S2")])
    _, r, _ = run("name_person_tool", name="Sam", file_id="F1", seconds=3.0, by="voice")
    assert r["person"]["name"] == "Sam" and r["person"]["id"] == "P1"
    head, again, _ = run("name_person_tool", name="sam", file_id="F1", seconds=12.0, by="voice")
    assert "already known" in head and again["person"] == "P1" and P.resolve_speakers(SHA1, P.load_voice_scan(SHA1), P.load_registry())[1]["status"] == "pinned"
    assert "no one is speaking" in REGISTRY["name_person_tool"].func(name="X", file_id="F1", seconds=18.0, by="voice")
    assert "no voice scan" in REGISTRY["name_person_tool"].func(name="X", file_id="F2", seconds=1.0, by="voice")


def test_a_voice_matched_to_the_wrong_person_can_be_corrected(env):
    seed_voices(SHA1, ("S1", unit(0, 256)), ("S2", unit(1, 256)), segments=[(0.0, 8.0, "S1"), (9.0, 15.0, "S2")])
    _, r, _ = run("fix_person_track_tool", file_id="F1", person="P1", speaker_id="S2")
    assert r["person"] == "P1" and r["speaker"].endswith(":S2")
    _, n, _ = run("fix_person_track_tool", file_id="F1", person="new", speaker_id="S2")
    assert n["person"] == "P3"
    assert "say which one" in REGISTRY["fix_person_track_tool"].func(file_id="F1", person="P1")
    assert "say which one" in REGISTRY["fix_person_track_tool"].func(file_id="F1", person="P1", track_id="s0t1", speaker_id="S1")
    assert "no such" in REGISTRY["fix_person_track_tool"].func(file_id="F1", person="P1", speaker_id="S9")


def test_the_list_shows_speaking_time_and_a_voice_that_goes_with_a_face(env):
    shots = [{"id": 0, "start": 0.0, "end": 20.0}, {"id": 1, "start": 20.0, "end": 40.0}]
    face = P.build_scan([{"t": t, "box": [0.4, 0.2, 0.1, 0.2], "px": 100, "score": 0.9, "vec": unit(0)} for t in (1.0, 10.0, 19.0)]
                        + [{"t": t, "box": [0.4, 0.2, 0.1, 0.2], "px": 100, "score": 0.9, "vec": unit(1)} for t in (21.0, 30.0, 39.0)], shots, 6, 40.0)
    P._write_json(P._scan_path(SHA1), face)
    P.assign_new_faces(SHA1, face)
    seed_voices(SHA1, ("S1", unit(0, 256)), ("S2", unit(1, 256)), segments=[(0.0, 19.0, "S1"), (21.0, 39.0, "S2")])
    _, r, out = run("list_people_tool", include_minor=True)
    by = {p["id"]: p for p in r["people"]}
    voice_people = [p for p in r["people"] if p["has_voice"] and not p["has_face"]]
    assert len(voice_people) == 2 and {p["speaking_seconds"] for p in voice_people} == {19.0, 18.0}
    linked = [p for p in voice_people if p["likely_same_as"]]
    assert len(linked) == 2 and by[linked[0]["likely_same_as"]["person"]]["has_face"] is True and linked[0]["likely_same_as"]["confidence"] > 0.5
    assert "embedding" not in out and "voices" not in out.replace("has_voice", "")


def test_search_by_person_includes_shots_where_their_voice_is_heard_and_says_so(env):
    seed(env, {SHA1: [unit(0), unit(1)]})              # faces: person 1 in shot 0, person 2 in shot 1
    seed_voices(SHA1, ("S1", unit(0, 256)), segments=[(11.0, 19.0, "S1")])        # a voice heard during shot 1
    P.merge_people("P1", "P3")                         # the user says that voice is person 1's
    P.name_person("P1", "Sam")
    _, r = tcall("search_footage_tool", person="sam")
    by_shot = {h["shot_id"]: h for h in r["hits"]}
    assert set(by_shot) == {0, 1} and by_shot[0]["person_heard"] is False and by_shot[1]["person_heard"] is True
    assert by_shot[1]["person_confidence"] == 1.0
