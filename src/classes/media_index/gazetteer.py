"""
 @file
 @brief Place names for GPS positions, from a small offline list of cities (GeoNames, CC BY 4.0). No network, no lookups sent anywhere.

 The data (``data/places.tsv.gz``, about 34,000 places with more than 15,000 people, or capitals) is loaded once into numpy arrays and a
 position is answered by the nearest of them. A position inside a city's reach is named after it ("Lisbon, Portugal"); a position
 farther out is "near Lisbon, Portugal"; one far from any listed place (open sea, polar ice, desert) gets no name and stays a coordinate.
"""

from __future__ import annotations

import gzip
import os
import threading
from typing import Any, Dict, List, Optional

import numpy as np

DATA = os.path.join(os.path.dirname(__file__), "data", "places.tsv.gz")
EARTH_KM = 6371.0088
IN_KM = 25.0                 # within this of a listed place: "in" it
NEAR_KM = 150.0              # within this: "near" it; beyond, no name
CITY_POPULATION = 100_000    # a position within CITY_KM of a place this big is named after it, not after a neighbourhood
CITY_KM = 30.0
CITY_SCALE_KM = 10.0         # among big places, population counts for less the farther away it is: pop / (1 + km / 10)
ATTRIBUTION = "Place names: GeoNames (geonames.org), CC BY 4.0"

_lock = threading.Lock()
_loaded: Optional[Dict[str, Any]] = None


def load(path: str = DATA) -> Dict[str, Any]:
    """The place list as arrays (cached): names, country codes, state codes, radians, population, and the country names."""
    global _loaded
    with _lock:
        if _loaded is not None and path == DATA:
            return _loaded
        names: List[str] = []
        country: List[str] = []
        admin: List[str] = []
        lat: List[float] = []
        lon: List[float] = []
        pop: List[int] = []
        countries: Dict[str, str] = {}
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#country"):
                    for pair in line.rstrip("\n").split("\t")[1:]:
                        code, _, full = pair.partition("=")
                        countries[code] = full
                    continue
                c = line.rstrip("\n").split("\t")
                if len(c) < 6:
                    continue
                names.append(c[0])
                country.append(c[1])
                admin.append(c[2])
                lat.append(float(c[3]))
                lon.append(float(c[4]))
                pop.append(int(c[5] or 0))
        data = {"names": names, "country": country, "admin": admin, "lat": np.radians(np.array(lat)), "lon": np.radians(np.array(lon)),
                "pop": np.array(pop), "countries": countries}
        if path == DATA:
            _loaded = data
        return data


def distances_km(lat: float, lon: float, data: Dict[str, Any]) -> np.ndarray:
    """Great-circle distance from a position to every listed place (haversine, so the date line and the poles need no special case)."""
    p1, l1 = np.radians(float(lat)), np.radians(float(lon))
    dphi, dl = data["lat"] - p1, data["lon"] - l1
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(data["lat"]) * np.sin(dl / 2) ** 2
    return 2 * EARTH_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def nearest(lat: float, lon: float, path: str = DATA) -> Optional[Dict[str, Any]]:
    """The place to name a position after: the biggest nearby city of 100,000+ within 30 km (population discounted by distance, so a city beats its own districts), else the closest listed place.

    Returns name, country, state (US), distance in km and population; None for an invalid position.
    """
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        return None
    data = load(path)
    d = distances_km(la, lo, data)
    big = np.nonzero((data["pop"] >= CITY_POPULATION) & (d <= CITY_KM))[0]
    i = int(big[np.argmax(data["pop"][big] / (1.0 + d[big] / CITY_SCALE_KM))]) if len(big) else int(np.argmin(d))
    return {"name": data["names"][i], "country_code": data["country"][i], "country": data["countries"].get(data["country"][i], data["country"][i]),
            "state": data["admin"][i] or None, "distance_km": round(float(d[i]), 1), "population": int(data["pop"][i])}


def describe(lat: float, lon: float, path: str = DATA) -> Optional[Dict[str, Any]]:
    """A name for a position, or None when it is farther than ``NEAR_KM`` from every listed place.

    ``{"label": "Lisbon, Portugal" or "near Lisbon, Portugal", "name", "country", "state", "near": bool, "distance_km"}``.
    """
    n = nearest(lat, lon, path)
    if n is None or n["distance_km"] > NEAR_KM:
        return None
    near = n["distance_km"] > IN_KM
    where = ", ".join(x for x in (n["name"], n["state"], n["country"]) if x)
    return {"label": ("near " if near else "") + where, "name": n["name"], "country": n["country"], "state": n["state"], "near": near, "distance_km": n["distance_km"]}
