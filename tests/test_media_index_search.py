"""Search over a project's saved index: ranking, fusion, filters, snapping, and the dossier."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index import dossier, library, search  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

DIMS = 16


def unit(i):
    """A unit vector that is orthogonal to every other index."""
    v = np.zeros(DIMS, np.float32)
    v[i] = 1.0
    return v


def blend(i, j, w=0.5):
    v = unit(i) * (1 - w) + unit(j) * w
    return v / np.linalg.norm(v)


def profile(luma=0.5, warm=0.0, sat=0.3, contrast=0.4):
    return {"present": True, "avg_luma": luma, "warm_cool": warm, "sat_proxy": sat, "contrast_span": contrast,
            "clipped_shadows": 0.0, "clipped_highlights": 0.0, "channel_means": {"red": 0.5, "green": 0.5, "blue": 0.5, "luma": luma}}


def build(shelf, sha, *, shots, watch=None, speech=None, text=(), image=(), audio=None, duration=30.0, orientation="landscape",
          look=None, layers=("structure", "look", "watch", "speech", "vectors", "audio")):
    """Write one file's layers. text/image are lists of (row dict, vector)."""
    shelf.set_source(sha, duration=duration, media_type="video", orientation=orientation)
    if "structure" in layers:
        shelf.write_json(sha, "structure.json", {"shots": shots})
        shelf.set_layer(sha, "structure", version=1, status="ready")
    if "look" in layers:
        shelf.write_json(sha, "look.json", {"pipeline": {"hdr": False}, "warnings": [], "file": look or {"profile": profile()},
                                            "shots": [{"id": s["id"], "profile": s.get("_look") or profile(), "extras": {}} for s in shots]})
        shelf.set_layer(sha, "look", version=1, status="ready")
    if "watch" in layers and watch is not None:
        shelf.write_json(sha, "watch.json", {"shots": watch})
        shelf.set_layer(sha, "watch", version=1, status="ready")
    if "speech" in layers and speech is not None:
        shelf.write_json(sha, "speech.json", speech)
        shelf.set_layer(sha, "speech", version=1, status="ready")
    if "audio" in layers and audio is not None:
        shelf.write_json(sha, "audio.json", audio)
        shelf.set_layer(sha, "audio", version=1, status="ready")
    if "vectors" in layers and (text or image):
        shelf.write_json(sha, "vectors_index.json", {"dims": DIMS, "text": [r for r, _ in text], "image": [r for r, _ in image]})
        shelf.write_bytes(sha, "vectors_text.f16", np.stack([v for _, v in text]).astype("<f2").tobytes() if text else b"")
        shelf.write_bytes(sha, "vectors_image.f16", np.stack([v for _, v in image]).astype("<f2").tobytes() if image else b"")
        shelf.set_layer(sha, "vectors", version=1, status="ready")


def shot(i, start, end, cam="static", black=False, **extra):
    return {"id": i, "start": start, "end": end, "duration": end - start, "motion": {"class": cam, "level": 0.1}, "black": black, **extra}


def w(i, start, end, desc, **kw):
    return {"id": i, "start": start, "end": end, "description": desc, "actions": [], "objects": [], "on_screen_text": [],
            "sound_events": [], "mood": "", "shot_type": "", **kw}


@pytest.fixture
def shelf(tmp_path):
    library.clear_cache()
    return Shelf(str(tmp_path / "shelf"))


SHA1, SHA2 = "1" * 64, "2" * 64


@pytest.fixture
def two_files(shelf):
    shots1 = [shot(0, 0, 10, "pan", _look=profile(0.2, -0.1, 0.1)), shot(1, 10, 20, "static", _look=profile(0.7, 0.2, 0.6)), shot(2, 20, 30, "handheld", black=True)]
    build(shelf, SHA1, shots=shots1,
          watch=[w(0, 0, 10, "A calm lake", shot_type="wide", mood="calm", objects=[{"label": "lake"}], on_screen_text=[]),
                 w(1, 10, 20, "A dog runs on a beach", shot_type="medium", mood="playful", objects=[{"label": "dog"}], on_screen_text=[{"text": "SUMMER SALE"}]),
                 w(2, 20, 30, "")],
          speech={"sentences": [{"start": 12.0, "end": 14.5, "text": "Come here boy", "speaker": "A"}], "per_shot": {"0": {"speech_ratio": 0.0}, "1": {"speech_ratio": 0.6}, "2": {"speech_ratio": 0.0}}},
          text=[({"kind": "shot", "shot": 0, "start": 0, "end": 10, "text": "A calm lake"}, unit(0)),
                ({"kind": "shot", "shot": 1, "start": 10, "end": 20, "text": "A dog runs on a beach"}, unit(1)),
                ({"kind": "speech", "shot": None, "start": 12.0, "end": 14.5, "text": "Come here boy"}, unit(2))],
          image=[({"shot": 0, "t": 2.0}, unit(3)), ({"shot": 1, "t": 12.0}, unit(1) * 0.6 + unit(4) * 0.8), ({"shot": 1, "t": 17.0}, unit(5))],
          audio={"loudness": {"integrated_lufs": -18.2}, "tempo": {"bpm": 120.0, "beats": [0.5, 1.0]}, "silence_ranges": [[25.0, 30.0]]})
    build(shelf, SHA2, shots=[shot(0, 0, 6, "zoom_in"), shot(1, 6, 12, "static")], duration=12.0, orientation="portrait",
          watch=[w(0, 0, 6, "A chef chops onions", shot_type="close", mood="busy"), w(1, 6, 12, "Steam rises from a pan", shot_type="close")],
          text=[({"kind": "shot", "shot": 0, "start": 0, "end": 6, "text": "A chef chops onions"}, unit(6)),
                ({"kind": "shot", "shot": 1, "start": 6, "end": 12, "text": "Steam rises from a pan"}, blend(1, 7, 0.4))],
          image=[({"shot": 0, "t": 3.0}, unit(6)), ({"shot": 1, "t": 9.0}, unit(8))])
    f1 = library.load_file_index(shelf, SHA1, file_id="F1", name="beach.mp4")
    f2 = library.load_file_index(shelf, SHA2, file_id="F2", name="kitchen.mp4")
    return [f1, f2]


# ============================ library ============================
def test_a_file_with_nothing_on_the_shelf_has_no_index(shelf):
    assert library.load_file_index(shelf, SHA1) is None


def test_layers_are_joined_by_shot(two_files):
    f1 = two_files[0]
    assert f1.name == "beach.mp4" and f1.duration == 30.0 and f1.orientation == "landscape" and len(f1.shots) == 3
    s1 = f1.shots[1]
    assert s1["watch"]["description"] == "A dog runs on a beach" and s1["speech"]["speech_ratio"] == 0.6
    assert s1["look"]["avg_luma"] == 0.7 and s1["motion"]["class"] == "static"
    assert f1.text_matrix.shape == (3, DIMS) and f1.image_matrix.shape == (3, DIMS) and f1.layers["audio"] is True
    assert f1.shot_at(15.0)["id"] == 1 and f1.shot_at(30.0)["id"] == 2 and [s["id"] for s in f1.shots_between(9.0, 11.0)] == [0, 1]


def test_a_truncated_vector_file_means_no_vectors_never_wrong_ones(shelf, two_files):
    blob = shelf.read_bytes(SHA1, "vectors_text.f16")
    shelf.write_bytes(SHA1, "vectors_text.f16", blob[:-10])
    fi = library.load_file_index(shelf, SHA1)
    assert fi.text_matrix is None and fi.text_rows == [] and fi.image_matrix is not None


def test_the_loaded_index_is_cached_and_refreshed_when_a_layer_changes(shelf, two_files):
    a = library.get_file_index(shelf, SHA1, file_id="F1")
    assert library.get_file_index(shelf, SHA1, file_id="F1") is a
    import time
    time.sleep(0.01)
    shelf.set_layer(SHA1, "audio", version=1, status="ready", note="again")
    assert library.get_file_index(shelf, SHA1, file_id="F1") is not a


def test_the_same_content_in_another_project_gets_its_own_file_id(shelf, two_files):
    a = library.get_file_index(shelf, SHA1, file_id="F1", name="a.mp4")
    b = library.get_file_index(shelf, SHA1, file_id="OTHER", name="b.mp4")
    assert (a.file_id, b.file_id, b.name) == ("F1", "OTHER", "b.mp4") and a.text_matrix is b.text_matrix


def test_the_cache_is_bounded_by_rows(shelf, two_files, monkeypatch):
    monkeypatch.setattr(library, "MAX_CACHED_ROWS", 7)
    library.get_file_index(shelf, SHA1)
    library.get_file_index(shelf, SHA2)
    assert len(library._cache) == 1


# ============================ ranking ============================
def top(res, n=1):
    return [(h["file_id"], h["shot_id"]) for h in res["hits"][:n]]


def test_a_text_query_finds_the_shot_whose_description_matches(two_files):
    res = search.search(two_files, query_vector=unit(0))
    assert top(res) == [("F1", 0)] and res["hits"][0]["scores"]["shot"] == pytest.approx(1.0, abs=1e-3)
    assert res["hits"][0]["why"] == "A calm lake" and res["ranked"] is True


def test_a_speech_match_snaps_to_the_sentence_not_the_whole_shot(two_files):
    res = search.search(two_files, query_vector=unit(2))
    h = res["hits"][0]
    assert (h["file_id"], h["shot_id"]) == ("F1", 1) and (h["start"], h["end"]) == (12.0, 14.5) and h["snapped_to"] == "speech"
    assert h["peak"] == 12.0 and h["why"] == "Come here boy"


def test_a_visual_match_snaps_to_the_shot_with_the_picture_time_as_the_peak(two_files):
    res = search.search(two_files, query_vector=unit(3))
    h = res["hits"][0]
    assert (h["file_id"], h["shot_id"]) == ("F1", 0) and (h["start"], h["end"]) == (0, 10) and h["peak"] == 2.0 and h["snapped_to"] == "shot"


def test_a_shot_matched_by_two_layers_beats_one_matched_by_one(two_files):
    q = unit(1)       # shot 1's description AND its first picture match; nothing else does
    res = search.search(two_files, query_vector=q)
    assert top(res) == [("F1", 1)]
    assert set(res["hits"][0]["scores"]) >= {"shot", "image"}


def test_weak_matches_are_noise_and_are_not_returned(two_files):
    res = search.search(two_files, query_vector=unit(15))
    assert res["hits"] == [] and res["total"] == 0


def test_spoken_only_ignores_what_is_seen(two_files):
    res = search.search(two_files, query_vector=unit(0), look_for="spoken")
    assert res["hits"] == []
    assert top(search.search(two_files, query_vector=unit(2), look_for="spoken")) == [("F1", 1)]


def test_on_screen_only_ignores_speech(two_files):
    assert search.search(two_files, query_vector=unit(2), look_for="on_screen")["hits"] == []
    assert top(search.search(two_files, query_vector=unit(0), look_for="on_screen")) == [("F1", 0)]


def test_results_rank_across_files(two_files):
    res = search.search(two_files, query_vector=unit(6))
    assert top(res) == [("F2", 0)]
    assert {h["file_id"] for h in search.search(two_files, query_vector=blend(1, 6, 0.5))["hits"]} == {"F1", "F2"}


def test_a_reference_look_ranks_the_shots_that_look_like_it(two_files):
    ref = profile(0.7, 0.2, 0.6)
    res = search.search(two_files, reference_look=ref)
    assert top(res)[0] == ("F1", 1) and res["hits"][0]["scores"]["look_distance"] == pytest.approx(0.0, abs=1e-3)


# ============================ filters ============================
def ids(res):
    return [(h["file_id"], h["shot_id"]) for h in res["hits"]]


def test_with_no_query_the_filters_alone_pick_the_shots(two_files):
    res = search.search(two_files, filters={"camera": "zoom_in"})
    assert ids(res) == [("F2", 0)] and res["ranked"] is False


def test_black_shots_are_left_out_unless_asked_for(two_files):
    assert ("F1", 2) not in ids(search.search(two_files))
    assert ("F1", 2) in ids(search.search(two_files, filters={"exclude_black": False}))


@pytest.mark.parametrize("filters,expected", [
    ({"shot_type": "close"}, [("F2", 0), ("F2", 1)]), ({"shot_type": "WIDE"}, [("F1", 0)]),
    ({"mood": "play"}, [("F1", 1)]), ({"object_label": "dog"}, [("F1", 1)]), ({"text_on_screen": "sale"}, [("F1", 1)]),
    ({"min_duration": 8}, [("F1", 0), ("F1", 1)]), ({"max_duration": 6.5}, [("F2", 0), ("F2", 1)]),
    ({"orientation": "portrait"}, [("F2", 0), ("F2", 1)]), ({"speech": True}, [("F1", 1)]),
    ({"camera": "pan"}, [("F1", 0)]),
])
def test_filters_use_the_measured_and_described_facts(two_files, filters, expected):
    assert ids(search.search(two_files, filters=filters)) == expected


def test_speech_false_excludes_talking_shots(two_files):
    got = ids(search.search(two_files, filters={"speech": False}))
    assert ("F1", 1) not in got and ("F1", 0) in got


@pytest.mark.parametrize("look,expected", [("dark", [("F1", 0)]), ("bright", [("F1", 1)]), ("warm", [("F1", 1)]), ("cool", [("F1", 0)]),
                                           ("saturated", [("F1", 1)]), ("muted", [("F1", 0)])])
def test_look_filters_read_the_measured_colour(two_files, look, expected):
    got = [x for x in ids(search.search(two_files, filters={"look": look})) if x[0] == "F1"]
    assert got == expected


def test_a_filter_narrows_a_ranked_query_without_reordering_the_rest(two_files):
    everything = ids(search.search(two_files, query_vector=blend(1, 6, 0.5)))
    only_f2 = ids(search.search(two_files, query_vector=blend(1, 6, 0.5), filters={"orientation": "portrait"}))
    assert only_f2 == [x for x in everything if x[0] == "F2"]


def test_unset_filters_exclude_nothing(two_files):
    base = ids(search.search(two_files))
    assert ids(search.search(two_files, filters={"camera": "", "shot_type": None, "min_duration": 0, "look": ""})) == base


def test_results_are_paged(two_files):
    first = search.search(two_files, limit=3)
    assert len(first["hits"]) == 3 and first["next"] == 3 and first["total"] == 4
    second = search.search(two_files, limit=3, offset=first["next"])
    assert len(second["hits"]) == 1 and second["next"] is None
    assert ids(first) + ids(second) == ids(search.search(two_files, limit=10))


# ============================ references ============================
def test_a_range_of_a_file_is_a_picture_reference_without_any_cloud_call(two_files):
    v = search.reference_vector_from(two_files[0], 0.0, 5.0)
    assert np.allclose(v, unit(3)) and search.reference_vector_from(two_files[0], 25.0, 29.0) is None
    both = search.reference_vector_from(two_files[0], 10.0, 20.0)
    assert np.linalg.norm(both) == pytest.approx(1.0, abs=1e-4)
    res = search.search(two_files, reference_vector=unit(3))
    assert top(res) == [("F1", 0)]


def test_a_look_reference_is_a_file_a_shot_or_a_range(two_files):
    assert search.reference_look_from(two_files[0])["avg_luma"] == 0.5
    one = search.reference_look_from(two_files[0], 10.0, 15.0)
    assert one["avg_luma"] == 0.7
    both = search.reference_look_from(two_files[0], 0.0, 20.0)
    assert both["avg_luma"] == pytest.approx(0.45, abs=0.01)
    assert search.reference_look_from(two_files[0], 100.0, 110.0) is None


# ============================ dossier ============================
def test_the_dossier_tells_what_happens_is_said_seen_and_heard(two_files):
    d = dossier.build_dossier(two_files[0])
    text = d["text"]
    assert text.startswith("FILE beach.mp4 (video, 0:30.0 long, landscape, 3 shots)")
    assert "loudness -18.2 LUFS" in text and "tempo 120 BPM" in text and "silent 0:25.0-0:30.0" in text
    assert "[0:10.0-0:20.0] shot 1 (medium, static, playful): A dog runs on a beach" in text
    assert 'says: A: "Come here boy"' in text and "sees: dog" in text and 'text: "SUMMER SALE"' in text and "look: luma 0.70" in text
    assert "BLACK" in text and d["shots_shown"] == 3 and d["next_start"] is None


def test_a_dossier_can_cover_just_a_range(two_files):
    d = dossier.build_dossier(two_files[0], 11.0, 15.0)
    assert d["shots_in_range"] == 1 and "shot 1" in d["text"] and "shot 0" not in d["text"]


def test_a_long_dossier_stops_and_says_where_to_continue(two_files):
    d = dossier.build_dossier(two_files[0], max_chars=330)
    assert d["shots_shown"] >= 1 and d["next_start"] is not None and "continue from" in d["text"]
    again = dossier.build_dossier(two_files[0], d["next_start"], None, max_chars=6000)
    assert again["shots_shown"] >= 1


def test_a_dossier_names_the_layers_that_are_still_missing(shelf):
    build(shelf, SHA1, shots=[shot(0, 0, 5)], layers=("structure",))
    d = dossier.build_dossier(library.load_file_index(shelf, SHA1, name="x.mp4"))
    assert "NOT INDEXED YET" in d["text"] and "watch" in d["text"] and "vectors" in d["text"]


def test_an_empty_range_says_so(two_files):
    assert "no analysed shots" in dossier.build_dossier(two_files[1], 500.0, 600.0)["text"]


# ============================ the same footage twice ============================
def test_the_same_footage_in_two_project_files_is_one_result_and_names_the_other(shelf, two_files):
    twin = library.load_file_index(shelf, SHA1, file_id="F1-COPY", name="beach copy.mp4")
    res = search.search([two_files[0], twin, two_files[1]], query_vector=unit(0))
    mine = [h for h in res["hits"] if h["shot_id"] == 0 and h["sha"] == SHA1]
    assert len(mine) == 1, "ranked once, not twice"
    assert mine[0]["file_id"] == "F1" and mine[0]["same_content_files"] == ["F1-COPY"], "the first file wins, deterministically"
    swapped = search.search([twin, two_files[0]], query_vector=unit(0))
    assert swapped["hits"][0]["file_id"] == "F1-COPY" and swapped["hits"][0]["same_content_files"] == ["F1"]


def test_a_file_without_a_twin_has_no_same_content_note(two_files):
    assert all(h["same_content_files"] is None for h in search.search(two_files, query_vector=unit(0))["hits"])


def test_duplicates_do_not_inflate_the_ranking(shelf, two_files):
    twin = library.load_file_index(shelf, SHA1, file_id="F1-COPY", name="copy")
    once = search.search(two_files, query_vector=unit(1))["hits"][0]["score"]
    twice = search.search([two_files[0], twin, two_files[1]], query_vector=unit(1))["hits"][0]["score"]
    assert once == twice


# ============================ editing filters and sorting ============================
def set_quality(fi, shot_id, **q):
    shot_ = next(s for s in fi.shots if s["id"] == shot_id)
    shot_["quality"] = {**(shot_.get("quality") or {}), **q}


def test_usable_only_leaves_out_blurry_black_and_model_judged_unusable_shots_but_keeps_shaky_ones(two_files):
    f1, f2 = two_files
    set_quality(f1, 0, flags=["blurry"], highlight=0.5)
    set_quality(f1, 1, flags=["shaky"], highlight=0.5)
    set_quality(f2, 0, flags=[], highlight=0.5, inferred={"usable": False})
    set_quality(f2, 1, flags=[], highlight=0.5, inferred={"usable": True})
    got = ids(search.search(two_files, filters={"usable_only": True}))
    assert ("F1", 0) not in got and ("F2", 0) not in got and ("F1", 1) in got and ("F2", 1) in got
    everything = ids(search.search(two_files))
    assert ("F1", 0) in everything and ("F2", 0) in everything, "off by default"


def test_min_highlight_keeps_only_striking_moments(two_files):
    f1, f2 = two_files
    for fi, sid, h in ((f1, 0, 0.2), (f1, 1, 0.8), (f2, 0, 0.6), (f2, 1, 0.4)):
        set_quality(fi, sid, highlight=h)
    assert ids(search.search(two_files, filters={"min_highlight": 0.5})) == [("F1", 1), ("F2", 0)]
    assert len(ids(search.search(two_files, filters={"min_highlight": 0}))) == 4


def test_sorting_by_highlight_puts_the_best_moments_first_and_by_time_follows_the_calendar(two_files):
    f1, f2 = two_files
    for fi, sid, h in ((f1, 0, 0.2), (f1, 1, 0.8), (f2, 0, 0.6), (f2, 1, 0.4)):
        set_quality(fi, sid, highlight=h)
    f1.captured_at, f2.captured_at = "2024-05-03T10:00:00+00:00", "2024-05-01T10:00:00+00:00"
    assert ids(search.search(two_files, sort="highlight")) == [("F1", 1), ("F2", 0), ("F2", 1), ("F1", 0)]
    assert ids(search.search(two_files, sort="chronological")) == [("F2", 0), ("F2", 1), ("F1", 0), ("F1", 1)]
    assert ids(search.search(two_files, sort="relevance")) == [("F1", 0), ("F1", 1), ("F2", 0), ("F2", 1)]


def test_a_ranked_query_can_still_be_sorted_by_highlight(two_files):
    set_quality(two_files[0], 0, highlight=0.1)
    set_quality(two_files[0], 1, highlight=0.9)
    res = search.search(two_files[:1], query_vector=blend(0, 1, 0.5), sort="highlight")
    assert [(h["file_id"], h["shot_id"]) for h in res["hits"]][:2] == [("F1", 1), ("F1", 0)] and res["hits"][0]["highlight"] == 0.9


def test_hits_report_their_highlight_and_when_they_were_shot(two_files):
    two_files[0].captured_at = "2024-05-01T09:00:00+00:00"
    set_quality(two_files[0], 0, highlight=0.7)
    hit = search.search(two_files)["hits"][0]
    assert hit["highlight"] == 0.7 and hit["captured_at"] == "2024-05-01T09:00:00+00:00"
    assert [h["captured_at"] for h in search.search(two_files)["hits"] if h["file_id"] == "F2"][0] is None


def test_the_loaded_index_refreshes_when_the_files_source_facts_change(shelf, two_files):
    a = library.get_file_index(shelf, SHA1, file_id="F1")
    assert a.captured_at == ""
    shelf.set_source(SHA1, captured_at="2024-05-01T09:00:00+00:00")
    b = library.get_file_index(shelf, SHA1, file_id="F1")
    assert b.captured_at == "2024-05-01T09:00:00+00:00" and b is not a


# ============================ the cut-offs below which a match is noise ============================
def at_cosine(i, c):
    """A query whose cosine with ``unit(i)`` is exactly *c* (and with every other row about zero)."""
    v = unit(i) * c + unit(DIMS - 1) * float(np.sqrt(1 - c * c))      # the last axis is used by no row
    return v / np.linalg.norm(v)


def test_the_calibrated_cut_offs_are_the_ones_measured_on_labelled_queries():
    assert search.MIN_COSINE == {"shot": 0.40, "speech": 0.52, "image": 0.36}


@pytest.mark.parametrize("layer,row,floor", [("shot", 0, search.MIN_COSINE["shot"]), ("speech", 2, search.MIN_COSINE["speech"]), ("image", 3, search.MIN_COSINE["image"])])
def test_a_match_just_under_its_cut_off_is_dropped_and_just_over_is_kept(two_files, layer, row, floor):
    assert search.search(two_files, query_vector=at_cosine(row, floor - 0.02))["hits"] == [], f"{layer}: noise"
    kept = search.search(two_files, query_vector=at_cosine(row, floor + 0.02))["hits"]
    assert kept and layer in kept[0]["scores"], f"{layer}: a real match"


def test_a_speech_score_that_passed_the_old_cut_off_is_now_noise(two_files):
    assert search.search(two_files, query_vector=at_cosine(2, 0.46))["hits"] == []


def test_each_kind_of_row_is_held_to_its_own_floor_whichever_is_lower(two_files, monkeypatch):
    monkeypatch.setattr(search, "MIN_COSINE", {"shot": 0.40, "speech": 0.30, "image": 0.36})
    assert search.search(two_files, query_vector=at_cosine(2, 0.35))["hits"], "a spoken match above a lower speech floor is kept"
    assert search.search(two_files, query_vector=at_cosine(0, 0.35))["hits"] == [], "a shot description under its own floor is not"
