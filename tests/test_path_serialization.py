"""External media paths stay absolute on save; in-project paths stay relative."""

import json
import os

from classes import info
from classes.json_data import JsonDataStore, path_context


class _PathStore(JsonDataStore):
    def __init__(self):
        # Skip JsonDataStore.__init__ (needs Qt app); we only need path helpers.
        pass


def _project_paths(tmp_path):
    project_dir = tmp_path / "MyProject"
    project_dir.mkdir()
    project_file = str(project_dir / "MyProject.zvn")
    assets = project_dir / "MyProject_assets"
    assets.mkdir()
    (assets / "media").mkdir()
    return project_dir, project_file, assets


def _round_trip(store, project_file, data):
    contents = json.dumps(data, ensure_ascii=False, indent=1)
    relative = store.convert_paths_to_relative(project_file, None, contents)
    absolute = store.convert_paths_to_absolute(project_file, relative)
    return json.loads(relative), json.loads(absolute)


def test_external_absolute_path_stays_absolute(tmp_path):
    project_dir, project_file, _assets = _project_paths(tmp_path)
    external = tmp_path / "Downloads" / "clip.mp4"
    external.parent.mkdir()
    external.write_bytes(b"x")

    store = _PathStore()
    relative, absolute = _round_trip(
        store, project_file, {"path": str(external)}
    )

    assert relative["path"] == str(external).replace("\\", "/")
    assert not relative["path"].startswith("..")
    assert os.path.normpath(absolute["path"]) == os.path.normpath(str(external))


def test_path_inside_project_folder_becomes_relative(tmp_path):
    project_dir, project_file, _assets = _project_paths(tmp_path)
    inside = project_dir / "footage" / "clip.mp4"
    inside.parent.mkdir()
    inside.write_bytes(b"x")

    store = _PathStore()
    relative, absolute = _round_trip(
        store, project_file, {"path": str(inside)}
    )

    assert relative["path"] == "footage/clip.mp4"
    assert os.path.normpath(absolute["path"]) == os.path.normpath(str(inside))


def test_path_in_project_subdir_becomes_relative(tmp_path):
    project_dir, project_file, _assets = _project_paths(tmp_path)
    nested = project_dir / "a" / "b" / "c.mp4"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")

    store = _PathStore()
    relative, absolute = _round_trip(
        store, project_file, {"path": str(nested)}
    )

    assert relative["path"] == "a/b/c.mp4"
    assert os.path.normpath(absolute["path"]) == os.path.normpath(str(nested))


def test_assets_token_unchanged(tmp_path):
    project_dir, project_file, assets = _project_paths(tmp_path)
    media = assets / "media" / "gen.mp4"
    media.write_bytes(b"x")

    store = _PathStore()
    relative, absolute = _round_trip(
        store, project_file, {"path": str(media)}
    )

    assert relative["path"].startswith("@assets/")
    assert os.path.normpath(absolute["path"]) == os.path.normpath(str(media))


def test_thumbnail_token_unchanged(tmp_path, monkeypatch):
    project_dir, project_file, assets = _project_paths(tmp_path)
    thumb_dir = assets / "thumbnail"
    thumb_dir.mkdir()
    thumb = thumb_dir / "abc.png"
    thumb.write_bytes(b"x")
    monkeypatch.setattr(info, "THUMBNAIL_PATH", str(thumb_dir))

    store = _PathStore()
    relative, _absolute = _round_trip(
        store, project_file, {"image": str(thumb)}
    )

    assert relative["image"] == "thumbnail/abc.png"


def test_transitions_colors_emojis_tokens(tmp_path, monkeypatch):
    project_dir, project_file, _assets = _project_paths(tmp_path)
    app_root = tmp_path / "app"
    transitions = app_root / "transitions" / "common"
    transitions.mkdir(parents=True)
    trans_file = transitions / "fade.svg"
    trans_file.write_bytes(b"x")

    colors = tmp_path / "colors"
    colors.mkdir()
    color_file = colors / "red.png"
    color_file.write_bytes(b"x")

    emojis = app_root / "emojis" / "color" / "svg"
    emojis.mkdir(parents=True)
    emoji_file = emojis / "grin.svg"
    emoji_file.write_bytes(b"x")

    monkeypatch.setattr(info, "PATH", str(app_root))
    monkeypatch.setattr(info, "COLORS_PATH", str(colors))

    store = _PathStore()
    relative, _absolute = _round_trip(
        store,
        project_file,
        {
            "path": str(trans_file),
            "image": str(color_file),
            "lut_path": str(emoji_file),
        },
    )

    assert relative["path"].startswith("@transitions/")
    assert relative["image"].startswith("@colors/")
    assert relative["lut_path"].startswith("@emojis/")


def test_relative_in_memory_path_joins_project_folder_not_cwd(tmp_path, monkeypatch):
    project_dir, project_file, _assets = _project_paths(tmp_path)
    inside = project_dir / "local.mp4"
    inside.write_bytes(b"x")

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    store = _PathStore()
    # Simulate a relative path already in memory (joined against project, not CWD).
    path_context["new_project_folder"] = str(project_dir)
    path_context["new_project_assets"] = str(project_dir / "MyProject_assets")
    match_path = "local.mp4"
    contents = json.dumps({"path": match_path}, ensure_ascii=False)
    relative = store.convert_paths_to_relative(project_file, None, contents)
    data = json.loads(relative)
    assert data["path"] == "local.mp4"


def test_windows_cross_drive_stays_absolute(tmp_path, monkeypatch):
    """Drive mismatch still keeps the absolute path (Windows semantics)."""
    project_dir, project_file, _assets = _project_paths(tmp_path)
    # Simulate splitdrive returning different drive letters.
    store = _PathStore()
    path_context["new_project_folder"] = str(project_dir)
    path_context["new_project_assets"] = str(project_dir / "MyProject_assets")

    real_splitdrive = os.path.splitdrive

    def fake_splitdrive(p):
        p_norm = os.path.normpath(str(p)).replace("\\", "/")
        if p_norm.startswith("/Volumes/External") or "D:" in str(p):
            return ("D:", p_norm)
        return ("C:", p_norm) if project_dir.as_posix() in p_norm or p_norm == str(project_dir).replace("\\", "/") else real_splitdrive(p)

    # Use a path that our fake treats as D:
    external = "/Volumes/External/foo.mp4"
    monkeypatch.setattr(os.path, "splitdrive", fake_splitdrive)

    contents = json.dumps({"path": external}, ensure_ascii=False)
    relative = store.convert_paths_to_relative(project_file, None, contents)
    data = json.loads(relative)
    assert data["path"].replace("\\", "/").endswith("/Volumes/External/foo.mp4")
    assert not data["path"].startswith("..")
