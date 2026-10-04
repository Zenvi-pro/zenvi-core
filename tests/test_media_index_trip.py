"""Days and places from capture time and GPS."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes.media_index import trip  # noqa: E402

SF = {"lat": 37.7749, "lon": -122.4194}
SF_NEAR = {"lat": 37.7790, "lon": -122.4150}          # about 0.6 km away
OAKLAND = {"lat": 37.8044, "lon": -122.2712}          # about 13 km away
TOKYO = {"lat": 35.6762, "lon": 139.6503}


def f(fid, when, gps=None, minutes=1.0):
    return {"file_id": fid, "captured_at": when, "gps": gps, "duration": minutes * 60.0}


def test_haversine_matches_known_distances():
    assert trip.haversine_km(SF, SF) == 0.0
    assert trip.haversine_km(SF, OAKLAND) == pytest.approx(13.4, abs=0.5)
    assert trip.haversine_km(SF, TOKYO) == pytest.approx(8280, abs=40)
    assert trip.haversine_km({"lat": 0, "lon": 179.9}, {"lat": 0, "lon": -179.9}) == pytest.approx(22.2, abs=0.5)   # across the date line


def test_clips_are_ordered_in_time_not_in_the_order_given():
    out = trip.trip_outline([f("c", "2024-05-01T12:00:00+00:00"), f("a", "2024-05-01T09:00:00+00:00"), f("b", "2024-05-01T10:30:00+00:00")])
    assert [d["file_ids"] for d in out["days"]] == [["a", "b", "c"]]


def test_a_long_gap_starts_a_new_day_and_a_late_evening_stays_with_its_day():
    out = trip.trip_outline([
        f("morning", "2024-05-01T09:00:00+00:00"), f("late", "2024-05-01T23:30:00+00:00"), f("after_midnight", "2024-05-02T00:40:00+00:00"),
        f("next_day", "2024-05-02T09:00:00+00:00")])
    # 09:00 -> 23:30 is a 14.5 h gap (a new day); 23:30 -> 00:40 is 1 h 10 (the same evening); 00:40 -> 09:00 is 8 h 20 (a new day)
    assert [d["file_ids"] for d in out["days"]] == [["morning"], ["late", "after_midnight"], ["next_day"]]
    assert [d["day"] for d in out["days"]] == list(range(1, len(out["days"]) + 1))


def test_the_gap_threshold_decides_where_days_break():
    clips = [f("a", "2024-05-01T09:00:00+00:00"), f("b", "2024-05-01T13:00:00+00:00")]      # 4 h apart
    assert len(trip.trip_outline(clips)["days"]) == 1
    assert len(trip.trip_outline(clips, day_gap_hours=3)["days"]) == 2


def test_nearby_fixes_are_one_place_and_far_ones_are_new_places_in_order_of_arrival():
    out = trip.trip_outline([f("a", "2024-05-01T09:00:00+00:00", SF), f("b", "2024-05-01T10:00:00+00:00", SF_NEAR),
                             f("c", "2024-05-01T11:00:00+00:00", OAKLAND), f("d", "2024-05-01T12:00:00+00:00", SF)])
    assert [p["file_ids"] for p in out["places"]] == [["a", "b", "d"], ["c"]]
    assert out["days"][0]["place_ids"] == [0, 1]
    assert out["places"][0]["first_seen"] == "2024-05-01T09:00:00+00:00"
    assert out["places"][0]["lat"] == pytest.approx(37.7766, abs=0.003)


def test_the_place_radius_is_adjustable():
    clips = [f("a", "2024-05-01T09:00:00+00:00", SF), f("b", "2024-05-01T10:00:00+00:00", SF_NEAR)]
    assert len(trip.trip_outline(clips)["places"]) == 1
    assert len(trip.trip_outline(clips, place_radius_km=0.2)["places"]) == 2


def test_clips_without_a_time_are_listed_apart_and_clips_without_a_position_have_no_place():
    out = trip.trip_outline([f("dated", "2024-05-01T09:00:00+00:00"), f("nodate", None, SF), f("junk", "yesterday")])
    assert out["undated"] == ["nodate", "junk"] and out["days"][0]["file_ids"] == ["dated"] and out["places"] == []
    assert out["files_with_position"] == 1


def test_minutes_are_totalled_per_day_and_per_place():
    out = trip.trip_outline([f("a", "2024-05-01T09:00:00+00:00", SF, 2.0), f("b", "2024-05-01T10:00:00+00:00", SF, 3.5),
                             f("c", "2024-05-03T10:00:00+00:00", TOKYO, 1.0)])
    assert [d["minutes"] for d in out["days"]] == [5.5, 1.0] and [p["minutes"] for p in out["places"]] == [5.5, 1.0]
    assert [d["date"] for d in out["days"]] == ["2024-05-01", "2024-05-03"]


def test_an_empty_project_is_an_empty_outline_and_inputs_are_not_changed():
    assert trip.trip_outline([]) == {"days": [], "places": [], "undated": [], "files_with_position": 0}
    clips = [f("a", "2024-05-01T09:00:00+00:00", SF)]
    trip.trip_outline(clips)
    assert clips == [f("a", "2024-05-01T09:00:00+00:00", SF)]
