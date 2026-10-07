"""The index travels with Collect Media and comes back when the project opens."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index import project_sync  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402

FP = "e" * 64
FP2 = "f" * 64
GOOD = {"analyzed": True, "short_summary": "a street at night", "chapters": [{"start": 0, "end": 4}]}


@pytest.fixture
def shelves(tmp_path, monkeypatch):
    here = Shelf(str(tmp_path / "here"))
    monkeypatch.setattr(project_sync, "default_shelf", lambda: here)
    return here, tmp_path


def _project(tmp_path, name="trip"):
    project = tmp_path / "projects" / f"{name}.zvn"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text("{}")
    return str(project)


def test_collect_copies_each_files_index_next_to_the_project(shelves):
    here, tmp_path = shelves
    here.save_v1_index(FP, GOOD, duration=9.0)
    project = _project(tmp_path)
    files = [{"id": "a", "fingerprint": {"sha256": FP}}, {"id": "b", "fingerprint": {"sha256": FP2}},
             {"id": "c"}, {"id": "d", "fingerprint": None}]

    assert project_sync.export_index_for_files(files, project) == [FP]
    assert os.path.isfile(tmp_path / "projects" / "trip_assets" / "index" / FP / "manifest.json")


def test_a_project_opened_on_another_machine_restores_its_index(shelves, monkeypatch):
    here, tmp_path = shelves
    here.save_v1_index(FP, GOOD, duration=9.0)
    project = _project(tmp_path)
    project_sync.export_index_for_files([{"fingerprint": {"sha256": FP}}], project)

    there = Shelf(str(tmp_path / "there"))
    # "Another machine": same project folder, a different, empty shelf.
    monkeypatch.setattr(project_sync, "default_shelf", lambda: there)
    assert project_sync.import_project_index(project) == [FP]
    assert there.load_v1_index(FP)["ai_metadata"]["short_summary"] == "a street at night"


def test_opening_a_project_without_an_index_does_nothing(shelves):
    _here, tmp_path = shelves
    project = _project(tmp_path, "plain")
    assert project_sync.import_project_index(project) == []
    assert project_sync.import_project_index("") == []
    assert project_sync.import_project_index(str(tmp_path / "missing.zvn")) == []


def test_no_thread_is_started_when_there_is_nothing_to_import(shelves, monkeypatch):
    _here, tmp_path = shelves
    started = []
    monkeypatch.setattr(project_sync.threading, "Thread", lambda *a, **k: started.append(k) or pytest.fail("thread"))
    project_sync.import_project_index_in_background(_project(tmp_path, "plain"))
    project_sync.import_project_index_in_background("")
    assert started == []


def test_the_background_import_runs_off_the_calling_thread(shelves, monkeypatch):
    here, tmp_path = shelves
    here.save_v1_index(FP, GOOD, duration=9.0)
    project = _project(tmp_path)
    project_sync.export_index_for_files([{"fingerprint": {"sha256": FP}}], project)

    there = Shelf(str(tmp_path / "there"))
    monkeypatch.setattr(project_sync, "default_shelf", lambda: there)
    project_sync.import_project_index_in_background(project)
    for _ in range(100):
        if there.has_entry(FP):
            break
        time.sleep(0.02)
    assert there.has_entry(FP)


def test_failures_never_escape(shelves, monkeypatch):
    here, tmp_path = shelves
    here.save_v1_index(FP, GOOD, duration=9.0)
    project = _project(tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(here, "export_entries", boom)
    monkeypatch.setattr(here, "import_entries", boom)
    assert project_sync.export_index_for_files([{"fingerprint": {"sha256": FP}}], project) == []
    os.makedirs(project_sync.project_index_dir(project, create=True), exist_ok=True)
    assert project_sync.import_project_index(project) == []


def test_collect_media_carries_the_index_along(shelves, tmp_path):
    """The real collect helper, not just the sync module."""
    here, tmp_path = shelves
    here.save_v1_index(FP, GOOD, duration=9.0)
    project = _project(tmp_path)
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x" * 100)
    files = [{"id": "a", "path": str(media), "fingerprint": {"sha256": FP}}]

    from classes.media_collect import copy_media_into_project

    copied, skipped, errors, moves = copy_media_into_project(files, project)
    assert errors == [] and len(copied) == 1 and len(moves) == 1
    assert os.path.isfile(tmp_path / "projects" / "trip_assets" / "index" / FP / "v1_index.json")
