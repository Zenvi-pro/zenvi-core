"""Offline place names: known places, the edges of the map, the naming policy, and that nothing leaves the machine."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from classes.media_index import gazetteer as G  # noqa: E402

# (what, lat, lon, the names that are right for it)
KNOWN = [
    ("Eiffel Tower", 48.8584, 2.2945, {"Paris"}), ("Statue of Liberty", 40.6892, -74.0445, {"New York City"}), ("Sydney Opera House", -33.8568, 151.2153, {"Sydney"}),
    ("Golden Gate Bridge", 37.8199, -122.4783, {"San Francisco"}), ("Big Ben", 51.5007, -0.1246, {"London"}), ("Shibuya crossing", 35.6595, 139.7005, {"Tokyo"}),
    ("Colosseum", 41.8902, 12.4922, {"Rome"}), ("Table Mountain", -33.9628, 18.4098, {"Cape Town"}), ("Brandenburg Gate", 52.5163, 13.3777, {"Berlin"}),
    ("Red Square", 55.7539, 37.6208, {"Moscow"}), ("Sagrada Familia", 41.4036, 2.1744, {"Barcelona"}), ("Taj Mahal", 27.1751, 78.0421, {"Agra"}),
    ("Christ the Redeemer", -22.9519, -43.2105, {"Rio de Janeiro"}), ("Times Square", 40.7580, -73.9855, {"New York City"}), ("Hollywood sign", 34.1341, -118.3215, {"Los Angeles"}),
    ("CN Tower", 43.6426, -79.3871, {"Toronto"}), ("Burj Khalifa", 25.1972, 55.2744, {"Dubai"}), ("Marina Bay Sands", 1.2834, 103.8607, {"Singapore"}),
    ("Gateway of India", 18.9220, 72.8347, {"Mumbai"}), ("Hagia Sophia", 41.0086, 28.9802, {"Istanbul"}), ("Reykjavik church", 64.1417, -21.9266, {"Reykjavik", "Reykjavík"}),
    ("Lisbon Belem Tower", 38.6916, -9.2160, {"Lisbon"}), ("Oakland downtown", 37.8044, -122.2712, {"Oakland"}), ("Tokyo Tower", 35.6586, 139.7454, {"Tokyo"}),
    ("Amsterdam Dam Square", 52.3731, 4.8932, {"Amsterdam"}), ("Vienna Stephansdom", 48.2086, 16.3731, {"Vienna"}), ("Cairo Tahrir", 30.0444, 31.2357, {"Cairo"}),
    ("Nairobi centre", -1.2864, 36.8172, {"Nairobi"}), ("Mexico City Zocalo", 19.4326, -99.1332, {"Mexico City"}), ("Auckland Sky Tower", -36.8484, 174.7622, {"Auckland"}),
]


@pytest.mark.parametrize("what,lat,lon,names", KNOWN, ids=[k[0] for k in KNOWN])
def test_a_known_place_is_named_after_its_city(what, lat, lon, names):
    got = G.describe(lat, lon)
    assert got is not None and got["name"] in names and not got["near"], (what, got)


def test_the_label_has_the_country_and_a_us_state_and_marks_a_nearby_place_as_near():
    assert G.describe(48.8584, 2.2945)["label"] == "Paris, France"
    assert G.describe(37.8199, -122.4783)["label"] == "San Francisco, CA, United States"
    machu = G.describe(-13.1631, -72.5450)
    assert machu["near"] is True and machu["label"].startswith("near ") and machu["label"].endswith("Peru") and 25 < machu["distance_km"] <= 150


def test_a_city_beats_its_own_districts_but_a_city_centre_beats_a_bigger_neighbour():
    assert G.nearest(48.8584, 2.2945)["name"] == "Paris", "Passy and the other arrondissements are listed too"
    assert G.nearest(37.8044, -122.2712)["name"] == "Oakland", "San Francisco is bigger and 13 km away"
    assert G.nearest(37.7749, -122.4194)["name"] == "San Francisco"


@pytest.mark.parametrize("what,lat,lon", [("open ocean", 0.0, 0.0), ("south pole", -90.0, 0.0), ("north pole", 90.0, 10.0), ("Sahara", 23.0, 12.0),
                                          ("outback", -25.3444, 131.0369), ("mid Pacific", -30.0, -140.0)])
def test_far_from_any_place_there_is_no_name_and_the_position_stays_a_coordinate(what, lat, lon):
    assert G.describe(lat, lon) is None and G.nearest(lat, lon)["distance_km"] > G.NEAR_KM, what


def test_the_date_line_and_the_poles_need_no_special_case():
    across = G.describe(-16.8, -179.9)                         # east of the date line, the nearest town is on the other side of it
    assert across is not None and across["country"] == "Fiji" and across["near"] is True
    assert G.describe(-16.8, 180.0)["label"] == G.describe(-16.8, -180.0)["label"]
    assert G.nearest(89.99, 0.0)["distance_km"] > 1000


@pytest.mark.parametrize("lat,lon", [(91, 0), (0, 181), (-91, 0), (0, -181), ("x", 1), (None, 1), (float("nan"), 0)])
def test_an_invalid_position_gets_no_answer(lat, lon):
    assert G.nearest(lat, lon) is None and G.describe(lat, lon) is None


def test_the_policy_lines_split_in_near_and_nothing_at_the_distances_they_say(monkeypatch):
    monkeypatch.setattr(G, "nearest", lambda *a, **k: {"name": "X", "state": None, "country": "Y", "distance_km": G.IN_KM})
    assert G.describe(0, 0)["near"] is False
    monkeypatch.setattr(G, "nearest", lambda *a, **k: {"name": "X", "state": None, "country": "Y", "distance_km": G.IN_KM + 0.1})
    assert G.describe(0, 0)["near"] is True
    monkeypatch.setattr(G, "nearest", lambda *a, **k: {"name": "X", "state": None, "country": "Y", "distance_km": G.NEAR_KM + 0.1})
    assert G.describe(0, 0) is None


def test_the_data_is_small_offline_and_licensed_with_the_credit_that_goes_with_it():
    size = Path(G.DATA).stat().st_size
    assert size < 1_000_000, "a few hundred KB, shipped with the app"
    data = G.load()
    assert len(data["names"]) > 25_000 and data["countries"]["PT"] == "Portugal" and G.ATTRIBUTION == "Place names: GeoNames (geonames.org), CC BY 4.0"
    doc = (Path(__file__).resolve().parents[1] / "docs" / "THIRD_PARTY_MODELS.md").read_text()
    assert "GeoNames" in doc and "CC BY 4.0" in doc


def test_the_lookup_has_no_way_to_reach_the_network_and_logs_nothing():
    src = (SRC / "classes" / "media_index" / "gazetteer.py").read_text()
    for banned in ("urllib", "requests", "http.client", "socket", "api_client", "websocket", "aiohttp", "httpx", "logger", "logging"):
        assert not re.search(rf"^\s*(import|from)\s+{re.escape(banned)}\b", src, re.M), f"gazetteer imports {banned}"
