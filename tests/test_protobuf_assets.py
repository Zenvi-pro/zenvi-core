"""Object Mask / Object Detector protobuf files follow the project into its assets folder.

Ported from upstream OpenShot's src/tests/test_assets.py and
src/tests/test_project_data.py additions in PR #6050, adapted to Zenvi's
move_temp_paths_to_project_folder (no thumbnail/proxy copying).
"""

import os
import sys
from unittest.mock import patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _make_store():
    from classes import project_data as pd

    store = pd.ProjectDataStore.__new__(pd.ProjectDataStore)
    store._data = {}
    store.has_unsaved_changes = False
    store.current_filepath = None
    return store


def test_get_assets_path_creates_protobuf_data_folder(tmp_path):
    from classes.assets import get_assets_path

    asset_path = get_assets_path(str(tmp_path / "example.zvn"), create_paths=True)

    assert os.path.isdir(os.path.join(asset_path, "protobuf_data"))


def test_move_temp_paths_copies_effect_protobuf_from_stale_path(tmp_path):
    from classes import project_data as pd

    store = _make_store()
    stale_proto_root = tmp_path / "old-runtime-protobuf"
    current_proto_root = tmp_path / "current-project-protobuf"
    roots = {
        name: tmp_path / name
        for name in ("thumbs", "titles", "blender", "clipboard")
    }
    for folder in [stale_proto_root, current_proto_root, *roots.values()]:
        folder.mkdir()

    stale_proto = stale_proto_root / "E1.data"
    stale_proto.write_text("object-mask-data", encoding="utf-8")

    project_path = str(tmp_path / "example.zvn")
    asset_path = str(tmp_path / "example_assets")
    store._data = {
        "files": [],
        "effects": [{"id": "T1", "protobuf_data_path": str(stale_proto)}],
        "clips": [
            {
                "id": "C1",
                "file_id": "",
                "reader": {},
                "effects": [{"id": "E1", "protobuf_data_path": str(stale_proto)}],
            },
        ],
    }

    with patch("classes.project_data.get_assets_path", lambda path, create_paths=True: asset_path), \
            patch("classes.project_data.info.THUMBNAIL_PATH", str(roots["thumbs"])), \
            patch("classes.project_data.info.TITLE_PATH", str(roots["titles"])), \
            patch("classes.project_data.info.BLENDER_PATH", str(roots["blender"])), \
            patch("classes.project_data.info.PROTOBUF_DATA_PATH", str(current_proto_root)), \
            patch("classes.project_data.info.CLIPBOARD_PATH", str(roots["clipboard"])):
        pd.ProjectDataStore.move_temp_paths_to_project_folder(store, project_path)

    expected_proto = os.path.join(asset_path, "protobuf_data", "E1.data")
    assert os.path.exists(expected_proto)
    assert store._data["effects"][0]["protobuf_data_path"] == expected_proto
    assert store._data["clips"][0]["effects"][0]["protobuf_data_path"] == expected_proto
