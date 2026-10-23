"""Three scripted directors on a real project (real tools, real undo, a real voice recording): the Round 2 abilities used the way an agent would.

1. A trip short: places named offline, the overview without coordinates, search by place, a moment judged with what is around it.
2. A talking head: retakes found, the exact voice edges chosen, b-roll laid over it, and what is under and above the talk read before changing it.
3. A named person: found across the footage by the name the user gave, only their shots used, and a mask handed to the masking tools.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

from classes import info  # noqa: E402
from classes.editor_tools import media_index_tools_people as TP, media_index_tools_precision as TX  # noqa: E402
from classes.media_index import library, people as P  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from eval import corpus  # noqa: E402
from test_media_index_scenario_trip_short import MUSIC, PICTURE, receipt_data, sha, trip  # noqa: E402,F401
from test_media_index_search import build, profile, shot, w  # noqa: E402
from test_media_index_voice import LINE, s  # noqa: E402
from test_media_index_voice import w as word  # noqa: E402


def unit(i, dims=128):
    import numpy as np
    v = np.zeros(dims, np.float32)
    v[i] = 1.0
    return v


# ============================ 1. the trip short ============================
def test_a_trip_short_uses_place_names_and_judges_a_moment_with_its_surroundings(trip, monkeypatch):  # noqa: F811
    ed = trip.ed
    overview = receipt_data(ed, "get_project_overview_tool")
    places = overview["trip"]["places"]
    assert [p["name"] for p in places] == ["Kyoto, Japan", "Osaka, Japan"] and all("lat" not in p for p in places), "names reach the model, not coordinates"
    assert "GeoNames" in overview["trip"]["place_names"]

    kyoto = receipt_data(ed, "search_footage_tool", place="kyoto", sort="highlight", filters={"usable_only": True}, limit=50)["hits"]
    osaka = receipt_data(ed, "search_footage_tool", place="Osaka", limit=50)["hits"]
    kyoto_files = {h["file_id"] for h in kyoto}
    assert kyoto_files
    assert kyoto_files <= {trip.files[i] for i in range(8)}, "days 1-2 are Kyoto"
    assert osaka and {h["file_id"] for h in osaka} <= {trip.files[i] for i in range(8, 12)}, "day 3 is Osaka"
    assert not kyoto_files & {h["file_id"] for h in osaka}

    best = kyoto[0]
    receipt_data(ed, "add_clips_to_timeline_tool", items=[{"file_id": best["file_id"], "source_start": best["start"], "source_end": best["start"] + 4.0}], track=str(PICTURE), start_seconds=0)
    moment = receipt_data(ed, "get_moment_tool", file_ids=[best["file_id"]], start_seconds=best["start"], end_seconds=best["start"] + 4.0, frames=0)
    assert moment["shots"] and moment["timeline"]["uses"] and moment["timeline"]["uses"][0]["source"][0] == pytest.approx(best["start"], abs=0.3)
    assert moment["audio_picture"]["included"] is False and "numbers" in moment["audio_picture"]["why"]
    clip_id = moment["timeline"]["uses"][0]["timeline_clip_id"]
    context = receipt_data(ed, "get_clip_context_tool", timeline_clip_id=clip_id, if_edit="delete")
    assert context["context"]["clip"]["id"] == clip_id and context["prediction"]["shifted_count"] == 0, "nothing after it to move"


# ============================ 2. the talking head ============================
@pytest.fixture
def talking(editor, tmp_path, monkeypatch):
    from classes import clip_utils, timeline_ops
    from timeline_edit_fakes import FakeTimeline, fake_openshot
    try:
        corpus.ffmpeg()
    except RuntimeError:
        pytest.skip("needs ffmpeg")
    monkeypatch.setattr(timeline_ops, "openshot", fake_openshot())
    monkeypatch.setattr(clip_utils, "get_app", lambda: editor.app)
    editor.fake = FakeTimeline(editor)
    editor.window.timeline = editor.fake
    editor.window.selected_tracks = []
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "shelf"))
    from classes.editor_tools import media_index_tools as T, media_index_tools_edit as TE, media_index_tools_review as TR
    for module in (T, TE, TR, TX):
        monkeypatch.setattr(module, "default_shelf", lambda: shelf)
    monkeypatch.setattr("classes.path_utils.absolute_media_path", lambda p: p, raising=False)
    editor.add_track(PICTURE, "Picture")
    editor.add_track(PICTURE + 1, "B-roll")
    voice, _ = corpus.voice_bursts(spans=((1.0, 3.0), (4.5, 6.0), (7.5, 10.0)), seconds=11.0)
    talk_sha = sha(40)
    sentences = [s(0.8, 3.2, LINE), s(4.4, 6.2, "So um today we are going to make fresh pasta from scratch."), s(7.4, 10.2, LINE)]
    words = [word(t, t + 0.4, "So") for t in (1.0, 1.6, 2.2)] + [word(4.6, 4.9, "um")] + [word(t, t + 0.4, "So") for t in (5.0, 5.5)] \
        + [word(t, t + 0.4, "So") for t in (7.6, 8.2, 8.8, 9.4)]
    build(shelf, talk_sha, shots=[shot(0, 0, 11, "static")], watch=[w(0, 0, 11, "A chef talks to camera in a kitchen", shot_type="medium")], duration=11.0,
          look={"profile": profile(0.5)})
    shelf.write_json(talk_sha, "speech.json", {"words": words, "sentences": sentences})
    shelf.set_layer(talk_sha, "speech", version=1, status="ready")
    shelf.set_source(talk_sha, duration=11.0, media_type="video", has_audio=True)
    talk = editor.add_file("video", duration=11.0, path=str(voice), fingerprint={"sha256": talk_sha})
    broll = []
    for i, what in enumerate(("flour poured onto a wooden board", "a pot of water boiling", "dough rolled thin")):
        b_sha = sha(50 + i)
        build(shelf, b_sha, shots=[shot(0, 0, 8, "static")], watch=[w(0, 0, 8, what, shot_type="close")], duration=8.0, look={"profile": profile(0.55)})
        shelf.set_source(b_sha, duration=8.0, media_type="video", has_audio=False)
        broll.append(editor.add_file("video", duration=8.0, path=f"/media/broll_{i}.mp4", fingerprint={"sha256": b_sha}))
    editor.mark()
    library.clear_cache()
    return SimpleNamespace(ed=editor, talk=talk, broll=broll, shelf=shelf)


def test_a_talking_head_picks_the_best_take_cuts_on_the_exact_voice_and_lays_b_roll_over_it(talking):
    ed = talking.ed
    retakes = receipt_data(ed, "find_retakes_tool", file_ids=[talking.talk])
    group = retakes["groups"][0]
    assert len(group["takes"]) == 3 and group["likely_best"] == 3, "the line said three times; the later clean take is the starting pick"
    assert group["takes"][1]["fillers"], "the middle take has the 'um'"
    best = group["takes"][group["likely_best"] - 1]
    assert best["start"] == pytest.approx(7.4, abs=0.1) and not best["fillers"], "the clean last take, not the one with 'um'"

    edges = receipt_data(ed, "get_voice_edges_tool", file_ids=[talking.talk], start_seconds=best["start"], end_seconds=best["end"] + 0.3)["voice"]
    assert edges["found"] and edges["start"] == pytest.approx(7.5, abs=0.08) and edges["end"] == pytest.approx(10.0, abs=0.08), "the voice itself, not the transcript's guess"
    start, end = edges["start"] - 0.1, edges["end"] + 0.1                       # a breath before, a beat after: never on the very edge

    ed.mark()
    laid = receipt_data(ed, "add_clips_to_timeline_tool", items=[{"file_id": talking.talk, "source_start": start, "source_end": end}], track=str(PICTURE), start_seconds=0)
    assert len(laid["clips"]) == 1 and ed.undo_steps_since_mark() == 1
    talk_clip = laid["clips"][0].get("timeline_clip_id") or laid["clips"][0]["id"]

    hits = receipt_data(ed, "search_footage_tool", filters={"shot_type": "close"}, file_ids=talking.broll, limit=10)["hits"]
    assert len(hits) == 3, "b-roll found by what it is, without the cloud"
    at = 0.5
    for h in hits:
        receipt_data(ed, "add_clips_to_timeline_tool", items=[{"file_id": h["file_id"], "source_start": 0.0, "source_end": 0.8}], track=str(PICTURE + 1), start_seconds=at)
        at += 0.8

    context = receipt_data(ed, "get_clip_context_tool", timeline_clip_id=talk_clip, if_edit="trim_end", seconds=1.0)
    above = context["context"]["above"]
    assert len(above) == 3 and all(a["hides"] for a in above), "three b-roll cutaways sit over the talk and hide it while they play"
    assert context["context"]["visible"]["hidden_seconds"] == pytest.approx(2.2, abs=0.1), "cutaways from 0.5 s to 2.9 s over a 2.7 s talk clip: the overlap hides 2.2 s"
    assert context["prediction"]["shifted_count"] == 0 and context["changed"] is False

    moment = receipt_data(ed, "get_moment_tool", file_ids=[talking.talk], start_seconds=7.0, end_seconds=10.5, frames=0)
    assert moment["words"]["count"] >= 1 and moment["timeline"]["uses"][0]["above"], "the pack for this moment says what is laid over it"


# ============================ 3. a named person ============================
def test_a_named_person_is_found_across_the_footage_used_alone_and_handed_to_the_masking_tools(trip, tmp_path, monkeypatch):  # noqa: F811
    ed = trip.ed
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "user"), raising=False)
    monkeypatch.setattr("classes.media_index.flags.people_enabled", lambda: True)
    monkeypatch.setattr(TP, "people_enabled", lambda: True)
    sam_in = [1, 4, 7, 10]                                                     # files where one face is on screen; the others have someone else
    for i in range(12):
        shots = [{"id": 0, "start": 0.0, "end": 10.0}, {"id": 1, "start": 10.0, "end": 20.0}]
        vec = unit(0 if i in sam_in else 1 + i % 5)
        scan = P.build_scan([{"t": 3.0, "box": [0.4, 0.2, 0.1, 0.2], "px": 120, "score": 0.9, "vec": vec}], shots, 1, 20.0)
        P._write_json(P._scan_path(sha(i)), scan)
        P.assign_new_faces(sha(i), scan)
    for i in range(12):
        trip.shelf.set_source(sha(i), technical={"video": {"width": 1920, "height": 1080, "fps": 25.0}})
    library.clear_cache()

    people = receipt_data(ed, "list_people_tool", include_minor=True)["people"]
    assert people[0]["shots"] == 4 and people[0]["name"] is None, "the most-seen person is a number until the user says who"
    out = ed.call_receipt("name_person_tool", name="Sam", person_id=people[0]["id"])
    assert out["status"] == "applied"

    found = receipt_data(ed, "search_footage_tool", person="sam", limit=50)
    assert {h["file_id"] for h in found["hits"]} == {trip.files[i] for i in sam_in} and found["unsure_shots"] == 0
    assert all(h["shot_id"] == 0 and h["person_confidence"] == 1.0 for h in found["hits"]), "only the shot Sam is in"

    items = [{"file_id": h["file_id"], "source_start": h["start"], "source_end": min(h["end"], h["start"] + 2.0)} for h in found["hits"]]
    laid = receipt_data(ed, "add_clips_to_timeline_tool", items=items, track=str(PICTURE), start_seconds=0)
    assert len(laid["clips"]) == 4

    for i in sam_in:
        pf = next(f for f in library_files(ed) if str(f.id) == trip.files[i])
        pf.data.update(width=1920, height=1080, video_length=500)
    located = receipt_data(ed, "locate_person_tool", person="Sam", for_action="blur_object")
    handoff = located["hits"][0]["handoff"]
    assert handoff["tool"] == "enhance_file_with_comfyui_tool" and handoff["args"]["action"] == "blur_object" and handoff["args"]["prompt"] == "Sam"
    box = handoff["args"]["boxes"][0]
    assert 0 <= box["x1"] < box["x2"] <= 1920 and 0 <= box["y1"] < box["y2"] <= 1080 and handoff["args"]["seed_frame"] == 76, "a mask seed on the frame Sam is in"

    gone = receipt_data(ed, "erase_people_data_tool", confirm=True)
    assert gone["removed"]["registry"] is True and not P.has_data()
    assert ed.call_receipt("search_footage_tool", person="Sam")["status"] != "applied", "after erasing, nobody is called Sam"


def library_files(ed):
    from classes.query import File
    return File.filter()
