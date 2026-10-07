"""When and where a clip was shot: parsed from tags, kept on this machine, never uploaded."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).parent))

import media_fixtures as mf  # noqa: E402
from classes.media_index import cloud, library, probe as P  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

GPS_TAG = "+37.7749-122.4194+012.000/"


# ============================ parsing (no ffprobe) ============================
@pytest.mark.parametrize("text,expected", [
    ("+37.7749-122.4194+012.000/", {"lat": 37.7749, "lon": -122.4194, "alt": 12.0}),
    ("+37.7749-122.4194/", {"lat": 37.7749, "lon": -122.4194}),
    ("-33.8688+151.2093+020.5CRSWGS_84/", {"lat": -33.8688, "lon": 151.2093, "alt": 20.5}),
    ("+00.0000+000.0000/", None),            # "no fix" as many cameras write it
    ("+95.0000+010.0000/", None),            # latitude out of range
    ("+10.0000+190.0000/", None),            # longitude out of range
    ("garbage", None), ("", None), (None, None),
])
def test_iso6709_locations(text, expected):
    assert P.parse_iso6709(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("2024-05-01T14:03:09.000000Z", "2024-05-01T14:03:09+00:00"),
    ("2024-05-01T16:03:09+0200", "2024-05-01T14:03:09+00:00"),        # zone converted to UTC
    ("2024-05-01T16:03:09+02:00", "2024-05-01T14:03:09+00:00"),
    ("2024-05-01 14:03:09", "2024-05-01T14:03:09+00:00"),              # no zone: taken as UTC
    ("1970-01-01T00:00:00Z", None), ("1904-01-01T00:00:00Z", None),    # a clock that was never set
    ("2999-01-01T00:00:00Z", None), ("not a date", None), ("", None), (None, None),
])
def test_capture_times(text, expected):
    assert P.parse_capture_time(text) == expected


def test_the_apple_date_beats_the_container_date_and_stream_tags_are_searched():
    both = P.parse_capture({"creation_time": "2024-05-02T00:00:00Z", "com.apple.quicktime.creationdate": "2024-05-01T10:00:00+0200"})
    assert both["captured_at"] == "2024-05-01T08:00:00+00:00" and both["captured_source"] == "com.apple.quicktime.creationdate"
    from_stream = P.parse_capture({}, [{"creation_time": "2023-07-04T12:00:00Z"}, {"location": GPS_TAG}])
    assert from_stream["captured_at"] == "2023-07-04T12:00:00+00:00" and from_stream["gps"]["lat"] == 37.7749


def test_tag_names_are_case_insensitive_and_missing_tags_give_nothing():
    assert P.parse_capture({"Creation_Time": "2024-05-01T14:03:09Z", "LOCATION": GPS_TAG})["gps"] == {"lat": 37.7749, "lon": -122.4194, "alt": 12.0}
    assert P.parse_capture(None) == {"captured_at": None, "captured_source": None, "gps": None}
    assert P.parse_capture({"title": "x"}, [None, "bad"]) == {"captured_at": None, "captured_source": None, "gps": None}


def test_parse_probe_carries_the_capture_block():
    out = P.parse_probe({"streams": [{"codec_type": "video", "width": 64, "height": 48, "avg_frame_rate": "30/1"}],
                         "format": {"duration": "2", "tags": {"location": GPS_TAG, "creation_time": "2024-05-01T14:03:09Z"}}})
    assert out["capture"]["gps"]["lon"] == -122.4194 and out["capture"]["captured_at"].startswith("2024-05-01T14")


# ============================ a real tagged file ============================
@pytest.fixture(scope="module")
def tagged(tmp_path_factory):
    ff = mf.need_ffmpeg()
    path = str(tmp_path_factory.mktemp("cap") / "trip.mp4")
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=640x360:rate=15", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=2", "-shortest", "-c:v", "libx264", "-c:a", "aac",
                    "-metadata", f"location={GPS_TAG}", "-metadata", "creation_time=2024-05-01T14:03:09Z", path], check=True)
    return path


def test_ffprobe_reads_the_tags_of_a_real_file(tagged):
    capture = P.probe_media(tagged)["capture"]
    assert capture["gps"] == {"lat": 37.7749, "lon": -122.4194, "alt": 12.0}
    assert capture["captured_at"] == "2024-05-01T14:03:09+00:00"


def test_the_upload_proxy_carries_no_location_or_date_tags(tagged, tmp_path):
    """The proxy goes to Gemini: the clip's GPS and date must not travel with it."""
    out = str(tmp_path / "proxy.mp4")
    ok, err = cloud.make_proxy(tagged, P.probe_media(tagged), out)
    assert ok, err
    raw = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", out],
                         capture_output=True, text=True, check=True).stdout
    assert GPS_TAG not in raw and "37.7749" not in raw and "2024-05-01" not in raw
    assert P.probe_media(out)["capture"]["gps"] is None


def test_a_keyframe_carries_no_tags_either(tagged, tmp_path):
    out = str(tmp_path / "k.jpg")
    assert cloud.extract_keyframe(tagged, 0.5, out)
    assert b"37.7749" not in Path(out).read_bytes()


# ============================ shelf and loading ============================
def test_capture_is_kept_in_the_manifest_and_loaded_with_the_file(tmp_path):
    library.clear_cache()
    shelf = Shelf(str(tmp_path / "s"))
    sha = "9" * 64
    shelf.set_source(sha, duration=2.0, media_type="video", captured_at="2024-05-01T14:03:09+00:00",
                     captured_source="creation_time", gps={"lat": 37.7749, "lon": -122.4194})
    shelf.write_json(sha, "structure.json", {"shots": [{"id": 0, "start": 0.0, "end": 2.0}]})
    shelf.set_layer(sha, "structure", version=1, status="ready")
    fi = library.load_file_index(shelf, sha, file_id="F")
    assert fi.captured_at == "2024-05-01T14:03:09+00:00" and fi.gps == {"lat": 37.7749, "lon": -122.4194}


def test_nothing_sent_to_the_cloud_contains_the_location(tagged, tmp_path):
    """The upload is the proxy file; the calls carry shots, transcript text and pictures. None may hold the GPS or date."""
    from test_media_index_cloud import FakeClient
    shelf = Shelf(str(tmp_path / "s"))
    sha = "8" * 64
    probe = P.probe_media(tagged)
    assert probe["capture"]["gps"], "the fixture must really be tagged"
    shelf.set_source(sha, duration=2.0, media_type="video", gps=probe["capture"]["gps"], captured_at=probe["capture"]["captured_at"])
    shelf.write_json(sha, "structure.json", {"shots": [{"id": 0, "start": 0.0, "end": 2.0, "motion": {"class": "static", "level": 0.1}}]})
    shelf.set_layer(sha, "structure", version=1, status="ready")
    client = FakeClient(watch={"shots": [{"id": 0, "start": 0.0, "end": 2.0, "description": "A test pattern", "actions": [],
                                          "objects": [], "on_screen_text": [], "sound_events": [], "mood": "", "shot_type": "wide"}]})
    uploaded = []

    def uploader(path, url, mime_type="video/mp4"):
        uploaded.append(Path(path).read_bytes())          # the file as it leaves this machine
        return {"name": "files/abc", "uri": "https://gemini/files/abc"}, ""

    out = cloud.compute_cloud(client, tagged, probe, sha, shelf, file_id="F", media_type="video", uploader=uploader)
    assert out["layers"]["watch"] == "ready" and len(uploaded) == 1
    assert b"37.7749" not in uploaded[0] and b"2024-05-01" not in uploaded[0]
    sent = json.dumps(client.calls, default=str)
    assert "37.7749" not in sent and "122.4194" not in sent and "2024-05-01" not in sent
