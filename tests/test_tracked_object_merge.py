"""ProjectDataStore._set deep-merges tracked-object ("objects") updates.

Ported from upstream OpenShot's src/tests/test_project_data.py
(test_set_deep_merges_tracked_object_updates, PR #6047): a narrow update for
one tracked object must not drop the other objects or the untouched
properties of the object being edited.
"""

import os
import sys

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


def test_set_deep_merges_tracked_object_updates():
    store = _make_store()
    store._data = {
        "clips": [
            {
                "id": "C1",
                "effects": [
                    {
                        "id": "E1",
                        "name": "Object Detector",
                        "objects": {
                            "E1-0": {
                                "delta_x": {"Points": []},
                                "delta_y": {"Points": []},
                            },
                            "E1-1": {
                                "delta_x": {"Points": []},
                                "delta_y": {"Points": []},
                            },
                        },
                    }
                ],
            }
        ]
    }

    store._set(
        ["clips", {"id": "C1"}, "effects", {"id": "E1"}],
        {"objects": {"E1-1": {"delta_x": {"Points": [{"co": {"X": 5, "Y": 0.25}}]}}}},
    )

    objects = store._data["clips"][0]["effects"][0]["objects"]
    assert "E1-0" in objects
    assert objects["E1-1"]["delta_y"] == {"Points": []}
    assert objects["E1-1"]["delta_x"]["Points"][0]["co"] == {"X": 5, "Y": 0.25}


def test_set_adds_new_tracked_object_without_touching_siblings():
    store = _make_store()
    store._data = {
        "clips": [
            {"id": "C1", "effects": [{"id": "E1", "objects": {"E1-0": {"visible": {"Points": [1]}}}}]}
        ]
    }

    store._set(
        ["clips", {"id": "C1"}, "effects", {"id": "E1"}],
        {"objects": {"all": {"visible": {"Points": [0]}}}},
    )

    objects = store._data["clips"][0]["effects"][0]["objects"]
    assert objects["E1-0"] == {"visible": {"Points": [1]}}
    assert objects["all"] == {"visible": {"Points": [0]}}
