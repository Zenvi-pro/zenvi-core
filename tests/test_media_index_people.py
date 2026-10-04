"""People identity: the scan, the registry, who-is-who with confidence, and everything the privacy rules require."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes import info  # noqa: E402
from classes.media_index import faces, people as P, people_models as PM  # noqa: E402
from eval import corpus  # noqa: E402

SHA = "a" * 64


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"), raising=False)
    PM.clear_sessions()
    return tmp_path / "user"


def unit(i, dims=128):
    v = np.zeros(dims, np.float32)
    v[i] = 1.0
    return v


def blend(i, j, w):
    v = unit(i) * (1 - w) + unit(j) * w
    return (v / np.linalg.norm(v)).astype(np.float32)


# ============================ who is who ============================
def test_a_face_is_a_match_unsure_or_new_by_how_alike_it_is():
    reg = P.load_registry()
    p = P.new_person(reg, unit(0))
    same = P.match(reg, unit(0))
    assert same == {"person": p["id"], "confidence": 1.0, "status": "match", "candidate": None}
    close = P.match(reg, blend(0, 1, 0.6))              # cosine 0.857 * ... = 0.6 sin: pick one between the lines
    sim = float(np.dot(blend(0, 1, 0.6), unit(0)))
    assert (sim >= P.MATCH) == (close["status"] == "match")
    between = np.array([P.UNSURE_LOW + 0.05, np.sqrt(1 - (P.UNSURE_LOW + 0.05) ** 2)] + [0] * 126, np.float32)
    got = P.match(reg, between)
    assert got["status"] == "unsure" and got["person"] is None and got["candidate"] == p["id"] and got["confidence"] == pytest.approx(P.UNSURE_LOW + 0.05, abs=1e-2)
    assert P.match(reg, unit(5))["status"] == "new" and P.match(P.load_registry(), unit(5))["person"] is None


def test_the_lines_are_the_ones_measured_and_recorded():
    rec = json.loads((Path(__file__).parent / "eval" / "results" / "people_calibration.json").read_text())
    assert rec["chosen"]["MATCH"] == P.MATCH and rec["chosen"]["UNSURE_LOW"] == P.UNSURE_LOW
    assert rec["by_threshold"][f"{P.MATCH:.2f}"]["false_merge"] < 0.0002 and rec["by_threshold"][f"{P.MATCH:.2f}"]["same_accepted"] > 0.97


def test_exemplars_grow_with_new_looks_but_not_with_near_copies_and_stay_bounded():
    reg = P.load_registry()
    p = P.new_person(reg, unit(0))
    P._add_exemplar(p, unit(0))
    assert len(p["exemplars"]) == 1
    for k in range(1, 30):
        P._add_exemplar(p, blend(0, k, 0.5))
    assert 1 < len(p["exemplars"]) <= P.MAX_EXEMPLARS


# ============================ planning and tracks ============================
def test_sampling_is_every_half_second_in_a_shot_bounded_per_shot_and_per_file():
    shots = [{"id": 0, "start": 0.0, "end": 2.0}, {"id": 1, "start": 2.0, "end": 62.0}, {"id": 2, "start": 62.0, "end": 62.08}]
    t = P.sample_plan(shots, 70.0)
    assert [x for x in t if x < 2.0] == pytest.approx([0.05, 0.55, 1.05, 1.55])
    assert len([x for x in t if 2.0 <= x < 62.0]) == P.MAX_PER_SHOT and t == sorted(t)
    assert not [x for x in t if x >= 62.0], "a shot shorter than the margins has no sample"
    many = [{"id": i, "start": i * 4.0, "end": i * 4.0 + 4.0} for i in range(400)]
    assert len(P.sample_plan(many, 1600.0)) == P.MAX_SAMPLES


def det(t, vec, px=100, box=(0.1, 0.1, 0.2, 0.2)):
    return {"t": t, "box": list(box), "px": px, "score": 0.9, "vec": vec}


def test_faces_in_a_shot_are_grouped_by_who_they_look_like_with_their_sightings():
    a, b = unit(0), unit(1)
    tracks = P.group_into_tracks([det(0.1, a), det(0.6, b), det(1.1, a), det(1.6, b), det(2.1, a), det(2.6, None)])
    assert sorted(len(t["samples"]) for t in tracks) == [2, 3]


def test_a_scan_summarises_tracks_counts_faces_and_keeps_the_too_small_ones_unrecognised():
    shots = [{"id": 0, "start": 0.0, "end": 3.0}, {"id": 1, "start": 3.0, "end": 6.0}]
    ds = [det(0.5, unit(0)), det(1.0, unit(0), box=(0.5, 0.2, 0.2, 0.2)), det(1.0, unit(1)), det(1.5, None, px=20), det(4.0, unit(0)), det(9.0, unit(2))]
    scan = P.build_scan(ds, shots, sampled=12, duration=6.0)
    assert [(t["id"], t["shot"], t["count"]) for t in scan["tracks"]] == [("s0t1", 0, 2), ("s0t2", 0, 1), ("s1t1", 1, 1)]
    assert scan["faces_most_in_a_frame"] == {"0": 2, "1": 1} and scan["unrecognised"] == {"0": 1}
    assert scan["tracks"][0]["start"] == 0.5 and scan["tracks"][0]["end"] == 1.0 and scan["tracks"][0]["best_px"] == 100
    json.dumps(scan)


# ============================ the registry and the user's names ============================
def scan_of(*vectors):
    shots = [{"id": i, "start": i * 3.0, "end": i * 3.0 + 3.0} for i in range(len(vectors))]
    return P.build_scan([det(i * 3.0 + 0.5, v) for i, v in enumerate(vectors)], shots, len(vectors), len(vectors) * 3.0)


def test_new_faces_become_unnamed_people_and_a_returning_face_is_the_same_person():
    first = P.assign_new_faces(SHA, scan_of(unit(0), unit(1)))
    assert first == {"matched": 0, "new": 2, "unsure": 0}
    again = P.assign_new_faces("b" * 64, scan_of(unit(1)))
    assert again == {"matched": 1, "new": 0, "unsure": 0}
    reg = P.load_registry()
    assert [(p["id"], p["name"]) for p in reg["people"]] == [("P1", None), ("P2", None)]
    rows = P.person_summary(reg, {SHA: scan_of(unit(0), unit(1)), "b" * 64: scan_of(unit(1))})
    assert rows[0]["id"] == "P2" and rows[0]["files"] == 2 and rows[0]["shots"] == 2 and rows[1]["id"] == "P1" and rows[1]["files"] == 1


def test_a_possible_match_is_reported_unsure_and_never_merged():
    P.assign_new_faces(SHA, scan_of(unit(0)))
    maybe = np.array([P.UNSURE_LOW + 0.05, np.sqrt(1 - (P.UNSURE_LOW + 0.05) ** 2)] + [0] * 126, np.float32)
    counts = P.assign_new_faces("b" * 64, scan_of(maybe))
    assert counts == {"matched": 0, "new": 0, "unsure": 1} and len(P.load_registry()["people"]) == 1
    tr = P.resolve_tracks("b" * 64, scan_of(maybe), P.load_registry())[0]
    assert tr["person"] is None and tr["status"] == "unsure" and tr["candidate"] == "P1"


def test_naming_is_the_users_alone_and_checks_what_it_is_given():
    P.assign_new_faces(SHA, scan_of(unit(0), unit(1)))
    assert P.load_registry()["people"][0]["name"] is None, "nobody is named automatically"
    assert P.name_person("P1", "  Sam   Lee ") == {"id": "P1", "name": "Sam Lee", "same_name_as": None}
    assert P.name_person("P2", "sam lee")["same_name_as"] == "P1", "a repeated name is flagged, not refused"
    with pytest.raises(ValueError):
        P.name_person("P1", "  ")
    with pytest.raises(KeyError):
        P.name_person("P9", "X")


def test_merging_moves_exemplars_pins_and_a_missing_name_and_refuses_nonsense():
    P._write_json(P._scan_path(SHA), scan_of(unit(0), unit(1)))
    P.assign_new_faces(SHA, scan_of(unit(0), unit(1)))
    P.name_person("P2", "Maya")
    P.pin_track(SHA, "s1t1", "P2")
    out = P.merge_people("P1", "P2")
    reg = P.load_registry()
    assert out == {"kept": "P1", "merged": "P2", "name": "Maya"} and [p["id"] for p in reg["people"]] == ["P1"] and set(reg["pins"].values()) == {"P1"}
    assert P.match(reg, unit(1))["person"] == "P1", "the folded person's face now matches the kept one"
    for bad in (("P1", "P1"),):
        with pytest.raises(ValueError):
            P.merge_people(*bad)
    with pytest.raises(KeyError):
        P.merge_people("P1", "P7")


def test_the_user_can_say_who_a_track_is_or_that_it_is_someone_new():
    scan = scan_of(unit(0), blend(0, 1, 0.9))
    P._write_json(P._scan_path(SHA), scan)
    P.assign_new_faces(SHA, scan)
    reg = P.load_registry()
    assert len(reg["people"]) == 2
    P.pin_track(SHA, "s1t1", "P1")
    assert [t["person"] for t in P.resolve_tracks(SHA, scan, P.load_registry())] == ["P1", "P1"]
    assert P.resolve_tracks(SHA, scan, P.load_registry())[1]["status"] == "pinned"
    moved = P.pin_track(SHA, "s1t1", None)
    assert moved["person"] == "P3" and P.resolve_tracks(SHA, scan, P.load_registry())[1]["person"] == "P3"
    with pytest.raises(KeyError):
        P.pin_track(SHA, "s9t9", "P1")
    with pytest.raises(KeyError):
        P.pin_track(SHA, "s0t1", "P99")


def test_appearances_give_when_where_and_how_sure():
    scan = scan_of(unit(0), unit(1), unit(0))
    P.assign_new_faces(SHA, scan)
    got = P.appearances(SHA, scan, P.load_registry(), "P1")
    assert [(a["shot"], a["start"]) for a in got] == [(0, 0.5), (2, 6.5)] and all(a["status"] == "match" and a["box"] == [0.1, 0.1, 0.2, 0.2] for a in got)


# ============================ storage, privacy, deletion ============================
def test_files_are_owner_only_and_a_failed_write_leaves_nothing(home):
    P.assign_new_faces(SHA, scan_of(unit(0)))
    P._write_json(P._scan_path(SHA), scan_of(unit(0)))
    for path in (P._registry_path(), P._scan_path(SHA)):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(P.people_root()).st_mode) == 0o700
    with pytest.raises(TypeError):
        P._write_json(P._scan_path("c" * 64), {"x": object()})
    assert not [n for n in os.listdir(os.path.dirname(P._scan_path(SHA))) if n.endswith(".part")] and not os.path.exists(P._scan_path("c" * 64))


def test_delete_all_removes_scans_registry_and_optionally_the_models(home):
    P.assign_new_faces(SHA, scan_of(unit(0)))
    P._write_json(P._scan_path(SHA), scan_of(unit(0)))
    os.makedirs(PM.models_dir(), exist_ok=True)
    Path(PM.models_dir(), PM.MODELS["detector"]["file"]).write_bytes(b"x")
    assert P.has_data()
    out = P.delete_all()
    assert out == {"scans": 1, "registry": True, "models": 0} and not os.path.exists(P.people_root()) and not P.has_data()
    assert P.load_registry()["people"] == [] and P.load_scan(SHA) is None
    assert os.path.exists(os.path.join(PM.models_dir(), PM.MODELS["detector"]["file"])), "the models stay unless asked"
    assert P.delete_all(include_models=True)["models"] == 1
    assert P.delete_all() == {"scans": 0, "registry": False, "models": 0}, "deleting nothing is fine"


def test_people_data_is_outside_the_shelf_so_it_is_never_exported_with_it(home, tmp_path):
    from classes.media_index.store import Shelf
    shelf = Shelf(str(home / "media_index"))
    shelf.set_source(SHA, duration=3.0, has_audio=False, media_type="video")
    shelf.write_json(SHA, "structure.json", {"shots": []})
    shelf.set_layer(SHA, "structure", version=1, status="ready")
    P._write_json(P._scan_path(SHA), scan_of(unit(0)))
    P.assign_new_faces(SHA, scan_of(unit(0)))
    assert not Path(P.people_root()).resolve().is_relative_to(Path(shelf.root).resolve())
    dest = tmp_path / "project_index"
    shelf.export_entries([SHA], str(dest))
    found = [p.name for p in dest.rglob("*") if p.is_file()]
    assert "structure.json" in found, "the shelf entry itself was exported"
    assert not any("people" in n or "registry" in n for n in found) and "embedding" not in " ".join(
        (p.read_text(errors="ignore") for p in dest.rglob("*.json") if p.is_file()))


def test_nothing_in_the_face_code_can_reach_the_network_or_the_cloud_client():
    for name in ("faces.py", "people.py"):
        src = (SRC / "classes" / "media_index" / name).read_text()
        for banned in ("urllib", "requests", "http.client", "socket", "api_client", "websocket", "aiohttp", "httpx"):
            assert not re.search(rf"^\s*(import|from)\s+{re.escape(banned)}\b", src, re.M), f"{name} imports {banned}"
    assert "urllib" in (SRC / "classes" / "media_index" / "people_models.py").read_text(), "only the model download fetches, and only the models"


def test_scanning_logs_counts_and_never_the_numbers_of_a_face(monkeypatch):
    messages = []
    monkeypatch.setattr(P.log, "info", lambda msg, *a, **k: messages.append(msg % a if a else msg))
    monkeypatch.setattr(P.log, "warning", lambda msg, *a, **k: messages.append(msg % a if a else msg))
    monkeypatch.setattr(P, "stream_frames", lambda *a, **k: iter([(0.55, np.zeros((90, 160, 3), np.uint8))]))
    monkeypatch.setattr(faces, "detect", lambda *_a, **_k: [{"box": [10.0, 10.0, 60.0, 60.0], "score": 0.9, "kps": faces.TEMPLATE[:5]}])
    monkeypatch.setattr(faces, "align", lambda *_a, **_k: np.zeros((112, 112, 3), np.uint8))
    monkeypatch.setattr(faces, "embed", lambda *_a, **_k: unit(3))
    scan = P.scan_file("/x.mp4", SHA, [{"id": 0, "start": 0.0, "end": 3.0}], 3.0, (160, 90), detector=object(), recogniser=object())
    secret = scan["tracks"][0]["embedding"]
    assert messages and all(secret not in m and "embedding" not in m.lower() for m in messages), messages
    assert re.search(r"\d+ track", messages[0])


# ============================ the scan on a real clip, with stand-in models ============================
@pytest.fixture
def blocks_video(tmp_path):
    try:
        ff = corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")
    path = tmp_path / "blocks.mp4"
    colours = ["0xff0000", "0x0000ff", "0xff0000"]
    cmd = [ff, "-y", "-v", "error"]
    for c in colours:
        cmd += ["-f", "lavfi", "-i", f"color=c=0x303030:s=320x180:r=25:d=3,drawbox=x=100:y=40:w=100:h=100:color={c}:t=fill"]
    cmd += ["-filter_complex", "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return str(path)


class BlockDetector:
    """Finds the coloured square: a 'face' wherever the picture has a strongly red or blue block."""

    def get_inputs(self):
        return [SimpleNamespace(name="input")]


def test_a_clip_is_scanned_into_tracks_and_the_returning_look_is_the_same_person(blocks_video, monkeypatch):
    def fake_detect(_session, frame):
        r, b = frame[..., 0].astype(int), frame[..., 2].astype(int)
        mask = (np.abs(r - b) > 150)
        if mask.sum() < 500:
            return []
        ys, xs = np.nonzero(mask)
        x1, y1, x2, y2 = xs.min(), ys.min(), xs.max(), ys.max()
        return [{"box": [float(x1), float(y1), float(x2 - x1), float(y2 - y1)], "score": 0.95, "kps": faces.TEMPLATE * 0.5 + np.array([x1, y1])}]

    monkeypatch.setattr(faces, "detect", fake_detect)
    monkeypatch.setattr(faces, "align", lambda frame, kps, size=112: frame[60:172, 100:212] if frame.shape[0] >= 172 else np.zeros((112, 112, 3), np.uint8))
    monkeypatch.setattr(faces, "embed", lambda session, aligned: unit(0) if aligned[..., 0].mean() > aligned[..., 2].mean() else unit(1))
    shots = [{"id": 0, "start": 0.0, "end": 3.0}, {"id": 1, "start": 3.0, "end": 6.0}, {"id": 2, "start": 6.0, "end": 9.0}]
    progress = []
    scan = P.scan_file(blocks_video, SHA, shots, 9.0, (320, 180), detector=object(), recogniser=object(), on_progress=progress.append)
    assert scan["sampled"] >= 15 and [(t["shot"], t["count"] > 2) for t in scan["tracks"]] == [(0, True), (1, True), (2, True)] and progress
    assert P.load_scan(SHA)["tracks"][0]["id"] == "s0t1"
    P.assign_new_faces(SHA, scan)
    who = [t["person"] for t in P.resolve_tracks(SHA, scan, P.load_registry())]
    assert who[0] == who[2] != who[1], "red, blue, red: the first and last shots are the same 'person'"
    assert all(0.0 <= s["box"][0] <= 1.0 and 0.0 <= s["box"][1] <= 1.0 for t in scan["tracks"] for s in t["samples"]), "boxes are fractions of the picture"


def test_a_cancelled_scan_saves_nothing(blocks_video, monkeypatch):
    monkeypatch.setattr(faces, "detect", lambda *_a, **_k: [])
    with pytest.raises(InterruptedError):
        P.scan_file(blocks_video, SHA, [{"id": 0, "start": 0.0, "end": 3.0}], 9.0, (320, 180), detector=object(), recogniser=object(), should_cancel=lambda: True)
    assert P.load_scan(SHA) is None and not P.has_data()


# ============================ the models ============================
def fake_fetch(blobs):
    def fetch(url):
        for key, data in blobs.items():
            if url.endswith(key):
                return io.BytesIO(data)
        raise OSError("not found")
    return fetch


@pytest.fixture
def tiny_models(monkeypatch):
    import hashlib
    blobs = {}
    spec = {}
    for name, spec_ in PM.MODELS.items():
        data = (name * 50).encode()
        blobs[spec_["file"]] = data
        spec[name] = {**spec_, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    monkeypatch.setattr(PM, "MODELS", spec)
    return blobs


def test_a_download_is_checked_against_its_hash_before_it_is_installed(home, tiny_models):
    assert PM.status()["models"] == {"detector": False, "recognizer": False} and PM.status()["download_bytes"] > 0
    steps = []
    out = PM.download(progress=steps.append, fetch=fake_fetch(tiny_models))
    assert out == {"ok": True, "installed": ["detector", "recognizer"], "error": None} and steps[-1] == 1.0
    assert PM.status()["models"] == {"detector": True, "recognizer": True} and PM.status()["download_bytes"] == 0
    assert PM.download(fetch=fake_fetch({}))["installed"] == [], "nothing is fetched twice"


def test_a_download_that_does_not_match_installs_nothing_and_leaves_no_part_file(home, tiny_models):
    bad = {k: v + b"tampered" for k, v in tiny_models.items()}
    out = PM.download(fetch=fake_fetch(bad))
    assert out["ok"] is False and "checksum" in out["error"] and out["installed"] == []
    assert PM.status()["models"] == {"detector": False, "recognizer": False}
    assert not [n for n in os.listdir(PM.models_dir()) if n.endswith(".part")]


def test_a_cancelled_or_failed_download_stops_cleanly(home, tiny_models):
    assert PM.download(fetch=fake_fetch(tiny_models), should_cancel=lambda: True)["error"] == "cancelled"
    out = PM.download(fetch=fake_fetch({}))
    assert out["ok"] is False and "could not download" in out["error"]
    assert not [n for n in os.listdir(PM.models_dir()) if n.endswith(".part")] and PM.status()["models"]["detector"] is False


def test_a_damaged_model_file_is_not_trusted(home, tiny_models):
    PM.download(fetch=fake_fetch(tiny_models))
    path = Path(PM.models_dir(), PM.MODELS["detector"]["file"])
    path.write_bytes(path.read_bytes()[:-1] + b"X")
    assert PM.model_path("detector") is None and PM.model_path("recognizer")


def test_a_missing_runtime_or_model_says_what_to_install(home, monkeypatch):
    monkeypatch.setattr(PM, "runtime_available", lambda: False)
    with pytest.raises(PM.PeopleUnavailable, match="onnxruntime"):
        PM.session("detector")
    monkeypatch.setattr(PM, "runtime_available", lambda: True)
    with pytest.raises(PM.PeopleUnavailable, match="not installed"):
        PM.session("detector")


def test_the_licences_and_sizes_the_user_is_told_are_the_ones_in_the_table():
    s = PM.status()
    assert s["licenses"] == {"detector": "MIT", "recognizer": "Apache-2.0"}
    assert PM.MODELS["recognizer"]["bytes"] < 40_000_000 and all(len(m["sha256"]) == 64 for m in PM.MODELS.values())


def test_a_face_the_user_corrected_is_left_alone_when_new_faces_are_assigned_again():
    scan = scan_of(unit(0), unit(1))
    P._write_json(P._scan_path(SHA), scan)
    assert P.assign_new_faces(SHA, scan) == {"matched": 0, "new": 2, "unsure": 0}
    P.pin_track(SHA, "s1t1", "P1")
    assert P.assign_new_faces(SHA, scan) == {"matched": 1, "new": 0, "unsure": 0}, "only the unpinned face is matched again"
    assert len(P.load_registry()["people"]) == 2


def test_a_face_too_small_to_recognise_is_counted_but_never_given_a_vector(monkeypatch):
    monkeypatch.setattr(P, "stream_frames", lambda *a, **k: iter([(0.55, np.zeros((90, 160, 3), np.uint8))]))
    monkeypatch.setattr(faces, "detect", lambda *_a, **_k: [{"box": [10.0, 10.0, faces.MIN_RECOGNISE_PX - 1.0, 60.0], "score": 0.9, "kps": faces.TEMPLATE[:5]}])
    monkeypatch.setattr(faces, "embed", lambda *_a, **_k: pytest.fail("a tiny face must not be embedded"))
    scan = P.scan_file("/x.mp4", SHA, [{"id": 0, "start": 0.0, "end": 3.0}], 3.0, (160, 90), detector=object(), recogniser=object())
    assert scan["tracks"] == [] and scan["unrecognised"] == {"0": 1} and scan["faces_most_in_a_frame"] == {"0": 1}
