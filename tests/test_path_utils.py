"""Media path resolution (classes.path_utils)."""

import os

import pytest

from classes import path_utils


@pytest.mark.parametrize("path", ["/media/a.mp4", r"\media\a.mp4"])
def test_a_rooted_path_is_absolute_without_asking_for_the_project(path, monkeypatch):
    """Python 3.13+ os.path.isabs() no longer counts "/media/a.mp4" as absolute
    on Windows, so it was treated as project-relative and resolved through
    get_app(). It is a path on the current drive, whatever the project is."""
    def no_app():
        raise AssertionError("a rooted path must not be resolved against the project")

    monkeypatch.setattr(path_utils, "get_app", no_app)
    expected = os.path.abspath("/media/a.mp4")
    assert path_utils.absolute_media_path(path) == expected
    assert path_utils.comparable_media_path(path) == os.path.normcase(expected)


def test_a_relative_path_still_resolves_inside_the_project(tmp_path):
    project = str(tmp_path / "proj" / "cut.zvn")
    assert path_utils.absolute_media_path("clips/a.mp4", project) == os.path.normpath(
        os.path.join(str(tmp_path / "proj"), "clips", "a.mp4"))
