"""The media index shelf: safe keys, atomic files, layers, and project round-trips."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index import store as store_mod  # noqa: E402
from classes.media_index.store import LAYER_V1, Shelf, sha_of  # noqa: E402

FP = "a" * 64
FP2 = "b" * 64
GOOD = {"analyzed": True, "short_summary": "a street at night", "chapters": [{"start": 0, "end": 4}],
        "index": {"status": "ready", "index_id": "zenvi-p1", "video_id": "f1"}}


@pytest.fixture
def shelf(tmp_path):
    return Shelf(str(tmp_path / "media_index"))


# -- keys ------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", None, "..", "../x", "/etc/passwd", "a" * 63, "a" * 65, "G" * 64,
                                 {"sha256": "../../" + "a" * 58}, {"size": 3}, 7])
def test_only_a_real_sha256_is_a_key(bad):
    assert sha_of(bad) == ""


def test_a_sha_is_normalised_from_a_fingerprint_dict_or_string():
    assert sha_of({"sha256": FP.upper()}) == FP
    assert sha_of(FP) == FP


def test_an_unsafe_key_never_touches_the_disk(shelf):
    assert shelf.entry_dir("../../etc", create=True) == ""
    assert shelf.write_json("../../etc", "x.json", {}) is False
    assert not os.path.exists(shelf.root)


def test_file_names_are_confined_to_the_entry(shelf):
    for name in ("../escape.json", "sub/x.json", "x.txt", "", ".hidden.json"):
        assert shelf.write_json(FP, name, {"a": 1}) is False
    assert shelf.read_json(FP, "../manifest.json") is None


# -- files -----------------------------------------------------------------------
def test_json_round_trips_and_leaves_no_partial_files(shelf):
    assert shelf.write_json(FP, "facts.json", {"shots": [1, 2, 3], "name": "café"})
    assert shelf.read_json(FP, "facts.json") == {"shots": [1, 2, 3], "name": "café"}
    assert [n for n in os.listdir(shelf.entry_dir(FP)) if n.endswith(".partial")] == []


def test_a_failed_write_keeps_the_old_file_and_cleans_up(shelf, monkeypatch):
    shelf.write_json(FP, "facts.json", {"v": 1})

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    assert shelf.write_json(FP, "facts.json", {"v": 2}) is False
    monkeypatch.undo()
    assert shelf.read_json(FP, "facts.json") == {"v": 1}
    assert [n for n in os.listdir(shelf.entry_dir(FP)) if n.endswith(".partial")] == []


def test_a_corrupt_file_reads_as_missing_not_as_a_crash(shelf):
    shelf.write_json(FP, "facts.json", {"v": 1})
    Path(shelf.entry_dir(FP), "facts.json").write_text("{not json")
    assert shelf.read_json(FP, "facts.json") is None


# -- layers ----------------------------------------------------------------------
def test_layers_track_version_and_status(shelf):
    assert not shelf.layer_ready(FP, "facts")
    assert shelf.set_layer(FP, "facts", version=2, status="ready", model="m")
    assert shelf.layer_ready(FP, "facts")
    assert shelf.layer_ready(FP, "facts", version=2)
    assert not shelf.layer_ready(FP, "facts", version=3), "a layer from an older algorithm is not current"
    shelf.set_layer(FP, "facts", version=2, status="failed")
    assert not shelf.layer_ready(FP, "facts")


def test_layer_names_are_validated(shelf):
    for bad in ("", "../x", "Facts", "a b", "1x"):
        assert shelf.set_layer(FP, bad, version=1, status="ready") is False


def test_concurrent_layer_updates_do_not_lose_each_other(shelf):
    names = ["layer%02d" % i for i in range(24)]
    barrier = threading.Barrier(len(names))

    def work(name):
        barrier.wait()
        assert shelf.set_layer(FP, name, version=1, status="ready")

    threads = [threading.Thread(target=work, args=(n,)) for n in names]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(shelf.manifest(FP)["layers"]) == names


# -- the v1 index ------------------------------------------------------------------
def test_a_finished_v1_analysis_is_kept_with_what_it_was_made_from(shelf):
    assert shelf.save_v1_index(FP, GOOD, duration=12.5, media_type="video", project_id="p1", file_id="f1")
    saved = shelf.load_v1_index(FP)
    assert saved["ai_metadata"] == GOOD
    assert saved["source"] == {"duration": 12.5, "media_type": "video", "project_id": "p1", "file_id": "f1"}
    assert shelf.manifest(FP)["source"]["duration"] == 12.5
    assert shelf.layer_ready(FP, LAYER_V1, version=1)


@pytest.mark.parametrize("meta", [None, {}, {"analyzed": False}, {"error": "boom"}, "text"])
def test_an_unfinished_or_failed_analysis_is_never_kept(shelf, meta):
    assert shelf.save_v1_index(FP, meta) is False
    assert shelf.load_v1_index(FP) is None
    assert not shelf.has_entry(FP)


def test_a_v1_entry_whose_layer_is_not_ready_is_not_served(shelf):
    shelf.save_v1_index(FP, GOOD)
    shelf.set_layer(FP, LAYER_V1, version=1, status="failed")
    assert shelf.load_v1_index(FP) is None


def test_entries_are_independent_per_content(shelf):
    shelf.save_v1_index(FP, GOOD)
    other = dict(GOOD, short_summary="a beach")
    shelf.save_v1_index(FP2, other)
    assert shelf.load_v1_index(FP)["ai_metadata"]["short_summary"] == "a street at night"
    assert shelf.load_v1_index(FP2)["ai_metadata"]["short_summary"] == "a beach"
    assert shelf.list_entries() == [FP, FP2]


# -- shelf <-> project ---------------------------------------------------------------
def test_collect_then_open_on_another_machine_restores_the_index(shelf, tmp_path):
    shelf.save_v1_index(FP, GOOD, duration=9.0)
    project_dir = tmp_path / "proj_assets" / "index"
    assert shelf.export_entries([FP, FP2, "../bad"], str(project_dir)) == [FP], "only real, existing entries are copied"

    elsewhere = Shelf(str(tmp_path / "other_machine" / "media_index"))
    assert elsewhere.import_entries(str(project_dir)) == [FP]
    assert elsewhere.load_v1_index(FP)["ai_metadata"] == GOOD
    assert elsewhere.manifest(FP)["source"]["duration"] == 9.0


def test_importing_never_replaces_a_newer_layer_with_an_older_one(shelf, tmp_path):
    shelf.save_v1_index(FP, dict(GOOD, short_summary="old"))
    project_dir = tmp_path / "proj_assets" / "index"
    shelf.export_entries([FP], str(project_dir))
    # The shelf moves on (a re-analysis); the project copy is now stale.
    shelf.save_v1_index(FP, dict(GOOD, short_summary="new"))
    assert shelf.import_entries(str(project_dir)) == []
    assert shelf.load_v1_index(FP)["ai_metadata"]["short_summary"] == "new"


def test_importing_fills_in_layers_the_shelf_lacks(shelf, tmp_path):
    src = Shelf(str(tmp_path / "src"))
    src.save_v1_index(FP, GOOD)
    src.write_json(FP, "facts.json", {"shots": 3})
    src.set_layer(FP, "facts", version=1, status="ready")
    shelf.save_v1_index(FP, GOOD)  # the shelf already has v1 (same timestamp or newer)
    assert shelf.import_entries(str(tmp_path / "src")) == [FP]
    assert shelf.read_json(FP, "facts.json") == {"shots": 3}
    assert shelf.layer_ready(FP, "facts")


def test_importing_ignores_junk_in_the_project_folder(shelf, tmp_path):
    root = tmp_path / "idx"
    (root / "notasha").mkdir(parents=True)
    (root / FP).mkdir()  # a folder without a manifest
    (root / "readme.txt").write_text("x")
    assert shelf.import_entries(str(root)) == []
    assert shelf.list_entries() == []
    assert shelf.import_entries(str(tmp_path / "missing")) == []


def test_the_default_shelf_follows_the_user_path(tmp_path, monkeypatch):
    from classes import info

    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "a"), raising=False)
    first = store_mod.default_shelf()
    assert first.root == str(tmp_path / "a" / "media_index")
    monkeypatch.setattr(info, "USER_PATH", str(tmp_path / "b"), raising=False)
    assert store_mod.default_shelf().root == str(tmp_path / "b" / "media_index")


def test_manifest_is_valid_json_with_a_schema_version(shelf):
    shelf.set_layer(FP, "facts", version=1, status="ready")
    raw = json.loads(Path(shelf.entry_dir(FP), "manifest.json").read_text())
    assert raw["schema_version"] == store_mod.SCHEMA_VERSION
