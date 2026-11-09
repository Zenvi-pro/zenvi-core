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


def test_assets_token_falls_back_to_sibling_folder(tmp_path):
    folder = tmp_path / "edit"
    folder.mkdir()
    (folder / "A_assets" / "title").mkdir(parents=True)
    svg = folder / "A_assets" / "title" / "x.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    copied = str(folder / "B.zvn")
    resolved = path_utils.absolute_media_path("@assets/title/x.svg", copied)
    assert resolved == os.path.normpath(str(svg))


def test_assets_token_skips_empty_new_folder_and_finds_sibling(tmp_path):
    folder = tmp_path / "edit"
    (folder / "A_assets" / "title").mkdir(parents=True)
    (folder / "B_assets" / "title").mkdir(parents=True)
    svg = folder / "A_assets" / "title" / "x.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    copied = str(folder / "B.zvn")
    resolved = path_utils.absolute_media_path("@assets/title/x.svg", copied)
    assert resolved == os.path.normpath(str(svg))


def test_assets_token_windows_slash_in_suffix(tmp_path):
    folder = tmp_path / "edit"
    (folder / "A_assets" / "title").mkdir(parents=True)
    svg = folder / "A_assets" / "title" / "x.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    copied = str(folder / "B.zvn")
    resolved = path_utils.absolute_media_path("@assets\\title\\x.svg", copied)
    assert resolved == os.path.normpath(str(svg))


def test_assets_token_missing_stays_missing(tmp_path):
    folder = tmp_path / "edit"
    folder.mkdir()
    copied = str(folder / "B.zvn")
    resolved = path_utils.absolute_media_path("@assets/title/gone.svg", copied)
    expected = os.path.normpath(str(folder / "B_assets" / "title" / "gone.svg"))
    assert resolved == expected
    assert not os.path.exists(resolved)


def test_opening_a_copy_does_not_create_assets_folder(tmp_path):
    from classes.assets import get_assets_path

    folder = tmp_path / "edit"
    folder.mkdir()
    copied = str(folder / "B.zvn")
    get_assets_path(copied, create_paths=False)
    assert not (folder / "B_assets").exists()
