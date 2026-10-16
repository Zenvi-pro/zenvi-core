"""Build src/classes/media_index/data/places.tsv.gz from GeoNames (CC BY 4.0, https://www.geonames.org).

    python scripts/build_gazetteer.py            # downloads cities15000.zip and countryInfo.txt, writes the data file

The data file is committed; this script exists so it can be rebuilt and checked. Rows: name, country code, state code (US only),
latitude, longitude, population, one per place with more than 15,000 people (or a capital). The first lines list country codes and names.
"""

from __future__ import annotations

import gzip
import io
import os
import urllib.request
import zipfile

BASE = "https://download.geonames.org/export/dump/"
OUT = os.path.join(os.path.dirname(__file__), "..", "src", "classes", "media_index", "data", "places.tsv.gz")


def fetch(name: str) -> bytes:
    with urllib.request.urlopen(BASE + name, timeout=120) as resp:
        return resp.read()


def build() -> bytes:
    countries = {}
    for line in fetch("countryInfo.txt").decode("utf-8").splitlines():
        if line and not line.startswith("#"):
            cols = line.split("\t")
            countries[cols[0]] = cols[4]
    with zipfile.ZipFile(io.BytesIO(fetch("cities15000.zip"))) as z:
        rows = z.read("cities15000.txt").decode("utf-8").splitlines()
    out = ["#country\t" + "\t".join(f"{k}={v}" for k, v in sorted(countries.items()) if k)]
    for row in rows:
        c = row.split("\t")
        name, lat, lon, cc, admin1, pop = c[1], float(c[4]), float(c[5]), c[8], c[10], c[14]
        out.append("\t".join([name.replace("\t", " "), cc, admin1 if cc == "US" else "", f"{lat:.4f}", f"{lon:.4f}", pop or "0"]))
    return gzip.compress(("\n".join(out) + "\n").encode("utf-8"), 9, mtime=0)


if __name__ == "__main__":
    data = build()
    with open(OUT, "wb") as fh:
        fh.write(data)
    print(f"wrote {len(data) / 1000:.0f} KB to {os.path.normpath(OUT)}")
